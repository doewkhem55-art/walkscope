import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fitPanelSize, resizePanel } from '../src/panelLayout.ts'

test('resizing grows from the free corner on either side', () => {
  const size = { width: 320, height: 420 }
  assert.deepEqual(resizePanel(size, 80, 50, 'left', { width: 1000, height: 700 }), { width: 400, height: 470 })
  assert.deepEqual(resizePanel(size, -80, 50, 'right', { width: 1000, height: 700 }), { width: 400, height: 470 })
})

test('a panel cannot shrink below usable controls or grow beyond its container', () => {
  assert.deepEqual(fitPanelSize({ width: 1, height: 1 }, { width: 1000, height: 700 }), { width: 280, height: 220 })
  assert.deepEqual(fitPanelSize({ width: 3000, height: 2000 }, { width: 1000, height: 700 }), { width: 976, height: 644 })
})

test('a viewport smaller than minimum size remains bounded instead of overflowing', () => {
  assert.deepEqual(fitPanelSize({ width: 400, height: 600 }, { width: 240, height: 180 }), { width: 216, height: 124 })
})
