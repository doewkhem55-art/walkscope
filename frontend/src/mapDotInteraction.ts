type ScreenDot = { id: string; x: number; y: number }
const CELL_PX = 48
export type DotHoverIndex = Map<string, ScreenDot[]>

// Screen-space only: build after projection changes, never on pointer movement.
export function indexHoverDots(dots: ScreenDot[]): DotHoverIndex {
  const index: DotHoverIndex = new Map()
  for (const dot of dots) {
    if (!Number.isFinite(dot.x) || !Number.isFinite(dot.y)) continue
    const key = `${Math.floor(dot.x / CELL_PX)}:${Math.floor(dot.y / CELL_PX)}`
    const bucket = index.get(key) ?? []
    bucket.push(dot)
    index.set(key, bucket)
  }
  return index
}

export function nearbyHoverDots(index: DotHoverIndex, x: number, y: number, radius = 44, limit = 18) {
  const near: Array<ScreenDot & { strength: number }> = []
  for (let row = Math.floor((y-radius)/CELL_PX); row <= Math.floor((y+radius)/CELL_PX); row++) {
    for (let col = Math.floor((x-radius)/CELL_PX); col <= Math.floor((x+radius)/CELL_PX); col++) {
      for (const dot of index.get(`${col}:${row}`) ?? []) {
        const distance = Math.hypot(dot.x-x, dot.y-y)
        if (distance < radius) near.push({ ...dot, strength: 1-distance/radius })
      }
    }
  }
  return near.sort((a, b) => b.strength-a.strength).slice(0, limit)
}
