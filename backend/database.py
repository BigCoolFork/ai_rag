"""SQLite 数据库模块。

职责：
- 提供 SQLite 连接（标准库 ``sqlite3``，不引入额外数据库）；
- 应用启动时初始化 users / documents 两张表。

表结构与 plan.md 第三章一致：
- users：用户账号（id / 用户名 / 密码哈希 / 创建时间）；
- documents：文档登记（属主、原始名、SHA256 哈希文件名、类型 / 大小、
  pending / success / failed 状态、失败原因），同一用户内
  (user_id, doc_sha256) 唯一以支持上传去重。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from config import settings

# ---------------------------------------------------------------------------
# 建表 DDL
# ---------------------------------------------------------------------------

_CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
"""

# 状态枚举：pending 已落盘待解析 / success 已向量化入库 / failed 解析或入库失败
_CREATE_DOCUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT    NOT NULL,             -- 属主用户 id（与 users.id 对应）
    doc_sha256       TEXT    NOT NULL,             -- 文件完整 SHA256，同用户内去重
    original_name    TEXT    NOT NULL,             -- 用户上传时的原始文件名
    stored_filename  TEXT    NOT NULL,             -- 磁盘哈希文件名 {sha256}.{ext}
    file_type        TEXT    NOT NULL,             -- pdf / docx / xlsx
    file_size        INTEGER NOT NULL,             -- 文件字节大小
    created_at       TEXT    NOT NULL,             -- 创建时间（UTC ISO8601）
    status           TEXT    NOT NULL DEFAULT 'pending',
    error_msg        TEXT,                          -- 失败原因，成功时为 NULL
    total_chunks     INTEGER NOT NULL DEFAULT 0,    -- 切片总数（解析后填入，用于进度）
    processed_chunks INTEGER NOT NULL DEFAULT 0,    -- 已向量化切片数（后台分批更新）
    UNIQUE (user_id, doc_sha256)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_documents_user_id
ON documents (user_id);
"""

# 历史库增量迁移：旧 documents 表缺 total_chunks / processed_chunks 列时补齐。
# SQLite 不支持 ADD COLUMN IF NOT EXISTS，故用 PRAGMA 检测后再 ALTER。
_DOCUMENT_EXTRA_COLUMNS = {
    "total_chunks": "INTEGER NOT NULL DEFAULT 0",
    "processed_chunks": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_documents_schema(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }
    for col, decl in _DOCUMENT_EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串（统一时间存储格式）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    """获取一个 SQLite 连接。

    - row_factory 设置为 Row，查询结果可按列名访问；
    - 开启外键约束（支持 ON DELETE CASCADE）与 WAL，兼顾并发读。

    注意：连接由调用方负责关闭；FastAPI 依赖中请使用 ``db_session``。
    """
    settings.setup_paths()
    conn = sqlite3.connect(
        settings.sqlite_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """事务上下文：正常退出提交，异常回滚并关闭连接。"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库表（幂等，应用启动时调用）。"""
    settings.setup_paths()
    with db_session() as conn:
        conn.execute(_CREATE_USERS_SQL)
        conn.execute(_CREATE_DOCUMENTS_SQL)
        _migrate_documents_schema(conn)
        conn.execute(_CREATE_INDEX_SQL)


if __name__ == "__main__":
    # 允许直接执行：python database.py —— 仅建表，不写入任何业务数据
    init_db()
    print(f"SQLite initialized at: {settings.sqlite_path}")
