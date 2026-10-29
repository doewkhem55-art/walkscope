"""Reuse forward measured route pairs before submitting any missing targets.

HTTP caches are read through the existing Baidu parsers with a cache-only
transport. Pair files retain exact input coordinates; caller labels are rebound
on return. The wrapped route callback keeps sole ownership of HTTP and budgets.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.modules.routing.service import _public_route, _summary, _unavailable
from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.routing import BaiduRoutingError, BaiduRoutingProvider, _format_point
from app.schemas.common import Meta, WarningItem
from app.schemas.routing import RouteResult, RouteStatus, RoutingData, RoutingResult, RoutingSuccess
from tools.bounded_baidu import _atomic_json


class CacheMiss(Exception):
    """The requested successful HTTP response is absent; never a quota refusal."""


class CacheValidationError(ValueError):
    """A cache hit is inconsistent or unsafe to reuse."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        raise CacheValidationError('Route cache entry cannot be read or validated.') from None


class _CacheOnlyTransport:
    def __init__(self, folder):
        self.folder = folder

    def __call__(self, url, timeout):
        parts = urlsplit(url)
        query = urlencode([(name, value) for name, value in parse_qsl(parts.query) if name != 'ak'])
        filename = hashlib.sha256((parts.path + '?' + query).encode()).hexdigest() + '.json'
        payload = _read(self.folder / filename)
        if payload is None:
            raise CacheMiss()
        if not isinstance(payload, dict):
            raise CacheValidationError('Route HTTP cache must contain a response object.')
        if type(payload.get('status')) is not int or payload['status'] != 0:
            raise CacheMiss()
        return payload


