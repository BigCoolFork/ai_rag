"""Milvus Lite 向量库封装单元测试。

测试策略：完全不依赖真实 Milvus（Windows 也无 milvus-lite wheel）：
- 用内存版 FakeCollection 模拟 pymilvus Collection（insert/delete/search、
  schema 校验、cosine 排序），通过 monkeypatch 替换 vector_store 的
  四个底层连接函数（_connect / _has_collection /
  _instantiate_collection / _create_new_collection / _list_collection_names）；
- 用确定性 FakeEmbeddings 替换 get_embeddings 单例，无需 Ollama。

在 backend/ 目录下运行：
    python -m pytest tests/test_vector_store.py -v
"""

from __future__ import annotations

import hashlib
import math
import random
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from config import settings
from services import ollama_client, vector_store as vs
from services.document_parser import DocumentFragment
from services.ollama_client import ModelStatus, OllamaStatus
from services.vector_store import (
    EmbeddingError,
    MilvusStatus,
    SchemaIncompatibleError,
    VectorStoreConnectionError,
    VectorStoreError,
)

DIM = settings.embedding_dim  # 768


# ---------------------------------------------------------------------------
# 测试替身：确定性 Embedding
# ---------------------------------------------------------------------------

class FakeEmbeddings:
    """以文本 md5 为种子的确定性伪向量；query 与 document 同文本同向量。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.embedded_texts: list[str] = []

    def _vector(self, text: str) -> list[float]:
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(seed)
        return [rng.random() for _ in range(self.dim)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.embedded_texts.append(text)
        return self._vector(text)


def _broken_embeddings() -> None:
    raise httpx.ConnectError("Ollama 连接被拒绝")


# ---------------------------------------------------------------------------
# 测试替身：内存版 pymilvus Collection
# ---------------------------------------------------------------------------

class _DeleteResult:
    def __init__(self, count: int) -> None:
        self.delete_count = count


class _Entity:
    def __init__(self, row: dict) -> None:
        self._row = row

    def get(self, key: str):  # noqa: ANN001 模拟 pymilvus entity.get
        return self._row.get(key)


class _Hit:
    def __init__(self, row: dict, score: float) -> None:
        self.id = row[vs.FIELD_PK]
        self.distance = score
        self.entity = _Entity(row)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeCollection:
    """内存集合：按列插入、按 doc_id 表达式删除、cosine TopK 检索。"""

    def __init__(self, name: str, schema=None) -> None:  # noqa: ANN001
        self.name = name
        self.schema = schema
        self.rows: list[dict] = []
        self._next_id = 1
        self.inserted_batches: list[list[list]] = []
        self.deleted_exprs: list[str] = []
        self.index_params: list[dict] = []
        self.loaded = False
        self.flushed = False
        # 故障注入开关
        self.fail_insert: bool = False
        self.fail_search: bool = False

    def create_index(self, field_name: str, index_params: dict) -> None:
        self.index_params.append({"field": field_name, **index_params})

    def load(self) -> None:
        self.loaded = True

    def insert(self, columns: list[list]) -> None:
        if self.fail_insert:
            raise RuntimeError("simulated milvus insert failure")
        assert len(columns) == len(vs._INSERT_FIELDS)
        width = len(columns[0])
        assert all(len(col) == width for col in columns), "列长度必须一致"
        for values in zip(*columns):
            row = dict(zip(vs._INSERT_FIELDS, values))
            row[vs.FIELD_PK] = self._next_id
            self._next_id += 1
            self.rows.append(row)
        self.inserted_batches.append(columns)

    def flush(self) -> None:
        self.flushed = True

    def delete(self, expr: str) -> _DeleteResult:
        self.deleted_exprs.append(expr)
        match = re.match(r'^doc_id == "([^"]+)"$', expr)
        assert match, f"未预期的删除表达式: {expr}"
        target = match.group(1)
        before = len(self.rows)
        self.rows = [r for r in self.rows if r[vs.FIELD_DOC_ID] != target]
        return _DeleteResult(before - len(self.rows))

    def search(self, data, anns_field, param, limit, output_fields):  # noqa: ANN001
        if self.fail_search:
            raise RuntimeError("simulated milvus search failure")
        assert anns_field == vs.FIELD_VECTOR
        assert output_fields == vs._OUTPUT_FIELDS
        query_vector = data[0]
        ranked = sorted(
            ((_cosine(query_vector, row[vs.FIELD_VECTOR]), row) for row in self.rows),
            key=lambda item: item[0],
            reverse=True,
        )
        return [[_Hit(row, score) for score, row in ranked[:limit]]]


class FakeMilvusEnv:
    """collection 注册表 + 底层连接函数替换。"""

    def __init__(self, embeddings: FakeEmbeddings | None = None) -> None:
        self.registry: dict[str, FakeCollection] = {}
        self.embeddings = embeddings or FakeEmbeddings()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeMilvusEnv":
        vs.reset_connection_cache()
        monkeypatch.setattr(vs, "_connect", lambda: "fake_alias")
        monkeypatch.setattr(vs, "_has_collection", lambda name: name in self.registry)
        monkeypatch.setattr(
            vs, "_list_collection_names", lambda: list(self.registry)
        )

        def _instantiate(name: str) -> FakeCollection:
            return self.registry[name]

        def _create(name: str) -> FakeCollection:
            assert name not in self.registry
            collection = FakeCollection(name, vs.build_schema(DIM))
            collection.create_index(
                vs.FIELD_VECTOR,
                {"index_type": "AUTOINDEX", "metric_type": "COSINE", "params": {}},
            )
            collection.load()
            self.registry[name] = collection
            return collection

        monkeypatch.setattr(vs, "_instantiate_collection", _instantiate)
        monkeypatch.setattr(vs, "_create_new_collection", _create)
        monkeypatch.setattr(vs, "get_embeddings", lambda: self.embeddings)
        return self

    def get(self, user_id: int) -> FakeCollection:
        return self.registry[vs.collection_name(user_id)]

    def put_broken(self, name: str, schema) -> FakeCollection:  # noqa: ANN001
        collection = FakeCollection(name, schema)
        self.registry[name] = collection
        return collection


# ---------------------------------------------------------------------------
# 碎片构造助手
# ---------------------------------------------------------------------------

def _fragment(
    text: str,
    *,
    page: int | None = 1,
    section: str | None = None,
    name: str = "spec.pdf",
    file_type: str = "pdf",
) -> DocumentFragment:
    return DocumentFragment(
        text=text,
        file_type=file_type,
        doc_name=name,
        source_path=f"/data/uploads/{name}",
        page=page,
        section=section,
        chunk_index=None,
    )


@pytest.fixture(autouse=True)
def _reset_verified_cache() -> None:
    """每个用例前后清空 schema 校验缓存，避免相互污染。"""
    vs.reset_connection_cache()
    yield
    vs.reset_connection_cache()


# ---------------------------------------------------------------------------
# 1. 命名与 Schema
# ---------------------------------------------------------------------------

def test_collection_name_follows_user_template() -> None:
    assert vs.collection_name(1) == "user_1_rag"
    assert vs.collection_name(42) == "user_42_rag"


@pytest.mark.parametrize("bad_user_id", [0, -1, "1", 1.5])
def test_collection_name_rejects_invalid_user_id(bad_user_id) -> None:  # noqa: ANN001
    with pytest.raises(VectorStoreError):
        vs.collection_name(bad_user_id)


def test_build_schema_v1_fields_and_dim() -> None:
    schema = vs.build_schema(DIM)
    fields = {field.name: field for field in schema.fields}

    assert list(fields) == [
        "doc_chunk_id", "doc_id", "source", "doc_name", "file_type",
        "page", "section", "text", "vector",
    ]
    # 主键 INT64 自增
    assert fields["doc_chunk_id"].is_primary is True
    assert fields["doc_chunk_id"].auto_id is True
    # page / section 允许为 None（DOCX/XLSX 无页码、PDF 无章节）
    assert fields["page"].nullable is True
    assert fields["section"].nullable is True
    # 向量维度来自 settings
    assert int(fields["vector"].params["dim"]) == DIM


# ---------------------------------------------------------------------------
# 2. insert_document_chunks
# ---------------------------------------------------------------------------

def test_insert_creates_collection_and_projects_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    chunks = [
        _fragment("第一章内容", page=1, section="第一章 概述"),
        _fragment("第二章内容", page=5, section="第二章 安装"),
    ]

    count = vs.insert_document_chunks(user_id=1, doc_id=42, chunks=chunks)

    assert count == 2
    coll = env.get(1)
    assert coll.name == "user_1_rag"
    assert coll.loaded is True  # 创建后自动 load
    assert coll.index_params[0]["field"] == "vector"
    assert coll.index_params[0]["metric_type"] == "COSINE"
    assert coll.flushed is True

    columns = coll.inserted_batches[0]
    assert len(columns) == len(vs._INSERT_FIELDS)
    by_name = dict(zip(vs._INSERT_FIELDS, columns))
    assert by_name["doc_id"] == ["42", "42"]          # 同文档共用 doc_id（字符串）
    assert by_name["page"] == [1, 5]                   # PDF 真实页码
    assert by_name["section"] == ["第一章 概述", "第二章 安装"]
    assert by_name["file_type"] == ["pdf", "pdf"]
    assert by_name["doc_name"] == ["spec.pdf", "spec.pdf"]
    assert all(len(v) == DIM for v in by_name["vector"])


def test_insert_docx_nullable_page_and_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    chunks = [
        _fragment("Word 段落，没有页码", page=None, section="标题一",
                  name="需求.docx", file_type="docx"),
        _fragment("表格内容", page=None, section=None,
                  name="需求.docx", file_type="docx"),
    ]

    vs.insert_document_chunks(7, "doc-88", chunks)

    by_name = dict(zip(vs._INSERT_FIELDS, env.get(7).inserted_batches[0]))
    assert by_name["page"] == [None, None]   # 绝不伪造页码
    assert by_name["section"] == ["标题一", None]
    assert by_name["doc_id"] == ["doc-88", "doc-88"]


def test_insert_reuses_existing_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    existing = env.put_broken("user_3_rag", vs.build_schema(DIM))

    vs.insert_document_chunks(3, 1, [_fragment("复用集合写入")])

    # 已存在则直接复用，不重复建索引；但必须确保已 load（重启后旧集合为 released）
    assert existing.loaded is True
    assert len(existing.index_params) == 0
    assert len(existing.rows) == 1


def test_ensure_loaded_when_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程重启后旧 collection 为 released（NotLoad）时，必须重新 load。"""
    import pymilvus
    from pymilvus.client.types import LoadState

    monkeypatch.setattr(vs, "_connect", lambda: "fake_alias")
    monkeypatch.setattr(
        pymilvus.utility, "load_state",
        lambda name, using: LoadState.NotLoad,
    )
    coll = FakeCollection("user_9_rag")
    vs._ensure_loaded(coll)
    assert coll.loaded is True


