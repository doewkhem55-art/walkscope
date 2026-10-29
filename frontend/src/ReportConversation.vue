<script setup lang="ts">
/**
 * One chronological conversation for report questions, prose revisions and versioned Word exports.
 *
 * Owned by work lines 3+4. It holds no report math, never submits a checkup and never
 * touches the map: it renders the conversation from `reportQuestions.ts` and asks the host
 * to reveal evidence through the `locate` event. Scoped styles only, so it cannot fight the
 * host layout.
 *
 * Every request asks for live retrieval. The answer's actual retrieval status remains
 * visible, including failures, alongside collapsible sources and report evidence.
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import {
  MAX_QUESTION_LENGTH,
  createReportConversation,
  evidenceLabel,
  parseEvidenceId,
  retrievalSummary,
} from './reportQuestions.ts'
import type {
  EvidenceRef,
  ReportConversationState,
  ReportQuestionAsker,
  RetrievalSummary,
  TranscriptEntry,
} from './reportQuestions.ts'
import { createDraftSession, reportMessageIntent } from './reportDrafts.ts'
import type { DraftState, ReportDraft, ReportDraftClient } from './reportDrafts.ts'
import type { AskOptions, CheckupFocus, QuestionMode } from './checkups.ts'

const props = defineProps<{
  /** Identifies the report this conversation belongs to. Any change resets it. */
  binding: { id: string; base: string }
  /** The shared CheckupClient (only `ask` is used). */
  client: ReportQuestionAsker
  draftClient?: ReportDraftClient
  /** Display names resolved from the 45-category catalog, for evidence chips. */
  facilityNames?: Record<string, string>
  groupNames?: Record<string, string>
  /** Display names for report POIs / routes, when the host has them. */
  poiNames?: Record<string, string>
  routeNames?: Record<string, string>
  /** `report_questions_mode` reported by the catalog, shown before the first answer. */
  reportMode?: 'model' | 'report_facts'
  /** Default text for the empty state. */
  placeholder?: string
  /** What the host currently shows: viewed category/group, selected categories or POI. */
  focus?: CheckupFocus | null
  /** Legacy host prop; requests from this view always use live retrieval. */
  defaultMode?: QuestionMode
}>()

const emit = defineEmits<{
  /** Ask the host to reveal the report section / map object behind this evidence key. */
  (event: 'locate', ref: EvidenceRef): void
}>()

const emptyState = (): ReportConversationState => ({
  phase: 'idle', progress: [], draftAnswer: '', transcript: [], failure: null, canRetry: false,
  reportId: props.binding.id, base: props.binding.base,
  mode: 'web',
})

