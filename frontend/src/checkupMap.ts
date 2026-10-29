type JsonRecord = Record<string, unknown>

export const MAP_GROUP_COLORS = ['#79a9ff', '#63d9df', '#b4a0ef', '#8dd2bc', '#cbdff3', '#9684cd', '#91c9ed'] as const

export type MapPoint = { lng: number; lat: number }
export type MapPolygon = MapPoint[][]
export type MapGeometry = MapPolygon[]
export type MapPoiMembership = { facilityId: string; facilityName: string; groupId: string; groupName: string; color: string }
export type CheckupMapPoi = { id: string; name: string; address: string; location: MapPoint; inIsochrone: boolean; memberships: MapPoiMembership[] }
export type CoverageKind = 'blind' | 'unknown'
export type CoverageFeature = { id: string; facilityId: string; kind: CoverageKind; geometry: MapGeometry }

const record = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
const string = (value: unknown): string => typeof value === 'string' ? value : ''

function mapPoint(value: unknown): MapPoint | null {
  const item = record(value)
  const { lng, lat } = item
  if ((item.crs !== undefined && item.crs !== 'BD09LL') || typeof lng !== 'number' || !Number.isFinite(lng)
      || Math.abs(lng) > 180 || typeof lat !== 'number' || !Number.isFinite(lat) || Math.abs(lat) > 90) return null
  return { lng, lat }
}

export function buildCheckupMapPois(rawPois: unknown, rawFacilities: unknown, rawGroups: unknown): CheckupMapPoi[] {
  const facilities = new Map((Array.isArray(rawFacilities) ? rawFacilities : []).map(raw => {
    const item = record(raw)
    return [string(item.id), item] as const
  }))
  const groups = new Map((Array.isArray(rawGroups) ? rawGroups : []).map(raw => {
    const item = record(raw)
    return [string(item.id), item] as const
  }))
  const byId = new Map<string, CheckupMapPoi>()
  for (const [index, raw] of (Array.isArray(rawPois) ? rawPois : []).entries()) {
    const row = record(raw)
    const location = mapPoint(row.location)
    if (!location || typeof row.in_isochrone !== 'boolean') continue
    const id = string(row.poi_id) || 'poi-' + index
    const facilityId = string(row.facility_id)
    const facility = facilities.get(facilityId)
    const groupId = string(facility?.group_id) || string(row.group_id)
    const group = groups.get(groupId)
    const membership: MapPoiMembership | null = facilityId || groupId ? {
      facilityId,
      facilityName: string(facility?.name_zh) || facilityId || '未分类',
      groupId,
      groupName: string(group?.name_zh) || '未分类',
      color: string(group?.color) || '#52728f',
    } : null
    const existing = byId.get(id)
    if (existing) {
      existing.inIsochrone ||= row.in_isochrone === true
      if (membership && !existing.memberships.some(item => item.facilityId === membership.facilityId && item.groupId === membership.groupId)) {
        existing.memberships.push(membership)
      }
    } else {
      byId.set(id, {
        id,
        name: string(row.name) || '未命名设施',
        address: string(row.address),
        location,
        inIsochrone: row.in_isochrone,
        memberships: membership ? [membership] : [],
      })
    }
  }
  return [...byId.values()]
}

export function filterCheckupMapPois(pois: CheckupMapPoi[], groupFilter = 'all', showCoverageCandidates = false): CheckupMapPoi[] {
  return pois.filter(poi => (poi.inIsochrone || showCoverageCandidates)
    && (!groupFilter || groupFilter === 'all' || poi.memberships.some(item => item.groupId === groupFilter)))
}

export function parsePolygonGeometry(value: unknown): MapGeometry | null {
  let geometry = record(value)
  if (geometry.type === 'Feature') geometry = record(geometry.geometry)
  const rings = (raw: unknown): MapPolygon | null => {
    if (!Array.isArray(raw) || !raw.length) return null
    const parsed = raw.map(ring => Array.isArray(ring) ? ring.map(pair => Array.isArray(pair)
      ? mapPoint({ lng: pair[0], lat: pair[1] }) : null) : [])
    if (parsed.some(ring => ring.length < 4 || ring.some(point => !point)
        || ring[0]?.lng !== ring[ring.length - 1]?.lng || ring[0]?.lat !== ring[ring.length - 1]?.lat)) return null
    return parsed as MapPolygon
  }
  if (geometry.type === 'Polygon') {
    const polygon = rings(geometry.coordinates)
    return polygon ? [polygon] : null
  }
  if (geometry.type === 'MultiPolygon' && Array.isArray(geometry.coordinates)) {
    const polygons = geometry.coordinates.map(rings)
    return polygons.length && polygons.every(Boolean) ? polygons as MapGeometry : null
  }
  return null
}

function parseCollection(value: unknown, kind: CoverageKind): CoverageFeature[] {
  const collection = record(value)
  if (collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) return []
  return collection.features.flatMap((raw, index) => {
    const feature = record(raw)
    const geometry = parsePolygonGeometry(feature.geometry)
    const properties = record(feature.properties)
    const facilityId = string(properties.facility_type)
    if (!geometry || !facilityId) return []
    return [{
      id: string(feature.id) || string(properties.cell_id) || kind + '-' + index,
      facilityId,
      kind,
      geometry,
    }]
  })
}

export function parseCoverageLayers(value: unknown): { blindSpots: CoverageFeature[]; unknownAreas: CoverageFeature[] } {
  const report = record(value)
  const coverage = record(report.blindspot ?? value)
  return {
    blindSpots: parseCollection(coverage.blind_spots ?? report.blind_spots, 'blind'),
    unknownAreas: parseCollection(coverage.unknown_areas ?? report.unknown_areas, 'unknown'),
  }
}

export function filterCoverageFeatures(features: CoverageFeature[], facilityId: string): CoverageFeature[] {
  return facilityId ? features.filter(feature => feature.facilityId === facilityId) : []
}

// Keep each category's original rings and holes; a group has no synthetic coverage ratio.
export function filterGroupCoverageFeatures(features: CoverageFeature[], categories: { id: string; group_id: string }[], groupId: string): CoverageFeature[] {
  if (!groupId) return []
  const ids = new Set(categories.filter(category => category.group_id === groupId).map(category => category.id))
  return features.filter(feature => ids.has(feature.facilityId))
}

/** Select only the saved child evidence; missing geometry is never inferred from its parent. */
export function selectFacilityCoverage(value: unknown, facilityId: string): JsonRecord | null {
  const report = record(value)
  const category = (Array.isArray(report.categories) ? report.categories : [])
    .map(record).find(row => row.facility_id === facilityId)
  if (!category) return null
  const layers = record(report.blindspot)
  const result: JsonRecord = { ...category, definition: 'single_facility' }
  let hasGeometry = false
  let complete = true
  for (const [key, ratio] of [['covered_areas', 'covered_ratio'], ['blind_spots', 'blind_ratio'], ['unknown_areas', 'unknown_ratio']] as const) {
    const collection = record(layers[key])
    const features = (Array.isArray(collection.features) ? collection.features : []).filter(raw => {
      const feature = record(raw)
      return record(feature.properties).facility_type === facilityId && parsePolygonGeometry(feature.geometry) !== null
    })
    result[key] = { type: 'FeatureCollection', features }
    hasGeometry ||= features.length > 0
    if (typeof category[ratio] !== 'number' || (Number(category[ratio]) > 1e-8 && !features.length)) complete = false
  }
  return { ...result, geometry_available: hasGeometry, geometry_complete: complete }
}
