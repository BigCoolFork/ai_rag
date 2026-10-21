"""问答链路服务（任务7）。

统一编排（同步 / 流式共用同一套检索与上下文构造逻辑）：
1. 在当前用户的 Milvus collection 内做 TopK 向量检索；
2. 按 COSINE 相似度阈值（settings.retrieve_score_threshold，默认 0.4）
   过滤低相关片段——全部低于阈值时视为空命中；
3. 空命中直接返回固定话术，**不调用 LLM**，节省算力并杜绝无依据生成；
4. 命中时把片段编号为 [n] 组装进上下文，系统提示词强制
   「只依据资料回答 / 不足明说不知道 / 结论标注 [n]」；
5. 通过任务3 的 get_llm() 单例调用本地 Ollama LLM；
6. sources 回传文档名、PDF 页码 / Word 章节 / Excel 工作表、片段与分数。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from config import settings
from schemas.chat import (
    NO_HIT_ANSWER,
    ChatResponse,
    SourceItem,
)
from services.ollama_client import get_llm
from services.vector_store import SearchResult, vector_search

logger = logging.getLogger(__name__)

# 来源片段在上下文中与响应 snippet 中的最大展示长度（字符）
SNIPPET_LIMIT = 150

_SYSTEM_PROMPT = (
    "你是一个严谨的私有化知识库问答助手，只能依据用户提供的<参考资料>回答问题。"
    "必须严格遵守以下规则：\n"
    "1. 只能使用<参考资料>中明确记载的信息作答，禁止引用资料以外的常识、推测或编造；\n"
    "2. 如果<参考资料>不足以回答问题，必须直接回复「根据现有资料无法回答该问题」，"
    "不要给出任何猜测性内容；\n"
    "3. 回答中引用资料的结论句末尾必须标注对应来源编号，格式如 [1]、[1][2]；\n"
    "4. 使用简体中文作答，条理清晰，直接回答问题，不输出与问题无关的内容，"
    "不要复述这些规则；\n"
    "5. 涉及数值汇总（求和、统计、计数等）时，直接给出最终结果数字，"
    "严禁输出「具体计算如下」「计算过程」「展开如下」等引导语，"
    "严禁展开逐项累加、列举数字序列或中间步骤。"
)


# ---------------------------------------------------------------------------
# 业务异常
# ---------------------------------------------------------------------------

class QaError(Exception):
    """问答链路业务异常基类。"""


class QaGenerationError(QaError):
    """LLM 生成答案失败（服务/模型就绪类异常直接复用任务3 定义）。"""


# ---------------------------------------------------------------------------
# 检索与上下文构造
# ---------------------------------------------------------------------------

@dataclass
class RetrievedContext:
    """阈值过滤后的检索结果及其编号化上下文。"""

    sources: list[SourceItem]
    context: str

    @property
    def hit(self) -> bool:
        return bool(self.sources)


def location_label(hit: SearchResult) -> str:
    """生成人类可读的片段定位：PDF 用真实页码，其余用章节/工作表名。"""
    if hit.file_type == "pdf":
        if hit.page is not None:
            return f"第 {hit.page} 页"
        return "页码未知"
    if hit.section:
        return f"章节/工作表：{hit.section}"
    return "位置未知"


def _to_source(index: int, hit: SearchResult) -> SourceItem:
    snippet = hit.text.strip()
    if len(snippet) > SNIPPET_LIMIT:
        snippet = snippet[:SNIPPET_LIMIT] + "…"
    return SourceItem(
        index=index,
        doc_id=hit.doc_id,
        doc_name=hit.doc_name,
        file_type=hit.file_type,
        page=hit.page,
        section=hit.section,
        snippet=snippet,
        score=round(hit.score, 6),
        chunk_id=hit.chunk_id,
    )


def retrieve_context(
    user_id: int,
    question: str,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> RetrievedContext:
    """检索并过滤相关片段，构造编号化上下文。

    向量服务不可用 / 嵌入失败时直接透出任务5 的 VectorStoreError 子类，
    由路由层统一映射为 503。
    """
    top_k = settings.retrieve_top_k if top_k is None else top_k
    threshold = (
        settings.retrieve_score_threshold
        if score_threshold is None
        else score_threshold
    )

    hits = vector_search(user_id, question, top_k=top_k)
    qualified = [hit for hit in hits if hit.score >= threshold]

    sources = [_to_source(i, hit) for i, hit in enumerate(qualified, start=1)]
    blocks: list[str] = []
    for source, hit in zip(sources, qualified):
        blocks.append(
            f"[{source.index}] 来源：《{source.doc_name}》（{location_label(hit)}）\n"
            f"{hit.text.strip()}"
        )
    return RetrievedContext(sources=sources, context="\n\n".join(blocks))


def build_prompt(question: str, context: str) -> str:
    """组装发给 OllamaLLM 的完整提示词（单字符串形式）。"""
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"<参考资料>\n{context}\n</参考资料>\n\n"
        f"问题：{question.strip()}\n"
        f"回答："
    )


# ---------------------------------------------------------------------------
# 同步问答
# ---------------------------------------------------------------------------

def build_answer(user_id: int, question: str) -> ChatResponse:
    """非流式问答：检索 → （空命中直接返回）→ LLM 生成。

    :raises VectorStoreError / EmbeddingError: 检索或嵌入失败
    :raises OllamaServiceUnavailable / OllamaModelMissing: 模型未就绪
    :raises QaGenerationError: LLM 生成过程出错
    """
    question = (question or "").strip()
    if not question:
        raise QaError("问题不能为空。")

    retrieved = retrieve_context(user_id, question)
    if not retrieved.hit:
        logger.info("用户 %s 的问题未命中任何片段（top_k=%d, 阈值=%.2f），不调用 LLM",
                    user_id, settings.retrieve_top_k,
                    settings.retrieve_score_threshold)
        return ChatResponse(answer=NO_HIT_ANSWER, sources=[], hit=False)

    prompt = build_prompt(question, retrieved.context)
    llm = get_llm()
    try:
        answer = str(llm.invoke(prompt)).strip()
    except QaError:
        raise
    except Exception as exc:  # noqa: BLE001 - OllamaClientError 之外的生成异常包装
        # 任务3 定义的可读异常直接向上抛，交给路由映射
        from services.ollama_client import OllamaClientError

        if isinstance(exc, OllamaClientError):
            raise
        raise QaGenerationError(
            f"本地大模型生成答案失败：{type(exc).__name__}: {exc}"
        ) from exc

    if not answer:
        raise QaGenerationError("本地大模型返回了空答案，请稍后重试。")
    return ChatResponse(answer=answer, sources=retrieved.sources, hit=True)


# ---------------------------------------------------------------------------
# SSE 流式问答
# ---------------------------------------------------------------------------

async def stream_answer(
    user_id: int,
    question: str,
    conversation_id: int | None = None,
) -> AsyncIterator[str]:
    """生成 SSE 帧的异步生成器（每帧格式 ``data: {json}\\n\\n``）。

    事件序列：
    - 若干 ``token`` 事件：答案增量文本；
    - 一个 ``done`` 事件：携带 sources 与 hit；
    - 生成中途异常：一个 ``error`` 事件后结束。

    若提供 ``conversation_id``，回答结束后将 user + assistant 消息入库；
    未提供时自动创建新会话再存储。每用户消息条数超限时自动清理最旧消息。
    """
    import asyncio
    import json

    from services import conversation as conv_service

    def _frame(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    question = (question or "").strip()

    # 确定会话 id：未提供则自动创建
    conv_id = conversation_id
    if conv_id is None:
        conv_id = conv_service.create_conversation(user_id)
    # 若会话标题为默认值，用首条提问命名
    conv_service.touch_conversation_title(user_id, conv_id, question)

    # 检索阶段
    try:
        retrieved = retrieve_context(user_id, question)
    except Exception as exc:  # noqa: BLE001 - 转为 error 事件，不中断连接
        logger.warning("流式问答检索失败：%s", exc)
        yield _frame({"type": "error", "code": "RETRIEVE_ERROR", "message": str(exc)})
        # 保存用户提问（无回答）
        conv_service.save_message(user_id, conv_id, "user", question)
        return

    if not retrieved.hit:
        yield _frame({"type": "token", "data": NO_HIT_ANSWER})
        yield _frame({"type": "done", "sources": [], "hit": False})
        conv_service.save_message(user_id, conv_id, "user", question)
        conv_service.save_message(
            user_id, conv_id, "assistant", NO_HIT_ANSWER,
            sources=[], hit=False,
        )
        return

    prompt = build_prompt(question, retrieved.context)
    sources_payload = [source.model_dump() for source in retrieved.sources]

    # 通知前端：检索完成，开始生成（用于重置前端空闲超时计时器）
    yield _frame({"type": "start", "sources": sources_payload, "hit": True})

    answer_buf: list[str] = []
    error_msg: str | None = None
    try:
        llm = get_llm()
        # num_predict 限制最大生成 token 数，防止模型陷入重复循环无限输出
        stream_iter = llm.stream(prompt)
        # llm.stream 是阻塞生成器，用 iterate_in_threadpool 放到线程池执行；
        # 每个 chunk 等待加空闲超时，避免模型卡死时前端一直等待。
        loop = asyncio.get_running_loop()
        timeout = settings.stream_idle_timeout

        async def _next_chunk() -> Any:
            return await loop.run_in_executor(None, next, stream_iter, None)

        while True:
            try:
                chunk = await asyncio.wait_for(_next_chunk(), timeout=timeout)
            except asyncio.TimeoutError:
                error_msg = f"生成超时（{timeout}s 无输出），已停止"
                logger.warning("流式问答空闲超时")
                break
            if chunk is None:
                break
            piece = str(chunk)
            if piece:
                answer_buf.append(piece)
                yield _frame({"type": "token", "data": piece})
    except Exception as exc:  # noqa: BLE001
        error_msg = f"本地大模型生成失败：{type(exc).__name__}: {exc}"
        logger.warning("流式问答生成失败：%s", exc)
        yield _frame({"type": "error", "code": "GENERATION_ERROR", "message": error_msg})

    # 保存消息（无论成功、超时还是出错，已生成内容都入库）
    conv_service.save_message(user_id, conv_id, "user", question)
    conv_service.save_message(
        user_id, conv_id, "assistant",
        content="".join(answer_buf) or (error_msg or ""),
        sources=retrieved.sources if not error_msg else [],
        hit=not error_msg,
        error=error_msg,
    )

    if not error_msg:
        yield _frame({"type": "done", "sources": sources_payload, "hit": True})
