import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as client from '../src/checkups.ts'
import * as mapHelpers from '../src/checkupMap.ts'
import * as reportDrafts from '../src/reportDrafts.ts'

// Exercise the actual script setup and lifecycle without a DOM or network SDK.
const source = readFileSync(new URL('../src/CheckupApp.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'checkup-app-offline' }).content.replaceAll('import.meta.env', '({})')
const code = ts.transpileModule(compiled, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const fixture = JSON.parse(readFileSync(new URL('../src/fixtures/facility-catalog.example.json', import.meta.url), 'utf8'))
const catalog = { ...fixture, version: 'offline-only', runtime: { map_service_configured: true } }
const spec = { paths: {
  '/api/v1/checkup-catalog': { get: {} }, '/api/v1/checkups': { post: {} },
  '/api/v1/checkups/{checkup_id}': { get: {} }, '/api/v1/checkups/{checkup_id}/questions': { post: {} },
} }
const response = (body, status = 200) => new Response(JSON.stringify(body), { status })
const pending = { id: 'offline-job', base: 'http://127.0.0.1:8000', status: 'running', stage: 'routes', progress: 50 }
const report = {
  report_id: pending.id, created_at: '2026-10-14T00:00:00Z', center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' },
  location_label: '仅供离线状态测试', catalog_version: catalog.version, isochrone: {},
  categories: catalog.facilities.map(f => ({ facility_id: f.id, group_id: f.group_id })),
  groups: catalog.groups.map(g => ({ ...g, score: null })), score: null, score_method: 'offline',
  summary: '合成测试，不是真实调查', pois: [], routes: [], warnings: [], blindspot: {}, quality: {},
}
const completed = { ...pending, status: 'completed', stage: 'completed', progress: 100, result: report, error: null }

async function settled(predicate) {
  for (let i = 0; i < 40; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    if (predicate()) return
  }
  assert.fail('Component did not settle')
}

function mount(request, restore = pending, search = '', extras = {}) {
  const entries = new Map()
  const elements = new Map()
  class FakeDetails { open = false; scrollIntoView() { this.scrolled = true } }
  const storage = { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value), removeItem: key => entries.delete(key) }
  if (restore) client.savePending(restore, storage)
  const calls = []
  const listeners = []
  const fetcher = async (url, init) => {
    calls.push({ origin: url.origin, path: url.pathname, method: init.method, body: init.body })
    if (url.pathname === '/openapi.json') return response(spec)
    if (url.pathname === '/api/v1/checkup-catalog') return response(catalog)
    return request(url, init)
  }
  const exports = {}
  vm.runInNewContext(code, {
    exports, require: name => name === 'vue' ? Vue : name === './checkupMap.ts' ? mapHelpers : name === './reportDrafts.ts' ? reportDrafts : name === './checkups.ts' ? {
      ...client,
      checkupClient: base => client.checkupClient(base, fetcher, 1000, 0),
      loadPending: () => client.loadPending(storage),
      savePending: value => client.savePending(value, storage),
    } : { default: {} },
    ...extras, AbortController, URL, Blob, Error, setTimeout, clearTimeout, console, window: { location: { search } },
    document: { getElementById: id => elements.get(id) ?? null,
      addEventListener: (type,handler,capture) => listeners.push({type,handler,capture}), removeEventListener() {} }, HTMLDetailsElement: FakeDetails,
  })
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({
    createElement: node, createText: node, createComment: node,
    insert: (child, parent) => parent.children.push(child), remove() {},
    setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ ...exports.default, render: () => null })
  const instance = app.mount(node())
  return { state: instance.$.setupState, storage, calls, listeners, elements, FakeDetails, stop: () => app.unmount() }
}

test('a fresh workspace honors the URL service while a restored task stays on its original service', async t => {
  const explicit = 'http://127.0.0.1:9001'
  const search = `?view=map&base=${encodeURIComponent(explicit)}`
  const fresh = mount(() => assert.fail('a new workspace must not start analysis by itself'), null, search)
  t.after(fresh.stop)
  await settled(() => fresh.state.ready && !fresh.state.busy)
  assert.equal(fresh.state.base, explicit)
  assert.ok(fresh.calls.every(call => call.origin === explicit && call.method === 'GET'))
  assert.ok(fresh.calls.some(call => call.path === '/api/v1/checkup-catalog'))

  const resumed = mount(() => response(completed), pending, search)
  t.after(resumed.stop)
  await settled(() => resumed.state.checkup?.status === 'completed' && !resumed.state.busy)
  assert.equal(resumed.state.base, pending.base)
  assert.equal(resumed.state.handle.base, pending.base)
  assert.ok(resumed.calls.every(call => call.origin === pending.base && call.method === 'GET'))
})

test('expired restored task clears the UI lock and storage without issuing a POST', async t => {
  const app = mount(() => response({ detail: 'expired' }, 404))
  t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  assert.equal(app.state.handle, null)
  assert.equal(app.state.checkup, null)
  assert.equal(client.loadPending(app.storage), null)
  assert.equal(app.state.activeTask, false)
  app.state.address = '下一处测试地点'
  assert.equal(app.state.canSubmit, true)
  assert.ok(app.calls.every(call => call.method === 'GET'))
})

test('definitive missing-key rejection does not leave an uncertain task lock', async t => {
  const app = mount(() => response({ detail: { code: 'MISSING_SERVER_AK' } }, 503), null)
  t.after(app.stop)
  await settled(() => app.state.ready)
  app.state.address = '测试地点'
  await app.state.submit()
  assert.equal(app.state.uncertainRequest, false)
  assert.equal(app.state.handle, null)
  assert.equal(app.state.ready, false)
  assert.equal(client.loadPending(app.storage), null)
  assert.match(app.state.error, /没有创建/)
  assert.equal(app.calls.filter(call => call.method === 'POST').length, 1)
})

test('changing the service drops the old report and its conversation', async t => {
  const app = mount(() => response(completed))
  t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)

  // The host no longer keeps a parallel transcript: the conversation lives in
  // ReportConversation.vue, which is keyed on the service base and the report id, and
  // its own tests cover the late-answer discard. This test still guards the host side:
  // a service change must drop the report, the handle and the binding the view reads.
  app.state.base = 'http://other-service.invalid:8000'
  await Vue.nextTick()
  await settled(() => !app.state.ready || app.state.connecting === false)
  assert.equal(app.state.checkup, null)
  assert.equal(app.state.handle, null)
  assert.equal(app.state.ready, false)
  assert.equal(typeof app.state.revealEvidence, 'function',
    'the host must still own the locate handler the conversation view calls')
})

test('facility evidence opens its collapsed group before scrolling to it', async t => {
  const app = mount(() => response(completed))
  t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  const id = catalog.facilities[0].id
  const details = new app.FakeDetails()
  app.elements.set(`checkup-group-${catalog.facilities[0].group_id}`, details)
  app.state.revealEvidence({ kind: 'facility', key: `facility:${id}`, id })
  assert.equal(details.open, true)
  assert.equal(details.scrolled, true)
})

test('new map selection does not move the already computed report center', async t => {
  const app = mount(() => response(completed))
  t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  app.state.mapSelect({ lng: 121.55, lat: 31.25 })
  assert.equal(app.state.coordinate.lng, 121.55)
  assert.equal(app.state.mapCenter.lng, report.center.lng)
  assert.equal(app.state.checkup.result.report_id, report.report_id)
  assert.equal(app.state.locationRequest().lng, 121.55)
})

const queued = { id: pending.id, status: 'queued', stage: 'queued', progress: 0 }
const checkupRequest = (_url, init) => init.method === 'POST' ? response(queued, 202) : response(completed)

test('loading a catalog selects all 45 categories for a new analysis', async t => {
  const app = mount(checkupRequest, null)
  t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  assert.equal(app.state.selectedCount, 45)
  assert.equal(app.state.selectionError, '')
  assert.deepEqual(app.state.facilityGroups.map(group => group.facilities.length),
    catalog.groups.map(group => catalog.facilities.filter(facility => facility.group_id === group.id).length))
  assert.deepEqual(app.state.facilityGroups.map(group => group.selected),
    app.state.facilityGroups.map(group => group.facilities.length))
  assert.equal(app.state.canSubmit, false, 'a location is still required')
  app.state.address = '测试地点'
  assert.equal(app.state.canSubmit, true)
})

test('the default selection submits all 45 category ids in catalog order', async t => {
  const app = mount(checkupRequest, null)
  t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  app.state.address = '测试地点'
  await app.state.submit()
  const post = app.calls.find(call => call.method === 'POST')
  assert.ok(post, 'a checkup should be submitted')
  assert.deepEqual(JSON.parse(post.body).facility_ids, catalog.facilities.map(facility => facility.id))
})

test('deselecting a group submits all remaining facility ids in catalog order', async t => {
  const app = mount(checkupRequest, null); t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  const removed = catalog.groups[0].id
  app.state.toggleGroup(removed, { target: { checked: false } })
  const expected = catalog.facilities.filter(f => f.group_id !== removed).map(f => f.id)
  assert.equal(app.state.selectedCount, expected.length)
  assert.equal(app.state.facilityGroups[0].selected, 0)
  app.state.address = '测试地点'
  await app.state.submit()
  assert.deepEqual(JSON.parse(app.calls.find(call => call.method === 'POST').body).facility_ids, expected)
})

test('an empty selection disables submit and shows the required hint', async t => {
  const app = mount(checkupRequest, null)
  t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  app.state.address = '测试地点'
  assert.equal(app.state.canSubmit, true)
  for (const group of catalog.groups) app.state.clearGroup(group.id)
  assert.equal(app.state.selectedCount, 0)
  assert.equal(app.state.canSubmit, false)
  assert.equal(app.state.selectionError, '至少选择一个设施类别')
  await app.state.submit()
  assert.equal(app.calls.filter(call => call.method === 'POST').length, 0)
})

test('group checkboxes restore member ids and lock selection during analysis', async t => {
  const app = mount(checkupRequest, null); t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  const group = app.state.facilityGroups[0]
  app.state.toggleGroup(group.id, { target: { checked: false } })
  assert.equal(app.state.selectedCount, 45 - group.facilities.length)
  assert.equal(app.state.facilityGroups[1].selected, app.state.facilityGroups[1].facilities.length)
  app.state.toggleGroup(group.id, { target: { checked: true } })
  assert.equal(app.state.selectedCount, 45)
  assert.deepEqual(app.state.currentFacilitySelection(), catalog.facilities.map(f => f.id))
  app.state.busy = true
  assert.equal(app.state.selectionLocked, true)
  app.state.toggleGroup(group.id, { target: { checked: false } })
  assert.equal(app.state.selectedCount, 45, 'running analysis keeps its original category selection')
  app.state.busy = false
  assert.equal(app.state.selectionLocked, false)
})

test('the conversation focus only names identifiers present in the current report', async t => {
  const app = mount(() => response(completed))
  t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)

  const categoryIds = completed.result.categories.map(category => category.facility_id)
  assert.deepEqual(app.state.conversationFocus.selected_facility_ids, categoryIds)
  assert.equal(app.state.conversationFocus.facility_id, null)
  assert.equal(app.state.conversationFocus.poi_id, null)
  assert.equal(app.state.conversationFocus.group_id, null)

  const group = catalog.groups[0]
  app.state.groupFilter = group.id
  assert.equal(app.state.conversationFocus.group_id, group.id)
  app.state.groupFilter = 'group-not-in-report'
  assert.equal(app.state.conversationFocus.group_id, null, 'a group outside the report must not be sent')

  app.state.clearGroup(group.id)
  const expected = catalog.facilities.filter(f => f.group_id !== group.id).map(f => f.id)
  assert.deepEqual(app.state.conversationFocus.selected_facility_ids, expected)

  await Vue.nextTick()
  app.state.checkup.result.pois.push({ poi_id: 'poi-1', name: '测试点位' })
  assert.equal(app.state.poiNames['poi-1'], '测试点位')
  const details = new app.FakeDetails()
  app.elements.set('checkup-poi-poi-1', details)
  app.state.revealEvidence({ kind: 'poi', key: 'poi:poi-1', id: 'poi-1' })
  assert.equal(details.open, true)
  assert.equal(details.scrolled, true)
})


