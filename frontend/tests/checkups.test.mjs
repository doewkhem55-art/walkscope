import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { CheckupApiError, checkupClient, parseCheckup, parseCheckupCatalog, savePending, loadPending, watchCheckup } from '../src/checkups.ts'

const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const snapshot = JSON.parse(readFileSync(new URL('../src/fixtures/facility-catalog.example.json', import.meta.url), 'utf8'))
const catalog = { version: 'offline-test-catalog', groups: snapshot.groups, facilities: snapshot.facilities,
  runtime: { map_service_configured: false, report_questions_mode: 'report_facts' } }
const openapi = { paths: {
  '/api/v1/checkup-catalog': { get: {} },
  '/api/v1/checkups': { post: {} },
  '/api/v1/checkups/{checkup_id}': { get: {} },
  '/api/v1/checkups/{checkup_id}/questions': { post: {} },
} }
const report = {
  report_id: 'offline-job', created_at: '2026-10-13T00:00:00Z', center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' },
  location_label: '离线测试样本', catalog_version: catalog.version, isochrone: { geometry: null, quality: 'offline-test' },
  search_radius_m: 1500, categories: catalog.facilities.map(f => ({ facility_id: f.id, group_id: f.group_id, name_zh: f.name_zh })),
  groups: catalog.groups.map(group => ({ ...group, score: 72.5 })),
  score: 72.5, score_method: 'offline-test', summary: '离线假数据，不是真实体检。', pois: [], routes: [],
  blindspot: {}, warnings: [], quality: { note: 'synthetic test only' },
}
const signal = () => new AbortController().signal

test('offline catalog fixture is explicitly transformed for the new 45 / 7 contract', () => {
  const parsed = parseCheckupCatalog(catalog)
  assert.equal(parsed.facilities.length, 45)
  assert.equal(parsed.groups.length, 7)
  assert.equal(parsed.runtime.map_service_configured, false)
  assert.equal(parsed.runtime.report_questions_mode, 'report_facts')
})

test('three-category responses fail clearly and are never accepted as a new catalog', () => {
  assert.throws(() => parseCheckupCatalog({ version: 'old', groups: catalog.groups.slice(0, 3), facilities: catalog.facilities.slice(0, 3) }),
    error => error instanceof CheckupApiError && error.code === 'catalog' && error.message.includes('45类、7组'))
})

test('fake fetch checks OpenAPI and then reads the live catalog endpoint', async () => {
  const calls = []
  const client = checkupClient('http://127.0.0.1:8000', async url => {
    calls.push(url.pathname)
    return response(url.pathname === '/openapi.json' ? openapi : catalog)
  }, 1000, 0)
  assert.equal((await client.catalog(signal())).version, catalog.version)
  assert.deepEqual(calls, ['/openapi.json', '/api/v1/checkup-catalog'])
})

test('fake submit accepts only 202 and sends the selected location', async () => {
  let sentBody
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    sentBody = JSON.parse(init.body)
    return response({ id: 'offline-job', status: 'queued', stage: 'queued', progress: 0 }, 202)
  }, 1000, 0)
  const accepted = await client.submit({ type: 'address', query: '离线测试地址', city: '上海市' }, signal())
  assert.equal(accepted.id, 'offline-job')
  assert.deepEqual(sentBody, { location: { type: 'address', query: '离线测试地址', city: '上海市' } })
})

test('facility selection is optional, validated, and sent only when provided', async () => {
  const bodies = []
  let posts = 0
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    posts++
    bodies.push(JSON.parse(init.body))
    return response({ id: 'offline-job', status: 'queued', stage: 'queued', progress: 0 }, 202)
  }, 1000, 0)
  const location = { type: 'address', query: '离线测试地址' }
  await client.submit(location, signal())
  await client.submit(location, signal(), ['facility-a', 'facility-b'])
  assert.deepEqual(bodies, [
    { location },
    { location, facility_ids: ['facility-a', 'facility-b'] },
  ])
  await assert.rejects(client.submit(location, signal(), []), error =>
    error instanceof CheckupApiError && error.code === 'input' && error.message.includes('至少选择一个'))
  await assert.rejects(client.submit(location, signal(), ['facility-a', 'facility-a']), error =>
    error instanceof CheckupApiError && error.code === 'input' && error.message.includes('不能重复'))
  assert.equal(posts, 2, 'invalid selections must not reach the network')
})

