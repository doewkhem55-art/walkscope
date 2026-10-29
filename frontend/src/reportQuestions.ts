/**
 * Report question conversation state for work line 3 (Gate 5), extended for the
 * 阶段 1 问答接口合同 v1 (2026-10-18).
 *
 * Deliberately framework-free and injectable so it can be unit tested offline without a
 * DOM, a network call or the shared app shell. `ReportConversation.vue` is only a view
 * over this module; the host (CheckupApp.vue, owned by work lines 1+2) wires the client,
 * the report, the current focus and the `locate` callback in.
 *
 * Request: POST /api/v1/checkups/{checkup_id}/questions
 *   legacy body { question } stays valid; v1 adds optional { history, focus, mode }.
 * Response: legacy { answer, evidence_ids, mode } stays valid; v1 adds optional
 *   { web_search_used, sources, retrieval, focus_applied, elapsed_ms }.
 */

import type {
  AskOptions,
  CheckupFocus,
  ExternalSource,
  QuestionHistoryTurn,
  QuestionMode,
  QuestionEvent,
  RetrievalInfo,
  RetrievalStatus,
} from './checkups.ts'

export interface ReportAnswer {
  answer: string
  evidence_ids: string[]
  mode: 'model' | 'report_facts'
  web_search_used?: boolean
  sources?: ExternalSource[]
  retrieval?: RetrievalInfo
  focus_applied?: CheckupFocus
  elapsed_ms?: number
}

/** The only thing this module needs from the shared client: `CheckupClient.ask`. */
export interface ReportQuestionAsker {
  ask(id: string, question: string, signal: AbortSignal, options?: AskOptions): Promise<ReportAnswer>
}

/** Identity of the report a conversation is bound to. A change means "discard everything". */
export interface ReportBinding {
  id: string
  base: string
}

export interface TranscriptEntry {
  /** Actual service stages only; provisional answer text never enters history. */
  progress?: Array<Extract<QuestionEvent, { type: 'stage' }>>
  question: string
  answer: string
  evidence_ids: string[]
  mode: ReportAnswer['mode']
  web_search_used: boolean
  sources: ExternalSource[]
  /** What this answer actually did on the network, when the backend reports it. */
  retrieval?: RetrievalInfo
  elapsed_ms?: number
}

export const MAX_QUESTION_LENGTH = 2000
/** Recent context only: the contract allows at most 12 messages / 6 user-assistant rounds. */
export const MAX_HISTORY_TURNS = 6
export const MAX_HISTORY_MESSAGES = 12
export const MAX_HISTORY_CONTENT_LENGTH = 4000
export const MAX_HISTORY_TOTAL_LENGTH = 12000
export const QUESTION_MODES: QuestionMode[] = ['auto', 'web', 'report']
export const DEFAULT_QUESTION_MODE: QuestionMode = 'auto'

export function normalizeQuestionMode(value: unknown): QuestionMode {
  return QUESTION_MODES.includes(value as QuestionMode) ? value as QuestionMode : DEFAULT_QUESTION_MODE
}

function limitHistoryText(text: string, limit = MAX_HISTORY_CONTENT_LENGTH): string {
  const chars = [...text]
  if (chars.length <= limit) return text
  return `${chars.slice(0, Math.max(1, limit - 1)).join('')}…`
}

/**
 * Turn the answered transcript into the bounded `history` array the backend accepts.
 *
 * Only the most recent 6 rounds are kept. Each message is truncated to 4000 characters and,
 * if the total is still above 12000, the oldest messages are dropped so the newest context
 * survives. An empty transcript produces no history at all.
 */
export function buildQuestionHistory(
  transcript: Array<Pick<TranscriptEntry, 'question' | 'answer'>>,
): QuestionHistoryTurn[] {
  const messages: QuestionHistoryTurn[] = []
  for (const entry of transcript.slice(-MAX_HISTORY_TURNS)) {
    const question = limitHistoryText(entry.question)
    const answer = limitHistoryText(entry.answer)
    if (question) messages.push({ role: 'user', content: question })
    if (answer) messages.push({ role: 'assistant', content: answer })
  }
  while (messages.length > MAX_HISTORY_MESSAGES) messages.shift()
  let total = messages.reduce((sum, message) => sum + [...message.content].length, 0)
  while (messages.length > 1 && total > MAX_HISTORY_TOTAL_LENGTH) {
    total -= [...messages[0].content].length
    messages.shift()
  }
  if (total > MAX_HISTORY_TOTAL_LENGTH) {
    messages[0] = { ...messages[0], content: limitHistoryText(messages[0].content, MAX_HISTORY_TOTAL_LENGTH) }
  }
  return messages
}