test('a selected POI becomes question focus only when it belongs to the current report', async t => {
  const app = mount(() => response(completed)); t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  app.state.checkup.result.pois.push({ poi_id: 'focus-school', name: '小学' })
  app.state.focusPoi('focus-school')
  assert.equal(app.state.conversationFocus.poi_id, 'focus-school')
  assert.equal(app.state.resultTab, 'report', 'selecting a map point must not open the question window')
  app.state.focusPoi('unknown-poi')
  assert.equal(app.state.conversationFocus.poi_id, 'focus-school')
  app.state.checkup = { ...completed, result: { ...report, report_id: 'another', pois: [] } }
  await Vue.nextTick()
  assert.equal(app.state.conversationFocus.poi_id, null)
})

test('restoring a single-category report keeps the next analysis at 45 without changing the report', async t => {
  const primary = catalog.facilities.find(facility => facility.id === 'primary_school')
  const singleCategory = { ...completed, result: { ...report,
    categories: [{ facility_id: primary.id, group_id: primary.group_id }],
    groups: report.groups.filter(group => group.id === primary.group_id),
    selected_facility_ids: [primary.id], score: 90,
  } }
  const app = mount(() => response(singleCategory)); t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  assert.equal(app.state.selectedCount, 45)
  assert.equal(app.state.checkup.result.categories.length, 1)
  assert.equal(app.state.checkup.result.score, 90)
  assert.deepEqual(app.state.checkup.result.selected_facility_ids, [primary.id])
  assert.deepEqual(app.state.conversationFocus.selected_facility_ids, [primary.id])
})

