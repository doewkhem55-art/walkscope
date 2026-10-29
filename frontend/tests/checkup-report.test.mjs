import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

// Render the real <script setup> + template of CheckupReport.vue with a tiny
// in-memory renderer. Assertions exercise the actual summaries and disclosure
// elements without a map SDK or external requests.
const filename = 'CheckupReport.vue'
const source = readFileSync(new URL('../src/CheckupReport.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source, { filename })
const compiled = compileScript(descriptor, { id: 'checkup-report-offline', inlineTemplate: true }).content
const code = ts.transpileModule(compiled, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const structuredData = { __esModule: true, default: { render: () => null } }
const loadComponent = () => {
  // A fresh module instance keeps Vue's hoisted vnodes and render cache local to
  // one mounted app; reusing them across mounts leaks element references.
  const exports = {}
  vm.runInNewContext(code, {
    exports,
    require: name => {
      if (name === 'vue') return Vue
      if (name === './StructuredData.vue') return structuredData
      if (name === './checkups.ts') return {}
      throw new Error(`unexpected import: ${name}`)
    },
    console,
  })
  return exports.default
}

const node = type => ({ type, children: [], props: {}, text: '' })
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

const text = element => [
  element.type !== '#comment' ? element.text : '',
  ...element.children.map(text),
].filter(Boolean).join(' ').replace(/\s+/g, ' ')
const find = (element, predicate, matches = []) => {
  if (predicate(element)) matches.push(element)
  for (const child of element.children) find(child, predicate, matches)
  return matches
}
const byClass = (element, className) => find(element, item => item.props.class === className)
const byId = (element, id) => find(element, item => item.props.id === id)

function category(overrides = {}) {
  return {
    facility_id: 'cat-1', group_id: 'group-1', name_zh: '社区卫生服务中心',
    search_status: 'partial', count: null, known_count: 2, reachable_count: 0,
    unresolved_count: 2, nearest_duration_s: null, nearest_distance_m: null,
    access_status: 'unverified', score: 75, score_provisional: true,
    score_profile: 'B', score_basis_count: 2, score_band: '2 处 → 75 分',
    next_band: '3 处 → 90 分', data_notes: '',
    ...overrides,
  }
}

function report(overrides = {}) {
  return {
    report_id: 'offline-report', catalog_version: 'offline-catalog',
    score: 75, score_method: 'presence-count-v3', summary: '离线渲染样本',
    isochrone: {}, search_radius_m: 1500, pois: [], routes: [], warnings: [],
    blindspot: {}, quality: {},
    groups: [{
      id: 'group-1', name_zh: '生活服务', score: 75, score_provisional: false,
      assessed_categories: 1, total_categories: 1, summary: '组摘要',
    }],
    categories: [category()],
    ...overrides,
  }
}

async function mount(value) {
  const app = renderer.createApp(loadComponent(), { report: value, catalog: { version: value.catalog_version } })
  app.component('StructuredData', { render: () => null })
  const root = node('#root')
  app.mount(root)
  await Vue.nextTick()
  return { root, stop: () => app.unmount() }
}

test('a full report renders seven collapsed group scores without 45 category cards', async t => {
  const fixture = JSON.parse(readFileSync(new URL('../src/fixtures/facility-catalog.example.json', import.meta.url), 'utf8'))
  const groups = fixture.groups.map((g, i) => ({ ...g, score: 60 + i }))
  const categories = fixture.facilities.map(f => category({ facility_id: f.id, group_id: f.group_id, search_status: 'complete' }))
  const app = await mount(report({ groups, categories })); t.after(app.stop)
  const rows = byClass(app.root, 'score-group')
  assert.equal(rows.length, 7)
  assert.ok(rows.every(row => row.type === 'details' && !row.props.open))
  for (const [i, group] of groups.entries()) {
    const row = byId(app.root, `checkup-group-${group.id}`)[0]
    assert.match(text(byClass(row, 'group-heading')[0]), new RegExp(`${group.name_zh}.*${60 + i}`))
    assert.match(text(byClass(row, 'facility-count')[0]), /圈内设施 0 处/)
  }
  assert.equal(find(app.root, row => String(row.props.id).startsWith('checkup-category-')).length, 0)
  assert.doesNotMatch(text(app.root), /模板 [ABC]|下一档|计分方式|组摘要|离线渲染样本/)
  assert.match(text(byClass(app.root, 'score-card')[0]), /设施配置参考分 75/)
  assert.equal(byClass(app.root, 'incomplete-note').length, 0, 'complete reports do not add an incompleteness notice')
})

test('expanded group quantities count unique inside-circle facilities across its member categories', async t => {
  const app = await mount(report({
    categories: [category({ search_status: 'complete' }), category({ facility_id: 'cat-2', search_status: 'complete' })],
    pois: [
      { poi_id: 'one', facility_id: 'cat-1', in_isochrone: true },
      { poi_id: 'one', facility_id: 'cat-2', in_isochrone: true },
      { poi_id: 'two', facility_id: 'cat-2', in_isochrone: true },
      { poi_id: 'outside', facility_id: 'cat-1', in_isochrone: false },
      { poi_id: 'unknown', facility_id: 'cat-1', in_isochrone: null },
      { poi_id: 'other-group', facility_id: 'cat-3', in_isochrone: true },
    ],
  })); t.after(app.stop)
  assert.match(text(byClass(app.root, 'facility-count')[0]), /圈内设施 2 处/)
  assert.equal(byClass(app.root, 'facility-count').length, 1, 'only the group aggregate is shown')
})

test('missing scores remain unscored and incomplete counts retain their known-only label', async t => {
  const app = await mount(report({ score: null, score_provisional: true,
    groups: [{ id: 'group-1', name_zh: '生活服务', score: null, score_provisional: true }],
  })); t.after(app.stop)
  assert.match(text(byClass(app.root, 'score-card')[0]), /设施配置参考分 暂不计分 暂估/)
  assert.match(text(byClass(app.root, 'group-heading')[0]), /暂估.*暂不计分/)
  assert.match(text(byClass(app.root, 'facility-count')[0]), /圈内已找到 0 处/)
  assert.match(text(app.root), /检索未齐的结果保留暂估或未计分状态/)
})

test('real zero scores display zero while analysis explanations start behind an info disclosure', async t => {
  const app = await mount(report({ score: 0,
    groups: [{ id: 'group-1', name_zh: '生活服务', score: 0 }],
    warnings: [{ message: '合成警告内容' }],
  })); t.after(app.stop)
  assert.match(text(byClass(app.root, 'score-card')[0]), /设施配置参考分 0/)
  assert.doesNotMatch(text(byClass(app.root, 'score-card')[0]), /暂不计分/)
  const hints = byClass(app.root, 'analysis-hints')[0]
  assert.equal(hints.type, 'details')
  assert.ok(!hints.props.open)
  assert.equal(find(hints, row => row.type === 'summary')[0].props['aria-label'], '展开分析提示')
  assert.match(text(hints), /合成警告内容/)
  assert.match(text(hints), /圈内独立点位去重/)
})

test('a historical single-category report keeps its original 90 score in the compact report', async t => {
  const app = await mount(report({ score: 90,
    groups: [{ id: 'group-1', name_zh: '生活服务', score: 90 }],
    quality: { historical_subset_replay: true },
  })); t.after(app.stop)
  assert.match(text(byClass(app.root, 'score-card')[0]), /设施配置参考分 90/)
  assert.equal(byClass(app.root, 'score-group').length, 1)
  assert.match(text(byClass(app.root, 'analysis-hints')[0]), /当前展示已保存报告/)
})


test('incomplete reports expose the actual complete count outside collapsed explanations', async t => {
  const categories = Array.from({ length: 45 }, (_, i) => category({
    facility_id: `cat-${i}`, search_status: i < 24 ? 'complete' : i < 36 ? 'partial' : 'failed',
  }))
  const app = await mount(report({ categories })); t.after(app.stop)
  const notes = byClass(app.root, 'incomplete-note')
  assert.equal(notes.length, 1)
  assert.equal(text(notes[0]), '部分结果 · 24/45 类检索完整')
  assert.equal(notes[0].type, 'p')
  assert.equal(parents.get(notes[0]).props.id, 'checkup-report', 'the warning must remain visible above collapsed groups and hints')
  assert.equal(byClass(byClass(app.root, 'analysis-hints')[0], 'incomplete-note').length, 0)
})
