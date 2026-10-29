export type JsonObject = Record<string, unknown>
export type CheckupStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled'
export type CheckupLocation =
  | { type: 'address'; query: string; city?: string }
  | { type: 'coordinate'; lng: number; lat: number; crs: 'BD09LL' }
export interface CheckupGroup { id: string; name_zh: string }
export interface CheckupFacility extends JsonObject { id: string; name_zh: string; group_id: string }
export interface CheckupCatalog {
  version: string
  groups: CheckupGroup[]
  facilities: CheckupFacility[]
  runtime?: { checkup_cancel_supported?: boolean; map_service_configured?: boolean; report_questions_mode?: 'model' | 'report_facts'; questions_mode?: 'model' | 'report_facts' }
}
export interface CheckupHandle { id: string; base: string; status: CheckupStatus; stage: string; progress: number }
export interface CheckupError { code: string; message: string; retryable: boolean }
export interface ProgressGroup extends CheckupGroup, JsonObject {
  status: 'pending' | 'searching' | 'complete' | 'partial'
  score: number | null
  score_provisional: boolean
  completed_categories: number
  total_categories: number
  facility_count: number
}
export interface PartialCheckup {
  report_id: string
  center: JsonObject | null
  location_label: string
  isochrone: JsonObject | null
  categories: CategoryAssessment[]
  groups: ProgressGroup[]
  pois: JsonObject[]
  routes: JsonObject[]
  completed_categories: number
  total_categories: number
  active_facility_id: string | null
}
export interface Checkup extends CheckupHandle {
  result: CheckupReport | null
  error: CheckupError | null
  partial_result?: PartialCheckup | null
  partial_revision?: number
}
export type MapReport = Pick<CheckupReport, 'report_id' | 'center' | 'isochrone' | 'pois' | 'routes' | 'groups' | 'categories' | 'blindspot' | 'quality'>
// Score method v3 ("设施配置参考分"): the score counts distinct facilities found
// in the analysis range. Route accessibility is reported separately and never
// changes this score. The five score_* fields are optional so an older or
// partially populated report still parses unchanged.
export interface CategoryAssessment extends JsonObject {
  facility_id: string
  name_zh?: string
  group_id: string
  search_status?: string
  count?: number | null
  known_count?: number | null
  reachable_count?: number | null
  unresolved_count?: number | null
  nearest_duration_s?: number | null
  nearest_distance_m?: number | null
  access_status?: string
  score?: number | null
  score_provisional?: boolean
  score_profile?: 'A' | 'B' | 'C' | null
  score_basis_count?: number | null
  score_band?: string | null
  next_band?: string | null
  covered_ratio?: number | null
  blind_ratio?: number | null
  unknown_ratio?: number | null
  data_notes?: string
}
export interface GroupAssessment extends JsonObject {
  id: string
  name_zh?: string
  score?: number | null
  score_provisional?: boolean
  assessed_categories?: number | null
  total_categories?: number | null
  summary?: string
}
export interface CheckupReport extends JsonObject {
  report_id: string
  created_at: string
  center: JsonObject
  location_label: string
  catalog_version: string
  isochrone: JsonObject
  search_radius_m: number
  categories: CategoryAssessment[]
  groups: GroupAssessment[]
  selected_facility_ids?: string[]
  score: number | null
  score_provisional?: boolean
  score_method: string
  summary: string
  pois: JsonObject[]
  routes: JsonObject[]
  blindspot: JsonObject
  warnings: JsonObject[]
  quality: JsonObject
}
export interface ExternalSource {
  title: string
  url: string
  published_at?: string | null
  snippet?: string | null
  site_name?: string | null
  authority?: number | null
}
export type QuestionMode = 'auto' | 'web' | 'report'
export type RetrievalStatus = 'not_requested' | 'searched' | 'no_results' | 'failed'
export interface QuestionHistoryTurn { role: 'user' | 'assistant'; content: string }
/** What the user is currently looking at; every non-null id must exist in the report. */
export interface CheckupFocus {
  facility_id?: string | null
  group_id?: string | null
  poi_id?: string | null
  selected_facility_ids?: string[] | null
}
/** Truthful record of what one question actually did on the network, when the server reports it. */
export interface RetrievalInfo {
  requested: boolean
  status: RetrievalStatus
  query?: string
  error?: string
  pages_read?: number
  attempts?: number
  elapsed_ms?: number
}
export type QuestionEvent =
  | { type: 'stage'; stage: string; message: string }
  | { type: 'draft'; text: string }
  | { type: 'draft_reset' }

