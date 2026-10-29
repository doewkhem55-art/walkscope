/**
 * Work line 3 offline tests for the Gate 5 conversation contract.
 *
 * Everything here runs without a DOM, a network call or a model: the shared `ask` is a fake
 * and the report binding is a plain object, so the state machine, retry semantics and the
 * report-switch isolation can be checked deterministically.
 *
 * The last group asserts the *view contract* of ReportConversation.vue (host events, ARIA,
 * length guard) by parsing the SFC, so the component cannot silently drift from what the
 * host is told to wire up.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from 'vue/compiler-sfc'
import vm from 'node:vm'
import ts from 'typescript'
import * as Vue from 'vue'
import * as reportQuestions from '../src/reportQuestions.ts'
import * as reportDrafts from '../src/reportDrafts.ts'
import {
  MAX_QUESTION_LENGTH,
  buildQuestionHistory,
  classifyFailure,
  createReportConversation,
  evidenceLabel,
  normalizeQuestionMode,
  parseEvidenceId,
  retrievalSummary,
} from '../src/reportQuestions.ts'

const answer = (text, evidence = ['report'], mode = 'report_facts') => ({ answer: text, evidence_ids: evidence, mode })
const failure = (code, message = '失败') => Object.assign(new Error(message), { code })

/** Fake asker whose promises the test settles by hand. */
function controllableAsker() {
  const calls = []
  const pendingResolvers = []
  const asker = {
    calls,
    ask(id, question, signal) {
      calls.push({ id, question, aborted: signal.aborted })
      return new Promise((resolve, reject) => {
        pendingResolvers.push({ resolve, reject, id, question })
        signal.addEventListener('abort', () => {
          reject(failure('stopped', '已停止请求。'))
        }, { once: true })
      })
    },
  }
  return {
    asker,
    calls,
    settle(index, value) { pendingResolvers[index].resolve(value) },
    fail(index, error) { pendingResolvers[index].reject(error) },
    rejectAll() { pendingResolvers.forEach(item => item.reject(failure('stopped', '已停止请求。'))) },
    pendingCount: () => pendingResolvers.length,
  }
}

/** Immediate fake asker that records the optional 4th argument (history/focus/mode). */
function recordingAsker(answerFor) {
  const calls = []
  const asker = {
    calls,
    async ask(id, question, signal, options) {
      calls.push({ id, question, options, aborted: signal.aborted })
      return answerFor(question)
    },
  }
  return { asker, calls }
}

function session(asker, binding) {
  const states = []
  const conversation = createReportConversation({
    client: asker,
    binding: () => binding,
    update: state => states.push(state),
  })
  return { conversation, states, last: () => states.at(-1) }
}

// --- lifecycle and success ---------------------------------------------------

test('starts idle and bound to the current report', () => {
  const bound = { id: 'checkup-1', base: 'http://127.0.0.1:8000' }
  const { conversation } = session(controllableAsker().asker, bound)
  const state = conversation.read()
  assert.equal(state.phase, 'idle')
  assert.deepEqual(state.transcript, [])
  assert.equal(state.reportId, 'checkup-1')
  assert.equal(state.base, 'http://127.0.0.1:8000')
  assert.equal(state.canRetry, false)
})

test('a successful question lands in the transcript with its evidence and mode', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const asking = conversation.ask('  婴幼儿托育有几个？  ')
  assert.equal(conversation.read().phase, 'asking')
  assert.deepEqual(fake.calls[0], { id: 'c1', question: '婴幼儿托育有几个？', aborted: false })

  fake.settle(0, answer('圈内检索到 1 个。', ['facility:childcare_0_3'], 'model'))
  await asking
  const state = conversation.read()
  assert.equal(state.phase, 'answered')
  assert.equal(state.transcript.length, 1)
  assert.equal(state.transcript[0].question, '婴幼儿托育有几个？')
  assert.deepEqual(state.transcript[0].evidence_ids, ['facility:childcare_0_3'])
  assert.equal(state.transcript[0].mode, 'model')
  assert.equal(state.failure, null)
})

test('a second submit while asking is ignored, not queued', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const first = conversation.ask('第一个问题')
  const second = conversation.ask('第二个问题')
  assert.equal(fake.calls.length, 1, 'the duplicate must not reach the network')
  fake.settle(0, answer('答复'))
  await Promise.all([first, second])
  assert.equal(conversation.read().transcript.length, 1)
  assert.equal(conversation.read().transcript[0].question, '第一个问题')
})

test('transcript accumulates across questions on the same report', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const one = conversation.ask('问题一'); fake.settle(0, answer('答复一')); await one
  const two = conversation.ask('问题二'); fake.settle(1, answer('答复二')); await two
  assert.deepEqual(conversation.read().transcript.map(item => item.question), ['问题一', '问题二'])
})

// --- contract v1: bounded history, focus and mode -----------------------------

