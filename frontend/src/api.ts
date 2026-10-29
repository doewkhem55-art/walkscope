// Display-facing subset of the October 6 handoff, pending runtime OpenAPI verification.
// Optional nested data stays opaque so additive backend fields remain inspectable.
export type JsonObject = Record<string, unknown>
export type JobStatus = 'queued' | 'running' | 'completed' | 'waiting_for_input' | 'failed'
export type RunStatus = 'completed' | 'waiting_for_input' | 'failed'
export interface Run extends JsonObject { run_id: string; status: RunStatus }
export interface Job { schema_version: '2.0'; job_id: string; run_id: string; status: JobStatus; result: Run | null }
export interface Handle { job_id: string; run_id: string; status: JobStatus; status_url: string }
export interface Facility extends JsonObject { id: string; name_zh: string; group_id: string }
export interface Catalog { catalog_version: string; facilities: Facility[]; groups: { id: string; name_zh: string }[] }
export interface Client {
  catalog(signal: AbortSignal): Promise<Catalog>
  submit(message: string, signal: AbortSignal): Promise<Handle>
  poll(handle: Handle, signal: AbortSignal): Promise<Job>
}
export const object = (v: unknown): JsonObject => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as JsonObject : {}
export const rows = (v: unknown): JsonObject[] => Array.isArray(v) ? v.filter(x => x !== null && typeof x === 'object' && !Array.isArray(x)) as JsonObject[] : []
export const strings = (v: unknown): string[] => Array.isArray(v) ? v.filter(x => typeof x === 'string') : []
export const text = (v: unknown, fallback = '未提供'): string => typeof v === 'string' && v.trim() ? v : fallback
export const terminal = (status: JobStatus): boolean => ['completed', 'waiting_for_input', 'failed'].includes(status)

