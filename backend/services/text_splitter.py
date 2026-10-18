"""文本分块模块：中文优先的递归切片。

- 使用 langchain_text_splitters.RecursiveCharacterTextSplitter；
- 分隔符按「段落 → 中文句读 → 西文句读 → 空格 → 强制切分」排序，
  尽量在语义自然边界断开；
- chunk_size / chunk_overlap 全部从 settings 读取（.env 可调）；
- 切片不跨原始片段：每个 PDF 页 / DOCX 章节块 / XLSX 工作表独立切分，
  因此每个切片都能保留准确的 page / section 元数据，杜绝来源错位。
"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import settings
from services.document_parser import DocumentFragment

# 中文优先分隔符（按优先级从高到低）
CHINESE_SEPARATORS: list[str] = [
    "\n\n",  # 段落
    "\n",    # 换行（表格行 / 列表）
    "。", "！", "？", "；",              # 中文句读
    ". ", "! ", "? ", "; ",            # 西文句读
    "，", "、", ", ",                   # 中文/西文停顿
    " ", "",                            # 空格 / 强制切分兜底
]


def build_splitter() -> RecursiveCharacterTextSplitter:
    """按当前配置构建递归分块器。"""
    if settings.chunk_overlap >= settings.chunk_size:
        raise ValueError(
            f"切片配置非法：CHUNK_OVERLAP({settings.chunk_overlap}) 必须小于 "
            f"CHUNK_SIZE({settings.chunk_size})。"
        )
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=CHINESE_SEPARATORS,
        length_function=len,
        keep_separator=True,
    )


def split_fragments(
    fragments: list[DocumentFragment],
    splitter: RecursiveCharacterTextSplitter | None = None,
) -> list[DocumentFragment]:
    """将原始片段切分为带全局序号的小块。

    - 每个原始片段独立切分，元数据（page / section / doc_name 等）原样继承；
    - chunk_index 按文档顺序全局递增，作为入库与引用定位的稳定序号。

    :param fragments: document_parser 解析出的原始片段
    :param splitter: 可注入自定义分块器（测试用）；缺省按 settings 构建
    """
    splitter = splitter or build_splitter()
    chunks: list[DocumentFragment] = []

    for fragment in fragments:
        # xlsx 已在解析阶段按行累积成目标大小的块（保证每条记录不被截断），
        # 此处不再二次切分，直接作为最终 chunk 入库；
        # PDF / DOCX 仍走递归分块器，保留段落与章节边界。
        if fragment.file_type == "xlsx":
            piece = fragment.text.strip()
            if piece:
                chunks.append(DocumentFragment(
                    text=piece,
                    file_type=fragment.file_type,
                    doc_name=fragment.doc_name,
                    source_path=fragment.source_path,
                    page=fragment.page,
                    section=fragment.section,
                    chunk_index=len(chunks),
                ))
            continue
        for piece in splitter.split_text(fragment.text):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(DocumentFragment(
                text=piece,
                file_type=fragment.file_type,
                doc_name=fragment.doc_name,
                source_path=fragment.source_path,
                page=fragment.page,       # 不跨页切分，页码准确
                section=fragment.section, # 不跨章节切分，定位准确
                chunk_index=len(chunks),
            ))
    return chunks
