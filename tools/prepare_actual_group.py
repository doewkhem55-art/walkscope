"""Prepare an offline internal group source; walking service areas remain unmeasured."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD
from app.modules.checkup.service import _projection
from app.modules.isochrone.geometry import ring_points
from app.schemas.checkup import CheckupJob
from app.schemas.common import WarningItem
from app.schemas.isochrone import IsochroneData
from tools.recompute_representative_case import subset_report


def prepare_group(source: dict | CheckupJob, isochrone: IsochroneData,
                  group_id: str, report_id: str) -> CheckupJob:
    """Recount one historical group in a compatible circle without provider access.

    The returned completed job describes preparation of an internal source snapshot,
    as required by recompute_actual_case's input contract. Its pending stage, empty
    coverage layers and all-unknown ratios explicitly exclude a measured-area claim.
    """
    raw_source = source.model_dump(mode='json') if isinstance(source, CheckupJob) else deepcopy(source)
    job = CheckupJob.model_validate(raw_source)
    if job.status != 'completed' or job.result is None:
        raise ValueError('A completed source report with a result is required')
    old = job.result
    if (not report_id or not all(char.isalnum() or char in '-_' for char in report_id)
            or report_id in {job.id, old.report_id}):
        raise ValueError('A fresh valid report id is required')
    # Revalidate instances too: model_copy/model_construct can bypass model validation.
    iso = IsochroneData.model_validate(isochrone.model_dump(mode='json', warnings=False))
    if old.center != old.isochrone.center or old.center != iso.center:
        raise ValueError('Source and isochrone must use the same centre and BD09LL coordinates')
    if iso.time_limit_s != 900 or iso.quality.grid_cell_m != 100:
        raise ValueError('The isochrone must use 900 seconds and a 100 m grid')
    catalog = load_catalog()
    if old.catalog_version != catalog['version']:
        raise ValueError('Source catalog version differs from the current catalog')
    selected = {item['id'] for item in catalog['facilities'] if item['group_id'] == group_id}
    if not selected:
        raise ValueError('Unknown group')
    if not selected <= {category.facility_id for category in old.categories}:
        raise ValueError('Source is missing categories required by this group')
    to_m, _ = _projection(iso.center)
    required_radius = math.ceil(max(math.hypot(*to_m(lng, lat))
                                   for lng, lat in ring_points(iso.geometry)) + 1000)
    if old.search_radius_m < required_radius:
        raise ValueError(f'Source search_radius_m={old.search_radius_m} is insufficient; '
                         f'at least {required_radius} m is required for circle + 1000 m candidates; '
                         'supplement POI searches before preparing this group')

    report = subset_report(raw_source, iso, group_id, report_id)
    report.blindspot = {}
    report.summary = (f'{old.location_label} · {report.groups[0].name_zh}内部来源快照：'
                      '历史设施按当前100米生活圈重计数量和评分；真实POI步行服务区待测，覆盖全部未知。')
    report.groups[0].summary = '内部来源快照；按当前生活圈重计数量，保留历史中心路线；服务区待测。'
    report.quality.update(internal_source_snapshot=True, coverage_status='pending', coverage_computed=False,
        target_coverage_method=ACTUAL_POI_METHOD, isochrone_recomputed=False, isochrone_reused=True,
        reused_center_isochrone=True, source_search_radius_m=old.search_radius_m,
        required_search_radius_m=required_radius, score_recomputed=True, new_http_requests=0,
        new_poi_search_requests=0, new_center_route_requests=0,
        source_report_warnings=[warning.model_dump(mode='json') for warning in old.warnings])
    report.warnings.append(WarningItem(code='COVERAGE_PENDING', message=(
        '这是待测的内部来源快照，尚未计算任何真实POI服务区；覆盖率0、已知盲区0、未知1。'
        'completed仅表示来源准备完成，不能作为已完成服务区结果展示。')))
    # subset_report preserves incomplete search statuses and resets every category to 0/0/1.
    return CheckupJob(id=report_id, status='completed', stage='prepared_actual_poi_source',
                      progress=100, result=report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--isochrone', type=Path, required=True,
                        help='saved IsochroneData or CheckupJob containing the current circle')
    parser.add_argument('--group', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--output', type=Path, required=True, help='new internal source snapshot path')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('destination exists; choose a new internal source snapshot path')
    try:
        raw_iso = json.loads(args.isochrone.read_text())
        iso_value = raw_iso.get('result', raw_iso)
        if not isinstance(iso_value, dict):
            raise ValueError('Isochrone source has no result')
        iso = IsochroneData.model_validate(iso_value.get('isochrone', iso_value))
        prepared = prepare_group(json.loads(args.source.read_text()), iso, args.group, args.id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            stream.write(prepared.model_dump_json(indent=2) + '\n')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(dict(saved=str(args.output), internal_source_snapshot=True,
                          coverage_status='pending', new_http_requests=0), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
