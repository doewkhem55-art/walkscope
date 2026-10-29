import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { ApiError } from '../src/api.ts'
import { calculateReferenceScore, fetchCompetition, parseCompetition, parseDiagnosis, requestDiagnosis } from '../src/competition.ts'

const fixtureUrl = new URL('../src/fixtures/tongji-life-circle-2026-10-08.json', import.meta.url)
const fixtureText = readFileSync(fixtureUrl, 'utf8')
const fixture = JSON.parse(fixtureText)
const signal = () => new AbortController().signal
const response = value => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })

test('validates the fixed sample packet and keeps nullable metrics', () => {
  assert.equal(createHash('sha256').update(fixtureText).digest('hex'), '4f45ca46cbfe1578b86705891f13dbbf8f64e56024ee5c026d5a775b90e28291')
  const parsed = parseCompetition(fixture)
  assert.equal(parsed.sample.mode, 'live')
  assert.equal(parsed.sample.data_date, '2026-10-08')
  assert.equal(parsed.diagnosis.data.metrics[0].poi_count, 29)
  assert.equal(parsed.diagnosis.data.metrics[0].confirmed_reachable_15m_count, 1)
  assert.equal(parsed.diagnosis.data.blindspot.grid_size_m, 200)

  const nullable = structuredClone(fixture)
  Object.assign(nullable.diagnosis.data.metrics[0], {
    poi_count: null, confirmed_reachable_15m_count: null, unresolved_route_count: null,
    nearest_walk_distance_m: null, nearest_walk_duration_s: null,
    covered_ratio: null, blind_ratio: null, unknown_ratio: null,
  })
  assert.equal(parseCompetition(nullable).diagnosis.data.metrics[0].poi_count, null)
})

test('reduces the coverage range to one score in 2.5-point steps', () => {
  const metrics = fixture.diagnosis.data.metrics
  const score = calculateReferenceScore(metrics)
  assert.equal(typeof score, 'number')
  assert.equal((score * 2) % 5, 0)
  assert.equal(score, 95)
  assert.equal(calculateReferenceScore(metrics.slice(0, 2)), null)
})

test('rejects wrong packet version, failed V1 envelope, mismatched centers, and malformed GeoJSON', () => {
  assert.throws(() => parseCompetition({ ...fixture, schema_version: '2.0' }), e => e instanceof ApiError && e.code === 'contract')

  for (const sample of [
    { ...fixture.sample, mode: ['live'] },
    { ...fixture.sample, source_sha256: [fixture.sample.source_sha256] },
    { ...fixture.sample, data_date: '2026-99-99' },
    { ...fixture.sample, collected_at: null },
  ]) {
    assert.throws(() => parseCompetition({ ...fixture, sample }), e => e.code === 'contract')
  }

  const failed = structuredClone(fixture)
  failed.diagnosis.ok = false
  assert.throws(() => parseCompetition(failed), e => e.code === 'contract')

  const mismatched = structuredClone(fixture)
  mismatched.diagnosis.data.center.lat += 0.001
  assert.throws(() => parseCompetition(mismatched), e => e.code === 'contract')

  const openRing = structuredClone(fixture)
  openRing.diagnosis.data.isochrone.geometry.coordinates[0][0][0] += 0.001
  assert.throws(() => parseCompetition(openRing), e => e.code === 'contract')

  const outOfRange = structuredClone(fixture)
  outOfRange.diagnosis.data.blindspot.unknown_areas.features[0].geometry.coordinates[0][0][0] = 181
  assert.throws(() => parseCompetition(outOfRange), e => e.code === 'contract')
})

test('fetches the sample with one read-only, no-store request', async () => {
  const calls = []
  const packet = await fetchCompetition('https://api.example.test', signal(), async (url, init) => {
    calls.push({ url: url.href, ...init })
    return response(fixture)
  })
  assert.equal(packet.sample.id, 'tongji')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://api.example.test/api/demo/sample')
  assert.equal(calls[0].method, 'GET')
  assert.equal(calls[0].cache, 'no-store')
  assert.equal(calls[0].credentials, 'omit')
  assert.equal(calls[0].redirect, 'error')
  assert.equal(calls[0].body, undefined)
})

test('times out without retrying the fixed sample request', async () => {
  let calls = 0
  await assert.rejects(fetchCompetition('https://api.example.test', signal(), async (_url, init) => {
    calls++
    return await new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('timeout')), { once: true }))
  }, 5), e => e instanceof ApiError && e.code === 'timeout')
  assert.equal(calls, 1)
})

test('parses the shared V1 diagnosis envelope and preserves unavailable metrics', () => {
  const parsed = parseDiagnosis(fixture.diagnosis)
  assert.equal(parsed.data.center.crs, 'BD09LL')
  assert.equal(parsed.data.metrics[0].confirmed_reachable_15m_count, 1)

  const unavailable = structuredClone(fixture.diagnosis)
  Object.assign(unavailable.data.metrics[0], {
    poi_count: null, confirmed_reachable_15m_count: null, unresolved_route_count: null,
    nearest_walk_distance_m: null, nearest_walk_duration_s: null,
    covered_ratio: null, blind_ratio: null, unknown_ratio: null,
  })
  assert.equal(parseDiagnosis(unavailable).data.metrics[0].covered_ratio, null)

  const invalidRatios = structuredClone(fixture.diagnosis)
  invalidRatios.data.metrics[0].covered_ratio = 0.8
  assert.throws(() => parseDiagnosis(invalidRatios), e => e instanceof ApiError && e.code === 'contract')
})

test('submits one V1 three-category diagnosis with the address or BD09LL location contract', async () => {
  const calls = []
  const result = await requestDiagnosis('https://api.example.test', {
    type: 'coordinate', lng: fixture.sample.center.lng, lat: fixture.sample.center.lat, crs: 'BD09LL',
  }, signal(), async (url, init) => {
    calls.push({ url: url.href, ...init })
    return response(fixture.diagnosis)
  })
  assert.equal(result.ok, true)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://api.example.test/api/v1/diagnosis')
  assert.equal(calls[0].method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].body), {
    schema_version: '1.0',
    location: { type: 'coordinate', lng: fixture.sample.center.lng, lat: fixture.sample.center.lat, crs: 'BD09LL' },
    facility_types: ['market', 'pharmacy', 'primary_school'],
  })
  assert.equal(calls[0].credentials, 'omit')
  assert.equal(calls[0].redirect, 'error')
})
