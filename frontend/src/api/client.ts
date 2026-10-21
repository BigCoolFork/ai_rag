/**
 * HTTP 客户端与接口封装。
 *
 * - axios 实例 baseURL=/api（开发期由 Vite 代理到 8000，生产同源由 FastAPI 托管）；
 * - 请求拦截器自动注入 Bearer JWT；
 * - 响应拦截器统一解包 data，401 时清除会话并跳转登录；
 * - 后端结构化错误 {detail:{code,message}} / FastAPI 422 校验数组 /
 *   纯字符串 detail 统一转换为带 code/message 的 ApiError 抛出。
 */

import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
} from 'axios'
import type {
  DocumentChunksResp,
  DocumentListResponse,
  DocumentUploadResp,
  ChatResponse,
  Conversation,
  ChatMessage,
  LoginPayload,
  TokenResponse,
  UserInfo,
} from './types'

const TOKEN_KEY = 'rag_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/** 统一业务错误：保留后端错误码，便于界面分支处理。 */
export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

function extractError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0
  const detail = (error.response?.data as { detail?: unknown })?.detail

  // 结构化错误：{"detail": {"code": "...", "message": "..."}}
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const d = detail as { code?: string; message?: string }
    return new ApiError(
      d.code ?? 'UNKNOWN',
      d.message ?? '请求失败，请稍后重试',
      status,
    )
  }

  // FastAPI 参数校验：detail 为数组
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string }
    return new ApiError('VALIDATION_ERROR', first.msg ?? '请求参数有误', status)
  }

  // 纯文本 detail（如 401 未登录提示）
  if (typeof detail === 'string' && detail) {
    return new ApiError(status === 401 ? 'UNAUTHORIZED' : 'ERROR', detail, status)
  }

  if (status === 0) {
    return new ApiError('NETWORK_ERROR', '无法连接服务器，请检查服务是否启动', 0)
  }
  return new ApiError('UNKNOWN', `请求失败（HTTP ${status}）`, status)
}

const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 180_000, // 大文档入库可能较慢，与后端同步入库模式匹配
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    const apiError = extractError(error)
    // 401：会话失效，清理并回到登录页（登录页自身的 401 不强制跳转）
    if (apiError.code === 'UNAUTHORIZED' && !window.location.pathname.startsWith('/login')) {
      clearToken()
      window.location.assign('/login')
    }
    return Promise.reject(apiError)
  },
)

// ---------------------------------------------------------------------------
// 接口封装
// ---------------------------------------------------------------------------

export const authApi = {
  register(payload: LoginPayload): Promise<{ id: number; username: string }> {
    return http.post('/auth/register', payload).then((r) => r.data)
  },
  login(payload: LoginPayload): Promise<TokenResponse> {
    return http.post('/auth/login', payload).then((r) => r.data)
  },
  me(): Promise<UserInfo> {
    return http.get('/auth/me').then((r) => r.data)
  },
}

export const documentApi = {
  list(page: number, pageSize: number): Promise<DocumentListResponse> {
    return http
      .get('/documents/list', { params: { page, page_size: pageSize } })
      .then((r) => r.data)
  },
  upload(file: File, onUploadProgress?: AxiosRequestConfig['onUploadProgress']) {
    const form = new FormData()
    form.append('file', file)
    return http
      .post<DocumentUploadResp>('/documents/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress,
      })
      .then((r) => r.data)
  },
  remove(docId: number): Promise<{ id: number; deleted: boolean; chunks_removed: number }> {
    return http.delete(`/documents/${docId}`).then((r) => r.data)
  },
  getChunks(docId: number): Promise<DocumentChunksResp> {
    return http.get(`/documents/${docId}/chunks`).then((r) => r.data)
  },
}

export const chatApi = {
  ask(question: string): Promise<ChatResponse> {
    return http.post('/chat/ask', { question }).then((r) => r.data)
  },
  listConversations(): Promise<Conversation[]> {
    return http.get('/chat/conversations').then((r) => r.data)
  },
  createConversation(title?: string): Promise<Conversation> {
    return http.post('/chat/conversations', { title: title ?? '新对话' }).then((r) => r.data)
  },
  listMessages(convId: number): Promise<ChatMessage[]> {
    return http.get(`/chat/conversations/${convId}/messages`).then((r) => r.data)
  },
  renameConversation(convId: number, title: string): Promise<Conversation> {
    return http.patch(`/chat/conversations/${convId}`, { title }).then((r) => r.data)
  },
  deleteConversation(convId: number): Promise<{ deleted: boolean }> {
    return http.delete(`/chat/conversations/${convId}`).then((r) => r.data)
  },
}