test('reports may cover a selected subset, and selected ids must match the report', () => {
  const subset = structuredClone(report)
  subset.categories = subset.categories.slice(0, 2)
  const groupIds = new Set(subset.categories.map(category => category.group_id))
  subset.groups = subset.groups.filter(group => groupIds.has(group.id)).map(group => ({ ...group, score: null }))
  subset.selected_facility_ids = subset.categories.map(category => category.facility_id)
  subset.score = null
  const job = { id: 'offline-job', status: 'completed', stage: 'completed', progress: 100, result: subset, error: null }
  const parsed = parseCheckup(job)
  assert.equal(parsed.result.categories.length, 2)
  assert.deepEqual(parsed.result.selected_facility_ids, subset.selected_facility_ids)

  const full = structuredClone(job)
  full.result = structuredClone(report)
  full.result.selected_facility_ids = full.result.categories.map(category => category.facility_id)
  assert.equal(parseCheckup(full).result.selected_facility_ids.length, 45)

  for (const corrupt of [
    value => { value.result.selected_facility_ids = [value.result.categories[0].facility_id] },
    value => { value.result.categories = [] },
    value => { value.result.groups = [] },
  ]) {
    const broken = structuredClone(job)
    corrupt(broken)
    assert.throws(() => parseCheckup(broken), error => error.code === 'contract')
  }
})

test('fake polling and report questions retain status and evidence references', async () => {
  const calls = []
  const client = checkupClient('http://127.0.0.1:8000', async (url, init) => {
    calls.push({ path: url.pathname, method: init.method })
    if (init.method === 'POST') return response({ answer: '只基于离线样本。', evidence_ids: ['evidence:test'], mode: 'report_facts' })
    return response({ id: 'offline-job', status: 'completed', stage: 'completed', progress: 100, result: report, error: null })
  }, 1000, 0)
  const job = await client.poll({ id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'routes', progress: 60 }, signal())
  assert.equal(job.result.score, 72.5)
  const answer = await client.ask(job.id, '只问离线样本', signal())
  assert.deepEqual(answer, { answer: '只基于离线样本。', evidence_ids: ['evidence:test'], mode: 'report_facts' })
  assert.deepEqual(calls.map(call => call.method), ['GET', 'POST'])
  assert.equal(calls[1].path, '/api/v1/checkups/offline-job/questions')
})

function partialSnapshot(id = 'offline-job') {
  return {
    report_id: id, center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' },
    location_label: '离线过程样本', isochrone: {},
    groups: [{ id: 'education', name_zh: '教育与儿童', status: 'searching', score: 60,
      score_provisional: true, completed_categories: 1, total_categories: 2, facility_count: 1 }],
    categories: [
      { facility_id: 'school', group_id: 'education', search_status: 'complete', known_count: 1, score: 60 },
      { facility_id: 'nursery', group_id: 'education', search_status: 'pending', known_count: 0, score: null },
    ],
    pois: [{ poi_id: 'school-1', facility_id: 'school', name: '离线小学',
      location: { lng: 121.501, lat: 31.2, crs: 'BD09LL' }, in_isochrone: true }],
    routes: [], completed_categories: 1, total_categories: 2, active_facility_id: 'nursery',
  }
}

