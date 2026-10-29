<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { object, rows, strings, text } from './api.ts'
import type { Run, Catalog, JsonObject } from './api.ts'
import StructuredData from './StructuredData.vue'
const props = defineProps<{ run: Run; catalog: Catalog | null }>()
const selected = ref('')
const selectedFact = ref<JsonObject | null>(null)
const factPanel = ref<HTMLElement | null>(null)
const proposal = computed(() => object(props.run.planning_proposal))
const dossiers = computed(() => rows(proposal.value.facility_dossiers))
const evidence = computed(() => rows(object(props.run.planning_evidence_bundle).items))
const factMap = computed(() => new Map(evidence.value.flatMap(item => [
  [text(item.evidence_id), item] as const,
  ...rows(item.facts).map(fact => [text(fact.fact_id), fact] as const),
])))
const counts = computed(() => rows(props.run.poi_results).flatMap(result => result.ok === true ? rows(object(result.data).counts) : []))
const ids = computed(() => [...new Set([...dossiers.value, ...counts.value, ...evidence.value, ...rows(proposal.value.actions)].map(x => text(x.facility_id)))])
const match = (item: JsonObject) => !selected.value || item.facility_id === selected.value
const references = (item: JsonObject) => [...new Set([...strings(item.fact_ids), ...strings(item.evidence_refs)])]
const name = (id: unknown) => props.catalog?.facilities.find(f => f.id === id)?.name_zh ?? text(id)
const reviewLabel = computed(() => ({ approved: '提案审查通过', revision_required: '提案需要修改', insufficient_evidence: '审查认为证据不足' })[String(object(props.run.review_result).status)] ?? '尚无审查结果')
const disposition = (v: unknown) => ({ recommendation_supported: '有建议依据', evidence_insufficient: '证据不足', metric_not_applicable: '指标不适用' })[String(v)] ?? text(v)
const sectionLabels: Record<string, string> = { existing_basis: '现状依据', spatial_diagnosis: '空间诊断', planning_goal: '规划目标', priority: '优先级', implementation_steps: '实施步骤', verification: '核验要求', limitations: '限制', preconditions: '前提条件' }
const openFact = async (id: string) => {
  selectedFact.value = factMap.value.get(id) ?? { statement: '本次结果中未找到该引用，请核对后端证据。', fact_id: id }
  await nextTick(); factPanel.value?.focus(); factPanel.value?.scrollIntoView({ block: 'nearest' })
}
const rawKeys = ['brief', 'resolved_center', 'poi_results', 'routing', 'diagnosis', 'community_diagnosis', 'planning_evidence_bundle']
</script>

<template>
  <div class="report-content">
    <p class="answer preserve">{{ text(run.answer, '本次未提供文字答案，请查看状态、限制和错误。') }}</p>
    <div v-if="strings(run.limitations).length || rows(run.errors).length || rows(run.warnings).length" class="notice" role="status">
      <strong>数据限制与提示</strong>
      <p v-for="(line, i) in strings(run.limitations)" :key="i">{{ line }}</p>
      <p v-for="(error, i) in rows(run.errors)" :key="`error-${i}`">{{ text(error.message) }} <small>{{ text(error.code, '') }}</small></p>
      <p v-for="(warning, i) in rows(run.warnings)" :key="`warning-${i}`">{{ text(warning.message) }}</p>
    </div>
    <p v-if="run.status === 'waiting_for_input'" class="notice">请在左侧补全地点与需求，再提交一个新问题。</p>
    <label v-if="ids.length" class="field">按类别查看
      <select v-model="selected"><option value="">全部类别</option><option v-for="id in ids" :key="id" :value="id">{{ name(id) }}</option></select>
    </label>
    <section v-if="counts.length">
      <h3>设施检索</h3>
      <div v-for="(item, i) in counts.filter(match)" :key="i" class="count-row">
        <strong>{{ name(item.facility_id) }}</strong>
        <span v-if="item.status === 'unavailable' || item.count == null">未知 · 数据不可用</span>
        <span v-else-if="item.status === 'partial'">已检索到 {{ item.count }} 个 · 部分结果</span>
        <span v-else-if="item.status === 'complete'">{{ item.count }} 个 · 本次检索完成</span>
        <span v-else>返回计数 {{ item.count }} · 完整性状态未知</span>
      </div>
      <p class="muted small">数量仅反映本次检索；未知不可视为 0，检索完成不代表供需已评估。</p>
    </section>
    <section>
      <h3>规划建议</h3>
      <p>{{ text(proposal.summary, '本次尚无规划提案。') }}</p>
      <details v-for="(item, i) in dossiers.filter(match)" :key="i" class="report-card" open>
        <summary>{{ text(item.facility_name, name(item.facility_id)) }} · {{ disposition(item.disposition) }}</summary>
        <div v-for="(label, key) in sectionLabels" :key="key" class="report-section">
          <h4>{{ label }}</h4><StructuredData :value="object(item.sections)[key]" />
        </div>
        <div class="references"><button v-for="id in references(item)" :key="id" class="link-button" @click="openFact(id)">证据：{{ id }}</button></div>
      </details>
      <details v-for="(action, i) in rows(proposal.actions).filter(match)" :key="`action-${i}`" class="report-card">
        <summary>行动：{{ text(action.title) }}</summary>
        <StructuredData :value="action" />
        <button v-for="id in references(action)" :key="id" class="link-button" @click="openFact(id)">查看证据 {{ id }}</button>
      </details>
      <details v-for="key in ['facility_dispositions', 'issues', 'quantitative_claims', 'recommendations', 'unsupported_dimensions'].filter(key => rows(proposal[key]).length)" :key="key" class="report-card">
        <summary>{{ ({ facility_dispositions: '类别结论', issues: '规划问题', quantitative_claims: '定量表述与引用', recommendations: '建议', unsupported_dimensions: '未评估维度' } as Record<string, string>)[key] }}</summary>
        <div v-for="(item, i) in rows(proposal[key]).filter(match)" :key="i" class="report-section">
          <StructuredData :value="key === 'facility_dispositions' ? { ...item, status: disposition(item.status) } : item" />
          <button v-for="id in references(item)" :key="id" class="link-button" @click="openFact(id)">查看证据 {{ id }}</button>
        </div>
        <p v-if="!rows(proposal[key]).filter(match).length" class="muted small">当前类别无条目。</p>
      </details>
    </section>
    <section class="review-block">
      <h3>{{ reviewLabel }}</h3>
      <p class="muted small">审查仅针对本次提案，不能据此推断所有类别已取得完整证据。</p>
      <details><summary>审查详情</summary><StructuredData :value="run.review_result" /></details>
    </section>
    <section v-if="selectedFact" ref="factPanel" class="notice" aria-live="polite" tabindex="-1">
      <h3>所引证据</h3><StructuredData :value="selectedFact" /><button class="secondary" @click="selectedFact = null">收起所引证据</button>
    </section>
    <details class="report-card"><summary>完整证据与诊断</summary>
      <details v-for="key in rawKeys" :key="key"><summary>{{ ({ brief: '需求解析', resolved_center: '中心坐标', poi_results: '检索与失败详情', routing: '步行路线', diagnosis: '类别诊断', community_diagnosis: '社区体检', planning_evidence_bundle: '逐类事实证据' } as Record<string, string>)[key] }}</summary><StructuredData :value="run[key]" /></details>
    </details>
  </div>
</template>
