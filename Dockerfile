# ==========================================================================
# 精简私有化 RAG 知识库 —— 多阶段镜像
#   阶段 1 frontend：node:20-alpine 内构建 Vue3 静态产物（输出到 backend/static）
#   阶段 2 runtime ：python:3.11-slim 运行 FastAPI + Milvus Lite + 托管前端
# 构建期允许走镜像源（默认国内源），运行时零外网：
#   docker build \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#     --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
#     -t rag-personal:0.1.0 .
# 内网环境把两个 ARG 换成内网源即可；离线环境请改用 wheelhouse / npm 缓存方案
# （见 README「离线机构建」）。
# ==========================================================================

# ------------------------- 阶段 1：前端构建 -------------------------
FROM node:20-alpine AS frontend
# 注意：工作目录必须与本机 frontend/ 同名层级 —— vite.config.ts 的 outDir
# 是相对项目根的 ../backend/static，层级错了产物会落到容器外路径
WORKDIR /build/frontend

# 构建期 npm 源可覆盖（默认 npmmirror）；仅在该 RUN 层生效，不进入运行时镜像
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" && npm ci

COPY frontend/ ./
# 产物输出到 /build/backend/static
RUN npm run build

# ------------------------- 阶段 2：运行时 ----------------------------
FROM python:3.11-slim AS runtime

# faiss-cpu / grpcio 的 manylinux wheel 依赖 GNU OpenMP 运行时（slim 镜像默认没有）；
# --no-install-recommends 避免拉入无关组件，装完即清 apt 缓存减小镜像层
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行；数据目录单独 chown
RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app

# 构建期 pip 源可覆盖（默认清华源）；版本全部锁在 requirements.txt
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 后端代码 + 前端构建产物（镜像内自带，单端口交付；.env 不打入镜像，运行时由 compose 注入）
COPY backend/ ./backend/
COPY --from=frontend /build/backend/static ./backend/static

# 运行时数据目录（SQLite / Milvus Lite / 上传文件），由 compose 挂载持久卷覆盖
RUN mkdir -p /app/backend/data && chown -R app:app /app
USER app

EXPOSE 8000

# /api/health 全绿返回 200，任一依赖未就绪返回 503；不依赖 curl/wget
HEALTHCHECK --interval=30s --timeout=6s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