test('process scores stay separate from final reports and reject another task or invalid counters', () => {
  const running = { id: 'offline-job', status: 'running', stage: 'poi', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const parsed = parseCheckup(running, 'offline-job')
  assert.equal(parsed.result, null, 'an early score is not a completed report')
  assert.equal(parsed.partial_result.groups[0].score, 60)
  assert.equal(parsed.partial_result.groups[0].score_provisional, true)
  assert.equal(parsed.partial_result.categories[1].score, null, 'not-yet-searched never means zero')
  assert.equal(parsed.partial_result.pois[0].poi_id, 'school-1')

  for (const corrupt of [
    job => { job.partial_result.report_id = 'some-other-task' },
    job => { job.partial_revision = -1 },
    job => { job.partial_result.completed_categories = 3 },
    job => { job.partial_result.groups[0].completed_categories = 3 },
    job => { job.partial_result.groups[0].facility_count = -1 },
    job => { job.partial_result.groups[0].score = 101 },
    job => { job.partial_result.groups[0].status = 'invented' },
    job => { job.partial_result.groups.push({ ...job.partial_result.groups[0] }) },
    job => { job.partial_result.categories[0].group_id = 'unknown-group' },
    job => { job.partial_result.center.crs = 'WGS84' },
  ]) {
    const broken = structuredClone(running)
    corrupt(broken)
    assert.throws(() => parseCheckup(broken), error => error.code === 'contract')
  }
  const legacy = { id: 'legacy', status: 'running', stage: 'coverage', progress: 65, result: null, error: null }
  assert.equal(parseCheckup(legacy).result, null)
  assert.equal(parseCheckup(legacy).partial_result, undefined, 'old servers need not supply process data')
})

test('percent-only polls reuse the same task snapshot and ask for data only after its revision', async () => {
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'poi', progress: 40 }
  const running = { id: handle.id, status: 'running', stage: 'poi', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const replies = [running, { ...running, stage: 'coverage', progress: 75, partial_result: null },
    { id: handle.id, status: 'completed', stage: 'completed', progress: 100,
      result: report, error: null, partial_revision: 3, partial_result: null }]
  const requests = []
  const client = checkupClient(handle.base, async (url, init) => {
    requests.push({ method: init.method, query: url.search })
    return response(replies.shift())
  }, 1000, 0)
  const initial = await client.poll(handle, signal())
  const later = await client.poll(handle, signal())
  assert.equal(later.progress, 75)
  assert.equal(later.partial_result, initial.partial_result, 'the unchanged points and scores remain visible')
  assert.equal(later.result, null)
  const finished = await client.poll(handle, signal())
  assert.equal(finished.result.report_id, handle.id)
  assert.equal(finished.partial_result, null, 'the final report replaces process data')
  assert.deepEqual(requests, [
    { method: 'GET', query: '' },
    { method: 'GET', query: '?after_revision=3' },
    { method: 'GET', query: '?after_revision=3' },
  ])
})

test('process caches do not leak to another task and reconnects request the complete snapshot', async () => {
  const base = 'http://127.0.0.1:8000'
  const paths = []
  const fetcher = async url => {
    paths.push(url.pathname + url.search)
    const id = url.pathname.split('/').at(-1)
    return response({ id, status: 'running', stage: 'poi', progress: 40, result: null, error: null,
      partial_revision: 3, partial_result: id === 'offline-job' ? partialSnapshot(id) : null })
  }
  const client = checkupClient(base, fetcher, 1000, 0)
  const handle = { id: 'offline-job', base, status: 'running', stage: 'poi', progress: 40 }
  await client.poll(handle, signal())
  const other = await client.poll({ ...handle, id: 'other-job' }, signal())
  assert.equal(other.partial_result, null)
  const reconnected = await checkupClient(base, fetcher, 1000, 0).poll(handle, signal())
  assert.equal(reconnected.partial_result.pois[0].poi_id, 'school-1')
  assert.deepEqual(paths, ['/api/v1/checkups/offline-job', '/api/v1/checkups/other-job', '/api/v1/checkups/offline-job'])
})

test('report questions get a longer timeout without changing submission timeout', async () => {
  let calls = 0
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    calls++
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => resolve(response({ answer: '仍可回答', evidence_ids: ['report'], mode: 'report_facts' })), 20)
      init.signal.addEventListener('abort', () => { clearTimeout(timer); reject(new Error('aborted')) }, { once: true })
    })
  }, 5, 0)
  assert.equal((await client.ask('offline-job', '还有哪些设施？', signal())).answer, '仍可回答')
  assert.equal(calls, 1)
})

