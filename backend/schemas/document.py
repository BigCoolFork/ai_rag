"""文档管理相关 Pydantic 模型。

与 documents 表字段一一对应（``DocumentRecord``），列表接口使用精简的
``DocumentListItem``，上传响应额外携带是否命中去重与本次切片数。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 文档状态枚举（与 database.py DDL 注释保持同步）
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class DocumentRecord(BaseModel):
    """documents 表完整记录。"""

    id: int
    user_id: str
    doc_sha256: str
    original_name: str
    stored_filename: str
    file_type: str  # pdf / docx / xlsx
    file_size: int
    status: str  # pending / success / failed
    error_msg: str | None = None
    created_at: str
    total_chunks: int = 0       # 切片总数（解析后填入）
    processed_chunks: int = 0   # 已向量化切片数（后台分批更新）
    is_public: int = 0          # 0=私有，1=公开


class DocumentUploadResp(BaseModel):
    """上传响应：document 为登记记录；duplicated 表示命中同用户哈希去重。"""

    duplicated: bool = False
    # 仅在本次实际入库时有值；命中去重时为 None
    chunk_count: int | None = None
    document: DocumentRecord


class DocumentListItem(BaseModel):
    """文档列表项（列表场景的精简字段）。"""

    id: int
    original_name: str
    file_type: str
    file_size: int
    status: str
    error_msg: str | None = None
    created_at: str
    total_chunks: int = 0
    processed_chunks: int = 0
    is_public: int = 0          # 0=私有，1=公开


class DocumentListResponse(BaseModel):
    """分页文档列表。"""

    items: list[DocumentListItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class DocumentDeleteResp(BaseModel):
    """删除响应：确认登记记录与向量切片均已删除。"""

    id: int
    deleted: bool = True
    chunks_removed: int = -1  # Milvus 无精确计数时为 -1


class DocumentChunkItem(BaseModel):
    """文档单条切片（预览用，按 chunk_id 升序）。"""

    chunk_id: int
    text: str
    page: int | None = None
    section: str | None = None


class DocumentChunksResp(BaseModel):
    """文档全部切片响应。"""

    doc_id: int
    doc_name: str
    file_type: str
    chunks: list[DocumentChunkItem] = Field(default_factory=list)
