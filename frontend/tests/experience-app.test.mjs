import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = readFileSync(new URL('../src/ExperienceApp.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
const code = ts.transpileModule(compileScript(descriptor, { id: 'experience', inlineTemplate: true }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function mount(url) {
  const stats = { workspaceMounts: 0 }
  const listeners = new Map()
  const browser = { location: { href: url }, history: { pushState(_state, _title, next) { browser.location.href = String(next) } },
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
  const home = { props: ['hasSavedReport'], emits: ['start', 'resume'], setup(props, { emit }) {
    return () => Vue.h('section', { 'data-home': true }, [Vue.h('button', { 'data-start': true, onClick: () => emit('start') }),
      ...(props.hasSavedReport ? [Vue.h('button', { 'data-resume': true, onClick: () => emit('resume') })] : [])])
  } }
  const workspace = { setup() { stats.workspaceMounts++; return () => Vue.h('article', { 'data-workspace': true }) } }
  const exports = {}
  vm.runInNewContext(code, { exports, URL, window: browser, require: name => name === 'vue' ? Vue
    : { __esModule: true, default: name === './HomeLanding.vue' ? home : workspace } })
  const node = type => ({ type, props: {}, children: [], style: {}, parent: null })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node,
    insert(child, parent, anchor) { child.parent = parent; const at = parent.children.indexOf(anchor); if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child) },
    remove(child) { child.parent.children = child.parent.children.filter(item => item !== child) },
    setText() {}, setElementText() {}, patchProp: (el, key, _prev, next) => { el.props[key] = next },
    parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null })
  const root = node('root'), app = renderer.createApp(exports.default)
  app.mount(root)
  const find = (predicate, el = root) => predicate(el) ? el : el.children.map(child => find(predicate, child)).find(Boolean)
  let stopped = false
  return { stats, browser, listeners, find, stop: () => { if (!stopped) app.unmount(); stopped = true } }
}

test('home opens without mounting map services, start enters map and browser back restores home', async t => {
  const page = mount('http://localhost:5173/?base=http://localhost:8768'); t.after(page.stop)
  assert.ok(page.find(el => el.props['data-home']))
  assert.equal(page.stats.workspaceMounts, 0, 'opening home must not initialize map or checkup services')
  page.find(el => el.props['data-start']).props.onClick(); await Vue.nextTick()
  assert.equal(new URL(page.browser.location.href).searchParams.get('view'), 'map')
  assert.equal(new URL(page.browser.location.href).searchParams.get('base'), 'http://localhost:8768')
  assert.equal(page.stats.workspaceMounts, 1)
  page.browser.location.href = 'http://localhost:5173/'
  page.listeners.get('popstate')(); await Vue.nextTick()
  assert.ok(page.find(el => el.props['data-home']))
  assert.equal(page.stats.workspaceMounts, 1)
})

test('direct report links bypass home and home/resume keeps the running workspace mounted', async t => {
  const url = 'http://localhost:5173/?report=saved-report&base=http://localhost:8768'
  const page = mount(url); t.after(page.stop)
  assert.equal(page.stats.workspaceMounts, 1)
  assert.equal(page.find(el => el.props['data-home']), undefined)
  page.find(el => el.props['aria-label'] === '返回首页').props.onClick(); await Vue.nextTick()
  assert.ok(page.find(el => el.props['data-home']))
  page.find(el => el.props['data-resume']).props.onClick(); await Vue.nextTick()
  assert.equal(page.browser.location.href, url)
  assert.equal(page.stats.workspaceMounts, 1, 'returning home must not reset a report or restart an analysis')
  page.stop()
  assert.equal(page.listeners.size, 0)
})
