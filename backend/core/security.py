"""安全模块：密码哈希与 JWT 签发/解析。

设计说明：
- 直接使用原生 bcrypt（hashpw/checkpw），不引入 passlib，
  规避 passlib 1.7.4 与 bcrypt>=4.x 的自检兼容问题；
- bcrypt 仅处理前 72 字节，超长或含中文的密码会超限，
  故先对原始密码做 SHA256 + base64（44 字节 ASCII）再交给 bcrypt，
  既不截断也支持任意长度密码；
- JWT 密钥、算法、有效期统一取自 config.settings，过期时间可在 .env 配置。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from config import settings


class TokenError(Exception):
    """Token 解析失败（过期 / 非法签名 / 格式错误等），由上层转为 401。"""


def _bcrypt_key(raw_password: str) -> bytes:
    """将任意长度原始密码转为 bcrypt 可接受的定长密钥（44 字节 ASCII）。"""
    digest = hashlib.sha256(raw_password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(raw_password: str) -> str:
    """生成 bcrypt 密码哈希，返回可入库的字符串。"""
    return bcrypt.hashpw(_bcrypt_key(raw_password), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配；哈希格式损坏时返回 False 而非抛异常。"""
    try:
        return bcrypt.checkpw(_bcrypt_key(raw_password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, extra_claims: dict[str, Any] | None = None) -> str:
    """签发 access token。

    :param user_id: 用户 id，写入标准声明 sub（字符串形式）
    :param extra_claims: 需要附加的额外声明
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """解析并校验 JWT；过期或非法时抛出 TokenError。"""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("无效的登录凭证") from exc
