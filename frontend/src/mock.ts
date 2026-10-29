import completed from './fixtures/synthetic-agent-job-state-completed.example.json'
import waiting from './fixtures/synthetic-agent-job-state-waiting.example.json'
import catalog from './fixtures/facility-catalog.example.json'
import { ApiError, parseCatalog, parseJob } from './api.ts'
import type { Client, Job } from './api.ts'

export type Scenario = 'completed' | 'waiting_for_input' | 'partial' | 'failed' | 'empty'
export function demoJob(scenario: Scenario): Job {
  const job = structuredClone(scenario === 'waiting_for_input' ? waiting : completed) as unknown as Record<string, any>
  if (scenario === 'failed') {
    job.status = 'failed'
    job.result = { schema_version: '2.0', run_id: job.run_id, status: 'failed', answer: '【合成示例】分析服务暂不可用，请稍后重试。', errors: [{ code: 'DEMO_FAILURE', message: '用于测试失败状态，无真实请求。' }], limitations: ['合成数据，不是真实分析。'] }
  }
  if (scenario === 'empty') job.result = null
  if (scenario === 'partial') {
    job.result.planning_proposal = null
    job.result.planning_evidence_bundle = null
    job.result.review_result = null
    job.result.answer = '【合成示例】市场检索返回 0 个候选，药店只完成部分检索，小学检索超时，数量未知。没有生成规划提案。'
    job.result.limitations = ['合成测试，不代表真实地点。', '部分检索不是全部数量；超时不等于没有设施。']
    job.result.poi_results = [{ ok: true, schema_version: '2.0', data: { pois: [], counts: [
      { facility_id: 'market', count: 0, status: 'complete' },
      { facility_id: 'pharmacy', count: 2, status: 'partial' },
      { facility_id: 'primary_school', count: null, status: 'unavailable' },
    ], categories_not_queried: [], failed_queries: [{ facility_id: 'primary_school', query_term: '小学', error_code: 'TIMEOUT', retryable: true }] } }]
  }
  return parseJob(job)
}
export function mockClient(scenario: Scenario): Client {
  let polls = 0
  const job = demoJob(scenario)
  return {
    async catalog() { return parseCatalog(catalog) },
    async submit() { polls = 0; return { job_id: job.job_id, run_id: job.run_id, status: 'queued', status_url: '/synthetic-job' } },
    async poll(_handle, signal) {
      if (signal.aborted) throw new ApiError('stopped', '已停止演示。')
      polls++
      return polls === 1 ? { ...job, status: 'queued', result: null } : polls === 2 ? { ...job, status: 'running', result: null } : job
    },
  }
}
