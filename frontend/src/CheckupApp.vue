<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import BaiduRoadMap from './BaiduRoadMap.vue'
import { loadBaiduMap } from './baidu.ts'
import ReportConversation from './ReportConversation.vue'
import { createReportDraftPageId, reportDraftClient } from './reportDrafts.ts'
import ResizeGrip from './ResizeGrip.vue'
import { MAP_GROUP_COLORS } from './checkupMap.ts'
import { CheckupApiError, checkupBase, checkupClient, loadPending, savePending, watchCheckup, parseCheckup } from './checkups.ts'
import type { Checkup, CheckupCatalog, CheckupFocus, CheckupHandle, CheckupLocation, MapReport } from './checkups.ts'
import type { EvidenceRef } from './reportQuestions.ts'

const roadMap = ref<InstanceType<typeof BaiduRoadMap> | null>(null)
function toggleBlindLayers() { roadMap.value?.toggleLayers() }

const initialBase = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8768'
// Shareable report link: /?report=<id>&base=http://127.0.0.1:8765
// Only a task id is accepted; the report still comes from the backend, and a
// missing/expired task falls back to the normal picker instead of faking data.
const search = typeof window !== 'undefined' && window.location ? window.location.search : ''
const reportMatch = /[?&]report=([^&]+)/.exec(search)
const baseMatch = /[?&]base=([^&]+)/.exec(search)
const queryReport = reportMatch ? decodeURIComponent(reportMatch[1]) : ''
const queryBase = baseMatch ? decodeURIComponent(baseMatch[1]) : initialBase
const restored = queryReport
  ? {
      id: queryReport,
      base: queryBase,
      status: 'running' as const, stage: 'queued', progress: 0,
    }
  : loadPending()
// Resume an accepted task on its own service; a fresh workspace honors the URL service.
const base = ref(restored?.base ?? queryBase)
const catalog = ref<CheckupCatalog | null>(null)
const selectedFacilityIds = ref<string[]>([])
const ready = ref(false)
const connectedBase = ref('')
const connecting = ref(false)
const connectionError = ref('')
const error = ref('')
const busy = ref(false)
const submitting = ref(false)
const controlBusy = ref(false)
const controlUncertain = ref(false)
const pauseRequested = ref(false)
const cancelRequested = ref(false)
const cancelling = ref(false)
const mode = ref<'address' | 'coordinate'>('address')
const address = ref('')
const city = ref('')
const longitude = ref('')
const latitude = ref('')
const checkup = ref<Checkup | null>(null)
const handle = ref<CheckupHandle | null>(null)
const uncertainRequest = ref(false)
const uncertainBase = ref('')
const groupFilter = ref('all')
const resultTab = ref<'report' | 'questions'>('report')
const focusedPoiId = ref<string | null>(null)
const resultPanel = ref<{ expand: () => void } | null>(null)
const searchOpen = ref(true)
const questionOpen = ref(false)
const reportConversation = ref<InstanceType<typeof ReportConversation> | null>(null)
// A refresh creates a new editing session; closing the dialog keeps this page's edits.
const reportPageSessionId = createReportDraftPageId()
const canGenerateReport = computed(() => checkup.value?.status === 'completed' && ['tongji-all7-repaired-20261026', 'tongji-all7-small-updated-20261027'].includes(checkup.value.result?.report_id ?? ''))
async function generateSmartReport() {
  if (!canGenerateReport.value) { error.value = '请先载入同济七组45小项的已保存报告。'; return }
  questionOpen.value = true
  await nextTick()
  await reportConversation.value?.startReportDraft()
}
const isCase = ref(false)
const caseLoading = ref(false)
const suggestions = ref<Array<{title: string; address: string; point: {lng:number;lat:number}}>>([])
const suggestionStatus = ref('')
const addressInput = ref<HTMLInputElement | null>(null)
const suggestionMenuStyle = ref<Record<string,string>>({})
function dismissSuggestions() {
  suggestionVersion++
  clearTimeout(suggestionTimer)
  suggestions.value=[]
  suggestionStatus.value=''
  suggestionIndex.value=-1
}
function positionSuggestions() {
  const r = addressInput.value?.getBoundingClientRect()
  if (r) suggestionMenuStyle.value = {position:'fixed',left:`${r.left}px`,top:`${r.bottom+7}px`,width:`${r.width}px`,maxHeight:`${Math.max(80,Math.min(320,window.innerHeight-r.bottom-24))}px`,zIndex:'100'}
}
watch(searchOpen, open => {if(!open){suggestionVersion++;suggestions.value=[];suggestionStatus.value='';clearTimeout(suggestionTimer)}})
const suggestionIndex = ref(-1)
const selectedPlace = ref<{lng:number;lat:number} | null>(null)
let suggestionTimer: ReturnType<typeof setTimeout> | undefined
let suggestionVersion = 0
// Narrow screens use a bottom drawer instead of shrinking the desktop panels: the map stays
// full-height and interactive, and one panel opens over it at a time.
let connectionController: AbortController | undefined
let controller: AbortController | undefined
let controlController: AbortController | undefined
let pollGeneration = 0

if (restored && 'uncertain' in restored) {
  uncertainRequest.value = true
  uncertainBase.value = restored.base
} else if (restored) {
  handle.value = restored
  checkup.value = { ...restored, result: null, error: null }
}

const activeTask = computed(() => Boolean(handle.value && (!checkup.value || ['queued','running','paused'].includes(checkup.value.status))))
const taskPaused = computed(() => checkup.value?.status === 'paused')
const controlLabel = computed(() => controlUncertain.value ? '核实检索状态' : pauseRequested.value ? '任务建立后暂停…' : controlBusy.value && !cancelling.value ? (taskPaused.value ? '恢复中…' : '正在暂停…') : taskPaused.value ? '继续检索' : '暂停检索')
const isCurrentBase = computed(() => {
  try { return ready.value && connectedBase.value === checkupBase(base.value).href.replace(/\/$/, '') }
  catch { return false }
})
const coordinate = computed(() => {
  const lng = Number(longitude.value), lat = Number(latitude.value)
  return longitude.value.trim() && latitude.value.trim() && Number.isFinite(lng) && Number.isFinite(lat)
    && Math.abs(lng) <= 180 && Math.abs(lat) <= 90 ? { lng, lat, crs: 'BD09LL' as const } : null
})
const catalogFacilityIds = computed(() => (catalog.value?.facilities ?? []).map(item => item.id))
const selectedFacilitySet = computed(() => new Set(selectedFacilityIds.value))
const selectedCount = computed(() => selectedFacilitySet.value.size)
const selectionLocked = computed(() => connecting.value || busy.value || caseLoading.value || activeTask.value)
const selectionError = computed(() => catalog.value && selectedCount.value === 0 ? '至少选择一个设施类别' : '')
const facilityGroups = computed(() => {
  const facilities = catalog.value?.facilities ?? []
  const selected = selectedFacilitySet.value
  return (catalog.value?.groups ?? []).map(group => {
    const members = facilities.filter(item => item.group_id === group.id)
    return { id: group.id, name_zh: group.name_zh, facilities: members,
      selected: members.filter(item => selected.has(item.id)).length }
  })
})
const canSubmit = computed(() => isCurrentBase.value && Boolean(catalog.value) && !busy.value && !connecting.value
  && catalog.value?.runtime?.map_service_configured !== false && !activeTask.value && !uncertainRequest.value
  && selectedCount.value > 0 && !caseLoading.value && !controlBusy.value && !cancelRequested.value
  && (mode.value === 'address' ? Boolean(address.value.trim()) : Boolean(coordinate.value)))
