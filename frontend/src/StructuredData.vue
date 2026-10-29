<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ value: unknown; depth?: number }>()
const level = computed(() => props.depth ?? 0)
const entries = computed(() => props.value !== null && typeof props.value === 'object' ? Object.entries(props.value) : [])
const labels: Record<string, string> = {
  facility_id: '设施标识', facility_name: '设施名称', statement: '事实说明', scope: '适用范围', basis: '依据',
  source: '来源', method: '方法', uncertainty: '不确定性', applicability: '适用条件', observed_at: '观测时间',
  limitations: '限制', fact_ids: '引用事实', evidence_refs: '证据引用', fact_id: '事实编号',
  status: '状态', count: '数量', poi_count: '检索候选数', reachable_count: '可达数量',
  covered_ratio: '覆盖比例', blind_ratio: '盲区比例', unknown_ratio: '未知比例',
  value: '值', unit: '单位', metric: '指标', issues: '问题', errors: '错误',
  preconditions: '前提', verification: '核验要求', implementation_steps: '实施步骤',
  explanation: '说明', title: '标题', rationale: '原因', objective: '目标', area_or_object: '对象或范围',
  revision_instructions: '修改要求', unsupported_dimensions: '未支持的维度',
}
</script>

<template>
  <span v-if="value === null || value === undefined" class="muted">未知 / 未提供</span>
  <span v-else-if="typeof value === 'boolean'">{{ value ? '是' : '否' }}</span>
  <span v-else-if="typeof value !== 'object'" class="preserve">{{ value === '' ? '未提供' : value }}</span>
  <span v-else-if="entries.length === 0" class="muted">{{ Array.isArray(value) ? '0 项 / 无条目' : '空对象' }}</span>
  <pre v-else-if="level > 7">{{ JSON.stringify(value, null, 2) }}</pre>
  <dl v-else class="structured">
    <div v-for="[key, child] in entries" :key="key">
      <dt>{{ Array.isArray(value) ? Number(key) + 1 : labels[key] ?? key }}</dt>
      <dd>
        <details v-if="child !== null && typeof child === 'object'">
          <summary>展开{{ Array.isArray(child) ? `（${child.length} 项）` : '' }}</summary>
          <StructuredData :value="child" :depth="level + 1" />
        </details>
        <StructuredData v-else :value="child" :depth="level + 1" />
      </dd>
    </div>
  </dl>
</template>