class CacheFirstRouting:
    def __init__(self, route, cache_dirs, pair_cache_dir, *, observation_policy='strict', request_batch_size=25):
        if observation_policy not in ('strict', 'isochrone_conservative'):
            raise ValueError('Unknown measured-route observation policy.')
        self._route = route
        self.cache_dirs = tuple(Path(folder) for folder in cache_dirs)
        self.pair_cache_dir = Path(pair_cache_dir)
        self.observation_policy = observation_policy
        if type(request_batch_size) is not int or not 1 <= request_batch_size <= 25:
            raise ValueError('Request batch size must be between 1 and 25.')
        self.request_batch_size = request_batch_size
        self._observations = {}
        self._duration_variants, self._distance_variants = set(), set()
        self._cached_target_uses = self._submitted_targets = 0

    def snapshot(self):
        return dict(observation_policy=self.observation_policy,
            cached_target_uses=self._cached_target_uses, submitted_targets=self._submitted_targets,
            duration_variant_pair_count=len(self._duration_variants),
            distance_variant_pair_count=len(self._distance_variants))

    @staticmethod
    def _identity(request, target):
        return dict(origin=request.origin.model_dump(), destination=target.location.model_dump(),
                    travel_mode=request.travel_mode.value)

    @staticmethod
    def _provider_identity(request, target):
        return dict(origin=_format_point(request.origin), destination=_format_point(target.location),
                    coord_type='bd09ll', travel_mode=request.travel_mode.value)

    def _bind(self, request, target):
        # Six-decimal HTTP requests cannot distinguish closer raw coordinates.
        # Bind their exact source identity before lookup or HTTP, including when
        # the callback later stops part-way through a batch.
        identity = self._identity(request, target)
        provider_identity = self._provider_identity(request, target)
        path = self.pair_cache_dir / 'provider-identities' / (_hash(provider_identity) + '.json')
        previous = _read(path)
        if previous is not None and previous != identity:
            raise CacheValidationError('Provider-rounded route identity belongs to a different exact point pair.')
        if previous is None:
            _atomic_json(path, identity)

    def _pair(self, request, target):
        identity = self._identity(request, target)
        path = self.pair_cache_dir / (_hash(identity) + '.json')
        record = _read(path)
        if record is None:
            return path, None
        if (not isinstance(record, dict) or record.get('identity') != identity
                or record.get('provider_identity') != self._provider_identity(request, target)
                or record.get('status') != 'success'):
            raise CacheValidationError('Measured route cache has a different identity or status.')
        try:
            observations = record.get('observations', [dict(distance_m=record['distance_m'],
                duration_s=record['duration_s'], source=record['source'])])
            route = self._select(request, target, observations)
            if (record['distance_m'], record['duration_s']) != (route.distance_m, route.duration_s):
                raise CacheValidationError('Saved selection differs from the complete measured observation.')
        except (KeyError, TypeError):
            raise CacheValidationError('Measured route cache has invalid distance or duration.') from None
        self._observations[_hash(identity)] = observations
        return path, route

    def _select(self, request, target, observations):
        if not isinstance(observations, list) or not observations:
            raise CacheValidationError('Measured route observations must be a nonempty list.')
        for observation in observations:
            if (not isinstance(observation, dict)
                    or set(observation) != {'distance_m', 'duration_s', 'source'}
                    or observation['source'] not in ('matrix_http_cache', 'direction_http_cache', 'routing_callback')):
                raise CacheValidationError('Measured route observation has invalid provenance.')
            try:
                route = RouteResult(target_id=target.target_id, location=target.location, status='success',
                    distance_m=observation['distance_m'], duration_s=observation['duration_s'])
            except (TypeError, ValueError):
                raise CacheValidationError('Measured route observation has invalid values.') from None
            self._validate_route(request, target, route)
        key = _hash(self._identity(request, target))
        durations = {row['duration_s'] for row in observations}
        distances = {row['distance_m'] for row in observations}
        if len(durations) > 1:
            self._duration_variants.add(key)
        if len(distances) > 1:
            self._distance_variants.add(key)
        if len(durations) > 1 or len(distances) > 1:
            if self.observation_policy == 'strict':
                raise CacheValidationError('Measured route caches contain conflicting observations.')
            if len({duration <= 900 for duration in durations}) != 1:
                raise CacheValidationError('Measured isochrone observations cross the 900-second threshold.')
        # Select an existing complete row; never combine independently chosen
        # distance and duration values from different provider observations.
        selected = max(observations, key=lambda row: (row['duration_s'], row['distance_m']))
        return RouteResult(target_id=target.target_id, location=target.location, status='success',
            distance_m=selected['distance_m'], duration_s=selected['duration_s'])

    @staticmethod
    def _validate_route(request, target, route):
        if route.target_id != target.target_id or route.location != target.location:
            raise CacheValidationError('Measured route result has a different target identity.')
        if route.status is RouteStatus.SUCCESS:
            distance, duration = route.distance_m, route.duration_s
            if (type(distance) is not int or type(duration) is not int or distance < 0 or duration < 0
                    or ((distance == 0) != (duration == 0) and (distance, duration) != (1, 0))
                    or (distance == duration == 0 and request.origin != target.location)):
                raise CacheValidationError('Measured route result has invalid distance or duration.')

    def _accept(self, request, target, route, known, source):
        self._validate_route(request, target, route)
        previous = known.get(target.target_id)
        if previous is not None and previous.status != route.status:
            raise CacheValidationError('Measured route caches contain conflicting observations.')
        known[target.target_id] = route
        if route.status is RouteStatus.SUCCESS:
            path, _ = self._pair(request, target)
            key = _hash(self._identity(request, target))
            observations = list(self._observations.get(key, []))
            observed = dict(distance_m=route.distance_m, duration_s=route.duration_s, source=source)
            if observed not in observations:
                observations.append(observed)
            selected = self._select(request, target, observations)
            self._observations[key] = observations
            known[target.target_id] = selected
            record = dict(identity=self._identity(request, target),
                provider_identity=self._provider_identity(request, target), status='success',
                distance_m=selected.distance_m, duration_s=selected.duration_s,
                source=max(observations, key=lambda row: (row['duration_s'], row['distance_m']))['source'],
                observations=observations, selection_policy=self.observation_policy)
            if _read(path) != record:
                _atomic_json(path, record)

    def _prefill(self, request):
        known = {}
        for target in request.targets:
            self._bind(request, target)
            _, saved = self._pair(request, target)
            if saved is not None:
                known[target.target_id] = saved
        matrix_batches = (request.targets[start:start+size]
            for size in dict.fromkeys((25, self.request_batch_size))
            for start in range(0, len(request.targets), size))
        for batch in matrix_batches:
            for folder in self.cache_dirs:
                provider = BaiduRoutingProvider('cache-only-placeholder', transport=_CacheOnlyTransport(folder))
                try:
                    measured = provider.route_matrix(request.origin, [target.location for target in batch])
                except CacheMiss:
                    continue
                except BaiduRoutingError:
                    raise CacheValidationError('Successful matrix cache contains invalid route measurements.') from None
                for target, observed in zip(batch, measured, strict=True):
                    self._accept(request, target, _public_route(request, target, observed), known, 'matrix_http_cache')
        for target in request.targets:
            for folder in self.cache_dirs:
                provider = BaiduDirectionProvider('cache-only-placeholder', transport=_CacheOnlyTransport(folder))
                try:
                    observed = provider.walking(request.origin, target.location)
                except CacheMiss:
                    continue
                except BaiduRoutingError:
                    raise CacheValidationError('Successful direction cache contains invalid route measurements.') from None
                self._accept(request, target, _public_route(request, target, observed), known, 'direction_http_cache')
        self._cached_target_uses += len(known)
        return known

    def route(self, request):
        known = self._prefill(request)
        had_cached = bool(known)
        missing = [target for target in request.targets if target.target_id not in known]
        warnings, first_failure = [], None
        if self.observation_policy == 'isochrone_conservative':
            warnings.append(WarningItem(code='ISOCHRONE_CONSERVATIVE_OBSERVATIONS', message=(
                '中心等时圈显式采用保守观测策略：仅当同点对全部已测时长处于900秒同侧时，'
                '选择时长最大的完整真实观测（时长相同选距离较大者）；原始变体保留，跨阈值拒绝。')))
        meta = Meta(schema_version='1.0', provider='baidu')
        had_success_envelope = False
        for start in range(0, len(missing), self.request_batch_size):
            batch = missing[start:start + self.request_batch_size]
            subrequest = request.model_copy(update={'targets': batch})
            self._submitted_targets += len(batch)
            # ProviderStopError deliberately propagates. Completed batches are
            # already durable, and partial HTTP responses remain in raw caches.
            root = RoutingResult.model_validate(self._route(subrequest).model_dump(mode='json')).root
            meta = root.meta
            for warning in root.warnings:
                if warning not in warnings:
                    warnings.append(warning)
            if not root.ok:
                first_failure = first_failure or root
                if root.error.code.value == 'RATE_LIMITED' and not root.error.retryable:
                    break
                continue
            had_success_envelope = True
            expected = {target.target_id: target for target in batch}
            rows = root.data.routes
            if (root.data.origin != request.origin or root.data.travel_mode != request.travel_mode
                    or len(rows) != len(batch) or {row.target_id for row in rows} != set(expected)):
                raise CacheValidationError('Routing callback returned a different request identity.')
            for row in rows:
                self._validate_route(request, expected[row.target_id], row)
            for row in rows:
                self._accept(request, expected[row.target_id], row, known, 'routing_callback')
        if first_failure is not None and not had_cached and not had_success_envelope:
            return RoutingResult(root=first_failure.model_copy(update={'warnings': warnings}))
        routes = [known.get(target.target_id) or _unavailable(target) for target in request.targets]
        return RoutingResult(root=RoutingSuccess(ok=True, data=RoutingData(origin=request.origin,
            travel_mode=request.travel_mode, routes=routes, summary=_summary(routes)), warnings=warnings, meta=meta))
