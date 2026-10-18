"""问答链路单元测试（任务7）。

测试策略：完全不调用真实 Ollama / Milvus：
- ``qa.vector_search`` 替换为返回内存 SearchResult 的桩；
- ``qa.get_llm`` 替换为 FakeLLM（记录 prompt，模拟 invoke / stream）；
- 路由层走真实 FastAPI TestClient，SSE 用 client.stream 逐帧解析。

在 backend/ 目录下运行：
    python -m pytest tests/test_qa.py -v
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from schemas.chat import NO_HIT_ANSWER
from services import qa
from services.ollama_client import (
    OllamaModelMissing,
    OllamaServiceUnavailable,
)
from services.vector_store import SearchResult, VectorStoreError


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeLLM:
    """记录提示词、模拟同步/流式生成，支持故障注入。"""

    def __init__(
        self,
        answer: str = "根据资料，密码策略要求每 90 天更换。[1]",
        chunks: list[str] | None = None,
        invoke_error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.chunks = chunks or ["根据资料，", "密码每 90 天更换。", "[1]"]
        self.invoke_error = invoke_error
        self.stream_error = stream_error
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.invoke_error:
            raise self.invoke_error
        return self.answer

    def stream(self, prompt: str):  # noqa: ANN201 - 模拟阻塞生成器
        self.prompts.append(prompt)
        if self.stream_error:
            raise self.stream_error
        yield from self.chunks


def _hit(
    text: str,
    score: float,
    *,
    doc_id: str = "10",
    doc_name: str = "信息安全制度.pdf",
    file_type: str = "pdf",
    page: int | None = 3,
    section: str | None = None,
) -> SearchResult:
    return SearchResult(
        text=text, score=score, doc_id=doc_id, doc_name=doc_name,
        source=f"/data/uploads/{doc_name}", file_type=file_type,
        page=page, section=section,
    )


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM()
    monkeypatch.setattr(qa, "get_llm", lambda: llm)
    return llm


def _login(client: TestClient, username: str = "qauser") -> tuple[str, int]:
    password = "Test123456!"
    client.post("/api/auth/register",
                json={"username": username, "password": password})
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    token = resp.json()["access_token"]
    uid = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {token}"}).json()["id"]
    return token, uid


def _parse_sse(response) -> list[dict]:  # noqa: ANN001
    events: list[dict] = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


# ---------------------------------------------------------------------------
# 1. 检索与阈值过滤（服务层）
# ---------------------------------------------------------------------------

def test_retrieve_context_filters_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        _hit("高相关内容：密码每 90 天更换", 0.91),
        _hit("勉强相关的内容", 0.40),
        _hit("完全无关的噪声片段", 0.35),
    ]
    captured: dict = {}

    def _search(user_id, query_text, top_k):  # noqa: ANN001
        captured.update(user_id=user_id, query=query_text, top_k=top_k)
        return hits

    monkeypatch.setattr(qa, "vector_search", _search)

    # 显式传阈值，避免依赖 settings 默认值（默认阈值经实测校准，可能调整）
    result = qa.retrieve_context(7, "密码多久换一次？", score_threshold=0.4)

    assert captured == {"user_id": 7, "query": "密码多久换一次？", "top_k": 4}
    assert [s.index for s in result.sources] == [1, 2]  # 0.35 被阈值剔除
    assert result.sources[0].page == 3
    assert result.sources[0].doc_name == "信息安全制度.pdf"
    # 编号化上下文
    assert "[1] 来源：《信息安全制度.pdf》（第 3 页）" in result.context
    assert "高相关内容" in result.context
    assert "完全无关" not in result.context


def test_retrieve_context_docx_location_uses_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit("Word 里的制度条文", 0.8, doc_name="手册.docx", file_type="docx",
             page=None, section="第二章 账号管理"),
    ])

    result = qa.retrieve_context(1, "问题")

    source = result.sources[0]
    assert source.page is None  # 不伪造页码
    assert source.section == "第二章 账号管理"
    assert "章节/工作表：第二章 账号管理" in result.context


def test_retrieve_context_xlsx_location(monkeypatch: pytest.MonkeyPatch) -> None:
    label = qa.location_label(_hit(
        "x", 0.8, doc_name="台账.xlsx", file_type="xlsx",
        page=None, section="Sheet1"))
    assert label == "章节/工作表：Sheet1"


def test_retrieve_context_snippet_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = "制度" * 200
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit(long_text, 0.9),
    ])

    result = qa.retrieve_context(1, "问题")
    source = result.sources[0]
    assert len(source.snippet) <= qa.SNIPPET_LIMIT + 1
    assert source.snippet.endswith("…")
    # 上下文中保留完整原文（让模型看到全文，展示给前端的 snippet 才截断）
    assert long_text in result.context


def test_prompt_is_constrained_and_numbered() -> None:
    prompt = qa.build_prompt("密码多久换？", "[1] 来源：《制度.pdf》（第 3 页）\n密码 90 天")

    assert "<参考资料>" in prompt
    assert "密码 90 天" in prompt
    assert "密码多久换？" in prompt
    # 约束性规则齐备
    assert "禁止" in prompt          # 禁止资料外内容
    assert "[1]" in prompt           # 来源编号
    assert "无法回答" in prompt      # 资料不足明说不知道


# ---------------------------------------------------------------------------
# 2. build_answer：空命中 / 成功 / 异常
# ---------------------------------------------------------------------------

def test_build_answer_empty_hit_skips_llm(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit("无关", 0.1), _hit("也无关", 0.2),
    ])

    response = qa.build_answer(1, "任意问题")

    assert response.hit is False
    assert response.sources == []
    assert response.answer == NO_HIT_ANSWER
    assert fake_llm.prompts == []  # 空命中绝不调用 LLM


def test_build_answer_no_collection_skips_llm(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [])

    response = qa.build_answer(999, "问题")

    assert response.hit is False
    assert response.answer == NO_HIT_ANSWER
    assert fake_llm.prompts == []


def test_build_answer_success_with_sources(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit("密码每 90 天更换一次", 0.92),
    ])

    response = qa.build_answer(1, "密码多久换？")

    assert response.hit is True
    assert "90 天" in response.answer
    assert response.sources[0].doc_name == "信息安全制度.pdf"
    assert response.sources[0].index == 1
    # prompt 中注入了真实片段
    assert "密码每 90 天更换一次" in fake_llm.prompts[0]


def test_build_answer_rejects_blank_question(fake_llm: FakeLLM) -> None:
    with pytest.raises(qa.QaError, match="不能为空"):
        qa.build_answer(1, "   ")


def test_build_answer_propagates_ollama_errors(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.invoke_error = OllamaModelMissing("缺少 qwen2.5:7b")

    with pytest.raises(OllamaModelMissing):
        qa.build_answer(1, "问题")


def test_build_answer_wraps_generic_generation_error(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.invoke_error = ConnectionError("推理中断")

    with pytest.raises(qa.QaGenerationError, match="生成答案失败"):
        qa.build_answer(1, "问题")


def test_build_answer_empty_string_raises(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.answer = "   "

    with pytest.raises(qa.QaGenerationError, match="空答案"):
        qa.build_answer(1, "问题")


def test_build_answer_vector_error_propagates(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    def _raise(*a, **k):  # noqa: ANN002
        raise VectorStoreError("Milvus 不可用")

    monkeypatch.setattr(qa, "vector_search", _raise)

    with pytest.raises(VectorStoreError):
        qa.build_answer(1, "问题")
    assert fake_llm.prompts == []


# ---------------------------------------------------------------------------
# 3. POST /api/chat/ask 路由
# ---------------------------------------------------------------------------

def test_ask_endpoint_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, uid = _login(client)
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit("密码每 90 天更换", 0.9),
    ])

    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": "密码多久换？"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["hit"] is True
    assert data["answer"] == fake_llm.answer
    assert data["sources"][0]["page"] == 3
    assert data["sources"][0]["score"] == pytest.approx(0.9)


def test_ask_endpoint_no_hit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "qauser2")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [])

    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": "无关问题"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["hit"] is False
    assert data["sources"] == []
    assert data["answer"] == NO_HIT_ANSWER
    assert fake_llm.prompts == []


def test_ask_endpoint_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/chat/ask", json={"question": "x"})
    assert resp.status_code == 401


def test_ask_endpoint_validates_question(
    client: TestClient, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "qauser3")
    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": ""})
    assert resp.status_code == 422


def test_ask_endpoint_model_missing_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "qauser4")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.invoke_error = OllamaModelMissing("缺模型")

    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": "问题"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "OLLAMA_MODEL_MISSING"


def test_ask_endpoint_ollama_down_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "qauser5")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.invoke_error = OllamaServiceUnavailable("连不上 Ollama")

    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": "问题"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "OLLAMA_UNAVAILABLE"


def test_ask_endpoint_vector_error_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _ = _login(client, "qauser6")

    def _raise(*a, **k):  # noqa: ANN002
        raise VectorStoreError("Milvus 挂了")

    monkeypatch.setattr(qa, "vector_search", _raise)

    resp = client.post("/api/chat/ask",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"question": "问题"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "VECTOR_STORE_ERROR"


# ---------------------------------------------------------------------------
# 4. GET /api/chat/stream SSE
# ---------------------------------------------------------------------------

def test_stream_emits_tokens_and_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "streamer1")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [
        _hit("密码每 90 天更换", 0.9),
    ])

    with client.stream(
        "GET",
        "/api/chat/stream",
        params={"question": "密码多久换？", "token": token},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(response)

    kinds = [event["type"] for event in events]
    assert kinds.count("token") == 3
    assert kinds[-1] == "done"
    tokens = [event["data"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "".join(fake_llm.chunks)
    done = events[-1]
    assert done["hit"] is True
    assert done["sources"][0]["doc_name"] == "信息安全制度.pdf"
    assert done["sources"][0]["page"] == 3


def test_stream_supports_bearer_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "streamer2")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])

    with client.stream(
        "GET",
        "/api/chat/stream?question=q",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        events = _parse_sse(response)
    assert events[-1]["type"] == "done"


def test_stream_no_hit_emits_guidance_and_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "streamer3")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [])

    with client.stream(
        "GET", "/api/chat/stream",
        params={"question": "无关", "token": token},
    ) as response:
        events = _parse_sse(response)

    assert events[0]["type"] == "token"
    assert events[0]["data"] == NO_HIT_ANSWER
    assert events[1] == {"type": "done", "sources": [], "hit": False}
    assert fake_llm.prompts == []


def test_stream_requires_token(client: TestClient) -> None:
    with client.stream(
        "GET", "/api/chat/stream", params={"question": "x"}
    ) as response:
        assert response.status_code == 401


def test_stream_validates_question(client: TestClient) -> None:
    token, _ = _login(client, "streamer4")
    with client.stream(
        "GET", "/api/chat/stream", params={"token": token}
    ) as response:
        assert response.status_code == 422  # 缺 question


def test_stream_generation_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "streamer5")
    monkeypatch.setattr(qa, "vector_search", lambda *a, **k: [_hit("x", 0.9)])
    fake_llm.stream_error = ConnectionError("流中断")

    with client.stream(
        "GET", "/api/chat/stream",
        params={"question": "问题", "token": token},
    ) as response:
        assert response.status_code == 200
        events = _parse_sse(response)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "GENERATION_ERROR"


def test_stream_retrieve_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM,
) -> None:
    token, _ = _login(client, "streamer6")

    def _raise(*a, **k):  # noqa: ANN002
        raise VectorStoreError("嵌入失败")

    monkeypatch.setattr(qa, "vector_search", _raise)

    with client.stream(
        "GET", "/api/chat/stream",
        params={"question": "问题", "token": token},
    ) as response:
        events = _parse_sse(response)

    assert events == [{"type": "error", "code": "RETRIEVE_ERROR",
                       "message": "嵌入失败"}]
    assert fake_llm.prompts == []
