<!--
  知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
  说明：前端构建链路于 Phase 1 后续迭代引入，本文件为源码交付（Vue 3 SFC），
  暂不参与构建；构建链路就绪后随 web/training 工程接入。

  TutorChat.vue —— AI 导师聊天最小源码（FR-TR-001 消费端，NFR-USA-03 导览锚点页）。
  职责：
  - SSE 消费：POST /api/v1/training/tutor/sessions/{sessionUid}/ask
    （Accept: text/event-stream），事件序列 meta → delta* → done，
    兜底路径 meta → gap → done，异常路径 error（SR-TR-001 §5）；
  - 来源引用渲染块：回答中的 [来源N] 标记渲染为可点击引用芯片，点击展开
    对应知识块（标题/相似度/chunk_id/内容摘录）——「回答必须可溯源」的前端呈现；
  - data-tour 锚点：输入区 data-tour="tutor-ask"、引用块 data-tour="tutor-sources"、
    消息历史 data-tour="tutor-history"——内置 v1 导览第 2~4 步锚定于此
    （core/guidance/config.py FIRST_LOGIN_TOUR，CI 选择器守护的对象）。

  降级口径：SSE 异常展示中文错误提示，不白屏；引用缺失（gap 回答）展示
  「已记录为知识缺口」提示条。
-->
<script setup lang="ts">
import { nextTick, ref } from 'vue'
import EmptyState from './EmptyState.vue'

/** 知识来源引用（SR-TR-001 §5 meta.refs / done.refs 的前端投影） */
interface SourceRef {
  no: number
  chunk_id: number
  title: string
  score: number
  kb_type?: string
  content?: string
}
/** 单条消息（学员提问或导师回答） */
interface ChatMessage {
  role: 'trainee' | 'tutor'
  text: string
  refs: SourceRef[]
  isGap: boolean
  streaming: boolean
}

const props = defineProps<{
  /** 会话 UID（POST /api/v1/training/tutor/sessions 创建后获得） */
  sessionUid: string
}>()

const messages = ref<ChatMessage[]>([])
const question = ref('')
const asking = ref(false)
const errorMessage = ref('')
/** 引用展开态：messageIndex → Set<引用编号>（[来源N] 点击展开/收起 chunk 内容） */
const expandedRefs = ref<Record<number, Set<number>>>({})

const API_BASE = '/api/v1/training/tutor/sessions'

/** SSE 事件流解析：按空行分帧，解析 event:/data: 两行（text/event-stream 规范） */
async function consumeSse(
  resp: Response,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): Promise<void> {
  if (!resp.body) throw new Error('SSE 响应无内容')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // 按 \n\n 分帧；残帧留在 buffer 等下一 chunk
    let sep = buffer.indexOf('\n\n')
    while (sep >= 0) {
      dispatchFrame(buffer.slice(0, sep), onEvent)
      buffer = buffer.slice(sep + 2)
      sep = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim()) dispatchFrame(buffer, onEvent)
}

/** 单帧解析：event: xxx / data: {...} 两行 → 回调 */
function dispatchFrame(
  frame: string,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): void {
  let event = 'message'
  let dataRaw = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataRaw += line.slice(5).trim()
  }
  if (!dataRaw) return
  try {
    onEvent(event, JSON.parse(dataRaw) as Record<string, unknown>)
  } catch {
    /* 非 JSON 数据帧忽略：SSE 协议注释帧等，不中断流消费 */
  }
}

