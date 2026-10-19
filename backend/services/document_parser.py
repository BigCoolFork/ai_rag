"""文档解析模块：PDF / DOCX / XLSX 统一解析为 DocumentFragment。

设计约定（与 plan.md 元数据对齐，供后续向量化入库）：
- PDF（PyMuPDF）：按页提取，page 为真实页码（从 1 开始）；
- DOCX（python-docx）：按文档顺序提取段落与表格，追踪最近标题作为
  section；Word 没有可靠页码，page 固定 None，**禁止伪造页码**；
- XLSX（openpyxl 只读模式）：流式读取，工作表名作为 section，
  data_only=True 取单元格缓存计算结果（非公式文本）；Excel 无页码，
  page 固定 None；
- 所有类型输出统一的 DocumentFragment 结构；
- 文本清洗：去不可见控制字符、压缩空白、过滤页码/页眉页脚噪声。

业务异常全部可读（加密、损坏、格式不符、空文档），由上层直接回显用户。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from config import settings


# ---------------------------------------------------------------------------
# 统一数据结构与业务异常
# ---------------------------------------------------------------------------

@dataclass
class DocumentFragment:
    """文档原始片段（切片前）。一个片段对应 PDF 一页 / DOCX 一个章节块 / XLSX 一张表。"""

    text: str
    file_type: str            # pdf / docx / xlsx
    doc_name: str             # 原始文件名（展示与引用用）
    source_path: str          # 落盘绝对路径
    page: int | None = None   # PDF 真实页码；DOCX/XLSX 恒为 None
    section: str | None = None  # DOCX 最近标题 / XLSX 工作表名；PDF 为 None
    chunk_index: int | None = None  # 切片后由 text_splitter 写入序号

    def metadata(self) -> dict:
        """导出为 Milvus 入库所需的元数据（与 plan.md schema 对齐）。"""
        return {
            "doc_name": self.doc_name,
            "source_path": self.source_path,
            "file_type": self.file_type,
            "page": self.page,
            "section": self.section,
            "chunk_index": self.chunk_index,
        }


class DocumentParseError(Exception):
    """文档解析业务异常基类（信息可直接展示给用户）。"""


class UnsupportedFileTypeError(DocumentParseError):
    """文件类型不在白名单内。"""


class FileTooLargeError(DocumentParseError):
    """文件超过大小上限。"""


class EncryptedDocumentError(DocumentParseError):
    """文档已加密，需要密码才能读取。"""


class CorruptedDocumentError(DocumentParseError):
    """文件损坏或格式与扩展名不符（如 .doc 改名为 .docx）。"""


class EmptyDocumentError(DocumentParseError):
    """未提取到任何文本（如空白 PDF、扫描件图片型 PDF）。"""


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

# 不可打印控制字符（保留换行 \n 与制表 \t）
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 零宽字符 / BOM（U+200B ZWSP、U+200C ZWNJ、U+200D ZWJ、U+FEFF BOM）
_ZERO_WIDTH_CHARS = re.compile("[​‌‍﻿]")
# 连续空白（含中文全角空格 \u3000、制表符）压缩为单个普通空格
_BLANK_RUN = re.compile(r"[ \t　]+")
# 典型页码 / 页眉页脚噪声行
_PAGE_NOISE_PATTERNS = [
    re.compile(r"^[-–—_·.．\s]*\d+\s*(?:/|／)?\s*(?:共\s*)?\d*\s*页?[-–—_·.．\s]*$"),
    re.compile(r"^第\s*\d+\s*页(?:\s*[／/共]\s*\d+\s*页)?$"),
    re.compile(r"^page\s*\d+(?:\s*of\s*\d+)?$", re.IGNORECASE),
]
# 视为页眉页脚噪声行的最大长度（正文段落不会这么短又重复出现）
_HEADER_FOOTER_MAX_LEN = 30


def clean_inline(text: str) -> str:
    """清洗单行文本：去控制字符/零宽字符、压缩空白，不做行级过滤。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = _ZERO_WIDTH_CHARS.sub("", text)
    text = _BLANK_RUN.sub(" ", text)
    return text.strip()


def clean_text(text: str) -> str:
    """清洗整段文本：去噪行、去连续重复行、压缩空行。

    - 去除纯页码类噪声行（如「第 3 页」「- 12 -」「Page 5 of 10」）；
    - 相邻完全相同的行只保留一次（同页重复页眉页脚）。
    """
    if not text:
        return ""
    cleaned_lines: list[str] = []
    for raw_line in clean_inline(text).split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.match(line) for pattern in _PAGE_NOISE_PATTERNS):
            continue
        if cleaned_lines and cleaned_lines[-1] == line:
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def remove_repeated_header_footer(page_texts: list[str]) -> list[str]:
    """跨页去除重复页眉页脚：短行在超过一半页面出现即视为噪声。

    仅对至少 3 页的文档生效；每个页面内同一只计一次，
    避免长正文中偶发重复句被误删。
    """
    cleaned_pages = [clean_text(t).split("\n") for t in page_texts]
    if len(cleaned_pages) < 3:
        return ["\n".join(lines) for lines in cleaned_pages]

    page_occurrence: Counter[str] = Counter()
    for lines in cleaned_pages:
        page_occurrence.update({
            line for line in lines if 0 < len(line) <= _HEADER_FOOTER_MAX_LEN
        })
    threshold = len(cleaned_pages) / 2
    noisy = {line for line, count in page_occurrence.items() if count > threshold}

    result: list[str] = []
    for lines in cleaned_pages:
        kept = [line for line in lines if line not in noisy]
        result.append("\n".join(kept))
    return result


