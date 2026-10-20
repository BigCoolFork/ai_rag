"""Ollama 本地模型服务层。

职责：
1. 以单例方式提供 langchain_ollama.OllamaLLM / OllamaEmbeddings；
   所有参数（地址、模型名、超时）全部来自 config.settings，无硬编码；
2. 提供独立的健康探测 probe_ollama()：检查 Ollama 服务连通性，
   并从 /api/tags 判断 LLM 与嵌入模型（默认 bge-m3）是否已本地安装；
3. 服务不可达 / 模型缺失时抛出明确可读的异常，错误信息直接给出
   需要执行的 ``ollama pull`` 命令（含 Docker 部署与离线搬运提示）。

硬约束：本模块是系统唯一的模型入口，严禁出现任何外网模型 API 地址。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

# 健康探测使用短超时，避免 /api/health 长时间挂起（与推理超时区分开）
PROBE_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class OllamaClientError(Exception):
    """Ollama 相关错误基类。"""


class OllamaServiceUnavailable(OllamaClientError):
    """Ollama 服务不可达（未启动 / 地址错误 / 网络不通）。"""


class OllamaModelMissing(OllamaClientError):
    """Ollama 服务正常，但指定模型未在本地安装。"""


# ---------------------------------------------------------------------------
# 状态结构
# ---------------------------------------------------------------------------

@dataclass
class ModelStatus:
    """单个模型的就绪状态。"""

    name: str
    ready: bool
    detail: str
    pull_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ready": self.ready,
            "detail": self.detail,
            "pull_command": self.pull_command,
        }


@dataclass
class OllamaStatus:
    """Ollama 整体探测结果（不抛异常，供健康检查与启动告警使用）。"""

    base_url: str
    ollama_ready: bool
    ollama_detail: str
    version: str | None = None
    installed_models: list[str] = field(default_factory=list)
    llm: ModelStatus | None = None
    embedding: ModelStatus | None = None

    @property
    def all_ready(self) -> bool:
        return (
            self.ollama_ready
            and self.llm is not None and self.llm.ready
            and self.embedding is not None and self.embedding.ready
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "ollama": {"ready": self.ollama_ready, "detail": self.ollama_detail,
                       "version": self.version},
            "installed_models": self.installed_models,
            "llm_model": self.llm.to_dict() if self.llm else None,
            "embedding_model": self.embedding.to_dict() if self.embedding else None,
        }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def normalize_base_url(base_url: str) -> str:
    """规范化 Ollama 地址。

    - 去除首尾空白与末尾斜杠；
    - 缺少 http(s) 前缀时补 ``http://``（兼容 ``127.0.0.1:11434`` 写法）；
    - 避免与后续 /api/xxx 拼接时出现双斜杠或缺协议。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("OLLAMA_BASE_URL 未配置")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _pull_command(model: str) -> str:
    """生成该模型的拉取命令提示（宿主机 / Docker 两种形态）。"""
    return (
        f"ollama pull {model}（Docker 部署："
        f"docker compose exec ollama ollama pull {model}；"
        f"离线环境请在有网机 pull 后搬运 Ollama 模型数据卷）"
    )


