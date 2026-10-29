<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { loadBaiduMap, metricScale, zoomMapView } from './baidu.ts'
import type { BaiduMap, BaiduNamespace } from './baidu.ts'
import type { CheckupFacility, CheckupGroup, MapReport } from './checkups.ts'
import { MAP_GROUP_COLORS, buildCheckupMapPois, filterCheckupMapPois, parseCoverageLayers, parsePolygonGeometry, selectFacilityCoverage } from './checkupMap.ts'
import type { CheckupMapPoi, MapGeometry } from './checkupMap.ts'
import { buildDotPlan, coverageAtPoint, dotRadiusForSpacing, dotStyleFor, pointInGeometry, TIER_DEFINITIONS } from './mapDots.ts'
import type { DotPoint, TimeTierId } from './mapDots.ts'
import { DARK_MAP_FILTER_CSS, applyDarkMapStyle, resolveDarkMapMode } from './mapStyle.ts'
import { createOfflineProjection } from './offlineProjection.ts'
import { indexHoverDots, nearbyHoverDots } from './mapDotInteraction.ts'
import ResizeGrip from './ResizeGrip.vue'

type Center = { lng: number; lat: number; crs?: string }
const props = defineProps<{ center: Center; caption: string; showCenter?: boolean; selectable?: boolean; selection?: Center | null; report?: MapReport | null; groups?: CheckupGroup[]; facilities?: CheckupFacility[]; groupFilter?: string }>()
const emit = defineEmits<{ select: [center: Center]; 'update:groupFilter': [value: string]; 'poi-select': [id: string | null] }>()
const host = ref<HTMLDivElement | null>(null)
const dotLayer = ref<SVGGElement | null>(null)
const state = ref<'loading' | 'ready' | 'offline'>('loading')
const viewport = ref({ width: 1200, height: 800 })
const offlineView = ref({ zoom: 1, panX: 0, panY: 0 })
const error = ref('')
const key = import.meta.env.VITE_BAIDU_MAP_AK?.trim() ?? ''
let sdk: BaiduNamespace | undefined
let map: BaiduMap | undefined
let observer: ResizeObserver | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let stopped = false
const projectionVersion = ref(0)
const layersOpen = ref(false)
const layerHeading = ref<HTMLElement | null>(null)
watch(layersOpen, async open => {
  if (open) { await nextTick(); layerHeading.value?.focus?.({ preventScroll: true }) }
})
function toggleLayers() { layersOpen.value = !layersOpen.value }
defineExpose({ toggleLayers, layersOpen })
const record = (value: unknown) => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const text = (value: unknown) => typeof value === 'string' ? value : ''
const point = (value: unknown): Center | null => {
  const valueRecord = record(value)
  const lng = valueRecord.lng, lat = valueRecord.lat
  return typeof lng === 'number' && Number.isFinite(lng) && Math.abs(lng) <= 180
      && typeof lat === 'number' && Number.isFinite(lat) && Math.abs(lat) <= 90
      && (valueRecord.crs === undefined || valueRecord.crs === 'BD09LL') ? { lng, lat } : null
}
const reportGeometry = computed(() => parsePolygonGeometry(record(props.report?.isochrone).geometry))
const isochronePoints = computed(() => reportGeometry.value?.flat(2) ?? [])
const activeGroupIds = computed(() => new Set((props.report?.groups ?? []).map(group => text(group.id))))
const groupsWithColors = computed(() => (props.groups ?? [])
  .map((group, index) => ({ ...group, color: MAP_GROUP_COLORS[index % MAP_GROUP_COLORS.length], number: index + 1 }))
  .filter(group => !props.report || activeGroupIds.value.has(group.id)))
const reportPois = computed<CheckupMapPoi[]>(() => isOfflineRenderSample.value ? [] : buildCheckupMapPois(props.report?.pois, props.facilities, groupsWithColors.value))
// Keep the initial map light; saved out-of-circle candidates remain available in layers.
const showCoverageCandidates = ref(false)
// Offline render samples (stage-3 tiers demo) borrow geometry from one city and POIs
// from another; drawing those POIs would place facilities in the wrong area.
const isOfflineRenderSample = computed(() => (props.report?.quality as Record<string, unknown> | undefined)?.offline_render_sample === true)
const showFacilities = ref(true)
const selectedCoverageGroupId = ref('')
const selectedCoverageFacilityId = ref('')
const coverageFacilities = computed(() => props.facilities?.length ? props.facilities :
  (props.report?.categories ?? []).map(category => ({ id: category.facility_id, group_id: category.group_id, name_zh: category.name_zh ?? category.facility_id })))
