import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createOfflineProjection } from '../src/offlineProjection.ts'

const center = { lng: 121.508602, lat: 31.288268 }
const locations = [center, { lng: 121.498602, lat: 31.278268 }, { lng: 121.518602, lat: 31.298268 }]

test('offline projection preserves geographic direction and fits saved geometry in the visible map area', () => {
  const projection = createOfflineProjection(center, locations, { width: 1440, height: 800, left: 320, right: 370, top: 100, bottom: 100 })
  const pixel = projection.pointToPixel(center)
  assert.ok(projection.pointToPixel({ ...center, lng: center.lng + .001 }).x > pixel.x)
  assert.ok(projection.pointToPixel({ ...center, lat: center.lat + .001 }).y < pixel.y)
  for (const location of locations) {
    const p = projection.pointToPixel(location)
    assert.ok(p.x >= 320 && p.x <= 1070 && p.y >= 100 && p.y <= 700)
  }
  assert.ok(projection.metersPerPixel > 0)
})

test('offline zoom and pan change only the viewport, and small or empty views stay finite', () => {
  const viewport = { width: 390, height: 600, left: 20, right: 20, top: 80, bottom: 80 }
  const initial = createOfflineProjection(center, locations, viewport)
  const zoomed = createOfflineProjection(center, locations, viewport, { zoom: 2, panX: 30, panY: -10 })
  const a = initial.pointToPixel(center), b = initial.pointToPixel(locations[1])
  const c = zoomed.pointToPixel(center), d = zoomed.pointToPixel(locations[1])
  assert.ok(Math.abs((d.x - c.x) - (b.x - a.x) * 2) < 1e-6)
  assert.ok(Math.abs(c.x - a.x - 30) < 1e-6)
  assert.ok(Math.abs(c.y - a.y + 10) < 1e-6)
  const small = createOfflineProjection(center, [], { width: 1, height: 1 })
  assert.ok(Number.isFinite(small.pointToPixel(center).x))
  assert.ok(Number.isFinite(small.pointToPixel(center).y))
})
