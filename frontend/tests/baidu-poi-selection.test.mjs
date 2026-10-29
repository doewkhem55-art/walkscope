import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as checkupMap from '../src/checkupMap.ts'
import * as mapDots from '../src/mapDots.ts'
import * as dotInteraction from '../src/mapDotInteraction.ts'
import * as mapStyle from '../src/mapStyle.ts'
import * as baidu from '../src/baidu.ts'
import * as offlineProjection from '../src/offlineProjection.ts'

const source = readFileSync(new URL('../src/BaiduRoadMap.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'baidu-poi-selection' }).content.replaceAll('import.meta.env', '({})')
const code = ts.transpileModule(compiled, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

// Real component state and watchers, with no DOM or remote SDK initialization.
// Removing the POI emission or forgetting a selection invalidation breaks this check.
test('map selection publishes the POI and clears focus when the detail disappears', async t => {
  const exports = {}
  vm.runInNewContext(code, {
    exports, require: name => ({ vue: Vue, './checkupMap.ts': checkupMap,
      './mapDots.ts': mapDots, './mapDotInteraction.ts': dotInteraction, './mapStyle.ts': mapStyle, './baidu.ts': baidu, './offlineProjection.ts': offlineProjection }[name]),
    setTimeout, clearTimeout, console,
  })
  const report = (report_id = 'report-one') => ({ report_id, center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' },
    pois: [{ poi_id: 'school-one', name: '测试小学', facility_id: 'school', group_id: 'education',
      location: { lng: 121.501, lat: 31.201, crs: 'BD09LL' }, in_isochrone: true }],
    categories: [], groups: [{ id: 'education' }], routes: [], isochrone: {}, quality: {} })
  const props = Vue.reactive({ center: report().center, caption: '离线测试', report: report(), groupFilter: 'all' })
  const events = []
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node,
    insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
    patchProp() {}, parentNode: () => null, nextSibling: () => null })
  let instance
  const component = { ...exports.default, render: () => null }
  const app = renderer.createApp({ render: () => Vue.h(component,
    { ...props, ref: value => { instance = value }, onPoiSelect: id => events.push(id) }) })
  app.mount(node())
  t.after(() => app.unmount())
  const select = async () => { instance.$.setupState.selectedPoiId = 'school-one'; await Vue.nextTick() }

  await select()
  assert.deepEqual(events, ['school-one'], 'a selected facility must reach the report question host')
  instance.$.setupState.selectedPoiId = ''
  await Vue.nextTick()
  assert.equal(events.at(-1), null, 'closing details clears question focus')
  await select()
  props.groupFilter = 'health'
  await Vue.nextTick()
  assert.equal(events.at(-1), null, 'filtering out the facility clears question focus')
  props.groupFilter = 'all'
  await Vue.nextTick()
  await select()
  instance.$.setupState.showFacilities = false
  await Vue.nextTick()
  assert.equal(events.at(-1), null, 'hiding facilities clears question focus')
  instance.$.setupState.showFacilities = true
  await Vue.nextTick()
  await select()
  props.report = report('report-two')
  await Vue.nextTick()
  assert.equal(events.at(-1), null, 'switching reports clears question focus even for a repeated POI ID')
})

test('the single blind selector filters POIs while preserving analysis, camera and partial snapshots', async t => {
  const exports = {}
  vm.runInNewContext(code, {
    exports, require: name => ({ vue: Vue, './checkupMap.ts': checkupMap,
      './mapDots.ts': mapDots, './mapDotInteraction.ts': dotInteraction, './mapStyle.ts': mapStyle, './baidu.ts': baidu, './offlineProjection.ts': offlineProjection }[name]),
    setTimeout, clearTimeout, console,
  })
  const groups = [{ id: 'education', name_zh: '教育' }, { id: 'health', name_zh: '医疗' }]
  const facilities = [{ id: 'school', name_zh: '小学', group_id: 'education' },
    { id: 'clinic', name_zh: '诊所', group_id: 'health' }]
  const report = (report_id = 'report-one') => ({ report_id, center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' }, groups,
    categories: facilities.map(item => ({ facility_id: item.id, name_zh: item.name_zh, group_id: item.group_id })),
    pois: facilities.map((item, index) => ({ poi_id: `poi-${item.id}`, name: item.name_zh, facility_id: item.id,
      location: { lng: 121.501 + index * .001, lat: 31.201, crs: 'BD09LL' }, in_isochrone: true })),
    routes: [], isochrone: {}, quality: {} })
  const props = Vue.reactive({ center: report().center, caption: '离线测试', report: report(), groups, facilities, groupFilter: 'all' })
  const events = []
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node,
    insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
    patchProp() {}, parentNode: () => null, nextSibling: () => null })
  let instance
  const component = { ...exports.default, render: () => null }
  const app = renderer.createApp({ render: () => Vue.h(component, {
    ...props, ref: value => { instance = value },
    'onUpdate:groupFilter': id => { events.push(id); props.groupFilter = id },
  }) })
  app.mount(node())
  t.after(() => app.unmount())
  await Vue.nextTick()
  assert.equal(props.groupFilter, 'all')
  assert.deepEqual(events, [], 'initially all facilities remain visible')
  assert.equal(instance.$.setupState.selectedCoverageGroupId, '')
  instance.$.setupState.state = 'offline'
  assert.equal(instance.$.setupState.showCoverageCandidates, false)
  props.report.pois.push({ ...props.report.pois[0], poi_id: 'outside-school', in_isochrone: false })
  assert.equal(instance.$.setupState.visibleReportPois.length, 2, 'outside candidates start hidden')
  assert.equal(props.report.pois.length, 3, 'map visibility never removes analysis data')
  instance.$.setupState.showCoverageCandidates = true
  assert.equal(instance.$.setupState.visibleReportPois.length, 3, 'the layer switch reveals saved candidates')
  instance.$.setupState.showCoverageCandidates = false
  instance.$.setupState.changeCoverageGroup({ target: { value: 'health' } })
  await Vue.nextTick()
  assert.deepEqual(events, ['health'])
  assert.equal(props.groupFilter, 'health')
  const state = instance.$.setupState
  assert.equal(state.selectedCoverageGroupId, 'health')
  assert.deepEqual(Array.from(state.visibleReportPois, poi => poi.id), ['poi-clinic'])
  assert.equal(props.report.pois.length, 3, 'the source analysis data is untouched')
  assert.equal(facilities.length, 2)
  state.changeCoverageGroup({ target: { value: 'total' } })
  await Vue.nextTick()
  assert.equal(props.groupFilter, 'all')
  assert.equal(state.selectedCoverageGroupId, 'total', 'the emitted all filter must not erase total selection')
  assert.equal(state.visibleReportPois.length, 2)
  state.changeCoverageGroup({ target: { value: 'education' } })
  await Vue.nextTick()
  state.selectedPoiId = 'poi-school'
  state.offlineView = { zoom: 2, panX: 25, panY: 10 }
  state.visibleTierIds = ['5']
  state.showTierOutlines = true
  props.report = { ...report(), pois: [...report().pois, { ...report().pois[0], poi_id: 'poi-second-school' }] }
  await Vue.nextTick()
  assert.equal(state.selectedPoiId, 'poi-school')
  assert.equal(state.offlineView.zoom, 2)
  assert.equal(state.offlineView.panX, 25)
  assert.deepEqual([...state.visibleTierIds], ['5'])
  assert.equal(state.showTierOutlines, true)
  assert.equal(state.selectedCoverageGroupId, 'education')
  assert.equal(state.visibleReportPois.length, 2, 'the partial snapshot preserves education selection')
  state.changeCoverageGroup({ target: { value: 'health' } })
  await Vue.nextTick()
  assert.equal(state.selectedPoiId, '', 'hiding the selected POI closes its details')
  state.changeCoverageGroup({ target: { value: '' } })
  await Vue.nextTick()
  assert.equal(props.groupFilter, 'all')
  assert.equal(state.visibleReportPois.length, 3, 'closing blinds restores all in-circle POIs')
  state.changeCoverageGroup({ target: { value: 'health' } })
  await Vue.nextTick()
  props.report = report('report-two')
  await Vue.nextTick()
  assert.equal(state.selectedCoverageGroupId, '')
  assert.equal(state.visibleReportPois.length, 2, 'a new report restores every POI group')
  assert.equal(props.groupFilter, 'all')
  state.offlineView = { zoom: 2, panX: 25, panY: 10 }

  let prevented = 0
  let stopped = 0
  const wheel = (deltaY, ctrlKey = false, onControl = false) => ({ deltaY, ctrlKey,
    target: { closest: () => onControl ? {} : null },
    preventDefault() { prevented++ }, stopPropagation() { stopped++ } })
  state.wheelMap(wheel(-45, true))
  assert.equal(state.offlineView.zoom, 2.5, 'trackpad pinch changes the map camera')
  assert.equal(prevented, 1)
  assert.equal(stopped, 1)
  state.wheelMap(wheel(80, false, true))
  assert.equal(prevented, 1, 'scrolling layer controls remains native')
  assert.equal(state.offlineView.zoom, 2.5)

  const touch = distance => ({ target: { closest: () => null },
    touches: { length: 2, item: index => ({ clientX: index * distance, clientY: 0 }) },
    preventDefault() {}, stopPropagation() {} })
  state.startPinch(touch(100))
  state.movePinch(touch(140))
  assert.equal(state.offlineView.zoom, 3.125, 'two-finger touch uses the same map-only camera path')
  assert.match(state.scaleBar.label, /米|公里/)
  assert.ok(state.scaleBar.widthPx <= 100)

  const sized = mapDots.buildDotPlan({ isochrone: { geometry: { type: 'Polygon', coordinates: [[[121.49,31.19],
    [121.51,31.19], [121.51,31.21], [121.49,31.21], [121.49,31.19]]] } } })
  props.report = { ...props.report, isochrone: { geometry: { type: 'Polygon', coordinates: [[[121.49,31.19],
    [121.51,31.19], [121.51,31.21], [121.49,31.21], [121.49,31.19]]] } } }
  await Vue.nextTick()
  state.offlineView = { zoom: 1, panX: 0, panY: 0 }
  const firstRadius = state.projectedDotRadius
  state.changeZoom(1)
  assert.ok(state.projectedDotRadius > firstRadius, 'zooming the geographic grid grows every dot together')
  assert.equal(new Set(state.visibleDots.map(dot => dot.r)).size, 1, 'every dot shares the same projected radius')
  assert.ok(state.visibleDots.length <= sized.points.length)

})


