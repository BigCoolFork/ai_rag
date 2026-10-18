"""认证相关 Pydantic 请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    """注册请求体。"""

    # 允许中英文、数字、下划线与连字符，长度 3~50
    username: str = Field(min_length=3, max_length=50, description="用户名")
    password: str = Field(min_length=6, max_length=128, description="密码")

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        return value


class LoginRequest(BaseModel):
    """登录请求体（JSON）。"""

    username: str = Field(min_length=1, max_length=50, description="用户名")
    password: str = Field(min_length=1, max_length=128, description="密码")

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str) -> str:
        return value.strip()


class TokenResponse(BaseModel):
    """登录成功响应：JWT 访问令牌。"""

    access_token: str = Field(description="JWT 访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(description="有效期（秒）")


class UserResponse(BaseModel):
    """用户信息响应（不含任何密码字段）。"""

    # 允许从 sqlite3.Row 转成的 dict 直接构造
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: str
