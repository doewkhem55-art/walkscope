<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from 'vue'
import type { DiagnosisView } from './competition.ts'
import { loadBaiduMap } from './baidu.ts'
import type { BaiduMap, BaiduNamespace } from './baidu.ts'

const props = defineProps<{ view: DiagnosisView }>()
type JsonRecord = Record<string, unknown>
type LonLat = { lng: number; lat: number }
type FacilityType = 'market' | 'pharmacy' | 'primary_school'
type PolygonGeometry = LonLat[][][]
type AreaFeature = { id: string; type?: FacilityType; geometry: PolygonGeometry }

const categories: { id: FacilityType; label: string; color: string }[] = [
  { id: 'market', label: '菜市场', color: '#b85c3d' },
  { id: 'pharmacy', label: '药店', color: '#527a5c' },
  { id: 'primary_school', label: '小学', color: '#52728f' },
]
const categoryById = Object.fromEntries(categories.map(item => [item.id, item])) as Record<FacilityType, typeof categories[number]>
const asRecord = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
const asArray = (value: unknown): unknown[] => Array.isArray(value) ? value : []
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const locationFrom = (value: unknown): LonLat | null => {
  const item = asRecord(value)
  const lng = item.lng
  const lat = item.lat
  if (item.crs !== undefined && item.crs !== 'BD09LL') return null
  return finite(lng) && finite(lat) && Math.abs(lng) <= 180 && Math.abs(lat) <= 90 ? { lng, lat } : null
}
const coordinateFrom = (value: unknown): LonLat | null => {
  if (!Array.isArray(value) || !finite(value[0]) || !finite(value[1])) return null
  return Math.abs(value[0]) <= 180 && Math.abs(value[1]) <= 90 ? { lng: value[0], lat: value[1] } : null
}
function polygonFrom(value: unknown): PolygonGeometry | null {
  const item = asRecord(value)
  const rings = (raw: unknown): LonLat[][] | null => {
    if (!Array.isArray(raw) || !raw.length) return null
    const parsed = raw.map(ring => Array.isArray(ring) ? ring.map(coordinateFrom) : [])
    if (parsed.some(ring => ring.length < 3 || ring.some(point => !point))) return null
    return parsed as LonLat[][]
  }
  if (item.type === 'Polygon') {
    const polygon = rings(item.coordinates)
    return polygon ? [polygon] : null
  }
  if (item.type === 'MultiPolygon') {
    if (!Array.isArray(item.coordinates) || !item.coordinates.length) return null
    const polygons = item.coordinates.map(rings)
    return polygons.some(polygon => !polygon) ? null : polygons as LonLat[][][]
  }
  return null
}
function featuresFrom(value: unknown): AreaFeature[] {
  return asArray(asRecord(value).features).flatMap((raw, index) => {
    const feature = asRecord(raw)
    const geometry = polygonFrom(feature.geometry)
    if (!geometry) return []
    const props = asRecord(feature.properties)
    const type = text(props.facility_type)
    return [{ id: text(feature.id) || String(index), type: type in categoryById ? type as FacilityType : undefined, geometry }]
  })
}

const data = computed(() => asRecord(props.view.diagnosis.data))
const center = computed(() => locationFrom(data.value.center) ?? props.view.center)
const spatial = computed(() => {
  const poiData = asRecord(data.value.poi)
  const pois = asArray(poiData.pois).flatMap((raw, index) => {
    const poi = asRecord(raw)
    const type = text(poi.facility_type)
    const location = locationFrom(poi.location)
    if (!location || !(type in categoryById)) return []
    return [{ id: text(poi.poi_id) || `poi-${index}`, name: text(poi.name) || '未命名设施', type: type as FacilityType, location, address: text(poi.address) }]
  })
  const isochrone = polygonFrom(asRecord(data.value.isochrone).geometry)
  const blindspot = asRecord(data.value.blindspot)
  return {
    pois,
    isochrone,
    blindSpots: featuresFrom(blindspot.blind_spots),
    unknownAreas: featuresFrom(blindspot.unknown_areas),
    blindspot,
    stages: asRecord(data.value.stages),
    routes: asArray(asRecord(data.value.facility_routes).routes).map(asRecord),
  }
})

