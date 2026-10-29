"""Stage 3 backend: optional 5/10/15-minute tiers + replayable time field.

The integration test rebuilds the grid time field from the saved Zhenpinglu
sample (``执行资料/改进执行_2026-10-18/阶段2_对照/grid_times.json``) and drives
``generate_grid_isochrone`` with a fake router: no network, no Baidu quota.
"""

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from app.api import checkups
from app.modules.isochrone.geometry import geometry_to_local
from app.modules.isochrone.grid import (
    EARTH_RADIUS_M,
    TIER_LABELS_ZH,
    generate_grid_isochrone,
)
from app.modules.isochrone.tiers import build_tiers
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.common import CenterPoint, Meta
from app.schemas.isochrone import IsochroneData, IsochroneRequest, IsochroneResult
from app.schemas.routing import (
    RouteResult,
    RouteStatus,
    RoutingData,
    RoutingRequest,
    RoutingResult,
    RoutingSuccess,
    RoutingSummary,
)

WORKSPACE = Path(__file__).resolve().parents[2]  # Packaged regression samples live inside the project.
SAVED_GRID = (
    WORKSPACE / "执行资料" / "改进执行_2026-10-18" / "阶段2_对照" / "grid_times.json"
)
CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "v1"
CENTER = CenterPoint(lng=121.4373, lat=31.2456, crs="BD09LL")
RADIUS_M = 1000.0
CELL_M = 100.0
SAVED_TIER_AREAS_M2 = {
    "cumulative": {300: 184_893.083, 600: 634_508.164, 900: 1_776_493.548},
    "bands": {"0-300": 184_893.083, "300-600": 449_615.081, "600-900": 1_141_985.385},
}


def load_saved_times() -> dict[str, dict]:
    return json.loads(SAVED_GRID.read_text(encoding="utf-8"))


class SavedFieldRouter:
    """Replay saved centre-arrival times by grid row/col; never touches network."""

    def __init__(self, times: dict[str, dict]) -> None:
        self.times = times
        self.requests: list[RoutingRequest] = []

    def __call__(self, request: RoutingRequest) -> RoutingResult:
        self.requests.append(request)
        routes = []
        for target in request.targets:
            parts = target.target_id.split(":")
            row, col = int(parts[1][1:]), int(parts[2][1:])
            item = self.times.get(f"{row}:{col}")
            duration = int(item["duration_s"]) if item else None
            routes.append(RouteResult(
                target_id=target.target_id,
                location=target.location,
                status=RouteStatus.SUCCESS if duration is not None else RouteStatus.UNAVAILABLE,
                distance_m=int(round(duration / 1.4)) if duration is not None else None,
                duration_s=duration,
            ))
        success = sum(route.status is RouteStatus.SUCCESS for route in routes)
        return RoutingResult(root=RoutingSuccess(
            ok=True,
            data=RoutingData(
                origin=request.origin,
                travel_mode=request.travel_mode,
                routes=routes,
                summary=RoutingSummary(
                    requested=len(routes),
                    success=success,
                    no_route=0,
                    unavailable=len(routes) - success,
                ),
            ),
            warnings=[],
            meta=Meta(schema_version="1.0", provider="offline-saved-field"),
        ))


def generate(include_tiers: bool = True, time_limit_s: int = 900) -> IsochroneData:
    result = generate_grid_isochrone(
        SavedFieldRouter(load_saved_times()),
        IsochroneRequest(schema_version="1.0", center=CENTER, time_limit_s=time_limit_s),
        radius_m=RADIUS_M,
        cell_m=CELL_M,
        include_tiers=include_tiers,
    )
    assert result.root.ok is True
    if include_tiers:
        assert "TIERS_SKIPPED_NON_900S" not in [w.code for w in result.root.warnings]
    return result.root.data


def to_local(geometry) -> object:
    cosine = math.cos(math.radians(CENTER.lat))

    def to_m(lng: float, lat: float) -> tuple[float, float]:
        return (
            math.radians(lng - CENTER.lng) * EARTH_RADIUS_M * cosine,
            math.radians(lat - CENTER.lat) * EARTH_RADIUS_M,
        )

    return geometry_to_local(geometry, to_m)


@pytest.fixture(scope="module")
def tiers_data() -> IsochroneData:
    return generate(include_tiers=True)