test('history keeps only the latest six rounds / twelve messages', () => {
  const transcript = Array.from({ length: 8 }, (_, index) => ({ question: `问题${index}`, answer: `答复${index}` }))
  const history = buildQuestionHistory(transcript)
  assert.equal(history.length, 12)
  assert.deepEqual(history[0], { role: 'user', content: '问题2' })
  assert.deepEqual(history[1], { role: 'assistant', content: '答复2' })
  assert.deepEqual(history.at(-1), { role: 'assistant', content: '答复7' })
  assert.deepEqual(buildQuestionHistory([]), [], 'no answered turns means no history')
})

test('history truncates one oversized message to the 4000-character limit', () => {
  const history = buildQuestionHistory([{ question: '短问题', answer: 'x'.repeat(5000) }])
  assert.equal(history.length, 2)
  assert.equal([...history[1].content].length, 4000)
  assert.ok(history[1].content.endsWith('…'), 'the cut must stay visible in the text')
})

test('history keeps the total length within 12000 characters and the newest turns', () => {
  const transcript = Array.from({ length: 6 }, (_, index) => ({ question: `问题${index}`, answer: 'y'.repeat(4000) }))
  const history = buildQuestionHistory(transcript)
  const total = history.reduce((sum, message) => sum + [...message.content].length, 0)
  assert.ok(total <= 12000, `bounded total, got ${total}`)
  assert.ok(total > 6000, 'the newest turns must survive the total cut')
  assert.equal(history.at(-1).role, 'assistant')
  assert.equal([...history.at(-1).content].length, 4000)
  assert.ok(history.some(message => message.content === '问题5'), 'the latest round is the last to be dropped')
})

test('ask sends bounded history, current focus and the selected mode', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = recordingAsker(question => answer(`答:${question}`, ['report'], 'model'))
  const conversation = createReportConversation({
    client: fake.asker,
    binding: () => bound,
    focus: () => ({ facility_id: 'primary_school', group_id: null, poi_id: null, selected_facility_ids: ['primary_school'] }),
    update: () => {},
  })

  await conversation.ask('问题一')
  await conversation.ask('问题二')
  assert.equal(fake.calls.length, 2)
  assert.equal(fake.calls[0].options.mode, 'auto', 'auto is the default mode')
  assert.equal(fake.calls[0].options.history, undefined, 'the first question has no history')
  assert.deepEqual(fake.calls[0].options.focus,
    { facility_id: 'primary_school', group_id: null, poi_id: null, selected_facility_ids: ['primary_school'] })
  assert.deepEqual(fake.calls[1].options.history, [
    { role: 'user', content: '问题一' },
    { role: 'assistant', content: '答:问题一' },
  ])
  assert.equal(fake.calls[1].options.mode, 'auto')
})

test('mode defaults to auto, switches per request, and survives a report reset', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = recordingAsker(() => answer('答复'))
  const conversation = createReportConversation({ client: fake.asker, binding: () => bound, update: () => {} })

  assert.equal(conversation.read().mode, 'auto')
  conversation.setMode('web')
  assert.equal(conversation.read().mode, 'web')
  await conversation.ask('问题一')
  assert.equal(fake.calls[0].options.mode, 'web')

  conversation.setMode('bogus')
  assert.equal(conversation.read().mode, 'web', 'an unknown mode is ignored rather than sent')

  bound.id = 'c2'
  conversation.reset()
  assert.equal(conversation.read().mode, 'web', 'the network preference is a UI choice, not report state')
  await conversation.ask('新报告的问题')
  assert.equal(fake.calls[1].options.mode, 'web')
  assert.equal(fake.calls[1].options.history, undefined, 'reset must clear the previous report history')
})

test('a host-provided mode is used and invalid defaults fall back to auto', () => {
  const bound = { id: 'c1', base: 'http://a' }
  const reportOnly = createReportConversation({ client: recordingAsker(() => answer('')).asker, binding: () => bound, mode: 'report' })
  assert.equal(reportOnly.read().mode, 'report')
  const invalid = createReportConversation({ client: recordingAsker(() => answer('')).asker, binding: () => bound, mode: 'sometimes' })
  assert.equal(invalid.read().mode, 'auto')
  assert.equal(normalizeQuestionMode('web'), 'web')
  assert.equal(normalizeQuestionMode(undefined), 'auto')
})

test('each answer keeps its truthful retrieval record in the transcript', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const retrieval = { requested: true, status: 'searched', query: '上海 小学', pages_read: 2, attempts: 3, elapsed_ms: 4200 }
  const fake = recordingAsker(() => ({
    ...answer('联网答复', ['facility:primary_school', 'web'], 'model'),
    web_search_used: true,
    sources: [{ title: '来源', url: 'https://example.invalid/a' }],
    retrieval,
    focus_applied: { facility_id: 'primary_school', group_id: null, poi_id: null },
    elapsed_ms: 12800,
  }))
  const conversation = createReportConversation({ client: fake.asker, binding: () => bound, update: () => {} })
  await conversation.ask('有什么最新消息？')

  const entry = conversation.read().transcript[0]
  assert.deepEqual(entry.retrieval, retrieval)
  assert.equal(entry.elapsed_ms, 12800)
  assert.equal(entry.web_search_used, true)
  assert.equal(entry.sources.length, 1)
})