/** Optional additive fields of the v1 report-question request; omitting them keeps the old body. */
export interface AskOptions {
  /** Requests actual workflow events and provisional answer text through NDJSON. */
  onEvent?: (event: QuestionEvent) => void
  history?: QuestionHistoryTurn[]
  focus?: CheckupFocus | null
  mode?: QuestionMode
}
export interface Answer {
  answer: string
  evidence_ids: string[]
  mode: 'model' | 'report_facts'
  web_search_used?: boolean
  sources?: ExternalSource[]
  retrieval?: RetrievalInfo
  focus_applied?: CheckupFocus
  elapsed_ms?: number
}
export interface CheckupClient {
  catalog(signal: AbortSignal): Promise<CheckupCatalog>
  submit(location: CheckupLocation, signal: AbortSignal, facilityIds?: string[] | null): Promise<CheckupHandle>
  poll(handle: CheckupHandle, signal: AbortSignal): Promise<Checkup>
  pause(handle: CheckupHandle, signal: AbortSignal): Promise<Checkup>
  resume(handle: CheckupHandle, signal: AbortSignal): Promise<Checkup>
  cancel(handle: CheckupHandle, signal: AbortSignal): Promise<Checkup>
  ask(id: string, question: string, signal: AbortSignal, options?: AskOptions): Promise<Answer>
}

const object = (value: unknown): JsonObject => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
const isText = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const invalid = () => new CheckupApiError('contract', '服务响应与新45类体检接口不符，请核对后端版本。')

export class CheckupApiError extends Error {
  code: string
  retryable: boolean
  constructor(code: string, message: string, retryable = false) { super(message); this.code = code; this.retryable = retryable }
}

export function checkupBase(raw: string): URL {
  let url: URL
  try { url = new URL(raw) } catch { throw new CheckupApiError('config', '请输入有效的后端 HTTP 地址。') }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new CheckupApiError('config', '请输入不含账号、密钥和查询参数的后端 HTTP 地址。')
  }
  return url
}

export function parseCheckupCatalog(value: unknown): CheckupCatalog {
  const v = object(value)
  if (!isText(v.version) || !Array.isArray(v.groups) || !Array.isArray(v.facilities)) throw invalid()
  const groups = v.groups.map(raw => {
    const group = object(raw)
    if (!isText(group.id) || !isText(group.name_zh)) throw invalid()
    return { id: group.id, name_zh: group.name_zh }
  })
  const facilities = v.facilities.map(raw => {
    const facility = object(raw)
    if (!isText(facility.id) || !isText(facility.name_zh) || !isText(facility.group_id)) throw invalid()
    return facility as CheckupFacility
  })
  const groupIds = new Set(groups.map(group => group.id))
  if (groups.length !== 7 || facilities.length !== 45
      || groupIds.size !== 7 || new Set(facilities.map(item => item.id)).size !== 45
      || facilities.some(item => !groupIds.has(item.group_id))) {
    throw new CheckupApiError('catalog', `目录核对未通过：服务返回 ${facilities.length} 类、${groups.length} 组，必须是45类、7组且类别归属完整。`)
  }
  const runtimeValue = object(v.runtime)
  const runtime: CheckupCatalog['runtime'] = {}
  if (typeof runtimeValue.checkup_cancel_supported === 'boolean') runtime.checkup_cancel_supported = runtimeValue.checkup_cancel_supported
  if (typeof runtimeValue.map_service_configured === 'boolean') runtime.map_service_configured = runtimeValue.map_service_configured
  if (runtimeValue.report_questions_mode === 'model' || runtimeValue.report_questions_mode === 'report_facts') {
    runtime.report_questions_mode = runtimeValue.report_questions_mode
  }
  if (runtimeValue.questions_mode === 'model' || runtimeValue.questions_mode === 'report_facts') runtime.questions_mode = runtimeValue.questions_mode
  return { version: v.version, groups, facilities, ...(Object.keys(runtime).length ? { runtime } : {}) }
}

