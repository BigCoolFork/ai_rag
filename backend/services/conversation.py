"""会话与消息持久化服务。

职责：
- 会话（conversations）的增删改查；
- 消息（messages）的写入与读取；
- 每用户消息条数上限兜底（超限删除最旧消息，空会话级联删除）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from config import settings
from database import db_session, utc_now_iso
from schemas.chat import MessageOut, SourceItem

DEFAULT_TITLE = "新对话"


def create_conversation(user_id: int, title: str = DEFAULT_TITLE) -> int:
    """创建会话，返回会话 id。"""
    now = utc_now_iso()
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (str(user_id), title, now, now),
        )
        return int(cur.lastrowid)


def list_conversations(user_id: int) -> list[dict[str, Any]]:
    """列出当前用户的会话，按 updated_at 倒序。"""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (str(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(user_id: int, conv_id: int) -> dict[str, Any] | None:
    """获取单个会话（校验属主）。"""
    with db_session() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE id = ? AND user_id = ?",
            (conv_id, str(user_id)),
        ).fetchone()
    return dict(row) if row else None


def rename_conversation(user_id: int, conv_id: int, title: str) -> bool:
    """重命名会话。返回是否成功。"""
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (title, utc_now_iso(), conv_id, str(user_id)),
        )
        return cur.rowcount > 0


def delete_conversation(user_id: int, conv_id: int) -> bool:
    """删除会话（级联删除消息）。返回是否成功。"""
    with db_session() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, str(user_id)),
        )
        return cur.rowcount > 0


def list_messages(user_id: int, conv_id: int) -> list[MessageOut]:
    """获取某会话的消息列表（按 id 正序）。"""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, sources, hit, error, created_at "
            "FROM messages WHERE conversation_id = ? AND user_id = ? "
            "ORDER BY id ASC",
            (conv_id, str(user_id)),
        ).fetchall()
    return [_row_to_message(row) for row in rows]


def save_message(
    user_id: int,
    conv_id: int,
    role: str,
    content: str,
    sources: list[SourceItem] | None = None,
    hit: bool = True,
    error: str | None = None,
) -> int:
    """保存一条消息，返回消息 id。同时执行条数上限清理。"""
    sources_json = json.dumps([s.model_dump() for s in sources], ensure_ascii=False) if sources else None
    now = utc_now_iso()
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content, sources, hit, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, str(user_id), role, content, sources_json, 1 if hit else 0, error, now),
        )
        msg_id = int(cur.lastrowid)
        # 更新会话 updated_at
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )
    # 条数上限清理（独立事务，避免影响主写入）
    _enforce_message_limit(user_id)
    return msg_id


def touch_conversation_title(user_id: int, conv_id: int, question: str) -> None:
    """若会话标题仍为默认值，用首条提问前 20 字命名。"""
    title = question.strip()[:20] or DEFAULT_TITLE
    with db_session() as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ? AND title = ?",
            (title, conv_id, str(user_id), DEFAULT_TITLE),
        )


def _enforce_message_limit(user_id: int) -> None:
    """每用户消息条数超过上限时，删除最旧的消息；空会话随之删除。"""
    limit = settings.max_messages_per_user
    if limit <= 0:
        return
    with db_session() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()["c"]
        if total <= limit:
            return
        # 保留最新的 limit 条，删除其余
        excess = total - limit
        rows = conn.execute(
            "SELECT id, conversation_id FROM messages WHERE user_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (str(user_id), excess),
        ).fetchall()
        if not rows:
            return
        ids = [r["id"] for r in rows]
        conn.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        # 清理空会话
        conv_ids = {r["conversation_id"] for r in rows}
        for cid in conv_ids:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?",
                (cid,),
            ).fetchone()["c"]
            if cnt == 0:
                conn.execute(
                    "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                    (cid, str(user_id)),
                )


def _row_to_message(row: sqlite3.Row) -> MessageOut:
    sources: list[SourceItem] = []
    if row["sources"]:
        try:
            raw = json.loads(row["sources"])
            sources = [SourceItem(**item) for item in raw]
        except (json.JSONDecodeError, TypeError, ValueError):
            sources = []
    return MessageOut(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        sources=sources,
        hit=bool(row["hit"]),
        error=row["error"],
        created_at=row["created_at"],
    )
