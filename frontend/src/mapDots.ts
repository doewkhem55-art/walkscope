/**
 * Rule-based walk-time dot matrix for the report map (阶段4a).
 *
 * The dots are a *visual expression* of the walking-time range, not facilities, population
 * or independent measurements. Equal-size circles sit on one geographic grid with one
 * staggered hexagonal spacing and opacity. Three shades of blue distinguish 0–5 / 5–10 / 10–15 minutes;
 * the grid never changes density between tiers.
 *
 * Stage-3 backend contract (`tiers-v1`, already served by the replay service) stores a
 * single object under `report.isochrone.tiers`:
 *
 *   tiers = {
 *     schema_version: 'tiers-v1', thresholds_s: [300, 600, 900],
 *     bands:      [{ tier_id: '0-300', from_s: 0, to_s: 300, label_zh: '5分钟', geometry: {...} }, ...],
 *     cumulative: [{ threshold_s: 300, label_zh: '5分钟', geometry: {...} }, ...],
 *     time_source: 'route-matrix', unknown_ratio: 0.28, unknown_mask: {...},
 *   }
 *
 * `bands[].geometry` is preferred (non-overlapping display tiers, keyed by `to_s`); if no
 * usable bands exist the parser falls back to `cumulative[].geometry` keyed by
 * `threshold_s`. Legacy array/alias shapes are still accepted, including
 * `report.tiers`/`report.walk_tiers`, `band_geojson`/`cumulative_geojson`, the stage-2A
 * payload object, and the aliases `max_seconds`/`max_duration_s`/`seconds`/`duration_s`/
 * `max_minutes`/`minutes`/`to_s`/`tier_id`/`level`, plus `geometry`/`polygon`/`boundary`.
 * It is the single place to adapt if stage 3 changes. Tiers may be cumulative or
 * non-overlapping; first match from inner to outer assigns each point exactly once.
 *
 * With no valid three-tier data the plan is `single`: only the existing 15-minute layer is
 * drawn (uniform dots), labelled "仅单层". The outer polygon is never scaled to fake 5/10.
 */

import { parsePolygonGeometry } from './checkupMap.ts'
import type { MapGeometry, MapPoint } from './checkupMap.ts'

export type TimeTierId = '5' | '10' | '15'
export type DotTier = TimeTierId | 'single'
export type DotMode = 'tiers' | 'single' | 'none'

export interface IsochroneTier {
  id: TimeTierId
  label: string
  /** Upper bound of the cumulative walking time for this tier. */
  maxSeconds: number
  radiusPx: number
  color: string
  opacity: number
  geometry: MapGeometry
}

export interface TierDefinition {
  id: TimeTierId
  label: string
  maxSeconds: number
  radiusPx: number
  color: string
  opacity: number
}

/** Uniform grid symbols; only blue shade encodes the measured walking-time band. */
export const TIER_DEFINITIONS: ReadonlyArray<TierDefinition> = [
  { id: '5', label: '0–5 分钟', maxSeconds: 300, radiusPx: 2.2, color: '#b8e5ff', opacity: 0.36 },
  { id: '10', label: '5–10 分钟', maxSeconds: 600, radiusPx: 2.2, color: '#609fcc', opacity: 0.36 },
  { id: '15', label: '10–15 分钟', maxSeconds: 900, radiusPx: 2.2, color: '#2e5f8a', opacity: 0.36 },
]

/** Uniform single-layer dots are not a time tier; label and legend must say so. */
export const SINGLE_LAYER_DOT = { radiusPx: 2.2, color: '#609fcc', opacity: 0.36 } as const

export interface DotPoint { lng: number; lat: number; tier: DotTier }
export interface DotStyle { r: number; fill: string; opacity: number }

/** All bands share the same screen radius, proportional to their projected grid step. */
export function dotRadiusForSpacing(spacingPx: number): number {
  return Number.isFinite(spacingPx) && spacingPx > 0 ? Math.max(.8, Math.min(128, spacingPx * .48)) : 2.2
}

export interface DotPlan {
  mode: DotMode
  /** Ordered inner-to-outer; empty unless all three tiers were valid. */
  tiers: IsochroneTier[]
  points: DotPoint[]
  /** Geographic grid spacing in metres; null when nothing can be drawn. */
  spacingM: number | null
}

