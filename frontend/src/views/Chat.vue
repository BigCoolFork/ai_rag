<template>
  <div class="chat-page">
    <!-- 左侧会话列表 -->
    <div class="conv-panel">
      <el-button type="primary" class="new-conv-btn" @click="onNewConversation">
        <el-icon><Plus /></el-icon>
        新建对话
      </el-button>
      <el-scrollbar class="conv-scroll">
        <div
          v-for="conv in chatStore.conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: conv.id === chatStore.currentId }"
          @click="onSelectConversation(conv.id)"
        >
          <span class="conv-title">{{ conv.title }}</span>
          <div class="conv-actions">
            <el-button
              text
              class="conv-rename"
              title="重命名"
              @click.stop="onRenameConversation(conv)"
            >
              <el-icon><Edit /></el-icon>
            </el-button>
            <el-button
              text
              class="conv-del"
              title="删除"
              @click.stop="onDeleteConversation(conv.id)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <div v-if="chatStore.conversations.length === 0" class="conv-empty">
          暂无对话，点击上方新建
        </div>
      </el-scrollbar>
    </div>

    <!-- 右侧对话区 -->
    <div class="chat-main">
      <el-scrollbar ref="listRef" class="chat-scroll">
        <div class="chat-list">
          <el-alert
            v-if="displayMessages.length === 0"
            type="info"
            :closable="false"
            show-icon
            class="empty-tip"
            title="向知识库提问"
            description="系统将在您已上传的文档中检索相关内容，并由本地大模型依据资料作答；答案中的 [n] 角标对应下方来源卡片。未检索到相关资料时不会强行作答。"
          />

          <template v-for="msg in displayMessages" :key="msg.tempId ?? msg.id">
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
                      @click="highlightSource(msg.tempId ?? String(msg.id), seg.index)"
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
                  :id="`sources-${msg.tempId ?? msg.id}`"
                >
                  <div class="sources-title">
                    <el-icon><CollectionTag /></el-icon>
                    参考来源（{{ msg.sources.length }}）
                  </div>
                  <div
                    v-for="source in msg.sources"
                    :key="source.index"
                    :id="`src-${msg.tempId ?? msg.id}-${source.index}`"
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
            :type="streaming ? 'danger' : 'primary'"
            class="send-btn"
            :loading="streaming && !canStop"
            :disabled="!streaming && !question.trim()"
            @click="streaming ? onStop() : onSend()"
          >
            <el-icon v-if="streaming"><VideoPause /></el-icon>
            <el-icon v-else><Promotion /></el-icon>
            {{ streaming ? '停止' : '发送' }}
          </el-button>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import {
  CollectionTag,
  Cpu,
  Delete,
  Edit,
  Loading,
  Plus,
  Promotion,
  VideoPause,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getToken } from '../api/client'
import { useChatStore } from '../stores/chat'
import type { SourceItem } from '../api/types'
import { formatLocation, formatScore } from '../utils/format'

interface Segment {
  text: string
  cite: boolean
  index: number
}

const CITE_PATTERN = /\[(\d+)\]/g
const IDLE_TIMEOUT = 30_000 // 30s 无 token 输出自动停止

const chatStore = useChatStore()
const displayMessages = computed(() => chatStore.displayMessages)

const question = ref('')
const streaming = ref(false)
const canStop = ref(true)
const listRef = ref()
let eventSource: EventSource | null = null
let idleTimer: ReturnType<typeof setTimeout> | null = null

function splitCites(text: string): Segment[] {
  const segments: Segment[] = []
  let lastIndex = 0
  for (const match of text.matchAll(CITE_PATTERN)) {
    if (match.index === undefined) continue
    if (match.index > lastIndex) {
      segments.push({ text: text.slice(lastIndex, match.index), cite: false, index: 0 })
    }
    segments.push({ text: match[0], cite: true, index: Number(match[1]) })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) {
    segments.push({ text: text.slice(lastIndex), cite: false, index: 0 })
  }
  return segments
}