test('changing the next analysis groups leaves the saved report scope and score unchanged', async t => {
  const app = mount(() => response(completed)); t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  app.state.clearGroup(catalog.groups[0].id)
  assert.ok(app.state.selectedCount < 45)
  assert.equal(app.state.checkup.result.categories.length, 45)
  assert.equal(app.state.checkup.result.score, report.score)
})

test('partial scores and map points appear before the final report without inventing pending scores', async t => {
  const app = mount(checkupRequest, null); t.after(app.stop)
  await settled(() => app.state.ready && !app.state.busy)
  assert.equal(app.state.progressGroups.length, 7)
  assert.ok(app.state.progressGroups.every(group => group.status === 'pending' && group.score === null))
  assert.equal(app.state.completedProgressGroups.length, 0)
  const partial = {
    report_id: pending.id, center: report.center, isochrone: { type: 'Polygon' },
    categories: [{ facility_id: catalog.facilities[0].id, group_id: catalog.groups[0].id }],
    groups: [{ id: catalog.groups[0].id, name_zh: catalog.groups[0].name_zh, status: 'complete', score: 70,
      score_provisional: false, completed_categories: 6, total_categories: 6, facility_count: 12 },
      { id: catalog.groups[1].id, name_zh: catalog.groups[1].name_zh, status: 'searching', score: null,
        score_provisional: true, completed_categories: 0, total_categories: 7, facility_count: 2 }],
    pois: [{ poi_id: 'live-poi', facility_id: catalog.facilities[0].id }], routes: [],
  }
  app.state.checkup = { ...pending, partial_result: partial, partial_revision: 2 }
  assert.equal(app.state.checkup.result, undefined)
  assert.equal(app.state.progressGroups[0].score, 70)
  assert.equal(app.state.progressGroups[1].score, null)
  assert.deepEqual(Array.from(app.state.completedProgressGroups, group => group.id), [catalog.groups[0].id],
    'only completed groups appear beside the progress bar')
  assert.equal(app.state.progressScore(null), '—')
  assert.equal(app.state.progressScore(0), '0', 'a genuine zero remains distinguishable from unknown')
  assert.equal(app.state.progressPercent, 0, 'stage percentage is not a completed-category percentage')
  app.state.checkup.partial_result.categories[0].search_status = 'complete'
  assert.equal(app.state.progressPercent, 2)
  assert.equal(app.state.mapReport.pois[0].poi_id, 'live-poi')
  assert.equal(app.state.mapReport.quality.processing, true)
  assert.equal(app.state.mapCenter.lng, report.center.lng)
  app.state.checkup = completed
  assert.equal(app.state.mapReport.report_id, report.report_id)
  assert.equal(app.state.mapReport.quality.processing, undefined)
})

