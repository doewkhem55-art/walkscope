"""Reconstruct saved walking contours from successful HTTP caches, with no network."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.routing.service import _public_route, _unavailable, _summary
from app.providers.baidu.direction import _parse_route
from app.schemas.checkup import CheckupJob
from app.schemas.common import Meta, WarningItem
from app.schemas.routing import RoutingData, RoutingResult, RoutingSuccess
from tools.recompute_actual_poi_case import recompute_actual_case, saved_actual_sample_cache


class CachedDirections:
    """Resolve exact provider request hashes; absent or invalid evidence is unknown."""
    def __init__(self, cache_dirs):
        self.cache_dirs = [Path(path) for path in cache_dirs]
        self.hits = self.misses = 0

    def __call__(self, request):
        if request.origin.crs != 'BD09LL' or request.travel_mode != 'walking':
            raise ValueError('Only BD09LL walking cache requests are supported')
        rows = []
        for target in request.targets:
            if target.location.crs != 'BD09LL':
                raise ValueError('Cache targets must use BD09LL coordinates')
            query = dict(origin=f'{request.origin.lat:.6f},{request.origin.lng:.6f}',
                destination=f'{target.location.lat:.6f},{target.location.lng:.6f}',
                coord_type='bd09ll', output='json')
            filename = hashlib.sha256(('/directionlite/v1/walking?' + urlencode(query)).encode()).hexdigest() + '.json'
            row = None
            for folder in self.cache_dirs:
                try:
                    payload = json.loads((folder / filename).read_text())
                    if payload.get('status') != 0 or isinstance(payload.get('status'), bool):
                        continue
                    route = _parse_route(payload['result']['routes'][0])
                except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError):
                    continue
                row = _public_route(request, target, route)
                break
            self.hits += row is not None
            self.misses += row is None
            rows.append(row if row is not None else _unavailable(target))
        return RoutingResult(root=RoutingSuccess(ok=True,
            data=RoutingData(origin=request.origin, travel_mode=request.travel_mode,
                             routes=rows, summary=_summary(rows)),
            warnings=[], meta=Meta(schema_version='1.0', provider='saved_http_cache')))


def recover_cached_contour(source_path, ledger_path, report_id, output, cache_dirs, *, max_route_pairs=5000):
    """Discover cached samples, then rebuild with a zero-route budget and fresh output."""
    source_path, ledger_path, output = map(Path, (source_path, ledger_path, output))
    if output.exists():
        raise FileExistsError('Refusing to replace a saved report')
    source = CheckupJob.model_validate_json(source_path.read_text())
    ledger_bytes = ledger_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    count = ledger.get('requests')
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('Source accounting requires a nonnegative request count')
    cache = CachedDirections(cache_dirs)
    discovered = recompute_actual_case(source, cache, report_id=report_id,
                                       max_route_pairs=max_route_pairs)
    samples, sample_info = saved_actual_sample_cache(discovered.result)
    # Cache misses are unmeasured, not failed provider measurements.
    for entry in samples.values():
        entry['failed_points'] = []

    def forbidden(_):
        raise AssertionError('Reconstruction must not issue any provider requests')

    job = recompute_actual_case(source, forbidden, report_id=report_id, sample_cache=samples,
        cache_info=dict(sample_info, source='successful_http_cache_only',
                        provider_origin_precision=6), max_route_pairs=0)
    quality = job.result.quality
    quality.update(cache_reconstruction=True, provider_requests=count, new_http_requests=0,
        reconstruction_new_http_requests=0, new_poi_search_requests=0, new_center_route_requests=0,
        source_accounting=deepcopy(ledger),
        source_accounting_path=str(ledger_path),
        source_accounting_sha256=hashlib.sha256(ledger_bytes).hexdigest(),
        cache_lookup_pairs=cache.hits + cache.misses, cache_lookup_hits=cache.hits,
        cache_lookup_misses=cache.misses, cache_lookup_pair_limit=max_route_pairs,
        coverage_route_pairs=0, coverage_route_batches=0, coverage_route_succeeded=0,
        coverage_route_no_route=0, coverage_route_unavailable=0,
        coverage_reused_distance_samples=sample_info['available_samples'],
        provider_budget_exhausted=count >= ledger.get('max_requests', 450),
        coverage_stop_reason='SAVED_SUCCESSFUL_HTTP_CACHE_ONLY')
    job.result.warnings.append(WarningItem(code='CACHED_CONTOUR_RECONSTRUCTION', message=(
        f'原运行记录{count}次HTTP尝试；本轮仅恢复成功响应缓存，新增HTTP为0。'
        '缓存缺失保持未知，不代表已确认无路或无服务。')))
    job = CheckupJob.model_validate(job.model_dump(mode='json'))
    text = job.model_dump_json(indent=2) + '\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        stream.write(text)
    return job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, action='append', required=True)
    args = parser.parse_args(argv)
    job = recover_cached_contour(args.source, args.ledger, args.id, args.output, args.cache)
    print(json.dumps(dict(saved=str(args.output), report_id=job.id,
        source_http_requests=job.result.quality['provider_requests'], new_http_requests=0,
        cached_samples=job.result.quality['coverage_distance_sample_count']), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
