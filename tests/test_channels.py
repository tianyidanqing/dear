# -*- coding: utf-8 -*-
"""渠道框架本身的测试：自动发现、配置优先级、以及「新增渠道不用改 core」。

这些用例守住的是可扩展性：
  - 子包放进去就被发现（不用维护清单）；
  - 配置支持 文件嵌套 / 文件旧扁平键 / 旧环境变量 / 新环境变量，且环境变量优先；
  - 任意第三方实现只需实现 parse/send，就能走通「收消息 → 建任务 → 回复」全链路。
"""
import json
import os
import sys
from dataclasses import dataclass
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schedule_agent.api.app import create_app                     # noqa: E402
from schedule_agent.channels import (                             # noqa: E402
    Channel,
    ChannelAuthError,
    ChannelConfig,
    IncomingMessage,
    discover,
    load_channels,
    names,
    register,
)
from schedule_agent.channels.config import collect                 # noqa: E402
from schedule_agent.channels.wecom.config import WeComConfig       # noqa: E402
from schedule_agent.config import Settings, set_settings           # noqa: E402
from schedule_agent.storage import auth as auth_store              # noqa: E402


# ---------------------------------------------------------------------------
# 1. 自动发现
# ---------------------------------------------------------------------------
def test_discover_finds_builtin_channels():
    found = discover()
    assert "wecom" in found
    assert found["wecom"].name == "wecom"


def test_template_scaffold_is_skipped():
    """`_template` 是脚手架，不该被当成真渠道。"""
    assert "_template" not in names()
    assert "template" not in names()


# ---------------------------------------------------------------------------
# 2. 配置解析与优先级
# ---------------------------------------------------------------------------
def test_config_from_dict_coerces_types():
    cfg = WeComConfig.from_dict({"corp_id": "ww1", "agent_id": 1000002,
                                 "bind_map": '{"zhangsan": "jiyu"}',
                                 "text_limit": "1024"})
    assert cfg.corp_id == "ww1"
    assert cfg.agent_id == "1000002"        # 数字统一转字符串
    assert cfg.bind_map == {"zhangsan": "jiyu"}   # JSON 字符串自动解析
    assert cfg.text_limit == 1024           # 字符串转 int


def test_config_missing_reports_required_fields():
    cfg = WeComConfig.from_dict({"corp_id": "ww1"})
    assert cfg.enabled is False
    assert set(cfg.missing()) == {"agent_id", "secret", "token", "aes_key"}


def test_config_legacy_flat_keys_still_work():
    """旧配置 `wecom_corp_id` 必须继续可用，否则升级即故障。"""
    out = collect({"wecom_corp_id": "ww-old", "wecom_secret": "s"})
    assert out["wecom"]["corp_id"] == "ww-old"
    assert out["wecom"]["secret"] == "s"


def test_config_nested_beats_flat():
    out = collect({"wecom_corp_id": "flat",
                   "channels": {"wecom": {"corp_id": "nested"}}})
    assert out["wecom"]["corp_id"] == "nested"


def test_config_env_beats_file(monkeypatch):
    monkeypatch.setenv("SCHEDULE_CHANNEL_WECOM_SECRET", "from-env")
    out = collect({"channels": {"wecom": {"secret": "from-file"}}})
    assert out["wecom"]["secret"] == "from-env"


def test_config_legacy_env_still_works(monkeypatch):
    monkeypatch.setenv("SCHEDULE_WECOM_TOKEN", "legacy-token")
    monkeypatch.delenv("SCHEDULE_CHANNEL_WECOM_TOKEN", raising=False)
    out = collect({})
    assert out["wecom"]["token"] == "legacy-token"


def test_config_empty_block_means_not_configured():
    """示例配置里留着空字符串，不该被当成「已配但残缺」去刷告警。"""
    out = collect({"channels": {"wecom": {"corp_id": "", "secret": ""}}})
    assert out == {}


def test_load_channels_skips_incomplete(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), log_level="WARNING",
                 channels={"wecom": {"corp_id": "only-this"}})
    assert load_channels(s) == {}


def test_load_channels_builds_when_complete(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), log_level="WARNING",
                 channels={"wecom": {"corp_id": "ww", "agent_id": "1000002",
                                     "secret": "s", "token": "t",
                                     "aes_key": "a" * 43}})
    chans = load_channels(s)
    assert "wecom" in chans
    assert chans["wecom"].callback_path == "/api/channels/wecom/callback"