const state = ref<ReportConversationState>(emptyState())
const draft = ref('')
const pendingQuestion = ref('')
const messageList = ref<HTMLElement | null>(null)
type ConversationTurn = TranscriptEntry & { report?: ReportDraft; changed?: string[] }
const transcript = ref<ConversationTurn[]>([])
const lastTopic = ref<'report' | 'question'>('report')
const draftState = ref<DraftState>({ current: null, busy: false, error: '', progress: [], preview: [], history: [] })
const draftSession = createDraftSession({
  binding: () => props.binding, client: () => props.draftClient, update: value => {
    const previous = draftState.value.current
    draftState.value = value
    if (value.current && !transcript.value.some(item => item.report?.version === value.current?.version)) {
      const report = value.current
      const loaded = value.history.at(-1)?.instruction === '载入已保存草稿'
      transcript.value.push({ question: loaded ? '' : pendingQuestion.value, answer: '', mode: report.mode, web_search_used: false, sources: [], evidence_ids: [], report,
        changed: previous ? [...report.chapters.filter(chapter => JSON.stringify(chapter) !== JSON.stringify(previous.chapters.find(c => c.id === chapter.id))).map(c => c.title), ...previous.chapters.filter(c => !report.chapters.some(next => next.id === c.id)).map(c => `${c.title}（已删除）`)] : [] })
      pendingQuestion.value = ''
      lastTopic.value = 'report'
    }
  },
})
const lastDraftAction = ref<'generate' | 'model' | 'revise' | 'export'>('generate')
const lastRevision = ref('')
const revisionSubmitted = ref(false)
const lastExportVersion = ref<number>()
async function startReportDraft() {
  if (asking.value) return
  messageList.value?.focus?.({ preventScroll: true })
  lastDraftAction.value = 'generate'
  await draftSession.loadOrGenerate()
  await nextTick()
  if (messageList.value && !draftState.value.error) messageList.value.scrollTop = 0
}
async function generateModelDraft(instruction = '') {
  if (asking.value) return
  lastDraftAction.value = 'model'
  await draftSession.generateModel(instruction, recentContext())
}
async function exportWord(version = draftState.value.current?.version) {
  if (asking.value) return
  lastDraftAction.value = 'export'
  lastExportVersion.value = version
  const file = await draftSession.export(version)
  if (!file) return
  const url = URL.createObjectURL(file)
  const link = document.createElement('a')
  link.href = url; link.download = '同济生活圈规划研判报告.docx'
  document.body.appendChild(link); link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function retryDraft() {
  if (asking.value) return
  if (lastDraftAction.value === 'model' || lastDraftAction.value === 'revise') {
    const previous = draftState.value.current?.version
    await draftSession.loadOrGenerate()
    if (draftState.value.error) return
    if (revisionSubmitted.value && draftState.value.current?.version !== previous) { draft.value = ''; pendingQuestion.value = ''; return }
    pendingQuestion.value = lastRevision.value
    revisionSubmitted.value = true
  }
  if (lastDraftAction.value === 'generate') await startReportDraft()
  else if (lastDraftAction.value === 'model') await generateModelDraft(lastRevision.value)
  else if (lastDraftAction.value === 'export') await exportWord(lastExportVersion.value)
  else await draftSession.revise(lastRevision.value, recentContext())
  if (!draftState.value.error) { draft.value = ''; pendingQuestion.value = '' }
}


const session = createReportConversation({
  // Report-internal text/version questions use saved context; external questions retain web retrieval.
  client: { ask: (id, question, signal, options) => props.client.ask(id, question, signal, questionOptions(question, options)) },
  binding: () => props.binding,
  focus: () => props.focus ?? null,
  mode: 'web',
  update: next => {
    const accepted = next.transcript.slice(state.value.transcript.length)
    state.value = next
    if (accepted.length) { transcript.value.push(...accepted); pendingQuestion.value = ''; lastTopic.value = 'question' }
  },
})

// Switching report or backend must not leave the previous report's answers on screen.
watch(() => `${props.binding.base}#${props.binding.id}`, () => {
  draft.value = ''
  pendingQuestion.value = ''
  session.reset()
  draftSession.reset()
  transcript.value = []
  lastTopic.value = 'report'
})
onUnmounted(() => { session.cancel(); draftSession.reset() })

const asking = computed(() => state.value.phase === 'asking' || draftState.value.busy)
const length = computed(() => [...draft.value].length)
const tooLong = computed(() => length.value > MAX_QUESTION_LENGTH)
const canAsk = computed(() => !asking.value && draft.value.trim().length > 0 && !tooLong.value)
const failure = computed(() => state.value.failure)
watch(() => [state.value.phase, state.value.transcript.length, state.value.progress.length, state.value.draftAnswer, draftState.value.preview.length, draftState.value.current?.version, draftState.value.progress.length], async () => {
  await nextTick()
  const list = messageList.value
  if (list) list.scrollTop = list.scrollHeight
})

function recentContext() {
  const last = state.value.transcript.at(-1)
  return last ? [...`最近提问：${last.question}\n最近答复：${last.answer}`].slice(0, 4000).join('') : ''
}
function questionOptions(question: string, options?: AskOptions): AskOptions {
  const current = draftState.value.current
  if (!current) return { ...options, mode: 'web' }
  const content = ['当前页面的报告内容', current.notice, ...current.chapters.map(c => `${c.title}\n${[...c.paragraphs.join('\n')].slice(0, 450).join('')}`)].join('\n')
  const localQuestion = /刚才|上一版|当前.*版本|第.+章|章节|文风|段落|正文|导出|word|删掉|删除/i.test(question)
    && !/最新|政策|法规|标准|官网|搜索|查一下|联网/.test(question)
  return { ...options, mode: localQuestion ? 'report' : 'web', history: [
    { role: 'user', content: '以下为当前页面报告的章节和正文上下文。请据此回答文字问题，无需提及内部编号；测算事实仍以原始数据为准。' },
    { role: 'assistant', content: [...content].slice(0, 3500).join('') },
    ...(options?.history ?? []).slice(-8).map(turn => ({ ...turn, content: [...turn.content].slice(0, 800).join('') })),
  ] }
}

async function submit() {
  if (!canAsk.value || asking.value) return
  const question = draft.value.trim()
  const intent = reportMessageIntent(question, lastTopic.value)
  draftSession.acknowledgeError()
  if (intent === 'export') { await exportWord(); if (!draftState.value.error) draft.value = ''; return }
  if (intent === 'generate' || intent === 'revise') {
    // Loading is automatic; a writing request must never fall through to ordinary Q&A.
    lastRevision.value = question
    lastDraftAction.value = intent === 'generate' ? 'model' : 'revise'
    revisionSubmitted.value = false
    pendingQuestion.value = question
    await draftSession.loadOrGenerate()
    if (!draftState.value.current || draftState.value.error) return
    session.acknowledgeFailure()
    pendingQuestion.value = question
    revisionSubmitted.value = true
    if (intent === 'generate') await generateModelDraft(question)
    else await draftSession.revise(question, recentContext())
    if (!draftState.value.error) { draft.value = ''; pendingQuestion.value = '' }
    return
  }
  const answeredBefore = state.value.transcript.length
  pendingQuestion.value = question
  await session.ask(question)
  if (state.value.transcript.length > answeredBefore && state.value.transcript.at(-1)?.question === question) draft.value = ''
}

async function retry() {
  if (asking.value || !state.value.canRetry) return
  const answeredBefore = state.value.transcript.length
  const retryQuestion = pendingQuestion.value
  await session.retry()
  if (state.value.transcript.length > answeredBefore && draft.value.trim() === retryQuestion) draft.value = ''
}

function dismissFailure() {
  session.acknowledgeFailure()
}

function cancel() {
  if (draftState.value.busy) draftSession.cancel()
  session.cancel()
  pendingQuestion.value = ''
}

function onEnter(event: KeyboardEvent) {
  // Enter confirms a Chinese IME candidate; only a completed keypress sends a message.
  if (event.isComposing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return
  event.preventDefault()
  void submit()
}

/** Evidence chips are clickable only for the families the host can locate today. */
function evidenceRef(id: string): EvidenceRef {
  return parseEvidenceId(id)
}

function isLocatable(id: string): boolean {
  const kind = parseEvidenceId(id).kind
  return kind === 'report' || kind === 'group' || kind === 'facility'
}

function locate(id: string) {
  const ref = evidenceRef(id)
  if (!isLocatable(id)) return
  emit('locate', ref)
}

function label(id: string) {
  return evidenceLabel(id, {
    groups: props.groupNames,
    facilities: props.facilityNames,
    pois: props.poiNames,
    routes: props.routeNames,
  })
}

function evidenceTitle(id: string): string {
  const ref = parseEvidenceId(id)
  if (ref.kind === 'poi') return `设施点证据 ${ref.id}；当前报告未渲染可定位元素，保留原始编号`
  if (ref.kind === 'route') return `路线证据 ${ref.id}；当前报告未渲染可定位元素，保留原始编号`
  if (ref.kind === 'web') return '本次联网检索来源，见下方公开来源列表'
  return `无法解析的证据键，原样显示：${id}`
}

function retrievalFor(item: TranscriptEntry): RetrievalSummary {
  return retrievalSummary(item)
}

function modeLabel(mode: 'model' | 'report_facts') {
  return mode === 'report_facts' ? '报告事实查询（未启用模型解读）' : '模型解读'
}

defineExpose({ session, state, startReportDraft, draftState })
</script>

<template>
  <section class="report-conversation" aria-label="围绕本报告提问" :aria-busy="asking">
    <div ref="messageList" class="message-list" tabindex="-1" role="log" aria-label="报告对话记录" aria-live="polite" aria-relevant="additions text">
      <div v-if="!transcript.length && !asking && !failure && !draftState.current && !draftState.error" class="conversation-welcome">
        <span class="assistant-symbol" aria-hidden="true">✧</span>
        <h3>从这份报告，查看设施现状与调研线索</h3>
        <p>可以提问，也可以直接输入报告修改要求；新报告下方可导出 Word。</p>
        <small>每次提问都会尝试联网，检索结果与失败原因会如实显示。</small>
        <small v-if="props.reportMode === 'report_facts'">当前服务仅支持报告事实查询，尚未启用模型解读。</small>
      </div>

      <template v-for="(item, index) in transcript" :key="`${index}-${item.question}`">
        <article v-if="item.question" class="chat-message user-message" aria-label="你的问题">
          <small class="speaker">你</small>
          <p class="user-bubble preserve">{{ item.question }}</p>
        </article>
        <article class="chat-message assistant-message" aria-label="报告助手的回答">
          <small class="speaker">✧ 报告助手</small>
          <div v-if="item.report" class="answer-card report-draft" aria-label="报告内容">
            <small>{{ item.question ? '已按要求更新' : '完整初稿' }}</small>
            <h3>{{ item.report.title }}</h3>
            <p v-if="item.changed?.length" class="revision-summary">已更新：{{ item.changed.join('、') }}。下方 Word 包含本次修改。</p>
            <details class="report-content">
              <summary>完整报告</summary>
              <p class="draft-notice">{{ item.report.notice }}</p>
              <section v-for="chapter in item.report.chapters" :key="chapter.id" class="draft-chapter">
                <h4>{{ chapter.title }}</h4>
                <p v-for="(paragraph,index) in chapter.paragraphs" :key="index" class="preserve">{{ paragraph }}</p>
                <div v-if="chapter.table_headers.length" class="draft-table-scroll">
                  <table><thead><tr><th v-for="(label,index) in chapter.table_headers" :key="index" scope="col">{{ label }}</th></tr></thead>
                    <tbody><tr v-for="(row,index) in chapter.table_rows" :key="index"><td v-for="(cell,column) in row" :key="column">{{ cell }}</td></tr></tbody>
                  </table>
                </div>
              </section>
            </details>
            <footer class="report-export"><button type="button" :disabled="asking" @click="exportWord(item.report.version)">导出 Word</button><small>可编辑 Word 文档</small></footer>
          </div>
          <div v-else class="answer-card">
            <p class="preserve">{{ item.answer }}</p>
            <details v-if="item.progress?.length" class="answer-details workflow-progress">
              <summary>查看处理过程与依据整理</summary>
              <ol><li v-for="(step, stepIndex) in item.progress" :key="stepIndex">{{ step.message }}</li></ol>
            </details>
            <p
              class="retrieval-status" :class="`retrieval-${retrievalFor(item).tone}`"
              :data-status="retrievalFor(item).status" data-testid="retrieval-status"
            >
              <span class="sr-only">检索状态：</span>{{ retrievalFor(item).label }}
            </p>
            <details class="answer-details">
              <summary>来源与依据 <span>{{ item.sources.length }} 条来源 · {{ item.evidence_ids.length }} 项依据</span></summary>
              <p class="small muted">{{ retrievalFor(item).detail }}</p>
              <p class="small muted">回答方式：{{ modeLabel(item.mode) }}<span v-if="item.elapsed_ms !== undefined"> · 耗时 {{ item.elapsed_ms }} ms</span></p>
              <div class="evidence-list"><strong>报告依据</strong>
                <span v-if="!item.evidence_ids.length" class="small muted">服务未返回证据编号。</span>
                <template v-for="id in item.evidence_ids" :key="id">
                  <button
                    v-if="isLocatable(id)" type="button" class="evidence-chip"
                    :title="`定位到 ${label(id)}`" @click="locate(id)"
                  >{{ label(id) }}</button>
                  <span v-else class="evidence-chip secondary" :title="evidenceTitle(id)">{{ label(id) }}</span>
                </template>
              </div>
              <div v-if="item.sources.length" class="source-list">
                <strong>公开来源</strong>
                <ul>
                  <li v-for="source in item.sources" :key="source.url">
                    <a :href="source.url" target="_blank" rel="noreferrer noopener">{{ source.title }}</a>
                    <span v-if="source.published_at" class="small muted"> · {{ source.published_at }}</span>
                  </li>
                </ul>
                <span class="small muted">外部信息不覆盖报告中的计数与分数。</span>
              </div>
            </details>
          </div>
        </article>
      </template>

      <article v-if="pendingQuestion && (asking || failure || draftState.error)" class="chat-message user-message" aria-label="你的问题">
        <small class="speaker">你</small>
        <p class="user-bubble preserve">{{ pendingQuestion }}</p>
      </article>
      <div v-if="draftState.busy" class="answer-card workflow-progress" role="status" aria-label="报告生成进度">
        <strong>正在处理报告…</strong>
        <p>{{ draftState.progress.at(-1) ?? '正在读取已保存报告…' }}</p>
        <details v-if="draftState.progress.length"><summary>处理进度</summary><ol><li v-for="(message,index) in draftState.progress" :key="index">{{ message }}</li></ol></details>
      </div>
      <div v-if="draftState.error" class="notice" role="alert">
        <p>{{ draftState.error }}</p>
        <button type="button" :disabled="asking" @click="retryDraft">重试</button>
      </div>
      <div v-if="asking && !draftState.busy" class="chat-message assistant-message" role="status">
        <small class="speaker">✧ 报告助手</small>
        <div class="answer-card live-answer">
          <div class="workflow-progress">
            <strong>处理过程与依据</strong>
            <ol v-if="state.progress.length" data-testid="question-progress">
              <li v-for="(step, index) in state.progress" :key="index">{{ step.message }}</li>
            </ol>
            <p v-else class="small muted">正在连接报告问答服务…</p>
          </div>
          <template v-if="state.draftAnswer">
            <small class="draft-label">答复草稿 · 正在生成，尚未通过依据校验</small>
            <p class="preserve" data-testid="question-draft">{{ state.draftAnswer }}</p>
          </template>
          <p class="thinking"><span aria-hidden="true"><i></i><i></i><i></i></span>{{ state.progress.at(-1)?.message ?? '等待服务响应…' }}</p>
        </div>
      </div>
      <div v-if="failure" class="notice" role="alert" data-testid="question-failure">
        <p>{{ failure.message }}<small v-if="failure.code"> · {{ failure.code }}</small></p>
        <small v-if="failure.kind === 'retryable'">可在同一份报告上重问，无需重新体检。</small>
        <small v-else-if="failure.kind === 'busy'">上一个问题仍在解读，请稍后再问。</small>
        <small v-else-if="failure.kind === 'terminal'">当前报告不可用，请核对服务或切换报告。</small>
        <div class="failure-actions">
          <button v-if="state.canRetry" type="button" @click="retry">重试这个问题</button>
          <button type="button" class="link" @click="dismissFailure">知道了</button>
        </div>
      </div>
    </div>

    <form class="ask-form" @submit.prevent="submit">
      <label class="sr-only" for="report-question">你的问题</label>
      <div class="composer">
        <textarea
          id="report-question" v-model="draft" :maxlength="MAX_QUESTION_LENGTH" rows="2"
          :disabled="asking" aria-describedby="report-question-help"
          :placeholder="props.placeholder ?? '可以提问，也可以直接说：把第四章写得更具体一些'"
          @keydown.enter="onEnter"
        />
        <button v-if="asking" type="button" class="send-button stop-button" @click="cancel" aria-label="停止本次提问">停止 <span aria-hidden="true">■</span></button>
        <button v-else type="submit" class="send-button" :disabled="!canAsk" aria-label="发送问题">发送 <span aria-hidden="true">↑</span></button>
      </div>
      <p id="report-question-help" class="composer-help">
        <span>Enter 发送 · Shift + Enter 换行</span>
        <span :class="{ 'over-limit': tooLong }">{{ length }} / {{ MAX_QUESTION_LENGTH }}</span>
      </p>
    </form>
  </section>
</template>

<style scoped>
.report-content { max-height: min(38dvh, 340px); overflow:auto; overscroll-behavior:contain; scrollbar-width:thin; }
.report-content > summary { cursor:pointer; color:#b5d8ee; font-size:11px; }
.report-content > section { margin-top:12px; }
.report-export { display:flex; align-items:center; flex-wrap:wrap; gap:10px; border-top:1px solid #b3cce51c; padding-top:10px; }
.report-export button { width:auto; padding:8px 13px; border:1px solid #b6dcff55; border-radius:9px; background:#93cbf228; color:#eaf8ff; cursor:pointer; }
.report-export button:disabled { opacity:.4; cursor:default; }
.report-export small,.revision-summary { color:#b6d5e8; font-size:10px; }
.revision-summary { margin:0; }
.report-draft h3 { margin:0; font-size:16px; line-height:1.8; }
.draft-notice { margin:0; color:#e8d6ac; font-size:11px; }
.draft-chapter { display:grid; gap:8px; min-width:0; }
.draft-chapter h4 { margin:5px 0 0; font-size:13px; color:#d8edfa; }
.draft-table-scroll { overflow-x:auto; }
.draft-table-scroll table { border-collapse:collapse; width:100%; min-width:340px; font-size:10px; }
.draft-table-scroll th,.draft-table-scroll td { text-align:left; padding:7px 6px; border:1px solid #b6dcff33; }
.draft-table-scroll th { background:#92caff16; }

.report-conversation { display: flex; flex-direction: column; min-height: 0; height: 100%; overflow: hidden; color: #e8eef7; }
.message-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 20px 18px 12px; scrollbar-width: thin; scrollbar-color: #b8deff38 transparent; }
.conversation-welcome { min-height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 12px; padding: 16px 6px; color: #b4c9db; }
.conversation-welcome h3 { margin: 0; font-size: 17px; color: #e7f7ff; font-weight: 500; letter-spacing: .03em; }
.conversation-welcome p { margin: 0; font-size: 12px; line-height: 1.8; }
.conversation-welcome small { font-size: 10px; line-height: 1.7; opacity: .8; }
.assistant-symbol { font-size: 32px; color: #b4e3ff; text-shadow: 0 0 22px #88cfff80; }
.chat-message { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin-bottom: 18px; }
.user-message { align-items: flex-end; padding-left: 12%; }
.speaker { font-size: 10px; letter-spacing: .06em; color: #a6c3d8; }
.preserve { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
.user-bubble { padding: 10px 14px; border-radius: 16px 16px 4px 16px; border: 1px solid #b4dfff38; background: linear-gradient(135deg, #5ca9db38, #406eab24); color: #eaf6ff; line-height: 1.7; font-size: 13px; }
.answer-card { display: grid; gap: 12px; max-width: 100%; padding: 13px 14px; border: 1px solid rgba(160, 190, 225, .16); border-radius: 4px 16px 16px 16px; background: rgba(255, 255, 255, .04); color: #e8eef7; line-height: 1.8; overflow-wrap: anywhere; font-size: 13px; }
.ask-form { flex-shrink: 0; display: grid; gap: 8px; padding: 12px 16px 10px; border-top: 1px solid #b1d5f421; background: #0c2037b8; }
.composer { display: flex; align-items: flex-end; gap: 8px; padding: 10px; border: 1px solid #b6dcff38; border-radius: 15px; background: #07182980; transition: border-color .2s, box-shadow .2s; }
.composer:focus-within { border-color: #c6eaff7a; box-shadow: 0 0 18px #85caff13; }
.composer textarea { flex: 1; width: 100%; min-width: 0; min-height: 42px; max-height: 120px; resize: vertical; margin: 0; padding: 3px 2px; border: 0; border-radius: 0; background: transparent; color: #edf7ff; font: inherit; font-size: 12px; line-height: 1.7; outline: none; box-shadow: none; }
.composer textarea::placeholder { color: #8da7bf; }
.send-button { flex-shrink: 0; width: auto; margin: 0; padding: 8px 10px; border: 1px solid #c4e8ff59; border-radius: 11px; background: #93cbf228; color: #eaf8ff; font-size: 11px; cursor: pointer; }
.send-button span { margin-left: 3px; }
.send-button:disabled { opacity: .35; cursor: default; }
.send-button:not(:disabled):hover { background: #93cbf243; box-shadow: 0 0 16px #a3d3ff22; }
.composer-help { display: flex; justify-content: space-between; gap: 6px; margin: 0; color: #91a9bf; font-size: 9px; line-height: 1.5; }
.notice { margin: 4px 0 14px; padding: 12px; border: 1px solid #f0b45a48; border-radius: 12px; background: #f0b45a12; color: #ffe7bd; font-size: 12px; line-height: 1.7; }
.notice p { margin: 0 0 6px; }
.notice small { display: block; }
.failure-actions { display: flex; gap: 10px; margin-top: 8px; }
.failure-actions button { width: auto; padding: 4px 9px; border: 1px solid #f0b45a55; border-radius: 8px; color: #ffe7bd; background: #f0b45a15; font-size: 11px; }
.notice .link { border: 0; background: none; text-decoration: underline; }
.over-limit { color: #ff8a80; }
.workflow-progress { font-size: 11px; color: #b6d5e8; }
.workflow-progress ol { margin: 8px 0; padding-left: 20px; }
.draft-label { font-size: 10px; color: #e8d6ac; }
.thinking { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 11px; color: #b6d5e8; }
.thinking>span { display: flex; gap: 3px; }
.thinking i { width: 4px; height: 4px; border-radius: 50%; background: #b8deff; animation: breathe 1.2s ease-in-out infinite; }
.thinking i:nth-child(2) { animation-delay: .15s; }
.thinking i:nth-child(3) { animation-delay: .3s; }
.retrieval-status { margin: 0; font-size: 10px; color: #adc8de; line-height: 1.6; }
.retrieval-web { color: #b9e7dd; }
.retrieval-empty { color: #e8d6ac; }
.retrieval-error { color: #ffbcb7; }
.answer-details { min-width: 0; border-top: 1px solid #b3cce51c; padding-top: 8px; }
.answer-details summary { cursor: pointer; color: #b5cce0; font-size: 10px; }
.answer-details summary span { margin-left: 5px; opacity: .7; font-size: 9px; }
.answer-details[open] summary { margin-bottom: 10px; }
.small { font-size: 10px; }
.muted { color: #9bb5ca; }
.evidence-list { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.evidence-list strong, .source-list strong { font-size: 10px; font-weight: 500; }
.evidence-chip { width: auto; padding: 3px 6px; border: 1px solid #a0bee14d; border-radius: 6px; background: #ffffff0f; color: #dcecf7; font-size: 10px; cursor: pointer; }
.evidence-chip.secondary { cursor: help; border-style: dashed; color: #c3d1e3; }
.source-list { display: grid; gap: 4px; margin-top: 10px; padding-top: 8px; border-top: 1px dashed #a0bee12e; }
.source-list ul { margin: 0; padding-left: 18px; }
.source-list a { color: #a6dcff; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }
button:focus-visible, summary:focus-visible { outline: 2px solid #b3e5ff; outline-offset: 3px; }
@keyframes breathe { 50% { opacity: .25; transform: translateY(-2px); } }
@media (prefers-reduced-motion: reduce) { .thinking i { animation: none; } }
@media (max-width: 520px) { .message-list { padding: 14px 12px 8px; } .ask-form { padding: 10px; } .answer-card { padding: 10px 12px; } }
</style>