const visibleTypes = reactive<Record<FacilityType, boolean>>({ market: true, pharmacy: true, primary_school: true })
const showIsochrone = ref(true)
const showBlindSpots = ref(true)
const showUnknownAreas = ref(true)
const selectedId = ref('')
const visiblePois = computed(() => spatial.value.pois.filter(poi => visibleTypes[poi.type]))
const visibleBlinds = computed(() => spatial.value.blindSpots.filter(feature => !feature.type || visibleTypes[feature.type]))
const visibleUnknown = computed(() => spatial.value.unknownAreas.filter(feature => !feature.type || visibleTypes[feature.type]))
const selectedPoi = computed(() => spatial.value.pois.find(poi => poi.id === selectedId.value) ?? null)
const selectedRoute = computed(() => selectedPoi.value ? spatial.value.routes.find(route => route.target_id === selectedPoi.value!.id) : undefined)
function toggleCategory(type: FacilityType) {
  visibleTypes[type] = !visibleTypes[type]
  if (!visibleTypes[type] && selectedPoi.value?.type === type) selectedId.value = ''
}

const patternKey = useId().replace(/[^a-zA-Z0-9_-]/g, '')
const blindPatternId = `blind-${patternKey}`
const unknownPatternId = `unknown-${patternKey}`
const plot = ref<HTMLDivElement | null>(null)
const controls = ref<HTMLDivElement | null>(null)
const basemap = ref<HTMLDivElement | null>(null)
const size = ref({ width: 0, height: 0 })
const controlsHeight = ref(88)
const mapKey = import.meta.env.VITE_BAIDU_MAP_AK?.trim() ?? ''
const roadState = ref<'missing' | 'loading' | 'ready' | 'error'>(mapKey ? 'loading' : 'missing')
const roadError = ref('')
const projectionVersion = ref(0)
const mapOffset = ref({ x: 0, y: 0 })
let sdk: BaiduNamespace | undefined
let roadMap: BaiduMap | undefined
let tileTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const redrawEvents = ['moving', 'moveend', 'zooming', 'zoomend', 'resize']
const redraw = () => { projectionVersion.value++ }
const caption = computed(() => roadState.value === 'ready' ? `真实道路底图 · ${props.view.source}`
  : roadState.value === 'loading' ? `道路底图加载中… · ${props.view.source}`
    : roadState.value === 'missing' ? '未配置浏览器端地图密钥 · 无道路底图' : `道路底图不可用 · ${props.view.source}`)
const mapHint = computed(() => roadState.value === 'missing'
  ? '在前端 .env.local 填写浏览器端 VITE_BAIDU_MAP_AK，重启后加载道路。'
  : roadState.value === 'error' ? roadError.value : '')