def _http_get_json(path: str, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """请求 Ollama HTTP API 并返回 JSON。

    使用 trust_env=False：本系统只访问内网/本地 Ollama，
    避免 Windows 系统代理（HTTP_PROXY）拦截回环与内网地址导致误判。
    """
    base_url = normalize_base_url(settings.ollama_base_url)
    url = f"{base_url}{path}"
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def model_is_installed(installed: list[str], wanted: str) -> bool:
    """判断目标模型是否已安装。

    Ollama 拉取未带 tag 的模型时 /api/tags 会显示 ``name:latest``，
    因此 ``bge-m3`` 应能匹配 ``bge-m3:latest``。
    """
    wanted = wanted.strip()
    names = {n.strip() for n in installed}
    if wanted in names:
        return True
    if ":" not in wanted and f"{wanted}:latest" in names:
        return True
    return False


def _list_installed_models(timeout: float = PROBE_TIMEOUT) -> list[str]:
    """调用 /api/tags，返回本地已安装模型名列表。"""
    payload = _http_get_json("/api/tags", timeout)
    names: set[str] = set()
    for item in payload.get("models", []) or []:
        name = item.get("name") or item.get("model")
        if name:
            names.add(str(name).strip())
    return sorted(names)


# ---------------------------------------------------------------------------
# 健康探测（不抛异常）
# ---------------------------------------------------------------------------

def probe_ollama(timeout: float = PROBE_TIMEOUT) -> OllamaStatus:
    """探测 Ollama 服务与两个本地模型是否就绪。

    本函数永不抛异常——任何失败都体现在返回状态中，
    供 ``/api/health`` 与应用启动告警使用。
    """
    base_url = normalize_base_url(settings.ollama_base_url)

    # 1) 连通性 + 版本：GET /api/version
    version: str | None = None
    try:
        info = _http_get_json("/api/version", timeout)
        version = str(info.get("version") or "").strip() or "unknown"
        ollama_ready = True
        ollama_detail = f"Ollama 服务可连接（版本 {version}）"
    except Exception as exc:  # noqa: BLE001 - 探测需保留任何失败的具体原因
        reason = f"{type(exc).__name__}: {exc}"
        detail = (
            f"无法连接 Ollama 服务（{base_url}），原因：{reason}。"
            f"请确认 Ollama 已启动（ollama serve）；"
            f"Docker 部署请确认 ollama 服务正常且 OLLAMA_BASE_URL={base_url}。"
        )
        logger.warning(detail)
        unavailable = OllamaStatus(
            base_url=base_url,
            ollama_ready=False,
            ollama_detail=detail,
        )
        unavailable.llm = ModelStatus(
            settings.llm_model, False, "Ollama 服务不可达，模型状态未知"
        )
        unavailable.embedding = ModelStatus(
            settings.embedding_model, False, "Ollama 服务不可达，模型状态未知"
        )
        return unavailable

    # 2) 已安装模型列表：GET /api/tags
    try:
        installed = _list_installed_models(timeout)
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        status = OllamaStatus(
            base_url=base_url,
            ollama_ready=True,
            ollama_detail=f"{ollama_detail}，但获取模型列表失败：{reason}",
            version=version,
            installed_models=[],
        )
        status.llm = ModelStatus(
            settings.llm_model, False, f"模型列表获取失败：{reason}"
        )
        status.embedding = ModelStatus(
            settings.embedding_model, False, f"模型列表获取失败：{reason}"
        )
        return status

    # 3) 逐个核对所需模型
    def _check(model: str, kind: str) -> ModelStatus:
        if model_is_installed(installed, model):
            return ModelStatus(model, True, f"{kind}模型已安装：{model}")
        return ModelStatus(
            name=model,
            ready=False,
            detail=(
                f"本地未检测到{kind}模型「{model}」，请在运行 Ollama 的机器上执行："
            ),
            pull_command=_pull_command(model),
        )

    return OllamaStatus(
        base_url=base_url,
        ollama_ready=ollama_ready,
        ollama_detail=ollama_detail,
        version=version,
        installed_models=installed,
        llm=_check(settings.llm_model, "LLM"),
        embedding=_check(settings.embedding_model, "Embedding"),
    )


# ---------------------------------------------------------------------------
# 模型单例（首次获取时主动探测，不就绪即抛出可读异常）
# ---------------------------------------------------------------------------

_llm: Any = None
_embeddings: Any = None


def get_llm() -> Any:
    """获取 OllamaLLM 单例（langchain_ollama.OllamaLLM）。

    首次调用时主动探测服务连通性与 LLM 模型是否存在；
    不就绪直接抛出 OllamaServiceUnavailable / OllamaModelMissing。
    """
    global _llm
    if _llm is not None:
        return _llm

    status = probe_ollama()
    if not status.ollama_ready:
        raise OllamaServiceUnavailable(status.ollama_detail)
    assert status.llm is not None
    if not status.llm.ready:
        raise OllamaModelMissing(
            f"{status.llm.detail} {status.llm.pull_command}"
        )

    # 延迟导入：未装 langchain_ollama 时，探测与健康检查仍可独立工作，
    # 且错误边界清晰（先保证服务/模型就位，再构造客户端）
    from langchain_ollama import OllamaLLM

    llm_kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": normalize_base_url(settings.ollama_base_url),
        # langchain-ollama 0.2.x 无顶层 timeout 字段，超时透传给底层 ollama Client
        "client_kwargs": {"timeout": settings.ollama_timeout},
        "temperature": 0.2,
        # 限制单次最大生成 token 数，防止模型无限输出导致流式永不结束
        "num_predict": settings.llm_max_tokens,
    }
    # 混合架构 CPU 需限制线程数为性能核数量，避免 E-core 拖慢推理
    if settings.ollama_num_threads > 0:
        llm_kwargs["num_thread"] = settings.ollama_num_threads

    _llm = OllamaLLM(**llm_kwargs)
    logger.info("OllamaLLM 已初始化：model=%s, num_thread=%s",
                settings.llm_model, settings.ollama_num_threads or "auto")
    return _llm


def get_embeddings() -> Any:
    """获取 OllamaEmbeddings 单例（langchain_ollama.OllamaEmbeddings）。

    首次调用时主动探测服务连通性与嵌入模型（默认 bge-m3）是否存在。
    """
    global _embeddings
    if _embeddings is not None:
        return _embeddings

    status = probe_ollama()
    if not status.ollama_ready:
        raise OllamaServiceUnavailable(status.ollama_detail)
    assert status.embedding is not None
    if not status.embedding.ready:
        raise OllamaModelMissing(
            f"{status.embedding.detail} {status.embedding.pull_command}"
        )

    from langchain_ollama import OllamaEmbeddings

    emb_kwargs: dict[str, Any] = {
        "model": settings.embedding_model,
        "base_url": normalize_base_url(settings.ollama_base_url),
        # langchain-ollama 0.2.x 无顶层 timeout 字段，超时透传给底层 ollama Client
        "client_kwargs": {"timeout": settings.ollama_timeout},
    }
    # 注意：OllamaEmbeddings 不接受 num_thread 顶层参数（pydantic 校验拒绝），
    # 需在实例化后通过包装 _client.embed 注入 options。

    _embeddings = OllamaEmbeddings(**emb_kwargs)

    # 混合架构 CPU 需限制线程数为性能核数量，避免 E-core 拖慢嵌入
    if settings.ollama_num_threads > 0:
        orig_embed = _embeddings._client.embed

        def _patched_embed(*args: Any, **kwargs: Any) -> Any:
            opts = dict(kwargs.get("options") or {})
            opts["num_thread"] = settings.ollama_num_threads
            kwargs["options"] = opts
            return orig_embed(*args, **kwargs)

        _embeddings._client.embed = _patched_embed  # type: ignore[method-assign]

    logger.info("OllamaEmbeddings 已初始化：model=%s, num_thread=%s",
                settings.embedding_model, settings.ollama_num_threads or "auto")
    return _embeddings


def reset_clients() -> None:
    """重置单例（主要供单元测试使用）。"""
    global _llm, _embeddings
    _llm = None
    _embeddings = None
