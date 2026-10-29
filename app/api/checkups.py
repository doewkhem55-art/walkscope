"""Bounded single-process jobs for the fixed workflow, independent of Agent V2."""

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.control import CheckupControl, CheckupInterrupted, CheckupCancelled
from app.modules.checkup.questions import answer_report
from app.modules.checkup.service import COVERAGE_GRID_SIZE_M, COVERAGE_METHOD, CheckupError, run_checkup
from app.modules.isochrone.grid import generate_grid_isochrone
from app.modules.isochrone.service import IsochroneService
from app.modules.location.service import resolve_location
from app.modules.routing.service import RoutingService
from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.geocoding import BaiduGeocodingProvider
from app.providers.baidu.poi import BaiduPOIProvider
from app.providers.baidu.routing import BaiduRoutingProvider
from app.providers.deepseek import DeepSeekLLM
from app.providers.deepseek_search import DeepSeekSearch
from app.schemas.checkup import (
    CheckupJob,
    CheckupReport,
    CheckupRequest,
    ReportAnswer,
    ReportFocus,
    ReportQuestion,
)
from app.schemas.common import ErrorDetail


logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/v1", tags=["Point checkup"])
_lock = threading.Lock()
_network_lock = threading.Lock()
_last_network_call = 0.0
_jobs = {}
_controls = {}
_execution = threading.local()
_checkpoint_lock = threading.Lock()
_questions_running = set()
# ponytail: local single-worker service; use durable jobs/auth before multi-user deployment.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkup")
_MAX_RECORDS = 10
_TTL_SECONDS = 24 * 3600
try:
    _HTTP_MIN_INTERVAL_SECONDS = max(0.2, float(os.environ.get("BAIDU_HTTP_MIN_INTERVAL_S", "1.5")))
except ValueError:
    _HTTP_MIN_INTERVAL_SECONDS = 1.5


def _checkpoint():
    control = getattr(_execution, 'control', None)
    if control is not None:
        control.checkpoint()


def _active_time():
    control = getattr(_execution, 'control', None)
    return control.active_time() if control is not None else time.monotonic()


def stop_checkup_workers():
    with _lock:
        controls = list(_controls.values())
    for control in controls:
        control.stop()


def _get_json(url, timeout):
    global _last_network_call
    _checkpoint()
    with _network_lock:
        _checkpoint()
        time.sleep(max(0, _HTTP_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_network_call)))
        # Pause may arrive during throttling. Gate again at the actual HTTP boundary.
        _checkpoint()
        _last_network_call = time.monotonic()
        with urlopen(url, timeout=timeout) as response:
            return json.load(response)


def _isochrone_tiers_enabled() -> bool:
    """Read ``ISOCHRONE_TIERS`` (default on); only the grid generator uses it.

    ``0``/``false``/``off``/``no`` keep the pre-tier single 900 s output
    byte-for-byte at the contract level (``tiers``/``time_field`` stay null).
    """
    value = os.environ.get("ISOCHRONE_TIERS", "1").strip().lower()
    return value not in {"0", "false", "off", "no"}


