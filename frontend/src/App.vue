<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { ApiError, apiBase, object, realClient, strings, terminal, text, watchJob } from './api.ts'
import type { Catalog, Client, Handle, Job } from './api.ts'
import { mockClient } from './mock.ts'
import type { Scenario } from './mock.ts'
import Report from './Report.vue'
import CompetitionMap from './CompetitionMap.vue'
import CompetitionReport from './CompetitionReport.vue'
import BaiduRoadMap from './BaiduRoadMap.vue'
import { fetchCompetition, parseCompetition, requestDiagnosis, viewFromCompetition } from './competition.ts'
import type { CompetitionPacket, DiagnosisView } from './competition.ts'
import bundledSample from './fixtures/tongji-life-circle-2026-10-08.json'

const mode = ref<'sample' | 'diagnosis' | 'agent' | 'demo'>('sample')
const samplePacket = shallowRef<CompetitionPacket | null>(null)
const liveView = shallowRef<DiagnosisView | null>(null)
const sampleError = ref('')
const scenario = ref<Scenario>('completed')
const base = ref(import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000')
const confirmed = ref(false)
watch(base, () => { confirmed.value = false; if (mode.value !== 'sample') { ready.value = false; catalog.value = null; liveView.value = null } })
const connectedBase = ref('')
const ready = ref(false)
const connecting = ref(false)
const connectionError = ref('')
const error = ref('')
const uncertain = ref(false)
const consent = ref(false)
const busy = ref(false)
const question = ref('')
const placeMode = ref<'address' | 'coordinate'>('address')
const address = ref('')
const city = ref('')
const longitude = ref('')
const latitude = ref('')
const search = ref('')
const catalog = shallowRef<Catalog | null>(null)
const job = shallowRef<Job | null>(null)
const handle = shallowRef<Handle | null>(null)
const client = shallowRef<Client>(mockClient('completed'))
const activeClient = shallowRef<Client | null>(null)
let controller: AbortController | undefined
let catalogController: AbortController | undefined
const labels: Record<string, string> = { queued: '排队中', running: '分析中', completed: '任务已结束', waiting_for_input: '待补充信息', failed: '分析失败' }
const count = computed(() => [...question.value].length)
const groups = computed(() => catalog.value?.groups.map(group => ({ ...group,
  items: catalog.value!.facilities.filter(f => f.group_id === group.id && f.name_zh.includes(search.value.trim())),
})).filter(g => g.items.length) ?? [])
const liveChanged = computed(() => mode.value === 'agent' && base.value !== connectedBase.value)
const canSubmit = computed(() => mode.value !== 'diagnosis' && !busy.value && !connecting.value && ready.value && !liveChanged.value && question.value.trim() && count.value <= 4000 && (mode.value !== 'agent' || !uncertain.value || consent.value) && !handle.value)
const activeStatus = computed(() => job.value?.status ?? handle.value?.status)
const sampleCenter = { lng: 121.508602, lat: 31.288268, crs: 'BD09LL' }
const pickedCenter = computed(() => placeMode.value === 'coordinate' && longitude.value.trim() && latitude.value.trim()
  ? { lng: Number(longitude.value), lat: Number(latitude.value), crs: 'BD09LL' as const } : null)
const canDiagnose = computed(() => !busy.value && !connecting.value && ready.value && base.value === connectedBase.value
  && (placeMode.value === 'address' ? Boolean(address.value.trim()) : Boolean(pickedCenter.value)
    && Math.abs(Number(longitude.value)) <= 180 && Math.abs(Number(latitude.value)) <= 90))
const activeView = computed(() => mode.value === 'sample'
  ? samplePacket.value ? viewFromCompetition(samplePacket.value) : null
  : mode.value === 'diagnosis' ? liveView.value : null)
const resultCenter = computed(() => {
  const center = object(job.value?.result?.resolved_center)
  return typeof center.lng === 'number' && Number.isFinite(center.lng)
    && typeof center.lat === 'number' && Number.isFinite(center.lat)
    && Math.abs(center.lng) <= 180 && Math.abs(center.lat) <= 90
    && center.crs === 'BD09LL'
    ? { lng: center.lng, lat: center.lat, crs: 'BD09LL' }
    : null
})
const roadMapCenter = computed(() => liveView.value?.center ?? pickedCenter.value ?? resultCenter.value ?? sampleCenter)
const roadMapCaption = computed(() => mode.value === 'diagnosis'
  ? pickedCenter.value ? '已选定分析中心 · 点击地图可重新选点' : '请输入地址，或在地图上选择体检中心'
  : resultCenter.value ? 'Agent 分析中心已定位。此视图不展示 Tool 1–6 空间结果。' : '请先完成分析以定位中心；当前地图仅作位置参考。')
const download = (value: unknown, filename: string) => {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
const exportResult = () => {
  if (job.value) download({ source: mode.value === 'demo' ? 'synthetic-ui-demo' : 'backend-response', job: job.value }, `${mode.value === 'demo' ? '合成示例' : '生活圈结果'}.json`)
}
function loadBundledSample() {
  samplePacket.value = null; sampleError.value = ''
  try { samplePacket.value = parseCompetition(bundledSample) }
  catch (e) { sampleError.value = e instanceof Error ? e.message : '样例不可用。' }
}
async function loadServerSample() {
  if (!confirmed.value || connecting.value) return
  catalogController?.abort(); catalogController = new AbortController()
  connecting.value = true; samplePacket.value = null; sampleError.value = ''
  try { samplePacket.value = await fetchCompetition(base.value, catalogController.signal) }
  catch (e) { sampleError.value = e instanceof Error ? e.message : '后端样例读取失败。' }
  finally { connecting.value = false }
}
async function connect() {
  if ((mode.value === 'diagnosis' || mode.value === 'agent') && !confirmed.value) return
  catalogController?.abort()
  catalogController = new AbortController()
  connecting.value = true; ready.value = false; catalog.value = null; connectionError.value = ''
  try {
    if (mode.value === 'diagnosis') {
      const response = await fetch(new URL('/openapi.json', apiBase(base.value)), { signal: catalogController.signal, cache: 'no-store', credentials: 'omit', redirect: 'error' })
      if (!response.ok) throw new Error(`无法读取接口说明（HTTP ${response.status}）。`)
      const spec = object(await response.json())
      if (!object(object(spec.paths)['/api/v1/diagnosis']).post) throw new Error('此服务没有提供 V1 社区体检接口。')
    } else {
      client.value = mode.value === 'demo' ? mockClient(scenario.value) : realClient(base.value)
      catalog.value = await client.value.catalog(catalogController.signal)
    }
    connectedBase.value = base.value; ready.value = true
  } catch (e) { connectionError.value = e instanceof Error ? e.message : '无法加载设施目录。' }
  finally { connecting.value = false }
}
function changeMode() {
  controller?.abort(); job.value = null; handle.value = null; activeClient.value = null
  liveView.value = null
  error.value = ''; consent.value = false; ready.value = false; catalog.value = null; connectionError.value = ''
  confirmed.value = false
  samplePacket.value = null; sampleError.value = ''
  if (mode.value === 'sample') loadBundledSample()
  if (mode.value === 'demo' || mode.value === 'diagnosis') void connect()
}
function addFacility(name: string) {
  if (question.value.includes(name)) return
  question.value = `${question.value.trim()}${question.value.trim() ? '；重点关注' : '请分析'}${name}`
}
async function poll() {
  if (!handle.value || !activeClient.value) return
  controller = new AbortController(); busy.value = true; error.value = ''
  try {
    await watchJob(activeClient.value, handle.value, controller.signal, value => { job.value = value })
    handle.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '查询中断，请重试状态查询。'
    if (e instanceof ApiError && e.code === '404') handle.value = null
  } finally { busy.value = false }
}
async function submitDiagnosis() {
  if (!canDiagnose.value) return
  controller = new AbortController(); busy.value = true; error.value = ''; liveView.value = null
  const requestedAt = new Date().toISOString()
  const location = placeMode.value === 'coordinate' && pickedCenter.value
    ? { type: 'coordinate' as const, ...pickedCenter.value }
    : { type: 'address' as const, query: address.value.trim(), ...(city.value.trim() ? { city: city.value.trim() } : {}) }
  try {
    const diagnosis = await requestDiagnosis(base.value, location, controller.signal)
    const data = object(diagnosis.data)
    const center = object(data.center)
    if (typeof center.lng !== 'number' || typeof center.lat !== 'number' || center.crs !== 'BD09LL') throw new ApiError('contract', '体检结果缺少有效的 BD09LL 分析中心。')
    const label = typeof data.location_label === 'string' && data.location_label.trim()
      ? data.location_label : placeMode.value === 'address' ? address.value.trim() : '地图选点'
    liveView.value = { diagnosis, center: { lng: center.lng, lat: center.lat, crs: 'BD09LL' }, label,
      source: '本次实时体检', collectedAt: requestedAt }
  } catch (e) { error.value = e instanceof Error ? e.message : '体检请求失败。' }
  finally { busy.value = false }
}
function mapSelect(center: { lng: number; lat: number; crs?: string }) {
  placeMode.value = 'coordinate'; longitude.value = String(center.lng); latitude.value = String(center.lat)
  liveView.value = null
}
function useAddress() { placeMode.value = 'address'; liveView.value = null }
function useCoordinate() { placeMode.value = 'coordinate'; liveView.value = null }
async function submit() {
  if (mode.value === 'diagnosis') { await submitDiagnosis(); return }
  if (!canSubmit.value) return
  controller = new AbortController(); busy.value = true; error.value = ''; job.value = null; consent.value = false
  if (mode.value === 'agent') uncertain.value = false
  activeClient.value = mode.value === 'demo' ? mockClient(scenario.value) : client.value
  try {
    handle.value = await activeClient.value.submit(question.value, controller.signal)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '提交失败。'
    if (mode.value === 'agent') uncertain.value = e instanceof ApiError && e.code === 'uncertain'
    busy.value = false; return
  }
  await poll()
}
onMounted(loadBundledSample)
onUnmounted(() => { controller?.abort(); catalogController?.abort() })
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div><p class="eyebrow">15-MINUTE LIFE CIRCLE</p><h1>15 分钟生活圈助手</h1></div>
      <span class="status-badge">{{ mode === 'sample' ? '历史分析 · 2026-10-08' : mode === 'demo' ? '流程预览 · 模拟数据' : ready && !liveChanged ? '分析服务已连接' : '分析服务待连接' }}</span>
    </header>
    <main class="workspace">
      <section class="map-panel" :class="{ 'sample-map-panel': mode === 'sample', 'road-map-panel': mode !== 'sample' }" :aria-label="mode === 'sample' ? '历史分析空间图' : '道路底图与分析中心位置'">
        <CompetitionMap v-if="activeView" :view="activeView" />
        <div v-else-if="mode === 'sample'" class="sample-map-empty">{{ connecting ? '正在读取固定样例…' : '样例未加载，暂不绘制图层。' }}</div>
        <BaiduRoadMap v-else :center="roadMapCenter" :show-center="Boolean(liveView || pickedCenter || resultCenter)" :caption="roadMapCaption" :selectable="mode === 'diagnosis' && !liveView" @select="mapSelect" />
      </section>
      <aside class="panel input-panel" aria-labelledby="input-title">
        <p class="section-label">01 / 开始探索</p><h2 id="input-title">了解你的生活圈</h2>
        <label class="field">查看内容
          <select v-model="mode" :disabled="busy || connecting || !!handle" @change="changeMode"><option value="sample">同济历史分析（2026-10-08）</option><option value="diagnosis">新地点体检（三类设施）</option><option value="agent">自然语言规划</option><option value="demo">流程预览（模拟数据）</option></select>
        </label>
        <template v-if="mode === 'sample'">
          <p class="notice small">展示 2026 年 10 月 8 日保存的历史分析，不会随地点输入改变，也不会重新搜索设施或规划路线。道路底图单独加载；图上的范围与盲区来自当时的分析结果，不是实时边界。</p>
          <h3>{{ samplePacket?.sample.label ?? '固定样例' }}</h3>
          <p class="small muted">先查看空间图与右侧体检指标。点选设施可查看已保存的步行距离与用时。分析其他地点可切换到“三类设施体检”。</p>
          <button class="secondary" :disabled="connecting" @click="loadBundledSample">恢复同济历史报告</button>
          <details><summary>读取分析服务中的历史报告</summary>
            <label class="field">样例服务地址<input v-model="base" type="url" :disabled="connecting" /></label>
            <label class="checkbox"><input v-model="confirmed" type="checkbox" :disabled="connecting" />此地址已由后端负责人确认</label>
            <button class="secondary" :disabled="!confirmed || connecting" @click="loadServerSample">{{ connecting ? '正在读取…' : '读取服务端固定样例' }}</button>
            <p class="small muted">只读取已保存的报告，不重新采集；失败不会自动替换为其他数据。</p>
          </details>
          <p v-if="sampleError" role="alert" class="notice">{{ sampleError }}</p>
          <p class="small muted">当前等时圈为步行路径采样近似范围，盲区为距离代理网格估算；均未沿街道或建筑轮廓绘制。</p>
        </template>
        <template v-if="mode === 'diagnosis'">
          <p class="notice small">按 15 分钟步行范围体检菜市场、药店和小学。选择地址或 BD09LL 地图坐标；提交后会调用确认的后端服务。</p>
          <label class="field">后端服务地址<input v-model="base" type="url" :disabled="busy || connecting" placeholder="http://127.0.0.1:8000" /></label>
          <label class="checkbox"><input v-model="confirmed" type="checkbox" :disabled="busy || connecting" />此地址已由后端负责人确认</label>
          <button class="secondary" :disabled="!confirmed || busy || connecting" @click="connect">{{ connecting ? '正在核对接口…' : '核对体检接口' }}</button>
          <p v-if="ready && base === connectedBase" class="small muted">体检接口已核对。提交会实时调用百度地图服务，可能消耗服务端额度。</p>
          <div class="place-choice"><button type="button" class="secondary" :aria-pressed="placeMode === 'address'" @click="useAddress">输入地址</button><button type="button" class="secondary" :aria-pressed="placeMode === 'coordinate'" @click="useCoordinate">地图选点</button></div>
          <label v-if="placeMode === 'address'" class="field">分析地点<input v-model="address" type="text" maxlength="300" placeholder="例如：上海市杨浦区同济大学四平路校区" /></label>
          <label v-if="placeMode === 'address'" class="field">城市（可选，用于地址消歧）<input v-model="city" type="text" maxlength="80" placeholder="例如：上海市" /></label>
          <template v-else>
            <p class="small muted">在地图上点击选择中心，或直接填写经纬度。坐标系按百度地图 BD09LL 发送。</p>
            <div class="coordinate-fields"><label class="field">经度<input v-model="longitude" type="number" step="any" min="-180" max="180" placeholder="121.508602" /></label><label class="field">纬度<input v-model="latitude" type="number" step="any" min="-90" max="90" placeholder="31.288268" /></label></div>
          </template>
          <p v-if="connectionError" role="alert" class="notice">{{ connectionError }}</p>
          <p v-if="error" role="alert" class="notice">{{ error }}</p>
          <button type="button" :disabled="!canDiagnose" @click="submitDiagnosis">{{ busy ? '正在进行体检…' : '开始生活圈体检' }}</button>
          <button v-if="busy" type="button" class="secondary" @click="controller?.abort()">停止等待</button>
          <p v-if="busy" class="small muted">停止等待不会取消后端任务，服务端可能仍在分析。</p>
        </template>
        <template v-if="mode === 'demo'">
          <p class="notice small">流程预览使用模拟任务和模拟结果，不会真实搜索设施或调用地图、模型服务。</p>
          <label class="field">演示场景
            <select v-model="scenario" :disabled="busy || !!handle"><option value="completed">逐类规划报告</option><option value="waiting_for_input">待补充信息</option><option value="partial">0 / 部分结果 / 未知</option><option value="failed">运行失败</option><option value="empty">任务结束但结果为空</option></select>
          </label>
        </template>
        <template v-else-if="mode === 'agent'">
          <label class="field">后端服务地址<input v-model="base" type="url" :disabled="busy || connecting || !!handle" placeholder="https://your-backend.example" /></label>
          <label class="checkbox"><input v-model="confirmed" type="checkbox" :disabled="busy || connecting || !!handle" />此地址已由后端负责人确认</label>
          <button class="secondary" :disabled="!confirmed || busy || connecting || !!handle" @click="connect">{{ connecting ? '正在核对接口…' : '核对接口并加载目录' }}</button>
          <p class="muted small">仅读取实时接口与目录。提交分析后将调用该服务，可能消耗地图和模型额度。</p>
        </template>
        <template v-if="mode === 'agent' || mode === 'demo'">
        <p v-if="connectionError" role="alert" class="notice">{{ connectionError }}</p>
        <p v-if="liveChanged && ready" class="notice small">地址已改变，请重新核对接口后提交。</p>
        <form @submit.prevent="submit">
          <label class="field" for="question">地点与问题</label>
          <textarea id="question" v-model="question" rows="5" placeholder="例如：上海市杨浦区同济大学附近，菜市场和药店是否方便到达？" :disabled="busy || !!handle" aria-describedby="question-note" />
          <p id="question-note" class="muted small">请写明城市、地点与设施需求。{{ count }} / 4000 字</p>
          <p v-if="error" class="notice" role="alert">{{ error }}</p>
          <label v-if="mode === 'agent' && uncertain" class="checkbox"><input v-model="consent" type="checkbox" />上次提交结果未知。我知道重试可能创建重复任务，仍要重新提交</label>
          <button type="submit" :disabled="!canSubmit">{{ busy ? '正在处理…' : mode === 'demo' ? '运行合成示例' : '提交分析问题' }}</button>
        </form>
        <div v-if="handle" class="job-controls">
          <button v-if="busy" type="button" class="secondary" @click="controller?.abort()">暂停查询</button>
          <button v-else type="button" class="secondary" @click="poll">继续查询原任务</button>
          <p class="small muted">暂停查询不会取消后端任务。查询中断时不会重新提交问题。</p>
        </div>
        <details class="catalog"><summary>设施目录{{ catalog ? ` · ${catalog.facilities.length} 类` : '' }}</summary>
          <p class="small muted">{{ mode === 'demo' ? '本地示例目录' : '当前服务目录' }} · {{ catalog?.catalog_version ?? '未加载' }}</p>
          <label class="field">查找设施<input v-model="search" type="search" placeholder="输入类别名称" /></label>
          <p class="small muted">点击类别会将它补充到上方问题中。</p>
          <section v-for="group in groups" :key="group.id"><h3>{{ group.name_zh }}</h3><details v-for="facility in group.items" :key="facility.id">
            <summary>{{ facility.name_zh }}</summary>
            <p class="small">{{ text(facility.data_notes) }}</p><p class="small muted">{{ text(facility.basis) }}</p>
            <p v-for="line in strings(facility.limitations)" :key="line" class="small muted">{{ line }}</p>
            <button class="link-button" :disabled="busy || !!handle" @click="addFacility(facility.name_zh)">加入问题</button>
          </details></section>
        </details>
        </template>
      </aside>
      <aside class="panel result-panel" aria-labelledby="result-title" :aria-busy="busy">
        <p class="section-label">02 / 分析结果</p><h2 id="result-title">社区分析报告</h2>
        <template v-if="mode === 'sample'">
          <CompetitionReport v-if="activeView" :view="activeView" />
          <p v-else class="notice">{{ sampleError || (connecting ? '正在读取固定样例…' : '暂无样例结果。') }}</p>
          <button v-if="samplePacket" class="secondary" @click="download(samplePacket, `同济已采集样例-${samplePacket.sample.data_date}.json`)">下载已采集样例 JSON</button>
        </template>
        <template v-else-if="mode === 'diagnosis'">
          <CompetitionReport v-if="liveView" :key="`${liveView.center.lng}-${liveView.center.lat}-${liveView.collectedAt}`" :view="liveView" />
          <p v-else-if="busy" class="job-status" role="status">正在按后端顺序完成位置解析、等时圈、设施检索、步行路线与覆盖分析…</p>
          <p v-else-if="error" class="notice" role="alert">{{ error }}</p>
          <p v-else class="muted">输入地点或在地图上选择中心，完成体检后会在这里显示综合结论和分类指标。</p>
          <button v-if="liveView" class="secondary" @click="download(liveView.diagnosis, '生活圈体检结果.json')">下载本次体检 JSON</button>
        </template>
        <template v-else>
        <div class="job-status" role="status" aria-live="polite">{{ activeStatus ? labels[activeStatus] : '等待你的问题' }}</div>
        <p v-if="busy && activeStatus && !terminal(activeStatus)" class="muted small">{{ mode === 'demo' ? '正在模拟任务流程。' : '正在等待后端；暂不提供百分比进度。' }}</p>
        <p v-if="mode === 'demo' && job" class="notice small">以下全部为合成 UI 示例，不作为真实分析或验收结论。</p>
        <template v-if="job && terminal(job.status)">
          <p class="muted small">运行状态：{{ job.result ? labels[job.result.status] : '未返回结果' }}</p>
          <Report v-if="job.result" :key="job.job_id" :run="job.result" :catalog="catalog" />
          <p v-else class="notice">任务已结束，但没有返回结果。请联系后端核对；不能据此生成报告。</p>
          <button class="secondary" @click="exportResult">下载本次结果 JSON</button>
        </template>
        <p v-else-if="!busy && !handle" class="muted">输入一个地点和你关心的问题。答案、逐类规划、所引证据与限制将在这里呈现。</p>
        <details v-if="handle || job" class="small"><summary>任务信息</summary><p class="preserve">{{ handle?.job_id ?? job?.job_id }}</p><p>任务记录由后端临时保存，刷新页面后本页不会自动恢复。收到结果后可下载保存。</p></details>
        <p v-if="job?.result?.status === 'failed'" class="notice">运行失败，不是成功报告。请参考错误说明后再决定是否提交。</p>
        </template>
      </aside>
    </main>
  </div>
</template>