def test_ensure_loaded_skips_when_already_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已是 Loaded 状态时不重复 load。"""
    import pymilvus
    from pymilvus.client.types import LoadState

    monkeypatch.setattr(vs, "_connect", lambda: "fake_alias")
    monkeypatch.setattr(
        pymilvus.utility, "load_state",
        lambda name, using: LoadState.Loaded,
    )
    coll = FakeCollection("user_9_rag")
    vs._ensure_loaded(coll)
    assert coll.loaded is False


def test_insert_empty_chunks_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMilvusEnv().install(monkeypatch)
    with pytest.raises(VectorStoreError, match="没有可入库"):
        vs.insert_document_chunks(1, 1, [])


@pytest.mark.parametrize("bad_doc_id", ['1"; DROP', "a b", "", "x" * 129])
def test_insert_rejects_illegal_doc_id(
    monkeypatch: pytest.MonkeyPatch, bad_doc_id,
) -> None:
    FakeMilvusEnv().install(monkeypatch)
    with pytest.raises(VectorStoreError, match="非法的文档 id"):
        vs.insert_document_chunks(1, bad_doc_id, [_fragment("x")])


def test_insert_wraps_embedding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    monkeypatch.setattr(vs, "get_embeddings", _broken_embeddings)

    with pytest.raises(EmbeddingError, match="生成文档向量失败"):
        vs.insert_document_chunks(1, 1, [_fragment("x")])
    # 嵌入失败时不得创建/写入集合
    assert "user_1_rag" not in env.registry


def test_insert_rejects_dim_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv(embeddings=FakeEmbeddings(dim=128)).install(monkeypatch)

    with pytest.raises(EmbeddingError, match=str(DIM)):
        vs.insert_document_chunks(1, 1, [_fragment("x")])
    assert "user_1_rag" not in env.registry


def test_insert_wraps_native_milvus_error(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    env.put_broken("user_1_rag", vs.build_schema(DIM)).fail_insert = True

    with pytest.raises(VectorStoreError) as exc_info:
        vs.insert_document_chunks(1, 1, [_fragment("x")])
    assert "写入 Milvus 失败" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. delete_document_by_docid
# ---------------------------------------------------------------------------

def test_delete_removes_all_chunks_of_one_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    vs.insert_document_chunks(1, 100, [_fragment("文档A-1"), _fragment("文档A-2")])
    vs.insert_document_chunks(1, 200, [_fragment("文档B-1")])
    coll = env.get(1)
    assert len(coll.rows) == 3

    deleted = vs.delete_document_by_docid(1, 100)

    assert deleted == 2
    assert coll.deleted_exprs == ['doc_id == "100"']
    assert {row[vs.FIELD_DOC_ID] for row in coll.rows} == {"200"}


def test_delete_missing_collection_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMilvusEnv().install(monkeypatch)
    assert vs.delete_document_by_docid(999, 1) == 0


def test_delete_rejects_illegal_doc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMilvusEnv().install(monkeypatch)
    with pytest.raises(VectorStoreError):
        vs.delete_document_by_docid(1, '1" or 1==1')


# ---------------------------------------------------------------------------
# 4. vector_search
# ---------------------------------------------------------------------------

def test_search_returns_ranked_results_with_full_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    target_text = "私有化部署需要完全离线运行"
    vs.insert_document_chunks(1, 100, [
        _fragment(target_text, page=3, section="部署章节", name="部署指南.pdf"),
        _fragment("另一段不相关内容", page=8, section="FAQ", name="部署指南.pdf"),
        _fragment("Word 内容无页码", page=None, section="安装",
                  name="手册.docx", file_type="docx"),
    ])

    results = vs.vector_search(1, target_text, top_k=2)

    assert len(results) == 2
    top = results[0]
    # 查询向量与目标 chunk 向量完全一致 → cosine ≈ 1 且排第一
    assert top.text == target_text
    assert top.score == pytest.approx(1.0, abs=1e-6)
    assert top.doc_id == "100"
    assert top.doc_name == "部署指南.pdf"
    assert top.file_type == "pdf"
    assert top.page == 3
    assert top.section == "部署章节"
    assert top.source.endswith("部署指南.pdf")
    # to_dict 与 metadata schema 对齐
    assert set(top.to_dict()) == {
        "text", "score", "doc_id", "doc_name", "source",
        "file_type", "page", "section",
    }
    assert env.get(1).rows[2][vs.FIELD_PAGE] is None  # docx 页码保持 None


def test_search_user_without_collection_returns_empty_without_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    assert vs.vector_search(888, "任意问题", top_k=4) == []
    assert "user_888_rag" not in env.registry  # 检索路径绝不自动建集合


def test_search_top_k_zero_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    vs.insert_document_chunks(1, 1, [_fragment("内容")])
    assert vs.vector_search(1, "内容", top_k=0) == []


def test_search_wraps_query_embedding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    vs.insert_document_chunks(1, 1, [_fragment("内容")])
    monkeypatch.setattr(vs, "get_embeddings", _broken_embeddings)

    with pytest.raises(EmbeddingError, match="生成查询向量失败"):
        vs.vector_search(1, "问题", top_k=3)


def test_search_wraps_native_milvus_error(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    coll = env.put_broken("user_1_rag", vs.build_schema(DIM))
    coll.fail_search = True

    with pytest.raises(VectorStoreError, match="Milvus 检索失败"):
        vs.vector_search(1, "问题", top_k=3)


def test_user_collections_are_physically_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    vs.insert_document_chunks(1, 10, [_fragment("用户一的私有内容", name="u1.pdf")])
    vs.insert_document_chunks(2, 20, [_fragment("用户二的私有内容", name="u2.pdf")])

    assert set(env.registry) == {"user_1_rag", "user_2_rag"}
    hits_u1 = vs.vector_search(1, "用户一的私有内容", top_k=5)
    hits_u2 = vs.vector_search(2, "用户一的私有内容", top_k=5)
    assert {h.doc_name for h in hits_u1} == {"u1.pdf"}
    assert {h.doc_name for h in hits_u2} == {"u2.pdf"}  # 检不到对方文档


# ---------------------------------------------------------------------------
# 5. Schema 兼容性自检
# ---------------------------------------------------------------------------

def test_verify_schema_passes_for_v1_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    coll = env.put_broken("user_1_rag", vs.build_schema(DIM))
    assert vs.verify_collection_schema(coll) is None


def test_verify_schema_detects_missing_field() -> None:
    from pymilvus import CollectionSchema

    broken = CollectionSchema(
        fields=[f for f in vs.build_schema(DIM).fields if f.name != "page"],
        description="old schema",
    )
    coll = FakeCollection("user_1_rag", broken)

    with pytest.raises(SchemaIncompatibleError) as exc_info:
        vs.verify_collection_schema(coll)
    message = str(exc_info.value)
    assert "page" in message
    assert "删除" in message  # 给出可操作的迁移指引


def test_verify_schema_detects_dim_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    env.put_broken("user_5_rag", vs.build_schema(1024))

    with pytest.raises(SchemaIncompatibleError, match="1024"):
        vs.get_or_create_collection(5)


# ---------------------------------------------------------------------------
# 6. probe_milvus：永不抛异常
# ---------------------------------------------------------------------------

def test_probe_ready_lists_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    env.put_broken("user_1_rag", vs.build_schema(DIM))
    env.put_broken("user_2_rag", vs.build_schema(DIM))

    status = vs.probe_milvus()

    assert isinstance(status, MilvusStatus)
    assert status.ready is True
    assert status.collections == ["user_1_rag", "user_2_rag"]
    assert status.incompatible_collections == []
    assert "2 个用户集合" in status.detail


def test_probe_flags_incompatible_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = FakeMilvusEnv().install(monkeypatch)
    env.put_broken("user_1_rag", vs.build_schema(DIM))
    env.put_broken("user_9_rag", vs.build_schema(1024))  # 维度不匹配

    status = vs.probe_milvus()

    assert status.ready is False
    assert status.incompatible_collections == ["user_9_rag"]
    assert "不兼容" in status.detail


def test_probe_connection_error_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMilvusEnv().install(monkeypatch)

    def _raise() -> None:
        raise VectorStoreConnectionError("无法连接 Milvus Lite")

    monkeypatch.setattr(vs, "_list_collection_names", _raise)

    status = vs.probe_milvus()
    assert status.ready is False
    assert "无法连接" in status.detail


def test_probe_unexpected_exception_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMilvusEnv().install(monkeypatch)
    monkeypatch.setattr(
        vs, "_list_collection_names",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    status = vs.probe_milvus()
    assert status.ready is False
    assert "boom" in status.detail


# ---------------------------------------------------------------------------
# 7. /api/health 新增 milvus 检查项
# ---------------------------------------------------------------------------

def _ollama_ready_status() -> OllamaStatus:
    return OllamaStatus(
        base_url="http://ollama:11434",
        ollama_ready=True,
        ollama_detail="ok",
        version="0.5.7",
        installed_models=["qwen2.5:7b", "nomic-embed-text:latest"],
        llm=ModelStatus("qwen2.5:7b", True, "LLM 已安装"),
        embedding=ModelStatus("nomic-embed-text", True, "Embedding 已安装"),
    )


def test_health_milvus_down_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_client, "probe_ollama", _ollama_ready_status)

    def _milvus_down() -> MilvusStatus:
        return MilvusStatus(
            ready=False, detail="无法连接 Milvus Lite", uri=":memory:",
            collections=[], incompatible_collections=[],
        )

    monkeypatch.setattr(vs, "probe_milvus", _milvus_down)

    resp = client.get("/api/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["checks"]["milvus"]["ready"] is False
    assert "无法连接" in data["checks"]["milvus"]["detail"]
    assert data["checks"]["ollama"]["ready"] is True
