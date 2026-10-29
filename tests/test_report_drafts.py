"""P0 boundary: persisted source → two revisions → identical editable Word, offline."""
import io
import json
from pathlib import Path
import shutil
import zipfile
import pytest
from fastapi.testclient import TestClient
from app.api.main import app
from app.api import checkups

ID = 'tongji-all7-repaired-20261026'
SOURCE = Path(__file__).resolve().parents[1] / f'data/reports/{ID}_report.json'
URL = f'/api/v1/checkups/{ID}/draft'

class Model:
    def __init__(self):
        self.reply = {'sections': [{'id':'planning','paragraphs':['建议先核查设施开放条件，再开展方案比选。']}]}
    def generate_object(self, **kwargs):
        assert 'locked_facts' in json.loads(kwargs['user_message'])
        return self.reply

@pytest.fixture
def context(monkeypatch, tmp_path):
    reports = tmp_path / 'reports'; reports.mkdir()
    target = reports / SOURCE.name; shutil.copyfile(SOURCE, target)
    before = target.read_bytes()
    monkeypatch.setenv('CHECKUP_REPORT_DIR', str(reports))
    monkeypatch.setenv('CHECKUP_DRAFT_DIR', str(tmp_path/'drafts'))
    monkeypatch.setattr(checkups, '_jobs', {})
    monkeypatch.setattr(checkups, '_get_json', lambda *a, **k: pytest.fail('P0 must not call Baidu'))
    saved = app.dependency_overrides.copy()
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    with TestClient(app) as client:
        yield client, target, before
    assert target.read_bytes() == before
    app.dependency_overrides.clear(); app.dependency_overrides.update(saved)

def events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line]

def create(client):
    messages = events(client.post(URL+'/stream', json={'action':'generate','expected_version':0}))
    assert messages[0]['type'] == 'stage'
    assert messages[-1]['type'] == 'result', messages
    return messages[-1]['result']

def test_no_key_creates_honest_template_and_editable_docx(context):
    client, _, _ = context
    draft = create(client)
    assert draft['mode'] == 'report_facts' and '未配置' in draft['notice']
    assert draft['version'] == 1 and draft['facts']['score'] == 55
    assert len(draft['facts']['categories']) == 45 and len(draft['facts']['groups']) == 7
    assert client.get(URL).json() == draft
    response = client.post(URL+'/export', json={'expected_version':1})
    assert response.status_code == 200
    assert 'wordprocessingml' in response.headers['content-type']
    from docx import Document
    document = Document(io.BytesIO(response.content))
    text = '\n'.join(p.text for p in document.paragraphs)
    assert draft['title'] in text and '规划研判' in text
    assert len(document.tables) >= 3
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert 'word/document.xml' in archive.namelist()
        assert b'documentProtection' not in archive.read('word/document.xml')
    failure = events(client.post(URL+'/stream', json={'action':'revise','instruction':'第四章聚焦养老','expected_version':1}))[-1]
    assert failure['type'] == 'error' and failure['code'] == 'MODEL_NOT_CONFIGURED'
    assert client.get(URL).json() == draft