def test_generator_attaches_three_tiers_and_replayable_time_field(tiers_data) -> None:
    tiers = tiers_data.tiers
    field = tiers_data.time_field
    assert tiers is not None and field is not None
    assert tiers.schema_version == "tiers-v1"
    assert tiers.algorithm_version == "grid-tiers-v1"
    assert tiers.thresholds_s == [300, 600, 900]
    assert [item.threshold_s for item in tiers.cumulative] == [300, 600, 900]
    assert [
        (band.tier_id, band.from_s, band.to_s, band.label_zh) for band in tiers.bands
    ] == [
        ("0-300", 0, 300, "5分钟"),
        ("300-600", 300, 600, "10分钟"),
        ("600-900", 600, 900, "15分钟"),
    ]
    assert [item.label_zh for item in tiers.cumulative] == ["5分钟", "10分钟", "15分钟"]
    assert tiers.time_source == "route-matrix"
    # 317 measured nodes inside the 1 km design circle, 124 unmeasured grid cells.
    assert tiers.sample_count == 317
    assert field.rows == 21 and field.cols == 21
    assert field.crs == "local-m"
    assert field.cell_m == CELL_M
    assert field.origin_x_m == -RADIUS_M and field.origin_y_m == -RADIUS_M
    assert field.time_limit_s == 900
    assert field.source == "route-matrix"
    assert field.collected_at
    measured = sum(1 for row in field.times_s for value in row if value is not None)
    assert measured == 317
    unknown = sum(1 for row in field.times_s for value in row if value is None)
    assert unknown == 124
    assert tiers.unknown_ratio == pytest.approx(124 / 441)
    assert tiers.unknown_mask is not None
    assert len(tiers.unknown_mask.mask_cells) == 124
    assert tiers.unknown_mask.mask_geometry is not None

    # The reported geometry matches the stage-2A offline metrics (same field).
    for threshold, expected in SAVED_TIER_AREAS_M2["cumulative"].items():
        cumulative = next(item for item in tiers.cumulative if item.threshold_s == threshold)
        assert to_local(cumulative.geometry).area == pytest.approx(expected, abs=0.5)
    for band, expected in zip(tiers.bands, SAVED_TIER_AREAS_M2["bands"].values(), strict=True):
        assert to_local(band.geometry).area == pytest.approx(expected, abs=0.5)
    # 900 s compatibility geometry is still the existing single-layer contract.
    assert tiers_data.algorithm_version == "grid-marching-v1"
    assert to_local(tiers_data.geometry).area == pytest.approx(
        SAVED_TIER_AREAS_M2["cumulative"][900], abs=0.5
    )


def test_tiers_are_nested_bands_partition_and_unknown_not_filled(tiers_data) -> None:
    tiers = tiers_data.tiers
    assert tiers is not None
    cumulative = [to_local(item.geometry) for item in tiers.cumulative]
    bands = [to_local(band.geometry) for band in tiers.bands]
    assert cumulative[1].covers(cumulative[0])
    assert cumulative[2].covers(cumulative[1])
    assert cumulative[0].difference(cumulative[1]).area == pytest.approx(0.0, abs=1e-6)
    assert cumulative[1].difference(cumulative[2]).area == pytest.approx(0.0, abs=1e-6)
    for left in range(len(bands)):
        for right in range(left + 1, len(bands)):
            assert bands[left].intersection(bands[right]).area == pytest.approx(0.0, abs=1e-6)
    from shapely.ops import unary_union

    assert unary_union(bands).symmetric_difference(cumulative[-1]).area == pytest.approx(
        0.0, abs=1e-6
    )

    # Unknown mask cells are never interpolated into a cumulative range.
    field = tiers_data.time_field
    assert field is not None and tiers.unknown_mask is not None
    cumulative_local = cumulative
    for cell in tiers.unknown_mask.mask_cells:
        row, col = (int(part) for part in cell.split(":"))
        point = Point(field.origin_x_m + col * field.cell_m,
                      field.origin_y_m + row * field.cell_m)
        assert not any(geometry.covers(point) for geometry in cumulative_local)


def test_time_field_replay_rebuilds_the_same_tiers(tiers_data) -> None:
    tiers = tiers_data.tiers
    field = tiers_data.time_field
    assert tiers is not None and field is not None

    import numpy as np

    from app.modules.isochrone.grid import GridField

    times = np.array([
        [np.nan if value is None else float(value) for value in row]
        for row in field.times_s
    ], dtype=float)
    rebuilt_field = GridField(
        times=times,
        origin_x_m=field.origin_x_m,
        origin_y_m=field.origin_y_m,
        cell_m=field.cell_m,
        center=tiers_data.center,
        time_limit_s=field.time_limit_s,
    )
    rebuilt = build_tiers(rebuilt_field)
    assert rebuilt.thresholds_s == tuple(tiers.thresholds_s)
    assert rebuilt.unknown_points == len(tiers.unknown_mask.mask_cells)
    for threshold, item in zip((300, 600, 900), tiers.cumulative, strict=True):
        reported = to_local(item.geometry)
        assert reported.symmetric_difference(rebuilt.cumulative_at(threshold)).area == pytest.approx(
            0.0, abs=0.01
        )
    for band, item in zip(("0-300", "300-600", "600-900"), tiers.bands, strict=True):
        reported = to_local(item.geometry)
        assert reported.symmetric_difference(rebuilt.band_for_label(band)).area == pytest.approx(
            0.0, abs=0.01
        )
    # Re-running the same saved field also reproduces the classifier labels.
    for x, y, expected in (
        (0, 0, "0-300"),
        (300, 0, "300-600"),
        (600, 0, "600-900"),
        (700, 0, "outside"),
        (1400, 0, "unknown"),  # outside the measured 1 km circle
    ):
        assert rebuilt.classify_local_xy(x, y) == expected