test('a failed report question can be retried without creating a new checkup', async () => {
  const calls = []
  const client = checkupClient('http://127.0.0.1:8000', async url => {
    calls.push(url.pathname)
    return calls.length === 1 ? response({ detail: 'untrusted model error' }, 502)
      : response({ answer: '重试成功', evidence_ids: ['report'], mode: 'report_facts' })
  }, 1000, 0)
  await assert.rejects(client.ask('offline-job', '报告怎么样？', signal()), error =>
    error.code === 'question' && error.message.includes('无需重新体检') && !error.message.includes('untrusted'))
  assert.equal((await client.ask('offline-job', '报告怎么样？', signal())).answer, '重试成功')
  assert.deepEqual(calls, ['/api/v1/checkups/offline-job/questions', '/api/v1/checkups/offline-job/questions'])
})

test('ambiguous fake POST timeout is marked uncertain and is never automatically retried', async () => {
  let calls = 0
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    calls++
    return await new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('fake timeout')), { once: true }))
  }, 5, 0)
  await assert.rejects(client.submit({ type: 'address', query: '离线测试地址' }, signal()), error => error.code === 'uncertain')
  assert.equal(calls, 1)
})

test('known pre-acceptance missing-key 503 is not an uncertain submission', async () => {
  let calls = 0
  const client = checkupClient('http://127.0.0.1:8000', async () => {
    calls++
    return response({ detail: { code: 'MISSING_SERVER_AK', message: 'untrusted upstream detail' } }, 503)
  }, 1000, 0)
  await assert.rejects(client.submit({ type: 'address', query: '测试地址' }, signal()), error =>
    error.code === 'MISSING_SERVER_AK' && error.message.includes('没有创建') && !error.message.includes('untrusted'))
  assert.equal(calls, 1)
})

test('unknown server errors still prohibit automatic resubmission', async () => {
  for (const [status, body] of [[500, {}], [503, {}], [503, 'not-json']]) {
    let calls = 0
    const client = checkupClient('http://127.0.0.1:8000', async () => {
      calls++
      return typeof body === 'string' ? new Response(body, { status }) : response(body, status)
    }, 1000, 0)
    await assert.rejects(client.submit({ type: 'address', query: '测试地址' }, signal()), error => error.code === 'uncertain')
    assert.equal(calls, 1)
  }
})

test('expired polling returns 404 without submitting or fabricating a failed report', async () => {
  const requests = []
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    requests.push(init.method)
    return response({ detail: 'expired' }, 404)
  }, 1000, 0)
  const pending = { id: 'expired', base: 'http://127.0.0.1:8000', status: 'running', stage: 'routes', progress: 60 }
  await assert.rejects(watchCheckup(client, pending, signal(), () => assert.fail('must not fabricate a status')), error => error.code === '404')
  assert.deepEqual(requests, ['GET'])
})

test('report identity, terminal states, seven groups and 2.5-point scores are checked at the client boundary', () => {
  const completed = { id: 'offline-job', status: 'completed', stage: 'completed', progress: 100, result: report, error: null }
  assert.equal(parseCheckup(completed).result.score, 72.5)
  const corruptions = [
    job => { job.result.report_id = 'another-report' },
    job => { job.result = null },
    job => { job.error = { code: 'FAIL', message: 'bad' } },
    job => { job.status = 'running' },
    job => { job.progress = 90 },
    job => { job.status = 'failed'; job.result = null },
    job => { job.result.categories[1].facility_id = job.result.categories[0].facility_id },
    job => { job.result.categories[0].group_id = 'missing' },
    job => { job.result.groups[1].id = job.result.groups[0].id },
    job => { job.result.groups[0].score = 101 },
    job => { job.result.score = -2.5 },
    job => { job.result.score = 72.7 },
  ]
  for (const corrupt of corruptions) {
    const job = structuredClone(completed)
    corrupt(job)
    assert.throws(() => parseCheckup(job), error => error.code === 'contract')
  }
  const unknown = structuredClone(completed)
  unknown.result.score = null
  unknown.result.groups[0].score = null
  assert.equal(parseCheckup(unknown).result.score, null)
})

test('pending task storage can restore an accepted job without resubmitting', () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  const pending = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'routes', progress: 40 }
  savePending(pending, storage)
  assert.deepEqual(loadPending(storage), pending)
  savePending({ base: pending.base, uncertain: true }, storage)
  assert.deepEqual(loadPending(storage), { base: pending.base, uncertain: true })
  savePending(null, storage)
  assert.equal(loadPending(storage), null)
})

