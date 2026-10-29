"""Saved real geometry checks only; no SDK, network or route provider."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from shapely.geometry import mapping, shape

from verify.audit_blindspot_geometry_20261023 import audit_report_geometry


CASE = Path(__file__).resolve().parents[1] / "frontend/public/cases/tongji-historical-20261024.json"
CURRENT_CASE = Path(__file__).resolve().parents[1] / "frontend/public/cases/tongji.json"
HISTORICAL_CASE = Path(__file__).resolve().parents[1] / "data/reports/3de7777d550f4562afc38d96140f0118_report.json"


def test_saved_tongji_outlines_are_unions_of_real_clipped_200m_cells():
    audit = audit_report_geometry(json.loads(HISTORICAL_CASE.read_text(encoding="utf-8")))
    assert audit["passed"], audit["failures"]
    assert audit["grid_size_m"] == 200 and audit["grid_cell_count"] == 50
    assert audit["bbox_area_m2"] > audit["domain_area_m2"] * 1.5
    assert audit["max_grid_union_difference_m2"] < 0.001
    assert audit["max_blind_unknown_overlap_m2"] == 0
    assert audit["rectangular_parts"] == 6  # Real isolated or adjacent rectangular cells.


def test_historical_20m_tongji_outlines_remain_unions_of_real_clipped_cells():
    audit = audit_report_geometry(json.loads(CASE.read_text(encoding="utf-8")))
    assert audit["passed"], audit["failures"]
    assert audit["grid_size_m"] == 20 and audit["grid_cell_count"] == 3709
    assert audit["max_grid_union_difference_m2"] < 0.001
    assert audit["max_blind_unknown_overlap_m2"] == 0


def test_current_case_contains_all_seven_groups_and_continuous_service_difference():
    from shapely import make_valid
    from shapely.ops import transform, unary_union
    from app.modules.checkup.service import _projection
    from app.schemas.checkup import CheckupJob

    job = CheckupJob.model_validate_json(CURRENT_CASE.read_text())
    report = job.result
    assert job.id == 'tongji-all7-small-updated-20261027'
    assert report.quality['coverage_acquisition_source_report_id'] == 'tongji-all7-repaired-20261026'
    assert report.score == 55 and len(report.categories) == 45 and len(report.groups) == 7
    assert report.quality['coverage_method'] == 'actual_poi_origins_measured_contour_1km_service_area_v2'
    assert report.quality['coverage_geometry_grid_m'] is None
    assert report.quality['coverage_validation']['poi_check_passed'] is True
    assert report.quality['coverage_validation']['evidence_complete'] is True
    to_m, _ = _projection(report.center)
    domain = transform(to_m, shape(report.isochrone.geometry.model_dump()))
    total = report.blindspot['total_coverage']

    def region(name):
        return unary_union([make_valid(transform(to_m, shape(f['geometry'])))
                            for f in total[name]['features']])

    covered, display = region('covered_areas'), region('display_blind_spots')
    assert display.symmetric_difference(domain.difference(covered)).area < .001
    assert covered.intersection(display).area < .001
    assert total['blind_ratio'] + total['unknown_ratio'] == pytest.approx(total['display_blind_ratio'], abs=1e-6)


def test_audit_rejects_replacing_the_saved_polygon_with_its_bbox():
    payload = json.loads(CASE.read_text(encoding="utf-8"))
    feature = payload["result"]["blindspot"]["blind_spots"]["features"][0]
    feature["geometry"] = json.loads(json.dumps(mapping(shape(feature["geometry"]).envelope)))
    audit = audit_report_geometry(payload)
    assert not audit["passed"]
    assert any("outside_analysis_domain" in failure for failure in audit["failures"])


def test_audit_rejects_unknown_regions_relabelled_as_blind():
    payload = json.loads(CASE.read_text(encoding="utf-8"))
    layers = payload["result"]["blindspot"]
    feature = deepcopy(layers["unknown_areas"]["features"][0])
    feature["properties"]["classification"] = "blind"
    layers["blind_spots"]["features"].append(feature)
    audit = audit_report_geometry(payload)
    assert not audit["passed"]
    assert any("unknown_also_classified_as_blind" in failure for failure in audit["failures"])
