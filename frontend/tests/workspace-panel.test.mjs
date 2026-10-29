import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as layout from '../src/panelLayout.ts'

const { descriptor } = parse(readFileSync(new URL('../src/WorkspacePanel.vue', import.meta.url), 'utf8'))
const code = ts.transpileModule(compileScript(descriptor, { id: 'panel-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function mount(props = {}) {
  const exports = {}
  vm.runInNewContext(code, { exports, require: name => name === 'vue' ? Vue : layout, console })
  const node = () => ({ children: [] })
  const renderer = Vue.createRenderer({ createElement: node, createText: node, createComment: node, insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
  const app = renderer.createApp({ ...exports.default, render: () => null }, { title: 'Report', side: 'right', initialWidth: 380, initialHeight: 520, ...props })
  const instance = app.mount(node())
  return { state: instance.$.setupState, stop: () => app.unmount() }
}

test('panels start collapsed and expanding preserves the chosen width', t => {
  const panel = mount(); t.after(panel.stop)
  assert.equal(panel.state.collapsed, true)
  assert.equal(panel.state.panelStyle.width, '148px')
  assert.equal(panel.state.panelStyle.height, 'auto')
  panel.state.expand()
  assert.equal(panel.state.collapsed, false)
  assert.equal(panel.state.displaySize.width, 380)
  panel.state.size = { width: 440, height: 480 }
  panel.state.toggleCollapsed()
  panel.state.toggleCollapsed()
  assert.equal(panel.state.displaySize.width, 440)
  assert.equal(panel.state.displaySize.height, 480)
})

test('shrinking the viewport keeps the panel within its workspace', t => {
  const panel = mount({ initialCollapsed: false }); t.after(panel.stop)
  panel.state.bounds = { width: 330, height: 400 }
  assert.equal(panel.state.displaySize.width, 280)
  assert.equal(panel.state.displaySize.height, 344)
  panel.state.bounds = { width: 240, height: 180 }
  assert.equal(panel.state.displaySize.width, 216)
  assert.equal(panel.state.displaySize.height, 124)
})

test('dragging the inner edge adjusts width only and released drags stop resizing', t => {
  const panel = mount({ initialCollapsed: false }); t.after(panel.stop)
  const target = { setPointerCapture() {} }
  panel.state.beginResize({ button: 0, pointerId: 1, clientX: 500, clientY: 500, currentTarget: target })
  panel.state.dragResize({ clientX: 430, clientY: 540 })
  assert.equal(panel.state.displaySize.width, 450)
  assert.equal(panel.state.displaySize.height, 520, 'vertical pointer motion must not resize height')
  panel.state.endResize()
  panel.state.dragResize({ clientX: 100, clientY: 800 })
  assert.equal(panel.state.displaySize.width, 450)
})

test('collapsed and secondary-button pointer presses cannot begin resizing', t => {
  const panel = mount(); t.after(panel.stop)
  const event = { button: 0, pointerId: 1, clientX: 500, clientY: 500, currentTarget: { setPointerCapture() { assert.fail('unexpected capture') } } }
  panel.state.beginResize(event)
  panel.state.dragResize({ clientX: 400, clientY: 700 })
  assert.equal(panel.state.displaySize.width, 380)
  panel.state.expand()
  panel.state.beginResize({ ...event, button: 2 })
  panel.state.dragResize({ clientX: 400, clientY: 700 })
  assert.equal(panel.state.displaySize.width, 380)
})

test('horizontal keyboard resizing follows the free side and leaves vertical keys alone', t => {
  for (const side of ['left', 'right']) {
    const panel = mount({ side, initialCollapsed: false }); t.after(panel.stop)
    let prevented = 0
    const key = value => panel.state.keyboardResize({ key: value, preventDefault() { prevented++ } })
    key('ArrowRight')
    assert.equal(panel.state.displaySize.width, side === 'left' ? 400 : 360)
    key('ArrowLeft')
    assert.equal(panel.state.displaySize.width, 380)
    key('ArrowUp'); key('ArrowDown')
    assert.equal(panel.state.displaySize.height, 520)
    assert.equal(prevented, 2)
  }
})
