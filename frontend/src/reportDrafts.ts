import { checkupBase, CheckupApiError } from './checkups.ts'

export interface DraftChapter {
  id: string; title: string; paragraphs: string[]; table_headers: string[]; table_rows: string[][]
}
export interface ReportDraft {
  report_id: string; source_hash: string; version: number; title: string
  mode: 'model' | 'report_facts'; notice: string; updated_at: string
  facts: Record<string, unknown>; chapters: DraftChapter[]
}
export type DraftEvent = { type: 'stage'; stage: string; message: string } | { type: 'chapter'; chapter: DraftChapter }
export interface DraftEdit { action: 'generate' | 'revise'; instruction?: string; context?: string; expected_version: number; session_id?: string }
export interface ReportDraftClient {
  get(id: string, signal: AbortSignal): Promise<ReportDraft | null>
  edit(id: string, body: DraftEdit, signal: AbortSignal, onEvent: (event: DraftEvent) => void): Promise<ReportDraft>
  export(id: string, version: number, signal: AbortSignal): Promise<Blob>
}
const record = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string')
function parseChapter(value: unknown): DraftChapter {
  const v = record(value)
  if (typeof v.id !== 'string' || typeof v.title !== 'string' || !strings(v.paragraphs) || !strings(v.table_headers)
    || !Array.isArray(v.table_rows) || !v.table_rows.every(strings)) throw new Error('报告章节格式无效，未替换当前草稿。')
  return v as unknown as DraftChapter
}
function parseDraft(value: unknown, id: string): ReportDraft {
  const v = record(value)
  if (v.report_id !== id || typeof v.source_hash !== 'string' || !/^[a-f0-9]{64}$/.test(v.source_hash)
    || !Number.isInteger(v.version) || Number(v.version) < 1 || typeof v.title !== 'string'
    || !['model', 'report_facts'].includes(String(v.mode)) || typeof v.notice !== 'string' || !Array.isArray(v.chapters)
    || !v.chapters.length || !v.facts || typeof v.facts !== 'object') throw new Error('报告草稿身份或格式无效，未替换当前版本。')
  v.chapters.forEach(parseChapter)
  return v as unknown as ReportDraft
}

export const createReportDraftPageId = () => crypto.randomUUID()

