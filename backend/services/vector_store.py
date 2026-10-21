"""Milvus Lite 向量库封装。

设计要点：
- 单机嵌入式 Milvus Lite（数据文件由 settings.milvus_uri 指定），
  不部署任何分布式集群；
- 每个用户一个 collection，命名 ``user_{user_id}_rag``，首次上传时
  惰性创建，已存在则复用——检索天然按用户物理隔离；
- Schema v1 字段固定（见下方 FIELD_* 常量），向量维度取
  settings.embedding_dim（bge-m3 = 1024）；
- 上层业务只暴露 insert / delete / search 三个函数与 SearchResult，
  pymilvus 原生异常一律在本模块捕获并转换为可读业务异常；
- probe_milvus() 永不抛异常，供 /api/health 与启动期探测使用。

注意：page / section 对部分文档类型为 None（DOCX/XLSX 无页码，
PDF 无章节），故这两个字段在 schema 中显式 nullable=True。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from config import settings
from services.document_parser import DocumentFragment
from services.ollama_client import get_embeddings

logger = logging.getLogger(__name__)

# Milvus 连接别名（本系统只使用一个本地连接）
_ALIAS = "rag_local"

# ---------------------------------------------------------------------------
# Schema v1 字段定义（字段名即入库/检索的唯一白名单，插入时严格按序投影）
# ---------------------------------------------------------------------------
FIELD_PK = "doc_chunk_id"   # INT64 主键，auto_id 由 Milvus 生成
FIELD_DOC_ID = "doc_id"      # STRING，同一份文档所有 chunk 共用
FIELD_SOURCE = "source"      # STRING，原始文件落盘路径
FIELD_DOC_NAME = "doc_name"  # STRING，原始文档名（引用展示）
FIELD_FILE_TYPE = "file_type"  # pdf / docx / xlsx
FIELD_PAGE = "page"          # INT64 nullable，PDF 真实页码，其余为 None
FIELD_SECTION = "section"    # STRING nullable，DOCX 标题 / XLSX 工作表名
FIELD_TEXT = "text"          # STRING，切片原文
FIELD_VECTOR = "vector"      # FLOAT_VECTOR，维度 = settings.embedding_dim

# 插入时的列顺序（auto_id 主键除外）——必须与 build_schema 中声明顺序一致
_INSERT_FIELDS: list[str] = [
    FIELD_DOC_ID, FIELD_SOURCE, FIELD_DOC_NAME, FIELD_FILE_TYPE,
    FIELD_PAGE, FIELD_SECTION, FIELD_TEXT, FIELD_VECTOR,
]
# 检索回传的元数据字段（不含向量本身）
_OUTPUT_FIELDS: list[str] = [
    FIELD_PK, FIELD_DOC_ID, FIELD_SOURCE, FIELD_DOC_NAME, FIELD_FILE_TYPE,
    FIELD_PAGE, FIELD_SECTION, FIELD_TEXT,
]

# VARCHAR 长度上限（Milvus 要求 VARCHAR 显式 max_length，text 上限 65535）
_MAX_DOC_ID = 128
_MAX_SOURCE = 1024
_MAX_DOC_NAME = 512
_MAX_SECTION = 512
_MAX_TEXT = 65535

# doc_id 只允许安全字符（上层为 SQLite 文档 id 转字符串），用于删除表达式
_DOC_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 相似度度量：COSINE，分数即余弦相似度（越高越相关，与 0.4 阈值口径一致）
_METRIC_TYPE = "COSINE"


# ---------------------------------------------------------------------------
# 业务异常与状态结构
# ---------------------------------------------------------------------------

class VectorStoreError(Exception):
    """向量库业务异常基类（信息可直接展示/记录）。"""


class VectorStoreConnectionError(VectorStoreError):
    """无法连接/初始化 Milvus Lite。"""


class SchemaIncompatibleError(VectorStoreError):
    """已存在 collection 的字段或向量维度与当前 Schema v1 不兼容。"""


class EmbeddingError(VectorStoreError):
    """调用本地 Ollama 嵌入模型失败。"""


@dataclass
class SearchResult:
    """单条向量检索命中。"""

    chunk_id: int
    text: str
    score: float
    doc_id: str
    doc_name: str
    source: str
    file_type: str
    page: int | None
    section: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "score": self.score,
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "source": self.source,
            "file_type": self.file_type,
            "page": self.page,
            "section": self.section,
        }


@dataclass
class MilvusStatus:
    """Milvus 健康探测结果（不抛异常）。"""

    ready: bool
    detail: str
    uri: str
    collections: list[str]
    incompatible_collections: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "uri": self.uri,
            "collections": self.collections,
            "incompatible_collections": self.incompatible_collections,
        }


# ---------------------------------------------------------------------------
# 命名与 schema
# ---------------------------------------------------------------------------

def collection_name(user_id: int) -> str:
    """生成用户专属 collection 名：user_{user_id}_rag。"""
    if not isinstance(user_id, int) or user_id <= 0:
        raise VectorStoreError(f"非法的用户 id：{user_id!r}")
    return settings.collection_name_template.format(user_id=user_id)


def build_schema(dim: int) -> Any:
    """构建 Schema v1（pymilvus CollectionSchema）。"""
    from pymilvus import CollectionSchema, DataType, FieldSchema

    fields = [
        FieldSchema(name=FIELD_PK, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=FIELD_DOC_ID, dtype=DataType.VARCHAR, max_length=_MAX_DOC_ID),
        FieldSchema(name=FIELD_SOURCE, dtype=DataType.VARCHAR, max_length=_MAX_SOURCE),
        FieldSchema(name=FIELD_DOC_NAME, dtype=DataType.VARCHAR, max_length=_MAX_DOC_NAME),
        FieldSchema(name=FIELD_FILE_TYPE, dtype=DataType.VARCHAR, max_length=16),
        FieldSchema(name=FIELD_PAGE, dtype=DataType.INT64, nullable=True),
        FieldSchema(name=FIELD_SECTION, dtype=DataType.VARCHAR,
                    max_length=_MAX_SECTION, nullable=True),
        FieldSchema(name=FIELD_TEXT, dtype=DataType.VARCHAR, max_length=_MAX_TEXT),
        FieldSchema(name=FIELD_VECTOR, dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    return CollectionSchema(
        fields=fields,
        description=f"RAG user knowledge base schema {settings.schema_version}",
    )


def _validate_doc_id(doc_id: Any) -> str:
    """校验并规范化 doc_id，防止删除表达式注入。"""
    doc_id_str = str(doc_id)
    if not _DOC_ID_PATTERN.match(doc_id_str):
        raise VectorStoreError(f"非法的文档 id：{doc_id_str!r}")
    return doc_id_str


# ---------------------------------------------------------------------------
# 连接管理（底层 pymilvus 操作集中于此，便于测试替换）
# ---------------------------------------------------------------------------

_connected: bool = False
# 已通过 schema 自检的 collection 名缓存（避免每次检索重复 RPC）
_verified: set[str] = set()


def _connect() -> str:
    """建立（或复用）到 Milvus Lite 的连接；失败转可读异常。"""
    global _connected
    if _connected:
        return _ALIAS

    settings.setup_paths()
    try:
        from pymilvus import connections

        # uri 指向本地 .db 文件时，pymilvus 自动以 milvus-lite 嵌入式模式打开
        connections.connect(alias=_ALIAS, uri=str(settings.milvus_uri))
        _connected = True
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常，不泄露原生堆栈
        raise VectorStoreConnectionError(
            f"无法连接 Milvus Lite（{settings.milvus_uri}）："
            f"{type(exc).__name__}: {exc}。请确认已安装 milvus-lite 且数据目录可写。"
        ) from exc
    return _ALIAS


def reset_connection_cache() -> None:
    """重置连接与校验缓存（主要供单元测试使用）。"""
    global _connected
    _connected = False
    _verified.clear()


def _has_collection(name: str) -> bool:
    """collection 是否存在。"""
    from pymilvus import utility

    return bool(utility.has_collection(name, using=_connect()))


def _list_collection_names() -> list[str]:
    """列出全部 collection 名。"""
    from pymilvus import utility

    return list(utility.list_collections(using=_connect()))


def _instantiate_collection(name: str) -> Any:
    """打开已存在的 collection（不创建）。"""
    from pymilvus import Collection

    return Collection(name=name, using=_connect())


def _ensure_loaded(collection: Any) -> Any:
    """确保 collection 已载入内存。

    Milvus Lite 重启进程后，磁盘上的旧 collection 为 released 状态，
    直接 search/get/query 会报 code=101（须先 load）。每次打开已存在集合时
    显式检查并按需 load；已 Loaded 时为本地廉价查询，不重复加载。
    """
    from pymilvus import utility

    try:
        from pymilvus.client.types import LoadState

        loaded = utility.load_state(collection.name, using=_connect()) is LoadState.Loaded
    except Exception:  # noqa: BLE001 - 状态查询失败时退化为直接 load（load 幂等）
        loaded = False
    if not loaded:
        collection.load()
    return collection


def _create_new_collection(name: str) -> Any:
    """按 Schema v1 创建集合并建立向量索引、加载。"""
    from pymilvus import Collection

    schema = build_schema(settings.embedding_dim)
    collection = Collection(name=name, schema=schema, using=_connect())
    collection.create_index(
        field_name=FIELD_VECTOR,
        index_params={
            "index_type": "AUTOINDEX",
            "metric_type": _METRIC_TYPE,
            "params": {},
        },
    )
    collection.load()
    logger.info("已创建 Milvus collection：%s（dim=%d, metric=%s）",
                name, settings.embedding_dim, _METRIC_TYPE)
    _verified.add(name)
    return collection


# ---------------------------------------------------------------------------
# Schema 兼容性自检
# ---------------------------------------------------------------------------

def verify_collection_schema(collection: Any, raise_on_error: bool = True) -> str | None:
    """核对已存在 collection 的字段集合与向量维度。

    :return: 兼容时返回 None；不兼容且 raise_on_error=False 时返回原因描述
    :raises SchemaIncompatibleError: 不兼容且 raise_on_error=True
    """
    existing_fields = {field.name: field for field in collection.schema.fields}
    missing = [name for name in _INSERT_FIELDS + [FIELD_PK]
               if name not in existing_fields]

    reason: str | None = None
    if missing:
        reason = (
            f"collection「{collection.name}」缺少字段 {missing}，"
            f"与当前 Schema {settings.schema_version} 不兼容。"
        )
    else:
        vector_field = existing_fields[FIELD_VECTOR]
        actual_dim = int(dict(vector_field.params).get("dim", 0))
        if actual_dim != settings.embedding_dim:
            reason = (
                f"collection「{collection.name}」向量维度为 {actual_dim}，"
                f"与嵌入模型 {settings.embedding_model} 的维度 "
                f"{settings.embedding_dim} 不一致。"
            )

    if reason is None:
        return None
    hint = (
        f"{reason}处理建议：嵌入模型与维度不可中途变更；请让管理员备份后删除该旧集合"
        f"（数据将按新 Schema 重新入库），或更换 collection 命名模板/嵌入模型配置。"
    )
    if raise_on_error:
        raise SchemaIncompatibleError(hint)
    return hint


def _get_existing_collection(user_id: int) -> Any | None:
    """打开用户 collection（只读路径使用）；不存在返回 None，不自动创建。"""
    name = collection_name(user_id)
    if not _has_collection(name):
        return None
    collection = _instantiate_collection(name)
    if name not in _verified:
        verify_collection_schema(collection, raise_on_error=True)
        _verified.add(name)
    return _ensure_loaded(collection)


def get_or_create_collection(user_id: int) -> Any:
    """获取用户 collection，不存在则按 Schema v1 创建（入库路径使用）。"""
    name = collection_name(user_id)
    if _has_collection(name):
        collection = _instantiate_collection(name)
        if name not in _verified:
            verify_collection_schema(collection, raise_on_error=True)
            _verified.add(name)
        return _ensure_loaded(collection)
    return _create_new_collection(name)


# ---------------------------------------------------------------------------
# 嵌入（复用任务3的 Ollama 单例，失败转业务异常）
# ---------------------------------------------------------------------------

def _embed_texts(texts: list[str], progress_callback: Any = None) -> list[list[float]]:
    """分批生成文档向量；单批失败/维度不符抛出 EmbeddingError。

    大文档会产生上千条切片，一次性塞给 Ollama /api/embed 在 CPU 上要十几分钟
    并触发 ReadTimeout。此处按 settings.embed_batch_size 分批请求，每批独立
    超时可控，全部拼接后返回（与原接口等价）。

    :param progress_callback: 可选 ``(processed, total)`` 回调，每批完成后触发；
        回调抛出异常会中断嵌入（上层据此实现「处理中被删除则中止」）。
    """
    batch_size = max(1, settings.embed_batch_size)
    vectors: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        batch = texts[start:start + batch_size]
        try:
            vectors.extend(get_embeddings().embed_documents(batch))
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(
                f"调用本地嵌入模型 {settings.embedding_model} 生成文档向量失败"
                f"（批次 {start // batch_size + 1}，{len(batch)} 条）："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if progress_callback is not None:
            progress_callback(min(start + batch_size, total), total)
    return _check_vector_dim(vectors, expect_count=total)


def _embed_query(text: str) -> list[float]:
    """生成单条查询向量。"""
    try:
        vector = get_embeddings().embed_query(text)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(
            f"调用本地嵌入模型 {settings.embedding_model} 生成查询向量失败："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return _check_vector_dim([vector], expect_count=1)[0]


def _check_vector_dim(
    vectors: list[list[float]], expect_count: int
) -> list[list[float]]:
    """校验向量条数与维度，杜绝维度错位写入。"""
    if len(vectors) != expect_count:
        raise EmbeddingError(
            f"嵌入返回向量数 {len(vectors)} 与文本数 {expect_count} 不一致。"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != settings.embedding_dim:
            raise EmbeddingError(
                f"第 {index} 条向量维度为 {len(vector)}，"
                f"与配置的 EMBEDDING_DIM={settings.embedding_dim} 不一致，"
                "请检查嵌入模型与 Milvus schema 是否匹配。"
            )
    return vectors


# ---------------------------------------------------------------------------
# 上层业务接口
# ---------------------------------------------------------------------------

def insert_document_chunks(
    user_id: int,
    doc_id: Any,
    chunks: list[DocumentFragment],
    progress_callback: Any = None,
) -> int:
    """将一份文档的切片批量向量化并写入用户专属 collection。

    :param user_id: 用户 id（决定 collection 物理隔离边界）
    :param doc_id: 文档业务 id（SQLite documents.id），同文档所有 chunk 共用
    :param chunks: 任务4 输出的 DocumentFragment 切片列表
    :param progress_callback: 可选回调 ``(processed, total) -> None``，
        每完成一批嵌入后调用；回调若抛出异常将中断入库（用于处理中删除场景）
    :return: 实际写入的 chunk 数
    """
    doc_id_str = _validate_doc_id(doc_id)
    if not chunks:
        raise VectorStoreError("没有可入库的文档切片。")

    vectors = _embed_texts(
        [chunk.text for chunk in chunks], progress_callback=progress_callback
    )
    collection = get_or_create_collection(user_id)

    # 严格按 schema 列顺序构造列数据（auto_id 主键不传）
    columns: list[list[Any]] = [
        [doc_id_str] * len(chunks),
        [chunk.source_path[:_MAX_SOURCE] for chunk in chunks],
        [chunk.doc_name[:_MAX_DOC_NAME] for chunk in chunks],
        [chunk.file_type for chunk in chunks],
        [chunk.page for chunk in chunks],       # None 进入 nullable 字段
        [(chunk.section or None) and chunk.section[:_MAX_SECTION] for chunk in chunks],
        [chunk.text[:_MAX_TEXT] for chunk in chunks],
        vectors,
    ]

    try:
        collection.insert(columns)
        collection.flush()
    except Exception as exc:  # noqa: BLE001 - 原生异常不对外暴露
        raise VectorStoreError(
            f"写入 Milvus 失败（user_id={user_id}, doc_id={doc_id_str}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    logger.info("doc_id=%s 向量化入库完成：%d 个切片 → collection %s",
                doc_id_str, len(chunks), collection.name)
    return len(chunks)


def delete_document_by_docid(user_id: int, doc_id: Any) -> int:
    """删除某用户下整份文档的全部 chunk；collection 不存在时幂等返回 0。

    :return: 删除的记录数（Milvus 无精确计数时返回 -1 表示已提交删除）
    """
    doc_id_str = _validate_doc_id(doc_id)
    collection = _get_existing_collection(user_id)
    if collection is None:
        return 0

    expr = f'{FIELD_DOC_ID} == "{doc_id_str}"'
    try:
        result = collection.delete(expr)
        collection.flush()
        deleted = int(getattr(result, "delete_count", -1) or -1)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(
            f"删除文档向量失败（user_id={user_id}, doc_id={doc_id_str}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    logger.info("已删除 doc_id=%s 的向量（collection=%s, count=%s）",
                doc_id_str, collection.name, deleted)
    return deleted


def vector_search(
    user_id: int,
    query_text: str,
    top_k: int | None = None,
) -> list[SearchResult]:
    """在用户专属 collection 内执行 TopK 相似度检索。

    - 用户从未上传（collection 不存在）时直接返回空列表，不自动建集合；
    - 分数为 COSINE 相似度，越高越相关；阈值过滤由上层 qa 模块负责。
    """
    if top_k is None:
        top_k = settings.retrieve_top_k
    if top_k <= 0:
        return []

    collection = _get_existing_collection(user_id)
    if collection is None:
        return []

    query_vector = _embed_query(query_text)
    try:
        search_results = collection.search(
            data=[query_vector],
            anns_field=FIELD_VECTOR,
            param={"metric_type": _METRIC_TYPE, "params": {}},
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(
            f"Milvus 检索失败（user_id={user_id}）：{type(exc).__name__}: {exc}"
        ) from exc

    hits = search_results[0] if search_results else []
    results: list[SearchResult] = []
    for hit in hits:
        entity = hit.entity
        results.append(SearchResult(
            chunk_id=int(entity.get(FIELD_PK) or 0),
            text=str(entity.get(FIELD_TEXT) or ""),
            score=float(hit.distance),
            doc_id=str(entity.get(FIELD_DOC_ID) or ""),
            doc_name=str(entity.get(FIELD_DOC_NAME) or ""),
            source=str(entity.get(FIELD_SOURCE) or ""),
            file_type=str(entity.get(FIELD_FILE_TYPE) or ""),
            page=entity.get(FIELD_PAGE),       # int 或 None
            section=entity.get(FIELD_SECTION),  # str 或 None
        ))
    return results


def get_document_chunks(
    user_id: int,
    doc_id: str,
) -> list[SearchResult]:
    """按 doc_chunk_id 升序返回某文档的全部片段（用于文档预览定位）。

    collection 不存在或文档无数据时返回空列表。
    """
    doc_id_str = _validate_doc_id(doc_id)
    collection = _get_existing_collection(user_id)
    if collection is None:
        return []

    expr = f'{FIELD_DOC_ID} == "{doc_id_str}"'
    try:
        # Milvus Lite query：按主键升序，取全部字段
        entities = collection.query(
            expr=expr,
            output_fields=_OUTPUT_FIELDS,
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(
            f"查询文档片段失败（user_id={user_id}, doc_id={doc_id_str}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    results = [
        SearchResult(
            chunk_id=int(e.get(FIELD_PK) or 0),
            text=str(e.get(FIELD_TEXT) or ""),
            score=0.0,
            doc_id=str(e.get(FIELD_DOC_ID) or ""),
            doc_name=str(e.get(FIELD_DOC_NAME) or ""),
            source=str(e.get(FIELD_SOURCE) or ""),
            file_type=str(e.get(FIELD_FILE_TYPE) or ""),
            page=e.get(FIELD_PAGE),
            section=e.get(FIELD_SECTION),
        )
        for e in entities
    ]
    results.sort(key=lambda r: r.chunk_id)
    return results


# ---------------------------------------------------------------------------
# 健康探测（永不抛异常）
# ---------------------------------------------------------------------------

def probe_milvus() -> MilvusStatus:
    """探测 Milvus Lite 是否可用，并轻量检查各 collection schema 兼容性。"""
    uri = str(settings.milvus_uri)
    try:
        names = _list_collection_names()
    except VectorStoreError as exc:
        return MilvusStatus(
            ready=False, detail=str(exc), uri=uri,
            collections=[], incompatible_collections=[],
        )
    except Exception as exc:  # noqa: BLE001 - 健康探针兜底
        return MilvusStatus(
            ready=False,
            detail=f"Milvus 探测异常：{type(exc).__name__}: {exc}",
            uri=uri, collections=[], incompatible_collections=[],
        )

    # 轻量 schema 自检：只校验字段与维度，不修改/加载业务数据
    incompatible: list[str] = []
    for name in names:
        try:
            collection = _instantiate_collection(name)
            reason = verify_collection_schema(collection, raise_on_error=False)
            if reason:
                incompatible.append(name)
        except Exception:  # noqa: BLE001 - 单个集合异常不影响整体探测
            incompatible.append(name)

    detail = f"Milvus Lite 可连接，共 {len(names)} 个用户集合。"
    if incompatible:
        detail += f" 其中 {incompatible} 与当前 Schema 不兼容，请查看日志处理。"
    return MilvusStatus(
        ready=not incompatible,
        detail=detail,
        uri=uri,
        collections=names,
        incompatible_collections=incompatible,
    )