def test_two_model_revisions_persist_and_export_the_same_final_version(context):
    client, _, _ = context
    model = Model(); app.dependency_overrides[checkups.get_report_model] = lambda:model
    first = create(client)
    model.reply = {'sections':[{'id':'planning','paragraphs':['建议后续调研重点：核查养老与助残设施的服务对象、开放条件、入口通行与居家照护衔接。','建议后续调研重点：补充服务人口、年龄结构、设施容量及实际使用情况，结合现有规划研判供需。']}]}
    second = events(client.post(URL+'/stream',json={'action':'revise','expected_version':1,'instruction':'第四章更聚焦养老设施'}))[-1]['result']
    model.reply = {'sections':[{'id':'planning','paragraphs':['建议纳入本轮内部调研清单：核查养老与助残设施的服务对象、开放条件、入口通行与居家照护衔接。','建议纳入本轮内部调研清单：补充服务人口、年龄结构、设施容量及实际使用情况，结合现有规划研判供需。']}]}
    third = events(client.post(URL+'/stream',json={'action':'revise','expected_version':2,'instruction':'改成规划部门内部汇报口吻'}))[-1]['result']
    assert [first['version'],second['version'],third['version']] == [1,2,3]
    assert first['facts'] == second['facts'] == third['facts']
    assert first['source_hash'] == third['source_hash']
    assert third['chapters'][3]['paragraphs'] == ['建议纳入本轮内部调研清单：核查养老与助残设施的服务对象、开放条件、入口通行与居家照护衔接。', '建议纳入本轮内部调研清单：补充服务人口、年龄结构、设施容量及实际使用情况，结合现有规划研判供需。']
    assert second['chapters'][2] == third['chapters'][2]
    assert second['chapters'][4] == third['chapters'][4]
    # GET must read disk, surviving module/process memory loss.
    monkey = client.get(URL).json(); assert monkey == third
    from docx import Document
    response = client.post(URL+'/export',json={'expected_version':3})
    text = '\n'.join(p.text for p in Document(io.BytesIO(response.content)).paragraphs)
    assert third['chapters'][3]['paragraphs'][0] in text
    assert client.post(URL+'/export',json={'expected_version':2}).status_code == 200
    assert client.post(URL+'/stream',json={'action':'revise','expected_version':1,'instruction':'改口吻'}).status_code == 409

@pytest.mark.parametrize('reply',[
    {'sections':[{'id':'planning','paragraphs':['覆盖率为零，且服务范围不存在盲区。']}]},
    {'sections':[{'id':'findings','paragraphs':['设施配置参考分为满分。']}]},
    {'sections':[{'id':'planning','paragraphs':['小学未知区域均已确认无服务，可以按设施缺失处理。']}]},
    {'sections':[{'id':'planning','paragraphs':['设施配置参考分已改为100分。']}]},
    {'sections':[{'id':'planning','paragraphs':['小学未知比例为百分之零。']}]},
    {'sections':[{'id':'scope','paragraphs':['已达到法定规划标准。']}]},
    {'sections':[{'id':'planning','paragraphs':['所有设施均已达标，没有任何盲区。']}]},
    {'sections':[{'id':'planning','paragraphs':['设施配置参考分为九十九分。']}]},
    {'sections':[{'id':'planning','paragraphs':['养老设施覆盖率为九成。']}]},
    {'sections':[{'id':'planning','paragraphs':['已完成现场核验，全部容量调查完成。']}]},
])
def test_model_cannot_replace_locked_facts_or_claim_completeness(context, reply):
    client, _, _ = context
    original = create(client)
    model = Model(); model.reply=reply
    app.dependency_overrides[checkups.get_report_model] = lambda:model
    result = events(client.post(URL+'/stream',json={'action':'revise','instruction':'调整表达','expected_version':1}))[-1]
    assert result['type'] == 'error' and result['code'] == 'FACT_PROTECTION'
    assert client.get(URL).json() == original

def test_failed_model_and_fact_change_instruction_preserve_confirmed_draft(context):
    client, _, _ = context; original=create(client)
    class Broken:
        def generate_object(self, **kwargs): raise RuntimeError('private provider detail')
    app.dependency_overrides[checkups.get_report_model] = lambda:Broken()
    result=events(client.post(URL+'/stream',json={'action':'revise','instruction':'优化第四章','expected_version':1}))[-1]
    assert result['code']=='MODEL_FAILED' and 'private' not in str(result)
    result=events(client.post(URL+'/stream',json={'action':'revise','instruction':'把55分改成100分，未知全部改为0','expected_version':1}))[-1]
    assert result['code']=='FACT_PROTECTION'
    assert client.get(URL).json()==original

