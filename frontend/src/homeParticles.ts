export type GlobePoint = { x: number; y: number; z: number; size: number }

// Decorative, deliberately simplified continent silhouettes. These are not map data.
const continents = [
  [[-168, 71], [-155, 61], [-140, 59], [-128, 50], [-123, 39], [-114, 29], [-106, 22], [-97, 16], [-86, 9], [-78, 9], [-84, 19], [-81, 25], [-81, 31], [-72, 41], [-61, 48], [-55, 53], [-64, 61], [-81, 64], [-94, 72], [-110, 78], [-139, 70]],
  [[-73, 60], [-45, 60], [-20, 77], [-36, 84], [-58, 81]],
  [[-81, 11], [-70, 12], [-61, 7], [-50, 0], [-35, -6], [-40, -21], [-48, -29], [-53, -35], [-66, -56], [-74, -45], [-75, -29], [-81, -8]],
  [[-17, 36], [-5, 36], [11, 37], [24, 32], [34, 31], [42, 13], [51, 11], [43, -1], [40, -13], [31, -29], [19, -35], [12, -24], [9, -5], [-1, 5], [-15, 10], [-18, 23]],
  [[-10, 36], [-10, 44], [-1, 50], [8, 55], [5, 60], [18, 71], [31, 71], [42, 64], [63, 69], [83, 73], [104, 77], [137, 71], [170, 70], [180, 64], [165, 58], [155, 51], [140, 46], [132, 35], [123, 39], [121, 27], [110, 18], [106, 8], [99, 8], [97, 21], [88, 22], [79, 7], [72, 21], [60, 25], [56, 17], [44, 13], [35, 29], [25, 40], [13, 44], [7, 36]],
  [[-8, 50], [2, 51], [-3, 59], [-7, 58]],
  [[130, 31], [136, 34], [141, 41], [145, 45], [141, 46], [136, 38]],
  [[95, 5], [105, -6], [115, -8], [118, -3], [108, 3]],
  [[109, 7], [119, 7], [117, -4], [110, -4]],
  [[125, -2], [141, -3], [151, -7], [145, -10], [133, -7]],
  [[113, -22], [115, -34], [129, -35], [138, -38], [148, -39], [154, -28], [146, -16], [137, -12], [129, -15], [122, -17]],
  [[166, -35], [179, -38], [174, -42], [167, -47], [165, -44]],
  [[44, -13], [50, -16], [48, -25], [44, -25]],
] as const

export function isDecorativeLand(lng: number, lat: number): boolean {
  return continents.some(ring => {
    let inside = false
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const a = ring[i]!, b = ring[j]!
      if ((a[1] > lat) !== (b[1] > lat) && lng < (b[0] - a[0]) * (lat - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside
    }
    return inside
  })
}

export function createGlobePoints(): GlobePoint[] {
  const points: GlobePoint[] = []
  const radians = Math.PI / 180
  // Equal spacing along each latitude keeps the poles from becoming bright clumps.
  for (let lat = -57; lat <= 82; lat += 1.5) {
    const latitude = lat * radians
    const step = 1.5 / Math.max(.2, Math.cos(latitude))
    for (let lng = -180; lng < 180; lng += step) {
      if (!isDecorativeLand(lng, lat)) continue
      const longitude = lng * radians
      points.push({ x: Math.cos(latitude) * Math.sin(longitude), y: Math.sin(latitude),
        z: Math.cos(latitude) * Math.cos(longitude), size: .8 + ((points.length * 17) % 11) / 28 })
    }
  }
  return points
}

export function projectGlobePoint(point: GlobePoint, yaw: number, pitch: number) {
  const x = point.x * Math.cos(yaw) + point.z * Math.sin(yaw)
  const z = point.z * Math.cos(yaw) - point.x * Math.sin(yaw)
  return { x, y: point.y * Math.cos(pitch) - z * Math.sin(pitch),
    z: point.y * Math.sin(pitch) + z * Math.cos(pitch) }
}
