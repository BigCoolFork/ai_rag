/**
 * 与后端 Pydantic 模型一一对应的接口类型。
 * 后端契约变更时同步修改本文件（backend/schemas/*.py）。
 */

// ---------------- 认证 ----------------

export interface LoginPayload {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

export interface UserInfo {
  id: number
  username: string
  created_at: string
}

// ---------------- 文档管理 ----------------

export type DocumentStatus = 'pending' | 'success' | 'failed'

export interface DocumentRecord {
  id: number
  user_id: string
  doc_sha256: string
  original_name: string
  stored_filename: string
  file_type: 'pdf' | 'docx' | 'xlsx' | string
  file_size: number
  status: DocumentStatus
  error_msg: string | null
  created_at: string
  total_chunks: number
  processed_chunks: number
  is_public: number
}

export interface DocumentUploadResp {
  duplicated: boolean
  chunk_count: number | null
  document: DocumentRecord
}

export interface DocumentListItem {
  id: number
  original_name: string
  file_type: string
  file_size: number
  status: DocumentStatus
  error_msg: string | null
  created_at: string
  total_chunks: number
  processed_chunks: number
  is_public: number
}

export interface DocumentListResponse {
  items: DocumentListItem[]
  total: number
  page: number
  page_size: number
}

export interface DocumentChunkItem {
  chunk_id: number
  text: string
  page: number | null
  section: string | null
}

export interface DocumentChunksResp {
  doc_id: number
  doc_name: string
  file_type: string
  chunks: DocumentChunkItem[]
}

// ---------------- 问答 ----------------

export interface SourceItem {
  index: number
  doc_id: string
  doc_name: string
  file_type: string
  page: number | null
  section: string | null
  snippet: string
  score: number
  chunk_id?: number | null
}

export interface ChatResponse {
  answer: string
  sources: SourceItem[]
  hit: boolean
}

// SSE 事件（data 帧）
export interface SSETokenEvent {
  type: 'token'
  data: string
}

export interface SSEDoneEvent {
  type: 'done'
  sources: SourceItem[]
  hit: boolean
}

export interface SSEErrorEvent {
  type: 'error'
  code: string
  message: string
}

export type SSEEvent = SSETokenEvent | SSEDoneEvent | SSEErrorEvent

// ---------------- 会话与消息 ----------------

export interface Conversation {
  id: number
  title: string
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  hit: boolean
  error: string | null
  created_at: string
}

// ---------------- 统一错误 ----------------

/** 后端结构化错误 detail：{ code, message } */
export interface ApiErrorDetail {
  code: string
  message: string
}
