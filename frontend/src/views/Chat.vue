<template>
  <div class="chat-page">
    <!-- 对话区 -->
    <el-scrollbar ref="listRef" class="chat-scroll">
      <div class="chat-list">
        <el-alert
          v-if="messages.length === 0"
          type="info"
          :closable="false"
          show-icon
          class="empty-tip"
          title="向知识库提问"
          description="系统将在您已上传的文档中检索相关内容，并由本地大模型依据资料作答；答案中的 [n] 角标对应下方来源卡片。未检索到相关资料时不会强行作答。"
        />

        <template v-for="msg in messages" :key="msg.id">
          <!-- 用户提问 -->
          <div v-if="msg.role === 'user'" class="msg-row msg-user">
            <div class="bubble bubble-user">{{ msg.content }}</div>
          </div>

          <!-- 助手回答 -->
          <div v-else class="msg-row msg-assistant">
            <el-avatar :size="32" class="assistant-avatar">
              <el-icon><Cpu /></el-icon>
            </el-avatar>
            <div class="bubble-wrap">
              <div class="bubble bubble-assistant">
                <span
                  v-for="(seg, i) in splitCites(msg.content)"
                  :key="i"
                >
                  <sup
                    v-if="seg.cite && hasSource(msg, seg.index)"
                    class="cite-mark"
                    :title="`查看来源 ${seg.index}`"
                    @click="highlightSource(msg.id, seg.index)"
                  >[{{ seg.index }}]</sup>
                  <template v-else>{{ seg.text }}</template>
                </span>
                <el-icon v-if="msg.streaming" class="typing-icon is-loading">
                  <Loading />
                </el-icon>
              </div>

              <el-alert
                v-if="!msg.streaming && !msg.error && msg.hit === false"
                type="info"
                :closable="false"
                show-icon
                class="hit-alert"
                title="知识库中未检索到相关内容，可尝试换一种问法或先上传相关文档"
              />
              <el-alert
                v-if="msg.error"
                type="error"
                :closable="false"
                show-icon
                class="hit-alert"
                :title="msg.error"
              />

              <!-- 来源卡片 -->
              <div
                v-if="msg.sources.length > 0"
                class="sources"
                :id="`sources-${msg.id}`"
              >
                <div class="sources-title">
                  <el-icon><CollectionTag /></el-icon>
                  参考来源（{{ msg.sources.length }}）
                </div>
                <div
                  v-for="source in msg.sources"
                  :key="source.index"
                  :id="`src-${msg.id}-${source.index}`"
                  class="source-card"
                >
                  <div class="source-index">{{ source.index }}</div>
                  <div class="source-body">
                    <div class="source-head">
                      <span class="source-name">{{ source.doc_name }}</span>
                      <el-tag size="small" effect="plain" class="source-loc">
                        {{ formatLocation(source.page, source.section) }}
                      </el-tag>
                      <span class="source-score">
                        相关度 {{ formatScore(source.score) }}
                      </span>
                    </div>
                    <div class="source-snippet">{{ source.snippet }}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-scrollbar>

    <!-- 输入区 -->
    <el-card shadow="never" class="composer">
      <div class="composer-row">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          maxlength="2000"
          resize="none"
          show-word-limit
          placeholder="请输入问题，Enter 发送，Shift + Enter 换行"
          :disabled="streaming"
          @keydown.enter.exact.prevent="onSend"
        />
        <el-button
          type="primary"
          class="send-btn"
          :loading="streaming"
          :disabled="!question.trim()"
          @click="onSend"
        >
          <el-icon v-if="!streaming"><Promotion /></el-icon>
          发送
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onUnmounted, reactive, ref } from 'vue'
import {
  CollectionTag,
  Cpu,
  Loading,
  Promotion,
} from '@element-plus/icons-vue'
import { getToken } from '../api/client'
import type { SourceItem } from '../api/types'
import { formatLocation, formatScore } from '../utils/format'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  hit: boolean
  streaming: boolean
  error: string | null
}

interface Segment {
  text: string
  cite: boolean
  index: number
}

const CITE_PATTERN = /\[(\d+)\]/g

const messages = ref<ChatMessage[]>([])
const question = ref('')
const streaming = ref(false)
const listRef = ref()
let eventSource: EventSource | null = null
let seq = 0

function nextId(prefix: string) {
  seq += 1
  return `${prefix}-${Date.now()}-${seq}`
}

/** 将答案文本拆分为普通片段与 [n] 引用标记。 */
function splitCites(text: string): Segment[] {
  const segments: Segment[] = []
  let lastIndex = 0
  for (const match of text.matchAll(CITE_PATTERN)) {
    if (match.index === undefined) continue
    if (match.index > lastIndex) {
      segments.push({
        text: text.slice(lastIndex, match.index),
        cite: false,
        index: 0,
      })
    }
    segments.push({ text: match[0], cite: true, index: Number(match[1]) })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) {
    segments.push({ text: text.slice(lastIndex), cite: false, index: 0 })
  }
  return segments
}

function hasSource(msg: ChatMessage, index: number): boolean {
  return msg.sources.some((s) => s.index === index)
}