// --- retrieval status display ------------------------------------------------

test('retrieval summaries distinguish live search, report-only, empty and failure', () => {
  const searched = retrievalSummary({
    retrieval: { requested: true, status: 'searched', query: '上海 小学', pages_read: 2 },
    web_search_used: true,
    sources: [{ title: 'a', url: 'https://a.invalid' }, { title: 'b', url: 'https://b.invalid' }],
  })
  assert.equal(searched.status, 'searched')
  assert.equal(searched.tone, 'web')
  assert.match(searched.label, /本次联网/)
  assert.match(searched.detail, /来源 2 条/)
  assert.match(searched.detail, /读取正文 2 页/)
  assert.match(searched.detail, /上海 小学/)

  const none = retrievalSummary({ retrieval: { requested: true, status: 'no_results' }, web_search_used: false, sources: [] })
  assert.equal(none.status, 'no_results')
  assert.equal(none.tone, 'empty')
  assert.match(none.label, /检索无结果/)

  const failed = retrievalSummary({ retrieval: { requested: true, status: 'failed', error: '余额不足' }, web_search_used: false, sources: [] })
  assert.equal(failed.tone, 'error')
  assert.match(failed.label, /检索失败/)
  assert.match(failed.detail, /余额不足/)

  const noReason = retrievalSummary({ retrieval: { requested: true, status: 'failed' }, web_search_used: false, sources: [] })
  assert.match(noReason.detail, /未提供可读原因/)

  const report = retrievalSummary({ retrieval: { requested: false, status: 'not_requested' }, web_search_used: false, sources: [] })
  assert.equal(report.status, 'not_requested')
  assert.equal(report.tone, 'report')
  assert.match(report.label, /仅报告事实/)
  assert.match(report.label, /未联网/)
})

test('an old backend without retrieval never claims a live search by accident', () => {
  const plain = retrievalSummary({ web_search_used: false, sources: [] })
  assert.equal(plain.status, 'unknown')
  assert.match(plain.label, /仅报告事实/)
  assert.match(plain.label, /未提供联网状态/)

  const web = retrievalSummary({ web_search_used: true, sources: [{ title: '来源', url: 'https://a.invalid' }] })
  assert.equal(web.status, 'unknown')
  assert.equal(web.tone, 'web')
  assert.match(web.label, /本次联网/)
  assert.match(web.label, /未提供检索状态/)
  assert.match(web.detail, /1 条来源/)
})

// --- failure semantics -------------------------------------------------------

test('error codes stay distinguishable instead of collapsing into one message', () => {
  assert.equal(classifyFailure(failure('429')).kind, 'busy')
  assert.equal(classifyFailure(failure('404')).kind, 'terminal')
  assert.equal(classifyFailure(failure('409')).kind, 'terminal')
  assert.equal(classifyFailure(failure('403')).kind, 'terminal')
  assert.equal(classifyFailure(failure('422')).kind, 'input')
  assert.equal(classifyFailure(failure('question')).kind, 'retryable')
  assert.equal(classifyFailure(failure('network')).kind, 'retryable')
  assert.equal(classifyFailure(failure('timeout')).kind, 'retryable')
  assert.equal(classifyFailure(failure('contract')).kind, 'terminal')
  assert.equal(classifyFailure(new Error('没有 code')).kind, 'retryable')
})

test('a 502 answers as retryable and offers to resend the same question', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const asking = conversation.ask('解释一下这份报告')
  fake.fail(0, failure('question', '报告问答暂不可用，可在同一份报告上重试问题；无需重新体检。'))
  await asking
  const state = conversation.read()
  assert.equal(state.phase, 'failed')
  assert.equal(state.failure.kind, 'retryable')
  assert.equal(state.canRetry, true)
  assert.match(state.failure.message, /无需重新体检/)
})

test('retry resends exactly the previous question on the same report', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const first = conversation.ask('托育情况如何？')
  fake.fail(0, failure('question'))
  await first

  const second = conversation.retry()
  assert.equal(fake.calls.length, 2)
  assert.equal(fake.calls[1].question, '托育情况如何？', 'retry must resend the same text')
  assert.equal(fake.calls[1].id, 'c1', 'retry must stay on the same checkup id')
  fake.settle(1, answer('恢复了'))
  await second
  assert.equal(conversation.read().phase, 'answered')
  assert.equal(conversation.read().transcript.length, 1)
})