export type QuestionPhase = 'idle' | 'asking' | 'answered' | 'failed'

/**
 * How the user may recover. Kept separate from the HTTP status so the UI never has to
 * guess, and so a future machine-readable reason code (proposal D) can slot in without
 * changing the view.
 */
export type FailureKind =
  | 'retryable'   // same report, re-send the same question is meaningful
  | 'busy'        // another question is already running on this report
  | 'input'       // the question itself must change
  | 'terminal'    // the report is gone or the contract is broken

export interface QuestionFailure {
  kind: FailureKind
  message: string
  /** Raw code from CheckupApiError, if any. Shown to the user only in a non-blocking way. */
  code: string
}

export interface ReportConversationState {
  phase: QuestionPhase
  progress: Array<Extract<QuestionEvent, { type: 'stage' }>>
  draftAnswer: string
  transcript: TranscriptEntry[]
  failure: QuestionFailure | null
  /** True when the last question can simply be sent again on the same report. */
  canRetry: boolean
  /** Identity of the report that produced this state. */
  reportId: string
  base: string
  /** User-selected network policy for the next question: auto (default), web or report. */
  mode: QuestionMode
}

/** Callbacks the view needs. `update` fires on every state change. */
export interface ReportConversationOptions {
  /** The shared CheckupClient (only `ask` is used). */
  client: ReportQuestionAsker
  /** Current report binding, read lazily so a report switch is picked up per request. */
  binding: () => ReportBinding
  /** Current focus (viewed category / POI / selected categories), read lazily per request. */
  focus?: () => CheckupFocus | null
  /** Initial network policy; the view may change it later through `setMode`. */
  mode?: QuestionMode
  /** Called on every state change so a Vue ref can be updated. */
  update?: (state: ReportConversationState) => void
}

const RETRYABLE_CODES = new Set(['question', 'network', 'timeout', '500', '502', '503', '504'])

function emptyState(binding: ReportBinding, mode: QuestionMode): ReportConversationState {
  return { phase: 'idle', progress: [], draftAnswer: '', transcript: [], failure: null, canRetry: false, reportId: binding.id, base: binding.base, mode }
}

/**
 * Map a thrown error to a user-facing failure.
 *
 * `CheckupApiError` carries a `code`; 409/404/403/422 must stay distinguishable instead of
 * collapsing into "模型错误" (Gate 4 point 7).
 */
export function classifyFailure(error: unknown): QuestionFailure {
  const raw = (error as { code?: unknown })?.code
  const code = typeof raw === 'string' ? raw : ''
  const message = error instanceof Error && error.message ? error.message : '报告问答失败。'
  return classifyCode(code, message)
}

/** Same classification for locally generated failures that never crossed the network. */
function classifyCode(code: string, message: string): QuestionFailure {
  if (code === 'stopped') return { kind: 'input', message, code }
  if (code === 'input' || code === '422') return { kind: 'input', message, code }
  if (code === '429') return { kind: 'busy', message, code }
  if (code === '404' || code === '409' || code === '403') return { kind: 'terminal', message, code }
  if (code === 'contract' || code === 'catalog' || code === 'openapi' || code === 'config') {
    return { kind: 'terminal', message, code }
  }
  if (RETRYABLE_CODES.has(code)) return { kind: 'retryable', message, code }
  // Unknown code: offer a retry, because the failure was network-shaped and a second
  // attempt on the same report is cheap and harmless. It never creates a new checkup.
  return { kind: 'retryable', message, code }
}

/** Split an evidence id into the reference shape the host may act on. */
export type EvidenceRef =
  | { kind: 'report'; key: 'report' }
  | { kind: 'group'; key: string; id: string }
  | { kind: 'facility'; key: string; id: string }
  | { kind: 'poi'; key: string; id: string }
  | { kind: 'route'; key: string; id: string }
  | { kind: 'web'; key: 'web' }
  | { kind: 'unsupported'; key: string }

/**
 * Evidence families expanded by the v1 contract: `poi:<poi_id>` and `route:<target_id>` are
 * resolved so the UI can label them, and `web` marks a live-search answer. A key without a
 * usable id stays `unsupported` and is rendered verbatim so nothing is silently dropped.
 */