const mapCenter = computed(() => {
  const center = checkup.value?.result?.center ?? checkup.value?.partial_result?.center
  if (center && typeof center.lng === 'number' && typeof center.lat === 'number') return { lng: center.lng, lat: center.lat, crs: 'BD09LL' }
  return coordinate.value ?? { lng: 121.508602, lat: 31.288268, crs: 'BD09LL' }
})
const mapCaption = computed(() => checkup.value?.result
  ? `${checkup.value.result.location_label} · 本次15分钟步行体检`
  : checkup.value && ['queued', 'running'].includes(checkup.value.status)
    ? `${checkup.value.stage} · ${checkup.value.progress}%`
    : mode.value === 'coordinate' ? '点击道路地图选定体检中心 · BD09LL' : '输入地址，或切换到地图选点')
const statusLabel = computed(() => controlUncertain.value ? '检索状态待确认' : checkup.value?.status === 'cancelled' ? '已终止检索' : taskPaused.value ? '已暂停检索' : isCase.value || checkup.value?.result?.quality.historical_replay ? '历史报告回放' : activeTask.value ? '分析中' : '准备就绪')
const progressPercent = computed(() => {
  const rows = checkup.value?.result?.categories ?? checkup.value?.partial_result?.categories ?? []
  const total = checkup.value?.partial_result?.total_categories ?? checkup.value?.result?.categories.length ?? selectedCount.value
  return total ? Math.round(rows.filter(c => c.search_status === 'complete').length / total * 100) : 0
})
const reportGroupIds = computed(() => new Set((checkup.value?.result?.groups ?? []).map(group => group.id)))
const stageNames: Record<string, string> = {
  queued: '排队中', location: '解析地址', isochrone: '生成步行等时圈',
  poi: '检索设施', routing: '计算中心步行路线', coverage: '计算覆盖与盲区',
  report: '生成报告', completed: '完成', failed: '失败', cancelled: '已终止检索',
}
const stageLabel = computed(() => taskPaused.value ? '已暂停检索' : stageNames[checkup.value?.stage ?? ''] ?? checkup.value?.stage ?? '')
// Compact toolbar: one place, one truthful task state, and the primary action.
const toolbarLocation = computed(() => {
  const report = checkup.value?.result
  if (report && typeof report.location_label === 'string' && report.location_label.trim()) return report.location_label
  if (address.value.trim()) return address.value.trim()
  if (coordinate.value) return `BD09LL ${coordinate.value.lng.toFixed(5)}, ${coordinate.value.lat.toFixed(5)}`
  return '未选择地点'
})
function stopPolling() {
  pollGeneration++
  controller?.abort()
  busy.value = false
}

watch(base, () => {
  ready.value = false
  connectedBase.value = ''
  catalog.value = null
  selectedFacilityIds.value = []
  connectionError.value = ''
  // Completed results belong to their original service, never to a newly connected catalog.
  if (!activeTask.value && !uncertainRequest.value) {
    checkup.value = null
    handle.value = null
    groupFilter.value = 'all'
  }
})

async function connect() {
  if (connecting.value) return
  connectionController?.abort()
  connectionController = new AbortController()
  connecting.value = true
  ready.value = false
  catalog.value = null
  selectedFacilityIds.value = []
  connectionError.value = ''
  try {
    const client = checkupClient(base.value)
    catalog.value = await client.catalog(connectionController.signal)
    selectedFacilityIds.value = catalog.value.facilities.map(item => item.id)
    connectedBase.value = checkupBase(base.value).href.replace(/\/$/, '')
    ready.value = true
    if (handle.value && handle.value.base === connectedBase.value && activeTask.value) void poll()
  } catch (reason) {
    connectionError.value = reason instanceof Error ? reason.message : '服务连接失败。'
  } finally { connecting.value = false }
}

function locationRequest(): CheckupLocation {
  if (mode.value === 'address' && selectedPlace.value) return { type: 'coordinate', ...selectedPlace.value, crs: 'BD09LL' }
  if (mode.value === 'coordinate' && coordinate.value) return { type: 'coordinate', ...coordinate.value }
  return { type: 'address', query: address.value.trim(), ...(city.value.trim() ? { city: city.value.trim() } : {}) }
}

// Always submit in catalog order so the request body is stable and duplicate-free.
function currentFacilitySelection(): string[] {
  const selected = selectedFacilitySet.value
  return catalogFacilityIds.value.filter(id => selected.has(id))
}

function selectGroup(groupId: string) {
  if (selectionLocked.value) return
  const members = facilityGroups.value.find(group => group.id === groupId)?.facilities ?? []
  const next = new Set(selectedFacilityIds.value)
  members.forEach(item => next.add(item.id))
  selectedFacilityIds.value = catalogFacilityIds.value.filter(id => next.has(id))
}

function clearGroup(groupId: string) {
  if (selectionLocked.value) return
  const members = new Set((facilityGroups.value.find(group => group.id === groupId)?.facilities ?? [])
    .map(item => item.id))
  selectedFacilityIds.value = selectedFacilityIds.value.filter(id => !members.has(id))
}

async function submit() {
  if (!canSubmit.value) return
  dismissSuggestions()
  stopPolling()
  controller = new AbortController()
  busy.value = true
  submitting.value = true
  pauseRequested.value = false
  cancelRequested.value = false
  controlUncertain.value = false
  error.value = ''
  try {
    isCase.value = false
    selectedGroupDetails.value = ''
    closeDock()
    questionOpen.value = false
    const accepted = await checkupClient(base.value).submit(locationRequest(), controller.signal, currentFacilitySelection())
    handle.value = accepted
    checkup.value = { ...accepted, result: null, error: null }
    groupFilter.value = 'all'
    savePending(accepted)
    resultTab.value = 'report'
    resultPanel.value?.expand()
    submitting.value = false
    if (cancelRequested.value) await cancelSearch()
    else if (pauseRequested.value) await pauseSearch()
    else await poll()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '体检提交失败。'
    if (reason instanceof CheckupApiError && reason.code === 'uncertain') {
      uncertainRequest.value = true
      uncertainBase.value = base.value
      savePending({ base: base.value, uncertain: true })
    }
    if (reason instanceof CheckupApiError && reason.code === 'MISSING_SERVER_AK') {
      ready.value = false
      savePending(null)
    }
  } finally { busy.value = false; submitting.value = false; pauseRequested.value = false; cancelRequested.value = false }
}

