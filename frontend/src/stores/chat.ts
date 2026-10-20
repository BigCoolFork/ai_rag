/**
 * 会话与消息状态管理。
 *
 * - 会话列表与当前会话消息从后端加载；
 * - 流式过程中的临时消息保存在内存（pendingMessages），流式结束后重新从后端拉取；
 * - 新建、切换、删除会话均通过后端 API。
 */

import { defineStore } from 'pinia'
import { chatApi } from '../api/client'
import type { ChatMessage, Conversation } from '../api/types'
import type { SourceItem } from '../api/types'

interface LocalMessage {
  /** 临时 id（流式中），入库后由后端 id 替代 */
  tempId?: string
  id?: number
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  hit: boolean
  error: string | null
  streaming: boolean
  stopped?: boolean
}

interface ChatState {
  conversations: Conversation[]
  currentId: number | null
  /** 已入库消息（后端返回） */
  messages: ChatMessage[]
  /** 流式中尚未入库的消息 */
  pendingMessages: LocalMessage[]
  loading: boolean
}

let tempSeq = 0

export const useChatStore = defineStore('chat', {
  state: (): ChatState => ({
    conversations: [],
    currentId: null,
    messages: [],
    pendingMessages: [],
    loading: false,
  }),

  getters: {
    currentConversation(state): Conversation | null {
      return state.conversations.find((c) => c.id === state.currentId) ?? null
    },
    /** 合并已入库消息与流式中的临时消息，供视图渲染 */
    displayMessages(state): LocalMessage[] {
      const persisted: LocalMessage[] = state.messages.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        sources: m.sources,
        hit: m.hit,
        error: m.error,
        streaming: false,
      }))
      return [...persisted, ...state.pendingMessages]
    },
  },

  actions: {
    async fetchConversations() {
      this.loading = true
      try {
        this.conversations = await chatApi.listConversations()
      } finally {
        this.loading = false
      }
    },

    async selectConversation(id: number) {
      this.currentId = id
      this.pendingMessages = []
      this.messages = await chatApi.listMessages(id)
    },

    async createConversation(title?: string): Promise<number> {
      const conv = await chatApi.createConversation(title)
      this.conversations.unshift(conv)
      this.currentId = conv.id
      this.messages = []
      this.pendingMessages = []
      return conv.id
    },

    async renameConversation(id: number, title: string) {
      const conv = await chatApi.renameConversation(id, title)
      const idx = this.conversations.findIndex((c) => c.id === id)
      if (idx >= 0) {
        this.conversations[idx] = conv
      }
    },

    async deleteConversation(id: number) {
      await chatApi.deleteConversation(id)
      this.conversations = this.conversations.filter((c) => c.id !== id)
      if (this.currentId === id) {
        if (this.conversations.length > 0) {
          await this.selectConversation(this.conversations[0].id)
        } else {
          this.currentId = null
          this.messages = []
          this.pendingMessages = []
        }
      }
    },

    /** 流式开始前添加用户提问与空助手消息到 pending */
    startStreaming(userContent: string): { userTempId: string; assistantTempId: string } {
      const userTempId = `u-${Date.now()}-${++tempSeq}`
      const assistantTempId = `a-${Date.now()}-${++tempSeq}`
      this.pendingMessages.push(
        { tempId: userTempId, role: 'user', content: userContent, sources: [], hit: true, error: null, streaming: false },
        { tempId: assistantTempId, role: 'assistant', content: '', sources: [], hit: true, error: null, streaming: true },
      )
      return { userTempId, assistantTempId }
    },

    appendToken(tempId: string, piece: string) {
      const msg = this.pendingMessages.find((m) => m.tempId === tempId)
      if (msg) msg.content += piece
    },

    finishStreaming(tempId: string, sources: SourceItem[], hit: boolean, error: string | null) {
      const msg = this.pendingMessages.find((m) => m.tempId === tempId)
      if (msg) {
        msg.streaming = false
        msg.sources = sources
        msg.hit = hit
        msg.error = error
        msg.stopped = !!error && error.includes('停止')
      }
    },

    /** 流式结束后从后端重新拉取消息，保证与持久化一致 */
    async refreshMessages() {
      if (this.currentId != null) {
        this.messages = await chatApi.listMessages(this.currentId)
        this.pendingMessages = []
        // 同步会话列表（标题/时间可能已更新）
        this.conversations = await chatApi.listConversations()
      }
    },
  },
})