test('poller waits for terminal fake status and does not re-submit', async () => {
  const states = ['queued', 'running', 'completed']
  const seen = []
  let calls = 0
  const client = { poll: async () => ({ id: 'offline-job', base: 'offline', status: states[calls++], stage: states[calls - 1], progress: calls * 30, result: null, error: null }) }
  const final = await watchCheckup(client, { id: 'offline-job', base: 'offline', status: 'queued', stage: 'queued', progress: 0 }, signal(), job => seen.push(job.status), async () => {})
  assert.equal(final.status, 'completed')
  assert.deepEqual(seen, states)
  assert.equal(calls, 3)
})

// --- report question contract v1 (additive fields) ---------------------------

test('ask sends history, focus and mode only when provided, keeping legacy calls unchanged', async () => {
  const bodies = []
  const client = checkupClient('http://127.0.0.1:8000', async (_url, init) => {
    bodies.push(JSON.parse(init.body))
    return response({ answer: '答复', evidence_ids: ['report'], mode: 'model' })
  }, 1000, 0)

  await client.ask('offline-job', '旧调用只有问题', signal())
  await client.ask('offline-job', '新调用带上下文', signal(), {
    history: [
      { role: 'user', content: '上一问' },
      { role: 'assistant', content: '上一答' },
    ],
    focus: { facility_id: 'primary_school', group_id: null, poi_id: null, selected_facility_ids: ['primary_school'] },
    mode: 'web',
  })

  assert.deepEqual(bodies[0], { question: '旧调用只有问题' }, 'omitted options must not add fields')
  assert.deepEqual(bodies[1], {
    question: '新调用带上下文',
    history: [
      { role: 'user', content: '上一问' },
      { role: 'assistant', content: '上一答' },
    ],
    focus: { facility_id: 'primary_school', group_id: null, poi_id: null, selected_facility_ids: ['primary_school'] },
    mode: 'web',
  })
})

test('new answer fields are parsed leniently while old responses stay unchanged', async () => {
  const replies = [
    { answer: '旧版答复', evidence_ids: ['report'], mode: 'report_facts' },
    {
      answer: '新版答复', evidence_ids: ['facility:primary_school', 'web'], mode: 'model',
      web_search_used: true,
      sources: [{ title: '来源', url: 'https://example.invalid/a', published_at: '2026-09-01', authority: 3 }],
      retrieval: { requested: true, status: 'searched', query: '上海 小学', pages_read: 2, attempts: 3, elapsed_ms: 4200 },
      focus_applied: { facility_id: 'primary_school', group_id: null, poi_id: null },
      elapsed_ms: 12800,
    },
    // Unknown optional values must not turn a usable answer into a contract failure.
    { answer: '残缺答复', evidence_ids: ['report'], mode: 'report_facts', retrieval: { status: 'bogus' }, elapsed_ms: 'soon' },
  ]
  let index = 0
  const client = checkupClient('http://127.0.0.1:8000', async () => response(replies[index++]), 1000, 0)

  const legacy = await client.ask('offline-job', '旧问题', signal())
  assert.deepEqual(legacy, { answer: '旧版答复', evidence_ids: ['report'], mode: 'report_facts' })
  assert.equal('retrieval' in legacy, false)

  const extended = await client.ask('offline-job', '新问题', signal(), { mode: 'auto' })
  assert.equal(extended.answer, '新版答复')
  assert.equal(extended.web_search_used, true)
  assert.deepEqual(extended.sources, [{ title: '来源', url: 'https://example.invalid/a', published_at: '2026-09-01', authority: 3 }])
  assert.equal(extended.retrieval.status, 'searched')
  assert.equal(extended.retrieval.pages_read, 2)
  assert.equal(extended.retrieval.query, '上海 小学')
  assert.equal(extended.focus_applied.facility_id, 'primary_school')
  assert.equal(extended.elapsed_ms, 12800)

  const partial = await client.ask('offline-job', '残缺问题', signal())
  assert.deepEqual(partial, { answer: '残缺答复', evidence_ids: ['report'], mode: 'report_facts' })
})