function parseReport(value: unknown): CheckupReport {
  const report = object(value)
  if (!isText(report.report_id) || !isText(report.created_at) || !isText(report.location_label)
      || !isText(report.catalog_version) || !isText(report.score_method) || !isText(report.summary)
      || !Array.isArray(report.categories) || report.categories.length < 1 || report.categories.length > 45
      || !Array.isArray(report.groups) || report.groups.length < 1 || report.groups.length > 7) throw invalid()
  const center = object(report.center)
  if (typeof center.lng !== 'number' || !Number.isFinite(center.lng) || Math.abs(center.lng) > 180
      || typeof center.lat !== 'number' || !Number.isFinite(center.lat) || Math.abs(center.lat) > 90
      || center.crs !== 'BD09LL') throw invalid()
  const validScore = (score: unknown) => score === null || typeof score === 'number' && Number.isFinite(score)
    && score >= 0 && score <= 100 && Math.abs(score / 2.5 - Math.round(score / 2.5)) < 1e-8
  const groups = report.groups.map(object)
  const categories = report.categories.map(object)
  const groupIds = new Set(groups.map(group => group.id))
  const categoryIds = categories.map(category => category.facility_id)
  if (!validScore(report.score) || groups.some(group => !isText(group.id) || !validScore(group.score))
      || groupIds.size !== groups.length
      || new Set(categoryIds).size !== categoryIds.length || categoryIds.some(id => !isText(id))
      || categories.some(category => !isText(category.group_id) || !groupIds.has(category.group_id))
      || [...groupIds].some(id => !categories.some(category => category.group_id === id))) throw invalid()
  // New reports may cover only the selected subset (1-45 categories in 1-7 groups).
  // selected_facility_ids is absent from older full-catalog reports and is accepted as empty there.
  const selected = report.selected_facility_ids
  if (selected != null) {
    if (!Array.isArray(selected) || selected.some(item => !isText(item))
        || new Set(selected).size !== selected.length) throw invalid()
    if (selected.length && (selected.length !== categories.length
        || categories.some(category => !selected.includes(category.facility_id)))) throw invalid()
  }
  if (!Array.isArray(report.pois) || !Array.isArray(report.routes) || !Array.isArray(report.warnings)) throw invalid()
  return report as CheckupReport
}