let observer: ResizeObserver | undefined
function measureOffset() {
  if (!plot.value || !basemap.value) return
  const a = plot.value.getBoundingClientRect()
  const b = basemap.value.getBoundingClientRect()
  mapOffset.value = { x: a.left - b.left, y: a.top - b.top }
}
function resetView() {
  if (!roadMap || !sdk || !basemap.value || !plot.value) return
  measureOffset()
  const full = basemap.value.getBoundingClientRect()
  const bounds = plot.value.getBoundingClientRect()
  const bottom = Number.parseFloat(getComputedStyle(plot.value).getPropertyValue('--controls-bottom')) || 12
  roadMap.setViewport(locations.value.map(point => new sdk!.Point(point.lng, point.lat)), {
    // SDK margins are top / right / bottom / left; leave panels and controls clear.
    margins: [90, Math.max(full.right - bounds.right, 0) + 28,
      controlsHeight.value + bottom + 28, Math.max(bounds.left - full.left, 0) + 28],
    enableAnimation: false,
  })
  redraw()
}
function destroyMap() {
  clearTimeout(tileTimer)
  if (roadMap) {
    for (const event of redrawEvents) roadMap.removeEventListener(event, redraw)
    roadMap.removeEventListener('tilesloaded', tilesLoaded)
    roadMap.destroy()
    roadMap = undefined
  }
}
function failMap(message: string) {
  roadState.value = 'error'
  roadError.value = message
  destroyMap()
}
function tilesLoaded() {
  if (disposed || !roadMap) return
  clearTimeout(tileTimer)
  roadState.value = 'ready'
  measureOffset()
  redraw()
}
async function startMap() {
  if (!mapKey) return
  try {
    sdk = await loadBaiduMap(mapKey)
    if (disposed || !basemap.value) return
    roadMap = new sdk.Map(basemap.value, {
      enableRotate: false, enableRotateGestures: false,
      enableTilt: false, enableTiltGestures: false,
      enableMapClick: false, enableWheelZoom: true, enableAutoResize: true,
      heading: 0, tilt: 0,
    })
    for (const event of redrawEvents) roadMap.addEventListener(event, redraw)
    roadMap.addEventListener('tilesloaded', tilesLoaded)
    tileTimer = setTimeout(() => failMap('未能确认道路底图加载。请检查浏览器端 AK、JavaScript API 权限、Referer 白名单和网络后刷新。'), 15000)
    roadMap.centerAndZoom(new sdk.Point(center.value.lng, center.value.lat), 15)
    resetView()
  } catch {
    // Provider errors can contain request URLs (and AKs); do not echo them.
    if (!disposed) failMap('百度底图加载失败或超时。请检查浏览器端 AK、JavaScript API 权限、Referer 白名单和网络后刷新。')
  }
}
onMounted(() => {
  if (!plot.value) return
  observer = new ResizeObserver(entries => {
    for (const entry of entries) {
      const box = entry.target.getBoundingClientRect()
      if (entry.target === plot.value) size.value = { width: box.width, height: box.height }
      if (entry.target === controls.value) controlsHeight.value = box.height
    }
    measureOffset()
    if (roadMap) { roadMap.checkResize(); resetView() }
  })
  observer.observe(plot.value)
  if (controls.value) observer.observe(controls.value)
  void startMap()
})
onBeforeUnmount(() => { disposed = true; observer?.disconnect(); destroyMap() })
watch(() => props.view, () => { selectedId.value = ''; resetView() }, { flush: 'post' })

function localPoint(location: LonLat) {
  const origin = center.value
  return {
    x: (location.lng - origin.lng) * 111320 * Math.cos(origin.lat * Math.PI / 180),
    y: (location.lat - origin.lat) * 111320,
  }
}
function geometryLocations(geometry: PolygonGeometry | null): LonLat[] {
  return geometry?.flat(2) ?? []
}
const locations = computed(() => [center.value, ...spatial.value.pois.map(poi => poi.location), ...geometryLocations(spatial.value.isochrone),
    ...spatial.value.blindSpots.flatMap(feature => geometryLocations(feature.geometry)),
    ...spatial.value.unknownAreas.flatMap(feature => geometryLocations(feature.geometry))])