export function parseEvidenceId(raw: string): EvidenceRef {
  if (raw === 'report') return { kind: 'report', key: 'report' }
  if (raw === 'web') return { kind: 'web', key: 'web' }
  for (const kind of ['group', 'facility', 'poi', 'route'] as const) {
    const prefix = `${kind}:`
    if (!raw.startsWith(prefix)) continue
    const id = raw.slice(prefix.length).trim()
    if (!id) return { kind: 'unsupported', key: raw }
    return { kind, key: raw, id }
  }
  return { kind: 'unsupported', key: raw }
}

export interface EvidenceNames {
  groups?: Record<string, string>
  facilities?: Record<string, string>
  pois?: Record<string, string>
  routes?: Record<string, string>
}

/** Human label for an evidence chip; falls back to the raw key so nothing is hidden. */
export function evidenceLabel(raw: string, names: EvidenceNames = {}): string {
  const ref = parseEvidenceId(raw)
  if (ref.kind === 'report') return '本次报告'
  if (ref.kind === 'group') return `${names.groups?.[ref.id] ?? ref.id}（组）`
  if (ref.kind === 'facility') return `${names.facilities?.[ref.id] ?? ref.id}（类别）`
  if (ref.kind === 'poi') return `${names.pois?.[ref.id] ?? ref.id}（点位）`
  if (ref.kind === 'route') return `${names.routes?.[ref.id] ?? ref.id}（路线）`
  if (ref.kind === 'web') return '本次联网来源'
  return raw
}

export type RetrievalTone = 'web' | 'report' | 'empty' | 'error' | 'unknown'

export interface RetrievalSummary {
  status: RetrievalStatus | 'unknown'
  label: string
  detail: string
  tone: RetrievalTone
}

/**
 * Turn the server's truthful retrieval record into display text.
 *
 * The only way to say "已联网" is a `searched`/`no_results` retrieval record (or, for an
 * older backend, an explicit `web_search_used=true`). A configured model is never enough.
 */
export function retrievalSummary(
  entry: Pick<TranscriptEntry, 'retrieval' | 'web_search_used' | 'sources'>,
): RetrievalSummary {
  const retrieval = entry.retrieval
  const sourceCount = entry.sources.length
  if (!retrieval) {
    if (entry.web_search_used) {
      return {
        status: 'unknown',
        label: '本次联网（旧版服务未提供检索状态）',
        detail: sourceCount ? `服务返回 ${sourceCount} 条来源；正文读取情况未提供。` : '服务未提供来源明细，不能确认检索结果。',
        tone: 'web',
      }
    }
    return {
      status: 'unknown',
      label: '仅报告事实（未提供联网状态）',
      detail: '服务未返回检索状态，不能据此宣称已联网。',
      tone: 'unknown',
    }
  }
  if (retrieval.status === 'searched') {
    const pages = retrieval.pages_read ?? 0
    const query = retrieval.query ? ` · 查询词：${retrieval.query}` : ''
    return { status: 'searched', label: '本次联网', detail: `来源 ${sourceCount} 条 · 读取正文 ${pages} 页${query}`, tone: 'web' }
  }
  if (retrieval.status === 'no_results') {
    return { status: 'no_results', label: '本次已联网，检索无结果', detail: '没有可用的公开来源；回答仍以报告事实为主。', tone: 'empty' }
  }
  if (retrieval.status === 'failed') {
    const reason = retrieval.error ? `原因：${retrieval.error}` : '服务未提供可读原因'
    return { status: 'failed', label: '联网检索失败', detail: `${reason}；回答仍以报告事实为主。`, tone: 'error' }
  }
  return {
    status: 'not_requested',
    label: '仅报告事实（本次未联网）',
    detail: '本次没有发起联网检索，回答只使用报告事实。',
    tone: 'report',
  }
}