test('a non-retryable failure offers no retry', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const asking = conversation.ask('还在吗？')
  fake.fail(0, failure('404', '任务不存在或已过期。'))
  await asking
  assert.equal(conversation.read().canRetry, false)
  await conversation.retry()
  assert.equal(fake.calls.length, 1, 'retry must not fire when it cannot help')
})

test('dismissing a failure keeps the already-answered transcript', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const ok = conversation.ask('问题一'); fake.settle(0, answer('答复一')); await ok
  const bad = conversation.ask('问题二'); fake.fail(1, failure('question')); await bad
  assert.equal(conversation.read().phase, 'failed')

  conversation.acknowledgeFailure()
  const state = conversation.read()
  assert.equal(state.phase, 'answered')
  assert.equal(state.failure, null)
  assert.equal(state.transcript.length, 1)
})

test('empty and over-long questions never reach the network', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  await conversation.ask('   ')
  assert.equal(conversation.read().failure.kind, 'input')
  await conversation.ask('x'.repeat(MAX_QUESTION_LENGTH + 1))
  assert.equal(conversation.read().failure.kind, 'input')
  assert.equal(fake.calls.length, 0)
})

// --- report switching isolation (Gate 5 lifecycle) ---------------------------

test('a late answer for a report the user left is discarded', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const asking = conversation.ask('旧报告的问题')
  // A host that switches report does both: rebinds and resets.
  bound.id = 'c2'
  conversation.reset()
  fake.settle(0, answer('旧报告的答复'))
  await asking

  assert.deepEqual(conversation.read().transcript, [], 'no stale answer may appear')
  assert.equal(conversation.read().reportId, 'c2')
})

test('reset clears transcript, failure and retry state on report switch', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const ok = conversation.ask('问题一'); fake.settle(0, answer('答复一')); await ok
  const bad = conversation.ask('问题二'); fake.fail(1, failure('question')); await bad
  assert.equal(conversation.read().transcript.length, 1)
  assert.equal(conversation.read().canRetry, true)

  bound.id = 'c2'
  conversation.reset()
  const state = conversation.read()
  assert.equal(state.phase, 'idle')
  assert.deepEqual(state.transcript, [])
  assert.equal(state.failure, null)
  assert.equal(state.canRetry, false)
  assert.equal(state.reportId, 'c2')
})

test('switching the backend also discards the previous report state', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const ok = conversation.ask('问题一'); fake.settle(0, answer('答复一')); await ok
  bound.base = 'http://b'
  conversation.reset()
  assert.equal(conversation.read().base, 'http://b')
  assert.deepEqual(conversation.read().transcript, [])
})

test('a stale in-flight request cannot append after reset', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, bound)

  const asking = conversation.ask('会被丢弃的问题')
  conversation.reset()
  fake.settle(0, answer('迟到答复'))
  await asking
  assert.deepEqual(conversation.read().transcript, [])
})

test('the conversation never submits a checkup of its own', async () => {
  const bound = { id: 'c1', base: 'http://a' }
  const calls = []
  const asker = {
    ask: async (id, question) => { calls.push({ id, question }); return answer('答复') },
  }
  const { conversation } = session(asker, bound)
  await conversation.ask('问题')
  await conversation.retry()
  assert.equal(calls.length, 1, 'only ask() may be used; there is no submit path here')
})

// --- evidence references -----------------------------------------------------

test('evidence families from contract v1 are resolved and raw keys are preserved', () => {
  assert.deepEqual(parseEvidenceId('report'), { kind: 'report', key: 'report' })
  assert.deepEqual(parseEvidenceId('group:education_children'), { kind: 'group', key: 'group:education_children', id: 'education_children' })
  assert.deepEqual(parseEvidenceId('facility:primary_school'), { kind: 'facility', key: 'facility:primary_school', id: 'primary_school' })
  assert.deepEqual(parseEvidenceId('poi:poi-123'), { kind: 'poi', key: 'poi:poi-123', id: 'poi-123' })
  assert.deepEqual(parseEvidenceId('route:target-1'), { kind: 'route', key: 'route:target-1', id: 'target-1' })
  assert.deepEqual(parseEvidenceId('web'), { kind: 'web', key: 'web' })
  assert.equal(parseEvidenceId('poi:').kind, 'unsupported', 'a prefix without an id cannot be resolved')
  assert.equal(parseEvidenceId('route:   ').kind, 'unsupported')
  assert.equal(parseEvidenceId('随便什么').kind, 'unsupported')
})