function acceptCheckupState(value: Checkup, saved: CheckupHandle) {
  const partial = value.partial_result ?? (value.status !== 'completed' ? checkup.value?.partial_result : null)
  checkup.value = partial && !value.result ? {...value, partial_result:partial} : value
  handle.value = {...saved, status:value.status, stage:value.stage, progress:value.progress}
  controlUncertain.value = false
  savePending(['queued','running','paused'].includes(value.status) ? handle.value : null)
}

async function poll() {
  const saved = handle.value
  if (!saved) return
  const generation = ++pollGeneration
  controller?.abort()
  controller = new AbortController()
  busy.value = true
  error.value = ''
  const client = checkupClient(saved.base)
  try {
    const result = await watchCheckup(client, saved, controller.signal, value => {
      if (generation === pollGeneration) acceptCheckupState(value,saved)
    })
    if (generation !== pollGeneration) return
    acceptCheckupState(result,saved)
    if (result.status === 'failed') error.value = result.error?.message ?? '服务端报告本次体检失败。'
  } catch (reason) {
    if (generation !== pollGeneration) return
    error.value = reason instanceof Error ? reason.message : '查询中断；可以继续查询原任务。'
    if (reason instanceof CheckupApiError && reason.code === '404') {
      // A confirmed missing record is not a still-running task. Do not resubmit automatically.
      handle.value = null
      checkup.value = null
      savePending(null)
    }
  } finally { if(generation === pollGeneration) busy.value = false }
}

async function pauseSearch() {
  if (submitting.value) { pauseRequested.value = true; return }
  const saved = handle.value
  if (!saved || !activeTask.value || taskPaused.value || controlBusy.value) return
  controlBusy.value = true
  controlController = new AbortController()
  error.value = ''
  try {
    const value = await checkupClient(saved.base).pause(saved,controlController.signal)
    if (handle.value?.id !== saved.id) return
    stopPolling()
    acceptCheckupState(value,saved)
  } catch (reason) {
    controlUncertain.value = reason instanceof CheckupApiError && reason.code === 'uncertain'
    error.value = reason instanceof Error ? reason.message : '暂停状态未确认，请核实检索状态。'
  } finally { controlBusy.value = false; pauseRequested.value = false }
}

async function resumeSearch() {
  const saved = handle.value
  if (!saved || !taskPaused.value || controlBusy.value) return
  controlBusy.value = true
  controlController = new AbortController()
  error.value = ''
  try {
    const value = await checkupClient(saved.base).resume(saved,controlController.signal)
    if (handle.value?.id !== saved.id) return
    stopPolling()
    acceptCheckupState(value,saved)
    if (value.status === 'queued' || value.status === 'running') void poll()
  } catch (reason) {
    controlUncertain.value = reason instanceof CheckupApiError && reason.code === 'uncertain'
    error.value = reason instanceof Error ? reason.message : '恢复状态未确认，请核实检索状态。'
  } finally { controlBusy.value = false }
}

async function cancelSearch() {
  if (controlBusy.value) return
  if (submitting.value) { cancelRequested.value = true; pauseRequested.value = false; return }
  const saved = handle.value
  if (!saved || !activeTask.value) return
  controlBusy.value = true
  cancelling.value = true
  controlController = new AbortController()
  error.value = ''
  try {
    const value = await checkupClient(saved.base).cancel(saved,controlController.signal)
    if (handle.value?.id !== saved.id) return
    stopPolling()
    acceptCheckupState(value,saved)
  } catch (reason) {
    controlUncertain.value = reason instanceof CheckupApiError && reason.code === 'uncertain'
    error.value = reason instanceof Error ? reason.message : '终止状态未确认，请核实检索状态。'
  } finally { controlBusy.value = false; cancelling.value = false; cancelRequested.value = false }
}

function mapSelect(center: { lng: number; lat: number; crs?: string }) {
  mode.value = 'coordinate'
  longitude.value = String(center.lng)
  latitude.value = String(center.lat)
}

function allowRetryAfterUncertain() {
  uncertainRequest.value = false
  savePending(null)
}

const facilityNames = computed(() => Object.fromEntries(
  (catalog.value?.facilities ?? []).map(item => [item.id, item.name_zh])))
const groupNames = computed(() => Object.fromEntries(
  (catalog.value?.groups ?? []).map(item => [item.id, item.name_zh])))
const poiNames = computed(() => {
  const names: Record<string, string> = {}
  for (const poi of checkup.value?.result?.pois ?? []) {
    if (typeof poi.poi_id === 'string' && poi.poi_id && typeof poi.name === 'string' && poi.name) names[poi.poi_id] = poi.name
  }
  return names
})

// Focus must only name identifiers that exist in the current report: the backend rejects an
// unknown facility/group/POI with 422 instead of silently ignoring it.
const conversationFocus = computed<CheckupFocus | null>(() => {
  const report = checkup.value?.result
  if (!report) return null
  const categoryIds = new Set(report.categories.map(category => category.facility_id))
  const selected = currentFacilitySelection().filter(id => categoryIds.has(id))
  return {
    facility_id: null,
    group_id: groupFilter.value !== 'all' && reportGroupIds.value.has(groupFilter.value) ? groupFilter.value : null,
    poi_id: focusedPoiId.value && report.pois.some(poi => poi.poi_id === focusedPoiId.value) ? focusedPoiId.value : null,
    selected_facility_ids: selected.length ? selected : null,
  }
})

function focusPoi(id: string | null) {
  if (id === null) {
    focusedPoiId.value = null
    return
  }
  const report = checkup.value?.result
  if (!report?.pois.some(poi => poi.poi_id === id)) return
  focusedPoiId.value = id

}

watch(() => checkup.value?.result?.report_id, (id, previous) => {
  if (id !== previous) {
    focusedPoiId.value = null
    resultTab.value = 'report'
  }
})

// The conversation component reports only which evidence to reveal; the host owns the view.
function revealEvidence(ref: EvidenceRef) {
  if (ref.kind === 'unsupported') return
  if (ref.kind === 'report' || ref.kind === 'group' || ref.kind === 'facility') resultTab.value = 'report'
  const target = ref.kind === 'report' ? 'checkup-report'
    : ref.kind === 'group' ? `checkup-group-${ref.id}`
      : ref.kind === 'facility' ? `checkup-group-${checkup.value?.result?.categories.find(c => c.facility_id === ref.id)?.group_id ?? ''}`
        : ref.kind === 'poi' ? `checkup-poi-${ref.id}`
          : ref.kind === 'route' ? `checkup-route-${ref.id}`
            : 'checkup-report'
  if (target.startsWith('checkup-group-')) {
    selectedGroupDetails.value = target.slice('checkup-group-'.length)
  }
  const element = document.getElementById(target)
  if (element instanceof HTMLDetailsElement) element.open = true
  element?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}
