from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD
from app.schemas.checkup import CheckupJob
from app.schemas.isochrone import IsochroneData


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/reports/3de7777d550f4562afc38d96140f0118_report.json'
CURRENT = ROOT / 'data/reports/tongji-education-health-walk-contour-20261025_report.json'


def preparation_tool():
    assert importlib.util.find_spec('tools.prepare_actual_group') is not None, 'group preparation tool is missing'
    return importlib.import_module('tools.prepare_actual_group')


@pytest.fixture(scope='module')
def saved_inputs():
    source = json.loads(SOURCE.read_text())
    isochrone = CheckupJob.model_validate_json(CURRENT.read_text()).result.isochrone
    return source, isochrone


def test_new_circle_recounts_real_pois_scores_and_keeps_both_inputs_immutable(saved_inputs):
    source, isochrone = saved_inputs
    before, iso_before = deepcopy(source), isochrone.model_dump(mode='json')
    job = preparation_tool().prepare_group(source, isochrone, 'elderly_disability', 'internal-elderly-source')
    report = job.result
    assert source == before
    assert isochrone.model_dump(mode='json') == iso_before
    assert report.isochrone == isochrone
    assert len(report.categories) == 5
    assert len(report.pois) == 64  # Keep outside candidates, not only the newly counted point.
    home_care = next(row for row in report.categories if row.facility_id == 'home_care_service')
    assert before['result']['categories'][16]['count'] == 0
    assert (home_care.count, home_care.known_count, home_care.score) == (1, 1, 60.0)
    assert (report.score, report.groups[0].score) == (12.5, 12.5)
    kept_ids = {poi.poi_id for poi in report.pois}
    assert [route.model_dump(mode='json') for route in report.routes] == [
        route for route in before['result']['routes'] if route['target_id'] in kept_ids]
    assert report.quality['source_report_quality'] == before['result']['quality']
    expected_hash = hashlib.sha256(json.dumps(before, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert report.quality['source_report_sha256'] == expected_hash
    assert report.quality['reused_poi_search'] is True
    assert report.quality['reused_center_isochrone'] is True
    assert report.quality['required_search_radius_m'] == 1899
    report.isochrone.center.lng += .00001
    assert isochrone.model_dump(mode='json') == iso_before


def test_prepared_source_has_no_old_coverage_or_measured_service_area_claim(saved_inputs):
    source, isochrone = saved_inputs
    source = deepcopy(source)
    source['result']['summary'] = 'shared500 500米共享代表点旧结果'
    job = preparation_tool().prepare_group(source, isochrone, 'daily_commerce', 'internal-commerce-source')
    report = job.result
    # completed describes this offline input snapshot; it remains visibly pending for coverage.
    assert job.status == 'completed'
    assert job.stage == 'prepared_actual_poi_source'
    assert report.quality['internal_source_snapshot'] is True
    assert report.quality['coverage_status'] == 'pending'
    assert report.quality['coverage_computed'] is False
    assert report.quality['target_coverage_method'] == ACTUAL_POI_METHOD
    assert report.quality.get('coverage_method') is None
    assert report.quality['new_http_requests'] == 0
    assert report.blindspot == {}
    assert all((row.covered_ratio, row.blind_ratio, row.unknown_ratio) == (0, 0, 1)
               for row in report.categories)
    visible = report.summary + ''.join(group.summary for group in report.groups)
    assert '待' in visible and '内部来源快照' in visible
    assert 'shared500' not in visible and '500米共享' not in visible
    assert any(w.code == 'COVERAGE_PENDING' for w in report.warnings)
    CheckupJob.model_validate_json(job.model_dump_json())


def test_incomplete_search_remains_incomplete_and_provisional(saved_inputs):
    source, isochrone = saved_inputs
    source = deepcopy(source)
    category = next(row for row in source['result']['categories'] if row['facility_id'] == 'restaurant')
    category.update(search_status='partial', count=None, score_provisional=True)
    job = preparation_tool().prepare_group(source, isochrone, 'daily_commerce', 'internal-partial-source')
    restaurant = next(row for row in job.result.categories if row.facility_id == 'restaurant')
    assert (restaurant.search_status, restaurant.count, restaurant.known_count) == ('partial', None, 37)
    assert restaurant.score == 100 and restaurant.score_provisional is True
    assert job.result.score_provisional is True
    assert restaurant.unknown_ratio == 1


def test_recounts_again_when_a_different_same_center_circle_is_supplied(saved_inputs):
    source, isochrone = saved_inputs
    raw_iso = isochrone.model_dump(mode='json')
    lng, lat = isochrone.center.lng, isochrone.center.lat
    raw_iso.update(tiers=None, time_field=None, geometry=dict(type='Polygon', coordinates=[[
        [lng-.00001, lat-.00001], [lng+.00001, lat-.00001],
        [lng+.00001, lat+.00001], [lng-.00001, lat+.00001], [lng-.00001, lat-.00001]]]))
    small = IsochroneData.model_validate(raw_iso)
    report = preparation_tool().prepare_group(source, small, 'elderly_disability', 'internal-small-source').result
    assert report.score == 0
    assert all(row.count == 0 and row.score == 0 for row in report.categories)
    assert len(report.pois) == 64
    assert not any(poi.in_isochrone for poi in report.pois)


@pytest.mark.parametrize('change,match', [
    ('status', 'completed'), ('result', 'completed'), ('center', 'centre|center'),
    ('source_iso_center', 'centre|center'), ('catalog', 'catalog'), ('crs', 'BD09LL'),
])
def test_rejects_incompatible_historical_sources(saved_inputs, change, match):
    source, isochrone = saved_inputs
    source = deepcopy(source)
    if change == 'status':
        source['status'] = 'running'
    elif change == 'result':
        source['result'] = None
    elif change == 'center':
        source['result']['center']['lng'] += .001
    elif change == 'source_iso_center':
        source['result']['isochrone']['center']['lng'] += .001
    elif change == 'catalog':
        source['result']['catalog_version'] = 'different-version'
    else:
        source['result']['center']['crs'] = 'WGS84'
    with pytest.raises(ValueError, match=match):
        preparation_tool().prepare_group(source, isochrone, 'elderly_disability', 'internal-invalid-source')


@pytest.mark.parametrize('change,match', [('center', 'centre|center'), ('time', '900'), ('grid', '100'), ('crs', 'BD09LL')])
def test_rejects_incompatible_new_isochrones(saved_inputs, change, match):
    source, isochrone = saved_inputs
    raw_iso = isochrone.model_dump(mode='json')
    raw_iso.update(tiers=None, time_field=None)
    if change == 'center':
        raw_iso['center']['lng'] += .001
    elif change == 'time':
        raw_iso['time_limit_s'] = 600
    elif change == 'grid':
        raw_iso['quality']['grid_cell_m'] = 200
    else:
        raw_iso['center']['crs'] = 'WGS84'
    iso = IsochroneData.model_construct(**raw_iso) if change == 'crs' else IsochroneData.model_validate(raw_iso)
    with pytest.raises(ValueError, match=match):
        preparation_tool().prepare_group(source, iso, 'elderly_disability', 'internal-invalid-iso')


def test_rejects_missing_outer_candidates_instead_of_renaming_search_complete(saved_inputs):
    source, isochrone = saved_inputs
    source = deepcopy(source)
    source['result']['search_radius_m'] = 1898
    before = deepcopy(source)
    with pytest.raises(ValueError, match=r'search_radius_m.*1898.*1899'):
        preparation_tool().prepare_group(source, isochrone, 'daily_commerce', 'internal-short-search')
    assert source == before
    source['result']['search_radius_m'] = 1899
    assert preparation_tool().prepare_group(source, isochrone, 'daily_commerce', 'internal-exact-search').result.score == 67.5


def test_recompute_accepts_snapshot_but_no_provider_is_called_when_budget_is_zero(saved_inputs):
    from tools.recompute_actual_poi_case import recompute_actual_case
    source, isochrone = saved_inputs
    snapshot = preparation_tool().prepare_group(CheckupJob.model_validate(source), isochrone,
                                               'elderly_disability', 'internal-measure-source')
    def forbidden(_):
        raise AssertionError('Preparing or replaying this snapshot must stay offline')
    result = recompute_actual_case(snapshot, forbidden, report_id='pending-measurement-result', max_route_pairs=0)
    assert result.result.score == 12.5
    assert result.result.quality['coverage_method'] == ACTUAL_POI_METHOD
    assert result.result.quality['new_http_requests'] == 0


def test_cli_writes_new_internal_snapshot_and_refuses_to_overwrite(saved_inputs, tmp_path):
    tool = preparation_tool()
    output = tmp_path / 'internal-source.json'
    args = ['--source', str(SOURCE), '--isochrone', str(CURRENT), '--group', 'elderly_disability',
            '--id', 'internal-cli-source', '--output', str(output)]
    assert tool.main(args) == 0
    raw = output.read_bytes()
    saved = CheckupJob.model_validate_json(raw)
    assert saved.result.score == 12.5
    assert saved.result.quality['coverage_computed'] is False
    with pytest.raises(SystemExit) as error:
        tool.main(args)
    assert error.value.code == 2
    assert output.read_bytes() == raw
