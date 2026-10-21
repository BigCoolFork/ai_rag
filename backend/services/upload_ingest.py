"""文档上传入库业务编排（任务6）。

流水线（全部同步，FastAPI 路由层在线程池中执行）：
1. 校验原始文件名后缀（pdf / docx / xlsx）与字节大小上限；
2. 计算文件完整 SHA256；
3. 同用户 (user_id, doc_sha256) 去重：已有 success 记录直接返回，
   不重复解析 / 落盘 / 向量化；已有 failed 记录则复用该行重试；
4. 写入 uploads/{user_id}/{sha256}.{ext}；
5. documents 表登记 status=pending；
6. 复用任务4 ``ingest_file`` 解析切片，复用任务5
   ``insert_document_chunks`` 向量化入库；
7. 成功置 status=success；解析 / 入库异常置 status=failed + error_msg
   并原样抛出（路由层负责转结构化 HTTP 错误）。

删除：先删 Milvus 向量（失败则保留登记），再删 SQLite 记录，
最后尽力清理磁盘文件。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from config import settings
from database import db_session, utc_now_iso
from services.document_parser import (
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from services.ingest import ingest_file
from services.vector_store import (
    PUBLIC_USER_ID,
    VectorStoreError,
    delete_document_by_docid,
    insert_document_chunks,
    move_document_chunks,
)

logger = logging.getLogger(__name__)

# 与 database.py / schemas/document.py 的状态枚举保持一致
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

# error_msg 列长度保护（SQLite 本身无强限制，保持错误信息可读即可）
_MAX_ERROR_MSG = 1000
# 原始文件名长度保护
_MAX_FILENAME = 255

_RECORD_COLUMNS = (
    "id, user_id, doc_sha256, original_name, stored_filename, "
    "file_type, file_size, status, error_msg, created_at, "
    "total_chunks, processed_chunks, is_public"
)


# ---------------------------------------------------------------------------
# 业务异常（文件落盘 / 记录查找类，解析与向量异常复用任务4/5 定义）
# ---------------------------------------------------------------------------

class UploadError(Exception):
    """上传业务异常基类。"""


class UploadStorageError(UploadError):
    """文件落盘 / 删除等磁盘 IO 失败。"""


class DocumentNotFoundError(UploadError):
    """文档不存在或不属于当前用户。"""


@dataclass
class UploadResult:
    """ingest_upload 返回值：登记记录 + 是否命中去重 + 本次切片数。"""

    record: dict
    duplicated: bool
    chunk_count: int | None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def normalize_user_id(user_id: str | int) -> str:
    """规范化用户标识：仅接受正整数（向量库 collection 按 int 用户隔离）。"""
    text = str(user_id).strip()
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise UploadError(f"非法的用户标识：{user_id!r}") from exc
    if value <= 0:
        raise UploadError(f"非法的用户标识：{user_id!r}")
    return str(value)


def sanitize_filename(original_filename: str) -> str:
    """剥离目录部分，防止路径穿越；返回纯文件名。"""
    name = Path(original_filename or "").name.strip()
    if not name:
        raise UnsupportedFileTypeError("未获取到上传文件名。")
    return name[:_MAX_FILENAME]


def detect_file_type(original_filename: str) -> str:
    """从原始文件名提取小写后缀并校验白名单，返回 file_type。"""
    ext = Path(original_filename).suffix.lstrip(".").lower()
    if ext not in settings.allowed_extensions:
        allowed = " / ".join(settings.allowed_extensions)
        raise UnsupportedFileTypeError(
            f"不支持的文件类型「.{ext}」，仅支持：{allowed}。"
        )
    return ext


def validate_size(file_bytes: bytes) -> None:
    """字节大小上限校验。"""
    size = len(file_bytes)
    if size == 0:
        raise UploadError("上传文件为空，请重新选择文件。")
    if size > settings.max_upload_bytes:
        raise FileTooLargeError(
            f"文件过大：{size / 1024 / 1024:.1f}MB，"
            f"上限为 {settings.max_upload_mb}MB。"
        )


def compute_sha256(file_bytes: bytes) -> str:
    """计算文件完整 SHA256（十六进制字符串）。"""
    return hashlib.sha256(file_bytes).hexdigest()


def user_upload_dir(user_id: str) -> Path:
    """用户独立上传目录 uploads/{user_id}（settings 已确保根目录存在）。"""
    settings.setup_paths()
    directory = Path(settings.uploads_dir) / user_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _row_to_dict(row) -> dict:  # noqa: ANN001 - sqlite3.Row
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "doc_sha256": row["doc_sha256"],
        "original_name": row["original_name"],
        "stored_filename": row["stored_filename"],
        "file_type": row["file_type"],
        "file_size": row["file_size"],
        "status": row["status"],
        "error_msg": row["error_msg"],
        "created_at": row["created_at"],
        "total_chunks": row["total_chunks"],
        "processed_chunks": row["processed_chunks"],
        "is_public": row["is_public"],
    }


class _IngestAborted(Exception):
    """内部信号：后台入库过程中文档被用户删除，应静默中止。"""


def _find_document(conn, user_id: str, doc_id: int) -> dict | None:  # noqa: ANN001
    row = conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM documents "
        "WHERE id = ? AND user_id = ?",
        (doc_id, user_id),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 状态更新
# ---------------------------------------------------------------------------

def _mark_failed(user_id: str, doc_id: int, exc: Exception) -> None:
    """将登记记录置为 failed；状态更新自身失败只记录日志，不掩盖原异常。"""
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE documents SET status = ?, error_msg = ? "
                "WHERE id = ? AND user_id = ?",
                (STATUS_FAILED, str(exc)[:_MAX_ERROR_MSG], doc_id, user_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("更新文档失败状态失败：user_id=%s doc_id=%s", user_id, doc_id)


def _update_progress(
    user_id: str,
    doc_id: int,
    *,
    total: int | None = None,
    processed: int | None = None,
) -> None:
    """更新切片总数 / 已处理数（后台分批嵌入时调用）。"""
    sets, params = [], []
    if total is not None:
        sets.append("total_chunks = ?")
        params.append(total)
    if processed is not None:
        sets.append("processed_chunks = ?")
        params.append(processed)
    if not sets:
        return
    params.extend([doc_id, user_id])
    try:
        with db_session() as conn:
            conn.execute(
                f"UPDATE documents SET {', '.join(sets)} "
                "WHERE id = ? AND user_id = ?",
                params,
            )
    except Exception:  # noqa: BLE001
        logger.exception("更新入库进度失败：user_id=%s doc_id=%s", user_id, doc_id)


def _mark_success(user_id: str, doc_id: int) -> None:
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE documents SET status = ?, error_msg = NULL "
                "WHERE id = ? AND user_id = ?",
                (STATUS_SUCCESS, doc_id, user_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("更新文档成功状态失败：user_id=%s doc_id=%s", user_id, doc_id)


def _document_exists(user_id: str, doc_id: int) -> bool:
    """登记记录是否仍存在（处理中删除的竞态检测）。"""
    with db_session() as conn:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE id = ? AND user_id = ?",
            (doc_id, user_id),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 主流程：异步入队 + 后台处理
# ---------------------------------------------------------------------------

def enqueue_upload(
    user_id: str | int,
    file_bytes: bytes,
    original_filename: str,
    is_public: bool = False,
) -> tuple[UploadResult, Path]:
    """上传同步前半段：校验 → 落盘 → 登记 pending。

    不做解析与向量化（耗时操作放后台），返回 (UploadResult, 落盘路径) 供路由层
    把落盘路径交给 BackgroundTasks。命中 success 去重时落盘路径无意义。

    :param is_public: True=公开文档（所有用户可检索），False=私有（仅属主可见）
    :raises 同 ingest_upload 的前半段异常（类型 / 大小 / 空文件 / 磁盘 IO）
    """
    uid = normalize_user_id(user_id)
    is_public_int = 1 if is_public else 0

    # 1) 基础校验
    filename = sanitize_filename(original_filename)
    file_type = detect_file_type(filename)
    validate_size(file_bytes)

    # 2) 哈希与落盘路径
    sha256 = compute_sha256(file_bytes)
    stored_filename = f"{sha256}.{file_type}"
    stored_path = user_upload_dir(uid) / stored_filename

    # 3) 同用户哈希去重（success 直接返回；failed 复用原行重试）
    with db_session() as conn:
        existing = conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM documents "
            "WHERE user_id = ? AND doc_sha256 = ?",
            (uid, sha256),
        ).fetchone()
    if existing is not None and existing["status"] == STATUS_SUCCESS:
        logger.info("命中上传去重：user_id=%s doc_id=%s", uid, existing["id"])
        return UploadResult(record=_row_to_dict(existing), duplicated=True,
                            chunk_count=None), stored_path

    # 4) 落盘（先写临时文件再替换，避免半成品被解析到）
    tmp_path = stored_path.with_suffix(stored_path.suffix + ".part")
    try:
        with open(tmp_path, "wb") as fp:
            fp.write(file_bytes)
        os.replace(tmp_path, stored_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise UploadStorageError(
            f"文件保存失败：{type(exc).__name__}: {exc}"
        ) from exc

    # 5) 登记 pending（新记录插入；failed 记录复用并重置）
    if existing is not None:
        doc_id = existing["id"]
        with db_session() as conn:
            conn.execute(
                "UPDATE documents SET status = ?, error_msg = NULL, "
                "original_name = ?, stored_filename = ?, file_type = ?, "
                "file_size = ?, created_at = ?, total_chunks = 0, "
                "processed_chunks = 0, is_public = ? WHERE id = ? AND user_id = ?",
                (STATUS_PENDING, filename, stored_filename, file_type,
                 len(file_bytes), utc_now_iso(), is_public_int, doc_id, uid),
            )
    else:
        with db_session() as conn:
            cursor = conn.execute(
                "INSERT INTO documents (user_id, doc_sha256, original_name, "
                "stored_filename, file_type, file_size, created_at, status, "
                "error_msg, total_chunks, processed_chunks, is_public) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)",
                (uid, sha256, filename, stored_filename, file_type,
                 len(file_bytes), utc_now_iso(), STATUS_PENDING, is_public_int),
            )
            doc_id = cursor.lastrowid

    with db_session() as conn:
        record = _find_document(conn, uid, doc_id)
    logger.info("文档已入队等待后台入库：user_id=%s doc_id=%s size=%d is_public=%d",
                uid, doc_id, len(file_bytes), is_public_int)
    return UploadResult(record=record, duplicated=False, chunk_count=None), stored_path


def process_document_ingest(
    user_id: str | int,
    doc_id: int,
    stored_path: Path,
    original_filename: str,
) -> int | None:
    """后台入库：解析 → 分批向量化（带进度）→ 写 Milvus → 置 success。

    处理中若文档被删除，进度回调会抛 _IngestAborted，本函数静默中止且不残留
    向量；Milvus 写入后若发现文档已被删，则立即清理刚写入的向量。

    向量写入的 collection 由文档 is_public 决定：
    - 公开文档 → 公共 collection（PUBLIC_USER_ID），所有用户可检索
    - 私有文档 → 属主专属 collection，仅属主可检索

    :return: 实际写入 chunk 数；被中止时返回 None
    """
    uid = normalize_user_id(user_id)

    # 读取文档的可见性，决定向量写入哪个 collection
    with db_session() as conn:
        record = _find_document(conn, uid, doc_id)
    is_public = bool(record and record.get("is_public"))
    vector_user_id = PUBLIC_USER_ID if is_public else int(uid)

    def _on_progress(processed: int, total: int) -> None:
        if not _document_exists(uid, doc_id):
            raise _IngestAborted()
        _update_progress(uid, doc_id, total=total, processed=processed)

    try:
        # 解析切片（必须传原始文件名，否则来源展示哈希存储名）
        chunks = ingest_file(stored_path, doc_name=original_filename)
        _update_progress(uid, doc_id, total=len(chunks), processed=0)

        chunk_count = insert_document_chunks(
            vector_user_id, str(doc_id), chunks, progress_callback=_on_progress
        )

        # 写入 Milvus 后再检测一次：若期间被删除，清理孤儿向量
        if not _document_exists(uid, doc_id):
            delete_document_by_docid(vector_user_id, str(doc_id))
            logger.info("文档 %s 入库完成时已被删除，已清理向量", doc_id)
            return None

        _mark_success(uid, doc_id)
        logger.info("文档入库成功：user_id=%s doc_id=%s chunks=%d is_public=%s",
                    uid, doc_id, chunk_count, is_public)
        return chunk_count
    except _IngestAborted:
        logger.info("文档 %s 处理中被删除，后台入库中止", doc_id)
        return None
    except Exception as exc:  # noqa: BLE001 - 标记失败后不再抛出（后台任务）
        _mark_failed(uid, doc_id, exc)
        logger.warning("后台文档入库失败：user_id=%s doc_id=%s %s",
                       uid, doc_id, exc)
        return None


def ingest_upload(
    user_id: str | int,
    file_bytes: bytes,
    original_filename: str,
    is_public: bool = False,
) -> UploadResult:
    """同步完整入库流水线（单测 / 脚本 / 容器内冒烟使用）。

    等价于 enqueue_upload 后立即在当前线程执行 process_document_ingest，
    失败时原样抛出（由调用方映射为 HTTP 错误）。
    """
    result, stored_path = enqueue_upload(
        user_id, file_bytes, original_filename, is_public=is_public
    )
    if result.duplicated:
        return result
    record = result.record
    chunk_count = process_document_ingest(
        user_id, record["id"], stored_path, original_filename
    )
    with db_session() as conn:
        final = _find_document(conn, normalize_user_id(user_id), record["id"])
    return UploadResult(
        record=final or record,
        duplicated=False,
        chunk_count=chunk_count,
    )


def recover_stale_pending() -> int:
    """启动恢复：把仍处于 pending 的记录标记为 failed（进程重启中断的后台任务）。

    用户在列表看到失败后可重新上传（同文件会复用该行重试）。
    :return: 恢复的记录数
    """
    msg = "服务重启，入库中断，请重新上传"
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE documents SET status = ?, error_msg = ? "
            "WHERE status = ?",
            (STATUS_FAILED, msg, STATUS_PENDING),
        )
        count = cur.rowcount
    if count:
        logger.warning("启动恢复：将 %d 个中断的 pending 文档标记为 failed", count)
    return count


# ---------------------------------------------------------------------------
# 列表 / 删除
# ---------------------------------------------------------------------------

def list_documents(
    user_id: str | int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """分页返回当前用户文档（按创建时间倒序），返回 (列表, 总数)。"""
    uid = normalize_user_id(user_id)
    offset = (page - 1) * page_size
    with db_session() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE user_id = ?",
            (uid,),
        ).fetchone()["cnt"]
        rows = conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM documents WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (uid, page_size, offset),
        ).fetchall()
    return [_row_to_dict(row) for row in rows], int(total)


def delete_document(user_id: str | int, doc_id: int) -> tuple[dict, int]:
    """删除整份文档：Milvus 向量 → SQLite 记录 → 磁盘文件。

    根据文档 is_public 决定从哪个 collection 删除向量：
    - 公开文档 → 公共 collection（PUBLIC_USER_ID）
    - 私有文档 → 属主专属 collection

    :return: (被删记录, 删除的向量数；无精确计数时为 -1)
    :raises DocumentNotFoundError: 文档不存在或不属于该用户
    :raises VectorStoreError: 向量删除失败（此时登记与文件保留不动）
    """
    uid = normalize_user_id(user_id)
    with db_session() as conn:
        record = _find_document(conn, uid, doc_id)
    if record is None:
        raise DocumentNotFoundError(
            f"文档 {doc_id} 不存在或无权删除。"
        )

    # 根据可见性选择向量所在的 collection
    is_public = bool(record.get("is_public"))
    vector_user_id = PUBLIC_USER_ID if is_public else int(uid)

    # 向量删除失败时不允许留下“有登记无向量”的半成品，整体中止
    chunks_removed = delete_document_by_docid(vector_user_id, str(doc_id))

    with db_session() as conn:
        conn.execute(
            "DELETE FROM documents WHERE id = ? AND user_id = ?",
            (doc_id, uid),
        )

    # 磁盘文件尽力清理：失败仅告警，不影响删除结果
    stored_path = user_upload_dir(uid) / record["stored_filename"]
    try:
        stored_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("文档登记与向量已删除，但磁盘文件清理失败：%s", stored_path)

    logger.info("文档已删除：user_id=%s doc_id=%s chunks=%s is_public=%s",
                uid, doc_id, chunks_removed, is_public)
    return record, chunks_removed


class DocumentStateError(UploadError):
    """文档状态不允许执行该操作（如对非 success 文档切换可见性）。"""


def set_document_visibility(
    user_id: str | int,
    doc_id: int,
    is_public: bool,
) -> tuple[dict, int]:
    """切换文档公开/私有：在用户 collection 与公共 collection 间搬移向量。

    - 仅属主可操作（_find_document 校验 user_id）；
    - 仅 status=success 的文档可转换（pending/failed 无向量可搬）；
    - 若目标可见性与当前一致，直接返回不做操作。

    :return: (更新后的记录, 搬移的 chunk 数)
    :raises DocumentNotFoundError: 文档不存在或不属于该用户
    :raises DocumentStateError: 文档状态非 success
    :raises VectorStoreError: 向量搬移失败（此时向量可能已丢失，记录置 failed）
    """
    uid = normalize_user_id(user_id)
    with db_session() as conn:
        record = _find_document(conn, uid, doc_id)
    if record is None:
        raise DocumentNotFoundError(
            f"文档 {doc_id} 不存在或无权操作。"
        )

    current_is_public = bool(record.get("is_public"))
    target_is_public = bool(is_public)
    if current_is_public == target_is_public:
        return record, 0

    if record["status"] != STATUS_SUCCESS:
        raise DocumentStateError(
            f"仅已入库（success）的文档可切换可见性，当前状态：{record['status']}"
        )

    source_uid = PUBLIC_USER_ID if current_is_public else int(uid)
    target_uid = PUBLIC_USER_ID if target_is_public else int(uid)

    try:
        moved = move_document_chunks(str(doc_id), source_uid, target_uid)
    except VectorStoreError:
        # 向量搬移失败：可能已丢，标记 failed 让用户重新上传
        _mark_failed(uid, doc_id, VectorStoreError("向量搬移失败，请重新上传"))
        raise

    with db_session() as conn:
        conn.execute(
            "UPDATE documents SET is_public = ? WHERE id = ? AND user_id = ?",
            (1 if target_is_public else 0, doc_id, uid),
        )
        record = _find_document(conn, uid, doc_id)

    logger.info("文档可见性已切换：user_id=%s doc_id=%s is_public=%s moved=%d",
                uid, doc_id, target_is_public, moved)
    return record or {}, moved