test('evidence labels fall back to the raw key so nothing is hidden', () => {
  const names = {
    groups: { education_children: '教育与儿童' }, facilities: { primary_school: '小学' },
    pois: { 'poi-123': '同济小学' }, routes: { 'target-1': '中心步行路线' },
  }
  assert.equal(evidenceLabel('report', names), '本次报告')
  assert.equal(evidenceLabel('group:education_children', names), '教育与儿童（组）')
  assert.equal(evidenceLabel('facility:primary_school', names), '小学（类别）')
  assert.equal(evidenceLabel('facility:unknown_id', names), 'unknown_id（类别）')
  assert.equal(evidenceLabel('poi:poi-123', names), '同济小学（点位）')
  assert.equal(evidenceLabel('poi:unknown_poi', names), 'unknown_poi（点位）')
  assert.equal(evidenceLabel('route:target-1', names), '中心步行路线（路线）')
  assert.equal(evidenceLabel('route:unknown_target', names), 'unknown_target（路线）')
  assert.equal(evidenceLabel('web', names), '本次联网来源')
  assert.equal(evidenceLabel('随便什么', names), '随便什么')
})

// --- view contract (ReportConversation.vue) ----------------------------------

const componentSource = readFileSync(new URL('../src/ReportConversation.vue', import.meta.url), 'utf8')
const { descriptor } = parse(componentSource)
const template = descriptor.template?.content ?? ''