const statuses: CheckupStatus[] = ['queued', 'running', 'paused', 'completed', 'failed', 'cancelled']
function parsePartial(value: unknown, id: string): PartialCheckup | null {
  if (value == null) return null
  const p = object(value)
  const count = (n: unknown) => typeof n === 'number' && Number.isInteger(n) && n >= 0
  if (p.report_id !== id || !Array.isArray(p.groups) || p.groups.length > 7
      || !Array.isArray(p.categories) || p.categories.length > 45 || !Array.isArray(p.pois) || !Array.isArray(p.routes)
      || !count(p.completed_categories) || !count(p.total_categories)
      || (p.completed_categories as number) > (p.total_categories as number) || (p.total_categories as number) > 45) throw invalid()
  const groups = p.groups.map(object), categories = p.categories.map(object)
  const ids = new Set(groups.map(g => g.id))
  if (ids.size !== groups.length || new Set(categories.map(c => c.facility_id)).size !== categories.length
      || groups.some(g => !isText(g.id) || !isText(g.name_zh) || !['pending', 'searching', 'complete', 'partial'].includes(String(g.status))
        || !count(g.completed_categories) || !count(g.total_categories) || !count(g.facility_count)
        || (g.completed_categories as number) > (g.total_categories as number)
        || !(g.score === null || typeof g.score === 'number' && Number.isFinite(g.score) && g.score >= 0 && g.score <= 100))
      || categories.some(c => !isText(c.facility_id) || !ids.has(c.group_id))) throw invalid()
  if (p.center !== null) {
    const center = object(p.center)
    if (typeof center.lng !== 'number' || !Number.isFinite(center.lng) || Math.abs(center.lng) > 180
        || typeof center.lat !== 'number' || !Number.isFinite(center.lat) || Math.abs(center.lat) > 90 || center.crs !== 'BD09LL') throw invalid()
  }
  return p as unknown as PartialCheckup
}
export function parseCheckup(value: unknown, expectedId?: string): Checkup {
  const v = object(value)
  if (!isText(v.id) || expectedId && v.id !== expectedId || !statuses.includes(v.status as CheckupStatus)
      || !isText(v.stage) || typeof v.progress !== 'number' || !Number.isFinite(v.progress) || v.progress < 0 || v.progress > 100) throw invalid()
  let result: CheckupReport | null = null
  if (v.result != null) result = parseReport(v.result)
  let error: CheckupError | null = null
  if (v.error != null) {
    const problem = object(v.error)
    if (!isText(problem.code) || !isText(problem.message)) throw invalid()
    error = { code: problem.code, message: problem.message, retryable: problem.retryable === true }
  }
  if (v.status === 'completed' ? !result || result.report_id !== v.id || error !== null || v.progress !== 100 || v.stage !== 'completed'
      : v.status === 'failed' ? result !== null || error === null || v.stage !== 'failed'
        : v.status === 'cancelled' ? result !== null || error !== null || v.stage !== 'cancelled'
        : result !== null || error !== null) throw invalid()
  const partial = parsePartial(v.partial_result, v.id)
  if (v.partial_revision != null && (typeof v.partial_revision !== 'number' || !Number.isInteger(v.partial_revision) || v.partial_revision < 0)) throw invalid()
  return { id: v.id, status: v.status as CheckupStatus, stage: v.stage, progress: v.progress, result, error, base: '',
    ...(v.partial_revision != null ? { partial_revision: v.partial_revision as number, partial_result: partial } : {}) }
}

const QUESTION_MODES: QuestionMode[] = ['auto', 'web', 'report']
const RETRIEVAL_STATUSES: RetrievalStatus[] = ['not_requested', 'searched', 'no_results', 'failed']

/** Request-side history: bounded in count, per-message size and total size before a POST. */
function normalizeHistory(value: unknown): QuestionHistoryTurn[] | undefined {
  if (value == null) return undefined
  if (!Array.isArray(value) || value.length > 12) throw new CheckupApiError('input', '对话历史格式无效（最多12条）。')
  const turns = value.map(raw => {
    const turn = object(raw)
    if ((turn.role !== 'user' && turn.role !== 'assistant') || !isText(turn.content) || [...turn.content].length > 4000) {
      throw new CheckupApiError('input', '对话历史格式无效（每条含 role 与1–4000字 content）。')
    }
    return { role: turn.role as QuestionHistoryTurn['role'], content: turn.content }
  })
  if (turns.reduce((total, turn) => total + [...turn.content].length, 0) > 12000) {
    throw new CheckupApiError('input', '对话历史过长，请只保留最近几轮。')
  }
  return turns
}

/** Request-side focus. Unknown/empty focus is omitted instead of being sent as noise. */
function normalizeFocus(value: unknown): CheckupFocus | undefined {
  if (value == null) return undefined
  if (typeof value !== 'object' || Array.isArray(value)) throw new CheckupApiError('input', '关注范围格式无效。')
  const record = value as JsonObject
  const focus: CheckupFocus = {}
  for (const key of ['facility_id', 'group_id', 'poi_id'] as const) {
    const item = record[key]
    if (item === undefined) continue
    if (item !== null && !isText(item)) throw new CheckupApiError('input', `关注范围 ${key} 无效。`)
    focus[key] = item === null ? null : item
  }
  const selected = record.selected_facility_ids
  if (selected !== undefined) {
    if (selected === null) focus.selected_facility_ids = null
    else {
      if (!Array.isArray(selected) || selected.length > 45 || selected.some(item => !isText(item))
          || new Set(selected).size !== selected.length) throw new CheckupApiError('input', '关注类别编号无效。')
      focus.selected_facility_ids = selected.map(item => item as string)
    }
  }
  return Object.keys(focus).length ? focus : undefined
}

