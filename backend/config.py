"""全局配置模块。

使用 pydantic-settings 从环境变量 / .env 文件读取配置；
所有模块统一通过 ``settings`` 单例访问，禁止散落的硬编码。

注意：LLM 与 Embedding 端点只允许指向配置的 Ollama 地址，
代码库中不得出现任何外网模型 API 地址。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录的绝对路径（本文件位于 backend/config.py）
BASE_DIR: Path = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """系统配置（字段名与 .env 中的环境变量一一对应，不区分大小写）。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用基础 ----------------
    app_name: str = "私有化RAG知识库"
    app_env: str = "production"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # ---------------- 认证（JWT） ----------------
    secret_key: str = "please-change-me-to-a-random-secret-key"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720

    # CORS 允许来源；单端口交付时仅开发期需要 Vite 的 5173
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # ---------------- Ollama 本地模型 ----------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: int = 120
    llm_model: str = "qwen2.5:7b"
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024

    # 推理线程数：Intel 混合架构（P-core + E-core）需限制为性能核数量，
    # 否则 E-core 不支持 AVX2 会拖慢推理数十倍。纯大核服务器留 0 让 ollama 自动用满。
    ollama_num_threads: int = 0

    # ---------------- 切片与检索 ----------------
    chunk_size: int = 500
    chunk_overlap: int = 50
    retrieve_top_k: int = 4

    # 嵌入批大小：单次 /api/embed 请求包含的文本条数。
    # bge-m3（567M）在 CPU 上约 2-4s/条；默认 16 条一批实测约 60-80s，
    # 留余量给 OLLAMA_TIMEOUT（120s），避免大文件一次性上千条嵌入导致 503。
    # 若部署到多核 GPU 机器可适当调大（如 64/128）。
    embed_batch_size: int = 16

    # xlsx 专用块大小（字符）：表格每行是一条独立记录，过细切块会把一条收入/
    # 支出记录拦腰截断且块数过多（CPU 嵌入时间不可接受）。默认 1200，约 8-15
    # 行记录一块，既保持记录完整又把近百万字符的大表从数千块压到数百块。
    xlsx_chunk_size: int = 1200
    # 检索相似度阈值（仅取最相关片段，降低幻觉）；具体度量在向量库模块落地。
    # bge-m3 实测校准：相关问题 score ≥ 0.71，无关问题被 0.5 完全过滤，
    # 取 0.6 留足余量（漏答可调至 0.55，乱命中可调至 0.65）。
    retrieve_score_threshold: float = 0.6

    # ---------------- 数据存储 ----------------
    data_dir: Path = BASE_DIR / "data"
    sqlite_path: Path | None = None
    milvus_uri: Path | None = None
    uploads_dir: Path | None = None
    static_dir: Path = BASE_DIR / "static"

    # 每用户一个 collection 实现知识库物理隔离，命名模板（仅支持 {user_id} 占位）
    collection_name_template: str = "user_{user_id}_rag"
    # schema 版本：仅用于兼容性自检与错误迁移提示
    schema_version: str = "v1"

    # ---------------- 上传限制 ----------------
    max_upload_mb: int = 50
    allowed_extensions: list[str] = Field(
        default_factory=lambda: ["pdf", "docx", "xlsx"]
    )

    # ---------------- 配置规范化与启动期校验 ----------------
    @field_validator("allowed_extensions", "cors_origins", mode="before")
    @classmethod
    def _parse_list(cls, value: object) -> object:
        """兼容逗号分隔字符串形式的环境变量（如 a,b），其余交给 pydantic 解析 JSON。"""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def setup_paths(self) -> None:
        """派生未显式配置的路径并确保运行时目录存在（应用启动时调用一次）。"""
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        if self.sqlite_path is None:
            self.sqlite_path = self.data_dir / "app.db"
        else:
            self.sqlite_path = Path(self.sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        if self.milvus_uri is None:
            self.milvus_uri = self.data_dir / "milvus_rag.db"
        else:
            self.milvus_uri = Path(self.milvus_uri)
        self.milvus_uri.parent.mkdir(parents=True, exist_ok=True)

        if self.uploads_dir is None:
            self.uploads_dir = self.data_dir / "uploads"
        else:
            self.uploads_dir = Path(self.uploads_dir)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    @property
    def max_upload_bytes(self) -> int:
        """上传大小上限（字节）。"""
        return self.max_upload_mb * 1024 * 1024

    def is_dev_secret(self) -> bool:
        """是否仍在使用默认密钥（生产环境应在启动日志中告警）。"""
        return self.secret_key == "please-change-me-to-a-random-secret-key"


@lru_cache
def get_settings() -> Settings:
    """配置单例（lru_cache 保证全程只读一次环境变量）。"""
    return Settings()


# 全局唯一配置入口
settings = get_settings()
