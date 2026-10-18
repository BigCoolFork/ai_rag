"""认证路由：注册 / 登录 / 当前用户。

路由挂载前缀 /api/auth（见 main.py），故此处路径写：
- POST /register
- POST /login
- GET  /me
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status

from config import settings
from core.deps import get_current_user, get_db
from core.security import create_access_token, hash_password, verify_password
from database import utc_now_iso
from schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter()

# 查询用户时需要的列（对外）/ 登录时需要密码哈希的列
_PUBLIC_COLUMNS = "id, username, created_at"


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
)
def register(
    body: RegisterRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> UserResponse:
    """用户名 + 密码注册；用户名重复返回 409。"""
    # 唯一约束兜底前先做一次显式检查，返回更友好的错误信息
    exists = db.execute(
        "SELECT 1 FROM users WHERE username = ?", (body.username,)
    ).fetchone()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在",
        )

    cursor = db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (body.username, hash_password(body.password), utc_now_iso()),
    )
    db.commit()

    row = db.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM users WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return UserResponse.model_validate(dict(row))


@router.post("/login", response_model=TokenResponse, summary="用户登录")
def login(
    body: LoginRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> TokenResponse:
    """校验用户名密码并签发 JWT；任一错误统一返回 401（不区分用户是否存在）。"""
    row = db.execute(
        "SELECT id, password_hash FROM users WHERE username = ?",
        (body.username,),
    ).fetchone()

    # 用户不存在与密码错误给出完全相同的响应，避免账号枚举
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(row["id"])
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserResponse, summary="当前登录用户")
def me(current_user: sqlite3.Row = Depends(get_current_user)) -> UserResponse:
    """凭 Bearer token 获取当前用户信息；未登录返回 401。"""
    return UserResponse.model_validate(dict(current_user))