def test_only_frozen_saved_case_and_same_source_can_be_used(context):
    client, target, _ = context
    create(client)
    assert client.post('/api/v1/checkups/another/draft/stream',json={'action':'generate','expected_version':0}).status_code in (404,409)
    data=json.loads(target.read_text());data['result']['quality']['audit_changed']=True
    target.write_text(json.dumps(data))
    assert client.get(URL).status_code==409
    # Restore to preserve the read-only-source check in the fixture.
    target.write_bytes(context[2])

def test_updated_same_tongji_case_has_its_own_draft_without_replacing_old(context):
    client, target, _ = context
    original = create(client)
    updated_id = 'tongji-all7-small-updated-20261027'
    source = json.loads(target.read_text())
    source['id'] = updated_id
    source['result']['report_id'] = updated_id
    source['result']['quality']['coverage_acquisition_source_report_id'] = ID
    (target.parent/f'{updated_id}_report.json').write_text(json.dumps(source))
    updated_url = f'/api/v1/checkups/{updated_id}/draft'
    result = events(client.post(updated_url+'/stream', json={'action':'generate','expected_version':0}))[-1]['result']
    assert result['report_id'] == updated_id and result['version'] == 1
    assert result['facts']['report_id'] == updated_id
    assert client.get(URL).json() == original
    assert client.get(updated_url).json() == result
    assert client.post(updated_url+'/export', json={'expected_version':1}).status_code == 200

def test_requested_prose_replaces_actual_chapter_and_both_versions_export(context):
    client, _, _ = context
    first = create(client)
    text = ['建议将养老调研拆分为服务对象核查、运营访谈和入户需求调查。',
            '请业务处室形成设施开放时段、照护衔接、人员配置与无障碍入口的核查清单，并在复核后开展布局比选。']
    model = Model(); model.reply = {'sections':[{'id':'planning','paragraphs':text}]}
    app.dependency_overrides[checkups.get_report_model] = lambda:model
    result = events(client.post(URL+'/stream',json={'action':'revise','expected_version':1,'instruction':'请把第四章重写成养老专项调研任务清单，写具体一些'}))[-1]
    assert result['type'] == 'result', result
    revised = result['result']
    assert revised['chapters'][3]['paragraphs'] == text
    assert revised['chapters'][:3] == first['chapters'][:3]
    assert revised['chapters'][4:] == first['chapters'][4:]
    assert revised['facts'] == first['facts']
    from docx import Document
    for version, expected in [(1, first),(2,revised)]:
        response = client.post(URL+'/export',json={'expected_version':version})
        assert response.status_code == 200
        exported = [p.text for p in Document(io.BytesIO(response.content)).paragraphs]
        for paragraph in expected['chapters'][3]['paragraphs']: assert paragraph in exported
    assert client.get(URL).json() == revised

def test_noop_revision_does_not_claim_a_new_saved_version(context):
    client, _, _ = context; first = create(client)
    model=Model();model.reply={'sections':[{'id':'planning','paragraphs':first['chapters'][3]['paragraphs']}]}
    app.dependency_overrides[checkups.get_report_model] = lambda:model
    result=events(client.post(URL+'/stream',json={'action':'revise','instruction':'修改第四章','expected_version':1}))[-1]
    assert result['code'] == 'NO_CHANGES'
    assert client.get(URL).json() == first

@pytest.mark.parametrize('instruction',['把第三章去掉','帮我把第三张去掉'])
def test_delete_requested_chapter_from_saved_report_and_word(context,instruction):
    client, _, _ = context; first=create(client)
    result=events(client.post(URL+'/stream',json={'action':'revise','instruction':instruction,'expected_version':1}))[-1]
    assert result['type'] == 'result', result
    revised=result['result']
    assert [c['id'] for c in revised['chapters']] == ['scope','method','planning','limitations','appendix']
    assert revised['facts'] == first['facts']
    assert revised['chapters'][2] == first['chapters'][3]
    from docx import Document
    response=client.post(URL+'/export',json={'expected_version':2})
    paragraphs=[p.text for p in Document(io.BytesIO(response.content)).paragraphs]
    assert '第三章 设施现状与服务缺口' not in paragraphs
    assert '第四章 规划研判与后续调研建议' in paragraphs
    assert client.post(URL+'/export',json={'expected_version':1}).status_code == 200

