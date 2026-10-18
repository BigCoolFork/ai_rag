"""Ollama 服务层与 /api/health 单元测试。

测试不依赖真实 Ollama：
- 纯函数（URL 规范化、模型名匹配）直接断言；
- probe_ollama 通过 monkeypatch 替换底层 HTTP 调用；
- 单例在「服务不可达 / 模型缺失」分支必须在导入 langchain_ollama
  之前抛出可读异常，故无需安装 langchain 即可验证。

在 backend/ 目录下运行：
    python -m pytest tests/test_ollama_health.py -v
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from services import ollama_client, vector_store
from services.ollama_client import (
    ModelStatus,
    OllamaModelMissing,
    OllamaServiceUnavailable,
    OllamaStatus,
    model_is_installed,
    normalize_base_url,
)
from services.vector_store import MilvusStatus


def _milvus_ready() -> MilvusStatus:
    """健康检查用例统一使用的 Milvus 就绪桩（本文件聚焦 Ollama 逻辑）。"""
    return MilvusStatus(
        ready=True, detail="Milvus Lite 可连接", uri=":memory:",
        collections=["user_1_rag"], incompatible_collections=[],
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_normalize_base_url_adds_scheme_and_strips_slash() -> None:
    assert normalize_base_url("127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert normalize_base_url(" http://ollama:11434/ ") == "http://ollama:11434"
    assert normalize_base_url("https://host:11434") == "https://host:11434"


def test_normalize_base_url_empty_raises() -> None:
    with pytest.raises(ValueError):
        normalize_base_url("  ")


def test_model_is_installed_matches_latest_tag() -> None:
    installed = ["bge-m3:latest", "qwen2.5:7b", "qwen2.5:3b"]
    # 未带 tag 的名字应匹配 :latest
    assert model_is_installed(installed, "bge-m3") is True
    assert model_is_installed(installed, "qwen2.5:7b") is True
    assert model_is_installed(installed, "qwen2.5:32b") is False
    assert model_is_installed(installed, "qwen2.5") is False  # 不做前缀模糊匹配


# ---------------------------------------------------------------------------
# probe_ollama：通过桩函数模拟 HTTP
# ---------------------------------------------------------------------------

def _fake_http_ok_full(path: str, timeout: float = 5.0) -> dict:
    """模拟服务正常且两个模型都已安装。"""
    if path == "/api/version":
        return {"version": "0.5.7"}
    if path == "/api/tags":
        return {"models": [
            {"name": "qwen2.5:7b", "model": "qwen2.5:7b"},
            {"name": "bge-m3:latest"},
        ]}
    raise AssertionError(f"未预期的路径: {path}")


def _fake_http_down(path: str, timeout: float = 5.0) -> dict:
    raise httpx.ConnectError("连接被拒绝")


def _fake_http_missing_models(path: str, timeout: float = 5.0) -> dict:
    """服务正常，但一个模型都没 pull。"""
    if path == "/api/version":
        return {"version": "0.5.7"}
    if path == "/api/tags":
        return {"models": []}
    raise AssertionError(f"未预期的路径: {path}")


def test_probe_all_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_client, "_http_get_json", _fake_http_ok_full)
    status = ollama_client.probe_ollama()

    assert status.ollama_ready is True
    assert status.version == "0.5.7"
    assert status.llm is not None and status.llm.ready is True
    assert status.embedding is not None and status.embedding.ready is True
    assert status.all_ready is True
    assert "qwen2.5:7b" in status.installed_models


def test_probe_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_client, "_http_get_json", _fake_http_down)
    status = ollama_client.probe_ollama()

    assert status.ollama_ready is False
    assert status.version is None
    # 探测函数本身不抛异常，原因写在 detail 中
    assert "无法连接 Ollama 服务" in status.ollama_detail
    assert "ollama serve" in status.ollama_detail
    assert status.llm is not None and status.llm.ready is False
    assert status.embedding is not None and status.embedding.ready is False
    assert status.all_ready is False


def test_probe_model_missing_lists_pull_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_client, "_http_get_json", _fake_http_missing_models)
    status = ollama_client.probe_ollama()

    assert status.ollama_ready is True
    assert status.llm is not None and status.llm.ready is False
    assert "ollama pull qwen2.5:7b" in status.llm.pull_command
    assert status.embedding is not None and status.embedding.ready is False
    assert "ollama pull bge-m3" in status.embedding.pull_command


# ---------------------------------------------------------------------------
# 单例异常路径（无需安装 langchain_ollama：探测失败先于客户端构造）
# ---------------------------------------------------------------------------

def test_get_llm_raises_when_service_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ollama_client.reset_clients()
    monkeypatch.setattr(ollama_client, "_http_get_json", _fake_http_down)

    with pytest.raises(OllamaServiceUnavailable) as exc_info:
        ollama_client.get_llm()
    assert "无法连接 Ollama 服务" in str(exc_info.value)
    ollama_client.reset_clients()


def test_get_embeddings_raises_with_pull_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ollama_client.reset_clients()
    monkeypatch.setattr(ollama_client, "_http_get_json", _fake_http_missing_models)

    with pytest.raises(OllamaModelMissing) as exc_info:
        ollama_client.get_embeddings()
    message = str(exc_info.value)
    assert "bge-m3" in message
    assert "ollama pull bge-m3" in message
    assert "docker compose exec ollama" in message  # Docker 离线提示
    ollama_client.reset_clients()


# ---------------------------------------------------------------------------
# /api/health 接口
# ---------------------------------------------------------------------------

def _ready_status() -> OllamaStatus:
    return OllamaStatus(
        base_url="http://ollama:11434",
        ollama_ready=True,
        ollama_detail="Ollama 服务可连接（版本 0.5.7）",
        version="0.5.7",
        installed_models=["qwen2.5:7b", "bge-m3:latest"],
        llm=ModelStatus("qwen2.5:7b", True, "LLM模型已安装"),
        embedding=ModelStatus("bge-m3", True, "Embedding模型已安装"),
    )


def _down_status() -> OllamaStatus:
    status = OllamaStatus(
        base_url="http://ollama:11434",
        ollama_ready=False,
        ollama_detail="无法连接 Ollama 服务",
    )
    status.llm = ModelStatus("qwen2.5:7b", False, "未探测",
                             pull_command="ollama pull qwen2.5:7b")
    status.embedding = ModelStatus("bge-m3", False, "未探测",
                                   pull_command="ollama pull bge-m3")
    return status


def test_health_all_ready_returns_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_client, "probe_ollama", _ready_status)
    monkeypatch.setattr(vector_store, "probe_milvus", _milvus_ready)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["checks"]["sqlite"]["ready"] is True
    assert data["checks"]["milvus"]["ready"] is True
    assert data["checks"]["ollama"]["ready"] is True
    assert data["checks"]["llm_model"]["ready"] is True
    assert data["checks"]["embedding_model"]["ready"] is True
    assert data["checks"]["llm_model"]["name"] == "qwen2.5:7b"


def test_health_degraded_returns_503_with_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_client, "probe_ollama", _down_status)
    monkeypatch.setattr(vector_store, "probe_milvus", _milvus_ready)

    resp = client.get("/api/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    # SQLite 与 Milvus 仍正常，只有 Ollama 侧不可用
    assert data["checks"]["sqlite"]["ready"] is True
    assert data["checks"]["milvus"]["ready"] is True
    assert data["checks"]["ollama"]["ready"] is False
    assert "ollama pull" in data["checks"]["llm_model"]["pull_command"]