async function scrollToBottom() {
  await nextTick()
  listRef.value?.setScrollTop(Number.MAX_SAFE_INTEGER)
}

function highlightSource(messageId: string, index: number) {
  const el = document.getElementById(`src-${messageId}-${index}`)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  el.classList.add('source-highlight')
  window.setTimeout(() => el.classList.remove('source-highlight'), 1200)
}

function finishStream() {
  streaming.value = false
  eventSource?.close()
  eventSource = null
  scrollToBottom()
}

function onSend() {
  const q = question.value.trim()
  if (!q || streaming.value) return

  const userMsg: ChatMessage = {
    id: nextId('u'),
    role: 'user',
    content: q,
    sources: [],
    hit: true,
    streaming: false,
    error: null,
  }
  // 必须用 reactive 包裹：对象 push 进响应式数组后，数组内是它的 proxy，
  // 若回调里仍持有普通原对象，累加 content 不会触发视图更新（流式无逐字、done 后不渲染）
  const assistantMsg = reactive<ChatMessage>({
    id: nextId('a'),
    role: 'assistant',
    content: '',
    sources: [],
    hit: true,
    streaming: true,
    error: null,
  })
  messages.value.push(userMsg, assistantMsg)
  question.value = ''
  streaming.value = true
  scrollToBottom()

  // EventSource 不能自定义请求头，按后端约定通过 ?token= 鉴权
  const url =
    `/api/chat/stream?question=${encodeURIComponent(q)}` +
    `&token=${encodeURIComponent(getToken())}`
  eventSource = new EventSource(url)

  eventSource.onmessage = (event: MessageEvent<string>) => {
    let payload: { type: string; data?: string; sources?: SourceItem[]; hit?: boolean; message?: string }
    try {
      payload = JSON.parse(event.data)
    } catch {
      return
    }

    if (payload.type === 'token' && typeof payload.data === 'string') {
      assistantMsg.content += payload.data
      scrollToBottom()
    } else if (payload.type === 'done') {
      assistantMsg.sources = payload.sources ?? []
      assistantMsg.hit = payload.hit ?? true
      assistantMsg.streaming = false
      finishStream()
    } else if (payload.type === 'error') {
      assistantMsg.streaming = false
      assistantMsg.error = payload.message || '生成失败，请稍后重试'
      finishStream()
    }
  }

  eventSource.onerror = () => {
    if (!streaming.value) return
    assistantMsg.streaming = false
    assistantMsg.error =
      assistantMsg.content.length > 0
        ? '连接中断，以下为已生成的部分内容'
        : '无法连接问答服务，请确认服务状态后重试'
    finishStream()
  }
}

onUnmounted(() => {
  eventSource?.close()
  eventSource = null
})
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 60px - 48px);
  gap: 14px;
}

.chat-scroll {
  flex: 1;
  min-height: 0;
}

.chat-list {
  max-width: 900px;
  margin: 0 auto;
  padding: 4px 4px 12px;
}

.empty-tip {
  margin: 8px 0;
}

.msg-row {
  display: flex;
  margin-bottom: 18px;
}

.msg-user {
  justify-content: flex-end;
}

.bubble {
  max-width: 78%;
  padding: 11px 14px;
  border-radius: 10px;
  font-size: 14.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble-user {
  background: var(--rag-primary);
  color: #fff;
  border-top-right-radius: 2px;
}

.msg-assistant {
  align-items: flex-start;
  gap: 10px;
}

.assistant-avatar {
  background: var(--rag-primary-light);
  color: var(--rag-primary);
  flex-shrink: 0;
  margin-top: 2px;
}

.bubble-wrap {
  max-width: 78%;
  min-width: 0;
}

.bubble-assistant {
  background: #fff;
  border: 1px solid var(--rag-border);
  border-top-left-radius: 2px;
}

.typing-icon {
  margin-left: 4px;
  color: var(--rag-text-sub);
  vertical-align: -2px;
}

.hit-alert {
  margin-top: 8px;
}

.sources {
  margin-top: 10px;
}

.sources-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  font-weight: 600;
  color: var(--rag-text-sub);
  margin-bottom: 8px;
}

.source-card {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: #fff;
  border: 1px solid var(--rag-border);
  border-radius: 8px;
  transition: box-shadow 0.2s, border-color 0.2s;
}

.source-index {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: var(--rag-primary);
  background: var(--rag-primary-light);
  border-radius: 50%;
}

.source-body {
  min-width: 0;
}

.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.source-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--rag-text-main);
}

.source-loc {
  font-weight: normal;
}

.source-score {
  margin-left: auto;
  font-size: 12px;
  color: var(--rag-text-sub);
}

.source-snippet {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--rag-text-sub);
  white-space: pre-wrap;
}

.composer {
  flex-shrink: 0;
}

.composer :deep(.el-card__body) {
  padding: 14px 16px;
}

.composer-row {
  display: flex;
  gap: 12px;
  align-items: stretch;
  max-width: 900px;
  margin: 0 auto;
}

.send-btn {
  height: auto;
  min-width: 92px;
}

@media (max-width: 640px) {
  .bubble,
  .bubble-wrap {
    max-width: 88%;
  }
}
</style>
