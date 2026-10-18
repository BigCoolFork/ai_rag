# 精简私有化 RAG 知识库系统 — 项目实施计划

> 本文件仅为实施计划，确认前不编写任何业务代码。

## 一、项目目标与约束

构建一套**单机私有化、运行时零外网依赖**的 RAG 知识库系统：

- 支持 PDF / Word 上传，自动解析、清洗、切片、向量化入库
- 基于本地 Ollama 模型进行问答，答案附带「文档名 + 页码」引用以抑制幻觉
- 多用户登录，知识库按用户物理隔离，用户只能管理自己的文档
- Docker 一键部署；LLM 与 Embedding 全部本地运行

### 技术栈（锁定）

| 层 | 选型 | 说明 |
|---|---|---|
| 后端框架 | FastAPI + Uvicorn | 模块化路由，单端口对外 |
| LLM 编排 | LangChain | `langchain_ollama.OllamaLLM` |
| Embedding | Ollama 本地模型 `nomic-embed-text`（768 维） | 经 `langchain_ollama.OllamaEmbeddings` 调用 |
| LLM 模型 | `qwen2.5:7b`（默认，可配置） | Ollama 本地推理，CPU/GPU 均可 |
| 向量库 | Milvus Lite（`milvus-lite`，嵌入式，经 `langchain-milvus` 使用） | 单文件本地库，不部署分布式集群 |
| 关系库 | SQLite（标准库 `sqlite3`） | 用户表、文档登记表 |
| 前端 | Vue3 + TypeScript + Vite + Element Plus + Pinia | 构建为静态文件，由 FastAPI 托管，**单端口交付** |
| 部署 | Dockerfile + docker-compose（app + ollama 两个服务） | 数据卷持久化，运行时离线 |

### 硬约束

1. 任何代码路径不得调用外网大模型 API；LLM/Embedding 端点只允许指向配置的 Ollama 地址。
2. 不引入 MySQL/Postgres/Redis 等额外中间件（Milvus Lite 与 SQLite 均为嵌入式）。
3. 运行时所需一切（前端构建产物、Python 依赖、Ollama 模型）均随镜像/挂载卷就位。
4. 后端按「薄入口 + 路由层 + 服务层」拆分，每个模块独立文件，注释清晰。

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    浏览器（前端 SPA）                     │
│         登录/注册 │ 文档上传管理 │ 问答(答案+引用)          │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / JWT（单端口 8000）
┌────────────────────────▼────────────────────────────────┐
│                      FastAPI 后端                         │
│  routers/ ── auth │ documents │ chat（鉴权依赖注入）       │
│  services/ ── ingest(编排)                                │
│      ├─ document_parser / text_splitter（解析清洗切片）    │
│      ├─ vector_store（Milvus Lite 入库/删除/检索）         │
│      └─ qa（检索→拼上下文→Prompt→OllamaLLM→引用拼装）      │
│  core/ ── security(JWT/密码哈希) · deps(当前用户)          │
│  SQLite: users / documents     storage: 原始文件落盘      │
└──────────┬─────────────────────────────┬────────────────┘
           │ pymilvus(嵌入式)             │ HTTP 11434