export class ApiError extends Error {
  code: string
  constructor(code: string, message: string) { super(message); this.code = code }
}
const invalid = () => new ApiError('contract', '服务响应与预期接口不符，请联系后端核对当前版本。')
const status = (v: unknown): JobStatus => {
  if (typeof v !== 'string' || !['queued', 'running', 'completed', 'waiting_for_input', 'failed'].includes(v)) throw invalid()
  return v as JobStatus
}
export function parseJob(value: unknown, handle?: Handle): Job {
  const v = object(value)
  if (v.schema_version !== '2.0' || typeof v.job_id !== 'string' || typeof v.run_id !== 'string') throw invalid()
  if (handle && (v.job_id !== handle.job_id || v.run_id !== handle.run_id)) throw invalid()
  const state = status(v.status)
  let result: Run | null = null
  if (v.result != null) {
    const run = object(v.result)
    if (run.schema_version !== '2.0' || run.run_id !== v.run_id || !terminal(status(run.status))) throw invalid()
    if (terminal(state) && run.status !== state) throw invalid()
    result = run as Run
  }
  // A terminal null result is displayed explicitly, never turned into a successful report.
  return { schema_version: '2.0', job_id: v.job_id, run_id: v.run_id, status: state, result }
}
export function parseCatalog(value: unknown): Catalog {
  const v = object(value)
  if (v.schema_version !== '2.0' || typeof v.catalog_version !== 'string' || !Array.isArray(v.facilities) || !Array.isArray(v.groups)) throw invalid()
  for (const f of v.facilities) {
    const item = object(f)
    if (![item.id, item.name_zh, item.group_id].every(x => typeof x === 'string' && x.length)) throw invalid()
  }
  for (const g of v.groups) if (![object(g).id, object(g).name_zh].every(x => typeof x === 'string' && x.length)) throw invalid()
  return v as unknown as Catalog
}
export function apiBase(raw: string): URL {
  const url = new URL(raw)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new ApiError('config', '请输入不含密钥和查询参数的后端 HTTP 地址。')
  return url
}
export function statusUrl(base: URL, path: string): string {
  const url = new URL(path, base)
  if (url.origin !== base.origin || !url.pathname.startsWith('/api/v2/agent/jobs/') || url.username || url.password || url.search || url.hash) throw invalid()
  return url.href
}
export function realClient(rawBase: string, fetcher: typeof fetch = fetch, timeoutMs = 15000): Client {
  const base = apiBase(rawBase)
  async function request(path: string, signal: AbortSignal, body?: unknown): Promise<unknown> {
    const controller = new AbortController()
    const abort = () => controller.abort()
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) controller.abort()
    const timer = setTimeout(abort, timeoutMs)
    try {
      const res = await fetcher(new URL(path, base), {
        method: body === undefined ? 'GET' : 'POST', signal: controller.signal,
        cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!res.ok) {
        const messages: Record<number, string> = {
          422: '请求不符合接口要求，请检查问题内容。',
          429: '任务队列已满，请稍后手动重试。',
          404: '任务不存在或已过期，也可能是后端已重启，请重新提交。',
        }
        if (body !== undefined && res.status >= 500) throw new ApiError('uncertain', '提交结果未知，重试可能创建重复任务。请确认后再手动重试。')
        throw new ApiError(String(res.status), messages[res.status] ?? `服务暂不可用（HTTP ${res.status}），请稍后重试。`)
      }
      if (body !== undefined && res.status !== 202) throw new ApiError('uncertain', '提交响应不是约定的 202，接收结果未知。请联系后端；重试可能创建重复任务。')
      return await res.json()
    } catch (error) {
      if (error instanceof ApiError) throw error
      if (body !== undefined) throw new ApiError('uncertain', '提交结果未知，重试可能创建重复任务。请确认后再手动重试。')
      if (signal.aborted) throw new ApiError('stopped', '已停止查询；后端任务可能仍在运行。')
      throw new ApiError('network', '连接失败或请求超时，请检查后端地址、网络和跨域配置。可以只重试状态查询。')
    } finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
  }
  return {
    async catalog(signal) {
      const spec = object(await request('/openapi.json', signal))
      const paths = object(spec.paths)
      if (!object(paths['/api/v2/facilities']).get || !object(paths['/api/v2/agent/jobs']).post || !object(paths['/api/v2/agent/jobs/{job_id}']).get) throw invalid()
      return parseCatalog(await request('/api/v2/facilities', signal))
    },
    async submit(message, signal) {
      if (!message.trim() || [...message].length > 4000) throw new ApiError('input', '请输入 1–4000 字的问题。')
      const value = await request('/api/v2/agent/jobs', signal, { schema_version: '2.0', user_message: message })
      try {
        const v = object(value)
        if (v.schema_version !== '2.0' || typeof v.job_id !== 'string' || typeof v.run_id !== 'string' || typeof v.status_url !== 'string') throw invalid()
        return { job_id: v.job_id, run_id: v.run_id, status: status(v.status), status_url: statusUrl(base, v.status_url) }
      } catch { throw new ApiError('uncertain', '任务可能已接收，但返回的查询地址或字段无效。请联系后端；自动重发可能创建重复任务。') }
    },
    async poll(handle, signal) { return parseJob(await request(statusUrl(base, handle.status_url), signal), handle) },
  }
}
export function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = () => { signal.removeEventListener('abort', abort); resolve() }
    const timer = setTimeout(finish, ms)
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new ApiError('stopped', '已停止查询；后端任务可能仍在运行。')) }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  })
}
export async function watchJob(client: Client, handle: Handle, signal: AbortSignal, update: (job: Job) => void,
  wait = delay, now = Date.now): Promise<Job> {
  const start = now()
  while (!signal.aborted) {
    const job = await client.poll(handle, signal)
    if (signal.aborted) throw new ApiError('stopped', '已停止查询。')
    update(job)
    if (terminal(job.status)) return job
    if (now() - start >= 12 * 60 * 1000) throw new ApiError('paused', '等待时间较长，已暂停自动查询。可继续查询原任务。')
    await wait(now() - start > 60000 ? 5000 : 1500, signal)
  }
  throw new ApiError('stopped', '已停止查询。')
}