const mapReport = computed<MapReport | null>(() => {
  if (checkup.value?.result) return checkup.value.result
  const p = checkup.value?.partial_result
  if (!p?.center || !p.isochrone) return null
  return { report_id: p.report_id, center: p.center, isochrone: p.isochrone, categories: p.categories,
    groups: p.groups, pois: p.pois, routes: p.routes, blindspot: {}, quality: { processing: true } }
})
const progressGroups = computed(() => checkup.value?.partial_result?.groups ?? facilityGroups.value
  .filter(g => g.selected > 0).map(g => ({ id: g.id, name_zh: g.name_zh, status: 'pending', score: null,
    score_provisional: false, completed_categories: 0, total_categories: g.selected, facility_count: 0 })))

const progressScore = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : '—'
function toggleGroup(id: string, event: Event) {
  if ((event.target as HTMLInputElement).checked) selectGroup(id)
  else clearGroup(id)
}


const displayGroups = computed(() => {
  const report = checkup.value?.result
  if (!report) return progressGroups.value.map(g => ({...g, visible: g.status !== 'pending', complete: g.status === 'complete', count: g.facility_count, detail: `${g.completed_categories}/${g.total_categories} 类已检索`}))
  return report.groups.map(g => {
    const members = report.categories.filter(c => c.group_id === g.id)
    const ids = new Set(members.map(c => c.facility_id))
    const count = new Set(report.pois.filter(p => p.in_isochrone === true && ids.has(String(p.facility_id))).map(p => p.poi_id)).size
    const complete = members.filter(c => c.search_status === 'complete').length
    return {...g, visible: true, complete: complete === members.length, count, detail: `${complete}/${members.length} 类检索完整`}
  })
})
const allGroupsComplete = computed(() => Boolean(checkup.value?.result && displayGroups.value.every(g => g.complete)))
const coverageNotice = computed(() => {
  const report = checkup.value?.result
  if (!report) return ''
  const audit = report.quality.coverage_validation as {unverified_poi_count?:number} | undefined
  const pending = audit?.unverified_poi_count ?? 0
  if (pending > 0) return `覆盖未完成核验：${pending} 个圈内设施尚未通过本组服务区检查，高亮中含未核验区域。`
  const groups = report.blindspot.group_coverage as Record<string,{unknown_ratio?:number}> | undefined
  return Object.values(groups ?? {}).some(g => (g.unknown_ratio ?? 0) > 1e-8)
    ? '覆盖仍有未核验区域；当前高亮包含未知，不能视为已确认无服务。' : ''
})
const completedProgressGroups = computed(() => displayGroups.value.filter(group => group.complete))
const selectedGroupDetails = ref('')
const dockHovered = ref(false)
const dockFocused = ref(false)
const dockExpanded = computed(() => Boolean(selectedGroupDetails.value) || dockHovered.value || dockFocused.value)
function closeDock(resetFilter = true) {
  if (resetFilter) groupFilter.value = 'all'
  selectedGroupDetails.value = ''
  dockFocused.value = false
  dockHovered.value = false
  if (typeof document !== 'undefined') {
    const focused = document.activeElement as HTMLElement | null
    if (focused?.closest?.('.score-dock')) focused.blur()
  }
}
function dismissDockOutside(event: Event) {
  if (!(event.target as Element | null)?.closest?.('.group-card')) closeDock(Boolean(selectedGroupDetails.value))
}
function groupAccent(id:string) {
  const index = (catalog.value?.groups ?? []).findIndex(g => g.id === id)
  return MAP_GROUP_COLORS[Math.max(0,index) % MAP_GROUP_COLORS.length]
}
function leaveDockFocus(event: FocusEvent) {
  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) dockFocused.value = false
}
function toggleDisplayGroup(id:string) {
  const closing = groupFilter.value === id && selectedGroupDetails.value === id
  groupFilter.value = closing ? 'all' : id
  selectedGroupDetails.value = closing ? '' : id
}
function groupCategories(id:string) {
  return (checkup.value?.result?.categories ?? checkup.value?.partial_result?.categories ?? []).filter(c => c.group_id === id)
}

const scoreLabel = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? progressScore(value) : '数据未齐'
async function showCase() {
  if (activeTask.value || uncertainRequest.value || busy.value || caseLoading.value) return
  controller?.abort()
  caseLoading.value = true
  try {
    const response = await fetch('/cases/tongji.json', { cache: 'no-store' })
    if (!response.ok) throw new Error('本地案例读取失败')
    const saved = await response.json()
    const job = parseCheckup(saved, saved.id)
    handle.value = {...job, base: base.value}
    checkup.value = {...job, base: base.value}
    isCase.value = true
    groupFilter.value = 'all'
    selectedGroupDetails.value = ''
    closeDock()
    suggestionVersion++
    clearTimeout(suggestionTimer)
    suggestionStatus.value = ''
    selectedPlace.value = null
    address.value = '同济大学（四平路校区）'
    suggestions.value = []
    error.value = ''
    questionOpen.value = false
    savePending(null)
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '案例无法加载' }
  finally { caseLoading.value = false }
}
watch(mode, () => {
  suggestionVersion++
  clearTimeout(suggestionTimer)
  suggestions.value = []
  suggestionStatus.value = ''
  suggestionIndex.value = -1
})
function chooseSuggestion(index: number) {
  const item = suggestions.value[index]
  if (!item) return
  suggestionVersion++
  clearTimeout(suggestionTimer)
  address.value = item.title
  selectedPlace.value = item.point
  suggestions.value = []
  suggestionStatus.value = ''
  suggestionIndex.value = -1
}
function suggestionKey(event: KeyboardEvent) {
  if (event.key === 'Escape') { dismissSuggestions(); return }
  if (!suggestions.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    suggestionIndex.value = (suggestionIndex.value + (event.key === 'ArrowDown' ? 1 : -1) + suggestions.value.length) % suggestions.value.length
  } else if (event.key === 'Enter' && suggestionIndex.value >= 0) { event.preventDefault(); chooseSuggestion(suggestionIndex.value) }
}
function addressChanged(event: Event) {
  positionSuggestions()
  address.value = (event.target as HTMLInputElement).value
  selectedPlace.value = null
  suggestions.value = []
  suggestionIndex.value = -1
  const version = ++suggestionVersion
  clearTimeout(suggestionTimer)
  const query = address.value.trim()
  suggestionStatus.value = ''
  if (query.length < 2) return
  suggestionTimer = setTimeout(async () => {
    suggestionStatus.value = '正在查找地点…'
    try {
      const sdk = await loadBaiduMap(import.meta.env.VITE_BAIDU_MAP_AK ?? '')
      if (version !== suggestionVersion) return
      if (!sdk.LocalSearch) throw new Error('地点联想暂不可用，可直接输入完整地址')
      const searcher = new sdk.LocalSearch(city.value || new sdk.Point(mapCenter.value.lng, mapCenter.value.lat), {
        pageCapacity: 6,
        onSearchComplete(results) {
          if (version !== suggestionVersion) return
          suggestions.value = Array.from({length: Math.min(6, results?.getCurrentNumPois?.() ?? 0)}, (_, i) => results.getPoi(i))
            .filter(p => p && Number.isFinite(p.point?.lng) && Number.isFinite(p.point?.lat))
          suggestionStatus.value = suggestions.value.length ? '' : '未找到建议，可补充城市或直接输入完整地址'
        },
      })
      searcher.search(query)
    } catch (reason) { if (version === suggestionVersion) suggestionStatus.value = reason instanceof Error ? reason.message : '地点建议暂不可用' }
  }, 450)
}
onUnmounted(() => { suggestionVersion++; clearTimeout(suggestionTimer) })

