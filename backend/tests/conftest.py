"""pytest 公共夹具。

关键：必须在 ``import main``（进而 import config）之前把 DATA_DIR
指向临时目录，保证测试使用独立 SQLite，绝不污染开发数据。
运行方式（在 backend/ 目录下）：
    python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ---- 在任何应用模块导入之前完成环境隔离 ----
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="rag_auth_test_"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["SECRET_KEY"] = "unit-test-secret-key"
os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "60"
# 固定模型配置：测试断言基于代码默认模型名，不能被开发机 .env（如临时切换 3B）污染
os.environ["LLM_MODEL"] = "qwen2.5:7b"
os.environ["EMBEDDING_MODEL"] = "nomic-embed-text"
os.environ["EMBEDDING_DIM"] = "768"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import db_session  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    """会话级 TestClient；with 块触发 lifespan（建表）。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_users_table(client: TestClient) -> None:
    """每个用例前清空 users 表，保证用例相互独立。

    显式依赖 client：即使单独运行不含 HTTP 用例的测试文件，
    也能先经由 TestClient 生命周期完成建表，避免 no such table。
    """
    with db_session() as conn:
        conn.execute("DELETE FROM users")
    yield