def get_checkup_runner():
    ak = os.environ.get("BAIDU_MAP_AK", "").strip()
    if not ak:
        raise HTTPException(503, detail={"code": "MISSING_SERVER_AK", "message": "请先配置百度服务端密钥；网页底图密钥不能替代。"})
    limits = {}
    for name, default, maximum in (
        ("CHECKUP_MAX_HTTP_REQUESTS", 10000, 1000000),
        ("CHECKUP_MAX_SECONDS", 14400, 604800),
        ("CHECKUP_MAX_COVERAGE_ROUTE_PAIRS", 50000, 1000000),
    ):
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            value = 0
        if not 1 <= value <= maximum:
            raise HTTPException(503, detail={"code": "INVALID_CHECKUP_LIMIT", "message": f"{name} 须为 1 到 {maximum} 的整数。"})
        limits[name] = value
    max_requests = limits["CHECKUP_MAX_HTTP_REQUESTS"]
    max_seconds = limits["CHECKUP_MAX_SECONDS"]
    limit_description = f"地图请求上限 {max_requests} 次或活动时长上限 {max_seconds} 秒"
    calls = 0
    route_pairs = 0
    started = None
    exhausted = False

    def transport(url, timeout):
        nonlocal calls, route_pairs, started, exhausted
        _checkpoint()
        if started is None:
            started = _active_time()
        if calls >= max_requests or _active_time() - started >= max_seconds:
            exhausted = True
            # Control flow, not a provider timeout: no retry, no backoff sleep,
            # and no further request in this task.
            raise ProviderStopError(
                "PROVIDER_BUDGET_EXHAUSTED",
                f"本次体检达到{limit_description}（已发出 {calls} 次请求、尝试 {route_pairs} 条步行路线）。",
            )
        calls += 1
        parts = urlsplit(url)
        if parts.path == "/routematrix/v2/walking":
            destinations = parse_qs(parts.query).get("destinations", [])
            route_pairs += len(destinations[0].split("|")) if destinations else 0
        elif parts.path == "/directionlite/v1/walking":
            # Single-route overflow channel: one origin->destination per request.
            route_pairs += 1
        return _get_json(url, timeout)

    try:
        route_batch_size = int(os.environ.get("BAIDU_ROUTING_BATCH_SIZE", "25"))
    except ValueError:
        raise HTTPException(503, detail={"code": "INVALID_ROUTE_BATCH_SIZE", "message": "步行算路单批路线数配置无效。"}) from None
    if not 1 <= route_batch_size <= 50:
        raise HTTPException(503, detail={"code": "INVALID_ROUTE_BATCH_SIZE", "message": "步行算路单批路线数须为 1 到 50。"})
    routing = RoutingService(BaiduRoutingProvider(ak, transport=transport), batch_size=route_batch_size,
                             max_retries=1, retry_delay_seconds=2,
                             fallback_provider=BaiduDirectionProvider(ak, transport=transport))
    algorithm = os.environ.get("ISOCHRONE_ALGORITHM", "grid").strip().lower()
    if algorithm == "radial":
        generate_isochrone = IsochroneService.detailed(routing.route).generate
    else:
        try:
            grid_cell = float(os.environ.get("ISOCHRONE_GRID_CELL_M", "100"))
        except ValueError:
            raise HTTPException(503, detail={"code": "INVALID_ISOCHRONE_CONFIG", "message": "等时圈网格配置无效。"}) from None
        generate_isochrone = partial(
            generate_grid_isochrone, routing.route, radius_m=2500.0, cell_m=grid_cell,
            include_tiers=_isochrone_tiers_enabled(), max_grid_points=2500,
        )
    run = partial(run_checkup,
        resolve_location=partial(resolve_location, geocode=BaiduGeocodingProvider(ak, transport=transport).geocode),
        generate_isochrone=generate_isochrone,
        poi_provider=BaiduPOIProvider(ak=ak, http_get=transport), route=routing.route,
        max_grid_routes=limits["CHECKUP_MAX_COVERAGE_ROUTE_PAIRS"],
        stage_manifest=os.environ.get("CHECKUP_STAGE_MANIFEST"))

    def budgeted_run(*args, **kwargs):
        try:
            report = run(*args, **kwargs)
        except CheckupError as exc:
            if exhausted:
                raise CheckupError(
                    "PROVIDER_BUDGET_EXHAUSTED",
                    f"本次体检达到{limit_description}（已发出 {calls} 次请求、"
                    f"尝试 {route_pairs} 条步行路线）；已停止分析，不会生成替代数据。",
                ) from None
            raise CheckupError(
                exc.code,
                f"{exc.message}本次已发出 {calls} 次地图请求、尝试 {route_pairs} 条步行路线。",
                exc.retryable,
            ) from None
        except Exception:
            if exhausted:
                raise CheckupError(
                    "PROVIDER_BUDGET_EXHAUSTED",
                    f"本次体检达到{limit_description}（已发出 {calls} 次请求、"
                    f"尝试 {route_pairs} 条步行路线）；"
                    "已停止分析，不会生成替代数据。",
                ) from None
            raise
        report.quality.update(provider_requests=calls, route_pairs_attempted=route_pairs,
                              provider_request_limit=max_requests, provider_time_limit_s=max_seconds,
                              provider_budget_exhausted=exhausted,
                              routing_single_route_pairs=routing.fallback_pairs)
        if exhausted:
            from app.schemas.common import WarningItem
            report.warnings.append(WarningItem(code="PROVIDER_BUDGET_EXHAUSTED", message=f"本次体检达到{limit_description}；未完成部分标为未知。"))
            report.quality["confidence"] = "low"
        return report
    return budgeted_run


