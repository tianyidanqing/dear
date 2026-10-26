# -*- coding: utf-8 -*-
"""「意图只由大模型判断」模式的回归测试。

设计变更（用户要求）：
  - 代码不能用关键词规则**替模型决定**意图——规则是闭集，遇到没覆盖的说法不会
    报错，只会硬塞进最接近的意图（历史 bug 都源于此）。
  - `use_rule_fallback` 默认 **False**：大模型不可用时**明确报错**。
  - 只有显式打开开关，才允许退化成关键词规则。
"""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schedule_agent.config import Settings, load_settings, set_settings   # noqa: E402
from schedule_agent.service.agent import ScheduleAgent                    # noqa: E402
from schedule_agent.storage.db import Store                               # noqa: E402


def _make(tmp_path, monkeypatch, fallback):
    """构造 agent：强制关闭大模型，用 fallback 控制是否允许规则兜底。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    store = Store(str(tmp_path / "t.db"))
    return ScheduleAgent(user_id="u", store=store,
                         cfg={"use_rule_fallback": fallback})


# --------------------------- 默认关闭 ---------------------------

def test_default_is_off():
    """不带任何配置时，默认不使用规则兜底。"""
    assert Settings().use_rule_fallback is False


def test_llm_down_raises_error_and_creates_nothing(tmp_path, monkeypatch):
    """模型不可用 + 开关关：必须报错，且一条任务都不能建。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)

    res = agent.chat("明天下午3点开会")

    assert res["ok"] is False
    assert res["action"] == "error"
    assert "大模型" in res["message"]
    assert len(agent.store.all("u")) == 0, "报错时绝不能偷偷建任务"


def test_error_message_contains_reason(tmp_path, monkeypatch):
    """报错要给出可排查的原因，而不是一句「出错了」。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)
    res = agent.chat("随便说点什么")
    assert "SCHEDULE_LLM_API_KEY" in res["message"]
    assert "SCHEDULE_USE_RULE_FALLBACK" in res["message"]


def test_add_task_refuses_without_llm(tmp_path, monkeypatch):
    """程序化入口 add_task 也不能在无模型时静默用规则解析。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)
    res = agent.add_task("明天下午3点开会")
    assert res["action"] == "error"
    assert len(agent.store.all("u")) == 0


def test_chat_stream_emits_error_event(tmp_path, monkeypatch):
    """流式通道同样报错，不产出任何任务。"""
    import asyncio

    agent = _make(tmp_path, monkeypatch, fallback=False)

    async def collect():
        return [ev async for ev in agent.chat_stream("明天10点开会")]

    events = asyncio.run(collect())
    assert events[0]["type"] == "error"
    assert "大模型" in events[0]["message"]
    assert len(agent.store.all("u")) == 0


# --------------------------- 开关打开才兜底 ---------------------------

def test_fallback_enabled_routes_by_rules(tmp_path, monkeypatch):
    """开关打开时才走关键词规则，并标记 degraded 便于前端提示。"""
    agent = _make(tmp_path, monkeypatch, fallback=True)

    res = agent.chat("今天上午10点 去自习 30分钟")

    assert res["action"] == "add"
    assert res.get("degraded") is True
    assert len(agent.store.all("u")) == 1


# --------------------------- 不再「猜」意图 ---------------------------

def _dispatch(agent, decision, text="随便"):
    return agent._dispatch(decision, text)


def test_alias_delete_not_treated_as_add(tmp_path, monkeypatch):
    """模型输出 delete/complete 等别名时，要归到正确动作，而不是漏成 add。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)
    agent.add_task(text="周报", start=datetime.now().replace(hour=9, minute=0,
                                                             second=0, microsecond=0),
                   skip_nlu=True)

    res = _dispatch(agent, {"intent": "delete", "title_hint": "周报"})
    assert res["action"] == "remove", res
    assert len([t for t in agent.store.all("u") if t.status != "removed"]) == 0


def test_alias_complete_not_treated_as_add(tmp_path, monkeypatch):
    agent = _make(tmp_path, monkeypatch, fallback=False)
    agent.add_task(text="周报", start=datetime.now().replace(hour=9, minute=0,
                                                             second=0, microsecond=0),
                   skip_nlu=True)

    res = _dispatch(agent, {"intent": "complete", "title_hint": "周报"})
    assert res["action"] == "done", res


def test_unknown_intent_does_not_create_task(tmp_path, monkeypatch):
    """模型给了系统不支持的动作名 -> 如实提问，绝不建成待办。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)
    res = _dispatch(agent, {"intent": "rename_owner", "title": "周报"}, "把周报换个负责人")
    assert res["action"] == "chat"
    assert len(agent.store.all("u")) == 0


def test_missing_intent_does_not_create_task(tmp_path, monkeypatch):
    """模型漏了 intent 字段（_coerce 不再默认 add）-> 提问而不是新增。"""
    agent = _make(tmp_path, monkeypatch, fallback=False)
    res = _dispatch(agent, {"intent": "", "title": "周报"}, "周报")
    assert res["action"] == "chat"
    assert len(agent.store.all("u")) == 0


# --------------------------- 配置优先级 ---------------------------

def test_env_overrides_config_file(tmp_path, monkeypatch):
    """环境变量优先于配置文件：两条都写 true/false 冲突时以环境变量为准。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"use_rule_fallback": False, "llm_model": "file-model"}),
                   encoding="utf-8")

    monkeypatch.setenv("SCHEDULE_CONFIG", str(cfg))
    monkeypatch.setenv("SCHEDULE_USE_RULE_FALLBACK", "1")
    monkeypatch.setenv("SCHEDULE_LLM_MODEL", "env-model")

    s = load_settings()
    assert s.use_rule_fallback is True, "环境变量应覆盖配置文件"
    assert s.llm_model == "env-model"


def test_config_file_used_when_env_absent(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"use_rule_fallback": True}), encoding="utf-8")
    monkeypatch.setenv("SCHEDULE_CONFIG", str(cfg))
    monkeypatch.delenv("SCHEDULE_USE_RULE_FALLBACK", raising=False)

    assert load_settings().use_rule_fallback is True


def test_bool_env_accepts_common_words(monkeypatch, tmp_path):
    for word, expect in [("true", True), ("TRUE", True), ("on", True), ("0", False),
                         ("false", False), ("off", False), ("no", False)]:
        monkeypatch.setenv("SCHEDULE_USE_RULE_FALLBACK", word)
        monkeypatch.setenv("SCHEDULE_CONFIG", str(tmp_path / "none.json"))
        assert load_settings().use_rule_fallback is expect, word


@pytest.fixture(autouse=True)
def _restore_settings():
    """避免本文件改动的全局 Settings 泄漏到其它用例。"""
    yield
    set_settings(Settings())
