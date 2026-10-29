from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evidence():
    files = [
        'wuhan-zhongnan-education-complete-20261026-groups-34',
        '12de2c15ac4a44798a2bdbeff6ff2a8f',
        'wuhan-zhongnan-education-restored-20261026',
    ]
    values = [(ROOT / 'data/reports' / f'{name}_report.json').read_bytes() for name in files]
    # The immutable intermediate contains a real domain overshoot. Construct a
    # valid partition fixture in memory; the untouched file has its own failure
    # regression below, and no saved report is rewritten by these tests.
    from shapely import make_valid
    from shapely.geometry import shape, mapping
    from shapely.ops import transform, unary_union
    from app.modules.checkup.service import _projection
    from app.modules.blindspot.service import _polygon_parts
    from app.schemas.common import CenterPoint
    payload = json.loads(values[0])
    report = payload['result']
    project, inverse = _projection(CenterPoint.model_validate(report['center']))
    domain = transform(project, shape(report['isochrone']['geometry']))
    to_geo = lambda x, y: (inverse(x, y).lng, inverse(x, y).lat)
    records = [report['blindspot'], *report['blindspot']['group_coverage'].values(),
               report['blindspot']['total_coverage']]
    for record in records:
        for name in ('covered_areas', 'blind_spots', 'unknown_areas'):
            for feature in record[name]['features']:
                region = unary_union(_polygon_parts(make_valid(transform(project, shape(feature['geometry']))).intersection(domain)))
                feature['geometry'] = mapping(transform(to_geo, region))
        if 'covered_ratio' in record:
            for name, key in [('covered_areas', 'covered_ratio'), ('blind_spots', 'blind_ratio'), ('unknown_areas', 'unknown_ratio')]:
                region = unary_union([transform(project, shape(f['geometry'])) for f in record[name]['features']])
                record[key] = min(1, max(0, region.area/domain.area))
    for row in report['categories']:
        for name, key in [('covered_areas', 'covered_ratio'), ('blind_spots', 'blind_ratio'), ('unknown_areas', 'unknown_ratio')]:
            region = unary_union([transform(project, shape(f['geometry'])) for f in report['blindspot'][name]['features']
                                  if f['properties']['facility_type'] == row['facility_id']])
            row[key] = min(1, max(0, region.area/domain.area))
    values[0] = json.dumps(payload).encode()
    return values


def test_offline_audit_preserves_input_bytes_and_checks_all_partitions(evidence):
    audit = importlib.import_module('verify.audit_zhongnan_20261026')
    before = deepcopy(evidence)
    result = audit.audit_report(*evidence)
    assert result['ok']
    assert result['poi_category_records'] == 191
    assert result['center_routes'] == 78
    assert len(result['categories']) == 6
    assert result['groups']['education_children']['unverified_poi_count'] == 0
    assert evidence == before


@pytest.mark.parametrize('damage', ['score', 'overlap', 'secret', 'require_complete'])
def test_audit_rejects_changed_evidence_overlap_secrets_and_incomplete_claims(evidence, damage):
    audit = importlib.import_module('verify.audit_zhongnan_20261026')
    payload = json.loads(evidence[0])
    kwargs = {}
    if damage == 'score':
        payload['result']['score'] = 42.5
    elif damage == 'overlap':
        group = payload['result']['blindspot']['group_coverage']['education_children']
        group['blind_spots'] = deepcopy(group['covered_areas'])
    elif damage == 'secret':
        payload['result']['quality']['unexpected_secret'] = 'private-test-key'
        kwargs['secret_values'] = ['private-test-key']
    else:
        kwargs['require_complete'] = True
    with pytest.raises(AssertionError):
        audit.audit_report(json.dumps(payload).encode(), *evidence[1:], **kwargs)


def test_original_intermediate_domain_overshoot_is_rejected(evidence):
    audit = importlib.import_module('verify.audit_zhongnan_20261026')
    raw = (ROOT / 'data/reports/wuhan-zhongnan-education-complete-20261026-groups-34_report.json').read_bytes()
    with pytest.raises(AssertionError, match='education_children'):
        audit.audit_report(raw, *evidence[1:])