test('a replay badge remains visible when the live map service is unconfigured', async t => {
  const app = mount(() => response(completed)); t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  app.state.checkup.result.quality.historical_replay = true
  app.state.catalog.runtime.map_service_configured = false
  await Vue.nextTick()
  assert.equal(app.state.statusLabel, '历史报告回放')
})


test('local case loads all seven scored groups without a new analysis POST', async t => {
  const saved = JSON.parse(readFileSync(new URL('../public/cases/tongji.json', import.meta.url), 'utf8'))
  const app = mount(() => assert.fail('case must not call analysis'), null, '', {fetch: async (path, options) => {
    assert.equal(options.cache, 'no-store')
    assert.equal(path, '/cases/tongji.json'); return response(saved)
  }})
  t.after(app.stop)
  await settled(() => app.state.ready)
  await app.state.showCase()
  assert.equal(app.state.checkup.result.score, 55)
  assert.equal(app.state.checkup.id, 'tongji-all7-small-updated-20261027')
  assert.equal(app.state.checkup.result.quality.coverage_validation.poi_check_passed, true)
  assert.equal(app.state.checkup.result.quality.coverage_validation.evidence_complete, true)
  assert.equal(app.state.checkup.result.categories.length, 45)
  assert.equal(app.state.checkup.result.quality.coverage_geometry_grid_m, null)
  assert.equal(app.state.displayGroups.length, 7)
  assert.equal(app.state.allGroupsComplete, true)
  assert.equal(app.state.progressPercent, 100)
  assert.equal(app.state.isCase, true)
  assert.equal(app.calls.filter(c => c.method === 'POST').length, 0)
  app.state.checkup.result.categories[0].search_status = 'partial'
  assert.equal(app.state.allGroupsComplete, false, 'missing data suppresses confirmed overall score')
})