test('broken core answers stay contract errors; invalid options never reach the network', async () => {
  let reply = { answer: '答复', evidence_ids: ['report'], mode: 'model' }
  let posts = 0
  const client = checkupClient('http://127.0.0.1:8000', async () => {
    posts++
    return response(reply)
  }, 1000, 0)

  for (const broken of [
    { answer: '', evidence_ids: ['report'], mode: 'model' },
    { answer: '答复', evidence_ids: [1], mode: 'model' },
    { answer: '答复', evidence_ids: ['report'], mode: 'other' },
    { answer: '答复', mode: 'model' },
  ]) {
    reply = broken
    await assert.rejects(client.ask('offline-job', '问题', signal()), error =>
      error instanceof CheckupApiError && error.code === 'contract')
  }

  const before = posts
  await assert.rejects(client.ask('offline-job', '问题', signal(), { mode: 'always' }), error => error.code === 'input')
  await assert.rejects(client.ask('offline-job', '问题', signal(), { history: [{ role: 'system', content: 'x' }] }), error => error.code === 'input')
  await assert.rejects(client.ask('offline-job', '问题', signal(), { history: [{ role: 'user', content: '' }] }), error => error.code === 'input')
  await assert.rejects(client.ask('offline-job', '问题', signal(), { focus: { facility_id: 42 } }), error => error.code === 'input')
  await assert.rejects(client.ask('offline-job', '问题', signal(), { focus: { selected_facility_ids: ['a', 'a'] } }), error => error.code === 'input')
  assert.equal(posts, before, 'invalid options must be rejected before the request')
})

test('inconsistent retrieval records are normalized without rejecting the answer', async () => {
  const client = checkupClient('http://127.0.0.1:8000', async () => response({
    answer: '答复', evidence_ids: ['report'], mode: 'report_facts',
    retrieval: { requested: false, status: 'searched', pages_read: -1, attempts: 2.7 },
  }), 1000, 0)
  const answer = await client.ask('offline-job', '问题', signal())
  assert.equal(answer.retrieval.requested, false)
  assert.equal(answer.retrieval.status, 'not_requested', 'requested=false can only mean not_requested')
  assert.equal('pages_read' in answer.retrieval, false, 'negative counts are ignored')
  assert.equal(answer.retrieval.attempts, 2)
})

// Real pause/resume controls: only a server response can establish the task state.
test('paused jobs preserve checkpoints and obey the same report/error boundary as other unfinished states', () => {
  const paused = { id: 'offline-job', status: 'paused', stage: 'poi', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const parsed = parseCheckup(paused, paused.id)
  assert.equal(parsed.status, 'paused')
  assert.equal(parsed.stage, 'poi', 'pausing need not erase the current analysis stage')
  assert.equal(parsed.partial_result.pois[0].poi_id, 'school-1')
  for (const change of [
    job => { job.result = report },
    job => { job.error = { code: 'oops', message: 'bad' } },
    job => { job.id = 'other-task' },
  ]) {
    const broken = structuredClone(paused)
    change(broken)
    assert.throws(() => parseCheckup(broken, paused.id), error => error.code === 'contract')
  }
})

test('pause and resume POST to the same task, retain the cached checkpoint, and return actual server states', async () => {
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'poi', progress: 40 }
  const running = { id: handle.id, status: 'running', stage: 'poi', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const replies = [running, { ...running, status: 'paused', partial_result: null },
    { ...running, status: 'queued', partial_result: null },
    { id: handle.id, status: 'completed', stage: 'completed', progress: 100, result: report, error: null }]
  const requests = []
  const client = checkupClient(handle.base, async (url, init) => {
    requests.push({ path: url.pathname, query: url.search, method: init.method, body: init.body })
    return response(replies.shift())
  }, 1000, 0)
  const first = await client.poll(handle, signal())
  const paused = await client.pause(handle, signal())
  assert.equal(paused.status, 'paused')
  assert.equal(paused.base, handle.base)
  assert.equal(paused.partial_result, first.partial_result)
  const resumed = await client.resume(paused, signal())
  assert.equal(resumed.status, 'queued', 'the server decides whether resumption is queued or running')
  assert.equal(resumed.partial_result, first.partial_result)
  const raced = await client.pause(resumed, signal())
  assert.equal(raced.status, 'completed', 'a task that finished before pause was accepted stays completed')
  assert.equal(raced.result.score, report.score)
  assert.deepEqual(requests, [
    { path: '/api/v1/checkups/offline-job', query: '', method: 'GET', body: undefined },
    { path: '/api/v1/checkups/offline-job/pause', query: '', method: 'POST', body: '{}' },
    { path: '/api/v1/checkups/offline-job/resume', query: '', method: 'POST', body: '{}' },
    { path: '/api/v1/checkups/offline-job/pause', query: '', method: 'POST', body: '{}' },
  ])
})

test('failed pause/resume/cancel acknowledgements never claim a confirmed server state and never auto-retry', async () => {
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'poi', progress: 40 }
  for (const method of ['pause', 'resume', 'cancel']) {
    for (const failAs of ['network', 'timeout', '500', 'invalid-json']) {
      let calls = 0
      const client = checkupClient(handle.base, async (_url, init) => {
        calls++
        if (failAs === 'network') throw new TypeError('offline')
        if (failAs === 'timeout') return new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }))
        if (failAs === 'invalid-json') return new Response('bad-json', { status: 200 })
        return response({}, 500)
      }, 5, 0)
      await assert.rejects(client[method](handle, signal()), error => error.code === 'uncertain'
        && error.message.includes({ pause: '暂停检索', resume: '继续检索', cancel: '终止检索' }[method])
        && error.message.includes('未知') && !/已暂停|已恢复|已终止/.test(error.message))
      assert.equal(calls, 1)
      assert.equal(handle.status, 'running', 'the old handle cannot be mutated into an unconfirmed state')
    }
  }
})