def test_plain_language_explanation_can_change_without_changing_a_ratio(context):
    client, _, _=context;first=create(client)
    model=Model();model.reply={'sections':[{'id':'findings','paragraphs':['覆盖表示保存测距下可获得设施服务的区域；未知表示证据不足，需要复核。']}]}
    app.dependency_overrides[checkups.get_report_model]=lambda:model
    result=events(client.post(URL+'/stream',json={'action':'revise','instruction':'修改第三章关于覆盖率的解释，写得通俗一些','expected_version':1}))[-1]
    assert result['type']=='result',result
    assert result['result']['facts']==first['facts']

def test_restore_deleted_chapter_uses_last_saved_text_and_same_tables(context):
    client, _, _=context;first=create(client)
    model=Model();model.reply={'sections':[{'id':'findings','paragraphs':['请先核查开放时段，整理服务对象访谈线索。']}]}
    app.dependency_overrides[checkups.get_report_model]=lambda:model
    second=events(client.post(URL+'/stream',json={'action':'revise','instruction':'第三章写具体一些','expected_version':1}))[-1]['result']
    events(client.post(URL+'/stream',json={'action':'revise','instruction':'把第三章去掉','expected_version':2}))
    restored=events(client.post(URL+'/stream',json={'action':'revise','instruction':'恢复第三章','expected_version':3}))[-1]
    assert restored['type']=='result',restored
    assert restored['result']['chapters']==second['chapters']
    assert restored['result']['facts']==first['facts']

def test_page_edits_are_isolated_and_refresh_starts_with_complete_initial_report(context):
    from app.modules.checkup import report_drafts
    from app.schemas.report_draft import ReportDraft
    client,target,_=context
    initial=create(client)
    root=report_drafts.directory(target.parent)
    report_drafts.save_draft(root/'baselines',ReportDraft.model_validate(initial))
    # Simulate an old persisted edit: new pages must never inherit this deletion.
    archived=events(client.post(URL+'/stream',json={'action':'revise','instruction':'把第三章去掉','expected_version':1}))[-1]['result']
    page_a='2f03a4a1-1175-42d8-a85d-3446e0616df0'
    page_b='9ef163b9-9021-43d3-ae13-dd812b27d0d6'
    assert client.get(URL,params={'session_id':page_a}).json()==initial
    edited=events(client.post(URL+'/stream',json={'action':'revise','instruction':'把第三章去掉','expected_version':1,'session_id':page_a}))[-1]['result']
    assert 'findings' not in [c['id'] for c in edited['chapters']]
    assert client.get(URL,params={'session_id':page_a}).json()==edited
    assert client.get(URL,params={'session_id':page_b}).json()==initial
    assert client.get(URL).json()==archived
    from docx import Document
    for session,version,has_third in [(page_a,2,False),(page_a,1,True),(page_b,1,True)]:
        response=client.post(URL+'/export',json={'expected_version':version,'session_id':session})
        assert response.status_code==200
        text=[p.text for p in Document(io.BytesIO(response.content)).paragraphs]
        assert ('第三章 设施现状与服务缺口' in text)==has_third
        assert not any('草稿版本' in paragraph for paragraph in text)
    assert json.loads((root/'baselines'/f'{ID}.json').read_text())==initial

def test_new_page_without_a_saved_baseline_still_starts_with_all_chapters(context):
    client,_,_=context
    create(client)
    events(client.post(URL+'/stream',json={'action':'revise','instruction':'把第三章去掉','expected_version':1}))
    response=client.get(URL,params={'session_id':'e047b58a-4380-44fd-af5a-c23a71d0ead3'})
    assert response.status_code==200
    assert [c['id'] for c in response.json()['chapters']]==['scope','method','findings','planning','limitations','appendix']
    assert client.get(URL,params={'session_id':'../../escape'}).status_code==422