test('completed search cannot conceal unfinished coverage or unverified inside POIs', async t => {
  const saved = structuredClone(completed)
  saved.result.quality.coverage_validation = {poi_check_passed:false,evidence_complete:false,unverified_poi_count:3}
  saved.result.blindspot.group_coverage = {community_safety:{unknown_ratio:.575,blind_ratio:0}}
  const app = mount(() => response(saved))
  t.after(app.stop)
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  assert.match(app.state.coverageNotice,/3.*设施.*未通过/)
  assert.match(app.state.coverageNotice,/未完成核验/)
  app.state.checkup.result.quality.coverage_validation = {poi_check_passed:true,evidence_complete:false,unverified_poi_count:0}
  assert.match(app.state.coverageNotice,/仍有未核验区域/)
  app.state.checkup.result.blindspot.group_coverage.community_safety.unknown_ratio=0
  app.state.checkup.result.quality.coverage_validation.evidence_complete=true
  assert.equal(app.state.coverageNotice,'')
})

test('a recomputed saved case binds questions to its own new report identity', async t => {
  const saved = JSON.parse(readFileSync(new URL('../public/cases/tongji.json', import.meta.url), 'utf8'))
  saved.id = saved.result.report_id = 'tongji-fine-grid'
  saved.result.quality.coverage_recomputed_from = 'original-report'
  saved.result.quality.grid_size_m = 20
  const app = mount(() => assert.fail('case must not start a new analysis'), null, '', {
    fetch: async () => response(saved),
  })
  t.after(app.stop)
  await settled(() => app.state.ready)
  await app.state.showCase()
  assert.equal(app.state.checkup.id, 'tongji-fine-grid')
  assert.equal(app.state.handle.id, 'tongji-fine-grid')
  assert.equal(app.state.checkup.result.quality.grid_size_m, 20)
  assert.equal(app.state.error, '')
})

test('chosen address uses exact suggested coordinates and keyboard selection closes the list', async t => {
  const app = mount(() => assert.fail('selection must not submit analysis'), null)
  t.after(app.stop)
  await settled(() => app.state.ready)
  app.state.suggestions = [{title:'同济大学',address:'四平路1239号',point:{lng:121.5,lat:31.2}}]
  app.state.suggestionKey({key:'ArrowDown',preventDefault(){}})
  assert.equal(app.state.suggestionIndex, 0)
  app.state.suggestionKey({key:'Enter',preventDefault(){}})
  assert.equal(app.state.address, '同济大学')
  assert.equal(app.state.suggestions.length, 0)
  assert.deepEqual(JSON.parse(JSON.stringify(app.state.locationRequest())), {type:'coordinate',lng:121.5,lat:31.2,crs:'BD09LL'})
})

test('case loading cannot interrupt a submitting task or overlap a new analysis', async t => {
  let release, reads = 0
  const saved = JSON.parse(readFileSync(new URL('../public/cases/tongji.json', import.meta.url), 'utf8'))
  const app = mount(() => assert.fail('analysis must stay locked'), null, '', {fetch: () => {
    reads++; return new Promise(resolve => { release = () => resolve(response(saved)) })
  }})
  t.after(app.stop)
  await settled(() => app.state.ready)
  app.state.address = '同济大学'
  app.state.busy = true
  await app.state.showCase()
  assert.equal(reads, 0, 'a pending POST cannot be aborted by case loading')
  app.state.busy = false
  const loading = app.state.showCase()
  assert.equal(app.state.caseLoading, true)
  assert.equal(app.state.canSubmit, false)
  assert.equal(app.state.selectionLocked, true)
  await app.state.showCase()
  assert.equal(reads, 1, 'a second case request cannot race the first')
  await app.state.submit()
  assert.equal(app.calls.filter(c=>c.method==='POST').length, 0)
  release()
  await loading
  assert.equal(app.state.caseLoading, false)
  assert.equal(app.state.checkup.result.report_id, saved.result.report_id)
})

test('switching to map selection invalidates and hides old address suggestions', async t => {
  const app = mount(() => assert.fail('switching modes is view-only'), null)
  t.after(app.stop)
  await settled(() => app.state.ready)
  app.state.suggestions = [{title:'旧建议',address:'旧地址',point:{lng:121.5,lat:31.2}}]
  app.state.suggestionStatus = '正在查找地点…'
  app.state.mode = 'coordinate'
  await Vue.nextTick()
  assert.equal(app.state.suggestions.length, 0)
  assert.equal(app.state.suggestionStatus, '')
  assert.equal(app.state.suggestionIndex, -1)
})