export function reportDraftClient(rawBase: string, fetcher: typeof fetch = fetch, sessionId?: string): ReportDraftClient {
  const base = checkupBase(rawBase)
  const path = (id: string) => `/api/v1/checkups/${encodeURIComponent(id)}/draft`
  async function request<T>(url: string, signal: AbortSignal, body: unknown, consume: (response: Response) => Promise<T>): Promise<T> {
    const control = new AbortController()
    const abort = () => control.abort()
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    const timer = setTimeout(abort, 120_000)
    try {
      const response = await fetcher(new URL(url, base), {
        method: body === undefined ? 'GET' : 'POST', signal: control.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!response.ok) {
        const error = record(record(await response.json().catch(() => ({}))).detail)
        throw new CheckupApiError(String(error.code ?? response.status), typeof error.message === 'string' ? error.message : `报告请求失败（${response.status}）。`, response.status >= 500)
      }
      return await consume(response)
    } catch (error) {
      if (control.signal.aborted) throw new Error(signal.aborted ? '本次报告请求已停止；可重新载入确认当前版本。' : '报告处理超时；请重新载入当前草稿后重试。')
      throw error
    } finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
  }
  return {
    async get(id, signal) {
      try { return await request(path(id)+(sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''), signal, undefined, async r => parseDraft(await r.json(), id)) }
      catch (error) { if (error instanceof CheckupApiError && error.code === 'NO_DRAFT') return null; throw error }
    },
    async edit(id, body, signal, onEvent) {
      return request(`${path(id)}/stream`, signal, { ...body, ...(sessionId ? { session_id: sessionId } : {}) }, async response => {
        if (!response.body || !response.headers.get('Content-Type')?.includes('application/x-ndjson')) throw new Error('报告流响应格式无效。')
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
        const abort = () => { void reader.cancel().catch(() => {}) }
        signal.addEventListener('abort', abort, { once: true })
        try {
          while (true) {
            const { value, done } = await reader.read()
            if (signal.aborted) throw new Error('已停止。')
            buffer += decoder.decode(value, { stream: !done })
            const lines = buffer.split('\n'); buffer = lines.pop() ?? ''
            if (done && buffer.trim()) lines.push(buffer)
            for (const line of lines) {
              if (!line.trim()) continue
              const event = record(JSON.parse(line))
              if (event.type === 'error') throw new CheckupApiError(String(event.code), String(event.message), true)
              if (event.type === 'result') return parseDraft(event.result, id)
              if (event.type === 'chapter') onEvent({ type: 'chapter', chapter: parseChapter(event.chapter) })
              else if (event.type === 'stage' && typeof event.stage === 'string' && typeof event.message === 'string') onEvent({ type: 'stage', stage: event.stage, message: event.message })
              else throw new Error('报告流事件格式无效。')
            }
            if (done) throw new Error('报告生成连接中断，请载入当前版本后重试。')
          }
        } finally { signal.removeEventListener('abort', abort); await reader.cancel().catch(() => {}); reader.releaseLock() }
      })
    },
    async export(id, version, signal) {
      return request(`${path(id)}/export`, signal, { expected_version: version, ...(sessionId ? { session_id: sessionId } : {}) }, async response => {
        if (!response.headers.get('Content-Type')?.includes('application/vnd.openxmlformats-officedocument.wordprocessingml.document')) throw new Error('Word导出格式无效。')
        const blob = await response.blob()
        if (!blob.size) throw new Error('Word文件为空。')
        return blob
      })
    },
  }
}

export interface DraftState {
  current: ReportDraft | null; busy: boolean; error: string; progress: string[]; preview: DraftChapter[]
  history: Array<{ instruction: string; version: number }>
}
export function isWordExportCommand(text: string) {
  return /^(?:请|帮我|请帮我)?\s*(?:导出|下载)\s*(?:word|docx|报告)(?:文档|文件)?[。！!\s]*$/i.test(text.trim())
}
export function reportMessageIntent(text: string, previous: 'report' | 'question' = 'report'): 'question' | 'revise' | 'generate' | 'export' {
  const value = text.trim()
  if (isWordExportCommand(value)) return 'export'
  // ponytail: explicit writing requests cover this local demo; use a semantic router if ambiguity becomes common.
  if (/(?:哪[些个一]?|什么|多少|为什么|为何|怎么|如何|是否|有没有)/.test(value)
    && !/(?:把|将).*(?:改|写|删|去掉|移除|增加|补充)|(?:请|帮我).*(?:修改|重写|扩写|润色|精简|缩短|删除|去掉|补充)/.test(value)) return 'question'
  if (/^(?:请|帮我|请帮我)?(?:恢复|还原|撤销删除)第.+[章张]|(?:把|将)第.+[章张](?:恢复|加回来|加回)/.test(value)) return 'revise'
  if (/^(?:为什么|为何|怎么|如何|什么|哪些|是否|有没有|你(?:觉得|建议)|能否解释)|(?:怎么|如何)(?:理解|修改|改写)|为什么.*(?:改|word)/i.test(value)) return 'question'
  if (/(?:修改|改写|重写|扩写|润色|精简|缩短|删掉|删除|去掉|移除|补充|增加|写进|写入|改成|改为|调整).+|(?:把|将).*(?:改|写|删|加|去掉|移除)|(?:报告|word|章节|第.+[章张]).*(?:详细|具体|简洁|清单|口吻|去掉|删掉|不要|聚焦|重点)|(?:写|讲|说)得?(?:更|再)?(?:详细|具体|简洁)|(?:能|可以).*(?:缩短|精简|改写)/i.test(value)) return 'revise'
  if (/(?:生成|重新生成|重做|撰写|写一份).*(?:报告|word)/i.test(value)) return 'generate'
  if (previous === 'report' && /^(?:再|更|太|不够|有点).*(?:详细|具体|简洁|长|短|简略|正式)/.test(value)) return 'revise'
  return 'question'
}
export function createDraftSession(options: {
  binding: () => { id: string; base: string }; client: () => ReportDraftClient | undefined; update: (state: DraftState) => void
}) {
  const empty = (): DraftState => ({ current: null, busy: false, error: '', progress: [], preview: [], history: [] })
  let state = empty(); let generation = 0; let controller: AbortController | undefined
  const publish = () => options.update({ ...state, progress: [...state.progress], preview: [...state.preview], history: [...state.history] })
  async function run<T>(action: (client: ReportDraftClient, id: string, signal: AbortSignal, valid: () => boolean) => Promise<T>): Promise<T | undefined> {
    if (state.busy) return
    const client = options.client()
    if (!client) { state.error = '当前报告服务未连接，请先载入有效同济报告。'; publish(); return }
    const token = ++generation; const binding = { ...options.binding() }; const control = new AbortController(); controller = control
    const valid = () => token === generation && binding.id === options.binding().id && binding.base === options.binding().base && !control.signal.aborted
    state.busy = true; state.error = ''; state.progress = []; state.preview = []; publish()
    try { const result = await action(client, binding.id, control.signal, valid); return valid() ? result : undefined }
    catch (error) { if (valid()) state.error = error instanceof Error ? error.message : '报告处理失败，请重试。' }
    finally { if (token === generation) { state.busy = false; state.preview = []; publish() } }
  }
  function accept(next: ReportDraft, instruction: string) {
    if (next.report_id !== options.binding().id) throw new Error('报告身份已变化，未替换草稿。')
    state.current = next; state.history.push({ instruction, version: next.version }); publish()
  }
  function event(e: DraftEvent, valid: () => boolean) {
    if (!valid()) return
    if (e.type === 'stage') state.progress.push(e.message)
    else state.preview = [...state.preview.filter(c => c.id !== e.chapter.id), e.chapter]
    publish()
  }
  return {
    read: () => state,
    loadOrGenerate: () => run(async (client, id, signal, valid) => {
      const saved = await client.get(id, signal)
      if (!valid()) return
      if (saved) { accept(saved, '载入已保存草稿'); return }
      const next = await client.edit(id, { action: 'generate', expected_version: 0 }, signal, e => event(e, valid))
      if (valid()) accept(next, '生成智能体检报告')
    }),
    generateModel: (instruction = '', context = '') => run(async (client, id, signal, valid) => {
      if (!state.current) throw new Error('请先生成或载入报告草稿。')
      const previous = state.current
      const next = await client.edit(id, { action: 'generate', expected_version: previous.version, ...(instruction ? { instruction } : {}), ...(context ? { context } : {}) }, signal, e => event(e, valid))
      if (valid()) {
        if (next.version !== previous.version + 1 || next.source_hash !== previous.source_hash) throw new Error('生成版本或测算依据不一致，已保留上一版。')
        accept(next, '生成模型组织的报告稿')
      }
    }),
    revise: (instruction: string, context = '') => run(async (client, id, signal, valid) => {
      if (!state.current) throw new Error('请先生成报告草稿。')
      const previous = state.current
      const next = await client.edit(id, { action: 'revise', instruction, expected_version: previous.version, ...(context ? { context } : {}) }, signal, e => event(e, valid))
      if (valid()) {
        if (next.version !== previous.version + 1 || next.source_hash !== previous.source_hash) throw new Error('修订版本或测算依据不一致，已保留上一版。')
        accept(next, instruction)
      }
    }),
    export: (version?: number) => run(async (client, id, signal) => {
      if (!state.current) throw new Error('请先生成报告草稿。')
      return await client.export(id, version ?? state.current.version, signal)
    }),
    cancel() { generation++; controller?.abort(); state.busy = false; state.preview = []; state.error = '已停止本次请求；可重新载入当前草稿确认保存状态。'; publish() },
    acknowledgeError() { state.error = ''; publish() },
    reset() { generation++; controller?.abort(); state = empty(); publish() },
  }
}