const facilitiesForGroup = (id: string) => coverageFacilities.value.filter(facility => facility.group_id === id)
const selectedFacilityName = computed(() => coverageFacilities.value.find(f => f.id === selectedCoverageFacilityId.value)?.name_zh ?? selectedCoverageFacilityId.value)
const coveragePercent = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(2)}%` : '未提供'

const selectedCoverage = computed(() => {
  if (selectedCoverageFacilityId.value) return selectFacilityCoverage(props.report, selectedCoverageFacilityId.value)
  const blindspot = record(props.report?.blindspot)
  const aggregate = record(selectedCoverageGroupId.value === 'total'
    ? blindspot.total_coverage : record(blindspot.group_coverage)[selectedCoverageGroupId.value])
  return aggregate.definition === 'all_categories_confirmed_blind' ? aggregate : null
})
const includesUnknownInBlind = computed(() => selectedCoverage.value?.display_policy === 'include_unknown')
const hasUnverifiedDisplay = computed(() => (Boolean(selectedCoverageFacilityId.value) || includesUnknownInBlind.value) && Number(selectedCoverage.value?.unknown_ratio) > 1e-8)
const unverifiedInsidePoiCount = computed(() => {
  // The stored audit is group-level; it cannot certify or block a child's independent evidence.
  if (selectedCoverageFacilityId.value) return 0
  const audit = record(record(props.report?.quality).coverage_validation)
  const selected = selectedCoverageGroupId.value === 'total'
    ? audit : record(record(audit.groups)[selectedCoverageGroupId.value])
  return Number(selected.unverified_poi_count) || 0
})
const coverageBlocked = computed(() => unverifiedInsidePoiCount.value > 0)
const coverageLayers = computed(() => parseCoverageLayers(includesUnknownInBlind.value
  ? { ...selectedCoverage.value, blind_spots: selectedCoverage.value?.display_blind_spots }
  : selectedCoverage.value))
const visibleBlindSpots = computed(() => selectedCoverageGroupId.value && !coverageBlocked.value ? coverageLayers.value.blindSpots : [])
const visibleUnknownAreas = computed(() => selectedCoverageGroupId.value && (selectedCoverageFacilityId.value || !includesUnknownInBlind.value) ? coverageLayers.value.unknownAreas : [])
const coverageStatus = computed(() => {
  if (selectedCoverageFacilityId.value) {
    const category = selectedCoverage.value
    const ratios = category ? `覆盖 ${coveragePercent(category.covered_ratio)} · 确认盲区 ${coveragePercent(category.blind_ratio)} · 未知 ${coveragePercent(category.unknown_ratio)}` : ''
    if (!category?.geometry_available) return `${selectedFacilityName.value}：该小类地图边界尚未具备可核验数据。${ratios}`
    const note = category.geometry_complete ? '' : ' · 部分边界缺失，仅显示已保存几何'
    return `${selectedFacilityName.value} · ${ratios}${note}${hasUnverifiedDisplay.value ? ' · 浅蓝高亮含未核验区域' : ''}`
  }
  if (!selectedCoverageGroupId.value) return '盲区已关闭 · 选择分组或总盲区查看'
  if (!selectedCoverage.value) return '暂无该范围汇总覆盖数据，无法判断服务盲区；未知不能视为盲区'
  if (coverageBlocked.value) return `覆盖未完成核验：${unverifiedInsidePoiCount.value} 个圈内设施尚未通过服务区检查，盲区高亮暂不可用。`
  if (includesUnknownInBlind.value) {
    const ratio = selectedCoverage.value.display_blind_ratio
    const label = hasUnverifiedDisplay.value ? '盲区（含未核验区域）' : '已核验共同服务盲区'
    return typeof ratio === 'number' && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1
      ? `${label} · ${(ratio * 100).toFixed(1)}%`
      : `${label} · 暂无面积统计`
  }
  const scope = selectedCoverageGroupId.value === 'total' ? '所选全部小项' : '本组所选全部小项'
  const hasUnknown = visibleUnknownAreas.value.length > 0 || Number(selectedCoverage.value.unknown_ratio) > 0
  const unknown = hasUnknown ? '；未知区域证据不足，不能视为盲区' : ''
  if (visibleBlindSpots.value.length) return `${scope}均确认无服务的共同盲区${unknown}`
  if (Number(selectedCoverage.value.blind_ratio) > 0) return `${scope}存在共同盲区统计，暂无可显示的盲区几何${unknown}`
  return `暂无已确认盲区：未发现${scope}均确认无服务的共同盲区，不代表全部覆盖${unknown}`
})
const coverageDescription = computed(() => selectedCoverageFacilityId.value
  ? '仅使用本小类已保存的测算结果。确认盲区与未核验区域统一使用浅蓝呼吸点阵；同色仅为展示，未知不等于已确认缺失。大类POI检查通过不能替代本小类完整核验。'
  : coverageBlocked.value
  ? '设施点和原始未知数据保留。补齐实测并通过圈内设施覆盖检查后，才启用本组盲区高亮。'
  : hasUnverifiedDisplay.value
  ? '生活圈内属于服务区之外的点阵同步呼吸并轻微浮动。服务区按设施起点和报告标注的距离口径计算，未核验部分仍按当前口径高亮；未核验区域不表示已确认无服务。'
  : includesUnknownInBlind.value
    ? '高亮为所选相关小项均未提供1公里步行服务的共同盲区，按实测距离插值估算；本组未核验面积接近0。'
  : '生活圈内盲区点阵同步呼吸并轻微浮动，原点阵位置保持不变；未知区域不作盲区高亮。所选相关小项均确认无服务的共同盲区才高亮。任一小项已有服务的区域不计入共同盲区。')
// Map filters are view-only. They never change the next checkup's analysis scope.
const visibleGroupIds = ref<string[] | null>(null)
let emittedGroupFilter: string | undefined
function publishGroupFilter() {
  const ids = visibleGroupIds.value
  const value = ids?.length === 1 ? ids[0]! : 'all'
  if (value === props.groupFilter) return
  emittedGroupFilter = value
  emit('update:groupFilter', value)
}
function changeCoverageGroup(value: Event | string) {
  const groupId = typeof value === 'string' ? value : (value.target as HTMLSelectElement).value
  selectedCoverageFacilityId.value = ''
  selectedCoverageGroupId.value = groupId
  visibleGroupIds.value = groupId && groupId !== 'total' ? [groupId] : null
  publishGroupFilter()
}
function selectCoverageFacility(facilityId: string) {
  const facility = coverageFacilities.value.find(row => row.id === facilityId)
  if (!facility || !groupsWithColors.value.some(group => group.id === facility.group_id)) return
  selectedCoverageFacilityId.value = facility.id
  selectedCoverageGroupId.value = facility.group_id
  visibleGroupIds.value = [facility.group_id]
  publishGroupFilter()
}
watch(() => props.groupFilter, groupId => {
  if (groupId === emittedGroupFilter) { emittedGroupFilter = undefined; return }
  selectedCoverageFacilityId.value = ''
  visibleGroupIds.value = groupId && groupId !== 'all' ? [groupId] : null
  selectedCoverageGroupId.value = visibleGroupIds.value?.[0] ?? ''
}, { immediate: true })
const filteredPois = computed(() => {
  const ids = visibleGroupIds.value === null ? null : new Set(visibleGroupIds.value)
  return filterCheckupMapPois(reportPois.value, 'all', showCoverageCandidates.value)
    .filter(poi => ids === null || poi.memberships.some(member => ids.has(member.groupId)))
    .filter(poi => !selectedCoverageFacilityId.value || poi.memberships.some(member => member.facilityId === selectedCoverageFacilityId.value))
})
const coverageGridLabel = computed(() => {
  const quality = record(props.report?.quality)
  if (quality.coverage_geometry_method === 'measured_distance_triangles_1000m_isoline_v1') return '1000米步行等距线 · 采样估算'
  const value = quality.grid_size_m
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? `${value}米网格估算 · 非道路边界` : '网格估算 · 非道路边界'
})

// --- Dark basemap -----------------------------------------------------------------
// Browser evidence in 阶段4a showed the SDK silently ignoring setMapStyleV2/setMapStyle:
// calling them without throwing is NOT proof the map turned dark (the current browser AK
// has no personalised-map permission). The default is therefore the verifiable CSS filter
// on the tile canvas; the SDK style is used only when explicitly requested AND confirmed
// (VITE_DARK_MAP_MODE=custom + VITE_DARK_MAP_CUSTOM_CONFIRMED=true). `mapStyleMode` is
// exposed as data-map-style so QA reads the actual path, never the requested one.
const darkMapMode = resolveDarkMapMode(import.meta.env)
const mapStyleMode = ref<'pending' | 'custom' | 'filter'>(darkMapMode === 'filter' ? 'filter' : 'pending')
const mapStyleLabel = computed(() => mapStyleMode.value === 'custom'
  ? '底图：自定义深色样式（已显式确认）'
  : mapStyleMode.value === 'filter'
    ? '底图：深色滤镜（默认可验证）'
    : '底图：深色样式初始化中')
// Expose the exact filter value so browser QA can prove the dark path from the DOM, not
// only from a screenshot (the tile canvas has no canvas element for generic probes).
const mapFilterCss = computed(() => mapStyleMode.value === 'filter' ? DARK_MAP_FILTER_CSS : 'none')

// --- Rule dot matrix (阶段4a) -----------------------------------------------------
// Equal-size circles on one geographic grid; only blue shade changes by walk-time band.
// `buildDotPlan` only produces tiers when the report really carries 5/10/15 tier polygons;
// otherwise it returns the existing 15-minute layer as `single` and the legend says 仅单层.
// Category progress snapshots do not rebuild unchanged geometry or its geographic grid.
const dotGeometryKey = computed(() => JSON.stringify(props.report ? {
  isochrone: props.report.isochrone,
  tiers: record(props.report).tiers,
  walk_tiers: record(props.report).walk_tiers,
  tiered_isochrone: record(props.report).tiered_isochrone,
} : null))
const dotPlan = computed(() => buildDotPlan(JSON.parse(dotGeometryKey.value)))
const dotKey = (point: DotPoint) => `${point.tier}:${point.lng.toFixed(6)}:${point.lat.toFixed(6)}`
const blindDotIds = computed(() => {
  const circle = reportGeometry.value
  const blind = visibleBlindSpots.value.map(feature => feature.geometry)
  if (!circle || !blind.length) return new Set<string>()
  const unknown = visibleUnknownAreas.value.map(feature => feature.geometry)
  return new Set(dotPlan.value.points.filter(point => pointInGeometry(circle, point)
    && coverageAtPoint(point, blind, unknown) === 'blind').map(dotKey))
})
const unknownDotIds = computed(() => {
  if (!selectedCoverageFacilityId.value) return new Set<string>()
  const circle = reportGeometry.value
  const unknown = visibleUnknownAreas.value.map(feature => feature.geometry)
  if (!circle || !unknown.length) return new Set<string>()
  return new Set(dotPlan.value.points.filter(point => pointInGeometry(circle, point)
    && unknown.some(geometry => pointInGeometry(geometry, point))).map(dotKey))
})
const visibleTierIds = ref<TimeTierId[]>(TIER_DEFINITIONS.map(tier => tier.id))
const showTierOutlines = ref(false)
const tierOutlines = computed(() => dotPlan.value.mode === 'tiers'
  ? dotPlan.value.tiers.map(tier => ({ id: tier.id, path: pathForGeometry(tier.geometry), color: tier.color }))
  : [])
const projectedDotRadius = computed(() => {
  void projectionVersion.value
  const spacingM = dotPlan.value.spacingM
  if (!spacingM || state.value === 'loading') return 2.2
  const center = analysisCenter.value
  const origin = project(center)
  const east = project({ lng: center.lng + spacingM / (111320 * Math.max(.2, Math.cos(center.lat * Math.PI / 180))), lat: center.lat })
  return dotRadiusForSpacing(Math.hypot(east.x-origin.x, east.y-origin.y))
})
const visibleDots = computed(() => {
  void projectionVersion.value
  if (state.value === 'loading' || dotPlan.value.mode === 'none') return []
  const visible = new Set(visibleTierIds.value)
  const radius = projectedDotRadius.value
  return dotPlan.value.points.flatMap(point => {
    if (point.tier !== 'single' && !visible.has(point.tier)) return []
    const pixel = project(point)
    if (!Number.isFinite(pixel.x) || !Number.isFinite(pixel.y)
        || pixel.x < -radius || pixel.y < -radius || pixel.x > viewport.value.width + radius || pixel.y > viewport.value.height + radius) return []
    return [{ tier: point.tier, id: dotKey(point), x: pixel.x, y: pixel.y,
      ...dotStyleFor(point), r: radius }]
  })
})

// Hover uses a small screen-space index and local DOM styles, never reactive per-dot state.
let hoverIndex = indexHoverDots([])
let hoverNodes = new Map<string, SVGCircleElement>()
const activeHoverNodes = new Set<SVGCircleElement>()
let hoverFrame: number | undefined
let hoverPointer: { x: number; y: number } | null = null
let lastHoverAt = 0
let motionQuery: MediaQueryList | undefined
const poiPointer = ref<{ x: number; y: number } | null>(null)
function clearDotHover() {
  if (hoverFrame !== undefined && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(hoverFrame)
  hoverFrame = undefined
  hoverPointer = null
  for (const node of activeHoverNodes) {
    node.style.removeProperty('--dot-hover-scale')
  }
  activeHoverNodes.clear()
}
function clearMapHover() {
  clearDotHover()
  poiPointer.value = null
}
function rebuildDotHoverIndex() {
  clearDotHover()
  hoverNodes = new Map()
  const screenDots = Array.from(dotLayer.value?.querySelectorAll<SVGCircleElement>('circle') ?? []).map(node => {
    const id = node.getAttribute('data-dot-id') ?? ''
    hoverNodes.set(id, node)
    return { id, x: Number(node.getAttribute('cx')), y: Number(node.getAttribute('cy')) }
  })
  hoverIndex = indexHoverDots(screenDots)
}
function animateDotHover(timestamp: number) {
  hoverFrame = undefined
  if (stopped || !hoverPointer || motionQuery?.matches || typeof document !== 'undefined' && document.hidden) { clearDotHover(); return }
  if (timestamp - lastHoverAt < 32) { hoverFrame = requestAnimationFrame(animateDotHover); return }
  lastHoverAt = timestamp
  const nextNodes = new Set<SVGCircleElement>()
  for (const dot of nearbyHoverDots(hoverIndex, hoverPointer.x, hoverPointer.y, 52, 96)) {
    const node = hoverNodes.get(dot.id)
    if (!node) continue
    node.style.setProperty('--dot-hover-scale', (1 - .64 * dot.strength).toFixed(3))
    nextNodes.add(node)
  }
  for (const node of activeHoverNodes) if (!nextNodes.has(node)) {
    node.style.removeProperty('--dot-hover-scale')
  }
  activeHoverNodes.clear()
  for (const node of nextNodes) activeHoverNodes.add(node)
}
function hoverDots(event: PointerEvent) {
  const overPoi = (event.target as Element | null)?.closest?.('.checkup-poi')
  if (overPoi && event.pointerType === 'mouse' && event.buttons === 0
      && !(typeof document !== 'undefined' && document.hidden)) {
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
    poiPointer.value = { x: event.clientX - bounds.left, y: event.clientY - bounds.top }
  } else poiPointer.value = null
  if (event.pointerType !== 'mouse' || event.buttons !== 0 || !isMapGesture(event)
      || overPoi || motionQuery?.matches
      || typeof document !== 'undefined' && document.hidden) { clearDotHover(); return }
  if (typeof requestAnimationFrame !== 'function') return
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
  hoverPointer = { x: event.clientX-bounds.left, y: event.clientY-bounds.top }
  if (hoverFrame === undefined) hoverFrame = requestAnimationFrame(animateDotHover)
}
watch(visibleDots, rebuildDotHoverIndex, { flush: 'post' })
function toggleTier(id: TimeTierId, event: Event) {
  const checked = (event.target as HTMLInputElement | null)?.checked === true
  const next = new Set(visibleTierIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  visibleTierIds.value = TIER_DEFINITIONS.map(tier => tier.id).filter(tierId => next.has(tierId))
}

const visibleReportPois = computed(() => {
  void projectionVersion.value
  if (!showFacilities.value) return []
  return filteredPois.value.flatMap(poi => {
    if (state.value === 'loading') return []
    const pixel = project(poi.location)
    const membership = poi.memberships.find(item => visibleGroupIds.value === null || visibleGroupIds.value.includes(item.groupId))
    const group = groupsWithColors.value.find(group => group.id === membership?.groupId)
    return Number.isFinite(pixel.x) && Number.isFinite(pixel.y) ? [{ ...poi, color: membership?.color || '#9fdcff', number: group?.number, pixel }] : []
  })
})
const reportCenter = computed(() => point(props.report?.center) ?? props.center)
const analysisCenter = computed(() => props.report ? reportCenter.value : props.center)
const reportLocations = computed(() => [reportCenter.value, ...isochronePoints.value])
const offlineProjection = computed(() => createOfflineProjection(analysisCenter.value, reportLocations.value,
  { ...viewport.value, left: 48, right: 48, top: 90, bottom: 90 }, offlineView.value))
function project(location: Center) {
  return map && sdk ? map.pointToPixel(new sdk.Point(location.lng, location.lat))
    : offlineProjection.value.pointToPixel(location)
}
function pathForGeometry(geometry: MapGeometry | null) {
  void projectionVersion.value
  if (state.value === 'loading' || !geometry) return ''
  return geometry.flatMap(polygon => polygon.map(ring => ring.map((location, index) => {
    const pixel = project(location)
    return `${index ? 'L' : 'M'}${pixel.x.toFixed(1)} ${pixel.y.toFixed(1)}`
  }).join('') + 'Z')).join('')
}
const isochronePath = computed(() => pathForGeometry(reportGeometry.value))
const highlightedBlindDots = computed(() => visibleDots.value.filter(dot => blindDotIds.value.has(dot.id)))
const highlightedUnknownDots = computed(() => visibleDots.value.filter(dot => unknownDotIds.value.has(dot.id)))
const selectedPoiId = ref('')
const selectedPoi = computed(() => filteredPois.value.find(poi => poi.id === selectedPoiId.value))
watch(() => selectedPoi.value?.id ?? null, id => emit('poi-select', id))
const poiDetailsOpen = ref(true)
const poiDetailsMaximized = ref(false)
watch(() => selectedPoi.value?.id, id => {
  if (id) {
    poiDetailsOpen.value = true
    poiDetailsMaximized.value = false
  }
})
const selectedRoute = computed(() => selectedPoi.value && props.report?.routes.find(route => text(route.target_id) === selectedPoi.value!.id))
const selectedPoiGroups = computed(() => [...new Set(selectedPoi.value?.memberships.map(item => item.groupName) ?? [])].join('、') || '未分类')
const selectedPoiFacilities = computed(() => [...new Set(selectedPoi.value?.memberships.map(item => item.facilityName) ?? [])].join('、') || '未分类')
const duration = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0
  ? `${Math.floor(value / 60)}分${Math.round(value % 60)}秒` : '—'
const routeText = (value: unknown) => ({ success: '已获取步行路线', no_route: '未找到可用路线', unavailable: '算路暂不可用' }[String(value)] ?? '路线状态未知')
const selectedPoiStatus = computed(() => selectedPoi.value && !selectedPoi.value.inIsochrone
  ? '圈外覆盖候选（不计入圈内）' : routeText(selectedRoute.value?.status))
const markerStyle = computed(() => {
  void projectionVersion.value
  if (state.value === 'loading' || !props.showCenter) return undefined
  const pixel = project(analysisCenter.value)
  return Number.isFinite(pixel.x) && Number.isFinite(pixel.y)
    ? { left: `${pixel.x}px`, top: `${pixel.y}px` }
    : undefined
})
const selectionStyle = computed(() => {
  void projectionVersion.value
  const location = point(props.selection)
  if (state.value === 'loading' || !location) return undefined
  const pixel = project(location)
  return Number.isFinite(pixel.x) && Number.isFinite(pixel.y) ? { left: `${pixel.x}px`, top: `${pixel.y}px` } : undefined
})
// Throttle projection recomputation to one frame: a 500+ dot layer must not force a Vue
// patch for every raw `moving` event. The geographic points never change here, so there is
// no drift: only their projected pixels follow the basemap.
let frame: ReturnType<typeof setTimeout> | undefined
let frameUsesRaf = false
const updateMarker = () => {
  if (frame !== undefined) return
  if (typeof requestAnimationFrame === 'function') {
    frameUsesRaf = true
    frame = requestAnimationFrame(() => { frame = undefined; if (!stopped) projectionVersion.value++ })
  } else {
    frameUsesRaf = false
    frame = setTimeout(() => { frame = undefined; if (!stopped) projectionVersion.value++ }, 16)
  }
}
const mapEvents = ['moving', 'moveend', 'zooming', 'zoomend', 'resize']
function mapClick(event?: unknown) {
  if (!props.selectable || !event || typeof event !== 'object') return
  const value = event as { latlng?: Center; point?: Center }
  const point = value.latlng ?? value.point
  if (point && Number.isFinite(point.lng) && Number.isFinite(point.lat)
      && Math.abs(point.lng) <= 180 && Math.abs(point.lat) <= 90) {
    emit('select', { lng: point.lng, lat: point.lat, crs: 'BD09LL' })
  }
}

function fail() {
  state.value = 'offline'
  error.value = key ? '道路底图暂不可用，当前仅回放保存的范围与设施。' : '未配置道路底图，当前仅回放保存的范围与设施。'
  clearTimeout(timer)
  map?.removeEventListener('tilesloaded', ready)
  map?.removeEventListener('click', mapClick)
  for (const event of mapEvents) map?.removeEventListener(event, updateMarker)
  map?.destroy()
  map = undefined
}
function ready() {
  if (stopped || !map) return
  clearTimeout(timer)
  state.value = 'ready'
  fitInitialReport()
}
function locate(center: Center) {
  if (!map || !sdk || center.crs && center.crs !== 'BD09LL') return
  if (!Number.isFinite(center.lng) || !Number.isFinite(center.lat) || Math.abs(center.lng) > 180 || Math.abs(center.lat) > 90) return
  map.centerAndZoom(new sdk.Point(center.lng, center.lat), 15)
}
function fitReport() {
  if (!map || !sdk || !props.report || reportLocations.value.length < 2) { updateMarker(); return }
  const width = host.value?.clientWidth ?? 1000
  const narrow = width < 960
  map.setViewport(reportLocations.value.map(location => new sdk!.Point(location.lng, location.lat)), {
    margins: narrow ? [76, 22, 110, 22] : [90, 180, 90, 180],
    enableAnimation: false,
  })
  updateMarker()
}
let fittedReportId: string | null = null
function fitInitialReport() {
  const reportId = props.report?.report_id
  if (state.value !== 'ready' || !reportId || reportId === fittedReportId || !map || !sdk || !reportGeometry.value) return
  fitReport()
  fittedReportId = reportId
}
watch(() => `${props.center.lng}:${props.center.lat}:${props.center.crs}`, () => locate(props.center))
watch(() => props.selection, updateMarker, { deep: true, flush: 'post' })
watch(() => props.showCenter, updateMarker, { flush: 'post' })
watch([filteredPois, showFacilities], () => {
  if (selectedPoiId.value && (!showFacilities.value || !filteredPois.value.some(poi => poi.id === selectedPoiId.value))) selectedPoiId.value = ''
  updateMarker()
}, { flush: 'post' })
watch(() => props.report?.report_id ?? null, () => {
  fittedReportId = null
  selectedPoiId.value = ''
  offlineView.value = { zoom: 1, panX: 0, panY: 0 }
  visibleTierIds.value = TIER_DEFINITIONS.map(tier => tier.id)
  showTierOutlines.value = false
  selectedCoverageGroupId.value = ''
  selectedCoverageFacilityId.value = ''
  visibleGroupIds.value = null
  publishGroupFilter()
}, { flush: 'post', immediate: true })
// Category snapshots update points, not the viewport or the user's layer choices.
// Fit only the first valid contour of a report; later partial geometry keeps the camera.
watch(() => `${props.report?.report_id ?? ''}:${JSON.stringify(record(props.report?.isochrone).geometry ?? null)}`,
  fitInitialReport, { flush: 'post' })

let offlineDrag: { x: number; y: number } | null = null
function startOfflineDrag(event: PointerEvent) {
  if (state.value !== 'offline' || event.button !== 0) return
  offlineDrag = { x: event.clientX, y: event.clientY }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function moveOfflineDrag(event: PointerEvent) {
  if (!offlineDrag) return
  offlineView.value = { ...offlineView.value, panX: offlineView.value.panX + event.clientX - offlineDrag.x,
    panY: offlineView.value.panY + event.clientY - offlineDrag.y }
  offlineDrag = { x: event.clientX, y: event.clientY }
}
function endOfflineDrag() { offlineDrag = null }
function zoomOffline(factor: number) {
  offlineView.value = { ...offlineView.value, zoom: Math.max(.5, Math.min(4, offlineView.value.zoom * factor)) }
}
function resetMapView() {
  if (state.value === 'ready' && map) {
    if (props.report) fitReport()
    else locate(analysisCenter.value)
  } else offlineView.value = { zoom: 1, panX: 0, panY: 0 }
}
function changeZoom(direction: 1 | -1) {
  if (state.value === 'ready' && map) zoomMapView(map, direction)
  else if (state.value === 'offline') zoomOffline(direction > 0 ? 1.25 : 1 / 1.25)
  updateMarker()
}
let lastWheelAt = 0
let wheelDelta = 0
function isMapGesture(event: Event) {
  const target = event.target as Element | null
  return !target?.closest?.('.map-controls, .checkup-map-legend, .checkup-route-card, .map-fallback')
}
function wheelMap(event: WheelEvent) {
  if (state.value === 'loading' || !isMapGesture(event)) return
  clearMapHover()
  // Scoped to the map: the browser menu/keyboard zoom and floating-panel scroll
  // remain native. Ctrl+wheel is the trackpad pinch signal on Chromium.
  event.preventDefault()
  event.stopPropagation()
  wheelDelta += event.deltaY
  const threshold = event.ctrlKey ? 5 : 40
  if (Math.abs(wheelDelta) < threshold || Date.now() - lastWheelAt < 120) return
  changeZoom(wheelDelta < 0 ? 1 : -1)
  wheelDelta = 0
  lastWheelAt = Date.now()
}
let pinchDistance: number | null = null
function touchDistance(event: TouchEvent) {
  if (event.touches.length !== 2) return null
  const first = event.touches.item(0), second = event.touches.item(1)
  return first && second ? Math.hypot(first.clientX-second.clientX, first.clientY-second.clientY) : null
}
function startPinch(event: TouchEvent) {
  clearMapHover()
  if (!isMapGesture(event)) return
  pinchDistance = touchDistance(event)
  if (pinchDistance !== null) { event.preventDefault(); event.stopPropagation() }
}
function movePinch(event: TouchEvent) {
  if (pinchDistance === null || !isMapGesture(event)) return
  const distance = touchDistance(event)
  if (distance === null) return
  event.preventDefault()
  event.stopPropagation()
  const ratio = distance / Math.max(1, pinchDistance)
  if (ratio > 1.16 || ratio < 1 / 1.16) {
    changeZoom(ratio > 1 ? 1 : -1)
    pinchDistance = distance
  }
}
function endPinch(event: TouchEvent) { if (event.touches.length < 2) pinchDistance = null }
const scaleBar = computed(() => {
  void projectionVersion.value
  if (state.value === 'offline') return metricScale(offlineProjection.value.metersPerPixel)
  if (!map || !sdk?.Pixel || !map.pixelToPoint || !map.getDistance || state.value !== 'ready') return null
  const y = Math.max(1, viewport.value.height / 2)
  const start = map.pixelToPoint(new sdk.Pixel(0, y))
  const end = map.pixelToPoint(new sdk.Pixel(100, y))
  return metricScale(map.getDistance(start, end) / 100)
})

onMounted(async () => {
  if (typeof window !== 'undefined' && window.matchMedia) {
    motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    motionQuery.addEventListener('change', clearMapHover)
  }
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', clearMapHover)
  if (host.value) {
    observer = new ResizeObserver(() => {
      if (host.value) viewport.value = { width: host.value.clientWidth, height: host.value.clientHeight }
      map?.checkResize()
      updateMarker()
    })
    observer.observe(host.value)
    viewport.value = { width: host.value.clientWidth, height: host.value.clientHeight }
  }
  if (!host.value || !key) {
    fail()
    return
  }
  try {
    sdk = await loadBaiduMap(key)
    if (stopped || !host.value) return
    map = new sdk.Map(host.value, { enableRotate: false, enableRotateGestures: false,
      enableTilt: false, enableTiltGestures: false, enableMapClick: false,
      enableWheelZoom: true, enableAutoResize: true, heading: 0, tilt: 0 })
    mapStyleMode.value = applyDarkMapStyle(map, darkMapMode)
    // One wheel handler above owns map gestures; disable the SDK's parallel
    // handler so a single wheel step cannot zoom twice.
    map.disableScrollWheelZoom?.()
    map.addEventListener('tilesloaded', ready)
    map.addEventListener('click', mapClick)
    for (const event of mapEvents) map.addEventListener(event, updateMarker)
    timer = setTimeout(fail, 15000)
    locate(props.center)
    fitInitialReport()
  } catch { if (!stopped) fail() }
})
onBeforeUnmount(() => {
  stopped = true
  clearMapHover()
  hoverNodes.clear()
  hoverIndex.clear()
  motionQuery?.removeEventListener('change', clearMapHover)
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', clearMapHover)
  if (frame !== undefined) {
    if (frameUsesRaf && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame as number)
    else clearTimeout(frame)
  }
  clearTimeout(timer)
  observer?.disconnect()
  map?.removeEventListener('tilesloaded', ready)
  map?.removeEventListener('click', mapClick)
  for (const event of mapEvents) map?.removeEventListener(event, updateMarker)
  map?.destroy()
  map = undefined
})
</script>

<template>
  <section
    class="road-map" :class="{ 'is-ready': state === 'ready', 'is-offline': state === 'offline', 'is-dark-filter': mapStyleMode === 'filter', 'poi-cursor-active': !!poiPointer }"
    :data-map-style="mapStyleMode" :data-map-mode="darkMapMode" :data-map-filter="mapFilterCss"
    :data-map-availability="state"
    :style="mapStyleMode === 'filter' ? { '--dark-map-filter': DARK_MAP_FILTER_CSS } : undefined"
    :data-dot-mode="dotPlan.mode" aria-label="地图工作区"
    @wheel.capture="wheelMap" @touchstart.capture="startPinch" @touchmove.capture="movePinch" @touchend.capture="endPinch" @touchcancel.capture="endPinch"
    @pointermove.capture="hoverDots" @pointerdown.capture="clearMapHover" @pointerleave="clearMapHover" @pointercancel="clearMapHover"
  >
    <div ref="host" class="road-map-canvas" :class="{ selectable, 'offline-canvas': state === 'offline' }" :aria-label="selectable ? '点击地图选择体检中心' : state === 'ready' ? '道路地图' : '离线范围预览，可拖动和缩放'" :aria-hidden="!selectable"
      @pointerdown="startOfflineDrag" @pointermove="moveOfflineDrag" @pointerup="endOfflineDrag" @pointercancel="endOfflineDrag" @lostpointercapture="endOfflineDrag" />
    <svg v-if="report && state !== 'loading'" class="checkup-map-overlay" :viewBox="`0 0 ${viewport.width || 1} ${viewport.height || 1}`" role="group" aria-label="体检中心、等时圈、规则点阵、设施点与覆盖区域">
      <!-- Only the existing single 15-minute polygon is drawn when no tier data exists. -->
      <path v-if="dotPlan.mode !== 'tiers' && isochronePath" :d="isochronePath" fill="none" stroke="#a8deff" stroke-opacity=".45" stroke-width=".8" fill-rule="evenodd">
        <title>15分钟步行等时圈（仅单层）</title>
      </path>
      <path v-for="tier in (showTierOutlines ? tierOutlines : [])" :key="`tier-outline-${tier.id}`" :d="tier.path" fill="none" :stroke="tier.color" stroke-width="1.2" stroke-dasharray="3 3" fill-rule="evenodd">
        <title>{{ tier.id }}分钟时间档轮廓（按服务返回范围绘制）</title>
      </path>
      <!-- Whole circles inherit the time band at their center; boundaries may extend by one radius. -->
      <g ref="dotLayer" class="checkup-dots" aria-hidden="true">
        <circle v-for="dot in visibleDots" :key="dot.id" class="walk-dot"
          :data-dot-id="dot.id" :cx="dot.x" :cy="dot.y" :r="dot.r" :fill="dot.fill" :fill-opacity="dot.opacity" />
      </g>
      <g v-if="highlightedBlindDots.length" class="blind-dots-glow" aria-hidden="true">
        <circle v-for="dot in highlightedBlindDots" :key="dot.id" :cx="dot.x" :cy="dot.y" :r="dot.r" />
      </g>
      <g v-if="highlightedUnknownDots.length" class="blind-dots-glow unknown-dots-glow" aria-hidden="true">
        <circle v-for="dot in highlightedUnknownDots" :key="dot.id" :cx="dot.x" :cy="dot.y" :r="dot.r" />
      </g>
      <TransitionGroup name="poi-filter" tag="g">
      <g v-for="poi in visibleReportPois" :key="poi.id" class="checkup-poi" role="button" tabindex="0" :aria-label="`${poi.name}，${poi.memberships.map(item => item.facilityName).join('、') || '未分类'}，${poi.memberships.map(item => item.groupName).join('、') || '未分类'}；${poi.inIsochrone ? '圈内设施' : '圈外覆盖候选，不计入圈内'}；选择查看路线`" :aria-pressed="selectedPoiId === poi.id" @click="selectedPoiId = poi.id" @keydown.enter.prevent="selectedPoiId = poi.id" @keydown.space.prevent="selectedPoiId = poi.id">
        <g :transform="`translate(${poi.pixel.x} ${poi.pixel.y}) scale(${selectedPoiId === poi.id ? 1.18 : 1})`">
          <circle cx="0" cy="-8" r="13" fill="transparent" />
          <path class="facility-pin" d="M0 0C-3.2-4-6.5-7.5-6.5-11A6.5 6.5 0 1 1 6.5-11C6.5-7.5 3.2-4 0 0Z"
            :fill="poi.color" :fill-opacity="selectedPoiId === poi.id ? .98 : poi.inIsochrone ? .82 : .45"
            stroke="#092035" stroke-opacity=".95" stroke-width="1.1" />
          <circle class="facility-pin-core" cx="0" cy="-11" r="2.5" fill="#f4fbff" stroke="#092035" stroke-width=".6" />
          <text v-if="selectedPoiId === poi.id" x="10" y="-15" class="poi-group-number">0{{ poi.number }}</text>
        </g>
        <title>{{ poi.number ? `0${poi.number} · ` : '' }}{{ poi.name }} · {{ poi.memberships.map(item => item.groupName).join('、') || '未分类' }}{{ poi.inIsochrone ? '' : ' · 圈外候选（不计入圈内）' }}</title>
      </g>
      </TransitionGroup>
    </svg>
    <div v-if="poiPointer" class="poi-hover-cursor" :style="{ left: `${poiPointer.x}px`, top: `${poiPointer.y}px` }" aria-hidden="true"><i></i><span>＋</span></div>
    <div v-if="showCenter && markerStyle" class="road-map-center" :style="markerStyle" aria-label="分析中心位置"><span>分析中心</span></div>
    <div v-if="selectionStyle" class="road-map-selection" :style="selectionStyle" aria-label="待体检地点"><span>待体检地点</span></div>
    <div v-if="report && selectedCoverage && !coverageBlocked && !layersOpen && state !== 'loading'" class="blind-symbol-key map-blind-key" role="note" aria-label="盲区显示图例">
      <svg viewBox="0 0 44 30" aria-hidden="true"><g class="blind-dots-glow"><circle v-for="(x, i) in [8, 20, 32, 14, 26]" :key="i" :cx="x" :cy="i < 3 ? 9 : 20" r="4.5" /></g></svg>
      <span><strong>盲区 · 呼吸点阵</strong><small>{{ selectedCoverageFacilityId ? `${selectedFacilityName} · ${hasUnverifiedDisplay ? '含未核验区域' : '确认盲区 · 采样估算'}` : hasUnverifiedDisplay ? '包含未核验区域' : '已核验共同服务盲区 · 采样估算' }}</small></span>
      <span v-if="selectedCoverageFacilityId" class="category-evidence-key"><span><i class="confirmed-swatch" />{{ hasUnverifiedDisplay ? '确认盲区＋未核验区域，同色显示' : '确认盲区' }}</span></span>
    </div>
    <div v-if="report && selectedCoverage && coverageBlocked && !layersOpen && state !== 'loading'" class="blind-symbol-key map-blind-key" role="status">{{ coverageStatus }}</div>
    <div v-if="state !== 'loading'" class="map-controls" aria-label="地图缩放控制">
      <div v-if="scaleBar" class="map-scale" aria-label="地图比例尺"><span>{{ scaleBar.label }}</span><i :style="{ width: `${scaleBar.widthPx}px` }" /></div>
      <div class="zoom-buttons">
        <button type="button" aria-label="放大地图" title="放大地图" @click="changeZoom(1)">＋</button>
        <button type="button" aria-label="恢复地图范围" title="恢复范围" @click="resetMapView">⌖</button>
        <button type="button" aria-label="缩小地图" title="缩小地图" @click="changeZoom(-1)">−</button>
      </div>
    </div>
    <Teleport to="body">
    <section v-if="report && state !== 'loading' && layersOpen" class="checkup-map-legend" aria-label="地图图层与图例" @keydown.esc.stop="layersOpen = false">
      <header ref="layerHeading" class="layer-heading" tabindex="-1"><strong>地图图层与盲区</strong><button type="button" aria-label="收起地图图层" title="收起地图图层" @click="layersOpen = false">×</button></header>
      <div class="legend-content">
      <div class="coverage-picker">
        <strong class="coverage-tree-title">查看服务盲区</strong>
        <small class="coverage-current-status" role="status">{{ coverageStatus }}</small>
        <div class="coverage-tree" aria-label="设施类别盲区">
          <div class="coverage-root-actions">
            <button type="button" :aria-pressed="!selectedCoverageGroupId" @click="changeCoverageGroup('')">关闭盲区</button>
            <button type="button" :aria-pressed="selectedCoverageGroupId === 'total'" @click="changeCoverageGroup('total')">总盲区</button>
          </div>
          <details v-for="group in groupsWithColors" :key="group.id" :open="selectedCoverageGroupId === group.id" class="coverage-branch">
            <summary>{{ group.name_zh }}<small v-if="selectedCoverageGroupId === group.id"> · 当前{{ selectedCoverageFacilityId ? '小类' : '大类' }}</small></summary>
            <div class="coverage-children">
              <button type="button" :aria-pressed="selectedCoverageGroupId === group.id && !selectedCoverageFacilityId" @click="changeCoverageGroup(group.id)">查看{{ group.name_zh }}大类共同盲区</button>
              <button v-for="facility in facilitiesForGroup(group.id)" :key="facility.id" type="button" :aria-pressed="selectedCoverageFacilityId === facility.id" @click="selectCoverageFacility(facility.id)">{{ facility.name_zh }}</button>
            </div>
          </details>
        </div>
        <div v-if="selectedCoverage && !coverageBlocked" class="blind-symbol-key" role="note" aria-label="盲区显示图例">
          <svg viewBox="0 0 44 30" aria-hidden="true"><g class="blind-dots-glow"><circle v-for="(x, i) in [8, 20, 32, 14, 26]" :key="i" :cx="x" :cy="i < 3 ? 9 : 20" r="4.5" /></g></svg>
          <span><strong>盲区 · 呼吸点阵</strong><small>{{ selectedCoverageFacilityId ? `${selectedFacilityName} · ${hasUnverifiedDisplay ? '含未核验区域' : '确认盲区 · 采样估算'}` : hasUnverifiedDisplay ? '包含未核验区域' : '已核验共同服务盲区 · 采样估算' }}</small></span>
        </div>
        <div v-if="selectedCoverageFacilityId" class="category-evidence-key" aria-label="小类统一浅蓝高亮图例"><span><i class="confirmed-swatch" />{{ hasUnverifiedDisplay ? '确认盲区＋未核验区域，同色显示' : '确认盲区' }}</span></div>
        <small v-if="selectedCoverageGroupId">{{ coverageDescription }}</small>
      </div>
      <p v-if="isOfflineRenderSample" class="small tier-note">渲染样例：该区域没有保存设施点数据，设施图层已隐藏（样例中的 POI 来自另一地点，不能显示在这里）。</p>
      <template v-if="dotPlan.mode === 'tiers'">
        <div v-for="tier in dotPlan.tiers" :key="`legend-tier-${tier.id}`" class="tier-legend-row">
          <span class="tier-dot-swatch" :style="{ background: tier.color, opacity: tier.opacity }" />
          <span>{{ tier.label }}</span>
        </div>
        <p class="small tier-note">统一点距、等大小的交错密铺圆点，用三档蓝色色阶表示步行时间区间；不代表设施或人口数量，每个位置只绘制一次。</p>
        <div class="tier-toggles">
          <label v-for="tier in dotPlan.tiers" :key="`toggle-tier-${tier.id}`" class="checkup-map-control">
            <input type="checkbox" :checked="visibleTierIds.includes(tier.id)" @change="toggleTier(tier.id, $event)" />显示{{ tier.label }}
          </label>
          <label class="checkup-map-control"><input v-model="showTierOutlines" type="checkbox" />显示时间档轮廓</label>
        </div>
      </template>
      <template v-else>
        <div><span class="checkup-range-swatch" />15分钟步行等时圈（仅单层）</div>
        <p class="small tier-note">仅单层：服务未返回 5/10/15 分钟分档数据，点阵只表达现有 15 分钟范围，未生成内层，也不把外圈缩成假档位。</p>
      </template>
      <label class="checkup-map-control"><input v-model="showFacilities" type="checkbox" :disabled="isOfflineRenderSample" />显示设施点</label>
      <label class="checkup-map-control"><input v-model="showCoverageCandidates" type="checkbox" />显示圈外覆盖候选（不计入圈内）</label>
      <small>设施点随所选大类或小类筛选，按组编号、着色；总盲区或关闭盲区显示全部设施。点击点位查看路线。</small>
      <small class="coverage-method-note">{{ coverageGridLabel }}</small>
      <small>仅反映指定中心步行圈的服务范围估算，供规划前期排查；不代表完整行政社区，需结合设施开放条件和容量复核。</small>
      <small class="map-style-note" data-map-style-note>{{ mapStyleLabel }}</small>
      </div>
      <ResizeGrip label="地图图层" anchor="left" drag-handle=".layer-heading" :min-width="240" :min-height="150" />
    </section>
    </Teleport>
    <div v-if="selectedPoi" class="checkup-route-card" role="status"
      :class="{ 'is-collapsed': !poiDetailsOpen, 'is-maximized': poiDetailsMaximized }">
      <header class="poi-card-heading"><strong>{{ selectedPoi.name }}</strong><div class="poi-card-tools">
        <button type="button" :aria-label="poiDetailsOpen ? '收起设施详情' : '展开设施详情'" :title="poiDetailsOpen ? '收起' : '展开'" @click="poiDetailsOpen = !poiDetailsOpen">{{ poiDetailsOpen ? '−' : '+' }}</button>
        <button type="button" :aria-label="poiDetailsMaximized ? '还原设施详情' : '最大化设施详情'" :title="poiDetailsMaximized ? '还原' : '最大化'" @click="poiDetailsMaximized = !poiDetailsMaximized">{{ poiDetailsMaximized ? '▣' : '□' }}</button>
        <button type="button" class="checkup-route-close" aria-label="关闭设施详情" @click="selectedPoiId = ''">×</button>
      </div></header>
      <div v-show="poiDetailsOpen" class="poi-card-body">
        <span>{{ selectedPoiFacilities }} · {{ selectedPoiGroups }} · {{ selectedPoiStatus }}</span>
        <span v-if="selectedRoute?.status === 'success'">步行 {{ typeof selectedRoute.distance_m === 'number' ? `${Math.round(selectedRoute.distance_m)}米` : '—' }} · {{ duration(selectedRoute.duration_s) }}{{ typeof selectedRoute.duration_s === 'number' ? (selectedRoute.duration_s <= 900 ? ' · 15分钟内' : ' · 超过15分钟') : '' }}</span>
        <span v-else-if="selectedPoi.address">{{ selectedPoi.address }}</span>
      </div>
      <ResizeGrip label="设施详情" anchor="right" drag-handle=".poi-card-heading" :min-width="230" :min-height="60" :disabled="poiDetailsMaximized" />
    </div>
    <p v-if="state === 'loading'" class="map-loading" role="status">正在加载地图…</p>
    <details v-else-if="state === 'offline'" class="map-fallback">
      <summary>ⓘ 离线预览</summary><p>{{ error }} 不含道路底图或道路配准。</p>
    </details>
  </section>
</template>

<style scoped>
.coverage-tree-title { font-size:12px; }
.coverage-current-status { position:sticky; top:0; z-index:3; padding:6px; border-radius:6px; background:#0c2238f5; }
.coverage-tree { display:grid; gap:5px; }
.coverage-root-actions { display:flex; gap:6px; }
.coverage-tree button { width:auto; text-align:left; padding:6px 8px; border:1px solid #9cc8ef33; border-radius:6px; background:#c3eaff09; color:#d8ecfc; font-size:11px; cursor:pointer; }
.coverage-tree button[aria-pressed=true] { background:#9acdfa28; border-color:#add9f299; }
.coverage-branch { border:1px solid #a0c9ed22; border-radius:7px; }
.coverage-branch summary { cursor:pointer; padding:7px 8px; font-size:12px; color:#d8ecfc; }
.coverage-branch summary small { font-size:10px; color:#a6d4ef; }
.coverage-children { display:grid; gap:4px; padding:0 8px 8px 18px; }
.category-evidence-key { display:flex; flex-wrap:wrap; gap:12px; font-size:10px; color:#d8e8f6; }
.category-evidence-key span { display:flex; align-items:center; gap:5px; }
.category-evidence-key i { width:8px; height:8px; border-radius:50%; display:inline-block; }
.confirmed-swatch { background:#c3eaff; }

.road-map { position: relative; width: 100%; height: 100%; min-width: 0; min-height: 0; overflow: hidden; background: #081020; }
.road-map.is-dark-filter .road-map-canvas { filter: var(--dark-map-filter); }
.road-map-canvas { position: absolute; inset: 0; touch-action: none; }
/* The z-index inside the map stacking context stays below the floating panels. */
.checkup-map-overlay { position: absolute; z-index: 10; inset: 0; width: 100%; height: 100%; overflow: hidden; pointer-events: none; touch-action: none; }
.checkup-map-overlay path { pointer-events: none; }
.checkup-dots { pointer-events: none; }
.blind-dots-glow { fill: #c3eaff; opacity: .65; filter: drop-shadow(0 0 2.5px #9edbff77); animation: blind-dots-breathe 3s ease-in-out infinite; }
.blind-symbol-key { display: flex; align-items: center; gap: 9px; padding: 9px 12px; border: 1px solid #c3e5ff40; border-radius: 12px; color: #e8f5ff; background: #0b1f32b8; backdrop-filter: blur(18px); }
.blind-symbol-key svg { flex: none; width: 44px; height: 30px; overflow: visible; }
.blind-symbol-key > span { display: grid; gap: 3px; }
.blind-symbol-key strong { font-size: 12px; font-weight: 600; }
.blind-symbol-key small { font-size: 10px; line-height: 1.4; color: #a9c8df; }
.map-blind-key { position: absolute; z-index: 12; left: 16px; bottom: 90px; max-width: calc(100% - 32px); box-sizing: border-box; pointer-events: none; }
@keyframes blind-dots-breathe { 0%, 100% { opacity: .48; transform: translateY(0); } 50% { opacity: .84; transform: translateY(-2.5px); } }
@media (prefers-reduced-motion: reduce) { .blind-dots-glow { animation: none; } }
.walk-dot { transform-box: fill-box; transform-origin: center; transform: scale(var(--dot-hover-scale, 1)); transition: transform 320ms ease; }
@media (prefers-reduced-motion: reduce) { .walk-dot { transition: none; } }
.checkup-poi { pointer-events: auto; cursor: crosshair; outline: none; }
.facility-pin { transition: fill .25s ease, fill-opacity .25s ease; }
.poi-filter-enter-active { transition: opacity .32s ease; }
.poi-filter-leave-active { transition: opacity .2s ease; pointer-events: none; }
.poi-filter-enter-from,.poi-filter-leave-to { opacity: 0; }
@media (prefers-reduced-motion: reduce) { .poi-filter-enter-active,.poi-filter-leave-active,.facility-pin { transition: none; } }
.road-map.poi-cursor-active .checkup-poi { cursor: none; }
.poi-hover-cursor { position: absolute; z-index: 18; width: 24px; height: 24px; transform: translate(-50%, -50%); pointer-events: none; color: #75c7f4; filter: drop-shadow(0 0 4px #122a3e); }
.poi-hover-cursor > i { position: absolute; inset: 2px; border: 1px solid #75c7f4; border-radius: 50%; animation: poi-cursor-pulse 1.8s ease-out infinite; }
.poi-hover-cursor > span { position: absolute; inset: 0; display: grid; place-items: center; color: #9fddff; font-size: 21px; font-weight: 300; line-height: 1; }
@keyframes poi-cursor-pulse { 0%, 100% { opacity: .75; transform: scale(.85); } 60% { opacity: .12; transform: scale(1.35); } }
@media (prefers-reduced-motion: reduce) { .poi-hover-cursor > i { animation: none; opacity: .65; } }
.checkup-poi:focus-visible { filter: drop-shadow(0 0 3px #d7f1ff); }
.checkup-poi:hover .facility-pin { fill-opacity: .95; }
.road-map-center, .road-map-selection { position: absolute; z-index: 10; pointer-events: none; }
.road-map-center { width: 14px; height: 14px; transform: translate(-50%,-50%); border: 2px solid #f1faff; border-radius: 50%; background: #75c7f4; box-shadow: 0 0 0 3px #0b203366; }
.road-map-center span, .road-map-selection span { position: absolute; top: 19px; left: 50%; transform: translateX(-50%); white-space: nowrap; padding: 2px 6px; border-radius: 4px; background: #07182d88; color: #e7f6ff; font-size: 10px; }
.road-map-selection { width: 24px; height: 24px; transform: translate(-50%,-50%); }
.road-map-selection::before { position: absolute; inset: 2px; border: 2px solid #e3f6ff; border-radius: 50% 50% 50% 0; background: #7cc9f0aa; content: ''; transform: rotate(-45deg); }
.road-map-selection span { top: 26px; }
.map-controls { position: absolute; z-index: 12; right: 18px; bottom: 90px; display: grid; justify-items: end; gap: 9px; color: #d8efff; }
.zoom-buttons { display: flex; gap: 2px; padding: 3px; border: 1px solid #a0c9ed33; border-radius: 9px; background: #07172d77; backdrop-filter: blur(12px); }
.zoom-buttons button { width: 34px; height: 32px; padding: 0; border: 0; border-radius: 6px; color: #e2f4ff; background: transparent; font-size: 18px; }
.zoom-buttons button:hover { background: #a1daff22; }
.map-scale { display: grid; justify-items: end; gap: 3px; color: #d9eeff; font-size: 10px; text-shadow: 0 1px 3px #000; }
.map-scale i { display: block; height: 6px; border: 1px solid #d9eeff; border-top: 0; }
.checkup-map-legend { display: flex; flex-direction: column; position: fixed; z-index: 65; bottom: 90px; left: 16px; width: 300px; height: 400px; min-width: 240px; min-height: 150px; max-width: calc(100% - 32px); max-height: min(70dvh,calc(100dvh - 120px)); box-sizing: border-box; overflow: hidden; border: 1px solid #c3e5ff55; border-radius: 15px; background: #0b1f32b8; backdrop-filter: blur(22px); box-shadow: inset 0 1px #ffffff18,0 15px 42px #0005; color: #e8f5ff; font-size: 12px; overscroll-behavior: contain; }

.layer-heading { flex: none; position: sticky; z-index: 2; top: 0; display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 42px; padding: 0 10px 0 14px; border-bottom: 1px solid #b6d8ef33; background: #0b2035f2; }
.layer-heading button { flex: none; width: 34px; height: 34px; padding: 0; border: 0; border-radius: 8px; background: transparent; color: #e8f5ff; font-size: 22px; cursor: pointer; }
.layer-heading button:hover { background: #c4eaff20; }
.legend-content { display: grid; gap: 10px; padding: 14px; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.legend-content > div, .checkup-map-control { display: flex; align-items: center; gap: 7px; }
.legend-content > .coverage-picker { display: grid; gap: 7px; padding-bottom: 10px; border-bottom: 1px solid #a0c9ed22; }
.checkup-map-control { line-height: 1.45; color: #cde4f6; }
.checkup-map-control input { flex: none; margin: 0; accent-color: #9ddaff; }
.checkup-map-control select { min-width: 0; width: 100%; padding: 5px; border: 1px solid #9cc8ef33; border-radius: 5px; background: #0a1c31; color: #ecf7ff; font: inherit; }
.tier-dot-swatch { display: inline-block; flex: none; width: 24px; height: 8px; border-radius: 2px; }
.tier-toggles { display: grid; gap: 6px; }
.tier-note, .checkup-map-legend small { margin: 0; color: #9cb8cf; font-size: 11px; line-height: 1.5; }
.checkup-range-swatch { display: inline-block; width: 16px; height: 10px; border: 1px solid #9bd9ff; background: #8bd0ff22; }
.checkup-route-card { position: absolute; z-index: 14; bottom: 90px; right: 16px; display: flex; flex-direction: column; width: 300px; min-width: 230px; min-height: 60px; max-width: calc(100% - 32px); max-height: min(60%,calc(100% - 220px)); overflow: hidden; border: 1px solid #a0c9ed33; border-radius: 10px; background: #07172d99; color: #e8f6ff; backdrop-filter: blur(14px); font-size: 12px; }
.checkup-route-card.is-maximized { inset: 220px 18px 90px !important; width: auto !important; height: auto !important; max-width: none; max-height: none; transform: none; }
.poi-card-heading { position: sticky; top: 0; z-index: 2; flex: none; background: #0b2035ed; display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; padding: 7px 9px 7px 12px; }
.poi-card-heading > strong { font-size: 13px; overflow-wrap: anywhere; }
.poi-card-tools { display: flex; flex: none; }
.poi-card-tools button { width: 25px; height: 26px; padding: 0; border: 0; color: #c7e5f9; background: transparent; font-size: 16px; }
.poi-card-body { flex: 1; min-height: 0; display: grid; gap: 6px; padding: 8px 12px 12px; overflow: auto; border-top: 1px solid #a0c9ed22; }
.poi-card-body span { color: #c7ddec; overflow-wrap: anywhere; }
.map-loading { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); color: #c9e9ff; font-size: 12px; }
.map-fallback { position: absolute; z-index: 12; left: 16px; bottom: 64px; max-width: min(290px,calc(100% - 28px)); padding: 0 7px; border-radius: 6px; color: #bdd5e9; background: #07172d88; font-size: 11px; }
.map-fallback > summary { padding: 4px 0; font-size: 11px; }
.map-fallback p { padding: 5px 0 9px; }
@media (max-width: 640px) {
  .map-controls { right: 10px; }
  .checkup-map-legend { left: 10px; }
  .checkup-map-legend { max-width: calc(100% - 20px); }
  .checkup-route-card { right: 10px; max-width: calc(100% - 20px); }
  .checkup-route-card.is-maximized { inset: 200px 10px 90px !important; }
}
/* Native cursor only: center hotspot follows pan/select without intercepting map events. */
.road-map-canvas, .road-map-canvas :deep(*) { cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='48' height='48' viewBox='0 0 48 48'%3E%3Cpath d='M24 4V20M24 28V44M4 24H20M28 24H44' fill='none' stroke='%2308172b' stroke-width='3.5' stroke-linecap='round' opacity='.7'/%3E%3Cpath d='M24 4V20M24 28V44M4 24H20M28 24H44' fill='none' stroke='%2375c7f4' stroke-width='1.5' stroke-linecap='round'/%3E%3Ccircle cx='24' cy='24' r='1.5' fill='%2375c7f4'/%3E%3C/svg%3E") 24 24, crosshair !important; }
.poi-group-number { fill: #f1f9ff; stroke: #081524; stroke-width: 2.5px; paint-order: stroke; font-size: 10px; font-weight: 700; }
.checkup-route-card:hover, .checkup-route-card:focus-within { z-index: 16; }
.road-map-canvas :deep(.anchorBL){bottom:55px!important}
.road-map-canvas :deep(.BMap_cpyCtrl){bottom:40px!important}
</style>
