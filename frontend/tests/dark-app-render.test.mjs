/**
 * Offline render check for the 阶段4a shell template.
 *
 * `npm run build` proves the SFC compiles; this renders the real <script setup> + template
 * with an in-memory renderer (no DOM, no Baidu SDK, no network) so the toolbar, both
 * floating panels and the narrow-screen drawer switcher provably exist in the output.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as checkups from '../src/checkups.ts'
import * as mapHelpers from '../src/checkupMap.ts'
import * as panelLayout from '../src/panelLayout.ts'
import * as reportDrafts from '../src/reportDrafts.ts'

const source = readFileSync(new URL('../src/CheckupApp.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'checkup-app-dark-offline', inlineTemplate: true })
  .content
  .replaceAll('import.meta.env', '({})')
const code = ts.transpileModule(compiled, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

const stub = name => ({ __esModule: true, default: { name, render: () => null } })
const catalog = JSON.parse(readFileSync(new URL('../src/fixtures/facility-catalog.example.json', import.meta.url), 'utf8'))
const offlineClient = {
  ...checkups,
  loadPending: () => null,
  savePending: () => {},
  checkupClient: () => ({
    catalog: async () => ({ ...catalog, version: 'offline', runtime: { map_service_configured: true } }),
  }),
}

const panelDescriptor = parse(readFileSync(new URL('../src/WorkspacePanel.vue', import.meta.url), 'utf8')).descriptor
const panelCode = ts.transpileModule(compileScript(panelDescriptor, { id: 'real-panel', inlineTemplate: true }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function loadComponent(options = {}) {
const panelExports = {}
vm.runInNewContext(panelCode, { exports: panelExports, require: name => name === 'vue' ? Vue : panelLayout, console })

const exports = {}
vm.runInNewContext(code, {
  exports,
  require: name => {
    if (name === 'vue') return Vue
    if (name === './checkupMap.ts') return mapHelpers
    if (name === './reportDrafts.ts') return reportDrafts
    if (name === './baidu.ts') return {loadBaiduMap: async () => ({})}
    if (name === './checkups.ts') return options.client ?? offlineClient
    if (name === './WorkspacePanel.vue') return { __esModule: true, default: panelExports.default }
    if (name === './BaiduRoadMap.vue') return options.map ?? stub('BaiduRoadMap')
    if (name === './CheckupReport.vue') return stub('CheckupReport')
    if (name === './ResizeGrip.vue') return stub('ResizeGrip')
    if (name === './ReportConversation.vue') return stub('ReportConversation')
    throw new Error(`unexpected import: ${name}`)
  },
  AbortController, URL, Blob, Error, setTimeout, clearTimeout, console,
})

  return exports.default
}

// Small element shim: v-model directives attach DOM listeners/values during mount.
const node = type => ({
  type, children: [], props: {}, style: {}, text: '', value: '',
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
})
const parents = new WeakMap()
const renderer = Vue.createRenderer({
  createElement: (type, isSVG, isCustomizedBuiltIn, vnodeProps) => ({ ...node(type), props: { ...vnodeProps } }),
  createText: text => ({ ...node('#text'), text }),
  createComment: text => ({ ...node('#comment'), text }),
  insert: (child, parent) => { parent.children.push(child); parents.set(child, parent) },
  remove: child => {
    const parent = parents.get(child)
    if (parent) parent.children.splice(parent.children.indexOf(child), 1)
  },
  setText: (element, text) => { element.text = text },
  setElementText: (element, text) => { element.text = text; element.children.length = 0 },
  patchProp: (element, key, previous, next) => { element.props[key] = next },
  parentNode: element => parents.get(element) ?? null,
  nextSibling: element => {
    const parent = parents.get(element)
    return parent ? parent.children[parent.children.indexOf(element) + 1] ?? null : null
  },
})

const textOf = element => [element.type !== '#comment' ? element.text : '', ...element.children.map(textOf)]
  .filter(Boolean).join(' ')
const find = (element, predicate, matches = []) => {
  if (predicate(element)) matches.push(element)
  for (const child of element.children) find(child, predicate, matches)
  return matches
}
const classesOf = element => String(element.props.class ?? '').split(/\s+/).filter(Boolean)
const byClass = (root, className) => find(root, element => classesOf(element).includes(className))

async function mount(options = {}) {
  const app = renderer.createApp(loadComponent(options))
  const root = node('#root')
  app.mount(root)
  await new Promise(resolve => setTimeout(resolve, 0))
  await Vue.nextTick()
  return { root, stop: () => app.unmount() }
}

test('map workspace presents a top search, seven selected groups, local case and no premature scores', async t => {
  const app = await mount(); t.after(app.stop)
  assert.equal(byClass(app.root, 'road-map-panel').length, 1)
  assert.equal(byClass(app.root, 'search-glass').length, 1)
  const choices = find(app.root, e => e.type === 'input' && e.props.type === 'checkbox')
  assert.equal(choices.length, 7)
  assert.ok(choices.every(e => e.props.checked))
  assert.match(textOf(app.root), /显示案例/)
  assert.equal(byClass(app.root, 'overall-score').length, 0)
  assert.equal(byClass(app.root, 'score-dock').length, 0)
})


test('a completed report with failed categories keeps one working blind action inside the main pane', async t => {
  const saved = { id: 'incomplete-completed', base: 'http://127.0.0.1:8768', status: 'running', stage: 'routes', progress: 50 }
  const report = { report_id: saved.id, center: { lng: 121.5, lat: 31.2, crs: 'BD09LL' },
    location_label: '含失败类别的完成报告', isochrone: {}, pois: [], routes: [], quality: {}, blindspot: {},
    groups: catalog.groups.map(group => ({ ...group, score: null })),
    categories: catalog.facilities.map((facility, index) => ({ facility_id: facility.id, group_id: facility.group_id,
      search_status: index === 0 ? 'failed' : 'complete' })), score: null }
  const job = { ...saved, status: 'completed', stage: 'completed', progress: 100, result: report }
  let toggles = 0
  const map = { __esModule: true, default: { setup(_props, { expose }) {
    const layersOpen = Vue.ref(false)
    expose({ layersOpen, toggleLayers() { layersOpen.value = !layersOpen.value; toggles++ } })
    return () => null
  } } }
  const app = await mount({ map, client: { ...offlineClient, loadPending: () => saved,
    watchCheckup: async (_client, _saved, _signal, onUpdate) => { onUpdate(job); return job } } })
  t.after(app.stop)
  assert.equal(byClass(app.root, 'overall-score').length, 0, 'failed categories do not unlock the comprehensive score')
  assert.match(textOf(app.root), /对已有结果提问/, 'the incomplete completed result has actually loaded')
  const buttons = byClass(app.root, 'score-blind-toggle')
  assert.equal(buttons.length, 1, 'an incomplete completed report still has exactly one blind entry')
  assert.equal(byClass(byClass(app.root, 'search-glass')[0], 'score-blind-toggle').length, 1)
  buttons[0].props.onClick()
  await Vue.nextTick()
  assert.equal(toggles, 1, 'the incomplete-report action reaches the exposed map panel control')
  assert.equal(buttons[0].props['aria-expanded'], true)
  assert.equal(byClass(app.root, 'question-glass')[0].style.display, 'none', 'opening map layers leaves report questions hidden')
})