/** Keep a hard circle budget for responsive projection and panning. */
export const MAX_DOTS = 2800
const MAX_GRID_CELLS = 40000
const MIN_SPACING_M = 12
const MAX_SPACING_M = 400

type JsonRecord = Record<string, unknown>
const record = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}

function secondsOf(item: JsonRecord): number | null {
  for (const key of ['max_seconds', 'max_duration_s', 'seconds', 'duration_s', 'to_s', 'threshold_s']) {
    const value = item[key]
    if (typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 900) return Math.round(value)
  }
  for (const key of ['max_minutes', 'minutes', 'duration_min']) {
    const value = item[key]
    if (typeof value === 'number' && Number.isFinite(value) && value > 0 && value * 60 <= 900) return Math.round(value * 60)
  }
  // Stage-3 style labels: tier_id '0-300' / '300-600' / '600-900' or a bare 5/10/15.
  for (const key of ['tier_id', 'tier', 'level', 'label']) {
    const value = item[key]
    const span = /(\d+)\s*[-–]\s*(\d+)/.exec(String(value))
    if (span) {
      const seconds = Number(span[2])
      if (seconds > 0 && seconds <= 900) return seconds
      continue
    }
    const minutes = typeof value === 'number' ? value : /^\d+$/.test(String(value)) ? Number(value) : NaN
    if ([5, 10, 15].includes(minutes)) return minutes * 60
    // A bare 300/600/900 in a tier id is already seconds.
    if ([300, 600, 900].includes(minutes)) return minutes
  }
  return null
}

/**
 * Flatten the known tier containers into one candidate list.
 *
 * `tiers-v1` objects put non-overlapping `bands` and cumulative rings in the same object;
 * bands are emitted first so the parser's first-match/dedupe keeps the band geometry. Any
 * remaining array/legacy shape is appended as a fallback, never silently dropped.
 */
function tierCandidates(report: JsonRecord): unknown[] {
  const isochrone = record(report.isochrone)
  const sources = [
    report.tiers,
    report.walk_tiers,
    report.tiered_isochrone,
    isochrone.tiers,
    isochrone.tiered_isochrone,
  ]
  const bands: unknown[] = []
  const fallback: unknown[] = []
  for (const source of sources) {
    if (Array.isArray(source)) {
      fallback.push(...source)
      continue
    }
    const candidate = record(source)
    if (Array.isArray(candidate.bands)) bands.push(...candidate.bands)
    if (Array.isArray(candidate.cumulative)) fallback.push(...candidate.cumulative)
    if (Array.isArray(candidate.tiers)) fallback.push(...candidate.tiers)
  }
  return [...bands, ...fallback]
}

function tierIdForSeconds(seconds: number): TimeTierId | null {
  if (seconds <= 300) return '5'
  if (seconds <= 600) return '10'
  if (seconds <= 900) return '15'
  return null
}

/**
 * Parse the report's three walk-time tiers.
 *
 * Returns `[]` unless all of 5/10/15 minutes are present with valid geometry. This strict
 * gate is the "no fabrication" guard: the existing 15-minute isochrone is never used to
 * synthesize missing inner tiers.
 */
export function parseIsochroneTiers(report: unknown): IsochroneTier[] {
  const parsed = new Map<TimeTierId, { seconds: number; geometry: MapGeometry }>()
  for (const raw of tierCandidates(record(report))) {
    const item = record(raw)
    const seconds = secondsOf(item)
    const id = seconds == null ? null : tierIdForSeconds(seconds)
    if (!seconds || !id || parsed.has(id)) continue
    // Stage-3 serialisation prefers explicit non-overlapping bands, then cumulative rings;
    // the older/simpler aliases keep the parser useful before that contract is frozen.
    const geometry = parsePolygonGeometry(item.band_geojson ?? item.cumulative_geojson
      ?? item.geometry ?? item.polygon ?? item.boundary)
    if (geometry) parsed.set(id, { seconds, geometry })
  }
  if (parsed.size !== TIER_DEFINITIONS.length) return []
  return TIER_DEFINITIONS.flatMap(definition => {
    const tier = parsed.get(definition.id)
    return tier ? [{
      id: definition.id,
      label: definition.label,
      maxSeconds: definition.maxSeconds,
      radiusPx: definition.radiusPx,
      color: definition.color,
      opacity: definition.opacity,
      geometry: tier.geometry,
    }] : []
  }).sort((a, b) => a.maxSeconds - b.maxSeconds)
}