const extent = computed(() => {
  const points = locations.value.map(localPoint)
  return {
    minX: Math.min(...points.map(point => point.x)), maxX: Math.max(...points.map(point => point.x)),
    minY: Math.min(...points.map(point => point.y)), maxY: Math.max(...points.map(point => point.y)),
  }
})
const fit = computed(() => {
  const { width, height } = size.value
  const bounds = extent.value
  const rangeX = Math.max(bounds.maxX - bounds.minX, 1)
  const rangeY = Math.max(bounds.maxY - bounds.minY, 1)
  const usableWidth = Math.max(width - 72, 0)
  const usableHeight = Math.max(height - 150, 0)
  const scale = usableWidth && usableHeight ? Math.min(usableWidth / rangeX, usableHeight / rangeY) : 0
  return { ...bounds, rangeX, rangeY, scale, width, height }
})
function pointOnMap(location: LonLat) {
  if (roadState.value === 'ready' && roadMap && sdk) {
    void projectionVersion.value
    const pixel = roadMap.pointToPixel(new sdk.Point(location.lng, location.lat))
    return { x: pixel.x - mapOffset.value.x, y: pixel.y - mapOffset.value.y }
  }
  const box = fit.value
  const drawnWidth = box.rangeX * box.scale
  const drawnHeight = box.rangeY * box.scale
  return {
    x: (box.width - drawnWidth) / 2 + (localPoint(location).x - box.minX) * box.scale,
    y: (box.height - drawnHeight) / 2 + (box.maxY - localPoint(location).y) * box.scale,
  }
}
function pathFor(geometry: PolygonGeometry | null) {
  if (!geometry || (!fit.value.scale && roadState.value !== 'ready')) return ''
  return geometry.flatMap(polygon => polygon.map(ring => {
    const points = ring.map(pointOnMap)
    return `M${points.map((point, index) => `${index ? 'L' : ''}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join('')}Z`
  })).join('')
}
const isochronePath = computed(() => pathFor(spatial.value.isochrone))
function categoryLabel(type?: FacilityType) { return type ? categoryById[type].label : '类别未注明' }
function routeStatus(route: JsonRecord | undefined) {
  const status = text(route?.status)
  if (status === 'success') return '已获取步行路线'
  if (status === 'no_route') return '未找到可用路线'
  if (status === 'unavailable') return '算路暂不可用'
  return status ? `路线状态：${status}` : '未返回路线数据'
}
function durationLabel(seconds: unknown) {
  if (!finite(seconds) || seconds < 0) return '—'
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return minutes ? `${minutes}分${remainder}秒` : `${remainder}秒`
}
const caveat = computed(() => {
  const status = text(spatial.value.stages.blindspot)
  const distance = spatial.value.blindspot.service_distance_m
  const grid = spatial.value.blindspot.grid_size_m
  const limits = finite(distance) && finite(grid) ? `${distance}m距离代理 / ${grid}m网格` : '距离代理 / 网格分析'
  return `轮廓说明：等时圈为路径采样近似，盲区为${limits}；两者均未沿街道或建筑边界绘制。盲区分析${status === 'partial' ? '部分完成' : status ? `· ${status}` : ''}。`
})
</script>

<template>
  <section class="competition-map" :class="{ 'with-basemap': roadState === 'loading' || roadState === 'ready' }" aria-label="生活圈道路底图与体检结果">
    <div ref="basemap" class="baidu-canvas" :class="{ 'is-ready': roadState === 'ready' }" :aria-hidden="roadState !== 'ready'" aria-label="百度道路底图" />
    <div ref="plot" class="plot-area">
      <div class="map-caption" role="status">{{ caption }}<small v-if="mapHint">{{ mapHint }}</small></div>
      <div class="map-legend" aria-label="地图图例">
        <strong>地图图例</strong>
        <div><span class="legend-center" aria-hidden="true" />体检中心</div>
        <div><span class="legend-area isochrone-legend" aria-hidden="true" />15 分钟步行范围估算</div>
        <div class="legend-categories">
          <span><i class="poi-swatch swatch-market" />菜市场</span>
          <span><i class="poi-swatch swatch-pharmacy" />药店</span>
          <span><i class="poi-swatch swatch-primary_school" />小学</span>
        </div>
        <div><span class="legend-area blind-swatch" aria-hidden="true" />服务盲区 · 网格估算</div>
        <div><span class="legend-area unknown-swatch" aria-hidden="true" />未知区域 · 证据不足</div>
      </div>
      <div v-if="roadState === 'ready'" class="map-navigation" aria-label="地图视野控制">
        <button type="button" aria-label="放大地图" @click="roadMap?.zoomIn()">＋</button>
        <button type="button" aria-label="缩小地图" @click="roadMap?.zoomOut()">−</button>
        <button type="button" @click="resetView">恢复视野</button>
      </div>
      <svg class="map-svg" :viewBox="`0 0 ${Math.max(size.width, 1)} ${Math.max(size.height, 1)}`" role="group" aria-label="同一BD09LL坐标系绘制的生活圈空间图">
        <defs>
          <pattern :id="blindPatternId" width="10" height="10" patternUnits="userSpaceOnUse">
            <rect width="10" height="10" fill="#fff0e8" />
            <path d="M-2 2L2 -2M0 10L10 0M8 12L12 8" stroke="#b85c3d" stroke-width="2" />
          </pattern>
          <pattern :id="unknownPatternId" width="10" height="10" patternUnits="userSpaceOnUse">
            <rect width="10" height="10" fill="#edf3f6" />
            <circle cx="3" cy="3" r="1.5" fill="#52728f" />
          </pattern>
        </defs>
        <path v-if="showIsochrone && isochronePath" :d="isochronePath" fill="#a4b743" fill-opacity=".18" stroke="#788b2e" stroke-width="2.5" fill-rule="evenodd">
          <title>15分钟步行范围估算 · 基于路径采样，不是道路或街区边界</title>
        </path>
        <template v-if="showUnknownAreas">
          <path v-for="feature in visibleUnknown" :key="`unknown-${feature.id}`" :d="pathFor(feature.geometry)" fill-rule="evenodd" :fill="`url(#${unknownPatternId})`" stroke="#52728f" stroke-width="1.4" stroke-dasharray="4 3">
            <title>未知区域 · {{ categoryLabel(feature.type) }} · 证据不足</title>
          </path>
        </template>
        <template v-if="showBlindSpots">
          <path v-for="feature in visibleBlinds" :key="`blind-${feature.id}`" :d="pathFor(feature.geometry)" fill-rule="evenodd" :fill="`url(#${blindPatternId})`" stroke="#b85c3d" stroke-width="1.6">
            <title>盲区 · {{ categoryLabel(feature.type) }} · 网格距离代理判断</title>
          </path>
        </template>
        <g v-for="poi in visiblePois" :key="poi.id" class="poi-marker" role="button" tabindex="0" :aria-pressed="selectedId === poi.id" :aria-label="`${poi.name}，${categoryLabel(poi.type)}；选择查看对应路线距离、耗时和状态`" @click="selectedId = poi.id" @keydown.enter.prevent="selectedId = poi.id" @keydown.space.prevent="selectedId = poi.id">
          <title>{{ poi.name }} · {{ categoryLabel(poi.type) }} · 选择查看路线信息</title>
          <circle :cx="pointOnMap(poi.location).x" :cy="pointOnMap(poi.location).y" r="11" fill="transparent" />
          <circle v-if="poi.type === 'market'" :cx="pointOnMap(poi.location).x" :cy="pointOnMap(poi.location).y" r="5.5" :fill="categoryById[poi.type].color" stroke="white" stroke-width="1.5" />
          <rect v-else-if="poi.type === 'pharmacy'" :x="pointOnMap(poi.location).x - 5" :y="pointOnMap(poi.location).y - 5" width="10" height="10" rx="1.5" :fill="categoryById[poi.type].color" stroke="white" stroke-width="1.5" />
          <path v-else :d="`M${pointOnMap(poi.location).x} ${pointOnMap(poi.location).y - 6}L${pointOnMap(poi.location).x + 6} ${pointOnMap(poi.location).y}L${pointOnMap(poi.location).x} ${pointOnMap(poi.location).y + 6}L${pointOnMap(poi.location).x - 6} ${pointOnMap(poi.location).y}Z`" :fill="categoryById[poi.type].color" stroke="white" stroke-width="1.5" />
          <circle v-if="selectedId === poi.id" :cx="pointOnMap(poi.location).x" :cy="pointOnMap(poi.location).y" r="9" fill="none" stroke="#171b08" stroke-width="2" />
        </g>
        <g class="center-marker" :transform="`translate(${pointOnMap(center).x} ${pointOnMap(center).y})`">
          <circle r="11" fill="#171b08" />
          <circle r="5" fill="#a4b743" stroke="white" stroke-width="1.5" />
          <text x="14" y="4">体检中心</text>
        </g>
      </svg>
      <div v-if="selectedPoi" class="route-card" :style="`--controls-height: ${controlsHeight}px`" aria-live="polite">
        <button type="button" class="route-close" aria-label="关闭设施详情" @click="selectedId = ''">×</button>
        <strong>{{ selectedPoi.name }}</strong>
        <span>{{ categoryLabel(selectedPoi.type) }} · {{ routeStatus(selectedRoute) }}</span>
        <span v-if="selectedRoute?.status === 'success'">步行 {{ finite(selectedRoute.distance_m) ? `${selectedRoute.distance_m}m` : '—' }} · {{ durationLabel(selectedRoute.duration_s) }}{{ finite(selectedRoute.duration_s) ? (selectedRoute.duration_s <= 900 ? ' · 900秒内' : ' · 超过900秒') : '' }}</span>
        <span v-else-if="selectedPoi.address">{{ selectedPoi.address }}</span>
      </div>
      <div ref="controls" class="map-controls" role="group" aria-label="地图图层与类别筛选">
        <label class="poi-select-label">查看路线
          <select v-model="selectedId" aria-label="选择设施查看路线" class="poi-select">
            <option value="">选择设施</option>
            <option v-for="poi in visiblePois" :key="`option-${poi.id}`" :value="poi.id">{{ poi.name }} · {{ categoryLabel(poi.type) }}</option>
          </select>
        </label>
        <button v-for="category in categories" :key="category.id" type="button" class="map-toggle" :aria-pressed="visibleTypes[category.id]" @click="toggleCategory(category.id)">
          <span class="poi-swatch" :class="`swatch-${category.id}`" :style="{ '--swatch-color': category.color }" aria-hidden="true" />{{ category.label }}
        </button>
        <button type="button" class="map-toggle" :aria-pressed="showIsochrone" @click="showIsochrone = !showIsochrone"><span class="area-swatch isochrone-swatch" aria-hidden="true" />15分钟范围估算</button>
        <button type="button" class="map-toggle" :aria-pressed="showBlindSpots" @click="showBlindSpots = !showBlindSpots"><span class="area-swatch blind-swatch" aria-hidden="true" />服务盲区估算</button>
        <button type="button" class="map-toggle" :aria-pressed="showUnknownAreas" @click="showUnknownAreas = !showUnknownAreas"><span class="area-swatch unknown-swatch" aria-hidden="true" />未知区域</button>
        <p class="map-caveat">{{ caveat }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.competition-map { position: relative; width: 100%; height: 100%; min-height: 0; overflow: hidden; background: #fff; }
.baidu-canvas { position: absolute; inset: 0; visibility: hidden; }
.baidu-canvas.is-ready { visibility: visible; }
.plot-area { --controls-bottom: 12px; pointer-events: none; position: absolute; inset: 0; left: clamp(276px, calc(24vw + 16px), 356px); right: clamp(336px, calc(31vw + 16px), 486px); }
.with-basemap .plot-area { --controls-bottom: 42px; }
.map-svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: hidden; pointer-events: none; }
.map-caption { position: absolute; z-index: 1; top: 14px; left: 50%; transform: translateX(-50%); width: max-content; max-width: calc(100% - 12px); padding: 7px 10px; border: 1px solid #171b08; border-radius: 3px; color: #171b08; background: #fff; font-size: .76rem; font-weight: 800; text-align: center; overflow-wrap: anywhere; }
.map-caption small { display: block; margin-top: 4px; font-weight: 400; line-height: 1.4; }
.map-legend { pointer-events: none; position: absolute; z-index: 1; top: 58px; left: 6px; display: grid; gap: 5px; max-width: min(240px, 62%); padding: 9px 10px; border: 1px solid #171b08; border-radius: 4px; color: #171b08; background: rgba(255,255,255,.96); box-shadow: 2px 2px 0 rgba(23,27,8,.65); font-size: .69rem; line-height: 1.35; }
.map-legend > div { display: flex; align-items: center; gap: 7px; }
.map-legend strong { font-size: .75rem; }
.legend-center { position: relative; width: 12px; height: 12px; flex: 0 0 12px; border-radius: 50%; background: #171b08; }
.legend-center::after { position: absolute; inset: 3px; border-radius: 50%; background: #a4b743; content: ""; }
.legend-area { width: 16px; height: 11px; flex: 0 0 16px; border: 1px solid; }
.isochrone-legend, .isochrone-swatch { border-color: #788b2e; background: rgba(164,183,67,.35); }
.legend-categories { display: flex; flex-wrap: wrap; gap: 5px 8px !important; }
.legend-categories span { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.legend-categories .poi-swatch { width: 8px; height: 8px; }
.map-navigation { position: absolute; z-index: 2; top: 58px; right: 4px; display: flex; gap: 4px; pointer-events: auto; }
.map-navigation button { width: auto; padding: 4px 7px; background: white; font-size: .72rem; box-shadow: none; border-width: 1px; }
.map-controls, .route-card { pointer-events: auto; }
.center-marker text { font: 800 12px system-ui, sans-serif; fill: #171b08; paint-order: stroke; stroke: #fff; stroke-width: 3px; stroke-linejoin: round; }
.poi-marker { cursor: pointer; outline: none; pointer-events: auto; }
.poi-marker:focus-visible { filter: drop-shadow(0 0 2px #171b08); }
.route-card { position: absolute; z-index: 2; bottom: calc(var(--controls-height, 88px) + var(--controls-bottom) + 8px); left: 50%; transform: translateX(-50%); display: grid; gap: 3px; min-width: min(280px, calc(100% - 20px)); max-width: min(420px, calc(100% - 20px)); padding: 10px 34px 10px 12px; border: 1.5px solid #171b08; border-radius: 4px; color: #171b08; background: #fff; box-shadow: 3px 3px 0 #171b08; font-size: .76rem; line-height: 1.4; }
.route-card strong { font-size: .82rem; }
.route-card span { overflow-wrap: anywhere; }
.route-close { position: absolute; top: 4px; right: 5px; width: 24px; height: 24px; padding: 0; border: 0; color: #171b08; background: transparent; box-shadow: none; font-size: 1.2rem; line-height: 1; }
.map-controls { position: absolute; z-index: 2; right: 0; bottom: var(--controls-bottom); left: 0; display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 6px; padding: 8px; border: 1px solid #c6cea1; border-radius: 5px; background: rgba(255,255,255,.97); }
.poi-select-label { display: inline-flex; align-items: center; gap: 5px; color: #171b08; font-size: .72rem; font-weight: 700; white-space: nowrap; }
.poi-select { width: min(210px, 48vw); min-width: 120px; padding: 5px 6px; border: 1px solid #9aa36c; border-radius: 3px; color: #171b08; background: #fff; font-size: .72rem; }
.map-toggle { display: inline-flex; align-items: center; gap: 6px; width: auto; padding: 6px 8px; border: 1px solid #9aa36c; border-radius: 3px; color: #171b08; background: #fff; box-shadow: none; font-size: .74rem; font-weight: 700; white-space: nowrap; }
.map-toggle[aria-pressed="false"] { opacity: .55; text-decoration: line-through; }
.poi-swatch { width: 10px; height: 10px; border: 1.5px solid white; outline: 1px solid var(--swatch-color); background: var(--swatch-color); }
.swatch-market { border-radius: 50%; }
.swatch-pharmacy { border-radius: 1px; }
.swatch-primary_school { transform: rotate(45deg) scale(.82); }
.area-swatch { width: 14px; height: 11px; border: 1px solid; }
.blind-swatch { border-color: #b85c3d; background: repeating-linear-gradient(135deg, #fff0e8 0 3px, #b85c3d 3px 4px); }
.unknown-swatch { border-color: #52728f; background-color: #edf3f6; background-image: radial-gradient(#52728f 1px, transparent 1px); background-size: 5px 5px; }
.map-caveat { flex-basis: 100%; color: #3d431f; font-size: .68rem; line-height: 1.3; text-align: center; }
@media (max-width: 960px) {
  .plot-area { inset: 0 8px 0; }
  .plot-area { --controls-bottom: 8px; }
  .map-controls { gap: 5px; padding: 6px; }
  .map-toggle { padding: 5px 6px; font-size: .7rem; }
  .map-caption { top: 8px; }
  .map-legend { top: 46px; left: 2px; max-width: min(220px, 54%); padding: 7px; font-size: .65rem; }
}
</style>