test('score cards toggle map group visibility and expand the actual subcategories without analysis', async t => {
 const app=mount(()=>response(completed));t.after(app.stop)
 await settled(()=>app.state.checkup?.status==='completed'&&!app.state.busy)
 assert.equal(app.state.dockExpanded,false)
 app.state.dockHovered=true
 assert.equal(app.state.dockExpanded,true)
 const id=catalog.groups[0].id
 app.state.toggleDisplayGroup(id)
 assert.equal(app.state.groupFilter,id)
 assert.equal(app.state.selectedGroupDetails,id)
 assert.ok(app.state.groupCategories(id).every(c=>c.group_id===id))
 app.state.dockHovered=false
 assert.equal(app.state.dockExpanded,true,'clicked details remain open until an outside click')
 app.state.toggleDisplayGroup(id)
 assert.equal(app.state.groupFilter,'all')
 assert.equal(app.state.selectedGroupDetails,'')
 app.state.dockHovered=false
 assert.equal(app.state.dockExpanded,false)
 assert.equal(app.calls.filter(c=>c.method==='POST').length,0)
})

test('scrolling or resizing the search pane dismisses its detached address popup', async t => {
 const app=mount(()=>response(completed),null);t.after(app.stop)
 await settled(()=>app.state.ready)
 app.state.suggestions=[{title:'地址建议',address:'四平路',point:{lng:121.5,lat:31.2}}]
 app.state.suggestionStatus='正在查找地点…'
 app.state.suggestionIndex=0
 app.state.dismissSuggestions()
 assert.equal(app.state.suggestions.length,0)
 assert.equal(app.state.suggestionStatus,'')
 assert.equal(app.state.suggestionIndex,-1)
})


test('an outside pointer collapses score details and restores every POI group', async t => {
 const app=mount(()=>response(completed)); t.after(app.stop)
 await settled(()=>app.state.checkup?.status==='completed'&&!app.state.busy)
 const id=catalog.groups[0].id
 app.state.toggleDisplayGroup(id)
 app.state.dockHovered=true
 app.state.dockFocused=true
 const listener=app.listeners.find(item=>item.type==='pointerdown')
 assert.equal(listener.capture,true,'map SDK bubbling prevention cannot swallow the outside click')
 listener.handler({target:{closest:selector=>selector==='.group-card'?{}:null}})
 assert.equal(app.state.selectedGroupDetails,id,'a pointer inside a card preserves its item')
 listener.handler({target:{closest:()=>null}})
 assert.equal(app.state.selectedGroupDetails,'')
 assert.equal(app.state.dockExpanded,false)
 assert.equal(app.state.groupFilter,'all','collapsing the card restores every POI layer')
 const nextId=catalog.groups[1].id
 app.state.toggleDisplayGroup(nextId)
 assert.equal(app.state.groupFilter,nextId,'another category still selects its own POI layer')
 assert.equal(app.state.selectedGroupDetails,nextId)
 assert.deepEqual(catalog.groups.map(g=>app.state.groupAccent(g.id)), [...mapHelpers.MAP_GROUP_COLORS])
})

const resumablePartial = {
  report_id: pending.id, center: report.center, location_label: report.location_label, isochrone: {},
  categories: catalog.facilities.map(f => ({ facility_id: f.id, group_id: f.group_id,
    search_status: f.group_id === catalog.groups[0].id ? 'complete' : 'pending' })),
  groups: catalog.groups.map((g, index) => ({ ...g, status: index === 0 ? 'complete' : 'pending',
    score: index === 0 ? 70 : null, score_provisional: false,
    completed_categories: index === 0 ? catalog.facilities.filter(f => f.group_id === g.id).length : 0,
    total_categories: catalog.facilities.filter(f => f.group_id === g.id).length, facility_count: index === 0 ? 12 : 0 })),
  pois: [{ poi_id: 'retained-poi', facility_id: catalog.facilities[0].id }], routes: [],
  completed_categories: catalog.facilities.filter(f => f.group_id === catalog.groups[0].id).length,
  total_categories: catalog.facilities.length, active_facility_id: catalog.facilities[6].id,
}
const resumableJob = { ...pending, stage: 'poi', result: null, error: null,
  partial_revision: 4, partial_result: resumablePartial }