test('a new report fits its first valid contour once while partial geometry updates preserve the camera', async t => {
  const mounted = []
  const exports = {}
  const views = []
  const handlers = {}
  const sdk = { Point: class { constructor(lng, lat) { this.lng = lng; this.lat = lat } },
    Map: class {
      centerAndZoom() {} setViewport(points) { views.push(points) }
      pointToPixel() { return { x: 100, y: 100 } }
      addEventListener(name, callback) { handlers[name] = callback } removeEventListener() {} destroy() {} checkResize() {}
    } }
  const sdkCode = ts.transpileModule(compiled.replaceAll('({})', '({VITE_BAIDU_MAP_AK:"fakeBrowserKeyForOfflineTest"})'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  vm.runInNewContext(sdkCode, { exports,
    require: name => ({ vue: { ...Vue, onMounted: callback => mounted.push(callback) },
      './checkupMap.ts': checkupMap, './mapDots.ts': mapDots, './mapDotInteraction.ts': dotInteraction, './mapStyle.ts': mapStyle,
      './baidu.ts': { ...baidu, loadBaiduMap: async () => sdk }, './offlineProjection.ts': offlineProjection }[name]),
    ResizeObserver: class { observe() {} disconnect() {} }, setTimeout, clearTimeout, console })
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const geometry = size => ({ type: 'Polygon', coordinates: [[[121.5, 31.2], [121.5 + size, 31.2],
    [121.5 + size, 31.2 + size], [121.5, 31.2 + size], [121.5, 31.2]]] })
  const report = (id, shape) => ({ report_id: id, center, groups: [], categories: [], pois: [], routes: [],
    isochrone: { geometry: shape }, quality: {} })
  const props = Vue.reactive({ center, caption: '', report: report('one', null) })
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node,
    insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
    patchProp() {}, parentNode: () => null, nextSibling: () => null })
  let instance
  const component = { ...exports.default, render: () => null }
  const app = renderer.createApp({ render: () => Vue.h(component, { ...props, ref: value => { instance = value } }) })
  app.mount(node())
  t.after(() => app.unmount())
  instance.$.setupState.host = { clientWidth: 1000, clientHeight: 700 }
  await mounted[0]()
  assert.equal(views.length, 0, 'empty partial geometry does not fit')
  props.report = report('one', geometry(.01))
  await Vue.nextTick()
  assert.equal(views.length, 0, 'usable geometry before tilesloaded must not mark a fit complete')
  handlers.tilesloaded()
  assert.equal(views.length, 1, 'the first tilesloaded event fits usable geometry once')
  handlers.tilesloaded()
  assert.equal(views.length, 1, 'later tilesloaded events never reset the camera')
  props.report = report('one', geometry(.02))
  await Vue.nextTick()
  assert.equal(views.length, 1, 'later geometry for the same run preserves the camera')
  props.report = report('two', geometry(.03))
  await Vue.nextTick()
  assert.equal(views.length, 2, 'a new report gets its own initial fit')
})


