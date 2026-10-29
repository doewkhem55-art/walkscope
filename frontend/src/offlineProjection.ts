type Point = { lng: number; lat: number }
type Viewport = { width: number; height: number; left?: number; right?: number; top?: number; bottom?: number }

// A local-metre view of saved coordinates, explicitly without road tiles or new measurements.
// This projection is only a replay fallback; it never changes report geometry or scores.
export function createOfflineProjection(center: Point, locations: Point[], viewport: Viewport,
  view = { zoom: 1, panX: 0, panY: 0 }) {
  const width = Math.max(1, viewport.width), height = Math.max(1, viewport.height)
  const cosine = Math.max(.01, Math.cos(center.lat * Math.PI / 180))
  const toMeters = (p: Point) => ({ x: (p.lng - center.lng) * 111195.08 * cosine, y: (p.lat - center.lat) * 111195.08 })
  const points = [center, ...locations].filter(p => Number.isFinite(p.lng) && Number.isFinite(p.lat)).map(toMeters)
  const xs = points.map(p => p.x), ys = points.map(p => p.y)
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys)
  const left = Math.min(viewport.left ?? 24, width * .4), right = Math.min(viewport.right ?? 24, width * .4)
  const top = Math.min(viewport.top ?? 24, height * .4), bottom = Math.min(viewport.bottom ?? 24, height * .4)
  const middleX = (minX + maxX) / 2, middleY = (minY + maxY) / 2
  const scale = Math.min((width - left - right) / Math.max(160, maxX - minX),
    (height - top - bottom) / Math.max(160, maxY - minY)) * .9 * view.zoom
  return {
    metersPerPixel: 1 / scale,
    pointToPixel(p: Point) {
      const value = toMeters(p)
      return { x: left + (width - left - right) / 2 + (value.x - middleX) * scale + view.panX,
        y: top + (height - top - bottom) / 2 - (value.y - middleY) * scale + view.panY }
    },
  }
}