┌──────────▼──────────┐        ┌──────────▼────────────────┐
│  milvus_lite 文件库  │        │  Ollama 容器/本机服务       │
│ 按用户 1 个 collection│        │ qwen2.5:7b + nomic-embed  │
└─────────────────────┘        └───────────────────────────┘
```

### 多用户隔离方案（已定）

**每个用户一个 Milvus collection**，命名 `kb_user_{uid}`：

- 检索天然只发生在用户自己的 collection 内，不依赖过滤表达式的正确性，隔离最可靠；
- collection 在用户首次上传时惰性创建，schema 固定并带版本后缀设计（见风险项）；
- 向量与元数据字段：

```text
id(pk)          自增主键
vector(768)     nomic-embed-text 向量
text            切片原文
user_id         用户 id（冗余，便于审计）
doc_id          文档登记 id（删除文档时按此过滤删除）
doc_name        原始文档名（引用展示）
page            页码（PDF 为整数；Word 为 null，另用 section 定位）
section         Word 最近标题/段落定位；PDF 为空
source_path     容器内原始文件存储路径
chunk_index     文档内切片序号
```

---

## 三、目录结构

```text
rag_personal/
├── plan.md                     # 本计划
├── README.md                   # 部署与使用文档（含离线导入模型步骤）
├── requirements.txt            # Python 依赖（锁定主版本）
├── .env.example                # 配置样例（OLLAMA_BASE_URL、模型名、密钥等）
├── .gitignore
├── .dockerignore
├── Dockerfile                  # 多阶段：node 构建前端 → python-slim 运行
├── docker-compose.yml          # app + ollama 两服务，数据卷持久化
├── scripts/
│   └── smoke_test.py           # 端到端冒烟脚本（注册→上传→问答→隔离验证）
├── backend/
│   ├── main.py                 # 薄入口：生命周期、CORS、路由挂载、SPA 静态托管
│   ├── config.py               # pydantic-settings 读取环境变量；启动期校验
│   ├── database.py             # SQLite 连接、建表初始化
│   ├── schemas/                # Pydantic 请求/响应模型
│   │   ├── auth.py
│   │   ├── document.py
│   │   └── chat.py
│   ├── core/
│   │   ├── security.py         # PBKDF2/bcrypt 密码哈希、JWT 签发与校验
│   │   └── deps.py             # 依赖注入：DB、当前登录用户
│   ├── routers/
│   │   ├── auth.py             # 注册 / 登录 / 当前用户
│   │   ├── documents.py        # 上传 / 列表 / 删除
│   │   └── chat.py             # 问答（含 SSE 流式）/ 健康检查
│   ├── services/
│   │   ├── ollama_client.py    # LLM/Embedding 单例；构造期探测模型存在性
│   │   ├── document_parser.py  # PDF(PyMuPDF 按页) / DOCX(python-docx) 加载
│   │   ├── text_splitter.py    # 文本清洗 + 中文友好递归切片
│   │   ├── vector_store.py     # collection 创建/入库/按 doc 删除/TopK 检索
│   │   ├── ingest.py           # 上传编排：落盘→去重→解析→切片→入库→登记
│   │   └── qa.py               # 检索→Prompt 模板→OllamaLLM→答案+引用
│   ├── static/                 # 前端构建产物（构建时生成，Git 忽略）
│   └── data/                   # 运行时数据（Git 忽略）
│       ├── app.db              # SQLite
│       ├── milvus_rag.db       # Milvus Lite 数据文件
│       └── uploads/{user_id}/  # 原始文档（SHA256 命名）
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    └── src/
        ├── main.ts
        ├── App.vue
        ├── router/index.ts     # 路由守卫（无 token 跳登录）
        ├── api/client.ts       # axios 实例、token 注入、401 处理
        ├── stores/auth.ts      # Pinia 用户状态
        ├── layouts/MainLayout.vue
        └── views/
            ├── Login.vue
            ├── Register.vue
            ├── Documents.vue   # 上传(拖拽)、列表、页数/状态、删除
            └── Chat.vue        # 问答区 + 引用角标 + 来源卡片
```

### SQLite 表结构

```text
users
  id INTEGER PK AUTOINCREMENT
  username TEXT UNIQUE NOT NULL
  password_hash TEXT NOT NULL
  created_at TEXT NOT NULL

documents
  id INTEGER PK AUTOINCREMENT
  user_id INTEGER NOT NULL            -- 外键逻辑关联 users.id
  original_name TEXT NOT NULL         -- 展示名，如 制度汇编.pdf
  stored_name TEXT NOT NULL           -- 落盘名 {sha256}.{ext}
  source_path TEXT NOT NULL           -- 容器内绝对路径
  file_hash TEXT NOT NULL             -- SHA256，同用户内去重
  file_type TEXT NOT NULL             -- pdf / docx
  chunk_count INTEGER NOT NULL DEFAULT 0
  status TEXT NOT NULL                -- processing / ready / failed
  created_at TEXT NOT NULL
  UNIQUE(user_id, file_hash)          -- 同一用户重复文件直接复用，不重复向量化
```

---

## 四、接口设计（REST + JWT）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/auth/register` | 否 | 用户名+密码注册 |
| POST | `/api/auth/login` | 否 | 返回 `access_token` |
| GET | `/api/auth/me` | 是 | 当前用户信息 |
| POST | `/api/documents/upload` | 是 | multipart 上传 PDF/DOCX，异步处理可先返回 processing，简化版同步处理后返回 ready |
| GET | `/api/documents` | 是 | 当前用户文档列表（名称、切片数、状态、时间） |
| DELETE | `/api/documents/{doc_id}` | 是 | 仅属主可删；同步删除文件、登记记录、向量 |
| POST | `/api/chat/ask` | 是 | 入参 question；返回 answer + sources[] |
| GET | `/api/chat/stream` | 是（SSE，token 走 query/header） | 流式答案，结束事件携带 sources |
| GET | `/api/health` | 否 | 后端、SQLite、Ollama 连通性、两个模型是否就位 |

**问答响应结构（引用可追溯）：**

