# 代码变更部署指南

本文档说明：修改了项目代码后，如何让变动在容器中生效。面向两种场景：

- **开发机**：可联网，能重新拉取基础镜像和安装依赖。
- **离线交付机**：无法访问外网镜像源 / pip / npm 源。

---

## 一、构建机制总览

项目通过 `docker-compose.yml` 编排两个服务：

| 服务 | 镜像 | 说明 |
| --- | --- | --- |
| `app` | `rag-personal:0.1.0` | 由根目录 `Dockerfile` 多阶段构建：阶段 1 用 `node:20-alpine` 编译前端 → 输出到 `backend/static`；阶段 2 用 `python:3.11-slim` 运行 FastAPI，`COPY backend/` 进镜像。 |
| `ollama` | `ollama-cpu:0.40.5` | 由 `vendor/Dockerfile` 基于官方 linux-amd64 发布包构建（剔除 GPU 库）。 |

关键设计：

- 前端构建产物在**镜像构建阶段**生成，不依赖宿主机安装 node。
- 后端代码在**镜像构建阶段**被 `COPY` 进镜像，容器运行时从镜像读取，不挂载宿主源码。
- 业务数据（SQLite / Milvus Lite / 上传文件）通过卷挂载到 `./data`，与镜像解耦。
- 模型权重在 docker 命名卷 `rag-personal_ollama_models`，与镜像解耦。

因此，**改了代码必须重新构建镜像**才能生效（本地开发热重载除外，见第七节）。

---

## 二、改了后端代码（`backend/**/*.py`）

后端代码在镜像构建时被 `COPY backend/ /app/backend/` 打入镜像。改动后：

```bash
# 方式 A：一条命令重建并重启 app 服务（推荐）
docker compose up -d --build app

# 方式 B：分两步
docker compose build app
docker compose up -d app
```

`--build` 会重新执行 `Dockerfile`。由于 Docker 层缓存，未改动的层（基础镜像、依赖安装）会复用，
只有 `COPY backend/` 及其之后的层会重建，通常几十秒内完成。

验证：

```bash
docker compose logs -f app          # 确认启动日志
curl http://localhost:8000/api/health
```

> 注意：`ollama` 服务不受后端代码改动影响，无需重启。

---

## 三、改了前端代码（`frontend/**/*.vue` / `*.ts` / `*.css`）

前端代码在 `Dockerfile` 阶段 1 内被编译为静态文件，输出到容器内的
`/build/backend/static`，再 `COPY` 到运行时镜像。改动后同样需要重建镜像：

```bash
docker compose up -d --build app
```

构建过程会重新执行 `npm ci && npm run build`，生成新的静态产物。

> 如果只改了前端、没改后端，也必须重建 `app` 镜像——因为前端产物是在 `app` 镜像的
> 构建阶段生成的，没有独立的前端容器。

---

## 四、改了依赖（`requirements.txt` / `frontend/package.json`）

依赖变更会使 Docker 构建的缓存层失效，需要重新安装依赖：

### 4.1 Python 依赖（`requirements.txt`）

```bash
docker compose build --no-cache app    # 强制重新 pip install
docker compose up -d app
```

> 加 `--no-cache` 是为了确保 pip 真正重新解析依赖；如果只是新增一个包，
> 不加也能触发重建（因为 `COPY requirements.txt` 这一层的哈希变了）。

### 4.2 前端依赖（`frontend/package.json` / `package-lock.json`）

```bash
docker compose build app
docker compose up -d app
```

`Dockerfile` 中 `COPY frontend/package.json frontend/package-lock.json ./` 会因
文件内容变化而使缓存失效，自动重新 `npm ci`。

---

## 五、改了配置（`.env` / `docker-compose.yml`）

### 5.1 `.env` 变更

`.env` 由 `docker-compose.yml` 的 `env_file` 在容器启动时注入，**不进镜像**。
改完只需重启服务，无需重建镜像：

```bash
docker compose up -d        # compose 检测到 env 变化会自动重建容器
```

或显式重启：

```bash
docker compose restart app ollama
```

> 注意：改 `LLM_MODEL` / `EMBEDDING_MODEL` 后，确保 ollama 容器内已 `ollama pull`
> 对应模型；换嵌入模型还需清空 `./data` 后重新上传文档（schema 维度变化）。

### 5.2 `docker-compose.yml` 变更

端口映射、卷挂载、环境变量等 compose 配置变更后：

```bash
docker compose up -d        # 自动按新配置重建容器
```

如果改了 `app` 服务的 `build` 段（如基础镜像、构建参数），需加 `--build`。

---

## 六、增量更新（外网不可用时）

当开发机或交付机**无法访问外网镜像源**（拉不到 `node:20-alpine` /
`python:3.11-slim`，或 pip / npm 源不可达）时，用 `Dockerfile.update` 做增量更新：

### 6.1 原理

`Dockerfile.update` 基于**已存在的** `rag-personal:0.1.0` 镜像，仅覆盖
`backend/` 目录，不重新拉取基础镜像、不重新安装依赖：

```dockerfile
FROM rag-personal:0.1.0
USER root
COPY backend/ /app/backend/
RUN chown -R app:app /app/backend
USER app
```

### 6.2 前提

本机必须已有 `rag-personal:0.1.0` 镜像（首次完整构建过，或离线 `docker load` 过）。