# ---------------------------------------------------------------------------
# PDF 解析（PyMuPDF，保留真实页码）
# ---------------------------------------------------------------------------

def parse_pdf(path: Path, doc_name: str, source_path: str) -> list[DocumentFragment]:
    """按页解析 PDF，每页输出一个 DocumentFragment（page 从 1 开始）。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 部署完整性问题
        raise DocumentParseError("缺少 PyMuPDF 依赖，无法解析 PDF") from exc

    try:
        doc = fitz.open(str(path))
    except (fitz.FileDataError, RuntimeError, OSError, ValueError) as exc:
        raise CorruptedDocumentError(
            f"PDF 文件已损坏或无法打开：{doc_name}（{type(exc).__name__}）"
        ) from exc

    try:
        if doc.needs_pass:
            raise EncryptedDocumentError(
                f"PDF 文档「{doc_name}」已加密，请去除打开密码后重新上传。"
            )

        raw_pages = [page.get_text("text") for page in doc]
    finally:
        doc.close()

    page_texts = remove_repeated_header_footer(raw_pages)
    fragments = [
        DocumentFragment(
            text=text,
            file_type="pdf",
            doc_name=doc_name,
            source_path=source_path,
            page=page_num,
        )
        for page_num, text in enumerate(page_texts, start=1)
        if text
    ]
    if not fragments:
        raise EmptyDocumentError(
            f"PDF「{doc_name}」未提取到任何文本，可能是扫描件/纯图片文档"
            "（当前版本暂不支持 OCR）。"
        )
    return fragments


# ---------------------------------------------------------------------------
# DOCX 解析（python-docx：段落 + 表格，追踪标题，不伪造页码）
# ---------------------------------------------------------------------------

def _is_heading(paragraph: object) -> bool:
    """判断段落是否为标题样式（兼容中英文样式名：Heading 1 / 标题 1）。"""
    style = getattr(paragraph, "style", None)
    style_name = getattr(style, "name", "") or ""
    return style_name.startswith("Heading") or style_name.startswith("标题")


def _table_to_text(table: object) -> str:
    """将 Word 表格按行序列化为「单元格 | 单元格」文本。"""
    lines: list[str] = []
    for row in table.rows:  # type: ignore[attr-defined]
        cells = [clean_inline(cell.text) for cell in row.cells]  # type: ignore[attr-defined]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def parse_docx(path: Path, doc_name: str, source_path: str) -> list[DocumentFragment]:
    """按文档顺序解析 DOCX 段落与表格；标题变更产生新片段，section 记录标题。"""
    try:
        from docx import Document as DocxDocument
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError("缺少 python-docx 依赖，无法解析 Word 文档") from exc

    try:
        doc = DocxDocument(str(path))
    except PackageNotFoundError as exc:
        # zip 包打不开：常见于损坏文件、加密 docx（OLE 复合文档）、旧版 .doc
        raise CorruptedDocumentError(
            f"Word 文档「{doc_name}」无法读取：文件可能已损坏、已加密，"
            "或为旧版 .doc 格式（请用 Word 另存为未加密的 .docx 后重试）。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底转成可读业务异常
        raise CorruptedDocumentError(
            f"Word 文档「{doc_name}」解析失败：{type(exc).__name__}: {exc}"
        ) from exc

    # iter_inner_content 按正文顺序产出 Paragraph / Table（python-docx >= 1.1.0）
    blocks = list(doc.iter_inner_content())

    fragments: list[DocumentFragment] = []
    current_section: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = clean_text("\n".join(buffer))
        if text:
            fragments.append(DocumentFragment(
                text=text,
                file_type="docx",
                doc_name=doc_name,
                source_path=source_path,
                page=None,  # Word 无可靠页码，禁止伪造
                section=current_section,
            ))

    for block in blocks:
        # 表格块：python-docx 的 Table 具备 rows 属性
        if hasattr(block, "rows"):
            table_text = _table_to_text(block)
            if table_text:
                buffer.append(table_text)
            continue

        # 段落块
        line = clean_inline(getattr(block, "text", ""))
        if not line:
            continue
        if _is_heading(block):
            # 标题切换：先封存上一章节，再以标题开启新章节缓冲
            flush()
            current_section = line
            buffer = [line]
        else:
            buffer.append(line)

    flush()

    if not fragments:
        raise EmptyDocumentError(f"Word 文档「{doc_name}」未提取到任何文本内容。")
    return fragments


# ---------------------------------------------------------------------------
# XLSX 解析（openpyxl 只读流式，工作表名为 section，data_only 取计算结果）
# ---------------------------------------------------------------------------

def parse_xlsx(path: Path, doc_name: str, source_path: str) -> list[DocumentFragment]:
    """逐工作表流式解析 XLSX，每张非空工作表输出一个片段。"""
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError("缺少 openpyxl 依赖，无法解析 Excel 文档") from exc

    try:
        # read_only=True：大文件低内存（按需流式读取行）；
        # data_only=True：取公式最近一次保存时的缓存计算结果而非公式文本。
        workbook = load_workbook(
            filename=str(path), read_only=True, data_only=True
        )
    except InvalidFileException as exc:
        message = str(exc).lower()
        if "encrypt" in message or "password" in message:
            raise EncryptedDocumentError(
                f"Excel 文档「{doc_name}」已加密，请去除打开密码后重新上传。"
            ) from exc
        raise CorruptedDocumentError(
            f"Excel 文件「{doc_name}」无法读取：文件可能已损坏或不是有效的 .xlsx。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - BadZipFile/OSError 等统一兜底
        raise CorruptedDocumentError(
            f"Excel 文件「{doc_name}」解析失败：{type(exc).__name__}: {exc}"
        ) from exc

    fragments: list[DocumentFragment] = []
    # xlsx 每行是一条独立记录，按 settings.xlsx_chunk_size 累积成行块：
    # - 不在行中间截断，保证一条收入/支出记录完整；
    # - 相比「整表一个巨型 fragment 再被 500 字符切分」，块数从数千降到数百，
    #   CPU 嵌入时间从不可接受降到可后台完成。
    target_chunk = max(200, settings.xlsx_chunk_size)
    header_repeat = settings.xlsx_header_repeat
    try:
        for worksheet in workbook.worksheets:
            row_iter = worksheet.iter_rows(values_only=True)

            # 第一行作为表头（若存在且非空），后续每个数据块前重复拼接，
            # 解决跨块后数据行丢失列语义的问题。
            header_line = ""
            if header_repeat:
                try:
                    first_row = next(row_iter)
                except StopIteration:
                    continue  # 空工作表，跳过
                header_cells = [
                    clean_inline(str(value))
                    for value in first_row
                    if value is not None and clean_inline(str(value))
                ]
                header_line = " | ".join(header_cells)

            buf: list[str] = []
            cur_len = 0
            for row in row_iter:
                cells = [
                    clean_inline(str(value))
                    for value in row
                    if value is not None and clean_inline(str(value))
                ]
                if not cells:
                    continue
                line = " | ".join(cells)
                buf.append(line)
                cur_len += len(line) + 1  # +1 留给换行符
                if cur_len >= target_chunk:
                    text = clean_text("\n".join([header_line] + buf)) \
                        if header_line else clean_text("\n".join(buf))
                    if text:
                        fragments.append(DocumentFragment(
                            text=text,
                            file_type="xlsx",
                            doc_name=doc_name,
                            source_path=source_path,
                            page=None,
                            section=worksheet.title,
                        ))
                    buf = []
                    cur_len = 0
            if buf:
                text = clean_text("\n".join([header_line] + buf)) \
                    if header_line else clean_text("\n".join(buf))
                if text:
                    fragments.append(DocumentFragment(
                        text=text,
                        file_type="xlsx",
                        doc_name=doc_name,
                        source_path=source_path,
                        page=None,
                        section=worksheet.title,
                    ))
    finally:
        workbook.close()

    if not fragments:
        raise EmptyDocumentError(
            f"Excel 文档「{doc_name}」未提取到任何数据（所有工作表均为空）。"
        )
    return fragments


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

_PARSERS = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "xlsx": parse_xlsx,
}


def parse_document(
    path: str | Path,
    file_type: str | None = None,
    doc_name: str | None = None,
) -> list[DocumentFragment]:
    """按扩展名分发到对应解析器，输出统一的 DocumentFragment 列表。

    :param path: 文件路径
    :param file_type: 显式指定类型；缺省取扩展名（不含点，小写）
    :param doc_name: 展示名；缺省取文件名
    """
    file_path = Path(path)
    file_type = (file_type or file_path.suffix.lstrip(".")).lower()
    parser = _PARSERS.get(file_type)
    if parser is None:
        allowed = " / ".join(settings.allowed_extensions)
        raise UnsupportedFileTypeError(
            f"不支持的文件类型「.{file_type}」，当前仅支持：{allowed}。"
        )
    name = doc_name or file_path.name
    return parser(file_path, name, str(file_path.resolve()))
