"""Offline progress, provenance and provider streaming regressions."""
import json

import httpx

from app.modules.checkup.questions import answer_report
from app.providers.deepseek import DeepSeekLLM
from tests.report_fixtures import build_report


def test_progress_precedes_model_work_and_invalid_draft_is_reset():
    events = []

    class Model:
        def stream_object(self, *, on_answer, **kwargs):
            assert events[-1]['stage'] == 'answer'
            on_answer('没有依据的草稿')
            return {'answer': '没有依据的草稿', 'mode': 'model', 'evidence_ids': ['invented']}

        def generate_object(self, **kwargs):
            assert events[-1]['type'] == 'draft_reset'
            return {'answer': '仍没有依据', 'mode': 'model', 'evidence_ids': ['invented']}

    result = answer_report(build_report('stream'), '说明报告', Model(), mode='report', on_event=events.append)
    assert result.mode == 'report_facts'
    assert '没有依据的草稿' not in result.answer
    assert [event['stage'] for event in events if event['type'] == 'stage'] == [
        'report', 'evidence', 'plan', 'retrieval', 'evidence_summary', 'answer', 'validation', 'correction',
    ]
    assert {'type': 'draft', 'text': '没有依据的草稿'} in events
    assert events[-1] == {'type': 'draft_reset'}


def test_no_model_progress_truthfully_reports_facts_only():
    events = []
    result = answer_report(build_report('stream'), '报告如何', mode='report', on_event=events.append)
    assert result.mode == 'report_facts'
    assert '未配置' in result.answer
    assert any(event.get('stage') == 'evidence_summary' for event in events)
    assert not any(event['type'] == 'draft' for event in events)


def test_deepseek_stream_exposes_only_public_answer_and_handles_split_json_escapes():
    content = json.dumps({'answer': '你好\n"建议"😀', 'mode': 'model', 'evidence_ids': ['report']}, ensure_ascii=True)
    chunks = [{'choices': [{'delta': {'reasoning_content': '不可外传的内部推理'}}]}]
    chunks.extend({'choices': [{'delta': {'content': char}}]} for char in content)
    chunks.append({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
    def handler(request):
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200, text=''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n')
    model = DeepSeekLLM('offline-only', client=httpx.Client(transport=httpx.MockTransport(handler)))
    drafts = []
    result = model.stream_object(system_prompt='答复', user_message='说明报告', response_schema={}, on_answer=drafts.append)
    assert result['answer'] == '你好\n"建议"😀'
    assert drafts[-1] == result['answer']
    assert len(drafts) > 2
    assert all('不可外传' not in draft for draft in drafts)


def test_stream_failure_resets_partial_answer_before_bounded_retry():
    events = []
    class Model:
        def stream_object(self, *, on_answer, **kwargs):
            on_answer('不完整')
            raise RuntimeError('offline interrupted')
        def generate_object(self, **kwargs):
            assert events[-1] == {'type': 'draft_reset'}
            return {'answer': '完整且有依据', 'mode': 'model', 'evidence_ids': ['report']}
    result = answer_report(build_report('stream'), '说明', Model(), mode='report', on_event=events.append)
    assert result.answer == '完整且有依据'
    assert result.evidence_ids == ['report']


def test_partial_answer_handles_nonfirst_top_level_field_without_leaking_nested_answer():
    from app.providers.deepseek import _partial_answer
    assert _partial_answer('{"metadata":{"answer":"内部字段"},"answer":"公开答复') == '公开答复'
    assert _partial_answer('{"metadata":{"answer":"内部字段') == ''
