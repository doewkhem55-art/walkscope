import { test } from 'node:test'
import assert from 'node:assert/strict'
import { indexHoverDots, nearbyHoverDots } from '../src/mapDotInteraction.ts'

test('hover lookup is local, bounded and ignores invalid projected dots', () => {
  const points = Array.from({ length: 2800 }, (_, i) => ({ id: String(i), x: (i%70)*9, y: Math.floor(i/70)*9 }))
  const index = indexHoverDots([...points, { id: 'bad', x: NaN, y: 10 }])
  const near = nearbyHoverDots(index, 200, 160)
  assert.equal(near.length, 18)
  assert.ok(near.every(dot => Math.hypot(dot.x-200, dot.y-160) < 44 && dot.strength > 0 && dot.strength <= 1))
  assert.deepEqual([...near].sort((a,b) => b.strength-a.strength), near)
  assert.deepEqual(nearbyHoverDots(index, -200, -200), [])
  assert.equal(points[0].x, 0, 'hover selection never moves stored geographic/projection coordinates')
})