test('paused handles restore alongside legacy queued/running storage, and the poller stops without clearing them', async () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'paused', stage: 'coverage', progress: 70 }
  for (const status of ['queued', 'running', 'paused']) {
    savePending({ ...handle, status }, storage)
    assert.deepEqual(loadPending(storage), { ...handle, status })
  }
  let calls = 0, waits = 0
  const seen = []
  const paused = { ...handle, result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const client = { poll: async () => { calls++; return paused } }
  const result = await watchCheckup(client, handle, signal(), job => seen.push(job), async () => { waits++ })
  assert.equal(result, paused)
  assert.equal(result.partial_result.pois[0].poi_id, 'school-1')
  assert.equal(calls, 1)
  assert.equal(waits, 0, 'a confirmed paused task must stop automatic polling immediately')
  assert.deepEqual(seen, [paused])
  assert.deepEqual(loadPending(storage), handle, 'the caller keeps the resumable task handle')
})


test('cancelled is a distinct terminal state with preserved progress and partial evidence', () => {
  const cancelled = { id: 'offline-job', status: 'cancelled', stage: 'cancelled', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const parsed = parseCheckup(cancelled, cancelled.id)
  assert.equal(parsed.status, 'cancelled')
  assert.equal(parsed.progress, 40, 'termination does not invent complete progress')
  assert.equal(parsed.partial_result.pois[0].poi_id, 'school-1')
  for (const change of [
    job => { job.result = report },
    job => { job.error = { code: 'cancelled', message: 'stopped' } },
    job => { job.stage = 'poi' },
  ]) {
    const broken = structuredClone(cancelled)
    change(broken)
    assert.throws(() => parseCheckup(broken, cancelled.id), error => error.code === 'contract')
  }
})

test('cancel POST stops the original task without resubmitting and retains cached process data', async () => {
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'poi', progress: 40 }
  const running = { id: handle.id, status: 'running', stage: 'poi', progress: 40,
    result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  const cancelled = { ...running, status: 'cancelled', stage: 'cancelled', partial_result: null }
  const replies = [running, cancelled, cancelled,
    { id: handle.id, status: 'completed', stage: 'completed', progress: 100, result: report, error: null }]
  const calls = []
  const client = checkupClient(handle.base, async (url, init) => {
    calls.push({ path: url.pathname, method: init.method, body: init.body })
    return response(replies.shift())
  }, 1000, 0)
  const before = await client.poll(handle, signal())
  const stopped = await client.cancel(handle, signal())
  assert.equal(stopped.status, 'cancelled')
  assert.equal(stopped.partial_result, before.partial_result)
  assert.equal(stopped.result, null)
  assert.equal(stopped.error, null)
  assert.equal(stopped.base, handle.base)
  const rechecked = await client.poll(stopped, signal())
  assert.equal(rechecked.status, 'cancelled')
  assert.equal(rechecked.partial_result, before.partial_result, 'terminal process evidence remains inspectable')
  const raced = await client.cancel(handle, signal())
  assert.equal(raced.status, 'completed', 'completion before cancellation wins without losing the report')
  assert.equal(raced.result.score, report.score)
  assert.deepEqual(calls, [
    { path: '/api/v1/checkups/offline-job', method: 'GET', body: undefined },
    { path: '/api/v1/checkups/offline-job/cancel', method: 'POST', body: '{}' },
    { path: '/api/v1/checkups/offline-job', method: 'GET', body: undefined },
    { path: '/api/v1/checkups/offline-job/cancel', method: 'POST', body: '{}' },
  ])
})

test('cancelled handles can be read from storage and the poller returns them without another wait', async () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  const handle = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'cancelled', stage: 'cancelled', progress: 40 }
  savePending(handle, storage)
  assert.deepEqual(loadPending(storage), handle)
  const cancelled = { ...handle, result: null, error: null, partial_revision: 3, partial_result: partialSnapshot() }
  let polls = 0, waits = 0
  const seen = []
  const result = await watchCheckup({ poll: async () => { polls++; return cancelled } }, handle, signal(), job => seen.push(job), async () => { waits++ })
  assert.equal(result, cancelled)
  assert.equal(polls, 1)
  assert.equal(waits, 0)
  assert.deepEqual(seen, [cancelled])
  assert.deepEqual(loadPending(storage), handle, 'storage clearing belongs to the app after it displays the result')
})