test('the view is a separate component with scoped styles only', () => {
  assert.ok(componentSource.includes('<style scoped>'), 'must not leak styles into the host')
  assert.ok(!componentSource.includes('import BaiduRoadMap'), 'must not touch the map')
  assert.ok(!componentSource.includes('/api/v1/checkups'), 'must not build its own endpoints')
  assert.ok(!/\.submit\(|checkupClient\(/.test(componentSource), 'must not submit a checkup')
})

test('the view asks the host to locate evidence instead of manipulating it', () => {
  assert.ok(componentSource.includes("emit('locate'"), 'evidence clicks must emit locate')
  assert.ok(componentSource.includes("(event: 'locate', ref: EvidenceRef)"), 'the event must carry a typed ref')
  assert.ok(componentSource.includes('isLocatable'), 'only families the host can reveal may be clickable')
  assert.ok(componentSource.includes("ref.kind === 'poi'"), 'poi evidence must be parsed for display')
  assert.ok(componentSource.includes("ref.kind === 'route'"), 'route evidence must be parsed for display')
  assert.ok(componentSource.includes('无法解析的证据键'), 'unparsed keys must be shown verbatim')
})

test('the view displays the truthful retrieval status of every answer', () => {
  assert.ok(componentSource.includes('检索状态'), 'the status must have a visible label')
  assert.ok(componentSource.includes('data-testid="retrieval-status"'), 'the status needs a test hook')
  assert.ok(componentSource.includes('retrievalFor(item)'), 'the status must come from the shared summary')
  assert.ok(componentSource.includes('retrievalSummary'), 'display logic must be unit-testable, not inline')
  assert.ok(componentSource.includes('item.sources.length'), 'the source list must still be rendered')
})

test('the view always asks for web retrieval without a mode selector', () => {
  assert.ok(!template.includes('mode-picker'))
  assert.ok(!template.includes('选择联网方式'))
  assert.ok(componentSource.includes("{ ...options, mode: 'web' }"), 'all outgoing requests must lock web mode')
  assert.ok(!componentSource.includes('mode: props.defaultMode'), 'legacy host defaults cannot override web mode')
})

test('the chat uses a scrollable transcript, collapsed evidence and a bottom composer', () => {
  assert.ok(template.includes('role="log"'))
  assert.ok(template.includes('class="chat-message user-message"'))
  assert.ok(template.includes('class="chat-message assistant-message"'))
  assert.ok(template.indexOf('class="message-list"') < template.indexOf('class="ask-form"'))
  assert.ok(template.includes('<details class="answer-details">'))
  assert.ok(!template.includes('<details open'))
  assert.ok(template.includes('Shift + Enter 换行'))
})

test('the template renders poi/route/web evidence chips without hiding raw keys', () => {
  assert.ok(template.includes(':title="evidenceTitle(id)"'), 'unlocatable chips must explain themselves')
  assert.ok(template.includes('isLocatable(id)'), 'locatable and display-only chips must differ')
  assert.ok(template.includes('label(id)'), 'resolved labels must be shown')
})

test('the view exposes loading, failure, retry and mode states accessibly', () => {
  assert.ok(componentSource.includes(':aria-busy="asking"'), 'loading must be announced')
  assert.ok(componentSource.includes('role="alert"'), 'failure must be announced')
  assert.ok(componentSource.includes('正在连接报告问答服务'), 'loading text must be visible')
  assert.ok(componentSource.includes('state.canRetry'), 'retry must be offered only when retryable')
  assert.ok(componentSource.includes("mode === 'report_facts'"), 'fact mode must be distinguishable from model mode')
  assert.ok(componentSource.includes(':maxlength="MAX_QUESTION_LENGTH"'), 'the length limit must be enforced in the view')
  assert.ok(componentSource.includes('{{ length }} / {{ MAX_QUESTION_LENGTH }}'), 'the user must see the length budget')
  assert.ok(componentSource.includes(':disabled="!canAsk"'), 'submit must be disabled while asking or invalid')
})

test('the view resets when the bound report changes', () => {
  assert.ok(/watch\(\(\) => `\$\{props\.binding\.base\}#\$\{props\.binding\.id\}`/.test(componentSource),
    'the binding watch must cover both base and id')
  assert.ok(componentSource.includes('session.reset()'), 'a binding change must reset the conversation')
})

test('the template renders the transcript with its evidence chips', () => {
  assert.ok(template.includes('v-for="(item, index) in transcript"'), 'the transcript must be rendered')
  assert.ok(template.includes('item.evidence_ids'), 'evidence ids must be shown')
  assert.ok(template.includes('@click="locate(id)"'), 'resolvable evidence must be clickable')
  assert.ok(template.includes('服务未返回证据编号'), 'an empty evidence list must be stated, not omitted')
})


// Compile and exercise the real chat component without a browser or paid API calls.
const chatCode = ts.transpileModule(compileScript(descriptor, { id: 'report-chat-test', inlineTemplate: true }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const chatExports = {}
vm.runInNewContext(chatCode, {
  exports: chatExports,
  // The renderer has no DOM; invoke v-model's generated update handler directly below.
  require: name => name === 'vue' ? { ...Vue, vModelText: {} } : name === './reportDrafts.ts' ? reportDrafts : reportQuestions,
  AbortController, console,
})
function mountChat(client, props = {}) {
  const parents = new WeakMap()
  const element = type => ({ type, children: [], props: {}, text: '', style: {}, focus() { this.focused = true }, getRootNode() { return { activeElement: null } }, addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {} })
  const renderer = Vue.createRenderer({
    createElement: element,
    createText: text => ({ ...element('#text'), text }),
    createComment: text => ({ ...element('#comment'), text }),
    insert(child, parent, anchor) {
      const old = parents.get(child)
      if (old) old.children.splice(old.children.indexOf(child), 1)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(child)
      else parent.children.splice(index, 0, child)
      parents.set(child, parent)
    },
    remove(child) { const parent = parents.get(child); if (parent) parent.children.splice(parent.children.indexOf(child), 1) },
    setText: (node, text) => { node.text = text },
    setElementText: (node, text) => { node.text = text; node.children.length = 0 },
    patchProp: (node, key, previous, next) => { node.props[key] = next },
    parentNode: node => parents.get(node) ?? null,
    nextSibling: node => { const parent = parents.get(node); return parent?.children[parent.children.indexOf(node) + 1] ?? null },
  })
  const root = element('root')
  const app = renderer.createApp(chatExports.default, { binding: { id: 'chat-report', base: '/local' }, client, ...props })
  const instance = app.mount(root)
  function all(node = root) { return [node, ...node.children.flatMap(child => all(child))] }
  return { app, instance, all, find: predicate => all().find(predicate) }
}
const flushChat = async () => { await Vue.nextTick(); await Vue.nextTick(); await Vue.nextTick() }
const keypress = (extra = {}) => ({ key: 'Enter', isComposing: false, shiftKey: false, preventDefault() {}, ...extra })

test('real chat sends Enter with web mode, preserves IME and Shift-Enter, and forwards follow-up context', async () => {
  const fake = recordingAsker(question => ({ ...answer(`答:${question}`, ['report'], 'model'), retrieval: { requested: true, status: 'searched', pages_read: 1 }, sources: [{ title: '公开资料', url: 'https://example.invalid/source' }] }))
  const chat = mountChat(fake.asker, { defaultMode: 'report' })
  try {
    let input = chat.find(node => node.type === 'textarea')
    input.props['onUpdate:modelValue']('第一个问题')
    await flushChat()
    input.props.onKeydown(keypress({ isComposing: true }))
    input.props.onKeydown(keypress({ shiftKey: true }))
    assert.equal(fake.calls.length, 0, 'IME selection and Shift-Enter must not send')
    input.props.onKeydown(keypress())
    await flushChat()
    assert.equal(fake.calls[0].options.mode, 'web')
    assert.equal(fake.calls[0].options.history, undefined)
    assert.ok(chat.find(node => node.props.class?.includes('user-message')))
    assert.ok(chat.find(node => node.props.class?.includes('assistant-message')))
    assert.ok(chat.find(node => node.type === 'details' && !node.props.open))
    assert.ok(chat.find(node => node.props.href === 'https://example.invalid/source'))

    input = chat.find(node => node.type === 'textarea')
    input.props['onUpdate:modelValue']('继续问')
    await flushChat()
    input.props.onKeydown(keypress())
    await flushChat()
    assert.equal(fake.calls[1].options.mode, 'web')
    assert.deepEqual(fake.calls[1].options.history, [{ role: 'user', content: '第一个问题' }, { role: 'assistant', content: '答:第一个问题' }])
  } finally { chat.app.unmount() }
})

test('real chat locks web on retry and exposes actual search failure rather than claiming success', async () => {
  let attempts = 0
  const calls = []
  const chat = mountChat({ async ask(id, question, signal, options) {
    calls.push(options)
    if (++attempts === 1) throw failure('network', '暂时连接失败')
    return { ...answer('本次依据报告回答', ['report'], 'model'), retrieval: { requested: true, status: 'failed', error: '搜索服务余额不足' } }
  } }, { defaultMode: 'auto' })
  try {
    const input = chat.find(node => node.type === 'textarea')
    input.props['onUpdate:modelValue']('看看这里的设施')
    await flushChat()
    input.props.onKeydown(keypress())
    await flushChat()
    assert.ok(chat.find(node => node.props.role === 'alert'))
    const retryButton = chat.find(node => node.type === 'button' && node.text === '重试这个问题')
    assert.ok(retryButton)
    await retryButton.props.onClick()
    await flushChat()
    assert.deepEqual(calls.map(options => options.mode), ['web', 'web'])
    assert.equal(chat.instance.state.transcript[0].retrieval.error, '搜索服务余额不足')
    const status = chat.find(node => node.props['data-testid'] === 'retrieval-status')
    assert.equal(status.props['data-status'], 'failed')
    assert.match(status.children.map(node => node.text).join(''), /检索失败/)
  } finally { chat.app.unmount() }
})

test('live stages and provisional answer reset immediately, with late report events isolated', async () => {
  let onEvent, finish
  const binding = { id: 'a', base: 'http://local' }
  const { conversation } = session({ ask: (_id, _q, _signal, options) => {
    onEvent = options.onEvent
    return new Promise(resolve => { finish = resolve })
  } }, binding)
  const pending = conversation.ask('说明')
  assert.equal(typeof onEvent, 'function')
  onEvent({ type: 'stage', stage: 'report', message: '读取报告' })
  onEvent({ type: 'draft', text: '待校验草稿' })
  assert.equal(conversation.read().draftAnswer, '待校验草稿')
  assert.equal(conversation.read().progress[0].message, '读取报告')
  assert.equal(conversation.read().transcript.length, 0)
  onEvent({ type: 'draft_reset' })
  assert.equal(conversation.read().draftAnswer, '')
  binding.id = 'b'
  conversation.reset()
  onEvent({ type: 'draft', text: '迟到草稿' })
  finish(answer('迟到结果'))
  await pending
  assert.equal(conversation.read().draftAnswer, '')
  assert.equal(conversation.read().transcript.length, 0)
})

test('an invalid second submit cannot unlock a question already in flight', async () => {
  const fake = controllableAsker()
  const { conversation } = session(fake.asker, { id: 'report', base: 'http://local' })
  const pending = conversation.ask('首个问题')
  await conversation.ask(' ')
  assert.equal(conversation.read().phase, 'asking')
  await conversation.ask('第三个问题')
  assert.equal(fake.calls.length, 1)
  fake.settle(0, answer('已完成'))
  await pending
})

test('completed transcript retains actual stages while final validated answer replaces draft', async () => {
  const { conversation } = session({ async ask(_id, _question, _signal, options) {
    options.onEvent({ type: 'stage', stage: 'evidence_summary', message: '读取公开正文 2 页' })
    options.onEvent({ type: 'draft', text: '未校验草稿' })
    return answer('已校验答案')
  } }, { id: 'report', base: 'http://local' })
  await conversation.ask('说明')
  const entry = conversation.read().transcript[0]
  assert.equal(entry.answer, '已校验答案')
  assert.equal(entry.progress[0].message, '读取公开正文 2 页')
  assert.equal(conversation.read().draftAnswer, '')
  assert.equal(JSON.stringify(entry).includes('未校验草稿'), false)
})


test('one conversation orders real report revisions and ordinary questions without mode buttons', async () => {
  let version=0
  let saved=null
  const fake=recordingAsker(question=>answer(`普通问答:${question}`))
  const draftClient={
    get:async()=>saved,
    async edit(id,body,signal,onEvent){
      assert.equal(body.expected_version,version)
      onEvent({type:'stage',stage:'source',message:'读取保存依据'})
      onEvent({type:'chapter',chapter:{id:'planning',title:'第四章 规划研判',paragraphs:['待保存文字'],table_headers:[],table_rows:[]}})
      version++
      saved = {report_id:id,source_hash:'a'.repeat(64),version,title:'同济规划研判报告',mode:'model',notice:'事实只读',updated_at:'today',facts:{score:55},chapters:[{id:'planning',title:'第四章 规划研判',paragraphs:[body.instruction??'初始报告'],table_headers:[],table_rows:[]}]}
      return saved
    },
    export:async()=>new Blob(['word']),
  }
  const chat=mountChat(fake.asker,{draftClient})
  try {
    assert.equal(typeof chat.instance.startReportDraft,'function')
    await chat.instance.startReportDraft();await flushChat()
    assert.ok(chat.find(n=>n.type==='h3'&&n.text==='同济规划研判报告'))
    assert.equal(chat.find(n=>n.props.role==='log').focused,true,'opening the report must raise/focus the original floating window')
    assert.equal(chat.find(n=>n.props.role==='log').scrollTop,0,'the title and current version must be visible after loading')
    for(const text of ['第四章更聚焦养老设施','把结论改成内部汇报口吻']) {
      const input=chat.find(n=>n.type==='textarea')
      input.props['onUpdate:modelValue'](text);await flushChat();await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    }
    assert.equal(chat.instance.draftState.current.version,3)
    assert.equal(chat.instance.draftState.current.chapters[0].paragraphs[0],'把结论改成内部汇报口吻')
    assert.equal(fake.calls.length,0)
    assert.ok(!chat.all().some(n=>/版本\s*\d|第\s*\d+\s*版/.test(n.text)),'internal revision numbers must not appear in the conversation')
    assert.equal(chat.find(n=>n.type==='nav'&&n.props['aria-label']==='报告工作方式'),undefined)
    const exportButtons=chat.all().filter(n=>n.type==='button'&&n.text==='导出 Word')
    assert.equal(exportButtons.length,3,'each saved report has an export below it')
    const input=chat.find(n=>n.type==='textarea');input.props['onUpdate:modelValue']('这个分数怎么理解');await flushChat();await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    assert.equal(fake.calls.length,1)
    assert.ok(fake.calls[0].options.history.some(turn=>turn.content.includes('把结论改成内部汇报口吻')),'questions must receive the currently edited report text')
    const questionInput=chat.find(n=>n.type==='textarea');questionInput.props['onUpdate:modelValue']('刚才删掉了哪一章？');await flushChat();await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    assert.equal(fake.calls[1].options.mode,'report','draft-internal questions must not wait for external search')
    const latestInput=chat.find(n=>n.type==='textarea');latestInput.props['onUpdate:modelValue']('请修改这个word，把第四章写详细一点');await flushChat();await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    assert.equal(chat.instance.draftState.current.version,4)
    const userMessages=chat.all().filter(n=>n.props.class?.includes('user-bubble')).map(n=>n.text)
    assert.deepEqual(userMessages,['第四章更聚焦养老设施','把结论改成内部汇报口吻','这个分数怎么理解','刚才删掉了哪一章？','请修改这个word，把第四章写详细一点'])
  } finally {chat.app.unmount()}
})

test('retry reconciles a report saved before the result connection broke without repeating the edit',async()=>{
  const make=version=>({report_id:'chat-report',source_hash:'a'.repeat(64),version,title:'同济报告',mode:'model',notice:'',updated_at:'today',facts:{score:55},chapters:[{id:'planning',title:'第四章 规划研判',paragraphs:[version===7?'原文':'已修改的正文'],table_headers:[],table_rows:[]}]})
  let saved=make(7),edits=0
  const chat=mountChat(recordingAsker(q=>answer(q)).asker,{draftClient:{get:async()=>saved,edit:async()=>{edits++;saved=make(8);throw new Error('连接中断，请确认已保存版本')},export:async()=>new Blob(['word'])}})
  try {
    await chat.instance.startReportDraft();await flushChat()
    const input=chat.find(n=>n.type==='textarea');input.props['onUpdate:modelValue']('第四章写具体一些');await flushChat()
    await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    assert.equal(chat.instance.draftState.current.version,7)
    await chat.find(n=>n.type==='button'&&n.text==='重试').props.onClick();await flushChat()
    assert.equal(chat.instance.draftState.current.version,8)
    assert.equal(chat.instance.draftState.error,'')
    assert.equal(edits,1,'a saved edit must not run again after a lost result')
  } finally {chat.app.unmount()}
})

test('a failed pre-edit read can retry the actual pending instruction without losing it',async()=>{
  const make=version=>({report_id:'chat-report',source_hash:'a'.repeat(64),version,title:'同济报告',mode:'model',notice:'',updated_at:'today',facts:{score:55},chapters:[{id:'planning',title:'第四章 规划研判',paragraphs:['正文'],table_headers:[],table_rows:[]}]})
  let gets=0;const edits=[]
  const chat=mountChat(recordingAsker(q=>answer(q)).asker,{draftClient:{get:async()=>{if(++gets===2)throw new Error('临时读取失败');return make(7)},edit:async(id,body)=>{edits.push(body);return make(8)},export:async()=>new Blob(['word'])}})
  try {
    await chat.instance.startReportDraft();await flushChat()
    const input=chat.find(n=>n.type==='textarea');input.props['onUpdate:modelValue']('第四章写具体一些');await flushChat()
    await chat.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await flushChat()
    assert.equal(edits.length,0)
    await chat.find(n=>n.type==='button'&&n.text==='重试').props.onClick();await flushChat()
    assert.equal(edits[0]?.instruction,'第四章写具体一些')
    assert.equal(chat.instance.draftState.current.version,8)
  } finally {chat.app.unmount()}
})