test('boundary dots stay complete circles, use one center-assigned band, and never add centers in unknown holes', () => {
  const ring = (low, high) => [[low,low],[high,low],[high,high],[low,high],[low,low]]
  const geometry = (...rings) => ({ type: 'Polygon', coordinates: rings })
  const sample = { isochrone: { geometry: geometry(ring(1,1.02)) }, tiers: [
    { max_seconds: 300, geometry: geometry(ring(1.008,1.012)) },
    { max_seconds: 600, geometry: geometry(ring(1.004,1.016)) },
    { max_seconds: 900, geometry: geometry(ring(1,1.02), ring(1.001,1.002)) },
  ] }
  const plan = mapDots.buildDotPlan(sample)
  const edge = plan.points.find(point => (point.lng-1)*111320 < plan.spacingM*.48)
  assert.ok(edge, 'the shared lattice contains a center near the real outer boundary')
  assert.equal(edge.tier, '15')
  assert.equal(mapDots.tierAtPoint(plan.tiers, edge), '15', 'a boundary center has exactly one time-band assignment')
  assert.equal(mapDots.dotStyleFor(edge).fill, '#2e5f8a')
  assert.equal(new Set(plan.points.map(point => `${point.lng}:${point.lat}`)).size, plan.points.length)
  assert.ok(plan.points.every(point => !(point.lng>1.001 && point.lng<1.002 && point.lat>1.001 && point.lat<1.002)), 'unknown-hole centers remain absent')
  assert.match(descriptor.template.content, /<circle v-for="dot in visibleDots"[^>]*:r="dot.r"/, 'the map renders full SVG circles')
  assert.doesNotMatch(descriptor.template.content, /<clipPath|clip-path=/, 'neither time-band nor outer geometry cuts the circles into partial arcs')
})

function mountMapForVisualCheck(t, props, globals = {}) {
  const exports = {}
  vm.runInNewContext(code, { exports, require: name => ({ vue: Vue, './checkupMap.ts': checkupMap,
    './mapDots.ts': mapDots, './mapDotInteraction.ts': dotInteraction, './mapStyle.ts': mapStyle,
    './baidu.ts': baidu, './offlineProjection.ts': offlineProjection }[name]), setTimeout, clearTimeout, console, ...globals })
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node,
    insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
    patchProp() {}, parentNode: () => null, nextSibling: () => null })
  let instance
  const component = { ...exports.default, render: () => null }
  const app = renderer.createApp({ render: () => Vue.h(component, { ...props, ref: value => { instance = value } }) })
  app.mount(node())
  t.after(() => app.unmount())
  return { state: instance.$.setupState, exposed: instance.$.exposed, unmount: () => app.unmount() }
}

