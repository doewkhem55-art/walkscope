"""P0 saved-case draft endpoints, isolated from checkup execution and question search."""
import asyncio
import json
import logging
import threading
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from app.api import checkups
from app.modules.checkup import report_drafts as drafts
from app.schemas.report_draft import DraftRequest, DraftExport

router=APIRouter(prefix='/api/v1/checkups',tags=['Report draft'])
# ponytail: one local user/worker; use a durable transaction store before multi-user deployment.
_draft_lock=threading.Lock()
logger=logging.getLogger('uvicorn.error')

def _source(checkup_id):
    if checkup_id not in drafts.CASE_IDS:
        raise HTTPException(409,detail={'code':'SAVED_CASE_REQUIRED','message':'请先载入同济七组45小项的已保存报告，再生成智能体检报告。'})
    path=checkups._report_path(checkup_id)
    job=checkups._read_persisted_job(path)
    if job is None or job.status!='completed' or job.result is None:
        raise HTTPException(409,detail={'code':'SAVED_CASE_REQUIRED','message':'同济保存报告不可用，请先载入有效结果。'})
    return job.result

def _root(session_id=None):
    root=drafts.directory(checkups._report_directory())
    return root/'sessions'/str(session_id) if session_id is not None else root

def _current(root, report, session_id=None):
    try:
        current=drafts.load_draft(root,report)
        if current is None and session_id is not None:
            current=drafts.initial_draft(_root(),report)
        return current
    except drafts.DraftError as exc:
        raise HTTPException(409,detail={'code':exc.code,'message':exc.message}) from None

def _version(current, expected):
    if (current.version if current else 0) != expected:
        raise HTTPException(409,detail={'code':'VERSION_CONFLICT','message':'草稿版本已更新，请载入当前版本后重试。'})

@router.get('/{checkup_id}/draft')
def get_draft(checkup_id:str,session_id:UUID|None=None):
    report=_source(checkup_id)
    current=_current(_root(session_id),report,session_id)
    if current is None:raise HTTPException(404,detail={'code':'NO_DRAFT','message':'尚未生成草稿。'})
    return current

@router.post('/{checkup_id}/draft/stream')
async def edit_draft(checkup_id:str,payload:DraftRequest,request:Request,model=Depends(checkups.get_report_model)):
    checkups._check_origin(request)
    report=_source(checkup_id);root=_root(payload.session_id)
    if not _draft_lock.acquire(blocking=False):
        raise HTTPException(429,detail={'code':'DRAFT_BUSY','message':'报告正在生成或修订，请稍后再试。'})
    try:
        current=_current(root,report,payload.session_id);_version(current,payload.expected_version)
        if payload.action=='revise' and current is None:
            raise HTTPException(409,detail={'code':'NO_DRAFT','message':'请先生成报告草稿。'})
        if payload.session_id is not None and not (root/f'{report.report_id}.json').exists():
            drafts.save_draft(root,current)
    except Exception:
        _draft_lock.release();raise
    loop=asyncio.get_running_loop();queue=asyncio.Queue();stopped=threading.Event()
    class Disconnected(Exception):pass
    def emit(event):
        if stopped.is_set():raise Disconnected()
        loop.call_soon_threadsafe(queue.put_nowait,event)
    def work():
        try:
            emit({'type':'stage','stage':'source','message':'正在读取同济保存报告并锁定测算事实；不发起新的地图请求。'})
            draft=drafts.produce_draft(report,current,payload.instruction,model,emit,payload.context,root)
            emit({'type':'stage','stage':'saving','message':'正在保存本次页面的报告修改。'})
            if drafts.source_hash(_source(checkup_id))!=draft.source_hash:
                raise drafts.DraftError('SOURCE_CHANGED','保存报告已变化，当前草稿未覆盖。')
            drafts.save_draft(root,draft)
            emit({'type':'result','result':draft.model_dump(mode='json')})
        except Disconnected:pass
        except drafts.DraftError as exc:
            if not stopped.is_set():emit({'type':'error','code':exc.code,'message':exc.message})
        except Exception:
            logger.exception('Report draft operation failed')
            if not stopped.is_set():emit({'type':'error','code':'DRAFT_FAILED','message':'报告处理失败，已确认草稿保持不变；可载入当前版本后重试。'})
        finally:_draft_lock.release()
    async def stream():
        worker=threading.Thread(target=work,daemon=True,name='report-draft')
        try:
            worker.start()
            while True:
                event=await queue.get()
                yield json.dumps(event,ensure_ascii=True)+'\n'
                if event['type'] in {'result','error'}:break
        finally:
            stopped.set()
            if worker.ident is None:_draft_lock.release()
    return StreamingResponse(stream(),media_type='application/x-ndjson',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})

@router.post('/{checkup_id}/draft/export')
def export_draft(checkup_id:str,payload:DraftExport,request:Request):
    checkups._check_origin(request)
    report=_source(checkup_id);root=_root(payload.session_id);current=_current(root,report,payload.session_id)
    if current is None:raise HTTPException(409,detail={'code':'NO_DRAFT','message':'请先生成并确认报告草稿。'})
    selected=current
    if payload.expected_version > current.version:_version(current,payload.expected_version)
    if current.version != payload.expected_version:
        try:selected=drafts.load_draft(root,report,payload.expected_version)
        except drafts.DraftError as exc:
            raise HTTPException(409,detail={'code':exc.code,'message':exc.message}) from None
        if selected is None:_version(current,payload.expected_version)
    return Response(drafts.export_docx(selected),media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    headers={'Content-Disposition':'attachment; filename="tongji-planning-report.docx"','Cache-Control':'no-store'})