export function createReportConversation({ client, binding, update, focus, mode: initialMode }: ReportConversationOptions) {
  let mode = normalizeQuestionMode(initialMode)
  let state = emptyState(binding(), mode)
  let controller: AbortController | null = null
  let lastQuestion = ''

  function set(next: Partial<ReportConversationState>) {
    state = { ...state, ...next }
    update?.(state)
    return state
  }

  /**
   * True when `token` still describes the report the user is looking at.
   *
   * Both the live binding and the state snapshot are checked: `reset()` rebinds the state
   * even when the caller's binding object is mutated in place, so comparing only against
   * `binding()` would let an answer that resolved during the reset still slip through.
   */
  function isCurrent(token: ReportBinding) {
    const current = binding()
    return current.id === token.id && current.base === token.base
      && state.reportId === token.id && state.base === token.base
  }

  function read() {
    return state
  }

  /**
   * Drop everything and rebind. Called when the host switches report or backend, which
   * must not inherit the previous report's transcript or history. The user's network mode
   * preference is kept, because it is a UI choice rather than report state.
   */
  function reset() {
    controller?.abort()
    controller = null
    lastQuestion = ''
    return set(emptyState(binding(), mode))
  }

  /** Change the network policy for the next question; invalid input is ignored. */
  function setMode(next: QuestionMode) {
    if (!QUESTION_MODES.includes(next)) return state
    mode = next
    return set({ mode: next })
  }

  async function ask(question: string) {
    // One question at a time: a second submit while asking is ignored, not queued.
    if (state.phase === 'asking') return state
    const text = question.trim()
    if (!text) return set({ phase: 'failed', canRetry: false, failure: classifyCode('input', '请输入问题。') })
    if ([...text].length > MAX_QUESTION_LENGTH) {
      return set({ phase: 'failed', canRetry: false, failure: classifyCode('input', `问题最多 ${MAX_QUESTION_LENGTH} 字。`) })
    }

    const token = binding()
    controller?.abort()
    controller = new AbortController()
    const requestController = controller

    // Bounded context is rebuilt from the answered transcript at send time, so a retry
    // resends the same contextual request and a reset starts with an empty history.
    const options: AskOptions = {
      mode: state.mode,
      onEvent: event => {
        if (!isCurrent(token) || requestController.signal.aborted || controller !== requestController) return
        if (event.type === 'stage') set({ progress: [...state.progress, event] })
        else if (event.type === 'draft') set({ draftAnswer: event.text })
        else if (event.type === 'draft_reset') set({ draftAnswer: '' })
      },
    }
    const history = buildQuestionHistory(state.transcript)
    if (history.length) options.history = history
    const currentFocus = focus?.() ?? null
    if (currentFocus) options.focus = currentFocus

    set({ phase: 'asking', progress: [], draftAnswer: '', failure: null, canRetry: false, reportId: token.id, base: token.base })
    try {
      const answer = await client.ask(token.id, text, requestController.signal, options)
      // A late answer for a report the user already left must not appear anywhere.
      if (!isCurrent(token) || requestController.signal.aborted) return read()
      lastQuestion = text
      const entry: TranscriptEntry = {
        question: text,
        progress: [...state.progress],
        answer: answer.answer,
        evidence_ids: [...answer.evidence_ids],
        mode: answer.mode,
        web_search_used: answer.web_search_used === true,
        sources: [...(answer.sources ?? [])],
      }
      if (answer.retrieval) entry.retrieval = answer.retrieval
      if (typeof answer.elapsed_ms === 'number') entry.elapsed_ms = answer.elapsed_ms
      return set({ phase: 'answered', draftAnswer: '', canRetry: false, transcript: [...state.transcript, entry] })
    } catch (error) {
      if (!isCurrent(token) || requestController.signal.aborted) return read()
      // A cancelled request is a user action, not a failure to report.
      if ((error as { code?: string })?.code === 'stopped') return set({ phase: state.transcript.length ? 'answered' : 'idle' })
      lastQuestion = text
      const failure = classifyFailure(error)
      return set({ phase: 'failed', draftAnswer: '', failure, canRetry: failure.kind === 'retryable' })
    }
  }

  /** Re-send exactly the previous question on the same report. Never creates a checkup. */
  async function retry() {
    if (!state.canRetry || !lastQuestion) return state
    return ask(lastQuestion)
  }

  /**
   * Keep a failed question in the box so the user can edit and resend it. The transcript
   * of already-answered questions is untouched.
   */
  function acknowledgeFailure() {
    if (state.phase !== 'failed') return state
    return set({ phase: state.transcript.length ? 'answered' : 'idle', failure: null, canRetry: false })
  }

  function cancel() {
    controller?.abort()
    controller = null
    return set({ phase: state.transcript.length ? 'answered' : 'idle', progress: [], draftAnswer: '', canRetry: false })
  }

  return { read, ask, retry, reset, cancel, acknowledgeFailure, setMode }
}

export type ReportConversation = ReturnType<typeof createReportConversation>
