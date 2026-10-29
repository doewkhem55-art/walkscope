import { test } from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as geometry from '../src/panelGeometry.ts'

const descriptor = parse(readFileSync(new URL('../src/ResizeGrip.vue', import.meta.url), 'utf8')).descriptor
const code = ts.transpileModule(compileScript(descriptor, { id: 'grip', inlineTemplate: true }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const bounds = { left: 12, top: 12, right: 788, bottom: 588 }
const initial = { left: 100, top: 80, width: 400, height: 300 }

test('all four edges and four corners resize independently with the opposite edge fixed', () => {
  for (const edge of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) {
    const rect = geometry.resizeFloatingRect(initial, 30, 20, edge, bounds)
    assert.equal(rect.left, edge.includes('w') ? 130 : 100)
    assert.equal(rect.top, edge.includes('n') ? 100 : 80)
    assert.equal(rect.width, 400 + (edge.includes('e') ? 30 : edge.includes('w') ? -30 : 0))
    assert.equal(rect.height, 300 + (edge.includes('s') ? 20 : edge.includes('n') ? -20 : 0))
    for (const delta of [-10000, 10000]) {
      const limited = geometry.resizeFloatingRect(initial, delta, delta, edge, bounds)
      assert.ok(limited.left >= bounds.left && limited.top >= bounds.top)
      assert.ok(limited.left + limited.width <= bounds.right && limited.top + limited.height <= bounds.bottom)
      assert.ok(limited.width >= 260 && limited.height >= 140)
    }
  }
})

test('magnetic dragging snaps to each edge or center, and a subsequent drag can leave again', () => {
  assert.equal(geometry.moveFloatingRect(initial, -80, 0, bounds).left, 12)
  assert.equal(geometry.moveFloatingRect(initial, 100, 0, bounds).left, 200)
  const right = geometry.moveFloatingRect(initial, 280, 0, bounds)
  assert.equal(right.left, 388)
  assert.equal(geometry.moveFloatingRect(right, -45, 0, bounds).left, 343)
  assert.equal(geometry.moveFloatingRect(initial, 0, -60, bounds).top, 12)
  assert.equal(geometry.moveFloatingRect(initial, 0, 78, bounds).top, 150)
  assert.equal(geometry.moveFloatingRect(initial, 0, 200, bounds).top, 288)
  assert.deepEqual(geometry.moveFloatingRect(initial, -9999, 9999, bounds), { left: 12, top: 288, width: 400, height: 300 })
})

test('tiny viewports and panels shorter than the normal minimum stay reachable', () => {
  const tiny = { left: 12, top: 12, right: 212, bottom: 112 }
  assert.deepEqual(geometry.fitFloatingRect(initial, tiny), { left: 12, top: 12, width: 200, height: 100 })
  for (const edge of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) {
    const rect = geometry.resizeFloatingRect(initial, 9999, -9999, edge, tiny)
    assert.ok(rect.left >= tiny.left && rect.top >= tiny.top)
    assert.ok(rect.left + rect.width <= tiny.right && rect.top + rect.height <= tiny.bottom)
  }
  const short = { left: 100, top: 40, width: 300, height: 60 }
  const moved = geometry.moveFloatingRect(short, 20, 20, bounds)
  assert.equal(moved.height, 60, 'dragging a collapsed window does not expand it')
})

function mount(props = {}, stageRect = null) {
  const events = new Map()
  const window = { innerWidth: 800, innerHeight: 600, addEventListener: (name, handler) => events.set(name, handler), removeEventListener: name => events.delete(name) }
  const exports = {}
  vm.runInNewContext(code, { exports, require: name => name === 'vue' ? Vue : geometry, window, console })
  const element = type => Vue.markRaw({
    type, children: [], props: {}, style: {}, attributes: {}, listeners: new Map(), captured: null,
    addEventListener(name, handler) { this.listeners.set(name, handler) },
    removeEventListener(name) { this.listeners.delete(name) },
    getAttribute(name) { return this.attributes[name] ?? null },
    setAttribute(name, value) { this.attributes[name] = value },
    removeAttribute(name) { delete this.attributes[name] },
    querySelector() { return this.children.find(child => child.type === 'header') ?? null },
    closest() { return ['button', 'input'].includes(this.type) ? this : null },
    setPointerCapture(id) { this.captured = id },
    hasPointerCapture(id) { return this.captured === id },
    releasePointerCapture() { this.captured = null },
  })
  const pane = element('section')
  const heading = element('header')
  heading.parentElement = pane
  pane.children.push(heading)
  const stage = stageRect && { clientLeft: 2, clientTop: 2, clientWidth: stageRect.width - 4, clientHeight: stageRect.height - 4, scrollLeft: 0, scrollTop: 0, getBoundingClientRect: () => stageRect }
  pane.offsetParent = stage
  pane.getBoundingClientRect = () => {
    const width = Number.parseFloat(pane.style.width) || initial.width
    const naturalHeight = pane.collapsed ? 60 : Number.parseFloat(pane.style.height) || initial.height
    const height = Math.min(naturalHeight, Number.parseFloat(pane.style.maxHeight) || Infinity)
    const left = pane.style.left ? Number.parseFloat(pane.style.left) + (stageRect?.left ?? 0) + (stage?.clientLeft ?? 0) : initial.left
    const top = pane.style.top ? Number.parseFloat(pane.style.top) + (stageRect?.top ?? 0) + (stage?.clientTop ?? 0) : initial.top
    return { left, top, width, height, right: left + width, bottom: top + height }
  }
  const renderer = Vue.createRenderer({
    createElement: element, createText: () => element('#text'), createComment: () => element('#comment'),
    insert(child, parent) { parent.children.push(child); child.parentElement = parent }, remove() {},
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text; node.children.length = 0 },
    patchProp(node, key, previous, next) { node.props[key] = next }, parentNode: node => node.parentElement ?? null, nextSibling: () => null,
  })
  let starts = 0
  const app = renderer.createApp(exports.default, { label: '测试窗', ...props, onResizeStart: () => starts++ })
  app.mount(pane)
  const handles = pane.children.find(child => child.type === 'span')
  const grip = edge => handles.children.find(child => child.type === 'button' && child.props.class.split(/\s+/).includes(`edge-${edge}`))
  const press = (target, extra = {}) => ({ button: 0, currentTarget: target, target, clientX: 0, clientY: 0, pointerId: 1, stopPropagation() {}, preventDefault() {}, ...extra })
  const fire = (target, name, event) => (target.listeners.get(name) ?? target.props[`on${name[0].toUpperCase()}${name.slice(1)}`])(press(target, event))
  return { pane, heading, app, grip, fire, window, events, starts: () => starts, stop: () => app.unmount() }
}

test('resize borders render no visible glyphs and remain directly draggable', t => {
  const m = mount(); t.after(m.stop)
  const textContent = node => (node.text ?? '') + node.children.map(textContent).join('')
  for (const edge of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) {
    assert.equal(textContent(m.grip(edge)), '', `${edge} must remain an invisible resize target`)
  }
  for (const [edge, left, top, width, height] of [
    ['n', '100px', '100px', '400px', '280px'],
    ['e', '100px', '80px', '430px', '300px'],
    ['s', '100px', '80px', '400px', '320px'],
    ['w', '130px', '80px', '370px', '300px'],
  ]) {
    const border = mount(); t.after(border.stop)
    border.fire(border.grip(edge), 'pointerdown')
    border.fire(border.grip(edge), 'pointermove', { clientX: 30, clientY: 20 })
    border.fire(border.grip(edge), 'pointerup')
    assert.equal(border.pane.style.left, left)
    assert.equal(border.pane.style.top, top)
    assert.equal(border.pane.style.width, width)
    assert.equal(border.pane.style.height, height)
  }
})

test('real component renders all eight grips, supports pointer/keyboard resizing and releases capture', t => {
  const m = mount(); t.after(m.stop)
  for (const edge of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) assert.ok(m.grip(edge))
  m.fire(m.grip('nw'), 'pointerdown')
  m.fire(m.grip('nw'), 'pointermove', { clientX: 30, clientY: 20 })
  assert.equal(m.pane.style.left, '130px')
  assert.equal(m.pane.style.top, '100px')
  assert.equal(m.pane.style.width, '370px')
  assert.equal(m.pane.style.height, '280px')
  m.fire(m.grip('nw'), 'pointerup')
  assert.equal(m.grip('nw').captured, null)
  assert.equal(m.pane.style.transition, undefined)
  m.fire(m.grip('s'), 'keydown', { key: 'ArrowDown', shiftKey: true })
  assert.equal(m.pane.style.height, '285px')
  assert.equal(m.starts(), 2)
})

test('title dragging handles centered/right anchored panes, snapping, keyboard movement and cleanup', () => {
  for (const anchor of ['center', 'right']) {
    const m = mount({ anchor })
    m.fire(m.heading, 'pointerdown')
    m.fire(m.heading, 'pointermove', { clientX: 100, clientY: 70 })
    assert.equal(m.pane.style.left, '200px')
    assert.equal(m.pane.style.top, '150px')
    assert.equal(m.pane.style.transform, 'none')
    assert.equal(m.pane.style.right, 'auto')
    assert.equal(m.pane.style.bottom, 'auto')
    m.fire(m.heading, 'pointerup')
    m.fire(m.heading, 'keydown', { key: 'ArrowRight' })
    assert.equal(m.pane.style.left, '220px')
    m.fire(m.heading, 'keydown', { key: 'ArrowLeft', shiftKey: true })
    assert.equal(m.pane.style.left, '215px', 'fine keyboard moves do not stick to magnetic guides')
    assert.equal(m.heading.attributes.tabindex, '0')
    m.stop()
    assert.equal(m.heading.listeners.size, 0)
    assert.equal(m.events.size, 0)
    assert.equal(m.heading.attributes.tabindex, undefined)
  }
})

test('title controls, secondary buttons, disabled panes and unrelated pointers never drag', t => {
  const m = mount(); t.after(m.stop)
  m.fire(m.heading, 'pointerdown', { target: m.grip('se') })
  m.fire(m.heading, 'pointermove', { clientX: 100, clientY: 100 })
  assert.deepEqual(m.pane.style, {})
  m.fire(m.grip('se'), 'pointerdown', { button: 2 })
  m.fire(m.grip('se'), 'pointermove', { clientX: 100, clientY: 100 })
  assert.deepEqual(m.pane.style, {})
  m.fire(m.grip('se'), 'pointerdown')
  m.fire(m.grip('se'), 'pointermove', { pointerId: 2, clientX: 100, clientY: 100 })
  assert.equal(m.pane.style.width, undefined)
  m.fire(m.grip('se'), 'pointercancel')
  const disabled = mount({ disabled: true }); t.after(disabled.stop)
  disabled.fire(disabled.heading, 'pointerdown')
  disabled.fire(disabled.grip('se'), 'pointerdown')
  disabled.fire(disabled.grip('se'), 'keydown', { key: 'ArrowDown' })
  assert.deepEqual(disabled.pane.style, {})
})

test('positioning uses the actual containing block, and viewport shrink keeps close buttons reachable', t => {
  const m = mount({}, { left: 50, top: 30, width: 650, height: 500 }); t.after(m.stop)
  m.fire(m.heading, 'pointerdown')
  m.fire(m.heading, 'pointermove', { clientX: 50, clientY: 50 })
  m.fire(m.heading, 'pointerup')
  assert.equal(m.pane.style.left, '98px', 'viewport left150 minus parent52')
  assert.equal(m.pane.style.top, '98px', 'viewport top130 minus parent32')
  m.window.innerWidth = 350
  m.window.innerHeight = 250
  m.events.get('resize')()
  const rect = m.pane.getBoundingClientRect()
  assert.ok(rect.left >= 12 && rect.top >= 12)
  assert.ok(rect.right <= 338 && rect.bottom <= 238)
})


test('moving or refitting a collapsed window preserves its natural or previously resized expanded height', t => {
  const m = mount(); t.after(m.stop)
  for (const height of [undefined, '420px']) {
    m.pane.style.height = height
    m.pane.collapsed = true
    m.fire(m.heading, 'pointerdown')
    m.fire(m.heading, 'pointermove', { clientX: 30, clientY: 20 })
    m.fire(m.heading, 'pointerup')
    assert.equal(m.pane.style.height, height, 'the collapsed 60px height must not replace the expanded declaration')
    m.events.get('resize')()
    assert.equal(m.pane.style.height, height, 'viewport refit also preserves the expanded declaration')
    m.pane.collapsed = false
    assert.equal(m.pane.getBoundingClientRect().height, height ? 420 : 300)
  }
  m.pane.collapsed = true
  m.fire(m.heading, 'pointerdown')
  m.fire(m.heading, 'pointermove', { clientX: 0, clientY: 9999 })
  m.fire(m.heading, 'pointerup')
  m.pane.collapsed = false
  assert.equal(m.pane.style.height, '420px', 'the chosen height survives even near the bottom')
  assert.ok(m.pane.getBoundingClientRect().bottom <= 588, 'expanded content is capped to the remaining visible space')
})


test('interacting with alternate windows promotes a shared layer and removes pane listeners on unmount', () => {
  const first = mount(), second = mount()
  try {
    first.fire(first.pane, 'pointerdown')
    const initialLayer = Number(first.pane.style.zIndex)
    assert.ok(initialLayer > 60)
    second.fire(second.pane, 'focusin')
    assert.ok(Number(second.pane.style.zIndex) > initialLayer)
    first.fire(first.heading, 'keydown', { key: 'ArrowRight' })
    assert.ok(Number(first.pane.style.zIndex) > Number(second.pane.style.zIndex))
    second.fire(second.pane, 'pointerdown')
    assert.ok(Number(second.pane.style.zIndex) > Number(first.pane.style.zIndex))
  } finally {
    first.stop(); second.stop()
    assert.equal(first.pane.listeners.size, 0)
    assert.equal(second.pane.listeners.size, 0)
  }
})
