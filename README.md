# 精简私有化 RAG 知识库

面向内网/离线环境的单机知识库系统：上传 PDF / Word / Excel → 本地切片与向量化 →
基于本地大模型的检索增强问答，带来源引用与流式输出。**运行时零外网调用**：
LLM 与嵌入模型全部由本机/本机房的 Ollama 提供。

## 快速开始（新用户 5 分钟启动）

> 目标：拿到代码后做最少改动就能跑起来。全程只需改一个 `.env` 文件。

### 前置要求

- Docker 24+ 与 docker compose v2（`docker compose version` 能正常输出即可）
- 内存：纯 CPU 跑 `qwen2.5:3b` 建议 8GB+；跑 `qwen2.5:7b` 建议 16GB+
- 磁盘：首次构建约 2GB，模型权重另需 2GB+

### 第一步：拉取代码

```bash
git clone <仓库地址> rag-personal
cd rag-personal
```

### 第二步：生成配置文件（只改一个值）

```bash
cp .env.example .env
```

用编辑器打开 `.env`，**只改这一项**：

```env
SECRET_KEY=换成一段随机长字符串   # 例如：openssl rand -hex 32 的输出
```

其余保持默认即可（LLM 用 `qwen2.5:7b`，嵌入用 `bge-m3`）。如果机器内存 < 16GB，
把 `LLM_MODEL` 改成 `qwen2.5:3b`，并把 `OLLAMA_TIMEOUT` 设为 `300`。

### 第三步：构建并启动

```bash
docker compose up -d --build
```

首次构建会拉取 `node:20-alpine`、`python:3.11-slim` 基础镜像并安装依赖，
国内环境默认走清华 pip 源与 npmmirror，通常 5-10 分钟。

### 第四步：拉取模型（首次必做，之后不再需要）

```bash
docker compose exec ollama ollama pull qwen2.5:7b   # 或 qwen2.5:3b，须与 .env 的 LLM_MODEL 一致
docker compose exec ollama ollama pull bge-m3
```

模型权重存放在 docker 命名卷 `rag-personal_ollama_models`，与业务数据分离，
重启容器不会丢失。

### 第五步：验证

打开浏览器访问 `http://<主机IP>:8000`，或检查健康状态：

```bash
curl http://localhost:8000/api/health
```

五项（SQLite / Ollama / 嵌入模型 / LLM 模型 / Milvus）全绿返回 `200` 即可开始使用。

> 健康检查某一项为红色时，参考下方「常见问题」章节；纯 CPU 首次问答较慢
> （冷启动载入模型需 2-3 分钟），属正常现象。

---

## 架构

| 层 | 选型 |
| --- | --- |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia，构建为静态文件 |
| 后端 | FastAPI + Uvicorn，单端口同时提供 API 与前端静态托管 |
| 关系库 | SQLite（用户、文档登记） |
| 向量库 | Milvus Lite 3.x（嵌入式，数据落在业务数据目录，免独立服务） |
| 模型 | Ollama 0.40.5：LLM 默认 `qwen2.5:7b`（可换 3b），嵌入 `bge-m3`（1024 维，中文/多语言强） |
| 部署 | 多阶段 Dockerfile + docker compose（app + ollama），数据卷持久化 |
| ollama 镜像 | 默认自建纯 CPU 镜像 `ollama-cpu:0.40.5`（212MB，剔除 CUDA/Vulkan）；GPU 环境可换官方 `ollama/ollama:0.40.5` |

```
.
├── backend/          # FastAPI 应用（routers / services / schemas / core）
│   └── static/       # 前端构建产物（镜像构建阶段生成，勿手改）
├── frontend/         # Vue3 工程
├── scripts/          # 端到端冒烟脚本
├── Dockerfile        # app：node:20-alpine 构建前端 → python:3.11-slim 运行
├── vendor/
│   ├── Dockerfile    # ollama-cpu：官方 linux-amd64 包剔除 GPU 库（构建上下文独立）
│   └── ollama-linux-amd64.tar.zst   # 构建前自行下载（gitignore，不入库，约 1.37GB）
├── docker-compose.yml
└── requirements.txt
```

## 一、本地开发（不使用容器）

```powershell
# 1) Python 3.11+ 虚拟环境安装依赖
pip install -r requirements.txt
# 2) 前端
cd frontend; npm install; npm run build; cd ..
# 3) 准备 Ollama 并拉模型（模型目录可通过 OLLAMA_MODELS 改到非系统盘）
ollama serve
ollama pull qwen2.5:3b            # 或 qwen2.5:7b
ollama pull bge-m3
# 4) 配置并启动
Copy-Item .env.example .env        # 本机开发 OLLAMA_BASE_URL=http://localhost:11434
cd backend; uvicorn main:app --host 127.0.0.1 --port 8000
```

