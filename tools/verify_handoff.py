"""Offline smoke check of the actual shipped app and saved reports. No provider IO."""
from pathlib import Path
import hashlib, json, os, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('BAIDU_MAP_AK','DEEPSEEK_API_KEY','CHECKUP_REPLAY_DIR','CHECKUP_REPLAY_REPORTS','CHECKUP_STAGE_MANIFEST','CHECKUP_DRAFT_DIR'):
    os.environ.pop(name,None)
os.environ['CHECKUP_REPORT_DIR']=str(ROOT/'data/reports')
from fastapi.testclient import TestClient
from app.api.main import app

def main():
    from app.modules.checkup.catalog import load_catalog
    from app.schemas.checkup import CheckupJob
    catalog=load_catalog()
    assert len(catalog['groups'])==7 and len(catalog['facilities'])==45
    records=[]
    for path in sorted((ROOT/'data/reports').glob('*_report.json')):
        record=CheckupJob.model_validate_json(path.read_text(encoding='utf-8'))
        assert record.status in ('completed','failed','cancelled')
        records.append(record.id)
    with TestClient(app) as client:
        assert client.get('/health').json()=={'status':'ok'}
        runtime=client.get('/api/v1/checkup-catalog').json()['runtime']
        assert runtime['coverage_grid_size_m']==20 and runtime['checkup_cancel_supported'] is True
        assert runtime['map_service_configured'] is False
        primary=client.get('/api/v1/checkups/tongji-all7-small-updated-20261027').json()
        assert primary['status']=='completed' and primary['result']['score']==55
        assert len(primary['result']['categories'])==45 and len(primary['result']['groups'])==7
        raw=(ROOT/'data/reports/tongji-all7-small-updated-20261027_report.json').read_bytes()
        assert hashlib.sha256(raw).hexdigest()=='9d944fb334035df407c1a4fb741a4e4a2859ba983e14cf20391ce2aa821c17cf'
        assert (ROOT/'frontend/public/cases/tongji.json').read_bytes()==raw
        session='00000000-0000-4000-8000-000000000001'
        draft=client.get('/api/v1/checkups/'+primary['id']+'/draft',params={'session_id':session}).json()
        assert len(draft['chapters'])==6 and draft['version']==1
        word=client.post('/api/v1/checkups/'+primary['id']+'/draft/export',json={'expected_version':1,'session_id':session})
        assert word.status_code==200 and word.content.startswith(b'PK')
        ended=client.get('/api/v1/checkups/6374446c22fc4bb0bfbeb3306dd4a911').json()
        assert ended['status']=='cancelled' and len(ended['partial_result']['pois'])==97
        # Both are idempotent reads of a terminal state, no new checkup is submitted.
        for action in ('cancel','resume'):
            result=client.post('/api/v1/checkups/'+ended['id']+'/'+action,json={})
            assert result.status_code==200 and result.json()['status']=='cancelled'
        for action in ('pause','resume','cancel'):
            assert '/api/v1/checkups/{checkup_id}/'+action in app.openapi()['paths']
    print(json.dumps({'ok':True,'provider_requests':0,'records_validated':len(records),'groups':7,'categories':45,'tongji_score':55,'coverage_grid_size_m':20,'cancelled_record_retained':True,'draft_chapters':6,'word_export':True,'current_case_byte_equal':True},ensure_ascii=False))
if __name__=='__main__':main()