/** Ray casting across one closed ring. */
export function pointInRing(ring: MapPoint[], point: MapPoint): boolean {
  let inside = false
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const current = ring[index]
    const last = ring[previous]
    if ((current.lat > point.lat) !== (last.lat > point.lat)
        && point.lng < ((last.lng - current.lng) * (point.lat - current.lat)) / (last.lat - current.lat) + current.lng) {
      inside = !inside
    }
  }
  return inside
}

/**
 * Even-odd point-in-polygon for the parsed geometry shape. Holes and disjoint components
 * are honoured, so dots are never painted over unknown holes for the sake of appearance.
 */
export function pointInGeometry(geometry: MapGeometry, point: MapPoint): boolean {
  for (const polygon of geometry) {
    let inside = false
    for (const ring of polygon) {
      if (ring.length >= 4 && pointInRing(ring, point)) inside = !inside
    }
    if (inside) return true
  }
  return false
}

// Unknown takes precedence if imperfect upstream geometries overlap; it is never a blind spot.
export function coverageAtPoint(point: MapPoint, blind: MapGeometry[], unknown: MapGeometry[]): 'blind' | 'unknown' | 'covered' {
  if (unknown.some(geometry => pointInGeometry(geometry, point))) return 'unknown'
  return blind.some(geometry => pointInGeometry(geometry, point)) ? 'blind' : 'covered'
}

/** The tier a point belongs to, using the shortest (innermost) containing tier. */
export function tierAtPoint(tiers: IsochroneTier[], point: MapPoint): TimeTierId | null {
  for (const tier of tiers) {
    if (pointInGeometry(tier.geometry, point)) return tier.id
  }
  return null
}

export function dotStyleFor(point: DotPoint): DotStyle {
  if (point.tier === 'single') {
    return { r: SINGLE_LAYER_DOT.radiusPx, fill: SINGLE_LAYER_DOT.color, opacity: SINGLE_LAYER_DOT.opacity }
  }
  const definition = TIER_DEFINITIONS.find(item => item.id === point.tier)
  return definition
    ? { r: definition.radiusPx, fill: definition.color, opacity: definition.opacity }
    : { r: SINGLE_LAYER_DOT.radiusPx, fill: SINGLE_LAYER_DOT.color, opacity: SINGLE_LAYER_DOT.opacity }
}

interface BoundingBox { minLng: number; maxLng: number; minLat: number; maxLat: number }

function unionBoundingBox(geometries: MapGeometry[]): BoundingBox | null {
  const box: BoundingBox = { minLng: Infinity, maxLng: -Infinity, minLat: Infinity, maxLat: -Infinity }
  for (const geometry of geometries) {
    for (const polygon of geometry) {
      for (const ring of polygon) {
        for (const point of ring) {
          box.minLng = Math.min(box.minLng, point.lng)
          box.maxLng = Math.max(box.maxLng, point.lng)
          box.minLat = Math.min(box.minLat, point.lat)
          box.maxLat = Math.max(box.maxLat, point.lat)
        }
      }
    }
  }
  return Number.isFinite(box.minLng) && box.minLng < box.maxLng && box.minLat < box.maxLat ? box : null
}

function metresPerDegree(box: BoundingBox) {
  const centreLat = (box.minLat + box.maxLat) / 2
  const latitude = 111320
  const longitude = 111320 * Math.max(0.2, Math.cos((centreLat * Math.PI) / 180))
  return { latitude, longitude }
}

function initialSpacingM(box: BoundingBox): number {
  const { latitude, longitude } = metresPerDegree(box)
  const widthM = (box.maxLng - box.minLng) * longitude
  const heightM = (box.maxLat - box.minLat) * latitude
  const area = Math.max(1, widthM * heightM)
  return Math.max(MIN_SPACING_M, Math.min(MAX_SPACING_M, Math.sqrt(area / (MAX_DOTS * Math.sqrt(3) / 2)) * 0.7))
}