/** 提问主流程：建流 → meta 引用先行渲染 → delta 增量追加 → done/gap 收尾 */
async function ask(): Promise<void> {
  const text = question.value.trim()
  if (!text || asking.value) return
  asking.value = true
  errorMessage.value = ''
  const tutorMessage: ChatMessage = { role: 'tutor', text: '', refs: [], isGap: false, streaming: true }
  messages.value.push({ role: 'trainee', text, refs: [], isGap: false, streaming: false }, tutorMessage)
  const messageIndex = messages.value.length - 1
  question.value = ''
  try {
    const resp = await fetch(`${API_BASE}/${encodeURIComponent(props.sessionUid)}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ question: text, client_msg_id: crypto.randomUUID() }),
    })
    if (!resp.ok) throw new Error(`导师服务响应异常（HTTP ${resp.status}）`)
    await consumeSse(resp, (event, data) => {
      if (event === 'meta') {
        // 检索完成先推引用：前端先行渲染「知识依据」卡片（SR-TR-001 §5）
        tutorMessage.refs = (data.refs as SourceRef[] | undefined) ?? []
      } else if (event === 'delta') {
        tutorMessage.text += String(data.text ?? '')
      } else if (event === 'done') {
        tutorMessage.streaming = false
        tutorMessage.isGap = Boolean(data.is_gap)
        const doneRefs = data.refs as SourceRef[] | undefined
        if (doneRefs && doneRefs.length > 0) tutorMessage.refs = doneRefs
      } else if (event === 'gap') {
        // 零编造兜底：固定话术（非 LLM 生成），问题已记录为知识缺口
        tutorMessage.streaming = false
        tutorMessage.isGap = true
        tutorMessage.text = String(data.message ?? '')
      } else if (event === 'error') {
        errorMessage.value = String(data.message ?? '知识服务暂不可用，请稍后重试')
        tutorMessage.streaming = false
      }
    })
    tutorMessage.streaming = false
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '网络异常，请稍后重试'
    tutorMessage.streaming = false
  } finally {
    asking.value = false
    void scrollToBottom()
  }
}

/** [来源N] 展开态切换：展开即显示 chunk 内容（标题/相似度/摘录） */
function toggleRef(messageIndex: number, no: number): void {
  const set = expandedRefs.value[messageIndex] ?? new Set<number>()
  if (set.has(no)) set.delete(no)
  else set.add(no)
  expandedRefs.value = { ...expandedRefs.value, [messageIndex]: set }
}

function isRefExpanded(messageIndex: number, no: number): boolean {
  return expandedRefs.value[messageIndex]?.has(no) ?? false
}

/** 回答正文中的 [N] 标记改渲染为引用芯片：拆分为 文本段 + 引用位（保留原文语义） */
function renderSegments(text: string): Array<{ kind: 'text'; value: string } | { kind: 'cite'; no: number }> {
  const segments: Array<{ kind: 'text'; value: string } | { kind: 'cite'; no: number }> = []
  let cursor = 0
  const pattern = /\[(\d+)\]/g
  for (const match of text.matchAll(pattern)) {
    if (match.index! > cursor) segments.push({ kind: 'text', value: text.slice(cursor, match.index) })
    segments.push({ kind: 'cite', no: Number(match[1]) })
    cursor = match.index! + match[0].length
  }
  if (cursor < text.length) segments.push({ kind: 'text', value: text.slice(cursor) })
  return segments
}

function scrollToBottom(): Promise<void> {
  return nextTick(() => {
    const history = document.querySelector<HTMLElement>('[data-tour="tutor-history"]')
    if (history) history.scrollTop = history.scrollHeight
  })
}
</script>

<template>
  <div class="tutor-chat">
    <!-- 消息历史（导览步骤 4 锚点：回看历史问答） -->
    <section class="tutor-chat__history" data-tour="tutor-history" aria-live="polite">
      <EmptyState v-if="messages.length === 0" scene-id="tutor_no_qa" />
      <article
        v-for="(message, messageIndex) in messages"
        :key="messageIndex"
        :class="['tutor-chat__msg', `tutor-chat__msg--${message.role}`]"
      >
        <!-- 学员提问：纯文本 -->
        <p v-if="message.role === 'trainee'">{{ message.text }}</p>

        <!-- 导师回答：正文（[N] → 引用芯片）+ 来源引用块（导览步骤 3 锚点） -->
        <template v-else>
          <div class="tutor-chat__answer">
            <template v-for="(segment, i) in renderSegments(message.text)" :key="i">
              <span v-if="segment.kind === 'text'">{{ segment.value }}</span>
              <button
                v-else
                type="button"
                class="tutor-chat__cite-chip"
                :title="`展开来源 ${segment.no}`"
                @click="toggleRef(messageIndex, segment.no)"
              >
                [来源{{ segment.no }}]
              </button>
            </template>
            <span v-if="message.streaming" class="tutor-chat__cursor">▍</span>
          </div>

          <!-- 缺口提示条：零编造兜底的可见呈现（知识库补充后会得到准确回答） -->
          <p v-if="message.isGap" class="tutor-chat__gap-tip">
            该问题已记录为知识缺口，知识库补充后将可追问。
          </p>

          <!-- 来源引用渲染块：[来源N] 展开 chunk 内容（回答 100% 可溯源的前端呈现） -->
          <div
            v-if="message.refs.length > 0"
            class="tutor-chat__sources"
            data-tour="tutor-sources"
          >
            <h4>知识来源（{{ message.refs.length }}）</h4>
            <ul>
              <li v-for="ref in message.refs" :key="ref.no">
                <button
                  type="button"
                  class="tutor-chat__ref-toggle"
                  @click="toggleRef(messageIndex, ref.no)"
                >
                  [来源{{ ref.no }}] {{ ref.title }}
                  <small>相似度 {{ (ref.score * 100).toFixed(0) }}%</small>
                </button>
                <div v-if="isRefExpanded(messageIndex, ref.no)" class="tutor-chat__ref-detail">
                  <p v-if="ref.kb_type">知识库类型：{{ ref.kb_type }}</p>
                  <p>知识块 #{{ ref.chunk_id }}</p>
                  <blockquote v-if="ref.content">{{ ref.content }}</blockquote>
                  <blockquote v-else>（chunk 摘录随 done.refs 完整下发，流中先行渲染标题与相似度）</blockquote>
                </div>
              </li>
            </ul>
          </div>
        </template>
      </article>
    </section>

    <p v-if="errorMessage" class="tutor-chat__error">{{ errorMessage }}</p>

    <!-- 提问输入区（导览步骤 2 锚点：向 AI 导师提问） -->
    <form class="tutor-chat__ask" data-tour="tutor-ask" @submit.prevent="ask">
      <input
        v-model="question"
        type="text"
        placeholder="问点什么？例如：ST-3200 支架的最大承载是多少"
        :disabled="asking"
        aria-label="向 AI 导师提问"
      />
      <button type="submit" :disabled="asking || question.trim().length === 0">
        {{ asking ? '回答生成中…' : '发送' }}
      </button>
    </form>
  </div>
</template>
