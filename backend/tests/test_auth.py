"""认证模块单元测试：注册 / 登录 / me / 密码哈希 / JWT 校验。

在 backend/ 目录下运行：
    python -m pytest tests/test_auth.py -v
"""

from __future__ import annotations

import jwt
from fastapi.testclient import TestClient

from config import settings
from core.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def test_register_success(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "alice"
    assert isinstance(data["id"], int) and data["id"] > 0
    assert "created_at" in data
    # 响应中绝不能出现密码相关字段
    assert "password" not in data and "password_hash" not in data


def test_register_duplicate_username_conflict(client: TestClient) -> None:
    payload = {"username": "bob", "password": "secret123"}
    assert client.post("/api/auth/register", json=payload).status_code == 201
    resp = client.post("/api/auth/register", json=payload)
    assert resp.status_code == 409
    assert "已存在" in resp.json()["detail"]


def test_register_invalid_input_returns_422(client: TestClient) -> None:
    # 用户名过短
    r1 = client.post("/api/auth/register", json={"username": "ab", "password": "secret123"})
    # 密码过短
    r2 = client.post("/api/auth/register", json={"username": "charlie", "password": "12345"})
    assert r1.status_code == 422 and r2.status_code == 422


def test_register_username_is_trimmed(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"username": "  dave  ", "password": "secret123"},
    )
    assert resp.status_code == 201
    assert resp.json()["username"] == "dave"


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def _register(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert resp.status_code == 201


def test_login_success_returns_jwt(client: TestClient) -> None:
    _register(client, "erin", "secret123")
    resp = client.post(
        "/api/auth/login", json={"username": "erin", "password": "secret123"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.access_token_expire_minutes * 60
    # 签发的 token 必须能被安全模块解析，且 sub 为用户 id
    payload = decode_token(data["access_token"])
    assert payload["sub"].isdigit()
    assert payload["type"] == "access"


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    _register(client, "frank", "secret123")
    resp = client.post(
        "/api/auth/login", json={"username": "frank", "password": "wrong-pass"}
    )
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_login_nonexistent_user_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "ghost", "password": "whatever1"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /me 鉴权
# ---------------------------------------------------------------------------

def test_me_with_valid_token(client: TestClient) -> None:
    _register(client, "grace", "secret123")
    token = client.post(
        "/api/auth/login", json={"username": "grace", "password": "secret123"}
    ).json()["access_token"]

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "grace"


def test_me_without_token_returns_401(client: TestClient) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_with_garbage_token_returns_401(client: TestClient) -> None:
    resp = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401


def test_me_with_token_of_deleted_user_returns_401(client: TestClient) -> None:
    _register(client, "henry", "secret123")
    token = create_access_token(999999)  # 数据库中不存在的用户 id
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# security 纯函数
# ---------------------------------------------------------------------------

def test_password_hash_and_verify() -> None:
    raw = "My-密码-123"
    hashed = hash_password(raw)
    assert hashed != raw  # 哈希不可逆，不是明文
    assert verify_password(raw, hashed) is True
    assert verify_password("wrong", hashed) is False
    # 同一密码两次哈希结果不同（bcrypt 自带盐）
    assert hash_password(raw) != hashed
    # 损坏的哈希字符串应安全返回 False，而不是抛异常
    assert verify_password(raw, "not-a-bcrypt-hash") is False


def test_long_chinese_password_exceeds_72_bytes() -> None:
    """中文每字 3 字节，30 字即 90 字节；SHA256 预处理后 bcrypt 仍可正常工作。"""
    raw = "超长中文密码" * 10
    assert len(raw.encode("utf-8")) > 72
    hashed = hash_password(raw)
    assert verify_password(raw, hashed) is True


def test_expired_token_rejected() -> None:
    # 手工构造一个已过期 token
    expired = jwt.encode(
        {"sub": "1", "exp": 1, "iat": 1},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    try:
        decode_token(expired)
    except TokenError:
        return
    raise AssertionError("过期 token 应抛出 TokenError")