访问 http://127.0.0.1:8000 ，健康检查：`GET /api/health`（五项全绿返回 200）。

## 二、Docker 部署（构建机可联网）

```bash
cp .env.example .env
# 必改：SECRET_KEY（随机长字符串）；按硬件决定 LLM_MODEL（默认 qwen2.5:7b，纯 CPU 建议 qwen2.5:3b）
docker compose up -d --build
# 首次需要把模型拉到持久卷（镜像不含模型权重）
docker compose exec ollama ollama pull qwen2.5:7b
docker compose exec ollama ollama pull bge-m3
```

- 应用：http://<主机IP>:8000 ，业务数据持久化在宿主机 `./data`（SQLite、Milvus Lite、上传原文）。
- 模型权重持久化在 docker 命名卷 `rag-personal_ollama_models`，与业务数据分离，便于独立备份/搬运。
- 构建期默认走国内源（pip 清华、npm npmmirror）。内网构建可覆盖：
  在 `.env` 中设置 `PIP_INDEX_URL` / `NPM_REGISTRY` 指向内网源即可，
  或 `docker build --build-arg PIP_INDEX_URL=... --build-arg NPM_REGISTRY=...`。
- 有 NVIDIA GPU 的机器：编辑 `docker-compose.yml`，将 ollama 服务的 `build` 段注释、
  改用 `image: ollama/ollama:0.40.5`（官方镜像含 CUDA 库约 3.6GB），并打开文末 GPU 段
  （宿主机需安装 NVIDIA Container Toolkit）。无 GPU 时默认 `ollama-cpu` 即可运行，3B 模型体验最佳。
- 纯 CPU 首问答较慢：模型冷启动 + 3B CPU 推理实测约 2-3 分钟，属正常；建议 `.env` 设
  `OLLAMA_TIMEOUT=300`（默认 120 秒可能超时），模型常驻内存后后续问答明显加快。
- 验证：`curl http://localhost:8000/api/health`，或 `docker compose ps` 查看健康状态。

> **改了代码如何生效？** 修改后端 / 前端 / 依赖后需要重建镜像，详细步骤见
> [DEPLOYMENT.md](DEPLOYMENT.md)（含完整构建、增量更新、离线重新打包等场景）。

## 三、纯离线交付（镜像 + 模型卷搬运）

离线界定为**运行时**零外网；打包在一台可联网的同架构 x86_64 机器上完成。

### 3.1 在联网打包机上制作交付物

```bash
# 0. 交付物目录 + 下载 ollama 官方发布包（约 1.37GB，vendor/ 已 gitignore）
mkdir -p dist vendor
# 浏览器/下载器均可；大文件建议 aria2 多线程（单连接慢时效果显著）：
#   aria2c -x16 -s16 -d vendor -o ollama-linux-amd64.tar.zst \
#     https://github.com/ollama/ollama/releases/download/v0.40.5/ollama-linux-amd64.tar.zst
# 建议同时下载 sha256sum.txt 校验：ollama-linux-amd64.tar.zst 的 SHA256 须一致

# 1. 构建两个镜像：app（约 1.0GB）与 ollama-cpu（约 212MB，剔除 GPU 库）
docker compose build

# 2. 起 ollama 并拉取要交付的模型（模型名必须与离线机 .env 的 LLM_MODEL / EMBEDDING_MODEL 一致；
#    纯 CPU 离线机建议 qwen2.5:3b）
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:3b
docker compose exec ollama ollama pull bge-m3

# 3. 导出镜像（约 270MB；层已是压缩态，无需再 gzip）
docker save rag-personal:0.1.0 ollama-cpu:0.40.5 -o dist/images.tar

# 4. 导出模型卷（权重几乎不压缩，2 个模型约 2GB；直接用 ollama-cpu 镜像里的 tar，无需额外镜像）
docker run --rm \
  -v rag-personal_ollama_models:/v \
  -v "$PWD/dist:/out" \
  --entrypoint tar ollama-cpu:0.40.5 \
  czf /out/ollama-models.tar.gz -C /v .

# 5. dist/ 与工程文件（含 .env.example、compose、Dockerfile、requirements.txt、前后端源码）一并拷走
#    vendor/ 发布包不必交付（离线机只 load 镜像，不构建）
```