onMounted(() => { void connect(); if(typeof window !== 'undefined') window.addEventListener?.('resize',positionSuggestions) })
onUnmounted(() => {if(typeof window !== 'undefined') window.removeEventListener?.('resize',positionSuggestions)})
// Map SDKs stop bubbling pointer events. Capture closes the cards before that happens.
onMounted(() => {if(typeof document !== 'undefined') document.addEventListener?.('pointerdown',dismissDockOutside,true)})
onUnmounted(() => {if(typeof document !== 'undefined') document.removeEventListener?.('pointerdown',dismissDockOutside,true)})
onUnmounted(() => { connectionController?.abort(); controlController?.abort(); stopPolling() })
</script>

<template>
  <div class="app-shell new-experience">
    <main class="workspace" :aria-label="toolbarLocation">
      <section class="map-panel road-map-panel" aria-label="生活圈地图">
        <BaiduRoadMap ref="roadMap" :center="mapCenter" :show-center="Boolean(mapReport || coordinate)" :caption="mapCaption"
          :selection="mode === 'coordinate' ? coordinate : null" :selectable="mode === 'coordinate' && !activeTask"
          :report="mapReport" :groups="catalog?.groups" :facilities="catalog?.facilities" :group-filter="groupFilter"
          @select="mapSelect" @update:group-filter="groupFilter = $event" @poi-select="focusPoi" />
      </section>
      <section class="search-glass glass" :class="{'search-collapsed':!searchOpen}" aria-label="检索地点">
        <header class="search-heading"><span>生活圈 / 检索地点 · {{ statusLabel }}</span><div class="search-actions">
          <button v-if="activeTask || submitting" class="search-control" type="button" :disabled="controlBusy || pauseRequested || cancelRequested || (controlUncertain && busy)" :aria-busy="controlBusy && !cancelling" @click="controlUncertain ? poll() : taskPaused ? resumeSearch() : pauseSearch()"><span aria-hidden="true">{{ taskPaused ? '▷' : 'Ⅱ' }}</span>{{ controlLabel }}</button>
          <button v-if="activeTask || submitting" class="search-control terminate-control" type="button" :disabled="controlBusy || cancelRequested || !catalog?.runtime?.checkup_cancel_supported" :aria-busy="cancelling" :title="catalog?.runtime?.checkup_cancel_supported ? '放弃本次检索，保留已有结果并允许重新体检' : '等待服务更新后启用终止检索'" @click="cancelSearch"><span aria-hidden="true">■</span>{{ cancelling ? '正在终止…' : cancelRequested ? '任务建立后终止…' : '终止检索' }}</button>
          <button class="text-button" @click="searchOpen = !searchOpen" :aria-expanded="searchOpen">{{ searchOpen ? '收起 −' : '展开 ＋' }}</button>
        </div></header>
        <div class="search-scroll" @scroll="dismissSuggestions">
        <button class="smart-report-button" type="button" :disabled="!canGenerateReport" :title="canGenerateReport ? '在原报告对话中生成、修改并导出Word' : '请先载入同济七组45小项的保存结果'" @click="generateSmartReport">✧ 生成智能体检报告</button>
        <form v-show="searchOpen" @submit.prevent="submit">
          <div class="search-line">
            <button type="button" class="mode-button" :disabled="selectionLocked" @click="mode = mode === 'address' ? 'coordinate' : 'address'">{{ mode === 'address' ? '地点输入' : '地图选点' }} ⌄</button>
            <div class="address-wrap">
              <input ref="addressInput" v-if="mode === 'address'" @focus="positionSuggestions" :value="address" @input="addressChanged" @keydown="suggestionKey" role="combobox" aria-autocomplete="list" :aria-expanded="suggestions.length > 0" aria-controls="address-options" :aria-activedescendant="suggestionIndex >= 0 ? `address-option-${suggestionIndex}` : undefined" aria-label="体检地址" :disabled="selectionLocked" placeholder="输入城市、街道或地点，选择地址建议" maxlength="300" />
              <span v-else class="selected-coordinate">{{ coordinate ? '已选位置 · ' + coordinate.lng.toFixed(5) + ', ' + coordinate.lat.toFixed(5) : '点击地图选择位置' }}</span>
              <Teleport to="body"><ul v-if="mode === 'address' && suggestions.length" :style="suggestionMenuStyle" id="address-options" class="address-options glass" role="listbox" aria-label="地址建议">
                <li v-for="(item,index) in suggestions" :key="index" role="option" :id="`address-option-${index}`" :aria-selected="index === suggestionIndex" @pointerdown.prevent="chooseSuggestion(index)"><span>⌖</span><div><strong>{{ item.title }}</strong><small>{{ item.address }}</small></div></li>
              </ul></Teleport>
              <small v-if="suggestionStatus" class="suggestion-status" role="status">{{ suggestionStatus }}</small>
            </div>
            <button class="case-button glow" type="button" @click="showCase" :disabled="activeTask || uncertainRequest || busy || caseLoading">{{ caseLoading ? '加载案例…' : '显示案例' }}</button>
            <button class="start-checkup glow" type="submit" :disabled="!canSubmit">{{ busy && !handle ? '提交中…' : '开始体检 ↗' }}</button>
          </div>
          <fieldset v-if="catalog" class="horizontal-groups" :disabled="selectionLocked"><legend class="sr-only">分析类别</legend><label v-for="group in facilityGroups" :key="group.id"><input type="checkbox" :checked="group.selected === group.facilities.length" @change="toggleGroup(group.id,$event)" /><span>{{ group.name_zh }}</span></label></fieldset>
        </form>
        <p v-if="connectionError || error || selectionError" class="compact-message" role="alert">{{ connectionError || error || selectionError }} <button v-if="connectionError" class="text-button" @click="connect">重新连接</button></p>
        <p v-if="uncertainRequest" class="compact-message">上次提交结果待确认。<button class="text-button" @click="allowRetryAfterUncertain">确认没有任务，允许重试</button></p>
        <div v-if="checkup && catalog && !isCase && !allGroupsComplete" class="analysis-progress" aria-label="设施检索进度">
          <div class="progress-caption"><span>{{ activeTask ? stageLabel : '本次分析结束' }} · 设施检索 {{ progressPercent }}%</span><span>{{ checkup.partial_result?.completed_categories ?? checkup.result?.categories.filter(c=>c.search_status==='complete').length ?? 0 }}/{{ checkup.partial_result?.total_categories ?? checkup.result?.categories.length ?? selectedCount }} 类</span></div>
          <progress class="standard-progress" :value="progressPercent" max="100" aria-label="设施检索完成比例">{{ progressPercent }}%</progress>
          <div v-if="completedProgressGroups.length" class="completed-groups" aria-label="已完成类别" aria-live="polite" aria-relevant="additions">
            <span v-for="group in completedProgressGroups" :key="group.id"><i aria-hidden="true">✓</i>{{ group.name_zh }}</span>
          </div>
          <p v-if="checkup.status === 'cancelled'" class="compact-message">已放弃本次检索，保留已有结果。可修改地点并重新开始体检。</p>
          <p v-else-if="taskPaused" class="compact-message">已暂停，保留当前结果；点击“继续检索”接着分析。在途请求可能仍会完成。</p>
          <p v-else-if="activeTask" class="compact-message">{{ stageLabel }}<button v-if="!busy" class="text-button" @click="poll">重新连接进度</button></p>
          <p v-else class="compact-message">数据未齐的类别不记作零分；可用“显示案例”查看本地完整结果。</p>
        </div>
        <Transition name="score-reveal"><section v-if="allGroupsComplete && checkup?.result" id="checkup-report" class="overall-score" aria-label="设施配置参考分">
          <div class="score-actions">
          <button class="score-question" type="button" aria-label="对该报告提出问题" @click="questionOpen = true">
            <span class="score-context"><small>{{ isCase ? '同济大学 · 本地保存服务区估算' : checkup.result.location_label }}</small><span>设施配置参考分</span><small>按圈内设施数量分档，非覆盖率或法定达标率</small><small v-if="checkup.result.score_provisional">暂估</small><small v-if="coverageNotice" role="status">{{ coverageNotice }}</small><span class="score-guide"><i aria-hidden="true">?</i> 点击分数，向报告提问 <span aria-hidden="true">↗</span></span></span>
            <span class="score-value"><strong>{{ scoreLabel(checkup.result.score) }}</strong><span class="score-question-mark" aria-hidden="true">?</span><span class="score-hover-prompt" aria-hidden="true">对该报告提出问题</span></span>
          </button>
          <button class="score-blind-toggle" type="button" aria-label="地图图层与盲区" :aria-expanded="roadMap?.layersOpen ?? false" @click="toggleBlindLayers"><span aria-hidden="true">▱</span>盲区</button>
          </div>
        </section></Transition>
        <div v-if="checkup?.result && !allGroupsComplete" class="score-actions">
          <button class="text-button" @click="questionOpen = !questionOpen">对已有结果提问 ↗</button>
          <button class="score-blind-toggle" type="button" aria-label="地图图层与盲区" :aria-expanded="roadMap?.layersOpen ?? false" @click="toggleBlindLayers"><span aria-hidden="true">▱</span>盲区</button>
        </div>
        </div>
        <ResizeGrip label="检索地点" anchor="center" @resize-start="dismissSuggestions" />
      </section>
      <nav v-if="checkup" class="score-dock" :class="{'is-open':dockExpanded}" aria-label="七组设施评分" @pointerenter="dockHovered = true" @pointerleave="dockHovered = false" @focusin="dockFocused = true" @focusout="leaveDockFocus" @keydown.esc="closeDock()">
        <p id="score-dock-help" class="sr-only">靠近卡片查看七类评分，点击任意卡片直接切换小项与地图点位，点击其他位置收起。</p>
        <div id="score-tray" class="dock-tray" aria-describedby="score-dock-help">
        <article v-for="(g,index) in displayGroups" :key="g.id" :id="`checkup-group-${g.id}`" class="group-card glass" :aria-hidden="!g.visible" :class="{revealed:g.visible, finished:g.complete,selected:groupFilter===g.id}" :style="{'--card-order':index,'--group-color':groupAccent(g.id)}">
          <div v-if="selectedGroupDetails === g.id" class="group-breakdown">
            <p>圈内独立设施 {{ g.count }} 处{{ !g.complete ? ' · 检索尚未完整' : '' }}</p>
            <ul><li v-for="category in groupCategories(g.id)" :key="category.facility_id"><span>{{ category.name_zh || facilityNames[category.facility_id] || category.facility_id }}<small>{{ category.count ?? category.known_count ?? '—' }}处</small></span><strong>{{ scoreLabel(category.score) }}<small v-if="category.score != null && category.score_provisional">暂估</small></strong></li></ul>
          </div>
          <button class="group-toggle" type="button" :disabled="!g.visible" @click="toggleDisplayGroup(g.id)" :aria-pressed="groupFilter===g.id" :aria-expanded="selectedGroupDetails === g.id"><span class="group-number">0{{ index+1 }} / {{ g.complete ? '检索完成' : g.visible ? (activeTask ? '分析中' : '未完成') : '待分析' }}</span><span class="group-title">{{ g.name_zh }}</span><strong>{{ !g.visible ? '—' : scoreLabel(g.score) }}<small v-if="g.score != null && g.score_provisional">暂估</small></strong><span class="group-detail">{{ g.detail }}</span><span class="card-action">{{ groupFilter===g.id ? '再次点击显示全部 ↺' : '查看小项与地图点位 ↗' }}</span></button>
        </article>
        </div>
      </nav>
      <section v-if="checkup?.result && catalog" v-show="questionOpen" class="question-glass glass" role="dialog" aria-label="报告对话">
        <header><div><strong>报告对话</strong><small>{{ catalog.runtime?.report_questions_mode === 'model' ? 'DeepSeek · 提问与修改都在这里' : '当前为报告查询 · 模型待配置' }}</small></div><button @click="questionOpen = false" aria-label="关闭对话">×</button></header>
        <ReportConversation ref="reportConversation" class="chat-body" :draft-client="reportDraftClient(handle?.base ?? base, undefined, reportPageSessionId)" :key="`${handle?.base}#${checkup.result.report_id}`" :binding="{id:checkup.result.report_id,base:handle?.base ?? base}" :client="checkupClient(handle?.base ?? base)" :facility-names="facilityNames" :group-names="groupNames" :poi-names="poiNames" :focus="conversationFocus" :report-mode="catalog.runtime?.report_questions_mode" @locate="revealEvidence" />
        <ResizeGrip label="报告对话" anchor="right" />
      </section>
    </main>
  </div>
