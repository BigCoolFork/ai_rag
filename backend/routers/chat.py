"""问答路由 + 健康检查。

- POST /chat/ask     非流式问答，返回 answer + sources（文档名+页码+片段）
- GET  /chat/stream  SSE 流式问答（EventSource 通过 ?token= 鉴权），
                     token/done/error 事件均为 data: {json} 帧，
                     done 事件携带 sources
- GET  /health       就绪探针

本模块挂载在 /api 前缀下，故路径内写全 /chat/... 与 /health。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from core.deps import get_current_user, get_current_user_sse
from database import get_connection
from schemas.chat import (
    ChatRequest,
    ChatResponse,
    ConversationCreate,
    ConversationOut,
    ConversationRename,
    MessageOut,
)
from services import conversation as conv_service
from services import ollama_client, qa, vector_store
from services.ollama_client import OllamaModelMissing, OllamaServiceUnavailable
from services.vector_store import VectorStoreError

router = APIRouter()


def _qa_error(exc: Exception) -> HTTPException:
    """问答链路业务异常 → 结构化 HTTP 错误。"""
    detail = {"code": "QA_ERROR", "message": str(exc)}
    if isinstance(exc, OllamaModelMissing):
        detail["code"] = "OLLAMA_MODEL_MISSING"
        return HTTPException(status_code=503, detail=detail)
    if isinstance(exc, OllamaServiceUnavailable):
        detail["code"] = "OLLAMA_UNAVAILABLE"
        return HTTPException(status_code=503, detail=detail)
    if isinstance(exc, VectorStoreError):
        detail["code"] = "VECTOR_STORE_ERROR"
        return HTTPException(status_code=503, detail=detail)
    if isinstance(exc, qa.QaGenerationError):
        detail["code"] = "LLM_GENERATION_ERROR"
        return HTTPException(status_code=503, detail=detail)
    detail["code"] = "QA_ERROR"
    return HTTPException(status_code=400, detail=detail)


@router.get("/health", tags=["健康检查"], summary="健康检查")
def health(response: Response) -> dict:
    """就绪探针：检查 SQLite、Milvus、Ollama 服务、Embedding 模型、LLM 模型。

    - 五项全部就绪：HTTP 200，status=ok；
    - 任意一项未就绪：HTTP 503，status=degraded，detail 给出可操作原因
      （如缺失模型时直接附带 ollama pull 命令，集合 schema 不兼容时提示迁移）。

    本接口为同步函数，FastAPI 会在线程池中执行，
    避免短超时 HTTP 探测阻塞事件循环。
    """
    checks: dict[str, dict] = {}

    # 1) SQLite：真实执行一次 SELECT 1
    sqlite_ready = False
    sqlite_detail = ""
    conn: sqlite3.Connection | None = None
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        sqlite_ready = True
        sqlite_detail = "SQLite 数据库可正常读写"
    except Exception as exc:  # noqa: BLE001 - 健康检查需把异常原因回传
        sqlite_detail = f"SQLite 不可用：{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
    checks["sqlite"] = {"ready": sqlite_ready, "detail": sqlite_detail}

    # 2) Ollama 服务 + 两个本地模型（探测函数内部已兜底，不抛异常）
    status = ollama_client.probe_ollama()
    checks["ollama"] = {
        "ready": status.ollama_ready,
        "detail": status.ollama_detail,
        "version": status.version,
        "base_url": status.base_url,
    }
    checks["embedding_model"] = (
        status.embedding.to_dict() if status.embedding
        else {"ready": False, "detail": "未探测"}
    )
    checks["llm_model"] = (
        status.llm.to_dict() if status.llm
        else {"ready": False, "detail": "未探测"}
    )

    # 3) Milvus Lite（探测函数内部已兜底，不抛异常）
    milvus_status = vector_store.probe_milvus()
    checks["milvus"] = milvus_status.to_dict()

    all_ready = (
        sqlite_ready
        and milvus_status.ready
        and status.ollama_ready
        and (status.embedding is not None and status.embedding.ready)
        and (status.llm is not None and status.llm.ready)
    )
    response.status_code = 200 if all_ready else 503

    return {
        "status": "ok" if all_ready else "degraded",
        "installed_models": status.installed_models,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 问答：非流式 / SSE 流式（检索与上下文构造逻辑只写一份，见 services/qa.py）
# ---------------------------------------------------------------------------

@router.post(
    "/chat/ask",
    response_model=ChatResponse,
    tags=["问答"],
    summary="非流式知识库问答",
)
def ask(
    payload: ChatRequest,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> ChatResponse:
    """同步返回答案与引用来源。

    - 空命中（无片段达到相似度阈值）：200，hit=false，answer 为固定引导话术，
      服务端不会调用 LLM；
    - Ollama / Milvus 不可用：503，detail 为 {code, message} 结构化错误。

    定义为同步函数，FastAPI 自动放到线程池执行，避免阻塞事件循环。
    """
    try:
        return qa.build_answer(int(current_user["id"]), payload.question)
    except Exception as exc:  # noqa: BLE001 - 统一映射为结构化错误
        raise _qa_error(exc) from exc


@router.get(
    "/chat/stream",
    tags=["问答"],
    summary="SSE 流式知识库问答（EventSource，?token= 鉴权）",
)
def stream(
    question: str = Query(
        ..., min_length=1, max_length=2000, description="用户提问"
    ),
    conversation_id: int | None = Query(
        None, description="会话 id；为空时自动创建新会话"
    ),
    current_user: sqlite3.Row = Depends(get_current_user_sse),
) -> StreamingResponse:
    """以 ``text/event-stream`` 返回 token / done / error 事件。

    鉴权支持 Authorization: Bearer 头或 ``?token=<JWT>`` 查询参数
    （浏览器 EventSource 不能自定义请求头）。检索/生成阶段错误通过
    error 事件回传，HTTP 层仅在鉴权失败或参数非法时返回 401/422。
    """
    user_id = int(current_user["id"])
    return StreamingResponse(
        qa.stream_answer(user_id, question, conversation_id=conversation_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲，保证逐 token 推送
        },
    )


# ---------------------------------------------------------------------------
# 会话与消息管理
# ---------------------------------------------------------------------------

@router.post(
    "/chat/conversations",
    response_model=ConversationOut,
    tags=["会话"],
    summary="创建会话",
)
def create_conversation(
    payload: ConversationCreate,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> ConversationOut:
    user_id = int(current_user["id"])
    conv_id = conv_service.create_conversation(user_id, payload.title)
    conv = conv_service.get_conversation(user_id, conv_id)
    assert conv is not None
    return ConversationOut(**conv)


@router.get(
    "/chat/conversations",
    response_model=list[ConversationOut],
    tags=["会话"],
    summary="列出当前用户的会话",
)
def list_conversations(
    current_user: sqlite3.Row = Depends(get_current_user),
) -> list[ConversationOut]:
    user_id = int(current_user["id"])
    return [ConversationOut(**c) for c in conv_service.list_conversations(user_id)]


@router.get(
    "/chat/conversations/{conv_id}/messages",
    response_model=list[MessageOut],
    tags=["会话"],
    summary="获取某会话的消息列表",
)
def list_messages(
    conv_id: int,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> list[MessageOut]:
    user_id = int(current_user["id"])
    conv = conv_service.get_conversation(user_id, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv_service.list_messages(user_id, conv_id)


@router.patch(
    "/chat/conversations/{conv_id}",
    response_model=ConversationOut,
    tags=["会话"],
    summary="重命名会话",
)
def rename_conversation(
    conv_id: int,
    payload: ConversationRename,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> ConversationOut:
    user_id = int(current_user["id"])
    ok = conv_service.rename_conversation(user_id, conv_id, payload.title)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    conv = conv_service.get_conversation(user_id, conv_id)
    assert conv is not None
    return ConversationOut(**conv)


@router.delete(
    "/chat/conversations/{conv_id}",
    tags=["会话"],
    summary="删除会话（级联删除消息）",
)
def delete_conversation(
    conv_id: int,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> dict:
    user_id = int(current_user["id"])
    ok = conv_service.delete_conversation(user_id, conv_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}
