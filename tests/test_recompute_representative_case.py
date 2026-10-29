"""Provider-free checks for safe reuse by the single-group live tool."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.recompute_representative_case import (
    load_coverage_samples_source, load_isochrone_source, subset_report,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def saved_case():
    source = json.loads((ROOT / 'data/reports/3de7777d550f4562afc38d96140f0118_report.json').read_text())
    iso_path = ROOT / 'data/progress/tongji-education-shared500-20261024_isochrone.json'
    iso = load_isochrone_source(iso_path, source['result']['center'])
    return source, iso


def test_recount_health_scope_preserves_source_and_names_actual_group(saved_case):
    source, iso = saved_case
    original = deepcopy(source)
    result = subset_report(source, iso, 'health_medical', 'health-audit')
    assert source == original
    assert len(result.categories) == 7
    assert all(c.group_id == 'health_medical' for c in result.categories)
    assert sum(p.in_isochrone for p in result.pois) == 7
    warning = next(w.message for w in result.warnings if w.code == 'SELECTED_SCOPE')
    assert '健康医疗7个小项' in warning and '教育与儿童' not in warning


@pytest.mark.parametrize('change', ['center', 'seconds', 'grid'])
def test_isochrone_reuse_rejects_other_scope(tmp_path, saved_case, change):
    source, iso = saved_case
    raw = iso.model_dump(mode='json')
    if change == 'center':
        raw['center']['lng'] += .001
    elif change == 'seconds':
        # Remove optional 900-second evidence so the intended guard is tested.
        raw.update(time_limit_s=600, tiers=None, time_field=None)
    else:
        raw['quality']['grid_cell_m'] = 200
        raw['time_field'] = None
    path = tmp_path / 'other-iso.json'
    text = json.dumps(raw)
    path.write_text(text)
    with pytest.raises(ValueError):
        load_isochrone_source(path, source['result']['center'])
    assert path.read_text() == text


def test_coverage_cache_reuses_only_finite_nonnegative_measurements(tmp_path, saved_case):
    source, iso = saved_case
    raw = json.loads((ROOT / 'data/reports/tongji-education-shared500-20261024_report.json').read_text())
    layers = raw['result']['blindspot']
    sample = next(f for f in layers['route_samples']['features'] if f['properties']['distance_m'] is not None)
    layers['route_samples']['features'] = []
    for distance in [123.0, None, -1, float('inf'), float('nan'), True]:
        item = deepcopy(sample)
        item['properties']['distance_m'] = distance
        layers['route_samples']['features'].append(item)
    path = tmp_path / 'coverage.json'
    text = json.dumps(raw)
    path.write_text(text)
    cache = load_coverage_samples_source(path, source['result']['center'], iso)
    assert sum(len(origin['samples']) for origin in cache.values()) == 1
    assert next(value for origin in cache.values() for value in origin['samples'].values()) == 123.0
    assert path.read_text() == text

    raw['result']['quality']['coverage_method'] = 'other-method'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='method differs'):
        load_coverage_samples_source(path, source['result']['center'], iso)


def test_coverage_cache_rejects_other_center_or_geometry(tmp_path, saved_case):
    source, iso = saved_case
    path = ROOT / 'data/reports/tongji-education-shared500-20261024_report.json'
    other_center = source['result']['center'] | {'lng': source['result']['center']['lng'] + .001}
    with pytest.raises(ValueError, match='centre differs'):
        load_coverage_samples_source(path, other_center, iso)

    raw = json.loads(path.read_text())
    def shifted(value):
        if len(value) == 2 and isinstance(value[0], (int, float)):
            return [value[0] + .001, value[1]]
        return [shifted(item) for item in value]
    raw['result']['isochrone']['geometry']['coordinates'] = shifted(raw['result']['isochrone']['geometry']['coordinates'])
    changed = tmp_path / 'shifted-coverage.json'
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='geometry differs'):
        load_coverage_samples_source(changed, source['result']['center'], iso)