test('blind selector uses confirmed-all aggregates only and keeps unknown separate', async t => {
  const rectangle = (left, bottom, right, top) => ({ type: 'Polygon', coordinates: [[[left,bottom], [right,bottom],
    [right,top], [left,top], [left,bottom]]] })
  const feature = (facility_type, geometry) => ({ type: 'Feature', properties: { facility_type }, geometry })
  const collection = (...features) => ({ type: 'FeatureCollection', features })
  const geometry = rectangle(121.49,31.19,121.51,31.21)
  const aggregate = (id, blind, unknown, blind_ratio, unknown_ratio) => ({
    definition: 'all_categories_confirmed_blind', facility_ids: ['school', 'nursery'],
    blind_spots: collection(...(blind ? [feature(id, geometry)] : [])),
    unknown_areas: collection(...(unknown ? [feature(id, geometry)] : [])),
    covered_ratio: 1-blind_ratio-unknown_ratio, blind_ratio, unknown_ratio,
  })
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const groups = ['education', 'health', 'care', 'daily', 'culture', 'transport', 'community'].map(id => ({ id, name_zh: id }))
  const props = Vue.reactive({ center, caption: '', groups, groupFilter: 'all', report: { report_id: 'visual-blind', center, groups, pois: [], routes: [],
    isochrone: { geometry }, quality: { grid_size_m: 200 }, categories: [
      { facility_id: 'school', group_id: 'education', score: 80, blind_ratio: .4, unknown_ratio: .1 },
      { facility_id: 'nursery', group_id: 'education', blind_ratio: .3, unknown_ratio: 0 },
    ], blindspot: {
      blind_spots: collection(feature('school', geometry), feature('nursery', geometry)),
      unknown_areas: collection(feature('school', geometry)),
      group_coverage: { education: aggregate('education', true, true, .2, .1),
        health: aggregate('health', false, false, 0, 0), daily: aggregate('daily', false, true, 0, 1) },
      total_coverage: aggregate('total', false, true, 0, .9),
    } } })
  const { state, exposed } = mountMapForVisualCheck(t, props)
  await Vue.nextTick()
  assert.equal(state.selectedCoverageGroupId, '')
  assert.equal(state.visibleBlindSpots.length, 0)
  assert.equal(state.groupsWithColors.length, 7)
  assert.equal(state.coverageGridLabel, '200米网格估算 · 非道路边界')
  const originalDots = JSON.stringify(state.visibleDots)
  state.changeCoverageGroup({ target: { value: 'education' } })
  await Vue.nextTick()
  assert.deepEqual(Array.from(state.visibleBlindSpots, feature => feature.facilityId), ['education'], 'never union child outlines')
  assert.equal(state.visibleUnknownAreas.length, 1)
  assert.equal(JSON.stringify(state.visibleDots), originalDots)
  assert.equal(props.report.categories[0].score, 80)
  assert.match(state.coverageStatus, /全部小项.*确认无服务/)
  assert.match(state.coverageStatus, /未知/)
  state.changeCoverageGroup({ target: { value: 'total' } })
  assert.equal(state.visibleBlindSpots.length, 0, 'total does not reuse any child or group blind polygon')
  assert.deepEqual(Array.from(state.visibleUnknownAreas, feature => feature.facilityId), ['total'])
  assert.match(state.coverageStatus, /暂无已确认盲区.*全部.*确认无服务.*不代表.*全部覆盖/)
  state.changeCoverageGroup({ target: { value: 'health' } })
  assert.equal(state.visibleUnknownAreas.length, 0)
  state.changeCoverageGroup({ target: { value: 'care' } })
  assert.equal(state.visibleBlindSpots.length, 0)
  assert.match(state.coverageStatus, /暂无.*汇总.*无法判断/)
  delete props.report.blindspot.group_coverage.education
  state.changeCoverageGroup({ target: { value: 'education' } })
  assert.equal(state.visibleBlindSpots.length, 0, 'legacy child data cannot substitute for a missing aggregate')
  props.report.blindspot.group_coverage.education = { ...aggregate('education', true, false, 1, 0), definition: 'any_category_blind' }
  assert.equal(state.visibleBlindSpots.length, 0, 'incompatible aggregate definitions are not rendered')
  state.changeCoverageGroup({ target: { value: '' } })
  assert.equal(state.visibleBlindSpots.length, 0)
  assert.match(state.coverageStatus, /盲区已关闭/)
  exposed.toggleLayers()
  assert.equal(state.layersOpen, true, 'the score-panel control opens the existing map layer panel')
  exposed.toggleLayers()
  assert.equal(state.layersOpen, false)
  assert.doesNotMatch(descriptor.template.content, /设施筛选 · 图例|map-layer-toggle/, 'redundant group checkboxes and standalone blind button are removed')
  assert.doesNotMatch(descriptor.template.content, /class="coverage-tool"|quick-coverage/, 'the duplicate blind popover is gone')
  assert.match(descriptor.template.content, /aria-label="收起地图图层"/)
  assert.match(source, /\.layer-heading \{[^}]*position: sticky;[^}]*top: 0;/, 'close header remains reachable while scrolling')
  assert.match(descriptor.template.content, /<ResizeGrip label="地图图层"[^>]*drag-handle="\.layer-heading"/)
  assert.match(descriptor.template.content, /<ResizeGrip label="设施详情"[^>]*:disabled="poiDetailsMaximized"/)
  assert.doesNotMatch(source, /resize: both/, 'edge handles own resizing instead of a hidden native corner')
  assert.doesNotMatch(descriptor.template.content, /class="blind-flow"|class="blind-outline"|class="coverage-unknown"/, 'no running light bands or blind boundary paths are added')
  assert.doesNotMatch(descriptor.template.content, /<pattern|blindPatternId|unknownPatternId|dot.coverage/, 'the area treatment never mutates individual dots')
})