</template>
<style scoped>
.smart-report-button{width:auto;margin:8px 0 12px;padding:9px 14px;border:1px solid #b6dbff55;border-radius:10px;background:#92caff19;color:#e3f4ff;font-size:12px;cursor:pointer}.smart-report-button:disabled{opacity:.4;cursor:default}.smart-report-button:not(:disabled):hover{background:#92caff30}

.glass{position:relative;background:linear-gradient(125deg,#bbddff14,#10283c30 45%,#a4bfff0a);border:1px solid #c1e6ff45;box-shadow:inset 0 1px 0 #e2f5ff25,0 16px 48px #0005;border-radius:20px;color:#e6f5ff}
.glass::before{content:"";position:absolute;inset:0;border-radius:inherit;clip-path:inset(0 round 20px);-webkit-backdrop-filter:blur(18px) saturate(1.3);backdrop-filter:blur(18px) saturate(1.3);z-index:0;pointer-events:none;background:#0a182b38}
.glass > *{position:relative;z-index:1}
.road-map-panel{z-index:auto}
.search-glass{display:flex;flex-direction:column;box-sizing:border-box;max-width:calc(100% - 24px);max-height:calc(100dvh - 110px);min-width:min(300px,calc(100% - 24px));view-transition-name:life-entry;position:absolute;z-index:30;top:76px;left:50%;transform:translateX(-50%);width:min(1050px,calc(100% - 120px));padding:14px 22px;animation:arrive .65s cubic-bezier(.2,.8,.2,1)}
.search-scroll{flex:1;min-height:0;overflow:auto;scrollbar-width:thin;padding-bottom:5px}.search-heading{flex-shrink:0}.search-glass form,.analysis-progress{overflow:visible}.search-collapsed{height:auto!important}
.search-heading>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.search-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}.search-control{display:inline-flex;align-items:center;gap:6px;width:auto;white-space:nowrap;padding:6px 11px;border:1px solid #a9d9ff66;border-radius:999px;background:#284a6866;color:#d4efff;box-shadow:inset 0 1px #e7f6ff16;font-size:11px;font-weight:500;letter-spacing:.02em;cursor:pointer;transition:background .2s,border-color .2s}.search-control:hover:not(:disabled){background:#3d658477;border-color:#c6eaffaa}.search-control:disabled{opacity:.55;cursor:wait}.search-control>span{font-size:13px;line-height:1}
.search-heading{display:flex;justify-content:space-between;align-items:center;font-size:10px;letter-spacing:.15em;color:#a9c6dd;margin-bottom:10px}.text-button{width:auto;background:none;border:0;box-shadow:none;color:#a9d7f7;padding:2px 6px;font-size:11px}.search-line{display:flex;align-items:center;gap:10px}.mode-button{width:auto;white-space:nowrap;background:transparent;border:0;border-right:1px solid #bddfff25;border-radius:0;font-size:12px;padding:10px}.address-wrap{flex:1;position:relative;min-width:0}.address-wrap input{width:100%;border:0;background:transparent;box-shadow:none;font-size:16px;padding:12px 8px;color:#ecf8ff;outline-offset:2px}.address-wrap input::placeholder{color:#94afc5;font-size:13px}.selected-coordinate{font-size:12px}.glow{width:auto;white-space:nowrap;border:1px solid #cceaff88;background:#142b4266;border-radius:24px;padding:10px 18px;color:#e8f8ff;box-shadow:0 0 15px #9bc8ff24,inset 0 0 8px #b9dcff10;transition:box-shadow .25s,background .25s}.glow:hover:not(:disabled){background:#36587566;box-shadow:0 0 24px #a3ceff66,0 0 3px #fff8}.glow:disabled{opacity:.4}.horizontal-groups{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;border:0;padding:12px 0 1px;margin:0}.horizontal-groups label{display:flex;align-items:center;gap:5px;font-size:11px;color:#bbd4e6;white-space:nowrap}.horizontal-groups input{width:12px;height:12px;accent-color:#a8dcff}.compact-message{font-size:11px;color:#b8d1e4;margin:8px 0 0}.address-options{box-sizing:border-box;list-style:none;padding:6px;margin:0;background:#0d2038ee;max-height:320px;overflow:auto}.address-options li{display:flex;gap:12px;padding:12px;cursor:pointer;border-radius:12px}.address-options li:hover,.address-options li[aria-selected=true]{background:#a6d9ff22}.address-options strong{font-size:13px}.address-options small{display:block;font-size:11px;color:#a3b9cb;margin-top:4px}.suggestion-status{font-size:10px;position:absolute;top:100%;left:8px;background:#10243f;padding:4px;z-index:2}
.search-line .start-checkup{font-size:17px;font-weight:600;letter-spacing:.06em;padding:11px 23px;border-color:#cceaffbb;background:linear-gradient(120deg,#76bdf53b,#142b4266);animation:checkup-invite 3.2s ease-in-out infinite}.search-line .start-checkup:disabled{animation:none}@keyframes checkup-invite{50%{box-shadow:0 0 24px #8eccff55,inset 0 0 12px #b9dcff20}}
.overall-score{border-top:1px solid #bfdaff24;margin-top:12px;padding-top:10px}
.score-actions{display:flex;align-items:center;gap:12px}.score-blind-toggle{display:flex;align-items:center;gap:5px;flex-shrink:0;width:auto;min-height:36px;padding:8px 12px;border:1px solid #b6dbff44;border-radius:9px;background:#92caff0b;color:#d8efff;font-size:12px}.score-blind-toggle[aria-expanded=true]{background:#92caff22}.score-question{position:relative;display:flex;align-items:center;justify-content:space-between;gap:16px;flex:1;min-width:0;min-height:88px;border:0!important;border-radius:14px;box-shadow:none!important;padding:8px 14px 8px 0;background:transparent!important;color:#ebfaff;text-align:left;cursor:help}
.score-guide{display:flex;align-items:center;gap:7px;width:fit-content;font-size:11px;font-weight:400;color:#b8e3ff}.score-guide i{display:grid;place-items:center;width:17px;height:17px;border:1px solid #a7dfff77;border-radius:50%;font:500 11px/1 sans-serif;background:#9bd6ff0d;box-shadow:0 0 13px #94cbff18}.score-guide>span{font-size:15px}.score-question:hover .score-guide{color:#f0faff}.score-context{display:grid;gap:9px;font-size:12px;color:#cee7f8}.score-context>small{color:#93b1c9;font-size:10px;font-weight:400}.score-value{position:relative;display:grid;place-items:center;flex-shrink:0;min-width:180px;height:74px}.score-value>strong{font-family:"Avenir Next","SF Pro Display","Helvetica Neue",sans-serif;font-size:64px;line-height:1;font-weight:600;letter-spacing:-.065em;font-variant-numeric:lining-nums tabular-nums;color:#ecf9ff;text-shadow:0 0 30px #8fceff35;transition:opacity .22s,transform .25s}.score-question-mark{position:absolute;font:400 60px/1 "Avenir Next",sans-serif;opacity:0;transform:translateY(9px) scale(.88);color:#c4eaff;text-shadow:0 0 26px #8bd2ff55;transition:opacity .22s,transform .25s}.score-hover-prompt{position:absolute;top:-3px;left:50%;transform:translate(-50%,-5px);white-space:nowrap;font-size:11px;letter-spacing:.05em;color:#cceaff;opacity:0;transition:opacity .22s,transform .25s}.score-question:hover .score-value>strong,.score-question:focus-visible .score-value>strong{opacity:0;transform:translateY(-4px) scale(.96)}.score-question:hover .score-question-mark,.score-question:focus-visible .score-question-mark{opacity:1;transform:none}.score-question:hover .score-hover-prompt,.score-question:focus-visible .score-hover-prompt{opacity:1;transform:translate(-50%,-8px)}.score-question:hover,.score-question:focus-visible{outline:1px solid #c7e8ff22;outline-offset:0;background:#b5dfff06!important}
.analysis-progress{padding-top:16px}.progress-caption{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:#bad4e6}.standard-progress{display:block;width:100%;height:6px;margin-top:11px;border:0;border-radius:999px;overflow:hidden;appearance:none;background:#b2d5ef1c;accent-color:#a8dcff}.standard-progress::-webkit-progress-bar{background:#b2d5ef1c;border-radius:999px}.standard-progress::-webkit-progress-value{background:linear-gradient(90deg,#689dd6,#b5e5ff);border-radius:999px;transition:width .4s ease}.standard-progress::-moz-progress-bar{background:linear-gradient(90deg,#689dd6,#b5e5ff);border-radius:999px}.completed-groups{display:flex;flex-wrap:wrap;gap:7px 14px;margin-top:12px;color:#b9d9ed;font-size:10px;line-height:1.5}.completed-groups>span{display:inline-flex;align-items:center;gap:5px}.completed-groups i{font-style:normal;color:#a2dfd8}
.score-dock{position:absolute;z-index:35;bottom:0;left:16px;right:16px;height:82px}
.dock-tray{position:absolute;left:0;right:0;bottom:0;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:9px;align-items:end}
.group-card{padding:0;border-radius:17px 17px 0 0;max-height:53px;opacity:.45;overflow:hidden;transition:max-height .36s cubic-bezier(.2,.8,.2,1),opacity .25s,transform .25s,border-color .25s,box-shadow .25s;transform-origin:50% 100%}.group-card:not(.revealed){opacity:.35}.group-card.revealed{opacity:1}.group-card .group-toggle>strong,.group-card .group-detail,.group-card .card-action{opacity:0;transition:opacity .18s}.is-open .group-card{max-height:min(65dvh,580px);border-radius:17px 17px 4px 4px}.is-open .group-toggle>strong,.is-open .group-detail{opacity:1}.is-open .group-card:hover,.is-open .group-card:focus-within{border-color:#e4f5ffc0;box-shadow:0 0 24px #9dd7ff30,0 18px 35px #0005}.group-card.selected{border-color:#a8dfffcc;background-color:#25476344}
.group-card .group-toggle::before{content:"";position:absolute;left:13px;right:13px;top:0;height:3px;border-radius:0 0 3px 3px;background:var(--group-color,#b8e7ff);box-shadow:0 0 9px color-mix(in srgb,var(--group-color,#b8e7ff) 35%,transparent)}
.group-card .group-toggle{position:relative;display:grid;text-align:left;gap:6px;background:none;border:0;box-shadow:none;padding:13px;width:100%;color:inherit}.group-number{font-size:9px;letter-spacing:.12em;color:#92b4ce}.group-title{font-size:12px;white-space:nowrap}.group-card strong{font-size:29px;font-weight:300;line-height:1.2}.group-card strong small{font-size:10px;margin-left:4px}.group-detail,.group-breakdown p{font-size:10px;color:#a4c0d4;margin:0}.card-action{font-size:9px;color:#b9d7e9}.is-open .group-card:hover .card-action,.is-open .group-card.selected .card-action,.is-open .group-card:focus-within .card-action{opacity:1}.group-breakdown{padding:14px 12px 10px;border-bottom:1px solid #b4d8f32b;animation:details-in .2s ease-out}.group-breakdown ul{list-style:none;padding:0;margin:10px 0 0;max-height:min(28dvh,240px);overflow:auto;scrollbar-width:thin}.group-breakdown li{display:flex;align-items:center;justify-content:space-between;gap:8px;border-top:1px solid #a1c7e322;padding:9px 0}.group-breakdown li>span{font-size:11px;line-height:1.5}.group-breakdown li small{display:block;color:#90b0c7;font-size:9px}.group-breakdown li strong{font-size:16px;white-space:nowrap}
@keyframes details-in{from{opacity:0;translate:0 5px}to{opacity:1;translate:0 0}}
.terminate-control{border-color:#e6b8ba66;background:#61444a44;color:#f3d6da}.terminate-control:hover:not(:disabled){background:#81545b66;border-color:#efc1c8aa}
.question-glass{position:absolute;right:24px;top:90px;bottom:auto;width:min(620px,calc(100% - 32px));height:min(760px,calc(100dvh - 130px));min-height:200px;max-width:calc(100% - 24px);max-height:calc(100dvh - 140px);z-index:45;overflow:hidden;padding:0;background-color:#08172bd9;display:flex;flex-direction:column}.question-glass .chat-body{flex:1;min-height:0}.question-glass>header{flex-shrink:0;padding:16px 18px;border-bottom:1px solid #b5dafa25;background:#0d2135e8}.question-glass header{display:flex;align-items:center;justify-content:space-between;margin:0}.question-glass header small{display:block;font-size:10px;color:#9fb9cd;margin-top:5px}.question-glass header button{width:30px;background:none;border:0}.score-reveal-enter-active{transition:opacity .4s,transform .4s}.score-reveal-enter-from{opacity:0;transform:translateY(-12px)}@keyframes arrive{from{opacity:0;translate:0 40px}to{opacity:1;translate:0 0}}@keyframes card-in{from{opacity:0;transform:translateY(40px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:760px){.search-glass{top:76px;width:calc(100% - 20px);padding:12px}.search-line{flex-wrap:wrap;gap:4px}.address-wrap{flex-basis:65%}.search-line .glow{font-size:11px;padding:8px 12px}.horizontal-groups{justify-content:flex-start;gap:10px}.score-dock{left:8px;right:8px}.dock-tray{gap:4px}.group-card .group-toggle{padding:10px 4px;gap:5px}.group-title{font-size:9px;white-space:normal;line-height:1.25}.group-number{font-size:7px;letter-spacing:0}.group-card{max-height:55px}.is-open .group-card{max-height:60dvh}.group-card strong{font-size:23px}.group-detail,.card-action{font-size:8px}.group-breakdown{padding:0 5px 9px}.group-breakdown li{flex-wrap:wrap}.score-guide{font-size:9px;gap:4px}.question-glass{top:76px;bottom:auto;right:12px;height:calc(100dvh - 108px)}.score-question{gap:8px;padding-right:0}.score-value{min-width:120px}.score-value>strong{font-size:48px}.score-context{font-size:11px}.score-context>small{font-size:9px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