def test_schema_roundtrip_and_old_report_without_tiers_still_loads(tiers_data) -> None:
    dumped = tiers_data.model_dump(mode="json")
    text = json.dumps(dumped, ensure_ascii=False)
    assert json.loads(text)["tiers"]["thresholds_s"] == [300, 600, 900]
    again = IsochroneData.model_validate(json.loads(text))
    assert again.tiers is not None and again.time_field is not None
    assert again.tiers.model_dump(mode="json") == tiers_data.tiers.model_dump(mode="json")

    old = json.loads((CONTRACTS / "isochrone-result.example.json").read_text(encoding="utf-8"))
    old_data = IsochroneResult.model_validate(old).root.data
    assert old_data.tiers is None and old_data.time_field is None
    old_payload = old_data.model_dump(mode="json")
    IsochroneData.model_validate(old_payload)  # v1.2 fields stay optional


def test_tiers_disabled_keeps_the_existing_single_layer(tiers_data) -> None:
    plain = generate(include_tiers=False)
    assert plain.tiers is None and plain.time_field is None
    assert plain.geometry.model_dump(mode="json") == tiers_data.geometry.model_dump(mode="json")
    assert plain.quality.model_dump(mode="json") == tiers_data.quality.model_dump(mode="json")
    assert plain.algorithm_version == "grid-marching-v1"
    # Coverage / blindspot consumers only see the unchanged 900 s geometry.
    assert minimum_poi_search_radius_m(CENTER, plain.geometry, 1000) == minimum_poi_search_radius_m(
        CENTER, tiers_data.geometry, 1000
    )
    assert to_local(plain.geometry).area == pytest.approx(to_local(tiers_data.geometry).area)


def test_non_900_request_skips_tiers_with_an_explicit_warning() -> None:
    result = generate_grid_isochrone(
        SavedFieldRouter(load_saved_times()),
        IsochroneRequest(schema_version="1.0", center=CENTER, time_limit_s=600),
        radius_m=RADIUS_M,
        cell_m=CELL_M,
        include_tiers=True,
    )
    assert result.root.ok is True
    data = result.root.data
    assert data.tiers is None and data.time_field is None
    assert "TIERS_SKIPPED_NON_900S" in [warning.code for warning in result.root.warnings]


def test_iso_chrone_tiers_env_switch_defaults_on_and_supports_off(monkeypatch) -> None:
    monkeypatch.delenv("ISOCHRONE_TIERS", raising=False)
    assert checkups._isochrone_tiers_enabled() is True
    for value in ("1", "true", "on"):
        monkeypatch.setenv("ISOCHRONE_TIERS", value)
        assert checkups._isochrone_tiers_enabled() is True
    for value in ("0", "false", "off", "no"):
        monkeypatch.setenv("ISOCHRONE_TIERS", value)
        assert checkups._isochrone_tiers_enabled() is False


def test_tier_contract_rejects_inconsistent_payloads(tiers_data) -> None:
    valid = tiers_data.model_dump(mode="json")

    swapped = json.loads(json.dumps(valid))
    swapped["tiers"]["thresholds_s"] = [600, 300, 900]
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(swapped)

    bad_band = json.loads(json.dumps(valid))
    bad_band["tiers"]["bands"][1]["from_s"] = 0
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(bad_band)

    bad_tier_id = json.loads(json.dumps(valid))
    bad_tier_id["tiers"]["bands"][2]["tier_id"] = "900-1200"
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(bad_tier_id)

    bad_ratio = json.loads(json.dumps(valid))
    bad_ratio["tiers"]["unknown_ratio"] = 0.0
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(bad_ratio)

    bad_mask = json.loads(json.dumps(valid))
    bad_mask["tiers"]["unknown_mask"]["mask_cells"] = bad_mask["tiers"]["unknown_mask"][
        "mask_cells"
    ][:-1]
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(bad_mask)

    bad_time_field = json.loads(json.dumps(valid))
    bad_time_field["time_field"]["rows"] = 20
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(bad_time_field)

    wrong_limit = json.loads(json.dumps(valid))
    wrong_limit["time_limit_s"] = 600
    with pytest.raises(ValidationError):
        IsochroneData.model_validate(wrong_limit)


def test_tier_labels_zh_cover_the_three_bands() -> None:
    assert TIER_LABELS_ZH == {"0-300": "5分钟", "300-600": "10分钟", "600-900": "15分钟"}