test('server pause keeps progress and locks new submission, then resumes the same task without another analysis', async t => {
  let releasePause
  const pauseReply = new Promise(resolve => { releasePause = resolve })
  let resumed = false
  const app = mount((url, init) => {
    if (url.pathname.endsWith('/pause')) return pauseReply
    if (url.pathname.endsWith('/resume')) {
      resumed = true
      return response({ ...resumableJob, status: 'queued', partial_result: null })
    }
    assert.equal(init.method, 'GET', 'neither pause nor resume may submit a new analysis')
    return response(resumed ? completed : resumableJob)
  })
  t.after(app.stop)
  await settled(() => app.state.ready && app.state.checkup?.partial_revision === 4)
  app.state.searchOpen = false
  app.state.address = '不能创建第二份任务'
  const percentage = app.state.progressPercent
  assert.equal(app.state.controlLabel, '暂停检索', 'control state remains available with the search form collapsed')
  const pausing = app.state.pauseSearch()
  await settled(() => app.calls.some(call => call.path.endsWith('/pause')))
  assert.equal(app.state.controlBusy, true)
  assert.equal(app.state.checkup.status, 'running', 'the UI waits for the server before claiming a pause')
  await app.state.pauseSearch()
  assert.equal(app.calls.filter(call => call.path.endsWith('/pause')).length, 1, 'a pending control request cannot be duplicated')
  releasePause(response({ ...resumableJob, status: 'paused', partial_result: null }))
  await pausing
  await settled(() => !app.state.busy && !app.state.controlBusy)
  assert.equal(app.state.checkup.status, 'paused')
  assert.equal(app.state.controlLabel, '继续检索')
  assert.equal(app.state.handle.id, pending.id)
  assert.equal(app.state.checkup.partial_result.completed_categories, resumablePartial.completed_categories)
  assert.equal(app.state.progressPercent, percentage)
  assert.equal(app.state.mapReport.pois[0].poi_id, 'retained-poi', 'the saved map snapshot stays visible')
  assert.equal(client.loadPending(app.storage).status, 'paused')
  assert.equal(app.state.activeTask, true)
  assert.equal(app.state.selectionLocked, true)
  assert.equal(app.state.canSubmit, false)
  await app.state.submit()
  assert.equal(app.calls.filter(call => call.path === '/api/v1/checkups' && call.method === 'POST').length, 0)
  const pollCount = app.calls.filter(call => call.path === `/api/v1/checkups/${pending.id}`).length
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(app.calls.filter(call => call.path === `/api/v1/checkups/${pending.id}`).length, pollCount,
    'confirmed pause leaves no automatic polling request running')
  await app.state.resumeSearch()
  await settled(() => app.state.checkup?.status === 'completed' && !app.state.busy)
  assert.equal(app.state.checkup.id, pending.id)
  assert.equal(app.state.handle.id, pending.id)
  assert.deepEqual(app.calls.filter(call => call.method === 'POST').map(call => call.path),
    [`/api/v1/checkups/${pending.id}/pause`, `/api/v1/checkups/${pending.id}/resume`])
  assert.ok(app.calls.filter(call => call.path === `/api/v1/checkups/${pending.id}`).length > pollCount,
    'resume queries the original task again')
  assert.equal(client.loadPending(app.storage), null, 'only completion clears the resumable task')
})

for (const [status, action, endpoint, label] of [
  ['running', 'pauseSearch', 'pause', '暂停检索'], ['paused', 'resumeSearch', 'resume', '继续检索'],
]) {
  test(`a rejected ${endpoint} request preserves the confirmed ${status} state`, async t => {
    const snapshot = { ...resumableJob, status }
    const app = mount((url, init) => init.method === 'POST'
      ? response({ detail: 'control rejected' }, 409) : response(snapshot), { ...pending, status })
    t.after(app.stop)
    await settled(() => app.state.ready && app.state.checkup?.partial_revision === 4)
    app.state.address = '仍然不能新建'
    await app.state[action]()
    assert.equal(app.state.checkup.status, status)
    assert.equal(app.state.handle.status, status)
    assert.equal(app.state.controlLabel, label)
    assert.equal(app.state.controlBusy, false)
    assert.equal(app.state.canSubmit, false)
    assert.equal(app.state.checkup.partial_result.completed_categories, resumablePartial.completed_categories)
    assert.equal(client.loadPending(app.storage).status, status)
    assert.ok(app.state.error, 'a rejected control request is reported instead of claiming success')
    assert.deepEqual(app.calls.filter(call => call.method === 'POST').map(call => call.path),
      [`/api/v1/checkups/${pending.id}/${endpoint}`])
  })
}

test('choosing another score card switches directly without an outside click', async t => {
 const app=mount(()=>response(completed));t.after(app.stop)
 await settled(()=>app.state.checkup?.status==='completed'&&!app.state.busy)
 const [first,second,third]=catalog.groups
 for(const group of [first,second,third,first]) {
   app.state.toggleDisplayGroup(group.id)
   assert.equal(app.state.groupFilter,group.id)
   assert.equal(app.state.selectedGroupDetails,group.id)
   assert.equal(app.state.dockExpanded,true)
 }
 assert.equal(app.calls.filter(c=>c.method==='POST').length,0)
})

