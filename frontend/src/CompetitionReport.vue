<script setup lang="ts">
import { computed } from 'vue'
import { object, rows } from './api.ts'
import { calculateReferenceScore, type DiagnosisView } from './competition.ts'
import StructuredData from './StructuredData.vue'

const props = defineProps<{ view: DiagnosisView }>()
const metrics = computed(() => rows(props.view.diagnosis.data.metrics))
const blindspot = computed(() => object(props.view.diagnosis.data.blindspot))
const isochrone = computed(() => object(props.view.diagnosis.data.isochrone))
const stages = computed(() => object(props.view.diagnosis.data.stages))
const quality = computed(() => object(props.view.diagnosis.data.quality))
const warnings = computed(() => rows(props.view.diagnosis.warnings))
const hasGridBasis = computed(() => Number.isInteger(blindspot.value.service_distance_m) && Number.isInteger(blindspot.value.grid_size_m))
const summary = computed(() => {
  const confirmed = metrics.value.filter(metric => typeof metric.confirmed_reachable_15m_count === 'number' && Number.isFinite(metric.confirmed_reachable_15m_count))
  const coverage = metrics.value.map(metric => metric.covered_ratio).filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  const unknown = metrics.value.map(metric => metric.unknown_ratio).filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  const weakest = metrics.value.filter(metric => typeof metric.covered_ratio === 'number' && Number.isFinite(metric.covered_ratio))
    .sort((a, b) => Number(a.covered_ratio) - Number(b.covered_ratio))[0]
  const score = calculateReferenceScore(metrics.value)
  const named = confirmed.map(metric => `${name(metric.facility_type)} ${count(metric.confirmed_reachable_15m_count)} 家`).join('、')
  const range = (values: number[]) => values.length
    ? values.length === 1 ? percent(values[0]) : `${percent(Math.min(...values))}–${percent(Math.max(...values))}`
    : '未提供'
  return {
    confirmed: named || '没有可确认的分类数据',
    coverage: range(coverage),
    unknown: range(unknown),
    weakest: weakest ? `${name(weakest.facility_type)}（${percent(weakest.covered_ratio)}）` : '未提供',
    hasCoverage: coverage.length > 0,
    score,
  }
})
const facilityNames: Record<string, string> = { market: '菜市场', pharmacy: '药店', primary_school: '小学' }
const stageNames: Record<string, string> = { complete: '完成', partial: '部分完成', unavailable: '不可用', skipped: '未执行' }
const name = (value: unknown) => facilityNames[String(value)] ?? String(value ?? '未知类别')
const count = (value: unknown) => typeof value === 'number' && Number.isInteger(value) ? value.toLocaleString('zh-CN') : '未知 / 未返回'
const distance = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${value.toLocaleString('zh-CN')} 米` : '未知 / 未返回'
const duration = (value: unknown) => {
  if (typeof value !== 'number' || !Number.isInteger(value)) return '未知 / 未返回'
  return `${Math.floor(value / 60)} 分 ${value % 60} 秒`
}
const percent = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '未知 / 未返回'
const stage = (value: unknown) => stageNames[String(value)] ?? '未知'
const confidence = (value: unknown) => ({ high: '高', medium: '中', low: '低' } as Record<string, string>)[String(value)] ?? '未知'
const timeLimit = computed(() => typeof isochrone.value.time_limit_s === 'number' && Number.isFinite(isochrone.value.time_limit_s)
  ? isochrone.value.time_limit_s === 900 ? '15 分钟' : `${count(isochrone.value.time_limit_s)} 秒`
  : '规定时限')
const collectionTime = (value: string | null) => {
  if (!value) return '未提供'
  const parts = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/)
  return parts ? `${parts[1]}年${parts[2]}月${parts[3]}日 ${parts[4]} ${parts[5] === 'Z' ? 'UTC' : `UTC${parts[5]}`}` : value
}
const calendarDate = (value: string) => value.replace(/^(\d{4})-(\d{2})-(\d{2})$/, (_, year, month, day) => `${year}年${Number(month)}月${Number(day)}日`)
</script>

<template>
  <section class="competition-report" aria-labelledby="competition-title">
    <h2 id="competition-title">{{ view.label }}</h2>
    <p class="notice fixed-note">{{ view.dataDate ? `这是 ${calendarDate(view.dataDate)} 保存的历史分析。` : `这是 ${view.source}，请求开始时间（本机）为 ${collectionTime(view.collectedAt ?? null)}。` }}设施、路线和诊断图层来自同一份体检结果。</p>
    <section class="summary-card" aria-labelledby="summary-title">
      <p class="summary-kicker">综合结论</p>
      <div v-if="summary.score !== null" class="score-line"><span>三类空间覆盖参考分</span><strong>{{ summary.score }}<small> / 100</small></strong></div>
      <h3 id="summary-title">已确认可达：{{ summary.confirmed }}</h3>
      <p v-if="summary.hasCoverage">按 {{ distance(blindspot.service_distance_m) }} 服务距离代理与 {{ count(blindspot.grid_size_m) }} 米网格估算，覆盖比例为 {{ summary.coverage }}，证据不足区域为 {{ summary.unknown }}。</p>
      <p v-else>当前记录没有足够空间数据计算覆盖估算。</p>
      <p v-if="summary.hasCoverage">当前估算中，{{ summary.weakest }}类别覆盖最低。建议优先核查该类设施的步行通达与数据完整性。</p>
      <p class="summary-caveat">{{ summary.score !== null ? '参考分取三类等权覆盖估算区间的中点，并按 2.5 分为一档取整；未知区域会扩大不确定性，因此需结合置信度解读。' : '部分空间数据不可用，因此不计算参考分。' }}本次整体置信度为“{{ confidence(quality.confidence) }}”，盲区分析{{ stage(stages.blindspot) }}。参考分依据 {{ distance(blindspot.service_distance_m) }}服务距离代理和{{ count(blindspot.grid_size_m) }}米网格，不等于15分钟步行可达评分。</p>
    </section>
    <dl class="sample-meta">
      <div><dt>{{ view.dataDate ? '数据日期' : '请求开始时间（本机）' }}</dt><dd>{{ view.dataDate ? calendarDate(view.dataDate) : collectionTime(view.collectedAt ?? null) }}</dd></div>
      <div><dt>分析中心（BD09LL）</dt><dd>{{ view.center.lng }}, {{ view.center.lat }}</dd></div>
      <div><dt>数据来源</dt><dd>{{ view.source }}</dd></div>
    </dl>

    <p class="muted small">检索候选数与中心出发 {{ timeLimit }}内确认可达数分开统计，后者按路线时长判断。</p>
    <p v-if="hasGridBasis" class="muted small">空间比例以 {{ distance(blindspot.service_distance_m) }} 服务距离为代理、按 {{ count(blindspot.grid_size_m) }} 米网格估算，不等于 {{ timeLimit }}步行可达比例。</p>
    <p v-else class="muted small">空间比例口径未提供，不能据此推断 {{ timeLimit }}内的可达范围。</p>
    <p v-if="stages.blindspot === 'partial'" class="notice">盲区分析仅部分完成。请结合数据日期、采集时间和下方提示解读。</p>
    <p class="small muted">盲区分析：{{ stage(stages.blindspot) }} · 数据可信度：{{ confidence(quality.confidence) }}</p>

    <section class="metric-grid" aria-label="设施类别指标">
      <article v-for="metric in metrics" :key="String(metric.facility_type)" class="metric-card">
        <h3>{{ name(metric.facility_type) }}</h3>
        <p>检索候选：{{ count(metric.poi_count) }}</p>
        <p>中心出发 {{ timeLimit }}内确认可达：{{ count(metric.confirmed_reachable_15m_count) }}</p>
        <p>最近步行距离：{{ distance(metric.nearest_walk_distance_m) }}</p>
        <p>最近步行时长：{{ duration(metric.nearest_walk_duration_s) }}</p>
        <dl class="ratios">
          <div><dt>网格覆盖</dt><dd>{{ percent(metric.covered_ratio) }}</dd></div>
          <div><dt>盲区</dt><dd>{{ percent(metric.blind_ratio) }}</dd></div>
          <div><dt>未知</dt><dd>{{ percent(metric.unknown_ratio) }}</dd></div>
        </dl>
      </article>
    </section>

    <section v-if="warnings.length" class="notice warnings" aria-label="数据提示">
      <h3>数据提示（{{ warnings.length }}）</h3>
      <p v-for="(warning, index) in warnings" :key="`${warning.code}-${index}`">
        <strong>提示</strong> {{ warning.message ?? '未提供说明' }}
      </p>
    </section>

    <details class="raw-data"><summary>展开完整诊断数据</summary><StructuredData :value="view.diagnosis.data" /></details>
  </section>
</template>

<style scoped>
.competition-report { display: grid; gap: 0.8rem; }
.competition-report h2, .competition-report h3, .competition-report p { margin: 0; }
.summary-card { display: grid; gap: .5rem; padding: .9rem; border: 2px solid #788b2e; border-radius: .7rem; background: #f2f5e6; }
.summary-kicker { color: #5d694d; font-size: .78rem; font-weight: 800; }
.summary-card h3 { font-size: 1rem; line-height: 1.5; }
.summary-card p { line-height: 1.6; }
.score-line { display: flex; align-items: baseline; justify-content: space-between; gap: .8rem; padding: .65rem .75rem; border: 1px solid #788b2e; background: white; font-weight: 800; }
.score-line strong { color: #424d15; font-size: 1.45rem; white-space: nowrap; }
.score-line small { font-size: .75rem; }
.summary-caveat { border-top: 1px solid #c6cea1; padding-top: .5rem; font-size: .8rem; }
.sample-meta, .ratios { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: 0.6rem; margin: 0; }
.sample-meta > div, .ratios > div, .metric-card { border: 1px solid #dfe5d4; border-radius: 0.7rem; padding: 0.7rem; }
.sample-meta > div { display: block; }
.sample-meta dt, .ratios dt { color: #5d694d; font-size: 0.8rem; }
.sample-meta dd, .ratios dd { margin: 0.2rem 0 0; overflow-wrap: anywhere; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); gap: 0.7rem; }
.metric-card { display: grid; gap: 0.4rem; }
.metric-card h3 { margin-bottom: 0.2rem; }
.ratios { margin-top: 0.3rem; }
.fixed-note, .warnings { padding: 0.7rem; border-radius: 0.6rem; }
.warnings { display: grid; gap: 0.4rem; }
.raw-data { overflow-wrap: anywhere; }
</style>
