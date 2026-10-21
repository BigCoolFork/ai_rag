"""文档管理路由：上传 / 列表 / 删除。

路由已在 main.py 挂载到前缀 ``/api/documents``，故此处路径写：
- POST   /upload          multipart/form-data 上传，同步解析切片并向量化
- GET    /list            当前用户文档分页列表
- DELETE /{doc_id}        删除登记记录并联动删除 Milvus 向量切片

所有接口必须携带有效 JWT（get_current_user），确保用户间数据隔离。
业务异常统一映射为结构化错误体：
    {"detail": {"code": "ERROR_CODE", "message": "可读原因"}}
"""

from __future__ import annotations

import sqlite3

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from core.deps import get_current_user
from schemas.document import (
    DocumentChunksResp,
    DocumentChunkItem,
    DocumentDeleteResp,
    DocumentListItem,
    DocumentListResponse,
    DocumentRecord,
    DocumentUploadResp,
    DocumentVisibilityResp,
)
from services.document_parser import (
    CorruptedDocumentError,
    DocumentParseError,
    EmptyDocumentError,
    EncryptedDocumentError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from services.upload_ingest import (
    DocumentNotFoundError,
    DocumentStateError,
    UploadError,
    UploadStorageError,
    delete_document,
    enqueue_upload,
    list_documents,
    process_document_ingest,
    set_document_visibility,
)
from services.vector_store import VectorStoreError, get_document_chunks

router = APIRouter()


def _error(status_code: int, code: str, message: str) -> HTTPException:
    """构造统一结构化错误。"""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _raise_upload_error(exc: Exception) -> None:
    """把业务异常映射为 HTTP 结构化错误（子类判断必须在基类之前）。"""
    if isinstance(exc, UnsupportedFileTypeError):
        raise _error(400, "UNSUPPORTED_FILE_TYPE", str(exc))
    if isinstance(exc, FileTooLargeError):
        raise _error(400, "FILE_TOO_LARGE", str(exc))
    if isinstance(exc, EncryptedDocumentError):
        raise _error(422, "ENCRYPTED_DOCUMENT", str(exc))
    if isinstance(exc, CorruptedDocumentError):
        raise _error(422, "CORRUPTED_DOCUMENT", str(exc))
    if isinstance(exc, EmptyDocumentError):
        raise _error(422, "EMPTY_DOCUMENT", str(exc))
    if isinstance(exc, DocumentParseError):
        raise _error(422, "DOCUMENT_PARSE_ERROR", str(exc))
    if isinstance(exc, UploadStorageError):
        raise _error(500, "FILE_STORAGE_ERROR", str(exc))
    if isinstance(exc, VectorStoreError):
        raise _error(503, "VECTOR_STORE_ERROR", str(exc))
    if isinstance(exc, UploadError):
        raise _error(400, "UPLOAD_ERROR", str(exc))
    # 兜底：不向前端泄露内部堆栈
    raise _error(500, "INTERNAL_ERROR", f"文档入库失败：{type(exc).__name__}")


@router.post(
    "/upload",
    response_model=DocumentUploadResp,
    status_code=status.HTTP_200_OK,
    summary="上传文档（PDF/DOCX/XLSX）",
)
def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="文档文件，仅支持 pdf/docx/xlsx"),
    is_public: int = Form(0, ge=0, le=1, description="0=私有（仅自己可见），1=公开（所有用户可检索）"),
    current_user: sqlite3.Row = Depends(get_current_user),
) -> DocumentUploadResp:
    """同步落盘 + 登记 pending，解析与向量化放后台异步执行。

    - 成功入队 / 命中去重：200，document.status 为 pending（去重时为 success）；
    - 类型 / 大小不合法 / 磁盘写入失败：400/500（同步阶段校验，立即返回）；
    - 解析 / 加密 / 损坏 / 向量库异常：在后台标记 status=failed，可通过列表接口查看。
    """
    try:
        file_bytes = file.file.read()
    except OSError as exc:
        raise _error(400, "UPLOAD_ERROR", f"读取上传文件失败：{exc}") from exc

    try:
        result, stored_path = enqueue_upload(
            user_id=str(current_user["id"]),
            file_bytes=file_bytes,
            original_filename=file.filename or "",
            is_public=bool(is_public),
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射，不外泄原生异常
        _raise_upload_error(exc)

    # 命中已有 success 记录：不重复后台入库
    if not result.duplicated:
        background_tasks.add_task(
            process_document_ingest,
            str(current_user["id"]),
            result.record["id"],
            stored_path,
            file.filename or result.record["original_name"],
        )

    return DocumentUploadResp(
        duplicated=result.duplicated,
        chunk_count=result.chunk_count,
        document=DocumentRecord(**result.record),
    )


@router.get(
    "/list",
    response_model=DocumentListResponse,
    summary="当前用户文档列表（分页）",
)
def list_user_documents(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，1-100"),
    current_user: sqlite3.Row = Depends(get_current_user),
) -> DocumentListResponse:
    """仅返回当前登录用户的文档，按入库时间倒序。"""
    rows, total = list_documents(
        user_id=str(current_user["id"]), page=page, page_size=page_size
    )
    return DocumentListResponse(
        items=[DocumentListItem(**row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/{doc_id}",
    response_model=DocumentDeleteResp,
    summary="删除文档及其全部向量切片",
)
def remove_document(
    doc_id: int,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> DocumentDeleteResp:
    """联动删除：Milvus 向量切片 → SQLite 登记记录 → 磁盘文件。

    - 不存在或不属于当前用户：404；
    - 向量库删除失败：503，登记记录与文件保留不动。
    """
    try:
        _record, chunks_removed = delete_document(
            user_id=str(current_user["id"]), doc_id=doc_id
        )
    except DocumentNotFoundError as exc:
        raise _error(404, "DOCUMENT_NOT_FOUND", str(exc)) from exc
    except VectorStoreError as exc:
        raise _error(503, "VECTOR_STORE_ERROR", str(exc)) from exc

    return DocumentDeleteResp(id=doc_id, deleted=True, chunks_removed=chunks_removed)


@router.put(
    "/{doc_id}/visibility",
    response_model=DocumentVisibilityResp,
    summary="切换文档公开/私有（搬移向量到对应 collection）",
)
def update_document_visibility(
    doc_id: int,
    is_public: int = Body(..., ge=0, le=1, embed=True, description="0=私有，1=公开"),
    current_user: sqlite3.Row = Depends(get_current_user),
) -> DocumentVisibilityResp:
    """在用户私有 collection 与公共 collection 间搬移文档向量。

    - 不存在或不属于当前用户：404；
    - 文档状态非 success（pending/failed）：400；
    - 向量库搬移失败：503（此时文档可能被标记为 failed）。
    """
    try:
        record, moved = set_document_visibility(
            user_id=str(current_user["id"]),
            doc_id=doc_id,
            is_public=bool(is_public),
        )
    except DocumentNotFoundError as exc:
        raise _error(404, "DOCUMENT_NOT_FOUND", str(exc)) from exc
    except DocumentStateError as exc:
        raise _error(400, "DOCUMENT_STATE_ERROR", str(exc)) from exc
    except VectorStoreError as exc:
        raise _error(503, "VECTOR_STORE_ERROR", str(exc)) from exc

    return DocumentVisibilityResp(
        id=doc_id,
        is_public=int(record.get("is_public", 0)),
        chunks_moved=moved,
    )


@router.get(
    "/{doc_id}/chunks",
    response_model=DocumentChunksResp,
    summary="获取文档全部切片（按 chunk_id 升序，用于预览定位）",
)
def get_document_chunks_api(
    doc_id: int,
    current_user: sqlite3.Row = Depends(get_current_user),
) -> DocumentChunksResp:
    """返回该文档的全部切片，按向量库主键升序（≈ 文档原始顺序）。

    - 不存在或不属于当前用户：404；
    - 向量库异常：503。
    """
    try:
        chunks = get_document_chunks(
            user_id=int(current_user["id"]), doc_id=str(doc_id)
        )
    except VectorStoreError as exc:
        raise _error(503, "VECTOR_STORE_ERROR", str(exc)) from exc

    if not chunks:
        raise _error(404, "DOCUMENT_NOT_FOUND", f"文档 {doc_id} 不存在或无切片数据")

    return DocumentChunksResp(
        doc_id=doc_id,
        doc_name=chunks[0].doc_name,
        file_type=chunks[0].file_type,
        chunks=[
            DocumentChunkItem(
                chunk_id=c.chunk_id,
                text=c.text,
                page=c.page,
                section=c.section,
            )
            for c in chunks
        ],
    )