for (const status of ['running','paused']) {
 test(`terminating a ${status} task preserves partial results and unlocks new analysis`, async t => {
  let releaseCancel
  const cancelReply=new Promise(resolve=>{releaseCancel=resolve})
  const app=mount((url)=>url.pathname.endsWith('/cancel')?cancelReply:response({...resumableJob,status}),{...pending,status})
  t.after(app.stop)
  await settled(()=>app.state.ready&&app.state.checkup?.partial_revision===4)
  app.state.address='下一处地点'
  const cancelling=app.state.cancelSearch()
  await settled(()=>app.calls.some(c=>c.path.endsWith('/cancel')))
  assert.equal(app.state.checkup.status,status,'cancellation must be confirmed by the server')
  assert.equal(app.state.canSubmit,false)
  await app.state.cancelSearch()
  assert.equal(app.calls.filter(c=>c.path.endsWith('/cancel')).length,1)
  releaseCancel(response({...resumableJob,status:'cancelled',stage:'cancelled',partial_result:null}))
  await cancelling
  assert.equal(app.state.checkup.status,'cancelled')
  assert.equal(app.state.statusLabel,'已终止检索')
  assert.equal(app.state.checkup.partial_result.completed_categories,resumablePartial.completed_categories)
  assert.equal(app.state.mapReport.pois[0].poi_id,'retained-poi')
  assert.equal(app.state.activeTask,false)
  assert.equal(app.state.busy,false)
  assert.equal(app.state.canSubmit,true)
  assert.equal(client.loadPending(app.storage),null)
  await app.state.resumeSearch()
  assert.equal(app.calls.filter(c=>c.path.endsWith('/resume')).length,0)
 })
}

test('terminating during submission waits for the accepted id and cancels that task', async t => {
 let accept
 const submittingReply=new Promise(resolve=>{accept=resolve})
 const app=mount((url,init)=>{
  if(url.pathname==='/api/v1/checkups'&&init.method==='POST')return submittingReply
  if(url.pathname.endsWith('/cancel'))return response({...resumableJob,status:'cancelled',stage:'cancelled'})
  assert.fail('No polling or unrelated cancellation is needed')
 },null)
 t.after(app.stop)
 await settled(()=>app.state.ready)
 app.state.address='离线测试地点'
 const submission=app.state.submit()
 await settled(()=>app.state.submitting)
 await app.state.cancelSearch()
 assert.equal(app.state.cancelRequested,true)
 assert.equal(app.calls.filter(c=>c.path.endsWith('/cancel')).length,0)
 accept(response(queued,202))
 await submission
 assert.equal(app.state.checkup.status,'cancelled')
 assert.equal(app.state.canSubmit,true)
 assert.deepEqual(app.calls.filter(c=>c.method==='POST').map(c=>c.path),['/api/v1/checkups',`/api/v1/checkups/${pending.id}/cancel`])
})


test('map clicks preserve a blind dropdown group when no score details are open', async t => {
 const app=mount(()=>response(completed)); t.after(app.stop)
 await settled(()=>app.state.checkup?.status==='completed'&&!app.state.busy)
 const id=catalog.groups[0].id
 app.state.groupFilter=id
 app.state.dockHovered=true
 const listener=app.listeners.find(item=>item.type==='pointerdown')
 listener.handler({target:{closest:()=>null}})
 assert.equal(app.state.groupFilter,id,'a map/control click does not erase a dropdown filter')
 assert.equal(app.state.dockExpanded,false)
 let toggles=0
 app.state.roadMap={toggleLayers:()=>toggles++}
 listener.handler({target:{closest:()=>null}})
 app.state.toggleBlindLayers()
 assert.equal(app.state.groupFilter,id,'opening the blind panel preserves the dropdown group')
 assert.equal(toggles,1,'the score action reaches the existing map panel')
 assert.match(descriptor.template.content, /ref="roadMap"/)
 assert.match(descriptor.template.content, /class="score-blind-toggle"[^>]*@click="toggleBlindLayers"/)
})

test('the updated Tongji source keeps the report workflow enabled while other cases stay excluded',async t=>{
 const app=mount(()=>response(completed));t.after(app.stop)
 await settled(()=>app.state.checkup?.status==='completed'&&!app.state.busy)
 const replaceId=id=>{app.state.checkup={...app.state.checkup,result:{...app.state.checkup.result,report_id:id}}}
 replaceId('tongji-all7-small-updated-20261027')
 assert.equal(app.state.canGenerateReport,true)
 replaceId('tongji-all7-repaired-20261026')
 assert.equal(app.state.canGenerateReport,true)
 replaceId('another-city')
 assert.equal(app.state.canGenerateReport,false)
})
