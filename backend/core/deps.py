"""FastAPI 依赖注入：数据库连接与当前登录用户。

所有需要登录的接口（文档管理、问答）统一依赖 ``get_current_user``，
这是多用户隔离的第一道关口：未携带/携带非法或过期 token 一律 401。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from core.security import TokenError, decode_token
from database import get_connection

# tokenUrl 仅用于 OpenAPI 文档的 Authorize 按钮定位登录接口，
# 实际 token 提取逻辑只认 Authorization: Bearer <token> 请求头。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# 统一的 401（携带 WWW-Authenticate，符合 Bearer 认证规范）
_unauthorized = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="未登录或登录已失效，请重新登录",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_db() -> Iterator[sqlite3.Connection]:
    """请求级 SQLite 连接依赖，请求结束自动关闭。"""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    """解析 JWT 并返回当前用户记录（users 表的 Row）。

    失败场景全部返回 401：
    - 未携带 Authorization 头（由 oauth2_scheme 拦截）；
    - token 非法签名 / 已过期；
    - token 中的用户不存在（账号可能已被删除）。
    """
    try:
        payload = decode_token(token)
    except TokenError:
        raise _unauthorized

    subject = payload.get("sub")
    if not subject:
        raise _unauthorized

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise _unauthorized

    # 不下发 password_hash，只取业务需要的列
    row = db.execute(
        "SELECT id, username, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise _unauthorized
    return row


def get_current_user_sse(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    """SSE（EventSource）专用鉴权。

    浏览器原生 EventSource 无法自定义 Authorization 请求头，
    因此除 Bearer 头外额外支持 ``?token=<JWT>`` 查询参数；
    校验规则与 get_current_user 完全一致，失败同样返回 401。
    """
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.query_params.get("token", "").strip()

    if not token:
        raise _unauthorized

    try:
        payload = decode_token(token)
    except TokenError:
        raise _unauthorized

    subject = payload.get("sub")
    if not subject:
        raise _unauthorized
    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise _unauthorized

    row = db.execute(
        "SELECT id, username, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise _unauthorized
    return row
