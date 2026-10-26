# -*- coding: utf-8 -*-
"""pytest 公共夹具：每个用例都用独立的临时数据目录，互不影响。"""
import os

import pytest
from fastapi.testclient import TestClient

from schedule_agent.api.app import create_app
from schedule_agent.config import Settings, set_settings
from schedule_agent.storage import auth as auth_store

# 测试不读开发者本机的 .env，避免本地配置影响断言
os.environ.setdefault("SCHEDULE_NO_DOTENV", "1")


@pytest.fixture
def settings(tmp_path):
    """注入一份指向临时目录的配置。"""
    s = Settings(
        data_dir=str(tmp_path / "data"),
        lead_minutes=1440,          # 放宽提醒窗口，便于测到「即将开始」
        interval=1,                 # 提醒引擎快速轮询
        daily_summary_time="00:00",
        admin_api_key="test-admin-key",
        log_level="WARNING",
        # 单元测试不连真实大模型，必须显式打开规则兜底才测得到路由逻辑。
        # 生产默认是 False：模型不可用就报错，绝不用关键词规则猜意图。
        use_rule_fallback=True,
    )
    s.ensure_dirs()
    set_settings(s)
    auth_store.reset_conn()         # 换库必须重开连接，否则用例之间串数据
    yield s
    auth_store.reset_conn()


@pytest.fixture
def app(settings):
    return create_app(settings=settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def register(client, user_id, password=None):
    """注册并返回 api_key。"""
    r = client.post("/api/auth/register",
                    json={"user_id": user_id, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


@pytest.fixture
def alice(client):
    return register(client, "alice", "pw123")


@pytest.fixture
def bob(client):
    return register(client, "bob")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}