def test_load_channels_reports_bad_config_without_crashing(tmp_path):
    """AESKey 长度不对只应跳过该渠道，不能让服务起不来。"""
    s = Settings(data_dir=str(tmp_path / "d"), log_level="WARNING",
                 channels={"wecom": {"corp_id": "ww", "agent_id": "1000002",
                                     "secret": "s", "token": "t",
                                     "aes_key": "too-short"}})
    assert load_channels(s) == {}


# ---------------------------------------------------------------------------
# 3. 新增渠道：只写 parse/send，不改 core
# ---------------------------------------------------------------------------
@dataclass
@dataclass
class DummyConfig(ChannelConfig):
    NAME = "dummy"
    DISPLAY_NAME = "测试渠道"
    REQUIRED = ("secret",)
    secret: str = ""


class DummyChannel(Channel):
    """一个假的第三方：JSON 收、内存发，用来验证框架本身。"""

    name = "dummy"
    display_name = "测试渠道"
    CONFIG = DummyConfig

    def __init__(self, cfg):
        super().__init__(cfg)
        self.outbox: list[tuple[str, str]] = []

    def parse(self, body, params):
        if params.get("sig") != self.cfg.secret:
            raise ChannelAuthError("签名不对")
        data = json.loads(body.decode("utf-8"))
        if not self.deduper.first_time(str(data.get("id") or "")):
            return None
        return IncomingMessage(channel=self.name, external_user=data["user"],
                               text=data["text"], msg_id=str(data.get("id") or ""))

    def send(self, external_user, text):
        self.outbox.append((external_user, text))


@pytest.fixture()
def dummy_app(tmp_path):
    register(DummyChannel)
    s = Settings(data_dir=str(tmp_path / "d"), log_level="WARNING",
                 use_rule_fallback=True,
                 channels={"dummy": {"secret": "s3cret"}})
    s.ensure_dirs()
    set_settings(s)
    auth_store.reset_conn()
    yield create_app(settings=s)
    auth_store.reset_conn()


def test_new_channel_mounts_without_core_change(dummy_app):
    with TestClient(dummy_app) as c:
        assert "dummy" in c.app.state.channels
        r = c.post("/api/channels/dummy/callback?sig=s3cret",
                   json={"id": "m1", "user": "u1", "text": "明天上午10点开会"})
        assert r.status_code == 200
        assert r.text == ""

        ch = c.app.state.channels["dummy"]
        assert len(ch.outbox) == 1, ch.outbox
        assert ch.outbox[0][0] == "u1"
        tasks = c.app.state.registry.get("dummy:u1").store.all("dummy:u1")
        assert len(tasks) == 1 and tasks[0].title == "开会"


def test_new_channel_rejects_bad_signature(dummy_app):
    with TestClient(dummy_app) as c:
        r = c.post("/api/channels/dummy/callback?sig=wrong",
                   json={"id": "m2", "user": "u1", "text": "明天10点开会"})
        assert r.status_code == 403
        assert c.app.state.channels["dummy"].outbox == []


def test_new_channel_dedupes(dummy_app):
    with TestClient(dummy_app) as c:
        payload = {"id": "m3", "user": "u2", "text": "明天10点开会"}
        c.post("/api/channels/dummy/callback?sig=s3cret", json=payload)
        c.post("/api/channels/dummy/callback?sig=s3cret", json=payload)
        ch = c.app.state.channels["dummy"]
        assert len(ch.outbox) == 1
        assert len(c.app.state.registry.get("dummy:u2").store.all("dummy:u2")) == 1


def test_unknown_channel_returns_503(dummy_app):
    with TestClient(dummy_app) as c:
        r = c.post("/api/channels/nope/callback", content=b"{}")
        assert r.status_code == 503
        assert "未配置" in r.text


def test_health_lists_enabled_channels(dummy_app):
    """运维要能一眼看出「哪些渠道真的启用了」，且不能泄露凭据。"""
    with TestClient(dummy_app) as c:
        data = c.get("/api/health").json()
        names = [x["name"] for x in data["channels"]]
        assert "dummy" in names
        assert "s3cret" not in json.dumps(data, ensure_ascii=False)