function normalizeMode(value: unknown): QuestionMode | undefined {
  if (value == null) return undefined
  if (typeof value !== 'string' || !QUESTION_MODES.includes(value as QuestionMode)) {
    throw new CheckupApiError('input', '联网方式无效，只能是 auto、web 或 report。')
  }
  return value as QuestionMode
}

/**
 * Response-side new fields are lenient on purpose: an older backend does not return them and a
 * partially populated retrieval record must not turn a usable answer into a contract failure.
 */
function parseRetrieval(value: unknown): RetrievalInfo | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined
  const record = value as JsonObject
  if (!RETRIEVAL_STATUSES.includes(record.status as RetrievalStatus)) return undefined
  const requested = typeof record.requested === 'boolean' ? record.requested : record.status !== 'not_requested'
  const info: RetrievalInfo = { requested, status: requested ? record.status as RetrievalStatus : 'not_requested' }
  if (isText(record.query)) info.query = record.query
  if (isText(record.error)) info.error = record.error
  for (const key of ['pages_read', 'attempts', 'elapsed_ms'] as const) {
    const count = record[key]
    if (typeof count === 'number' && Number.isFinite(count) && count >= 0) info[key] = Math.floor(count)
  }
  return info
}

function parseFocusApplied(value: unknown): CheckupFocus | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined
  const record = value as JsonObject
  const focus: CheckupFocus = {}
  for (const key of ['facility_id', 'group_id', 'poi_id'] as const) {
    const item = record[key]
    if (item === null || isText(item)) focus[key] = item === null ? null : item
  }
  const selected = record.selected_facility_ids
  if (selected === null) focus.selected_facility_ids = null
  else if (Array.isArray(selected) && selected.every(item => isText(item))) {
    focus.selected_facility_ids = selected.map(item => item as string)
  }
  return Object.keys(focus).length ? focus : undefined
}

function parseSources(value: unknown): ExternalSource[] | undefined {
  if (!Array.isArray(value)) return undefined
  const sources: ExternalSource[] = []
  for (const raw of value) {
    const item = object(raw)
    if (!isText(item.title) || !isText(item.url)) continue
    const source: ExternalSource = { title: item.title, url: item.url }
    if (typeof item.published_at === 'string') source.published_at = item.published_at
    if (typeof item.snippet === 'string') source.snippet = item.snippet
    if (typeof item.site_name === 'string') source.site_name = item.site_name
    if (typeof item.authority === 'number' && Number.isFinite(item.authority)) source.authority = item.authority
    sources.push(source)
  }
  return sources
}

const nextRequestAt = new Map<string, number>()
async function waitForRequestSlot(origin: string, signal: AbortSignal, intervalMs: number) {
  const now = Date.now()
  const slot = Math.max(now, nextRequestAt.get(origin) ?? now)
  nextRequestAt.set(origin, slot + intervalMs)
  const delayMs = slot - now
  if (!delayMs) return
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, delayMs)
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new CheckupApiError('stopped', '已停止请求。')) }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  })
}

