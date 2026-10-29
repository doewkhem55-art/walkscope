<script setup lang="ts">
import { computed } from 'vue'
import type { CheckupCatalog, CheckupReport } from './checkups.ts'
const props = defineProps<{ report: CheckupReport; catalog: CheckupCatalog }>()
const score = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : '暂不计分'
const rows = computed(() => props.report.groups.map(group => {
  const members = props.report.categories.filter(c => c.group_id === group.id)
  const memberIds = new Set(members.map(c => c.facility_id))
  const ids = new Set(props.report.pois.filter(p => p.in_isochrone === true && memberIds.has(String(p.facility_id))).map(p => p.poi_id))
  return { ...group, facilityCount: ids.size, complete: members.every(c => c.search_status === 'complete') }
}))
const historical = computed(() => props.report.quality.historical_replay === true || props.report.quality.historical_subset_replay === true)
const completeCount = computed(() => props.report.categories.filter(item => item.search_status === 'complete').length)
</script>

<template>
  <div id="checkup-report" class="checkup-report">
    <section class="score-card" aria-label="设施配置参考分">
      <span>设施配置参考分</span>
      <strong>{{ score(report.score) }}<small v-if="report.score_provisional">暂估</small></strong>
    </section>
    <p v-if="completeCount < report.categories.length" class="incomplete-note">部分结果 · {{ completeCount }}/{{ report.categories.length }} 类检索完整</p>
    <details v-for="group in rows" :key="group.id" :id="`checkup-group-${group.id}`" class="score-group">
      <summary class="group-heading">
        <span>{{ group.name_zh }}<small v-if="group.score_provisional">暂估</small></span>
        <strong>{{ score(group.score) }}</strong><span class="group-arrow" aria-hidden="true">⌄</span>
      </summary>
      <p class="facility-count">{{ group.complete ? '圈内设施' : '圈内已找到' }} <strong>{{ group.facilityCount }}</strong> 处</p>
    </details>
    <details class="analysis-hints">
      <summary aria-label="展开分析提示" title="分析提示"><span aria-hidden="true">ⓘ</span> 分析提示</summary>
      <div class="hint-content">
        <p v-if="historical">当前展示已保存报告。</p>
        <p v-if="report.catalog_version !== catalog.version">当前目录与报告目录版本不同，数量与评分按该报告显示。</p>
        <p>设施数量按圈内独立点位去重；分数由服务端提供。设施配置参考分按数量分档，不是覆盖率、法定达标率或服务容量评价。</p>
        <p>用于自然资源和规划主管部门的前期基础排查。范围为指定中心点生成的步行圈，不代表完整行政社区；布局建议需结合人口、容量、开放条件和现场调查复核。</p>
        <p v-if="report.score_provisional || report.score == null">检索未齐的结果保留暂估或未计分状态。</p>
        <p v-for="(warning, index) in report.warnings" :key="index">{{ warning.message }}</p>
      </div>
    </details>
  </div>
</template>

<style scoped>
.checkup-report { display: grid; gap: 0; color: var(--ink); font-size: 13px; }
.score-card { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0 20px; }
.score-card > span { color: var(--muted); font-size: 13px; }
.score-card > strong { color: #e7f7ff; font-size: 42px; line-height: 1.1; font-weight: 400; letter-spacing: -.04em; }
.score-card small, .group-heading small { margin-left: 7px; color: #a8dfff; font-size: 10px; font-weight: 400; letter-spacing: 0; }
.incomplete-note { color: #a8dfff; font-size: 11px; padding: 0 0 12px; }
.score-group { border-top: 1px solid var(--line); }
.group-heading { display: grid; grid-template-columns: minmax(0,1fr) auto 14px; align-items: center; gap: 8px; padding: 8px 0; list-style: none; font-size: 13px; font-weight: 400; }
.group-heading::-webkit-details-marker { display: none; }
.group-heading strong { color: #b8e7ff; font-size: 18px; font-weight: 400; }
.group-arrow { color: var(--muted); transition: transform .15s; }
.score-group[open] .group-arrow { transform: rotate(180deg); }
.facility-count { padding: 0 0 14px; color: var(--muted); font-size: 12px; }
.facility-count strong { color: var(--ink); margin: 0 4px; font-weight: 500; }
.analysis-hints { margin-top: 12px; }
.analysis-hints > summary { width: fit-content; padding: 5px 0; color: var(--muted); font-size: 11px; font-weight: 400; list-style: none; }
.analysis-hints > summary::-webkit-details-marker { display: none; }
.analysis-hints > summary span { font-size: 15px; margin-right: 4px; }
.hint-content { display: grid; gap: 7px; margin-top: 7px; padding: 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); font-size: 11px; line-height: 1.65; }
</style>