```json
{
  "answer": "……根据制度汇编第 3 页……[1]……",
  "sources": [
    {"index": 1, "doc_name": "制度汇编.pdf", "page": 3, "section": null,
     "snippet": "命中片段前 80 字…", "score": 0.82}
  ]
}
```

**Prompt 约束（降低幻觉）：** 系统提示词强制「只能依据提供的资料回答；资料不足必须明确回答不知道；每个结论用 [n] 标注来源」。检索 TopK=4，相似度阈值过滤，命中为空时直接返回「知识库中未检索到相关内容」，不调用 LLM 编造。

---

## 五、开发顺序与任务清单

按依赖顺序推进，每步可独立验证：

1. **项目脚手架**：目录、`requirements.txt`、`.env.example`、`config.py`（含启动期配置校验）、`main.py` 骨架、CORS、统一错误处理。
2. **存储与认证**：`database.py` 建表；`security.py`（密码哈希 + JWT）；`deps.py`；auth 路由（注册/登录/me）。
3. **Ollama 服务层**：`ollama_client.py` 单例封装 `OllamaLLM` 与 `OllamaEmbeddings`；构造期探测 `OLLAMA_BASE_URL` 与 `nomic-embed-text`、LLM 模型是否存在，缺失时给出明确报错（不静默运行到半路）；健康检查接口。
4. **文档解析与切片**：
   - PDF：PyMuPDF 按页提取，逐页生成带 `page` 元数据的 Document；
   - DOCX：python-docx 遍历段落与表格，记录最近标题作为 `section`，`page=null`；
   - 清洗：去页眉页脚噪声、多余空白、控制字符；中文友好分隔符的 `RecursiveCharacterTextSplitter`（chunk 500 / overlap 50，可调）。
5. **向量库封装**：`vector_store.py`——惰性建 collection（带 schema 版本命名）、批量入库、按 `doc_id` 删向量、带 `doc_name/page/section` 元数据的 TopK 检索。
6. **入库编排与文档管理**：`ingest.py`（SHA256 落盘 → 同用户去重 → 解析切片 → 向量化 → 写登记）；documents 路由（上传/列表/删除联动向量与文件）。
7. **问答链路**：`qa.py` 检索→阈值过滤→组装带编号上下文→约束性 Prompt→`OllamaLLM` 生成→拼装 sources；chat 路由含 SSE 流式（检索逻辑只写一份，流式/非流式复用）。
8. **前端**：Vite 工程骨架 → 登录/注册（Element Plus 表单）→ 主布局（顶部用户栏、侧边导航）→ 文档页（拖拽上传、进度、列表、删除）→ 问答页（流式渲染、`[n]` 角标、来源卡片：文档名+页码）；路由守卫与 401 处理。简约大气、间距宽松、按钮尺寸兼顾触摸。
9. **容器化与离线部署**：多阶段 Dockerfile（`node:20-alpine` 构建 → `python:3.11-slim` 运行，托管 static）；compose 编排 app 与 `ollama/ollama`（GPU 段注释可选），挂载 `./data` 与模型卷；README 写明：有网机构建/拉取并 `docker save` + `ollama` 模型导出 → 离线机 `docker load` + 模型卷导入 → `docker compose up -d` 全离线启动。
10. **验收与文档**：`scripts/smoke_test.py` 端到端验证；README 完善（接口说明、配置项、常见问题）。

### requirements.txt 主要依赖（实施时锁定兼容版本）

```text
fastapi
uvicorn[standard]
python-multipart
pydantic
pydantic-settings
langchain
langchain-core
langchain-ollama
langchain-milvus
milvus-lite
pymilvus
PyMuPDF
python-docx
pyjwt
bcrypt
httpx
```

---

## 六、验收 Checklist

**功能验收**

- [x] 可注册、登录、退出；错误密码/过期 token 被正确拒绝（401）（smoke_test 认证节实测；过期 token 由单测覆盖）
- [x] 上传 PDF 后，文档列表可见，状态 ready，切片数 > 0（smoke_test 实测）
- [x] 上传 DOCX 后可正常入库与检索（来源显示文档名 + 章节定位）（smoke_test 实测 section 非空）
- [x] Milvus 元数据包含：文档名、页码（PDF）、来源路径（入库字段单测覆盖；page=2 来源 smoke 实测）
- [x] 提问能返回基于文档内容的答案，且附带 sources（文档名 + 页码 + 片段）
- [x] 资料中没有答案时，明确回复未检索到，不编造（阈值校准 0.65 后无关问题 hit=false 实测）
- [x] 流式接口逐 token 输出，结束时带出来源（SSE 帧序列 smoke 实测）
- [x] 删除文档后：登记记录、落盘文件、Milvus 向量全部清除，再问相关问题无命中（smoke_test 实测：200 删除、列表消失、重复删 404、再问 hit=false）
- [x] 同一用户重复上传同一文件被去重，不产生重复向量（smoke_test 实测 duplicated=true；夹具须复用同一字节流）

