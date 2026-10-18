"""文档入库编排模块。

当前（任务4）提供文件级入口 ``ingest_file``：
1. 校验文件存在性、扩展名白名单、大小上限；
2. 调用 document_parser 解析（加密 / 损坏 / 空文档抛出可读业务异常）；
3. 调用 text_splitter 切片，返回带元数据的 DocumentFragment 列表。

任务6 将在此基础上继续完整入库流水线（上传落盘后执行）：
4. 计算 SHA256，按 {sha256}.{ext} 落盘到 uploads/{user_id}/；
5. 同用户 (user_id, file_hash) 去重；
6. embeddings 向量化 → vector_store 写入用户专属 collection；
7. documents 表登记 chunk_count / 状态，失败回滚。
"""

from __future__ import annotations

from pathlib import Path

from config import settings
from services.document_parser import (
    DocumentFragment,
    DocumentParseError,
    EmptyDocumentError,
    FileTooLargeError,
    UnsupportedFileTypeError,
    parse_document,
)
from services.text_splitter import split_fragments


def validate_upload_file(path: str | Path) -> Path:
    """校验上传文件的类型与大小，返回规范化后的 Path。

    失败时抛出可直接回显用户的业务异常：
    - 文件不存在 / 不是普通文件：DocumentParseError
    - 扩展名不在白名单：UnsupportedFileTypeError
    - 超过 MAX_UPLOAD_MB：FileTooLargeError
    """
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise DocumentParseError(f"文件不存在或无法读取：{file_path}")

    ext = file_path.suffix.lstrip(".").lower()
    if ext not in settings.allowed_extensions:
        allowed = " / ".join(settings.allowed_extensions)
        raise UnsupportedFileTypeError(
            f"不支持的文件类型「.{ext}」，仅支持：{allowed}。"
        )

    size_bytes = file_path.stat().st_size
    if size_bytes > settings.max_upload_bytes:
        raise FileTooLargeError(
            f"文件过大：{size_bytes / 1024 / 1024:.1f}MB，"
            f"上限为 {settings.max_upload_mb}MB。"
        )
    return file_path


def ingest_file(path: str | Path, doc_name: str | None = None) -> list[DocumentFragment]:
    """文件解析 + 切片上层入口（向量化之前的全部本地处理）。

    :param doc_name: 引用展示用的文档名；上传入库时必须传用户可见的原始文件名，
                     缺省（如本地脚本直接调用）才回退为磁盘文件名（可能是哈希存储名）
    :return: 切分后的 DocumentFragment 列表（含 page/section 等元数据与
             全局 chunk_index），可直接交给任务6 的向量化入库环节
    :raises DocumentParseError 及其子类: 校验/解析各环节的可读错误
    """
    file_path = validate_upload_file(path)
    # parse_document 内部会对加密 PDF / 损坏 docx/xlsx / 空文档抛出业务异常
    fragments = parse_document(file_path, doc_name=doc_name)
    chunks = split_fragments(fragments)
    if not chunks:
        raise EmptyDocumentError(
            f"文档「{file_path.name}」切片结果为空，无法入库。"
        )
    return chunks


# TODO(任务6):
#   def ingest_upload(user_id: int, original_name: str, payload: bytes) -> DocumentRecord
#       - SHA256 落盘 uploads/{user_id}/{sha256}.{ext}
#       - SQLite (user_id, file_hash) 去重
#       - chunks = ingest_file(stored_path)
#       - embeddings 向量化 → vector_store.add_chunks(user_id, doc_id, chunks)
#       - 登记 documents（chunk_count / status=ready），失败回滚
#   def remove_document(user_id: int, doc_id: int) -> None
