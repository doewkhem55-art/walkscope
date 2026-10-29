import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parseJob, parseCatalog, realClient, watchJob, statusUrl, ApiError } from '../src/api.ts'

const signal = () => new AbortController().signal
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const handle = { schema_version: '2.0', job_id: 'job:demo', run_id: 'run:demo', status: 'queued', status_url: '/api/v2/agent/jobs/job:demo' }
const job = (status, result = null) => ({ schema_version: '2.0', job_id: handle.job_id, run_id: handle.run_id, status, result })
const run = (status) => ({ schema_version: '2.0', run_id: handle.run_id, status })

test('supplied synthetic fixtures and dynamic catalog load without zero-filling missing fields', () => {
  for (const file of ['completed', 'waiting']) {
    const value = JSON.parse(readFileSync(new URL(`../src/fixtures/synthetic-agent-job-state-${file}.example.json`, import.meta.url), 'utf8'))
    assert.equal(parseJob(value).result.status, value.result.status)
  }
  const empty = parseJob(job('completed', run('completed')))
  assert.equal(empty.result.diagnosis, undefined)
  assert.equal(parseJob(job('completed')).result, null)
  const catalog = parseCatalog({ schema_version: '2.0', catalog_version: 'future', groups: [], facilities: [] })
  assert.equal(catalog.facilities.length, 0)
})
test('unknown state, wrong version and cross-job results are rejected', () => {
  assert.throws(() => parseJob(job('other')))
  assert.throws(() => parseJob(job(['completed'])))
  assert.throws(() => parseJob(job(['queued'])))
  assert.throws(() => parseJob({ ...job('running'), schema_version: '1.0' }))
  assert.throws(() => parseJob(job('completed', { ...run('completed'), run_id: 'run:else' }), handle))
  assert.throws(() => parseJob(job('completed', run('failed')), handle))
})
test('status_url resolves using URL semantics and rejects foreign origins', () => {
  const base = new URL('https://api.example.test/')
  assert.equal(statusUrl(base, handle.status_url), 'https://api.example.test/api/v2/agent/jobs/job:demo')
  for (const path of ['https://other.example.test/api/v2/agent/jobs/job:demo', '//other.example.test/api/v2/agent/jobs/x', '/api/v2/agent/jobs/x?token=secret', 'javascript:alert(1)']) assert.throws(() => statusUrl(base, path))
})
test('live catalog first checks the runtime OpenAPI then loads the actual list', async () => {
  const calls = []
  const client = realClient('https://api.example.test', async (url) => {
    calls.push(url.pathname)
    return response(url.pathname === '/openapi.json' ? { paths: { '/api/v2/facilities': { get: {} }, '/api/v2/agent/jobs': { post: {} }, '/api/v2/agent/jobs/{job_id}': { get: {} } } } : { schema_version: '2.0', catalog_version: 'runtime-new', groups: [], facilities: [] })
  })
  assert.equal((await client.catalog(signal())).catalog_version, 'runtime-new')
  assert.deepEqual(calls, ['/openapi.json', '/api/v2/facilities'])
})
test('submit sends only public V2 fields and follows returned status_url', async () => {
  const calls = []
  const client = realClient('https://api.example.test', async (url, init) => {
    calls.push({ url: url.href, ...init })
    return init.method === 'POST' ? response(handle, 202) : response(job('completed', run('completed')))
  })
  const accepted = await client.submit('上海药店', signal())
  await client.poll(accepted, signal())
  assert.deepEqual(JSON.parse(calls[0].body), { schema_version: '2.0', user_message: '上海药店' })
  assert.equal(calls[0].credentials, 'omit')
  assert.equal(calls[1].url, accepted.status_url)
  assert.equal(calls[1].method, 'GET')
})
for (const code of [422, 429, 404]) test(`HTTP ${code} produces a readable error without retry`, async () => {
  let calls = 0
  const client = realClient('https://api.example.test', async () => { calls++; return response({}, code) })
  await assert.rejects(client.poll(handle, signal()), e => e instanceof ApiError && e.code === String(code))
  assert.equal(calls, 1)
})
test('submission timeout never automatically resubmits', async () => {
  let calls = 0
  const client = realClient('https://api.example.test', async (_url, init) => {
    calls++
    return await new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('timeout')), { once: true }))
  }, 5)
  await assert.rejects(client.submit('上海药店', signal()), e => e.code === 'uncertain')
  assert.equal(calls, 1)
})
test('malformed accepted response is an uncertain submission', async () => {
  const client = realClient('https://api.example.test', async () => response({ ...handle, status_url: 'https://evil.test/' }, 202))
  await assert.rejects(client.submit('上海药店', signal()), e => e.code === 'uncertain')
})
for (const code of [200, 500]) test(`unexpected POST HTTP ${code} keeps submission uncertain without retry`, async () => {
  let calls = 0
  const client = realClient('https://api.example.test', async () => { calls++; return response(handle, code) })
  await assert.rejects(client.submit('上海药店', signal()), e => e.code === 'uncertain')
  assert.equal(calls, 1)
})
for (const status of ['completed', 'waiting_for_input', 'failed']) test(`polling stops at ${status} even with HTTP success`, async () => {
  const states = ['queued', 'running', status]
  const seen = []
  let calls = 0
  const client = { poll: async () => parseJob(job(states[calls++], calls === 3 ? run(status) : null)) }
  await watchJob(client, handle, signal(), j => seen.push(j.status), async () => {})
  assert.deepEqual(seen, states)
  assert.equal(calls, 3)
})
test('GET failure can resume the existing job without another POST', async () => {
  let gets = 0
  const client = { poll: async () => { if (++gets === 1) throw new ApiError('network', 'offline'); return parseJob(job('completed', run('completed'))) } }
  await assert.rejects(watchJob(client, handle, signal(), () => {}))
  assert.equal((await watchJob(client, handle, signal(), () => {})).status, 'completed')
  assert.equal(gets, 2)
})
test('aborting stops future GETs and does not submit or cancel a backend task', async () => {
  const controller = new AbortController()
  let calls = 0
  const client = { poll: async () => { calls++; return parseJob(job('running')) } }
  await assert.rejects(watchJob(client, handle, controller.signal, () => controller.abort(), async () => {}), e => e.code === 'stopped')
  assert.equal(calls, 1)
})