**权限隔离验收**

- [x] A、B 两账号：A 上传的文档在 B 的列表中不可见（smoke_test 实测）
- [x] B 用 A 的 doc_id 调删除接口返回 403/404，数据不受影响（smoke_test 实测 404）
- [x] B 的问答检索不到 A 的任何文档内容（smoke_test 实测 hit=false）

**部署与离线验收**

- [x] `docker compose up -d` 一条命令启动 app + ollama，浏览器单端口可用（两容器 healthy，/api/health 五项全绿；注册→上传→问答闭环实测通过）
- [x] 容器重启后用户、文档、向量数据不丢失（卷持久化；down/up 后旧 Milvus 集合经 load 自愈，检索正常）
- [x] 离线状态下：注册登录、上传解析向量化、问答全链路正常（镜像 load + 模型卷恢复后全程无外网；运行时仅访问 http://ollama:11434）
- [x] 代码中不存在任何外网模型 API 地址；Ollama 地址仅由配置指定
- [x] README 含完整的「有网机导出 → 离线机导入」步骤（含自建 ollama-cpu 镜像、模型卷 tar 导入、CPU 超时建议）

**工程质量验收**

- [x] 后端模块独立文件、分层清晰（入口/路由/服务/核心），关键逻辑有注释
- [x] `requirements.txt`、`.env.example`、README 齐全
- [x] `scripts/smoke_test.py` 跑通主流程与隔离场景（25 项断言全过：6 大场景；`--quick` 19 项 9 秒）
- [x] 前端页面无信息冗余、布局简约、操作路径短（符合既定 UI 偏好）

---

## 七、风险与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | `milvus-lite` 对 Windows 原生支持可能不完整 | 生产/交付以 Linux 容器为准（compose 一键）；Windows 本地开发同样在 Dev Container/容器内运行后端，README 注明 |
| 2 | langchain / langchain-ollama / langchain-milvus / pymilvus 版本兼容敏感 | requirements 锁定一组实测兼容版本并在 README 标注；先做最小连通验证再铺开业务 |
| 3 | Word 格式没有可靠页码 | DOCX 引用以「文档名 + 最近章节标题 + 段落」定位，page 字段为 null，前端区分展示，不伪造页码 |
| 4 | Ollama 模型体积大（7B 模型数 GB），离线机无法 pull | compose 挂载模型卷；README 提供 `ollama pull`（有网机）→ 模型目录/卷搬运 → 离线机挂载的标准流程；健康检查明确提示缺哪个模型 |
| 5 | 无 GPU 时 7B 推理慢 | 模型名全部走环境变量配置，可换更小量化模型；接口默认支持流式改善体感；compose 提供 GPU 透传可选段 |
| 6 | Milvus schema 后续演进导致旧 collection 不兼容 | collection 名含 schema 版本（如 `kb_user_{uid}_v1`），启动时检测并给出明确迁移指引，不把手工删库留给用户踩坑 |
| 7 | Docker **构建期**需拉取 pip/npm 依赖，与「全程离线」的表述冲突 | 界定：离线指**运行时**零外网；README 同时提供离线机构建方案（内网源 / wheelhouse / `docker save/load` 镜像搬运），由实施时按客户环境二选一 |
| 8 | 文件上传安全（路径穿越、超大文件、恶意类型） | 落盘一律采用 SHA256 重命名（不信任原文件名）；白名单扩展名校验 + MIME 复核；配置上传大小上限；按用户目录隔离存储 |
| 9 | PowerShell 执行策略可能拦截本地脚本验证 | 验证以容器内命令和冒烟脚本为准，不以被拦截命令的退出码判断成功 |

---

## 八、待确认事项

1. LLM 默认模型 `qwen2.5:7b` 是否认可？（也可换 qwen2.5:3b / 其他已下载模型，均通过环境变量配置，不改代码）
2. 前端采用 **Vue3 + Element Plus 构建后由 FastAPI 单端口托管**（推荐，交付简单），是否认可？
3. 上传后处理采用**同步入库**（文件小、实现简单，上传请求等待至 ready）还是需要后台异步任务+进度轮询？默认同步。
4. 离线交付方式倾向哪种：① 目标机构建期可访问内网镜像源；② 纯堡垒机搬运镜像（docker save/load）。将影响 README 详写哪条路径。
```
