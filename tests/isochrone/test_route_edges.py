"""Stage 2B tests: offline validator for adjacent-point candidate contour edges.

Two groups of tests:

* the eight real walking routes saved in
  ``执行资料/改进执行_2026-10-18/阶段0_路径探测/direction_*.json`` are replayed
  through the validator (geometry + time).  They are centre -> point probes, not
  A -> B adjacent-sample routes, so the expected decisions document what the
  rules do on real polylines, not a proven A -> B contour edge;
* artificial adversarial edges (inward fold, detour, self intersection, hole /
  unknown crossing, wrong closure, normal short edge) plus time-evidence states.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from app.modules.isochrone.route_edges import (
    EdgeThresholds,
    ReasonCode,
    bd09ll_to_local,
    validate_candidate_edge,
)

WORKSPACE = Path(__file__).resolve().parents[2]  # Packaged regression samples live inside the project.
ROUTE_DIR = WORKSPACE / "执行资料" / "改进执行_2026-10-18" / "阶段0_路径探测"
EARTH_RADIUS_M = 6_371_008.8
TIER = 900

requires_real_routes = pytest.mark.skipif(
    not ROUTE_DIR.is_dir(),
    reason="交接包中的真实路径探测数据不在预期位置",
)


def _load_route(filename: str) -> tuple[tuple[float, float], tuple[float, float], float, list[tuple[float, float]]]:
    """Return (origin, destination, route duration, polyline) from a Baidu direction JSON."""
    payload = json.loads((ROUTE_DIR / filename).read_text(encoding="utf-8"))
    assert payload["status"] == 0
    result = payload["result"]
    origin = (float(result["origin"]["lng"]), float(result["origin"]["lat"]))
    destination = (float(result["destination"]["lng"]), float(result["destination"]["lat"]))
    route = result["routes"][0]
    polyline: list[tuple[float, float]] = []
    for step in route["steps"]:
        for pair in step.get("path", "").split(";"):
            if not pair.strip():
                continue
            lng_text, lat_text = pair.split(",")
            polyline.append((float(lng_text), float(lat_text)))
    return origin, destination, float(route["duration"]), polyline


def _codes(decision, severity: str | None = None) -> set[ReasonCode]:
    return {
        reason.code
        for reason in decision.reasons
        if severity is None or reason.severity == severity
    }


# filename, label, expected status, expected non-info codes, start snap, end snap,
# straight A->B distance, detour ratio, route duration
REAL_ROUTES = [
    ("direction_01_校园内部院落.json", "01", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     18.534, 14.177, 194.19, 1.2494, 205.0),
    ("direction_02_普通街道短途.json", "02", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     18.534, 4.005, 1023.80, 1.5364, 1421.0),
    ("direction_03_普通街道中途.json", "03", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     18.534, 25.879, 1920.90, 1.4273, 2561.0),
    ("direction_04_接近15分钟.json", "04", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     18.534, 4.165, 1272.37, 1.4852, 1706.0),
    ("direction_05_明显超时.json", "05", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     18.534, 16.729, 5650.60, 1.2092, 6652.0),
    ("direction_06_跨苏州河.json", "06", "reject",
     {ReasonCode.ENDPOINT_SNAP_EXCEEDED, ReasonCode.TIME_TIER_INCOMPATIBLE},
     31.921, 8.875, 3315.82, 1.1379, 3519.0),
    ("direction_07_跨苏州河二.json", "07", "reject", {ReasonCode.TIME_TIER_INCOMPATIBLE},
     5.199, 2.236, 1387.72, 1.3583, 1748.0),
    ("direction_08_铁路车站周边.json", "08", "reject",
     {ReasonCode.ENDPOINT_SNAP_EXCEEDED, ReasonCode.TIME_TIER_INCOMPATIBLE},
     1.254, 33.368, 800.82, 1.1564, 836.0),
]


@requires_real_routes
@pytest.mark.parametrize(
    ("filename", "label", "expected_status", "expected_codes", "start_snap", "end_snap",
     "straight", "detour", "duration"),
    REAL_ROUTES,
    ids=[row[1] for row in REAL_ROUTES],
)
def test_real_route_decision_with_time_evidence(
    filename: str,
    label: str,
    expected_status: str,
    expected_codes: set[ReasonCode],
    start_snap: float,
    end_snap: float,
    straight: float,
    detour: float,
    duration: float,
) -> None:
    origin, destination, route_duration, polyline = _load_route(filename)
    assert route_duration == pytest.approx(duration)

    decision = validate_candidate_edge(
        center=origin,
        a=origin,
        b=destination,
        polyline=polyline,
        tier_s=TIER,
        crs="BD09LL",
        center_to_a_duration_s=0.0,
        route_duration_s=route_duration,
    )

    assert decision.status == expected_status
    assert _codes(decision, "reject") == expected_codes
    metrics = decision.metrics
    assert metrics.start_snap_m == pytest.approx(start_snap, abs=0.05)
    assert metrics.end_snap_m == pytest.approx(end_snap, abs=0.05)
    assert metrics.straight_distance_m == pytest.approx(straight, rel=0.002)
    assert metrics.detour_ratio == pytest.approx(detour, rel=0.002)
    assert metrics.route_duration_s == pytest.approx(duration)
    assert metrics.estimated_center_to_b_duration_s == pytest.approx(duration)
    assert metrics.time_evidence_complete is True
    assert metrics.time_compatibility == "incompatible"
    # The saved routes are ordinary vendor polylines: no fold, no self
    # intersection.  These are centre -> point probes, so A is the centre.
    assert metrics.self_intersects is False
    assert metrics.backtrack_depth_m == pytest.approx(0.0)
    assert metrics.center_to_a_m == pytest.approx(0.0)
    # No legitimate hole / unknown geometry was supplied for the probes.
    assert metrics.obstacle_geometry_supplied is False
    assert metrics.crosses_hole is None
    assert metrics.crosses_unknown is None
    assert ReasonCode.OBSTACLE_GEOMETRY_NOT_PROVIDED in _codes(decision, "info")


@requires_real_routes
@pytest.mark.parametrize(
    ("filename", "expected_status"),
    [
        ("direction_01_校园内部院落.json", "unverified"),
        ("direction_02_普通街道短途.json", "unverified"),
        ("direction_03_普通街道中途.json", "unverified"),
        ("direction_04_接近15分钟.json", "unverified"),
        ("direction_05_明显超时.json", "unverified"),
        ("direction_06_跨苏州河.json", "reject"),
        ("direction_07_跨苏州河二.json", "unverified"),
        ("direction_08_铁路车站周边.json", "reject"),
    ],
    ids=["01", "02", "03", "04", "05", "06", "07", "08"],
)
def test_real_route_without_time_evidence_is_not_accepted(
    filename: str,
    expected_status: str,
) -> None:
    """Geometry alone must never produce ``accept``: missing time => unverified."""
    origin, destination, _duration, polyline = _load_route(filename)
    decision = validate_candidate_edge(
        center=origin,
        a=origin,
        b=destination,
        polyline=polyline,
        tier_s=TIER,
        crs="BD09LL",
    )

    assert decision.status == expected_status
    assert decision.accepted is False
    assert ReasonCode.TIME_EVIDENCE_MISSING in _codes(decision)
    assert decision.metrics.time_evidence_complete is False
    assert decision.metrics.time_compatibility == "unverified"
    if expected_status == "reject":
        assert ReasonCode.ENDPOINT_SNAP_EXCEEDED in _codes(decision, "reject")


# ---------------------------------------------------------------------------
# Artificial local-plane edges
# ---------------------------------------------------------------------------

GOOD_POLYLINE = [(900.0, 0.0), (902.0, 40.0), (905.0, 80.0), (900.0, 120.0)]


def test_normal_short_edge_is_accepted() -> None:
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=GOOD_POLYLINE,
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
    )

    assert decision.status == "accept"
    assert decision.accepted is True
    assert _codes(decision, "reject") == set()
    assert decision.metrics.detour_ratio == pytest.approx(1.0, abs=0.02)
    assert decision.metrics.max_snap_m == pytest.approx(0.0, abs=1e-6)
    assert decision.metrics.time_compatibility == "compatible"
    # Missing obstacle geometry is reported as info, but does not downgrade an
    # otherwise verified edge; it is also never reported as "no crossing".
    assert ReasonCode.OBSTACLE_GEOMETRY_NOT_PROVIDED in _codes(decision, "info")
    assert decision.metrics.crosses_hole is None
    assert decision.metrics.crosses_unknown is None


def test_inward_fold_toward_center_is_rejected() -> None:
    # A and B are ~900 m from the centre; the route detours via a point only
    # 800 m from the centre, i.e. it moves back toward the analysis centre.
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 500.0),
        polyline=[(900.0, 0.0), (800.0, 0.0), (900.0, 500.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=850.0,
        route_duration_s=200.0,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.BACKTRACK_TOWARD_CENTER}
    assert decision.metrics.backtrack_depth_m == pytest.approx(100.0, abs=0.5)
    assert decision.metrics.detour_ratio < 2.0


def test_detour_ratio_is_rejected() -> None:
    # A -> B is 60 m, but the route bulges out to ~(960, 30): ratio > 2.
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 60.0),
        polyline=[(900.0, 0.0), (960.0, 30.0), (900.0, 60.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=860.0,
        route_duration_s=100.0,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.DETOUR_RATIO_EXCEEDED}
    assert decision.metrics.detour_ratio == pytest.approx(2.236, abs=0.02)


def test_self_intersecting_route_is_rejected() -> None:
    # Figure: A(850,0) -> (950,100) -> (850,100) -> B(950,0); the first and
    # last segments cross at (900, 50).
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(850.0, 0.0),
        b=(950.0, 0.0),
        polyline=[(850.0, 0.0), (950.0, 100.0), (850.0, 100.0), (950.0, 0.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
        # Isolate self-intersection from the detour check.
        thresholds=EdgeThresholds(max_detour_ratio=10.0),
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.ROUTE_SELF_INTERSECTION}
    assert decision.metrics.self_intersects is True


def test_hole_crossing_is_rejected() -> None:
    hole = Polygon([(880.0, 40.0), (920.0, 40.0), (920.0, 80.0), (880.0, 80.0)])
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=[(900.0, 0.0), (900.0, 40.0), (900.0, 80.0), (900.0, 120.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
        holes=hole,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.HOLE_CROSSED}
    assert decision.metrics.obstacle_geometry_supplied is True
    assert decision.metrics.crosses_hole is True
    assert decision.metrics.crosses_unknown is None
    assert decision.metrics.hole_crossing_length_m == pytest.approx(40.0, abs=0.5)
    assert ReasonCode.OBSTACLE_GEOMETRY_NOT_PROVIDED not in _codes(decision)


def test_unknown_crossing_is_rejected() -> None:
    unknown = Polygon([(880.0, 40.0), (920.0, 40.0), (920.0, 80.0), (880.0, 80.0)])
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=[(900.0, 0.0), (900.0, 40.0), (900.0, 80.0), (900.0, 120.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
        unknown=unknown,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.UNKNOWN_CROSSED}
    assert decision.metrics.crosses_unknown is True
    assert decision.metrics.crosses_hole is None
    assert decision.metrics.unknown_crossing_length_m == pytest.approx(40.0, abs=0.5)


def test_wrong_closure_is_rejected_by_endpoint_snap() -> None:
    # The route stops 110 m from B (a wrong closure), so it is not an A -> B edge
    # even though its geometry is short and simple.
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=[(900.0, 0.0), (950.0, 60.0), (900.0, 10.0)],
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.ENDPOINT_SNAP_EXCEEDED}
    assert decision.metrics.end_snap_m == pytest.approx(110.0, abs=0.5)
    assert "终点 B" in (decision.reasons[0].detail or "")


def test_thresholds_are_configurable() -> None:
    polyline = [(900.5, 0.0), *GOOD_POLYLINE[1:]]
    kwargs = dict(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=polyline,
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
    )
    assert validate_candidate_edge(**kwargs).status == "accept"

    strict = validate_candidate_edge(
        **kwargs, thresholds=EdgeThresholds(max_endpoint_snap_m=0.1)
    )
    assert strict.status == "reject"
    assert _codes(strict, "reject") == {ReasonCode.ENDPOINT_SNAP_EXCEEDED}


@pytest.mark.parametrize(
    ("missing", "expected_missing_name"),
    [
        ("both", "center_to_a_duration_s、route_duration_s"),
        ("center_to_a", "center_to_a_duration_s"),
        ("route_duration", "route_duration_s"),
    ],
)
def test_missing_time_evidence_is_unverified(missing: str, expected_missing_name: str) -> None:
    center_to_a = None if missing in ("both", "center_to_a") else 880.0
    route_duration = None if missing in ("both", "route_duration") else 100.0
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=GOOD_POLYLINE,
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=center_to_a,
        route_duration_s=route_duration,
    )

    assert decision.status == "unverified"
    assert decision.accepted is False
    assert _codes(decision, "reject") == set()
    assert _codes(decision, "unverified") == {ReasonCode.TIME_EVIDENCE_MISSING}
    assert expected_missing_name in (decision.reasons[0].detail or "") or expected_missing_name in (
        decision.reasons[0].message
    )


def test_time_tier_incompatible_is_rejected() -> None:
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=GOOD_POLYLINE,
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=200.0,
        route_duration_s=100.0,
    )

    assert decision.status == "reject"
    assert _codes(decision, "reject") == {ReasonCode.TIME_TIER_INCOMPATIBLE}
    assert decision.metrics.time_compatibility == "incompatible"
    assert decision.metrics.estimated_center_to_b_duration_s == pytest.approx(300.0)


def test_bd09ll_inputs_are_projected_to_metres() -> None:
    center = (121.508602, 31.288268)
    cosine = math.cos(math.radians(center[1]))

    def to_bd09ll(local: tuple[float, float]) -> tuple[float, float]:
        lng = center[0] + math.degrees(local[0] / (EARTH_RADIUS_M * cosine))
        lat = center[1] + math.degrees(local[1] / EARTH_RADIUS_M)
        return lng, lat

    a = to_bd09ll((900.0, 0.0))
    b = to_bd09ll((900.0, 120.0))
    polyline = [to_bd09ll(point) for point in GOOD_POLYLINE]

    decision = validate_candidate_edge(
        center=center,
        a=a,
        b=b,
        polyline=polyline,
        tier_s=TIER,
        crs="BD09LL",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
    )

    assert decision.status == "accept"
    assert decision.metrics.straight_distance_m == pytest.approx(120.0, abs=0.5)
    assert decision.metrics.max_snap_m == pytest.approx(0.0, abs=0.01)
    projected = bd09ll_to_local(a, center)
    assert projected[0] == pytest.approx(900.0, abs=0.5)
    assert projected[1] == pytest.approx(0.0, abs=0.5)


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        validate_candidate_edge(
            center=(0.0, 0.0), a=(0.0, 0.0), b=(0.0, 100.0),
            polyline=[(0.0, 0.0), (0.0, 100.0)], tier_s=500, crs="local",
        )
    with pytest.raises(ValueError):
        validate_candidate_edge(
            center=(0.0, 0.0), a=(0.0, 0.0), b=(0.0, 100.0),
            polyline=[(0.0, 0.0)], tier_s=900, crs="local",
        )
    with pytest.raises(ValueError):
        EdgeThresholds(max_detour_ratio=0.0)
    with pytest.raises(ValueError):
        EdgeThresholds(time_band_ratio=1.0)


def test_decision_is_json_serialisable() -> None:
    decision = validate_candidate_edge(
        center=(0.0, 0.0),
        a=(900.0, 0.0),
        b=(900.0, 120.0),
        polyline=GOOD_POLYLINE,
        tier_s=TIER,
        crs="local",
        center_to_a_duration_s=880.0,
        route_duration_s=100.0,
    )
    payload = decision.as_dict()
    text = json.dumps(payload, ensure_ascii=False)
    reloaded = json.loads(text)
    assert reloaded["status"] == "accept"
    assert reloaded["reason_codes"][-1] == "OBSTACLE_GEOMETRY_NOT_PROVIDED"
    assert reloaded["thresholds"]["max_endpoint_snap_m"] == 30.0