test('streamed questions deliver stages and drafts before final response, across UTF-8 boundaries', async () => {
  let push
  const body = new ReadableStream({ start(controller) { push = controller } })
  const events = []
  const client = checkupClient('http://127.0.0.1:8768', async (url) => {
    assert.ok(url.pathname.endsWith('/questions/stream'))
    return new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } })
  }, 1000, 0)
  const pending = client.ask('report', '说明', signal(), { onEvent: event => events.push(event) })
  const bytes = new TextEncoder().encode(JSON.stringify({ type: 'stage', stage: 'report', message: '读取报告' }) + '\n' + JSON.stringify({ type: 'draft', text: '初稿' }) + '\n')
  for (const byte of bytes) push.enqueue(new Uint8Array([byte]))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(events[0].message, '读取报告')
  assert.equal(events[1].text, '初稿')
  push.enqueue(new TextEncoder().encode(JSON.stringify({ type: 'result', result: { answer: '最终答案', mode: 'report_facts', evidence_ids: ['report'] } }) + '\n'))
  push.close()
  assert.equal((await pending).answer, '最终答案')
})

test('stream interruption and error never return a provisional draft as a completed answer', async () => {
  for (const suffix of ['', JSON.stringify({ type: 'error', message: '失败' }) + '\n']) {
    const client = checkupClient('http://127.0.0.1:8768', async () => new Response(
      JSON.stringify({ type: 'draft', text: '未校验' }) + '\n' + suffix,
      { headers: { 'Content-Type': 'application/x-ndjson' } },
    ), 1000, 0)
    const events = []
    await assert.rejects(client.ask('report', '说明', signal(), { onEvent: event => events.push(event) }), error => error.code === 'question')
    assert.equal(events[0].text, '未校验')
  }
})

test('aborting a pending question stream cancels its reader immediately', async () => {
  let cancelled = false
  const body = new ReadableStream({ cancel() { cancelled = true } })
  const controller = new AbortController()
  const client = checkupClient('http://127.0.0.1:8768', async () => new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } }), 1000, 0)
  const pending = client.ask('report', '说明', controller.signal, { onEvent() {} })
  await new Promise(resolve => setImmediate(resolve))
  controller.abort()
  await assert.rejects(pending, error => error.code === 'stopped')
  assert.equal(cancelled, true)
})