> 为什么自建 ollama 镜像：官方 `ollama/ollama:0.40.5` 内置 CUDA v12/v13 等 GPU 库，
> 单层压缩后约 3.6GB，国内镜像代理普遍在该大层限速卡死；纯 CPU 交付只需其中约 50MB
> 运行时文件。`vendor/Dockerfile` 用官方发布包解压后删除 `cuda_v12/cuda_v13/vulkan`
> 目录，构建产物与官方镜像 CLI/entrypoint 完全一致。

> Windows PowerShell 下第 4 步等价命令（一行）：
> `docker run --rm -v rag-personal_ollama_models:/v -v "${PWD}/dist:/out" --entrypoint tar ollama-cpu:0.40.5 czf /out/ollama-models.tar.gz -C /v .`

交付清单：工程目录（不含 `data/`、`.env`、`vendor/`）+ `dist/images.tar` + `dist/ollama-models.tar.gz`。

### 3.2 在离线机上恢复启动

```bash
# 1. 导入镜像（离线机全程不需要联网，也不需要 vendor/ 发布包）
docker load -i dist/images.tar

# 2. 恢复模型卷（卷名与 compose 工程名绑定：rag-personal_ollama_models）
docker volume create rag-personal_ollama_models
docker run --rm \
  -v rag-personal_ollama_models:/v \
  -v "$PWD/dist:/in" \
  --entrypoint tar ollama-cpu:0.40.5 \
  xzf /in/ollama-models.tar.gz -C /v

# 3. 配置并启动（不再访问任何外网）
cp .env.example .env      # 修改 SECRET_KEY、LLM_MODEL（须与打包的模型一致，如 qwen2.5:3b）、OLLAMA_TIMEOUT=300
docker compose up -d
curl http://localhost:8000/api/health
```

升级版本时：放入新镜像 tar 后 `docker load`，滚动 `docker compose up -d`；
模型不变则无需动模型卷，业务数据目录 `./data` 与模型卷均保留。

## 四、接口说明

所有业务接口前缀 `/api`；除健康检查外均需 `Authorization: Bearer <JWT>`
（SSE 的 EventSource 不能自定义请求头，改用 `?token=` 查询参数）。
错误响应体为 `{"detail": {"code": "错误码", "message": "中文说明"}}`，
鉴权类失败 `detail` 直接为字符串。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 就绪探针：SQLite / Ollama / 嵌入模型 / LLM 模型 / Milvus 五项，全绿 200，否则 503 |
| POST | `/api/auth/register` | 注册，入参 `{username,password}`；成功 201，重名 409 |
| POST | `/api/auth/login` | 登录，返回 `{access_token, token_type, expires_in}`；失败 401 |
| GET | `/api/auth/me` | 当前登录用户信息 |
| POST | `/api/documents/upload` | `multipart/form-data`，字段 `file`（pdf/docx/xlsx）；返回 `{duplicated, chunk_count, document}` |
| GET | `/api/documents/list?page=1&page_size=20` | 当前用户文档分页列表 |
| DELETE | `/api/documents/{id}` | 联动删除登记记录、磁盘文件与全部向量切片；越权/不存在 404 |
| POST | `/api/chat/ask` | 非流式问答，入参 `{question}`，返回 `{answer, sources[], hit}` |
| GET | `/api/chat/stream?question=...&token=...` | SSE 流式问答，`data:` 帧：`{type:"token",data:"片段"}`、`{type:"done",sources,hit}`、`{type:"error",code,message}` |

`sources[]` 元素：`{index, doc_id, doc_name, file_type, page, section, snippet, score}`，
`index` 与答案中的 `[n]` 角标对应；PDF 有真实 `page`，DOCX/XLSX 为 `null` 但带
`section`（标题 / 工作表名）。`hit=false` 时不调用 LLM，返回固定引导话术。

## 五、端到端冒烟测试

[scripts/smoke_test.py](scripts/smoke_test.py) 是黑盒 HTTP 脚本，对运行中的实例
依次验收：健康检查 → 认证（401/409）→ DOCX/PDF 上传与去重 → 非流式问答（含 PDF
页码来源、无关问题不编造）→ SSE 帧序列 → 删除后无命中 → A/B 账号权限隔离。

```powershell
# 开发机（venv 已装依赖），默认打 http://127.0.0.1:8000
python scripts/smoke_test.py
# 只验鉴权与文档管理（秒级，不调模型）
python scripts/smoke_test.py --quick
# 打远程/容器实例
python scripts/smoke_test.py --base-url http://<主机IP>:8000 --timeout 600
```

