export type FloatingRect = { left: number; top: number; width: number; height: number }
export type FloatingBounds = { left: number; top: number; right: number; bottom: number }
export type ResizeEdge = 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w' | 'nw'

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max)

/** Keep the entire floating window (including its close button) inside its workspace. */
export function fitFloatingRect(rect: FloatingRect, bounds: FloatingBounds): FloatingRect {
  const width = Math.min(rect.width, Math.max(0, bounds.right - bounds.left))
  const height = Math.min(rect.height, Math.max(0, bounds.bottom - bounds.top))
  return { width, height, left: clamp(rect.left, bounds.left, bounds.right - width), top: clamp(rect.top, bounds.top, bounds.bottom - height) }
}

/** Snap to the workspace edges or center only while nearby; the next drag can leave freely. */
export function moveFloatingRect(rect: FloatingRect, dx: number, dy: number, bounds: FloatingBounds, distance = 14): FloatingRect {
  const fitted = fitFloatingRect(rect, bounds)
  function snap(value: number, start: number, end: number, size: number) {
    const candidates = [start, (start + end - size) / 2, end - size]
    const nearest = candidates.reduce((best, candidate) => Math.abs(candidate - value) < Math.abs(best - value) ? candidate : best)
    return clamp(Math.abs(nearest - value) <= distance ? nearest : value, start, end - size)
  }
  return { ...fitted, left: snap(rect.left + dx, bounds.left, bounds.right, fitted.width), top: snap(rect.top + dy, bounds.top, bounds.bottom, fitted.height) }
}

/** Each edge moves independently; the opposite edge stays fixed. */
export function resizeFloatingRect(rect: FloatingRect, dx: number, dy: number, edge: ResizeEdge, bounds: FloatingBounds, minimum = { width: 260, height: 140 }): FloatingRect {
  const fitted = fitFloatingRect(rect, bounds)
  let { left, top } = fitted
  let right = left + fitted.width, bottom = top + fitted.height
  if (edge.includes('w')) left = clamp(left + dx, bounds.left, right - Math.min(minimum.width, right - bounds.left))
  if (edge.includes('e')) right = clamp(right + dx, left + Math.min(minimum.width, bounds.right - left), bounds.right)
  if (edge.includes('n')) top = clamp(top + dy, bounds.top, bottom - Math.min(minimum.height, bottom - bounds.top))
  if (edge.includes('s')) bottom = clamp(bottom + dy, top + Math.min(minimum.height, bounds.bottom - top), bounds.bottom)
  return { left, top, width: right - left, height: bottom - top }
}

// Shared by all floating windows, including those inside the map component.
let floatingLayer = 60
export function nextFloatingLayer(): number { return ++floatingLayer }