/**
 * One hexagonal grid for every tier. Rows are anchored by their global BD09LL row number,
 * so panning/zooming never changes which geographic positions carry dots (no drift and no
 * flicker); only their projected pixels move together with the basemap.
 */
function gridPoints(box: BoundingBox, spacingM: number): MapPoint[] {
  const { latitude, longitude } = metresPerDegree(box)
  const dLat = spacingM * Math.sqrt(3) / 2 / latitude
  const dLng = spacingM / longitude
  const rows = Math.floor((box.maxLat - box.minLat) / dLat) + 2
  const columns = Math.floor((box.maxLng - box.minLng) / dLng) + 2
  if (rows * columns > MAX_GRID_CELLS) return []
  const startRow = Math.floor(box.minLat / dLat)
  const startColumn = Math.floor(box.minLng / dLng) - 1
  const points: MapPoint[] = []
  for (let row = 0; row <= rows; row++) {
    const globalRow = startRow + row
    const lat = globalRow * dLat
    if (lat < box.minLat || lat > box.maxLat) continue
    const halfStep = ((globalRow % 2 + 2) % 2) / 2
    for (let column = 0; column <= columns + 1; column++) {
      const lng = (startColumn + column + halfStep) * dLng
      if (lng < box.minLng || lng > box.maxLng) continue
      points.push({ lng, lat })
    }
  }
  return points
}

function dotsForTiers(box: BoundingBox, tiers: IsochroneTier[]): { points: DotPoint[]; spacingM: number } {
  let spacingM = initialSpacingM(box)
  let points: DotPoint[] = []
  for (let attempt = 0; attempt < 8; attempt++) {
    points = []
    for (const point of gridPoints(box, spacingM)) {
      const tier = tierAtPoint(tiers, point)
      if (tier) points.push({ lng: point.lng, lat: point.lat, tier })
    }
    if (points.length <= MAX_DOTS) break
    spacingM = Math.ceil(spacingM * Math.sqrt(points.length / MAX_DOTS) * 1.01)
  }
  return { points: points.length <= MAX_DOTS ? points : [], spacingM }
}

function dotsForSingleLayer(box: BoundingBox, geometry: MapGeometry): { points: DotPoint[]; spacingM: number } {
  let spacingM = initialSpacingM(box)
  let points: DotPoint[] = []
  for (let attempt = 0; attempt < 8; attempt++) {
    points = gridPoints(box, spacingM)
      .filter(point => pointInGeometry(geometry, point))
      .map(point => ({ lng: point.lng, lat: point.lat, tier: 'single' as const }))
    if (points.length <= MAX_DOTS) break
    spacingM = Math.ceil(spacingM * Math.sqrt(points.length / MAX_DOTS) * 1.01)
  }
  return { points: points.length <= MAX_DOTS ? points : [], spacingM }
}

/**
 * Build the dot plan for a report. Pure geography: no pixel projection, no DOM.
 *
 * `tiers` mode requires valid 5/10/15 tier polygons. Otherwise `single` draws the existing
 * 15-minute isochrone only (labelled "仅单层" by the view) and `none` draws no dots.
 */
export function buildDotPlan(report: unknown): DotPlan {
  const reportRecord = record(report)
  const tiers = parseIsochroneTiers(reportRecord)
  if (tiers.length === TIER_DEFINITIONS.length) {
    const box = unionBoundingBox(tiers.map(tier => tier.geometry))
    if (box) return { mode: 'tiers', tiers, ...dotsForTiers(box, tiers) }
  }
  const singleGeometry = parsePolygonGeometry(record(reportRecord.isochrone).geometry)
  const singleBox = singleGeometry ? unionBoundingBox([singleGeometry]) : null
  if (singleGeometry && singleBox) {
    return { mode: 'single', tiers: [], ...dotsForSingleLayer(singleBox, singleGeometry) }
  }
  return { mode: 'none', tiers: [], points: [], spacingM: null }
}
