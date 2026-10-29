"""Synthetic CheckupReport fixtures for work line 3 (report questions).

These fixtures are built by hand so that the question tests stay offline, deterministic
and independent of the map providers. They reuse the real catalog only for category
identity (id / name_zh / group_id) and the real Pydantic models, so a shape change in
the shared schema still breaks these tests instead of silently passing.

Not a product module: nothing here is imported by app code.
"""

import json
from pathlib import Path

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import _quantize
from app.schemas.checkup import (
    CategoryAssessment,
    CheckupPOI,
    CheckupReport,
    GroupAssessment,
    RouteResult,
)
from app.schemas.common import CenterPoint
from app.schemas.isochrone import BoundaryPoint, IsochroneData

# A category that is reachable, one that is complete but empty, one that is partially
# searched, and the two catalog classes whose service area has no supporting data.
REACHABLE_FACILITY = "childcare_0_3"
EMPTY_FACILITY = "kindergarten"
PARTIAL_FACILITY = "primary_school"
SERVICE_AREA_FACILITY = "home_care_service"
FACILITY_AREA_FACILITY = "park_green"
GROUP_ID = "education_children"

ROOT = Path(__file__).resolve().parents[1]


def _center() -> CenterPoint:
    return CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")


def _isochrone() -> IsochroneData:
    center = _center()
    corners = [
        (0.0, CenterPoint(lng=121.5, lat=31.209, crs="BD09LL")),
        (90.0, CenterPoint(lng=121.51, lat=31.2, crs="BD09LL")),
        (180.0, CenterPoint(lng=121.5, lat=31.191, crs="BD09LL")),
        (270.0, CenterPoint(lng=121.49, lat=31.2, crs="BD09LL")),
    ]
    boundary = [
        BoundaryPoint(
            boundary_id=f"boundary:{int(bearing):03d}",
            bearing_deg=bearing,
            location=location,
            radial_distance_m=1000,
        )
        for bearing, location in corners
    ]
    ring = [[point.location.lng, point.location.lat] for point in boundary]
    ring.append(ring[0])
    return IsochroneData(
        center=center,
        time_limit_s=900,
        geometry={"type": "Polygon", "coordinates": [ring]},
        boundary_points=boundary,
        quality={
            "requested_directions": 4,
            "valid_boundary_directions": 4,
            "routing_sample_count": 24,
            "routing_success_count": 24,
            "sample_success_rate": 1,
            "confidence": "high",
        },
    )


def _category(item, **overrides) -> CategoryAssessment:
    fields = {
        "facility_id": item["id"],
        "name_zh": item["name_zh"],
        "group_id": item["group_id"],
        "search_status": "complete",
        "count": 0,
        "known_count": 0,
        "reachable_count": 0,
        "unresolved_count": 0,
        "nearest_duration_s": None,
        "nearest_distance_m": None,
        "access_status": "not_found",
        "score": 0,
        "covered_ratio": None,
        "blind_ratio": None,
        "unknown_ratio": None,
        "data_notes": item.get("data_notes", ""),
    }
    fields.update(overrides)
    return CategoryAssessment(**fields)


def build_report(report_id: str = "synthetic-report-fixture") -> CheckupReport:
    """Return a full 45-category / 7-group synthetic report with several states."""
    catalog = load_catalog()
    center = _center()
    categories = []
    for item in catalog["facilities"]:
        if item["id"] == REACHABLE_FACILITY:
            categories.append(
                _category(
                    item,
                    count=1,
                    known_count=1,
                    reachable_count=1,
                    nearest_duration_s=540,
                    nearest_distance_m=720,
                    access_status="reachable",
                    score=100,
                )
            )
        elif item["id"] == PARTIAL_FACILITY:
            # Incomplete search: count must stay null, and a positive reachability fact
            # may still be kept even though other candidates were never routed.
            categories.append(
                _category(
                    item,
                    search_status="partial",
                    count=None,
                    known_count=2,
                    reachable_count=1,
                    unresolved_count=1,
                    nearest_duration_s=600,
                    nearest_distance_m=800,
                    access_status="reachable",
                    score=100,
                )
            )
        elif item["id"] == SERVICE_AREA_FACILITY:
            # A single reachable point, yet overall coverage stays unknown.
            categories.append(
                _category(
                    item,
                    count=1,
                    known_count=1,
                    reachable_count=1,
                    nearest_duration_s=780,
                    nearest_distance_m=900,
                    access_status="reachable",
                    score=100,
                    covered_ratio=0,
                    blind_ratio=0,
                    unknown_ratio=1,
                )
            )
        elif item["id"] == FACILITY_AREA_FACILITY:
            categories.append(
                _category(
                    item,
                    count=0,
                    access_status="unknown",
                    score=None,
                    covered_ratio=0,
                    blind_ratio=0,
                    unknown_ratio=1,
                )
            )
        else:
            categories.append(_category(item))

    groups = []
    for group in catalog["groups"]:
        members = [c for c in categories if c.group_id == group["id"]]
        scored = [c for c in members if c.score is not None]
        enough = len(scored) >= -(-len(members) * 8 // 10)  # ceil(members * 0.8)
        raw = (sum(c.score for c in scored) / len(scored)) if enough and scored else None
        groups.append(
            GroupAssessment(
                **group,
                score=_quantize(raw) if raw is not None else None,
                assessed_categories=len(scored),
                total_categories=len(members),
                summary=f"{group['name_zh']}合成测试小结",
            )
        )

    pois = [
        CheckupPOI(
            poi_id=f"poi:{REACHABLE_FACILITY}:in",
            name="合成托育点（圈内）",
            facility_id=REACHABLE_FACILITY,
            location=CenterPoint(lng=121.501, lat=31.201, crs="BD09LL"),
            address="合成地址",
            in_isochrone=True,
        ),
        CheckupPOI(
            poi_id=f"poi:{EMPTY_FACILITY}:out",
            name="合成幼儿园（圈外候选）",
            facility_id=EMPTY_FACILITY,
            location=CenterPoint(lng=121.53, lat=31.2, crs="BD09LL"),
            address=None,
            in_isochrone=False,
        ),
    ]
    routes = [
        RouteResult(
            target_id=pois[0].poi_id,
            location=pois[0].location,
            status="success",
            distance_m=720,
            duration_s=540,
        ),
        RouteResult(
            target_id=pois[1].poi_id,
            location=pois[1].location,
            status="no_route",
            distance_m=None,
            duration_s=None,
        ),
    ]

    return CheckupReport(
        report_id=report_id,
        created_at="2026-10-14T00:00:00+00:00",
        center=center,
        location_label="合成测试地点（非真实调查结果）",
        catalog_version=catalog["version"],
        isochrone=_isochrone(),
        search_radius_m=2600,
        categories=categories,
        groups=groups,
        score=None,
        score_method="设施可达性综合参考分（合成测试口径）",
        summary="合成测试报告摘要：本报告用于离线问答测试，不代表任何真实地点。",
        pois=pois,
        routes=routes,
        blindspot={"blind": {"type": "FeatureCollection", "features": []}},
        warnings=[],
        quality={"confidence": "low", "provider_requests": 12, "provider_request_limit": 450},
    )


def report_from_example(name: str) -> CheckupReport:
    """Load a completed-job example shipped with the handoff package, if present."""
    path = ROOT / "examples" / name
    payload = json.loads(path.read_text())
    return CheckupReport.model_validate(payload["result"])
