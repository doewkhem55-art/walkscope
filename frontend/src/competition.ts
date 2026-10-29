import { ApiError, apiBase, object } from './api.ts'
import type { JsonObject } from './api.ts'

export interface CompetitionCenter { lng: number; lat: number; crs: 'BD09LL' }
export interface CompetitionSample {
  id: string
  label: string
  center: CompetitionCenter
  collected_at: string | null
  data_date: string
  mode: 'live' | 'frozen'
  source_file: string
  source_sha256: string
}
export interface CompetitionDiagnosis { ok: true; data: JsonObject; warnings: unknown; meta: unknown }
export interface CompetitionPacket {
  schema_version: 'competition-demo-1'
  sample: CompetitionSample
  diagnosis: CompetitionDiagnosis
}

const invalid = () => new ApiError('contract', '历史分析记录与预期接口不符，请联系服务维护人员核对版本。')
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const validDate = (value: unknown): value is string => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) return false
  const date = new Date(`${value}T00:00:00.000Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}
const validTimestamp = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value))
const hasTimezone = (value: string) => /(?:Z|[+-]\d{2}:\d{2})$/.test(value)

function center(value: unknown): CompetitionCenter {
  const v = object(value)
  if (!finite(v.lng) || !finite(v.lat) || v.lng < -180 || v.lng > 180 || v.lat < -90 || v.lat > 90 || v.crs !== 'BD09LL') throw invalid()
  return { lng: v.lng, lat: v.lat, crs: 'BD09LL' }
}

const sameCenter = (a: CompetitionCenter, b: CompetitionCenter) => a.lng === b.lng && a.lat === b.lat && a.crs === b.crs
const validPosition = (v: unknown): v is number[] => Array.isArray(v) && v.length === 2 && finite(v[0]) && finite(v[1]) && v[0] >= -180 && v[0] <= 180 && v[1] >= -90 && v[1] <= 90

function validRing(value: unknown): boolean {
  if (!Array.isArray(value) || value.length < 4 || !value.every(validPosition)) return false
  const first = value[0] as number[]
  const last = value[value.length - 1] as number[]
  return first[0] === last[0] && first[1] === last[1] && new Set(value.slice(0, -1).map(p => (p as number[]).join(','))).size >= 3
}

function validGeometry(value: unknown, isochrone = false): boolean {
  const geometry = object(value)
  if (geometry.type === 'Polygon') {
    const rings = geometry.coordinates
    return Array.isArray(rings) && rings.length > 0 && (!isochrone || rings.length === 1) && rings.every(validRing)
  }
  if (!isochrone && geometry.type === 'MultiPolygon') {
    const polygons = geometry.coordinates
    return Array.isArray(polygons) && polygons.length > 0 && polygons.every(polygon =>
      Array.isArray(polygon) && polygon.length > 0 && polygon.every(validRing))
  }
  return false
}

function validateFeatures(value: unknown): void {
  const collection = object(value)
  if (collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) throw invalid()
  for (const raw of collection.features) {
    const feature = object(raw)
    if (feature.type !== 'Feature' || !validGeometry(feature.geometry)) throw invalid()
  }
}

export interface DiagnosisView {
  diagnosis: CompetitionDiagnosis
  center: CompetitionCenter
  label: string
  source: string
  dataDate?: string
  collectedAt?: string | null
}

export function calculateReferenceScore(metrics: Record<string, unknown>[]): number | null {
  const eligible = metrics.filter(metric =>
    typeof metric.covered_ratio === 'number' && Number.isFinite(metric.covered_ratio) &&
    typeof metric.unknown_ratio === 'number' && Number.isFinite(metric.unknown_ratio),
  )
  if (eligible.length !== 3 || new Set(eligible.map(metric => metric.facility_type)).size !== 3) return null
  const lower = eligible.reduce((sum, metric) => sum + Number(metric.covered_ratio), 0) / 3
  const upper = eligible.reduce((sum, metric) => sum + Math.min(1, Number(metric.covered_ratio) + Number(metric.unknown_ratio)), 0) / 3
  return Math.min(100, Math.max(0, Math.round(((lower + upper) * 50) / 2.5) * 2.5))
}

export function parseDiagnosis(value: unknown): CompetitionDiagnosis {
  const diagnosis = object(value)
  if (diagnosis.ok !== true || !Array.isArray(diagnosis.warnings) || object(diagnosis.meta).schema_version !== '1.0') throw invalid()
  const data = object(diagnosis.data)
  const dataCenter = center(data.center)
  validateMetrics(data.metrics)
  const isochrone = object(data.isochrone)
  if (!Number.isInteger(isochrone.time_limit_s) || (isochrone.time_limit_s as number) !== 900 || !validGeometry(isochrone.geometry, true)) throw invalid()
  if (!sameCenter(dataCenter, center(isochrone.center))) throw invalid()
  if (data.blindspot != null) {
    const blindspot = object(data.blindspot)
    if (!Number.isInteger(blindspot.service_distance_m) || !Number.isInteger(blindspot.grid_size_m) ||
        (blindspot.service_distance_m as number) <= 0 || (blindspot.grid_size_m as number) <= 0) throw invalid()
    if (!sameCenter(dataCenter, center(blindspot.center)) || !validGeometry(blindspot.analysis_geometry, true)) throw invalid()
    validateFeatures(blindspot.blind_spots)
    validateFeatures(blindspot.unknown_areas)
  }
  return { ok: true, data, warnings: diagnosis.warnings, meta: diagnosis.meta }
}

export function viewFromCompetition(packet: CompetitionPacket): DiagnosisView {
  return {
    diagnosis: packet.diagnosis,
    center: packet.sample.center,
    label: packet.sample.label,
    source: '保存的历史分析',
    dataDate: packet.sample.data_date,
    collectedAt: packet.sample.collected_at,
  }
}

export async function requestDiagnosis(
  base: string,
  location: { type: 'address'; query: string; city?: string } | { type: 'coordinate'; lng: number; lat: number; crs: 'BD09LL' },
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
  timeoutMs = 180000,
): Promise<CompetitionDiagnosis> {
  const url = new URL('/api/v1/diagnosis', apiBase(base))
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timer = setTimeout(abort, timeoutMs)
  try {
    const response = await fetcher(url, {
      method: 'POST', signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema_version: '1.0', location, facility_types: ['market', 'pharmacy', 'primary_school'] }),
    })
    if (signal.aborted) throw new ApiError('stopped', '已停止等待体检结果。')
    let value: unknown
    try { value = await response.json() } catch { throw new ApiError('contract', '体检服务返回的数据无法读取。') }
    if (signal.aborted) throw new ApiError('stopped', '已停止等待体检结果。')
    const envelope = object(value)
    if (envelope.ok === false) {
      const detail = object(envelope.error)
      throw new ApiError('diagnosis', typeof detail.message === 'string' ? detail.message : '这个地点暂时无法完成体检。')
    }
    if (!response.ok) throw new ApiError(String(response.status), `体检服务暂不可用（HTTP ${response.status}）。`)
    return parseDiagnosis(value)
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (signal.aborted) throw new ApiError('stopped', '已停止等待体检结果。')
    if (controller.signal.aborted) throw new ApiError('timeout', '体检等待超时。服务端可能仍在处理，请先确认结果再重新提交。')
    throw new ApiError('network', '无法连接体检服务，请检查后端地址、网络和跨域配置。')
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', abort)
  }
}

function validateMetrics(value: unknown): void {
  if (!Array.isArray(value) || value.length < 1 || value.length > 3) throw invalid()
  const integerFields = ['poi_count', 'confirmed_reachable_15m_count', 'unresolved_route_count', 'nearest_walk_distance_m', 'nearest_walk_duration_s']
  const ratioFields = ['covered_ratio', 'blind_ratio', 'unknown_ratio']
  for (const raw of value) {
    const metric = object(raw)
    if (!nonempty(metric.facility_type)) throw invalid()
    for (const key of integerFields) {
      const item = metric[key]
      if (item !== null && (!finite(item) || !Number.isInteger(item) || item < 0)) throw invalid()
    }
    for (const key of ratioFields) {
      const item = metric[key]
      if (item !== null && (!finite(item) || item < 0 || item > 1)) throw invalid()
    }
    const ratios = ratioFields.map(key => metric[key])
    if (ratios.some(item => item === null) && ratios.some(item => item !== null)) throw invalid()
    if (ratios.every(item => item !== null) && Math.abs(Number(ratios[0]) + Number(ratios[1]) + Number(ratios[2]) - 1) > 1e-4) throw invalid()
    if ((metric.nearest_walk_distance_m === null) !== (metric.nearest_walk_duration_s === null)) throw invalid()
  }
}

export function parseCompetition(value: unknown): CompetitionPacket {
  const packet = object(value)
  const sample = object(packet.sample)
  const diagnosis = object(packet.diagnosis)
  if (packet.schema_version !== 'competition-demo-1' || !nonempty(sample.id) || !nonempty(sample.label) ||
      !nonempty(sample.source_file) || !validDate(sample.data_date) ||
      !(sample.collected_at === null || validTimestamp(sample.collected_at)) ||
      !['live', 'frozen'].some(mode => sample.mode === mode) || (sample.mode === 'live' && sample.collected_at === null) ||
      (sample.mode === 'live' && !(typeof sample.collected_at === 'string' && hasTimezone(sample.collected_at))) ||
      typeof sample.source_sha256 !== 'string' || !/^[a-f\d]{64}$/i.test(sample.source_sha256)) throw invalid()
  const sampleCenter = center(sample.center)
    const parsedDiagnosis = parseDiagnosis(diagnosis)
  const data = parsedDiagnosis.data
  const dataCenter = center(data.center)
  if (!sameCenter(sampleCenter, dataCenter)) throw invalid()

  return {
    schema_version: 'competition-demo-1',
    sample: {
      id: sample.id,
      label: sample.label,
      center: sampleCenter,
      collected_at: sample.collected_at as string | null,
      data_date: sample.data_date,
      mode: sample.mode as 'live' | 'frozen',
      source_file: sample.source_file,
      source_sha256: sample.source_sha256,
    },
    diagnosis: parsedDiagnosis,
  }
}

export async function fetchCompetition(
  base: string, signal: AbortSignal, fetcher: typeof fetch = fetch, timeoutMs = 15000,
): Promise<CompetitionPacket> {
  const url = new URL('/api/demo/sample', apiBase(base))
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timer = setTimeout(abort, timeoutMs)
  try {
    const response = await fetcher(url, {
      method: 'GET', signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      headers: { Accept: 'application/json' },
    })
    if (signal.aborted) throw new ApiError('stopped', '已停止读取固定样例。')
    if (controller.signal.aborted) throw new ApiError('timeout', '读取固定样例超时，请检查后端地址和网络。')
    if (!response.ok) throw new ApiError(String(response.status), `固定样例暂不可用（HTTP ${response.status}）。`)
    let value: unknown
    try { value = await response.json() } catch { throw invalid() }
    if (signal.aborted) throw new ApiError('stopped', '已停止读取固定样例。')
    if (controller.signal.aborted) throw new ApiError('timeout', '读取固定样例超时，请检查后端地址和网络。')
    return parseCompetition(value)
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (signal.aborted) throw new ApiError('stopped', '已停止读取固定样例。')
    if (controller.signal.aborted) throw new ApiError('timeout', '读取固定样例超时，请检查后端地址和网络。')
    throw new ApiError('network', '无法连接固定样例接口，请检查后端地址、网络和跨域配置。')
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', abort)
  }
}