async function readQuestionStream(response: Response, signal: AbortSignal, onEvent: NonNullable<AskOptions['onEvent']>): Promise<unknown> {
  if (!response.body || !response.headers.get('Content-Type')?.includes('application/x-ndjson')) throw invalid()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const abort = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) abort()
  function consume(line: string): { result: unknown } | undefined {
    if (!line.trim()) return
    let event: JsonObject
    try { event = object(JSON.parse(line)) } catch { throw invalid() }
    if (event.type === 'result') return { result: event.result }
    if (event.type === 'error') throw new CheckupApiError('question', '报告问答暂不可用，可在同一份报告上重试。', true)
    if (event.type === 'stage' && isText(event.stage) && isText(event.message)) {
      onEvent({ type: 'stage', stage: event.stage, message: event.message })
    } else if (event.type === 'draft' && typeof event.text === 'string') {
      onEvent({ type: 'draft', text: event.text })
    } else if (event.type === 'draft_reset') onEvent({ type: 'draft_reset' })
    else throw invalid()
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (signal.aborted) throw new Error('Question stream aborted')
      buffer += decoder.decode(value, { stream: !done })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      if (done && buffer.trim()) lines.push(buffer)
      for (const line of lines) {
        const completed = consume(line)
        if (completed) return completed.result
      }
      if (done) throw new CheckupApiError('question', '报告问答连接中断，可在同一份报告上重试。', true)
    }
  } finally {
    signal.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export function checkupClient(rawBase: string, fetcher: typeof fetch = fetch, timeoutMs = 15000, minRequestIntervalMs = 1050): CheckupClient {
  const base = checkupBase(rawBase)
  const processSnapshots = new Map<string, { revision: number; snapshot: PartialCheckup }>()
  async function request(path: string, signal: AbortSignal, body?: unknown, postLabel = '请求', requestTimeoutMs = timeoutMs, onEvent?: AskOptions['onEvent']) {
    let sent = false
    try {
      await waitForRequestSlot(base.origin, signal, minRequestIntervalMs)
      if (signal.aborted) throw new CheckupApiError('stopped', '已停止请求。')
    } catch (error) { throw error instanceof CheckupApiError ? error : new CheckupApiError('stopped', '已停止请求。') }
    const controller = new AbortController()
    const abort = () => controller.abort()
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) controller.abort()
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, requestTimeoutMs)
    try {
      sent = true
      const response = await fetcher(new URL(path, base), {
        method: body === undefined ? 'GET' : 'POST', signal: controller.signal,
        cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: body === undefined ? { Accept: 'application/json' } : { Accept: onEvent ? 'application/x-ndjson' : 'application/json', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!response.ok) {
        // This specific backend error is raised before a job is created. Other 5xx POSTs remain ambiguous.
        if (path === '/api/v1/checkups' && body !== undefined && response.status === 503) {
          const failure = object(await response.clone().json().catch(() => null))
          if (object(failure.detail).code === 'MISSING_SERVER_AK') {
            throw new CheckupApiError('MISSING_SERVER_AK', '地图分析服务尚未配置，本次没有创建体检任务；配置后请重新核对服务。')
          }
        }
        if (body !== undefined && response.status >= 500) {
          if (/\/questions(?:\/stream)?$/.test(path)) throw new CheckupApiError('question', '报告问答暂不可用，可在同一份报告上重试问题；无需重新体检。', true)
          throw new CheckupApiError('uncertain', `${postLabel}结果未知（HTTP ${response.status}），服务可能已接收；请先查询，不要自动重发。`, true)
        }
        const messages: Record<number, string> = {
          404: path.startsWith('/api/v1/checkups/') ? '任务不存在或已过期，请确认服务状态。' : '服务未提供此接口，请核对后端地址和版本。',
          422: '请求内容不符合接口要求，请检查地址或问题。',
          429: '服务队列繁忙，请稍后再试。',
        }
        throw new CheckupApiError(String(response.status), messages[response.status] ?? `服务请求失败（HTTP ${response.status}）。`, response.status >= 500)
      }
      if (body !== undefined && response.status !== 202 && path === '/api/v1/checkups') {
        throw new CheckupApiError('uncertain', '体检提交响应不是约定的202；接收结果未知，请勿自动重发。', true)
      }
      return onEvent ? await readQuestionStream(response, controller.signal, onEvent) : await response.json()
    } catch (error) {
      if (error instanceof CheckupApiError) throw error
      if (signal.aborted && /\/questions(?:\/stream)?$/.test(path)) throw new CheckupApiError('stopped', '已停止请求。')
      if (body !== undefined && sent) {
        if (/\/questions(?:\/stream)?$/.test(path)) throw new CheckupApiError('question', '报告问答连接中断或超时，可在同一份报告上重试问题；无需重新体检。', true)
        const reason = signal.aborted ? `${postLabel}请求已停止` : timedOut ? `${postLabel}超时` : `${postLabel}连接中断`
        throw new CheckupApiError('uncertain', `${reason}，结果未知；服务可能已接收，请勿自动重发。`, true)
      }
      if (signal.aborted) throw new CheckupApiError('stopped', '已停止查询；已接收的服务端任务仍可继续查询。', true)
      if (timedOut) throw new CheckupApiError('timeout', '查询超时，可继续查询原任务。', true)
      throw new CheckupApiError('network', '连接失败，请检查服务地址、网络和跨域配置；可继续查询原任务。', true)
    } finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
  }
  const checkupPath = (id: string) => `/api/v1/checkups/${encodeURIComponent(id)}`
  function acceptJob(value: unknown, handle: CheckupHandle): Checkup {
    const checkup = parseCheckup(value, handle.id)
    const cached = processSnapshots.get(handle.id)
    if (checkup.partial_result) {
      processSnapshots.set(handle.id, { revision: checkup.partial_revision ?? 0, snapshot: checkup.partial_result })
    } else if (cached && cached.revision === checkup.partial_revision && ['queued', 'running', 'paused', 'cancelled'].includes(checkup.status)) {
      checkup.partial_result = cached.snapshot
    }
    if (checkup.status === 'completed') processSnapshots.delete(handle.id)
    return { ...checkup, base: handle.base }
  }
  return {
    async catalog(signal) {
      const spec = object(await request('/openapi.json', signal))
      const paths = object(spec.paths)
      const statusPath = Object.keys(paths).find(path => /^\/api\/v1\/checkups\/\{[^/]+\}$/.test(path))
      const questionPath = Object.keys(paths).find(path => /^\/api\/v1\/checkups\/\{[^/]+\}\/questions$/.test(path))
      if (!object(paths['/api/v1/checkup-catalog']).get || !object(paths['/api/v1/checkups']).post
          || !statusPath || !object(paths[statusPath]).get || !questionPath || !object(paths[questionPath]).post) {
        throw new CheckupApiError('openapi', '服务未同时提供新体检目录、提交、任务状态和报告问答接口。')
      }
      return parseCheckupCatalog(await request('/api/v1/checkup-catalog', signal))
    },
    async submit(location, signal, facilityIds) {
      if (location.type === 'address' && !location.query.trim()) throw new CheckupApiError('input', '请输入体检地址。')
      if (location.type === 'coordinate' && (!Number.isFinite(location.lng) || !Number.isFinite(location.lat)
          || Math.abs(location.lng) > 180 || Math.abs(location.lat) > 90 || location.crs !== 'BD09LL')) throw new CheckupApiError('input', '请输入有效的 BD09LL 经纬度。')
      const body: JsonObject = { location }
      // Omitted/null keeps the original full-catalog request compatible. A provided
      // selection must be non-empty because the backend rejects an empty list.
      if (facilityIds != null) {
        if (!Array.isArray(facilityIds) || facilityIds.length === 0) throw new CheckupApiError('input', '至少选择一个设施类别。')
        if (facilityIds.some(item => !isText(item))) throw new CheckupApiError('input', '设施类别编号无效。')
        if (new Set(facilityIds).size !== facilityIds.length) throw new CheckupApiError('input', '设施类别不能重复选择。')
        body.facility_ids = [...facilityIds]
      }
      const value = object(await request('/api/v1/checkups', signal, body, '体检提交'))
      if (!isText(value.id) || value.status !== 'queued' || value.stage !== 'queued' || value.progress !== 0) {
        throw new CheckupApiError('uncertain', '服务可能已接收任务，但返回字段不是约定的排队状态；请勿自动重发。', true)
      }
      return { id: value.id, base: base.href.replace(/\/$/, ''), status: 'queued', stage: 'queued', progress: 0 }
    },
    async poll(handle, signal) {
      const cached = processSnapshots.get(handle.id)
      const path = checkupPath(handle.id) + (cached ? `?after_revision=${cached.revision}` : '')
      return acceptJob(await request(path, signal), handle)
    },
    async pause(handle, signal) {
      return acceptJob(await request(`${checkupPath(handle.id)}/pause`, signal, {}, '暂停检索'), handle)
    },
    async resume(handle, signal) {
      return acceptJob(await request(`${checkupPath(handle.id)}/resume`, signal, {}, '继续检索'), handle)
    },
    async cancel(handle, signal) {
      return acceptJob(await request(`${checkupPath(handle.id)}/cancel`, signal, {}, '终止检索'), handle)
    },
    async ask(id, question, signal, options) {
      if (!question.trim() || [...question].length > 2000) throw new CheckupApiError('input', '请输入1–2000字的问题。')
      // Only the legacy `question` field is mandatory: omitting every new option keeps the
      // old three-argument call's body byte-for-byte compatible with an older backend.
      const body: JsonObject = { question: question.trim() }
      const history = normalizeHistory(options?.history)
      if (history?.length) body.history = history
      const focus = normalizeFocus(options?.focus)
      if (focus) body.focus = focus
      const mode = normalizeMode(options?.mode)
      if (mode) body.mode = mode
      const path = `${checkupPath(id)}/questions${options?.onEvent ? '/stream' : ''}`
      const value = object(await request(path, signal, body, '报告问答', Math.max(timeoutMs, 60_000), options?.onEvent))
      if (!isText(value.answer) || !Array.isArray(value.evidence_ids) || value.evidence_ids.some(item => typeof item !== 'string')
          || !['model', 'report_facts'].includes(String(value.mode))) throw invalid()
      const answer: Answer = { answer: value.answer, evidence_ids: value.evidence_ids as string[], mode: value.mode as Answer['mode'] }
      if (typeof value.web_search_used === 'boolean') answer.web_search_used = value.web_search_used
      const sources = parseSources(value.sources)
      if (sources !== undefined) answer.sources = sources
      const retrieval = parseRetrieval(value.retrieval)
      if (retrieval) answer.retrieval = retrieval
      const focusApplied = parseFocusApplied(value.focus_applied)
      if (focusApplied) answer.focus_applied = focusApplied
      if (typeof value.elapsed_ms === 'number' && Number.isFinite(value.elapsed_ms) && value.elapsed_ms >= 0) {
        answer.elapsed_ms = Math.floor(value.elapsed_ms)
      }
      return answer
    },
  }
}

export function savePending(value: CheckupHandle | { base: string; uncertain: true } | null, storage: Storage = localStorage) {
  try {
    if (value) storage.setItem('15min-life.checkup.pending', JSON.stringify(value))
    else storage.removeItem('15min-life.checkup.pending')
  } catch { /* Private browsing or full storage must not block a checkup. */ }
}

export function loadPending(storage: Storage = localStorage): CheckupHandle | { base: string; uncertain: true } | null {
  try {
    const value = JSON.parse(storage.getItem('15min-life.checkup.pending') ?? 'null') as unknown
    if (!value || typeof value !== 'object') return null
    const record = value as Record<string, unknown>
    if (typeof record.base !== 'string') return null
    if (record.uncertain === true) return { base: record.base, uncertain: true }
    if (isText(record.id) && statuses.includes(record.status as CheckupStatus) && isText(record.stage)
        && typeof record.progress === 'number' && Number.isFinite(record.progress)) {
      return { id: record.id, base: record.base, status: record.status as CheckupStatus, stage: record.stage, progress: record.progress }
    }
  } catch { /* Ignore unreadable stale state. */ }
  return null
}

export async function watchCheckup(client: CheckupClient, handle: CheckupHandle, signal: AbortSignal,
  update: (checkup: Checkup) => void, wait = (ms: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, ms)
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new CheckupApiError('stopped', '已停止查询；可继续查询原任务。', true)) }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  }), now = Date.now): Promise<Checkup> {
  const started = now()
  while (!signal.aborted) {
    const checkup = await client.poll(handle, signal)
    if (signal.aborted) throw new CheckupApiError('stopped', '已停止查询；可继续查询原任务。', true)
    update(checkup)
    if (checkup.status === 'completed' || checkup.status === 'failed' || checkup.status === 'paused' || checkup.status === 'cancelled') return checkup
    if (now() - started >= 12 * 60 * 1000) throw new CheckupApiError('paused', '已暂停自动查询，服务端任务仍在运行；可继续查询原任务。', true)
    await wait(now() - started > 60000 ? 5000 : 1500, signal)
  }
  throw new CheckupApiError('stopped', '已停止查询；可继续查询原任务。', true)
}