离线机没有 Python 时，可直接在 app 容器内运行（镜像自带全部依赖）：

```bash
docker cp scripts/smoke_test.py rag-personal-app:/tmp/smoke_test.py
docker compose exec app python /tmp/smoke_test.py --base-url http://127.0.0.1:8000
```

全部通过退出码 0，任一失败 1（CI/巡检可直接用退出码判定）。

## 六、配置项（.env）

全部配置项与注释见 [.env.example](.env.example)，部署最常改：

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | JWT 签名密钥，生产必须改为随机长字符串 |
| `LLM_MODEL` | 本机 LLM 标签，须与模型卷内已拉取模型一致（默认 `qwen2.5:7b`） |
| `EMBEDDING_MODEL` | 嵌入模型标签（默认 `bge-m3`，1024 维） |
| `OLLAMA_BASE_URL` | compose 已固定注入 `http://ollama:11434`，无需改 |
| `MAX_UPLOAD_MB` / `ALLOWED_EXTENSIONS` | 上传大小与白名单扩展名 |
| `DATA_DIR` | 容器内固定 `/app/backend/data`；宿主落点可用 compose 的 `DATA_DIR` 变量调整（默认 `./data`） |

## 七、常见问题

- **首次提问很慢或返回 503 超时**：纯 CPU 冷启动需把模型载入内存，qwen2.5:3b 实测
  约 150 秒、7b 更久。把 `.env` 的 `OLLAMA_TIMEOUT` 调到 300-600；纯 CPU 机器
  建议 8GB+ 内存跑 3b、16GB+ 再考虑 7b。模型常驻后后续问答明显加快。
- **健康检查 503，Milvus 项未就绪**：检查宿主机 `./data` 目录可写、磁盘未满；
  首次启动会在其中创建 `milvus_rag.db`。容器重启后已入库集合的重新载入由应用自动完成。
- **上传返回 422**：文件加密、损坏或解析后为空（如空表格、扫描件无文本层）。
  扫描件请先 OCR；系统不内置 OCR，以保持运行时零外网依赖。
- **所有问题都提示未检索到**：① 确认上传后文档状态 `success`；② 换更贴近文档
  原文的问法；③ 可调低 `RETRIEVE_SCORE_THRESHOLD`（默认 0.65，按实测校准；
  小语料库里无关中文问题的相似度基线可达 0.62）；
  ④ 确认健康检查中嵌入模型与 LLM 均为已安装状态。
- **问题与资料无关却返回了来源卡片**：阈值偏低，把 `RETRIEVE_SCORE_THRESHOLD`
  调到 0.68-0.7 并重启。注意即使误带弱相关片段，LLM 仍会按系统约束回复
  「根据现有资料无法回答该问题」，不会编造答案。
- **想换 LLM 模型**：模型卷内先有该模型（`docker compose exec ollama ollama pull <标签>`，
  需在联网环境），再改 `.env` 的 `LLM_MODEL` 后重启即可。**嵌入模型与维度不能中途
  更换**（`EMBEDDING_MODEL`/`EMBEDDING_DIM` 决定向量库 schema），换嵌入模型需清空
  `./data` 后重新上传全部文档。
- **8000 端口被占用**：修改 `docker-compose.yml` 中 `"8000:8000"` 左侧端口，
  或停止占用进程；容器内端口固定 8000 不变。
- **修改 SECRET_KEY 后所有用户被要求重新登录**：正常现象，旧 JWT 立即失效，
  数据库与文档不受影响。生产环境务必在首次部署时就设置随机长密钥。
- **Windows 本地开发（非容器）依赖装不上**：Milvus Lite 2.5.x 无 Windows wheel，
  必须使用 requirements 锁定的 `pymilvus==3.0.2` + `milvus-lite==3.2.2`（Python 3.11+）。

## 八、备份与运维

- 业务数据：备份宿主机 `./data` 目录即可（停机或低峰期拷贝）。
- 模型权重：`docker run --rm -v rag-personal_ollama_models:/v -v "$PWD/dist:/backup" --entrypoint tar ollama-cpu:0.40.5 czf /backup/models.tar.gz -C /v .`
- 日志：`docker compose logs -f app ollama`。
- 模型在纯 CPU 上首 token 较慢（冷启动 + 3B 推理实测约 2-3 分钟），属正常；常驻后问答明显加快。
- 交付后巡检：定期执行 `scripts/smoke_test.py --quick`（秒级，不调模型）即可覆盖
  服务、鉴权、入库与隔离主链路。