### 6.3 操作步骤

**改了后端代码**：

```bash
docker build -f Dockerfile.update --pull=false -t rag-personal:0.1.0 .
docker compose up -d app
```

**改了前端代码**：前端必须在宿主机先构建出产物，再用增量镜像覆盖：

```bash
# 1. 宿主机安装前端依赖并构建（需 node 20+）
cd frontend
npm ci
npm run build          # 产物输出到 ../backend/static
cd ..

# 2. 用增量 Dockerfile 覆盖 backend/（含新构建的 static/）
docker build -f Dockerfile.update --pull=false -t rag-personal:0.1.0 .
docker compose up -d app
```

> `Dockerfile.update.dockerignore` 会排除 `backend/tests/`、`__pycache__/`、`data/`
> 等，避免把测试代码和运行时数据打进镜像。

### 6.4 增量更新的局限

- **不能变更依赖**：`requirements.txt` / `package.json` 的变更无法通过增量更新生效，
  必须用完整 `Dockerfile` 重建（需要外网或内网源）。
- **不能变更 Dockerfile 本身的结构**：如基础镜像、apt 安装项等。
- 前端变更需要宿主机有 node 环境来预构建。

---

## 七、本地开发热重载（不进容器）

开发阶段频繁改代码时，可不用 Docker，直接在宿主机运行以获得热重载：

### 7.1 后端

```powershell
# 1. 虚拟环境（Python 3.11+）
pip install -r requirements.txt

# 2. 启动 Ollama（另开终端）
ollama serve

# 3. 配置 .env（OLLAMA_BASE_URL=http://localhost:11434）
Copy-Item .env.example .env

# 4. 启动后端（--reload 热重载）
cd backend
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

改 `.py` 文件后 uvicorn 自动重载，无需重启。

### 7.2 前端

```powershell
cd frontend
npm install
npm run dev          # Vite 开发服务器，http://localhost:5173，HMR 热更新
```

Vite 已配置 `/api` 代理到 `http://localhost:8000`，前后端联调无需跨域配置。

> 前后端都在宿主机运行时，前端访问 `http://localhost:5173`，API 请求自动代理到
> 后端 8000 端口。生产部署时前端由 FastAPI 单端口托管，无代理。

---

## 八、离线环境：重新打包交付物

当代码变更需要交付到**纯离线环境**时，在联网打包机上重新生成镜像 tar：

### 8.1 重新构建并导出镜像

```bash
# 1. 用最新代码构建 app 镜像
docker compose build app

# 2. 导出镜像（ollama-cpu 镜像未变则不必重新导出）
docker save rag-personal:0.1.0 -o dist/app.tar
```

如果 `ollama-cpu` 镜像也有变更（如升级 ollama 版本），一并导出：

```bash
docker save rag-personal:0.1.0 ollama-cpu:0.40.5 -o dist/images.tar
```

### 8.2 在离线机上更新

```bash
# 1. 导入新镜像
docker load -i dist/app.tar         # 或 images.tar

# 2. 滚动重启（业务数据 ./data 与模型卷均保留）
docker compose up -d

# 3. 验证
curl http://localhost:8000/api/health
```

### 8.3 模型卷是否需要重新搬运

- 只改了代码、模型不变：**不需要**重新搬运模型卷，离线机原有 `rag-personal_ollama_models` 卷继续使用。
- 升级了 LLM 或嵌入模型：需在联网机重新 `ollama pull` 后重新打包模型卷（见 README「纯离线交付」章节）。

---

## 九、版本号管理

- `app` 镜像标签固定为 `rag-personal:0.1.0`，定义在 `docker-compose.yml` 的 `image` 字段。
- 升级版本时：修改 `docker-compose.yml` 中 `app` 服务的 `image` 标签（如 `0.2.0`），
  同时更新 `Dockerfile` 注释中的版本号。
- 版本号变更不影响数据卷与模型卷，只需 `docker compose up -d --build` 即可。

---

## 十、常见问题

### Q1：改了代码但容器里没生效？

检查是否用了旧镜像。确认构建并重启：

```bash
docker compose up -d --build app
docker compose exec app cat /app/backend/main.py | head -5   # 确认代码已更新
```

### Q2：增量更新后前端没变？

增量更新 `Dockerfile.update` 只 `COPY backend/`，前端产物必须先在宿主机
`npm run build` 输出到 `backend/static`，再执行增量构建。否则容器内仍是旧的
`static/`。

### Q3：重建镜像后数据丢了？

不会。业务数据在 `./data`（宿主目录挂载），模型在命名卷，均不随镜像重建丢失。
只有删除 `./data` 目录或 `docker volume rm rag-personal_ollama_models` 才会丢数据。

### Q4：`docker compose build` 卡在拉取基础镜像？

检查网络与镜像源。可在 `.env` 中配置内网源：

```env
PIP_INDEX_URL=http://内网pip源/simple
NPM_REGISTRY=http://内网npm源
```

完全离线时改用 `Dockerfile.update` 增量更新（第六节）。

### Q5：如何确认当前运行的镜像版本？

```bash
docker compose images          # 查看各服务使用的镜像与标签
docker inspect rag-personal-app --format '{{.Image}}'   # 查看实际镜像 ID
```