def get_report_model():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    # 4000 tokens leaves room for the structured answer plus sources; the adapter
    # still retries independently at the HTTP level only when configured to.
    model = DeepSeekLLM(key, max_tokens=4000, max_retries=0, timeout_seconds=60) if key else None
    try:
        yield model
    finally:
        if model is not None:
            model._client.close()


def get_report_search():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    enabled = os.environ.get("DEEPSEEK_SEARCH", "1").strip().lower() not in {"0", "false", "off"}
    search = DeepSeekSearch(key) if key and enabled else None
    try:
        yield search
    finally:
        if search is not None:
            search.close()


def _prune():
    expired = [key for key, (created, job) in _jobs.items()
               if job.status in {"completed", "failed", "cancelled"} and time.monotonic() - created > _TTL_SECONDS]
    for key in expired:
        del _jobs[key]


def _report_directory() -> Path:
    configured = os.environ.get("CHECKUP_REPORT_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2] / "data" / "reports"


def _report_path(checkup_id: str) -> Path | None:
    # IDs also arrive from the status URL; never treat them as paths.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", checkup_id):
        return None
    return _report_directory() / f"{checkup_id}_report.json"


def _read_persisted_job(path: Path) -> CheckupJob | None:
    try:
        if path.is_symlink():
            raise ValueError("Report files must not be symlinks")
        job = CheckupJob.model_validate_json(path.read_text(encoding="utf-8"))
        if _report_path(job.id) is None or path.name != f"{job.id}_report.json":
            raise ValueError("Report identity mismatch")
        if job.status == "completed":
            if (job.result is None or job.result.report_id != job.id or job.error is not None
                    or job.stage != "completed" or job.progress != 100):
                raise ValueError("Incomplete report")
        elif job.status == "cancelled":
            if job.result is not None or job.error is not None or job.stage != "cancelled":
                raise ValueError("Invalid cancelled job")
        elif job.status != "failed" or job.error is None or job.result is not None or job.stage != "failed":
            raise ValueError("Only terminal jobs can be restored")
        return job
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Ignored invalid or unreadable persisted checkup report.")
        return None


def _save_terminal_job(job: CheckupJob) -> None:
    path = _report_path(job.id)
    temporary = None
    try:
        if path is None:
            raise ValueError("Invalid report ID")
        path.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: one local worker; atomic files suffice until multi-user storage is needed.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".report-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(job.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError):
        logger.warning("Could not persist terminal checkup %s; its result remains available in memory.", job.id)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass



def _read_interrupted_pause(checkup_id):
    if _report_path(checkup_id) is None:
        return None
    path = _report_directory().parent / 'progress' / f'{checkup_id}_partial.json'
    try:
        if path.is_symlink():
            return None
        job = CheckupJob.model_validate_json(path.read_text(encoding='utf-8'))
        if job.id != checkup_id or job.status != 'paused' or job.result is not None:
            return None
    except (OSError, ValueError):
        return None
    # A persisted snapshot has data, not the original Python stack or provider state.
    job.status, job.stage = 'failed', 'failed'
    job.error = ErrorDetail(code='PAUSED_TASK_INTERRUPTED', retryable=False,
        message='服务已重启，暂停任务的执行现场已丢失；已保留已有结果，无法原地继续，请重新开始体检。')
    if job.partial_result is not None:
        job.partial_result['active_facility_id'] = None
        for group in job.partial_result.get('groups', []):
            if group.get('status') == 'searching':
                group['status'] = 'partial'
        job.partial_revision += 1
    _save_terminal_job(job)
    return job


def load_persisted_reports() -> list[str]:
    """Restore terminal jobs without re-running providers; bad files stay isolated."""
    directory = _report_directory()
    try:
        paths = list(directory.glob("*_report.json"))
    except OSError:
        logger.warning("Could not read the persisted checkup directory.")
        return []
    # Keep startup memory bounded. Older terminal jobs remain available through
    # checkup_status's validated on-demand disk lookup.
    paths.sort(key=lambda path: (path.stat().st_mtime_ns if path.exists() else 0, path.name), reverse=True)
    paths = paths[:_MAX_RECORDS]
    loaded = []
    with _lock:
        for path in paths:
            job = _read_persisted_job(path)
            if job is not None and job.id not in _jobs:
                _jobs[job.id] = (time.monotonic(), job)
                loaded.append(job.id)
        # Only paused checkpoints are restored, honestly as interrupted failures.
        # Running snapshots retain the existing behavior and never restart providers.
        try:
            checkpoints = sorted((directory.parent / 'progress').glob('*_partial.json'))
        except OSError:
            checkpoints = []
        for path in checkpoints:
            if len(_jobs) >= _MAX_RECORDS:
                break
            identifier = path.name.removesuffix('_partial.json')
            if identifier not in _jobs and (job := _read_interrupted_pause(identifier)) is not None:
                _jobs[job.id] = (time.monotonic(), job)
                loaded.append(job.id)
    return loaded


# ---------------------------------------------------------------------------
# Saved-report replay (offline evidence without re-running the checkup)
# ---------------------------------------------------------------------------

def load_replay_reports(paths) -> list[str]:
    """Load saved completed reports into the in-memory job store.

    The formal question pipeline (model + search) then works on these reports
    exactly as it does on a live one; no map/facility request is made.
    """
    loaded: list[str] = []
    with _lock:
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and "result" in payload:
                job = CheckupJob.model_validate(payload)
            else:
                report = CheckupReport.model_validate(payload)
                job = CheckupJob(id=report.report_id, status="completed", stage="completed",
                                 progress=100, result=report)
            if job.status != "completed" or job.result is None:
                continue
            replay_job = job.model_copy(deep=True)
            replay_job.result.quality.update({
                "historical_replay": True,
                "not_live_checkup": True,
                "replay_source_id": replay_job.id,
                "replay_source_created_at": replay_job.result.created_at,
            })
            _jobs[job.id] = (time.monotonic(), replay_job)
            loaded.append(job.id)
    return loaded


def load_replay_reports_from_env() -> list[str]:
    """``CHECKUP_REPLAY_DIR`` (directory) and/or ``CHECKUP_REPLAY_REPORTS`` (files)."""
    paths: list[Path] = []
    directory = os.environ.get("CHECKUP_REPLAY_DIR", "").strip()
    if directory:
        candidate = Path(directory).expanduser()
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*_report.json")))
            if not paths:
                paths.extend(sorted(candidate.glob("*.json")))
    files = os.environ.get("CHECKUP_REPLAY_REPORTS", "").strip()
    if files:
        paths.extend(Path(item.strip()).expanduser() for item in files.split(",") if item.strip())
    seen, unique = set(), []
    for path in paths:
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return load_replay_reports(unique)


def _validate_question_focus(report: CheckupReport, focus: ReportFocus | None) -> None:
    if focus is None:
        return
    facility_ids = {category.facility_id for category in report.categories}
    group_ids = {group.id for group in report.groups}
    poi_ids = {poi.poi_id for poi in report.pois}
    problems = []
    if focus.facility_id is not None and focus.facility_id not in facility_ids:
        problems.append("设施类别")
    if focus.group_id is not None and focus.group_id not in group_ids:
        problems.append("设施组")
    if focus.poi_id is not None and focus.poi_id not in poi_ids:
        problems.append("设施点")
    if focus.selected_facility_ids is not None and any(
        item not in facility_ids for item in focus.selected_facility_ids
    ):
        problems.append("勾选类别")
    if problems:
        raise HTTPException(422, detail=f"问答焦点（{'、'.join(problems)}）不在本报告中，请重新选择。")


def _save_progress_checkpoint(checkup_id):
    # Serialize pause/resume and worker publication writes; always snapshot the latest state.
    with _checkpoint_lock:
        with _lock:
            saved = _jobs[checkup_id][1].model_copy(deep=True)
        directory = _report_directory().parent / 'progress'
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{checkup_id}_partial.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(saved.model_dump_json(), encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            logger.warning('Could not persist progress for checkup %s.', checkup_id)


def _execute(checkup_id, payload, runner):
    with _lock:
        entry = _jobs.get(checkup_id)
        if entry is None or entry[1].status in {'completed', 'failed', 'cancelled'}:
            _controls.pop(checkup_id, None)
            return
        control = _controls.setdefault(checkup_id, CheckupControl())
    _execution.control = control

    def progress(stage, value):
        while True:
            _checkpoint()
            with _lock:
                if control.cancelled:
                    raise CheckupCancelled()
                if control.paused:
                    continue
                _jobs[checkup_id][1].status = "running"
                _jobs[checkup_id][1].stage = stage
                _jobs[checkup_id][1].progress = value
                break

    def publish(snapshot):
        if snapshot.get('report_id') != checkup_id:
            raise CheckupError('PROGRESS_IDENTITY_MISMATCH', '过程数据与当前任务不一致，已停止分析。')
        while True:
            _checkpoint()
            with _lock:
                if control.cancelled:
                    raise CheckupCancelled()
                if control.paused:
                    continue
                job = _jobs[checkup_id][1]
                job.partial_revision += 1
                job.partial_result = deepcopy(snapshot)
                break
        _save_progress_checkpoint(checkup_id)

    progress.publish = publish
    try:
        _checkpoint()
        report = runner(payload, report_id=checkup_id, progress=progress)
        _checkpoint()
        _publish_terminal_job(checkup_id, result=report)
    except CheckupInterrupted:
        pass
    except Exception as exc:
        try:
            _checkpoint()
            error = ErrorDetail(code=exc.code, message=exc.message, retryable=exc.retryable) if isinstance(exc, CheckupError) else ErrorDetail(
                code="CHECKUP_FAILED", message="本次体检未完成，请检查后端配置或稍后重试；未生成替代数据。", retryable=True)
            _publish_terminal_job(checkup_id, error=error)
        except CheckupInterrupted:
            pass
    finally:
        with _lock:
            _controls.pop(checkup_id, None)
        del _execution.control


def _publish_terminal_job(checkup_id, *, result=None, error=None):
    """Serialize terminal persistence with cancellation, so a late result cannot overwrite it."""
    while True:
        _checkpoint()
        with _checkpoint_lock, _lock:
            control = getattr(_execution, 'control', None)
            if control is not None and control.cancelled:
                raise CheckupCancelled()
            if control is not None and control.paused:
                continue
            entry = _jobs.get(checkup_id)
            if entry is None or entry[1].status == 'cancelled':
                return
            created, job = entry
            terminal = job.model_copy(deep=True)
            terminal.result, terminal.error = result, error
            if error is None:
                terminal.partial_result = None
                terminal.status, terminal.stage, terminal.progress = 'completed', 'completed', 100
            else:
                terminal.status, terminal.stage = 'failed', 'failed'
                _stop_partial_activity(terminal)
            _save_terminal_job(terminal)
            _jobs[checkup_id] = (created, terminal)
            return


def _stop_partial_activity(job):
    if job.partial_result is not None:
        job.partial_result['active_facility_id'] = None
        for group in job.partial_result.get('groups', []):
            if group.get('status') == 'searching':
                group['status'] = 'partial'
        job.partial_revision += 1


def _check_origin(request):
    # Reject cross-site POSTs even when the client can send a simple CORS request.
    origin = request.headers.get("origin")
    allowed = os.environ.get("CORS_ORIGINS") or "http://localhost:5173,http://127.0.0.1:5173"
    if origin and origin not in [s.strip() for s in allowed.split(",")]:
        raise HTTPException(403, detail="来源未获允许。")


@router.get("/checkup-catalog")
def catalog_api():
    catalog = load_catalog()
    return {**catalog, "facilities": [{k: v for k, v in f.items() if k != "search_terms"} for f in catalog["facilities"]],
            "runtime": {
                "coverage_grid_size_m": COVERAGE_GRID_SIZE_M,
                "coverage_method": COVERAGE_METHOD,
                "coverage_route_direction": "poi_to_sample",
                "coverage_origin_mode": "actual_poi",
                "coverage_origin_grid_m": None,
                "coverage_service_distance_m": 1000,
                "coverage_geometry_method": "measured_distance_triangles_1000m_isoline_v1",
                "coverage_geometry_grid_m": None,
                "isochrone_grid_cell_m": float(os.environ.get("ISOCHRONE_GRID_CELL_M", "100")),
                "checkup_cancel_supported": True,
                "map_service_configured": bool(os.environ.get("BAIDU_MAP_AK", "").strip()),
                "report_questions_mode": "model" if os.environ.get("DEEPSEEK_API_KEY", "").strip() else "report_facts",
                "search_configured": bool(
                    os.environ.get("DEEPSEEK_API_KEY", "").strip()
                    and os.environ.get("DEEPSEEK_SEARCH", "1").strip().lower() not in {"0", "false", "off"}
                ),
                "replay_report_ids": sorted(_jobs),
            }}


@router.post("/checkups", response_model=CheckupJob, status_code=202)
def submit_checkup(payload: CheckupRequest, request: Request, runner=Depends(get_checkup_runner)):
    _check_origin(request)
    with _lock:
        _prune()
        if any(job.status in {"queued", "running", "paused"} for _, job in _jobs.values()):
            raise HTTPException(429, detail="已有体检在运行，请等待它完成后再提交。")
        if len(_jobs) >= _MAX_RECORDS:
            oldest = next(iter(_jobs))
            del _jobs[oldest]
        checkup_id = uuid.uuid4().hex
        job = CheckupJob(id=checkup_id, status="queued", stage="queued", progress=0)
        _jobs[checkup_id] = (time.monotonic(), job)
        _controls[checkup_id] = CheckupControl()
        response = job.model_copy(deep=True)
    _executor.submit(_execute, checkup_id, payload, runner)
    return response


def _job_response(job):
    response = job.model_copy(deep=True)
    if response.result is not None:
        from app.modules.checkup.coverage_display import report_with_display_policy
        response.result = report_with_display_policy(response.result)
    return response


@router.get("/checkups/{checkup_id}", response_model=CheckupJob)
def checkup_status(checkup_id: str, after_revision: int | None = None):
    with _lock:
        _prune()
        if checkup_id not in _jobs:
            path = _report_path(checkup_id)
            saved = _read_persisted_job(path) if path is not None else None
            if saved is None:
                saved = _read_interrupted_pause(checkup_id)
            if saved is None:
                raise HTTPException(404, detail="体检记录不存在或已过期，请重新体检。")
            _jobs[checkup_id] = (time.monotonic(), saved)
        job = _jobs[checkup_id][1]
        # Percent-only polls stay small; a reconnect without a revision receives
        # the complete process snapshot for this task.
        if after_revision == job.partial_revision and job.status in {'queued', 'running', 'paused'}:
            return job.model_copy(update={'partial_result': None})
        return _job_response(job)



def _change_pause(checkup_id, request, *, paused):
    _check_origin(request)
    checkup_status(checkup_id)  # Validated disk lookup; a restarted paused checkpoint becomes failed.
    with _lock:
        job = _jobs[checkup_id][1]
        if job.status in {'completed', 'failed', 'cancelled'}:
            return _job_response(job)
        control = _controls.get(checkup_id)
        if control is None:
            raise HTTPException(409, detail="任务执行现场已丢失，无法原地继续，请重新体检。")
        if paused:
            control.pause()
            job.status = 'paused'
        else:
            control.resume()
            job.status = 'queued' if job.stage == 'queued' else 'running'
        response = job.model_copy(deep=True)
    _save_progress_checkpoint(checkup_id)
    return response


@router.post("/checkups/{checkup_id}/pause", response_model=CheckupJob)
def pause_checkup(checkup_id: str, request: Request):
    return _change_pause(checkup_id, request, paused=True)


@router.post("/checkups/{checkup_id}/resume", response_model=CheckupJob)
def resume_checkup(checkup_id: str, request: Request):
    return _change_pause(checkup_id, request, paused=False)



@router.post("/checkups/{checkup_id}/cancel", response_model=CheckupJob)
def cancel_checkup(checkup_id: str, request: Request):
    _check_origin(request)
    checkup_status(checkup_id)
    # Same persistence lock as final publication: whichever finishes first wins.
    with _checkpoint_lock, _lock:
        job = _jobs[checkup_id][1]
        if job.status in {'completed', 'failed', 'cancelled'}:
            return _job_response(job)
        control = _controls.get(checkup_id)
        if control is not None:
            control.cancel()
        cancelled = job.model_copy(update={
            'status': 'cancelled', 'stage': 'cancelled', 'result': None, 'error': None,
        }, deep=True)
        _stop_partial_activity(cancelled)
        _save_terminal_job(cancelled)
        _jobs[checkup_id] = (_jobs[checkup_id][0], cancelled)
        return cancelled.model_copy(deep=True)


@router.post("/checkups/{checkup_id}/questions", response_model=ReportAnswer)
def question_api(checkup_id: str, payload: ReportQuestion, request: Request,
                 model=Depends(get_report_model), search=Depends(get_report_search)):
    _check_origin(request)
    job = checkup_status(checkup_id)
    if job.status != "completed" or job.result is None:
        raise HTTPException(409, detail="请先完成体检，再就该报告提问。")
    _validate_question_focus(job.result, payload.focus)
    with _lock:
        if checkup_id in _questions_running:
            raise HTTPException(429, detail="上一个问题正在解读，请稍后再问。")
        _questions_running.add(checkup_id)
    try:
        return answer_report(
            job.result, payload.question, model, search,
            history=payload.history, focus=payload.focus, mode=payload.mode,
        )
    except Exception:
        # Keep the client message generic but never lose the cause: retries and
        # acceptance debugging both need it in the server log.
        logger.exception("report question failed for checkup %s", checkup_id)
        raise HTTPException(502, detail="报告解读暂不可用；体检结果不受影响。") from None
    finally:
        with _lock:
            _questions_running.discard(checkup_id)


@router.post("/checkups/{checkup_id}/questions/stream")
async def question_stream_api(checkup_id: str, payload: ReportQuestion, request: Request,
                              model=Depends(get_report_model), search=Depends(get_report_search)):
    """Additive NDJSON endpoint; the legacy JSON route keeps its contract.

    The bounded question worker owns the in-flight lock until it actually exits,
    including when a browser disconnects during an outstanding provider call.
    """
    _check_origin(request)
    job = checkup_status(checkup_id)
    if job.status != "completed" or job.result is None:
        raise HTTPException(409, detail="请先完成体检，再就该报告提问。")
    _validate_question_focus(job.result, payload.focus)
    with _lock:
        if checkup_id in _questions_running:
            raise HTTPException(429, detail="上一个问题正在解读，请稍后再问。")
        _questions_running.add(checkup_id)
    loop = asyncio.get_running_loop()
    events = asyncio.Queue()
    stopped = threading.Event()

    class StreamStopped(Exception):
        pass

    def emit(event):
        if stopped.is_set():
            raise StreamStopped()
        loop.call_soon_threadsafe(events.put_nowait, event)

    def work():
        try:
            result = answer_report(
                job.result, payload.question, model, search,
                history=payload.history, focus=payload.focus, mode=payload.mode,
                on_event=emit,
            )
            emit({"type": "result", "result": result.model_dump(mode="json")})
        except StreamStopped:
            pass
        except Exception:
            logger.exception("report question stream failed for checkup %s", checkup_id)
            if not stopped.is_set():
                emit({"type": "draft_reset"})
                emit({"type": "error", "code": "question", "message": "报告解读暂不可用；可在同一份报告上重试。"})
        finally:
            with _lock:
                _questions_running.discard(checkup_id)

    async def stream():
        worker = threading.Thread(target=work, daemon=True, name="report-question")
        try:
            worker.start()
            while True:
                event = await events.get()
                yield json.dumps(event, ensure_ascii=True) + "\n"
                if event["type"] in {"result", "error"}:
                    break
        finally:
            stopped.set()
            if worker.ident is None:
                with _lock:
                    _questions_running.discard(checkup_id)

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })
