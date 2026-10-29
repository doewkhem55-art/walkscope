import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as particles from '../src/homeParticles.ts'

const source = readFileSync(new URL('../src/HomeLanding.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
const code = ts.transpileModule(compileScript(descriptor, { id: 'home-offline', inlineTemplate: true, templateOptions: { compilerOptions: { hoistStatic: false } } }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

const node = type => ({ type, children: [], props: {}, text: '',
  getBoundingClientRect: () => ({ width: 380, height: 240, left: 0, top: 0 }), getContext: () => null })
const parents = new WeakMap()
const renderer = Vue.createRenderer({
  createElement: type => node(type), createText: text => ({ ...node('#text'), text }), createComment: () => node('#comment'),
  insert(child, parent) { parent.children.push(child); parents.set(child, parent) },
  remove(child) { const parent = parents.get(child); if (parent) parent.children.splice(parent.children.indexOf(child), 1) },
  setText(element, text) { element.text = text }, setElementText(element, text) { element.text = text; element.children = [] },
  patchProp(element, key, _old, value) { element.props[key] = value },
  parentNode: element => parents.get(element) ?? null, nextSibling: () => null,
})
const find = (root, predicate) => [root, ...root.children.flatMap(child => find(child, () => true))].filter(predicate)
const byClass = (root, name) => find(root, item => String(item.props.class ?? '').split(' ').includes(name))
const text = root => [root.text, ...root.children.map(text)].filter(Boolean).join(' ')

async function mount({ reduced = false, saved = false } = {}) {
  const documentEvents = new Map(), windowEvents = new Map(), preferenceEvents = new Map(), frames = new Map()
  const document = { hidden: false, addEventListener: (name, callback) => documentEvents.set(name, callback),
    removeEventListener: name => documentEvents.delete(name) }
  const preference = { matches: reduced, addEventListener: (name, callback) => preferenceEvents.set(name, callback),
    removeEventListener: name => preferenceEvents.delete(name) }
  let nextFrame = 0
  const window = { devicePixelRatio: 3, matchMedia: () => preference,
    requestAnimationFrame: callback => { const id = ++nextFrame; frames.set(id, callback); return id },
    cancelAnimationFrame: id => frames.delete(id), addEventListener: (name, callback) => windowEvents.set(name, callback),
    removeEventListener: name => windowEvents.delete(name) }
  let disconnected = false
  class ResizeObserver { observe() {} disconnect() { disconnected = true } }
  const exports = {}
  vm.runInNewContext(code, { exports, require: name => name === 'vue' ? Vue : particles, window, document, ResizeObserver, console })
  const events = []
  const app = renderer.createApp(exports.default, { hasSavedReport: saved, onStart: () => events.push('start'), onResume: () => events.push('resume') })
  const root = node('#root')
  app.mount(root)
  await Vue.nextTick()
  return { root, events, frames, document, documentEvents, windowEvents, preference, preferenceEvents,
    disconnected: () => disconnected, stop: () => app.unmount() }
}

test('decorative globe particles form recognizable continents with a bounded, deterministic point count', () => {
  const points = particles.createGlobePoints()
  assert.ok(points.length > 2500 && points.length < 7000)
  assert.deepEqual(points, particles.createGlobePoints())
  assert.ok(points.every(point => Math.abs(Math.hypot(point.x, point.y, point.z) - 1) < 1e-12))
  for (const [lng, lat] of [[110, 35], [20, 0], [-60, -15], [-100, 40], [135, -25]]) {
    assert.equal(particles.isDecorativeLand(lng, lat), true)
  }
  assert.equal(particles.isDecorativeLand(-140, 0), false, 'the Pacific stays dark instead of filling the whole sphere')
})

test('sphere projection preserves depth and front/back visibility through a rotation', () => {
  const front = { x: 0, y: 0, z: 1, size: 1 }
  assert.equal(particles.projectGlobePoint(front, 0, 0).z, 1)
  assert.equal(particles.projectGlobePoint(front, Math.PI, 0).z, -1)
  const tilted = particles.projectGlobePoint(front, .6, -.2)
  assert.ok(Math.abs(Math.hypot(tilted.x, tilted.y, tilted.z) - 1) < 1e-12)
})

test('homepage preserves a readable title and a single primary entry even when canvas is unavailable', async t => {
  const home = await mount(); t.after(home.stop)
  assert.equal(text(byClass(home.root, 'title-semantic')[0]), '15分钟生活圈')
  assert.equal(byClass(home.root, 'title-fallback').length, 1)
  assert.match(text(home.root), /面向自然资源和规划主管部门，.*辅助公共服务设施布局的前期排查/)
  assert.equal(byClass(home.root, 'landing-resume').length, 0)
  byClass(home.root, 'landing-start')[0].props.onClick()
  assert.deepEqual(home.events, ['start'])
  assert.equal(find(home.root, item => item.type === 'a').length, 0, 'no decorative dead navigation links')
})

test('resume is offered only when a saved report exists and emits its own action', async t => {
  const home = await mount({ saved: true }); t.after(home.stop)
  assert.equal(byClass(home.root, 'landing-resume').length, 1)
  byClass(home.root, 'landing-resume')[0].props.onClick()
  assert.deepEqual(home.events, ['resume'])
})

test('background visibility and unmount stop scheduling and clear the walking trail', async () => {
  const home = await mount()
  const landing = byClass(home.root, 'home-landing')[0]
  assert.equal(byClass(home.root, 'motion-toggle').length, 0, 'homepage has no pause button')
  assert.equal(home.frames.size, 1)
  const move = (x, y) => landing.props.onPointermove({ clientX: x, clientY: y, currentTarget: landing, pointerType: 'mouse' })
  move(10, 10); move(50, 10); move(90, 10)
  await Vue.nextTick()
  const prints = byClass(home.root, 'walking-print')
  assert.equal(prints.length, 2)
  assert.notEqual(prints[0].props.style.transform, prints[1].props.style.transform, 'left and right soles alternate')
  assert.match(prints[0].props.style.transform, /rotate\(90deg\)/, 'shoe points along travel direction')
  for (let x = 130; x < 1800; x += 40) move(x, 10)
  await Vue.nextTick()
  assert.equal(byClass(home.root, 'walking-print').length, 22, 'the footprint trail stays bounded')
  home.document.hidden = true
  home.documentEvents.get('visibilitychange')()
  await Vue.nextTick()
  assert.equal(home.frames.size, 0)
  assert.equal(byClass(home.root, 'walking-print').length, 0)
  home.document.hidden = false
  home.documentEvents.get('visibilitychange')()
  assert.equal(home.frames.size, 1)
  home.stop()
  assert.equal(home.frames.size, 0)
  assert.equal(home.documentEvents.size + home.windowEvents.size + home.preferenceEvents.size, 0)
  assert.equal(home.disconnected(), true)
})

test('system reduced-motion keeps the globe static and disables footprints without a manual toggle', async t => {
  const home = await mount({ reduced: true }); t.after(home.stop)
  const landing = byClass(home.root, 'home-landing')[0]
  assert.equal(home.frames.size, 0)
  assert.equal(landing.props['data-motion'], 'reduced')
  const move = x => landing.props.onPointermove({ clientX: x, clientY: 20, currentTarget: landing, pointerType: 'mouse' })
  move(10); move(70)
  await Vue.nextTick()
  assert.equal(byClass(home.root, 'walking-print').length, 0)
  home.preference.matches = false
  home.preferenceEvents.get('change')()
  await Vue.nextTick()
  assert.equal(home.frames.size, 1)
  assert.equal(landing.props['data-motion'], 'running')
  move(10); move(70)
  await Vue.nextTick()
  assert.ok(byClass(home.root, 'walking-print').length > 0)
  home.preference.matches = true
  home.preferenceEvents.get('change')()
  await Vue.nextTick()
  assert.equal(home.frames.size, 0)
  assert.equal(byClass(home.root, 'walking-print').length, 0)
})

test('walking traces follow turns without generating footsteps while idle or using touch', async t => {
  const home = await mount(); t.after(home.stop)
  const landing = byClass(home.root, 'home-landing')[0]
  const move = (x, y, pointerType = 'mouse') => landing.props.onPointermove({ clientX: x, clientY: y, currentTarget: landing, pointerType })
  move(10, 10, 'touch'); move(70, 10, 'touch')
  await Vue.nextTick()
  assert.equal(byClass(home.root, 'walking-print').length, 0)
  move(10, 10); move(50, 10); move(50, 50)
  await Vue.nextTick()
  const prints = byClass(home.root, 'walking-print')
  assert.equal(prints.length, 2)
  assert.match(prints[0].props.style.transform, /rotate\(90deg\)/)
  assert.match(prints[1].props.style.transform, /rotate\(180deg\)/)
  assert.match(prints[0].props.style.transform, /scaleX\(1\)/)
  assert.match(prints[1].props.style.transform, /scaleX\(-1\)/)
  const [id, animate] = home.frames.entries().next().value
  home.frames.delete(id)
  animate(100)
  await Vue.nextTick()
  assert.equal(byClass(home.root, 'walking-print').length, 2, 'an animation frame never invents new steps')
  assert.doesNotMatch(source, /cursor:\s*url\(/, 'no fixed single footprint cursor remains')
  assert.match(source, /@media \(hover: hover\) and \(pointer: fine\).*data-motion="running".*cursor: none;/s)
  assert.match(source, /prefers-reduced-motion: reduce.*animation: none !important/s, 'entry breathing and edge lights honor reduced motion')
})
