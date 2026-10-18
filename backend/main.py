"""FastAPI 应用薄入口。

职责仅限 HTTP 边界与生命周期：
1. 启动时准备数据目录、初始化 SQLite 表；
2. 配置 CORS；
3. 挂载各业务路由（auth 认证 / documents 文档管理 / chat 问答与健康检查）；
4. 生产环境托管 frontend 构建产物（backend/static），实现单端口交付。

业务逻辑全部位于 routers / services 层，本文件不写业务代码。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from config import settings
from database import init_db
from services import ollama_client, vector_store
from services.upload_ingest import recover_stale_pending
from routers import auth as auth_router
from routers import chat as chat_router
from routers import documents as documents_router

logger = logging.getLogger("rag_personal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    """应用生命周期：启动初始化 / 关闭释放资源。"""
    # ---- 启动 ----
    settings.setup_paths()
    init_db()
    # 重启恢复：上次进程被中断时仍在 pending 的文档标记为 failed，
    # 避免列表里出现永久「处理中」的僵尸记录
    recover_stale_pending()
    logger.info("数据目录: %s", settings.data_dir)
    logger.info("SQLite: %s | Milvus Lite: %s", settings.sqlite_path, settings.milvus_uri)
    logger.info("Ollama: %s | LLM=%s | Embedding=%s",
                settings.ollama_base_url, settings.llm_model, settings.embedding_model)
    if settings.is_dev_secret() and settings.app_env == "production":
        logger.warning("SECRET_KEY 仍为默认值！生产部署请在 .env 中设置随机密钥。")
    # 启动时探测 Ollama 与本地模型——仅告警不阻断启动，
    # 详细状态可通过 GET /api/health 查询
    try:
        ollama_status = ollama_client.probe_ollama()
        if ollama_status.all_ready:
            logger.info("Ollama 就绪：%s | 已安装模型 %s",
                        ollama_status.ollama_detail,
                        ollama_status.installed_models)
        else:
            if not ollama_status.ollama_ready:
                logger.warning("Ollama 不可达：%s", ollama_status.ollama_detail)
            if ollama_status.llm and not ollama_status.llm.ready:
                logger.warning("LLM 模型未就位，%s %s",
                               ollama_status.llm.detail,
                               ollama_status.llm.pull_command)
            if ollama_status.embedding and not ollama_status.embedding.ready:
                logger.warning("Embedding 模型未就位，%s %s",
                               ollama_status.embedding.detail,
                               ollama_status.embedding.pull_command)
    except Exception as exc:  # noqa: BLE001 - 探测异常绝不阻断启动
        logger.warning("启动期 Ollama 探测出现异常：%s", exc)
    # 启动时探测 Milvus Lite 与已有 collection schema 兼容性——仅告警不阻断启动，
    # 首个用户上传时仍会惰性建集合；详细状态可通过 GET /api/health 查询
    try:
        milvus_status = vector_store.probe_milvus()
        if milvus_status.ready:
            logger.info("Milvus Lite 就绪：%s（用户集合 %d 个）",
                        milvus_status.uri, len(milvus_status.collections))
        else:
            logger.warning("Milvus Lite 未就绪：%s", milvus_status.detail)
        for bad_name in milvus_status.incompatible_collections:
            logger.warning("Milvus collection「%s」schema 不兼容，"
                           "该用户入库/检索将被拒绝并给出迁移提示。", bad_name)
    except Exception as exc:  # noqa: BLE001 - 探测异常绝不阻断启动
        logger.warning("启动期 Milvus 探测出现异常：%s", exc)
    yield
    # ---- 关闭 ----
    logger.info("应用关闭，资源已释放。")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------- CORS ----------------
# 单端口交付时同源访问；此处主要服务本地 Vite 开发服务器（5173）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 业务路由挂载（脚手架占位） ----------------
app.include_router(auth_router.router, prefix="/api/auth", tags=["认证"])
app.include_router(documents_router.router, prefix="/api/documents", tags=["文档管理"])
# chat 路由内含 /api/health，故挂在 /api 前缀下，端点路径在模块内写全
app.include_router(chat_router.router, prefix="/api", tags=["问答"])


# ---------------- 前端 SPA 静态托管（单端口） ----------------
# frontend 构建产物复制到 backend/static 后自动生效；
# 未构建（后端独立开发期）时不挂载，访问根路径返回提示 JSON。
def _register_spa() -> None:
    static_dir: Path = settings.static_dir
    index_file = static_dir / "index.html"
    if not index_file.exists():
        logger.info("未发现前端构建产物 %s，SPA 托管未启用（前端独立开发模式）。", index_file)
        return

    # 静态资源目录（assets 等）必须挂在兜底路由之前
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):  # noqa: ANN201, ARG001
        """SPA history 路由兜底：非 /api 路径统一返回 index.html。

        已注册的 /api/* 路由会优先匹配，不会进入此兜底；
        前端深链（如 /documents、/chat）刷新时也由此回到 SPA 入口。
        """
        # 安全考虑：API 路径即使未匹配也不返回 HTML
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index_file)


_register_spa()


if __name__ == "__main__":
    # 本地直跑：python main.py（容器内使用 uvicorn 命令启动）
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )
