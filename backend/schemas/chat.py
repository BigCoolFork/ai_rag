"""问答相关 Pydantic 模型。

- ChatRequest：用户提问（长度受 1..2000 限制）；
- SourceItem：单条引用来源（编号、文档、定位、片段、相似度）；
- ChatResponse：同步问答响应（答案 + 来源列表 + 是否命中文库）；
- SSE 事件：token 增量事件 / done 结束事件（携带 sources）/ error 事件。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 空命中时统一返回的答案，前端可据此展示"未命中"引导
NO_HIT_ANSWER = (
    "知识库中未检索到与该问题相关的内容，无法根据现有资料回答。"
    "请尝试更换提问方式，或先上传相关文档。"
)


class ChatRequest(BaseModel):
    """同步问答请求。"""

    question: str = Field(..., min_length=1, max_length=2000, description="用户提问")


class SourceItem(BaseModel):
    """答案引用来源（与上下文中的 [n] 编号对应）。"""

    index: int = Field(ge=1, description="引用编号，对应答案中的 [n]")
    doc_id: str
    doc_name: str
    file_type: str  # pdf / docx / xlsx
    page: int | None = None       # PDF 真实页码；DOCX/XLSX 为 None
    section: str | None = None    # DOCX 标题 / XLSX 工作表名
    snippet: str = Field("", description="命中片段原文（截断展示）")
    score: float = Field(..., ge=0.0, le=1.0, description="COSINE 相似度")
    chunk_id: int | None = Field(None, description="向量库主键，用于文档预览定位")


class ChatResponse(BaseModel):
    """同步问答响应。"""

    answer: str
    sources: list[SourceItem] = Field(default_factory=list)
    hit: bool = Field(..., description="是否在知识库中检索到相关片段")


# ---------------------------------------------------------------------------
# SSE 事件（data 字段的 JSON 结构，路由层统一序列化为 data: {...}\\n\\n）
# ---------------------------------------------------------------------------

class SSETokenEvent(BaseModel):
    """流式增量文本。"""

    type: Literal["token"] = "token"
    data: str


class SSEDoneEvent(BaseModel):
    """流结束：携带与同步接口一致的 sources 与 hit 标记。"""

    type: Literal["done"] = "done"
    sources: list[SourceItem] = Field(default_factory=list)
    hit: bool = True


class SSEErrorEvent(BaseModel):
    """生成过程中的错误（检索阶段错误在建立 SSE 连接前以 HTTP 状态码返回）。"""

    type: Literal["error"] = "error"
    code: str
    message: str


def sse_format(payload: dict[str, Any]) -> str:
    """把事件字典序列化为 SSE 帧（单条 data 行 + 空行）。"""
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 会话与消息（持久化到 SQLite）
# ---------------------------------------------------------------------------

class ConversationCreate(BaseModel):
    """创建会话请求。"""

    title: str = Field("新对话", max_length=100, description="会话标题，默认「新对话」")


class ConversationOut(BaseModel):
    """会话列表项。"""

    id: int
    title: str
    created_at: str
    updated_at: str


class ConversationRename(BaseModel):
    """重命名会话。"""

    title: str = Field(..., min_length=1, max_length=100)


class MessageOut(BaseModel):
    """单条消息（从 DB 读出后返回前端）。"""

    id: int
    conversation_id: int
    role: Literal["user", "assistant"]
    content: str
    sources: list[SourceItem] = Field(default_factory=list)
    hit: bool = True
    error: str | None = None
    created_at: str