function hasSource(msg: { sources: SourceItem[] }, index: number): boolean {
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

function resetIdleTimer() {
  if (idleTimer) clearTimeout(idleTimer)
  idleTimer = setTimeout(() => {
    if (streaming.value) {
      ElMessage.warning('生成超时，已自动停止')
      onStop()
    }
  }, IDLE_TIMEOUT)
}

function clearIdleTimer() {
  if (idleTimer) {
    clearTimeout(idleTimer)
    idleTimer = null
  }
}

function finishStream() {
  streaming.value = false
  canStop.value = true
  clearIdleTimer()
  eventSource?.close()
  eventSource = null
  scrollToBottom()
}

function onStop() {
  if (!streaming.value) return
  canStop.value = false
  eventSource?.close()
  eventSource = null
  // 标记当前流式中的助手消息为停止状态
  const assistantMsg = chatStore.pendingMessages.find((m) => m.streaming)
  if (assistantMsg) {
    assistantMsg.streaming = false
    assistantMsg.error = '已停止生成'
    assistantMsg.stopped = true
  }
  finishStream()
  // 停止后重新从后端拉取（已生成内容已入库）
  chatStore.refreshMessages()
}

async function onSend() {
  const q = question.value.trim()
  if (!q || streaming.value) return

  // 若无当前会话，先创建
  if (chatStore.currentId == null) {
    await chatStore.createConversation()
  }
  const convId = chatStore.currentId!

  const { assistantTempId } = chatStore.startStreaming(q)
  question.value = ''
  streaming.value = true
  canStop.value = true
  scrollToBottom()

  const url =
    `/api/chat/stream?question=${encodeURIComponent(q)}` +
    `&conversation_id=${convId}` +
    `&token=${encodeURIComponent(getToken())}`
  eventSource = new EventSource(url)
  resetIdleTimer()

  eventSource.onmessage = (event: MessageEvent<string>) => {
    let payload: { type: string; data?: string; sources?: SourceItem[]; hit?: boolean; message?: string }
    try {
      payload = JSON.parse(event.data)
    } catch {
      return
    }

    if (payload.type === 'token' && typeof payload.data === 'string') {
      chatStore.appendToken(assistantTempId, payload.data)
      resetIdleTimer()
      scrollToBottom()
    } else if (payload.type === 'done') {
      chatStore.finishStreaming(assistantTempId, payload.sources ?? [], payload.hit ?? true, null)
      finishStream()
      chatStore.refreshMessages()
    } else if (payload.type === 'error') {
      chatStore.finishStreaming(assistantTempId, [], false, payload.message || '生成失败')
      finishStream()
      chatStore.refreshMessages()
    }
  }

  eventSource.onerror = () => {
    if (!streaming.value) return
    const assistantMsg = chatStore.pendingMessages.find((m) => m.tempId === assistantTempId)
    const hasContent = assistantMsg && assistantMsg.content.length > 0
    chatStore.finishStreaming(
      assistantTempId,
      [],
      false,
      hasContent ? '连接中断，以下为已生成的部分内容' : '无法连接问答服务，请确认服务状态后重试',
    )
    finishStream()
    chatStore.refreshMessages()
  }
}

async function onNewConversation() {
  if (streaming.value) {
    ElMessage.warning('请先停止当前生成')
    return
  }
  try {
    const { value } = await ElMessageBox.prompt('请输入对话名称', '新建对话', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputPlaceholder: '留空则默认「新对话」',
      inputValidator: (v: string) => {
        if (v && v.length > 100) return '名称不能超过 100 个字符'
        return true
      },
    })
    await chatStore.createConversation(value?.trim() || undefined)
  } catch {
    // 用户取消，不处理
  }
}

async function onRenameConversation(conv: { id: number; title: string }) {
  if (streaming.value) {
    ElMessage.warning('请先停止当前生成')
    return
  }
  try {
    const { value } = await ElMessageBox.prompt('修改对话名称', '重命名', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: conv.title,
      inputValidator: (v: string) => {
        if (!v || !v.trim()) return '名称不能为空'
        if (v.length > 100) return '名称不能超过 100 个字符'
        return true
      },
    })
    await chatStore.renameConversation(conv.id, value.trim())
  } catch {
    // 用户取消，不处理
  }
}

async function onSelectConversation(id: number) {
  if (streaming.value) {
    ElMessage.warning('请先停止当前生成')
    return
  }
  await chatStore.selectConversation(id)
  scrollToBottom()
}

async function onDeleteConversation(id: number) {
  try {
    await ElMessageBox.confirm('确认删除该对话？删除后不可恢复。', '删除对话', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await chatStore.deleteConversation(id)
}

onMounted(async () => {
  await chatStore.fetchConversations()
  // 默认选中最近的会话
  if (chatStore.conversations.length > 0) {
    await chatStore.selectConversation(chatStore.conversations[0].id)
  }
})

onUnmounted(() => {
  clearIdleTimer()
  eventSource?.close()
  eventSource = null
})
</script>

<style scoped>
.chat-page {
  display: flex;
  height: calc(100vh - 60px - 48px);
  gap: 0;
}

/* 左侧会话面板 */
.conv-panel {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--rag-border);
  background: #fafafa;
}

.new-conv-btn {
  margin: 12px;
}

.conv-scroll {
  flex: 1;
  min-height: 0;
}

.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  margin: 2px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}

.conv-item:hover {
  background: #eef2ff;
}

.conv-item.active {
  background: var(--rag-primary-light);
}

.conv-title {
  font-size: 13.5px;
  color: var(--rag-text-main);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.conv-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.15s;
  flex-shrink: 0;
}

.conv-item:hover .conv-actions {
  opacity: 1;
}

.conv-rename,
.conv-del {
  color: var(--rag-text-sub);
  padding: 2px;
}

.conv-rename:hover {
  color: var(--rag-primary);
}

.conv-del:hover {
  color: #f56c6c;
}

.conv-empty {
  padding: 24px 16px;
  text-align: center;
  color: var(--rag-text-sub);
  font-size: 13px;
}

/* 右侧对话区 */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
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
  .conv-panel {
    width: 160px;
  }
  .bubble,
  .bubble-wrap {
    max-width: 88%;
  }
}
</style>