test('a synchronized overlay highlights original circle dots only, excluding unknown and polygon holes', async t => {
  const ring = (left, bottom, right, top) => [[left,bottom], [right,bottom], [right,top], [left,top], [left,bottom]]
  const geometry = (...rings) => ({ type: 'Polygon', coordinates: rings })
  const collection = (id, shape) => ({ type: 'FeatureCollection', features: shape ? [{
    type: 'Feature', properties: { facility_type: id }, geometry: shape }] : [] })
  const aggregate = (id, blind, unknown = null) => ({ definition: 'all_categories_confirmed_blind', facility_ids: ['school'],
    blind_spots: collection(id, blind), unknown_areas: collection(id, unknown), blind_ratio: blind ? .3 : 0, unknown_ratio: unknown ? .1 : 0 })
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const groups = [{ id: 'education', name_zh: '教育' }, { id: 'health', name_zh: '医疗' }]
  const report = { report_id: 'dot-glow', center, groups, pois: [], routes: [], categories: [], quality: {},
    isochrone: { geometry: geometry(ring(121.49,31.19,121.51,31.21)) }, blindspot: {
      blind_spots: collection('school', geometry(ring(121.49,31.19,121.51,31.21))),
      group_coverage: { education: aggregate('education', geometry(ring(121.49,31.19,121.5,31.21), ring(121.492,31.192,121.495,31.196)),
          geometry(ring(121.49,31.2,121.5,31.21))),
        health: aggregate('health', geometry(ring(121.5,31.19,121.51,31.21))) },
      total_coverage: aggregate('total', null, geometry(ring(121.49,31.19,121.51,31.21))),
    } }
  const props = Vue.reactive({ center, caption: '', groups, report, groupFilter: 'all' })
  const { state } = mountMapForVisualCheck(t, props)
  await Vue.nextTick()
  state.state = 'offline'
  const originalDots = state.visibleDots
  const originalPlan = state.dotPlan
  const key = point => `${point.tier}:${point.lng.toFixed(6)}:${point.lat.toFixed(6)}`
  const pick = predicate => {
    const point = state.dotPlan.points.find(predicate)
    assert.ok(point)
    return key(point)
  }
  const westBlind = pick(p => p.lng > 121.496 && p.lng < 121.499 && p.lat > 31.197 && p.lat < 31.199)
  const westUnknown = pick(p => p.lng < 121.499 && p.lat > 31.202)
  const westHole = pick(p => p.lng > 121.492 && p.lng < 121.495 && p.lat > 31.192 && p.lat < 31.196)
  const eastBlind = pick(p => p.lng > 121.502 && p.lat < 31.198)
  assert.equal(state.highlightedBlindDots.length, 0)
  state.changeCoverageGroup({ target: { value: 'education' } })
  assert.ok(state.blindDotIds.has(westBlind))
  assert.equal(state.blindDotIds.has(westUnknown), false)
  assert.equal(state.blindDotIds.has(westHole), false)
  assert.equal(state.blindDotIds.has(eastBlind), false)
  assert.ok(state.highlightedBlindDots.length > 0)
  const originalById = new Map(originalDots.map(dot => [dot.id, dot]))
  assert.ok(state.highlightedBlindDots.every(dot => originalById.get(dot.id) === dot), 'highlight circles reuse original visible dot coordinates and radii')
  assert.ok(state.dotPlan.points.filter(point => state.blindDotIds.has(key(point))).every(point => mapDots.pointInGeometry(state.reportGeometry, point)), 'every highlighted center is inside the actual life circle')
  assert.equal(state.visibleDots, originalDots, 'coverage selection does not rebuild the projected matrix')
  assert.equal(state.dotPlan, originalPlan, 'grid geometry, spacing and time bands stay unchanged')
  const cached = state.blindDotIds
  const beforeProjection = state.highlightedBlindDots[0]
  state.offlineView = { zoom: 2, panX: 10, panY: 8 }
  assert.equal(state.blindDotIds, cached, 'camera movement does not change geographical membership')
  assert.notEqual(state.highlightedBlindDots[0], beforeProjection, 'the overlay follows camera projection')
  assert.ok(state.highlightedBlindDots.every(dot => state.visibleDots.includes(dot)))
  state.changeCoverageGroup({ target: { value: 'health' } })
  assert.ok(state.blindDotIds.has(eastBlind))
  assert.equal(state.blindDotIds.has(westBlind), false)
  state.changeCoverageGroup({ target: { value: 'total' } })
  assert.equal(state.highlightedBlindDots.length, 0, 'an all-unknown legacy total does not invent a blind area')
  delete props.report.blindspot.group_coverage.education
  state.changeCoverageGroup({ target: { value: 'education' } })
  assert.equal(state.highlightedBlindDots.length, 0, 'legacy category polygons never become highlight fallback')
  state.changeCoverageGroup({ target: { value: '' } })
  assert.equal(state.highlightedBlindDots.length, 0)
  assert.match(descriptor.template.content, /<g v-if="highlightedBlindDots.length" class="blind-dots-glow"/)
  assert.match(descriptor.template.content, /<circle v-for="dot in highlightedBlindDots"[^>]*:cx="dot.x" :cy="dot.y" :r="dot.r"/)
  assert.ok(descriptor.template.content.indexOf('class="blind-dots-glow"') > descriptor.template.content.indexOf('ref="dotLayer"'), 'the original static dots anchor the overlay')
  assert.ok(descriptor.template.content.indexOf('class="blind-dots-glow"') < descriptor.template.content.indexOf('<TransitionGroup name="poi-filter"'), 'POIs stay above the overlay')
  assert.doesNotMatch(source, /blind-area|blindAreaPaths|unknownAreaPaths|is-confirmed-blind/, 'no filled area, rectangular grid or per-point animation remains')
  assert.match(source, /\.blind-dots-glow \{[^}]*animation: blind-dots-breathe 3s ease-in-out infinite;/)
  assert.match(source, /prefers-reduced-motion: reduce[^}]*\.blind-dots-glow \{ animation: none;/)
  const keyframes = source.match(/@keyframes blind-dots-breathe \{.*$/m)?.[0]
  assert.ok(keyframes)
  assert.match(keyframes, /opacity: \.48; transform: translateY\(0\).*opacity: \.84; transform: translateY\(-2\.5px\)/, 'the overlay brightens and gently rises together while the original dots stay fixed')
  assert.doesNotMatch(keyframes, /scale|stroke-dashoffset/, 'the region never grows or runs a light band')
})

test('explicit include-unknown display highlights the merged area without changing raw evidence or legacy behavior', async t => {
  const rectangle = (left, bottom, right, top) => ({ type: 'Polygon', coordinates: [[[left,bottom], [right,bottom],
    [right,top], [left,top], [left,bottom]]] })
  const collection = (id, geometry) => ({ type: 'FeatureCollection', features: geometry ? [{
    type: 'Feature', properties: { facility_type: id }, geometry }] : [] })
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const groups = [{ id: 'education', name_zh: '教育' }]
  const geometry = rectangle(121.49,31.19,121.51,31.21)
  const aggregate = id => ({ definition: 'all_categories_confirmed_blind', facility_ids: ['school'],
    blind_spots: collection(id, rectangle(121.49,31.19,121.5,31.2)),
    unknown_areas: collection(id, rectangle(121.49,31.2,121.5,31.21)),
    covered_ratio: .4, blind_ratio: .3, unknown_ratio: .3,
    display_policy: 'include_unknown', display_blind_ratio: .6,
    display_blind_spots: collection(id, rectangle(121.49,31.19,121.5,31.21)) })
  const props = Vue.reactive({ center, caption: '', groups, groupFilter: 'all', report: {
    report_id: 'include-unknown', center, groups, pois: [], routes: [], categories: [], quality: {},
    isochrone: { geometry }, blindspot: { group_coverage: { education: aggregate('education') },
      total_coverage: aggregate('total') } } })
  const rawEvidence = JSON.stringify(props.report.blindspot)
  const { state } = mountMapForVisualCheck(t, props)
  await Vue.nextTick()
  state.state = 'offline'
  const originalPlan = state.dotPlan
  const originalDots = state.visibleDots
  const sample = predicate => {
    const point = state.dotPlan.points.find(predicate)
    assert.ok(point)
    return point
  }
  const areaAt = point => mapDots.coverageAtPoint(point, state.visibleBlindSpots.map(feature => feature.geometry),
    state.visibleUnknownAreas.map(feature => feature.geometry))
  const confirmedBlind = sample(p => p.lng < 121.499 && p.lat < 31.198)
  const unverified = sample(p => p.lng < 121.499 && p.lat > 31.202)
  const covered = sample(p => p.lng > 121.502)
  state.changeCoverageGroup({ target: { value: 'education' } })
  assert.equal(areaAt(confirmedBlind), 'blind')
  assert.equal(areaAt(unverified), 'blind', 'raw unknown geometry no longer excludes the merged display area')
  assert.notEqual(areaAt(covered), 'blind')
  const key = point => `${point.tier}:${point.lng.toFixed(6)}:${point.lat.toFixed(6)}`
  assert.ok(state.blindDotIds.has(key(confirmedBlind)))
  assert.ok(state.blindDotIds.has(key(unverified)), 'the overlay includes formerly unknown centers under the explicit display policy')
  assert.equal(state.blindDotIds.has(key(covered)), false)
  assert.equal(state.visibleUnknownAreas.length, 0)
  assert.equal(state.selectedCoverage.unknown_areas.features.length, 1, 'unknown evidence remains available')
  assert.equal(state.selectedCoverage.unknown_ratio, .3)
  assert.equal(state.selectedCoverage.blind_ratio, .3)
  assert.match(state.coverageStatus, /盲区（含未核验区域）.*60\.0%/)
  assert.match(state.coverageDescription, /未核验区域不表示已确认无服务/)
  assert.equal(state.dotPlan, originalPlan)
  assert.equal(state.visibleDots, originalDots, 'the existing map matrix and visual style remain unchanged')
  state.changeCoverageGroup({ target: { value: 'total' } })
  assert.deepEqual(Array.from(state.visibleBlindSpots, feature => feature.facilityId), ['total'])
  assert.equal(areaAt(unverified), 'blind')
  assert.ok(state.blindDotIds.has(key(unverified)))
  assert.equal(JSON.stringify(props.report.blindspot), rawEvidence, 'view selection cannot mutate report evidence')
  props.report.quality.coverage_validation = {unverified_poi_count:1,groups:{education:{unverified_poi_count:1}}}
  state.changeCoverageGroup({target:{value:'education'}})
  assert.equal(state.visibleBlindSpots.length,0,'an unverified inside POI blocks this group from presenting a blind overlay')
  assert.equal(state.blindDotIds.size,0)
  assert.match(state.coverageStatus,/未完成.*核验|未核验/)
  delete props.report.quality.coverage_validation
  state.changeCoverageGroup({target:{value:'total'}})
  delete props.report.blindspot.total_coverage.display_policy
  assert.notEqual(areaAt(unverified), 'blind', 'a report without the explicit policy keeps original semantics')
  assert.equal(state.blindDotIds.has(key(unverified)), false)
  assert.equal(state.visibleUnknownAreas.length, 1)
  assert.match(state.coverageStatus, /确认无服务/)
})

test('a verified area does not retain the unverified-area label after its unknown ratio reaches zero', async t => {
  const saved=JSON.parse(readFileSync(new URL('../../data/reports/tongji-all7-repaired-20261026_report.json',import.meta.url),'utf8')).result
  const groups=saved.groups
  const facilities=saved.categories.map(c=>({id:c.facility_id,group_id:c.group_id,name_zh:c.name_zh}))
  const props=Vue.reactive({center:saved.center,caption:'',groups,facilities,groupFilter:'elderly_disability',report:saved})
  const {state}=mountMapForVisualCheck(t,props)
  await Vue.nextTick()
  state.changeCoverageGroup({target:{value:'elderly_disability'}})
  assert.match(state.coverageStatus,/已核验.*42\.2%/)
  assert.doesNotMatch(state.coverageStatus,/含未核验/)
  assert.equal(state.hasUnverifiedDisplay,false)
  props.report.blindspot.group_coverage.elderly_disability.unknown_ratio=.01
  assert.equal(state.hasUnverifiedDisplay,true)
  assert.match(state.coverageStatus,/含未核验/)
})

test('one shared POI appears once in every matching dropdown group and card selections stay in sync', async t => {
  const groups = [{ id: 'education', name_zh: '教育' }, { id: 'health', name_zh: '医疗' }]
  const facilities = [{ id: 'school', group_id: 'education' }, { id: 'clinic', group_id: 'health' }]
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const pois = [
    { poi_id: 'shared', facility_id: 'school', in_isochrone: true, location: center },
    { poi_id: 'shared', facility_id: 'clinic', in_isochrone: true, location: center },
    { poi_id: 'school-only', facility_id: 'school', in_isochrone: true, location: center },
  ]
  const props = Vue.reactive({ center, caption: '', groups, facilities, groupFilter: 'all',
    report: { report_id: 'shared-poi', center, groups, pois, routes: [], categories: [], isochrone: {}, quality: {} } })
  const { state } = mountMapForVisualCheck(t, props)
  await Vue.nextTick()
  state.state = 'offline'
  state.changeCoverageGroup({ target: { value: 'health' } })
  assert.deepEqual(Array.from(state.visibleReportPois, poi => poi.id), ['shared'])
  assert.equal(state.visibleReportPois[0].memberships.length, 2)
  state.changeCoverageGroup({ target: { value: 'education' } })
  assert.deepEqual(Array.from(state.visibleReportPois, poi => poi.id), ['shared', 'school-only'])
  state.changeCoverageGroup({ target: { value: 'total' } })
  assert.deepEqual(Array.from(state.visibleReportPois, poi => poi.id), ['shared', 'school-only'])
  props.groupFilter = 'health'
  await Vue.nextTick()
  assert.equal(state.selectedCoverageGroupId, 'health', 'a score-card selection updates the same dropdown')
  assert.deepEqual(Array.from(state.visibleReportPois, poi => poi.id), ['shared'])
  props.groupFilter = 'all'
  await Vue.nextTick()
  assert.equal(state.selectedCoverageGroupId, '', 'closing score-card details restores the full POI layer')
  assert.equal(state.visibleReportPois.length, 2)
  assert.equal(props.report.pois.length, 3, 'render deduplication does not remove source category memberships')
})

test('local hover coalesces frames, leaves gestures and pins native, and stops for reduced motion, hidden pages and unmount', async t => {
  const frames = new Map(), listeners = new Map(), mediaListeners = new Map()
  let nextFrame = 0
  const page = { hidden: false, addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name) }
  const motion = { matches: false, addEventListener: (name, callback) => mediaListeners.set(name, callback),
    removeEventListener: name => mediaListeners.delete(name) }
  const center = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }
  const { state, unmount } = mountMapForVisualCheck(t, Vue.reactive({ center, caption: '' }), {
    document: page, window: { matchMedia: () => motion },
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame },
    cancelAnimationFrame: id => frames.delete(id),
  })
  await Vue.nextTick()
  const fakeCircle = (id, x, y) => {
    const values = {}
    const style = { setProperty(name, value) { values[name] = value }, removeProperty(name) { delete values[name]; if (name === 'opacity') delete this.opacity } }
    return { style, values, getAttribute: name => ({ 'data-dot-id': id, cx: String(x), cy: String(y), 'data-dot-lift': '0' }[name] ?? null) }
  }
  const near = fakeCircle('near',100,100), far = fakeCircle('far',700,500)
  state.dotLayer = { querySelectorAll: () => [near, far] }
  state.rebuildDotHoverIndex()
  const event = (pointerType = 'mouse', buttons = 0, overPin = false) => ({ pointerType, buttons, clientX: 100, clientY: 100,
    target: { closest: selector => overPin && selector === '.checkup-poi' ? {} : null },
    currentTarget: { getBoundingClientRect: () => ({ left: 0, top: 0 }) } })
  const originalDots = state.visibleDots
  const projection = state.projectionVersion
  state.hoverDots(event('touch'))
  assert.equal(frames.size, 0)
  for (let i=0; i<8; i++) state.hoverDots(event())
  assert.equal(frames.size, 1, 'pointer moves share one pending animation frame')
  const [id, callback] = [...frames][0]
  frames.delete(id)
  callback(100)
  assert.equal(near.values['--dot-hover-scale'], '0.360')
  assert.equal(near.style.opacity, undefined, 'hover only shrinks circles; it never hides or fades dots')
  assert.equal(near.values['--dot-hover-lift'], undefined, 'dot centers stay anchored')
  assert.deepEqual(far.values, {}, 'far nodes are never mutated')
  assert.equal(state.projectionVersion, projection)
  assert.equal(state.visibleDots, originalDots, 'hover never invalidates the Vue geometry/projection result')
  state.hoverDots(event('mouse',1))
  assert.deepEqual(near.values, {}, 'dragging immediately restores local hover offsets')
  state.hoverDots(event('mouse',0,true))
  assert.equal(frames.size, 0, 'facility pins keep their own click and hover target')
  assert.equal(state.poiPointer.x, 100)
  assert.equal(state.poiPointer.y, 100)
  state.hoverDots(event())
  page.hidden = true
  listeners.get('visibilitychange')()
  assert.equal(frames.size, 0, 'hidden pages cancel pending animation')
  assert.equal(state.poiPointer, null)
  state.hoverDots(event())
  assert.equal(frames.size, 0)
  page.hidden = false
  motion.matches = true
  mediaListeners.get('change')()
  state.hoverDots(event())
  assert.equal(frames.size, 0, 'reduced-motion preference disables dot shrinking')
  state.hoverDots(event('mouse',0,true))
  assert.ok(state.poiPointer, 'the POI cursor remains usable with its pulse disabled by reduced-motion CSS')
  assert.match(source, /prefers-reduced-motion: reduce[^}]*\.poi-hover-cursor > i \{ animation: none;/)
  motion.matches = false
  state.hoverDots(event())
  assert.equal(frames.size, 1)
  unmount()
  assert.equal(frames.size, 0)
  assert.equal(listeners.size, 0)
  assert.equal(mediaListeners.size, 0)
})

