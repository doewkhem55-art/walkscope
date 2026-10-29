"""Run one bounded real 45-category fixed checkup and save its public report locally.

Requires RUN_BAIDU_CHECKUP=1 and a server-side BAIDU_MAP_AK in the environment.
The report is stored under ignored data/cache/; no key or provider raw response is saved.
"""

import json
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.checkups import get_checkup_runner
from app.modules.checkup.service import CheckupError
from app.schemas.checkup import CheckupRequest


def main() -> int:
    if os.environ.get("RUN_BAIDU_CHECKUP") != "1":
        print("Skipped: set RUN_BAIDU_CHECKUP=1 for one real checkup.")
        return 2
    if not os.environ.get("BAIDU_MAP_AK", "").strip():
        print("Skipped: server-side BAIDU_MAP_AK is not configured.")
        return 2

    request = CheckupRequest.model_validate({
        "location": {"type": "address", "query": "同济大学四平路校区", "city": "上海市"},
    })
    report_id = uuid.uuid4().hex
    print("report_id:", report_id, flush=True)
    try:
        report = get_checkup_runner()(
            request,
            report_id=report_id,
            progress=lambda stage, value: print("progress:", stage, value, flush=True),
        )
    except CheckupError as exc:
        print("checkup_error:", exc.code, exc.message, flush=True)
        return 1
    except Exception as exc:
        print("checkup_exception_type:", type(exc).__name__, flush=True)
        return 1

    output = ROOT / "data" / "cache" / f"checkup-live-{report_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", output.relative_to(ROOT), flush=True)
    print("score:", report.score, flush=True)
    print("category_statuses:", {
        status: sum(category.search_status == status for category in report.categories)
        for status in ("complete", "partial", "failed")
    }, flush=True)
    print("group_scores:", [(group.id, group.score) for group in report.groups], flush=True)
    print("quality:", report.quality, flush=True)
    print("warning_codes:", sorted({warning.code for warning in report.warnings}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
