"""文档上传 / 列表 / 删除模块单元测试。

测试策略（不依赖真实解析器与 Milvus）：
- ``ingest_file``（任务4 解析切片）被替换为返回内存 DocumentFragment 的桩；
- ``insert_document_chunks`` / ``delete_document_by_docid``（任务5）
  被替换为 MagicMock，仅校验调用参数与联动顺序；
- 上传走真实 multipart 接口 + 真实 SQLite（conftest 临时 DATA_DIR），
  文件真实落盘到临时 uploads/{user_id}/ 以验证哈希命名与删除清理。

在 backend/ 目录下运行：
    python -m pytest tests/test_doc_upload.py -v
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from config import settings
from services import upload_ingest
from services.document_parser import (
    DocumentFragment,
    EncryptedDocumentError,
)
from services.vector_store import VectorStoreError


# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------

def _fragments(path: Path, doc_name: str | None = None) -> list[DocumentFragment]:
    """模拟任务4 的切片输出（含真实落盘路径元数据）。

    doc_name 由上传流水线透传原始文件名（引用展示用），缺省回退测试名。
    """
    name = doc_name or "制度汇编.pdf"
    return [
        DocumentFragment(
            text=f"第 {i} 段：私有化 RAG 知识库测试内容",
            file_type="pdf",
            doc_name=name,
            source_path=str(path),
            page=i + 1,
            section=None,
            chunk_index=i,
        )
        for i in range(2)
    ]


@pytest.fixture()
def pipeline(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """替换解析与向量库依赖，返回 insert mock 以便断言。"""
    parse_mock = MagicMock(side_effect=_fragments)

    def _insert_side(uid, doc_id, chunks, **kw):  # noqa: ANN001
        # 模拟真实 insert 的进度回调：全部嵌入完成后回调 (processed=total)
        cb = kw.get("progress_callback")
        if cb is not None:
            cb(len(chunks), len(chunks))
        return len(chunks)

    insert_mock = MagicMock(side_effect=_insert_side)
    delete_mock = MagicMock(return_value=2)
    monkeypatch.setattr(upload_ingest, "ingest_file", parse_mock)
    monkeypatch.setattr(upload_ingest, "insert_document_chunks", insert_mock)
    monkeypatch.setattr(upload_ingest, "delete_document_by_docid", delete_mock)
    holder = MagicMock()
    holder.parse = parse_mock
    holder.insert = insert_mock
    holder.delete = delete_mock
    return holder


def _register_and_login(client: TestClient, username: str) -> tuple[str, int]:
    password = "Test123456!"
    client.post("/api/auth/register",
                json={"username": username, "password": password})
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    token = resp.json()["access_token"]
    uid = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {token}"}).json()["id"]
    return token, uid


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _upload(
    client: TestClient,
    token: str,
    filename: str = "制度汇编.pdf",
    content: bytes = b"%PDF-1.4 fake pdf bytes for upload test",
    ctype: str = "application/pdf",
):
    return client.post(
        "/api/documents/upload",
        headers=_auth_headers(token),
        files={"file": (filename, content, ctype)},
    )


def _stored_path(user_id: int, content: bytes) -> Path:
    sha = hashlib.sha256(content).hexdigest()
    settings.setup_paths()
    return Path(settings.uploads_dir) / str(user_id) / f"{sha}.pdf"


# ---------------------------------------------------------------------------
# 1. 上传成功
# ---------------------------------------------------------------------------

def test_upload_success_persists_file_and_vectors(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, uid = _register_and_login(client, "alice")
    content = b"%PDF-1.4 successful upload content"

    resp = _upload(client, token, content=content)

    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicated"] is False
    # 异步入队：响应返回时还未入库，chunk_count 为 None、status 为 pending
    assert data["chunk_count"] is None
    doc = data["document"]
    sha = hashlib.sha256(content).hexdigest()
    assert doc["doc_sha256"] == sha
    assert doc["stored_filename"] == f"{sha}.pdf"
    assert doc["original_name"] == "制度汇编.pdf"
    assert doc["file_type"] == "pdf"
    assert doc["file_size"] == len(content)
    assert doc["status"] == "pending"
    assert doc["error_msg"] is None
    assert doc["user_id"] == str(uid)

    # 文件按 uploads/{user_id}/{sha256}.{ext} 真实落盘
    stored = _stored_path(uid, content)
    assert stored.exists()
    assert stored.read_bytes() == content

    # TestClient 会同步执行 background tasks，通过列表确认最终入库成功
    listed = client.get("/api/documents/list",
                        headers=_auth_headers(token)).json()["items"]
    assert listed[0]["status"] == "success"
    assert listed[0]["total_chunks"] == 2
    assert listed[0]["processed_chunks"] == 2

    # 向量化入库参数：用户 int 隔离边界 + 字符串 doc_id + 切片列表
    pipeline.insert.assert_called_once()
    call_uid, call_doc_id, call_chunks = pipeline.insert.call_args.args
    assert call_uid == uid
    assert call_doc_id == str(doc["id"])
    assert len(call_chunks) == 2
    assert all(chunk.source_path == str(stored) for chunk in call_chunks)
    # 解析必须拿到用户可见的原始文件名，而非 {sha256}.pdf 存储名
    pipeline.parse.assert_called_once_with(stored, doc_name="制度汇编.pdf")


def test_upload_filename_path_traversal_is_stripped(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, uid = _register_and_login(client, "bob")

    resp = _upload(client, token, filename="../../evil.pdf",
                   content=b"traversal test bytes")

    assert resp.status_code == 200
    assert resp.json()["document"]["original_name"] == "evil.pdf"
    # 文件必须位于用户目录内，逃逸路径上不得出现该文件
    settings.setup_paths()
    assert not (Path(settings.uploads_dir) / "evil.pdf").exists()
    assert not (Path(settings.data_dir) / "evil.pdf").exists()


# ---------------------------------------------------------------------------
# 2. 去重
# ---------------------------------------------------------------------------

def test_duplicate_upload_skips_parse_and_vectorization(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, uid = _register_and_login(client, "carol")
    content = b"same content uploaded twice"

    first = _upload(client, token, filename="第一次.pdf", content=content)
    second = _upload(client, token, filename="第二次改名.pdf", content=content)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["duplicated"] is False
    second_data = second.json()
    assert second_data["duplicated"] is True
    assert second_data["chunk_count"] is None
    # 去重沿用首次登记（同一文档 id），不新增记录
    assert second_data["document"]["id"] == first.json()["document"]["id"]

    pipeline.parse.assert_called_once()
    pipeline.insert.assert_called_once()

    listed = client.get("/api/documents/list", headers=_auth_headers(token))
    assert listed.json()["total"] == 1
    # 原始名保留首次上传的名字
    assert listed.json()["items"][0]["original_name"] == "第一次.pdf"


# ---------------------------------------------------------------------------
# 3. 校验失败
# ---------------------------------------------------------------------------

def test_upload_rejects_unsupported_extension(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "dave")

    resp = _upload(client, token, filename="notes.txt",
                   content=b"plain text", ctype="text/plain")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "UNSUPPORTED_FILE_TYPE"
    assert "pdf" in detail["message"]
    pipeline.insert.assert_not_called()


def test_upload_rejects_oversized_file(
    client: TestClient, pipeline: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = _register_and_login(client, "erin")
    # 把大小上限压到 10 字节
    monkeypatch.setattr(
        type(settings), "max_upload_bytes", property(lambda self: 10)
    )

    resp = _upload(client, token, content=b"x" * 11)

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "FILE_TOO_LARGE"
    pipeline.parse.assert_not_called()
    pipeline.insert.assert_not_called()


# ---------------------------------------------------------------------------
# 4. 失败状态：加密 / 向量库异常 / 失败后重试
# ---------------------------------------------------------------------------

def test_encrypted_pdf_marks_failed(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "frank")
    pipeline.parse.side_effect = EncryptedDocumentError("PDF 已加密")

    # 异步：解析在后台执行，HTTP 同步阶段只校验类型/大小，故返回 200 + pending
    resp = _upload(client, token, content=b"encrypted-bytes")
    assert resp.status_code == 200
    pipeline.insert.assert_not_called()

    items = client.get("/api/documents/list",
                       headers=_auth_headers(token)).json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert "加密" in items[0]["error_msg"]


def test_vector_store_failure_marks_failed(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "grace")
    pipeline.insert.side_effect = VectorStoreError("Milvus 不可用")

    resp = _upload(client, token, content=b"vector fail bytes")
    assert resp.status_code == 200

    items = client.get("/api/documents/list",
                       headers=_auth_headers(token)).json()["items"]
    assert items[0]["status"] == "failed"
    assert "Milvus 不可用" in items[0]["error_msg"]


def test_failed_document_can_be_retried_with_same_bytes(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "heidi")
    content = b"retry after failure content"

    pipeline.parse.side_effect = EncryptedDocumentError("首次加密失败")
    first = _upload(client, token, content=content)
    assert first.status_code == 200

    # 恢复解析器后用相同文件重试：复用 failed 行，最终 success 且仅一条记录
    pipeline.parse.side_effect = _fragments
    second = _upload(client, token, content=content)
    assert second.status_code == 200

    listed = client.get("/api/documents/list",
                        headers=_auth_headers(token)).json()
    assert listed["total"] == 1
    assert listed["items"][0]["status"] == "success"
    pipeline.insert.assert_called_once()  # 首次失败未入库，重试仅入库一次


def test_failed_document_can_be_retried_with_same_bytes_id_check(
    client: TestClient, pipeline: MagicMock
) -> None:
    """重试复用同一文档 id（与上例分开断言，避免占位写法）。"""
    token, _ = _register_and_login(client, "ivan")
    content = b"retry id check content"
    pipeline.parse.side_effect = EncryptedDocumentError("加密")
    _upload(client, token, content=content)
    pipeline.parse.side_effect = _fragments
    ok = _upload(client, token, content=content).json()

    rows = client.get("/api/documents/list",
                      headers=_auth_headers(token)).json()["items"]
    assert [r["id"] for r in rows] == [ok["document"]["id"]]
    assert rows[0]["status"] == "success"


# ---------------------------------------------------------------------------
# 5. 列表分页
# ---------------------------------------------------------------------------

def test_list_is_paginated_and_owner_scoped(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "judy")
    for index in range(3):
        resp = _upload(client, token, filename=f"doc{index}.pdf",
                       content=f"content-{index}".encode())
        assert resp.status_code == 200

    page1 = client.get("/api/documents/list?page=1&page_size=2",
                       headers=_auth_headers(token)).json()
    page2 = client.get("/api/documents/list?page=2&page_size=2",
                       headers=_auth_headers(token)).json()

    assert page1["total"] == 3
    assert page1["page"] == 1 and page1["page_size"] == 2
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    # 按 id 倒序：最新上传在前
    assert page1["items"][0]["original_name"] == "doc2.pdf"
    # 列表项为精简字段
    assert "doc_sha256" not in page1["items"][0]
    assert {"id", "original_name", "file_type", "file_size",
            "status", "created_at"} <= set(page1["items"][0])


def test_list_invalid_pagination_returns_422(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, _ = _register_and_login(client, "kyle")
    assert client.get("/api/documents/list?page=0",
                      headers=_auth_headers(token)).status_code == 422
    assert client.get("/api/documents/list?page_size=101",
                      headers=_auth_headers(token)).status_code == 422


# ---------------------------------------------------------------------------
# 6. 删除联动
# ---------------------------------------------------------------------------

def test_delete_removes_db_vectors_and_file(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, uid = _register_and_login(client, "leo")
    content = b"delete me content"
    doc_id = _upload(client, token, content=content).json()["document"]["id"]
    stored = _stored_path(uid, content)
    assert stored.exists()

    resp = client.delete(f"/api/documents/{doc_id}",
                         headers=_auth_headers(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True and body["id"] == doc_id
    # 先删 Milvus 向量（参数为 int 用户 id 与字符串 doc_id）
    pipeline.delete.assert_called_once_with(uid, str(doc_id))
    # SQLite 记录与磁盘文件均已清除
    assert client.get("/api/documents/list",
                      headers=_auth_headers(token)).json()["total"] == 0
    assert not stored.exists()
    # 重复删除返回 404
    again = client.delete(f"/api/documents/{doc_id}",
                          headers=_auth_headers(token))
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"


def test_delete_vector_failure_keeps_record(
    client: TestClient, pipeline: MagicMock
) -> None:
    token, uid = _register_and_login(client, "mia")
    content = b"vector delete fails content"
    doc_id = _upload(client, token, content=content).json()["document"]["id"]
    pipeline.delete.side_effect = VectorStoreError("删除向量失败")

    resp = client.delete(f"/api/documents/{doc_id}",
                         headers=_auth_headers(token))

    assert resp.status_code == 503
    # 登记记录与文件保留，便于重试删除
    rows = client.get("/api/documents/list",
                      headers=_auth_headers(token)).json()["items"]
    assert [r["id"] for r in rows] == [doc_id]
    assert _stored_path(uid, content).exists()


def test_user_cannot_delete_other_users_document(
    client: TestClient, pipeline: MagicMock
) -> None:
    token_a, _ = _register_and_login(client, "nina")
    doc_id = _upload(client, token_a, content=b"nina private doc").json()["document"]["id"]

    token_b, _ = _register_and_login(client, "oscar")
    resp = client.delete(f"/api/documents/{doc_id}",
                         headers=_auth_headers(token_b))

    assert resp.status_code == 404
    pipeline.delete.assert_not_called()
    # 属主文档仍在
    rows = client.get("/api/documents/list",
                      headers=_auth_headers(token_a)).json()["items"]
    assert [r["id"] for r in rows] == [doc_id]


# ---------------------------------------------------------------------------
# 7. 鉴权
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("post", "/api/documents/upload"),
    ("get", "/api/documents/list"),
    ("delete", "/api/documents/1"),
])
def test_document_endpoints_require_auth(
    client: TestClient, pipeline: MagicMock, method: str, path: str
) -> None:
    if method == "post":
        resp = client.post(path, files={"file": ("a.pdf", b"x", "application/pdf")})
    else:
        resp = getattr(client, method)(path)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 8. 服务层直接校验
# ---------------------------------------------------------------------------

def test_service_normalizes_and_validates_user_id() -> None:
    assert upload_ingest.normalize_user_id(7) == "7"
    assert upload_ingest.normalize_user_id(" 12 ") == "12"
    for bad in ("0", "-1", "abc", ""):
        with pytest.raises(upload_ingest.UploadError):
            upload_ingest.normalize_user_id(bad)


def test_service_compute_sha256_matches_known_value() -> None:
    # sha256("abc") 的标准已知值
    assert upload_ingest.compute_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