test('the larger theme-blue map cursor has a centered native hotspot and leaves POI hit targets intact', () => {
  const cursorRule = source.match(/\.road-map-canvas, \.road-map-canvas :deep\(\*\) \{ cursor: url\("([^"\n]+)"\) (\d+) (\d+), crosshair !important;/)
  assert.ok(cursorRule, 'the custom cursor stays scoped to the basemap canvas')
  const svg = decodeURIComponent(cursorRule[1].slice('data:image/svg+xml,'.length))
  assert.match(svg, /width='48' height='48'/)
  assert.deepEqual(cursorRule.slice(2, 4), ['24', '24'], 'the click point is the exact center')
  assert.match(svg, /stroke='#75c7f4'/, 'matches the theme blue of the analysis center')
  assert.match(svg, /M24 4V20M24 28V44M4 24H20M28 24H44/, 'four lightweight arms span forty pixels')
  assert.match(source, /\.checkup-poi \{ pointer-events: auto; cursor: crosshair;/, 'POIs keep hit targets without the hand cursor')
  assert.match(source, /\.road-map\.poi-cursor-active \.checkup-poi \{ cursor: none;/)
  assert.match(source, /\.poi-hover-cursor \{[^}]*pointer-events: none;/)
  assert.match(source, /animation: poi-cursor-pulse 1\.8s/)
  assert.doesNotMatch(descriptor.template.content, /class="cursor-overlay"/, 'no moving overlay intercepts map events')
})

test('P1 switches saved small-category evidence and POIs without changing the report or dot grid', async t => {
  const saved=JSON.parse(readFileSync(new URL('../../data/reports/tongji-all7-repaired-20261026_report.json',import.meta.url),'utf8')).result
  const before=JSON.stringify(saved)
  const facilities=saved.categories.map(c=>({id:c.facility_id,name_zh:c.name_zh,group_id:c.group_id}))
  const props=Vue.reactive({center:saved.center,caption:'',groups:saved.groups,facilities,groupFilter:'all',report:saved})
  const {state}=mountMapForVisualCheck(t,props,{fetch:()=>assert.fail('category switching must stay offline')})
  await Vue.nextTick();state.state='offline'
  const plan=state.dotPlan
  for(const [id,group,unknown] of [['primary_school','education_children',.3276418275592749],['pharmacy','health_medical',.3145371445218812],['market','daily_commerce',.31381652449043423]]) {
    state.selectCoverageFacility(id);await Vue.nextTick()
    assert.equal(state.selectedCoverageFacilityId,id)
    assert.equal(state.selectedCoverageGroupId,group)
    assert.equal(state.selectedCoverage.definition,'single_facility')
    assert.ok(Math.abs(state.selectedCoverage.unknown_ratio-unknown)<1e-6)
    assert.ok(state.visibleUnknownAreas.length>0)
    assert.ok(state.visibleUnknownAreas.every(feature=>feature.facilityId===id))
    assert.ok(state.visibleBlindSpots.every(feature=>feature.facilityId===id))
    assert.ok(state.filteredPois.every(poi=>poi.memberships.some(member=>member.facilityId===id)))
    assert.match(state.coverageStatus,/确认盲区.*未知/)
    assert.match(state.coverageDescription,/未知.*不.*确认/)
    assert.ok(state.unknownDotIds.size>0)
    assert.ok([...state.unknownDotIds].every(id=>!state.blindDotIds.has(id)))
    assert.equal(state.dotPlan,plan)
  }
  state.changeCoverageGroup({target:{value:'health_medical'}});await Vue.nextTick()
  assert.equal(state.selectedCoverageFacilityId,'')
  assert.equal(state.selectedCoverage.definition,'all_categories_confirmed_blind')
  assert.ok(state.filteredPois.some(poi=>poi.memberships.some(m=>m.facilityId==='hospital')))
  assert.equal(JSON.stringify(saved),before)
})

test('P1 summary-only categories and incomplete geometry never borrow a group or another category boundary', async t => {
  const saved=JSON.parse(readFileSync(new URL('../../data/reports/tongji-all7-repaired-20261026_report.json',import.meta.url),'utf8')).result
  const id='primary_school'
  for(const key of ['covered_areas','blind_spots','unknown_areas']) saved.blindspot[key].features=saved.blindspot[key].features.filter(f=>f.properties.facility_type!==id)
  const facilities=saved.categories.map(c=>({id:c.facility_id,name_zh:c.name_zh,group_id:c.group_id}))
  const props=Vue.reactive({center:saved.center,caption:'',groups:saved.groups,facilities,groupFilter:'all',report:saved})
  const {state}=mountMapForVisualCheck(t,props);await Vue.nextTick()
  state.selectCoverageFacility(id)
  assert.equal(state.visibleBlindSpots.length,0)
  assert.equal(state.visibleUnknownAreas.length,0)
  assert.equal(state.blindDotIds.size,0)
  assert.equal(state.unknownDotIds.size,0)
  assert.match(state.coverageStatus,/地图边界尚未具备可核验数据/)
  assert.ok(state.selectedCoverage.unknown_ratio>.32,'numeric evidence stays available')
  const selected=state.selectedCoverageFacilityId
  state.selectCoverageFacility('not-a-real-category')
  assert.equal(state.selectedCoverageFacilityId,selected)
  props.report={...saved,report_id:'another-saved-report'};await Vue.nextTick()
  assert.equal(state.selectedCoverageFacilityId,'')
  assert.equal(state.selectedCoverageGroupId,'')
})

test('P1 category with missing result is labelled unavailable and switching parent clears the child', async t=>{
  const center={lng:121.5,lat:31.2,crs:'BD09LL'}
  const groups=[{id:'education',name_zh:'教育'}]
  const facilities=[{id:'school',name_zh:'小学',group_id:'education'}]
  const props=Vue.reactive({center,caption:'',groups,facilities,groupFilter:'all',report:{report_id:'incomplete',center,groups,categories:[],pois:[],routes:[],isochrone:{},blindspot:{},quality:{}}})
  const {state}=mountMapForVisualCheck(t,props);await Vue.nextTick()
  state.selectCoverageFacility('school');await Vue.nextTick()
  assert.equal(state.selectedCoverageFacilityId,'school')
  assert.equal(state.selectedCoverage,null)
  assert.match(state.coverageStatus,/地图边界尚未具备可核验数据/)
  props.groupFilter='education';await Vue.nextTick()
  assert.equal(state.selectedCoverageFacilityId,'school','the host echo preserves child selection')
  props.groupFilter='all';await Vue.nextTick()
  assert.equal(state.selectedCoverageFacilityId,'')
})
