# -*- coding: utf-8 -*-
"""「只改优先级」意图的回归测试。

背景（真实 bug）：用户说「ai开发设置为高优先级」，系统把它当成**新增任务**，
凭空多出《ai开发》一条待办；因为没给时间，还被排到了「当前时刻」（凌晨 00:58）。
根因有两条：
  1) NLU 的 intent 枚举里没有「改优先级」，模型只能判 add；
  2) modify_task 只接受时间参数，即使判成 modify 也改不了优先级。
这两个漏洞都在此文件里钉住。
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schedule_agent.domain import nlp                       # noqa: E402
from schedule_agent.domain.models import Priority          # noqa: E402
from schedule_agent.service.agent import ScheduleAgent     # noqa: E402
from schedule_agent.storage.db import Store                # noqa: E402


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    """独立 DB + 关闭大模型，专测规则与服务层（避免真实 API 干扰）。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    store = Store(str(tmp_path / "t.db"))
    return ScheduleAgent(user_id="tester", store=store, cfg={"use_rule_fallback": True})


def _find(agent, title):
    hits = [t for t in agent.store.all("tester") if t.title == title]
    assert len(hits) == 1, [t.title for t in agent.store.all("tester")]
    return hits[0]


# --------------------------- 领域层：解析 ---------------------------

def test_parse_priority_basic():
    r = nlp.parse_priority("ai开发设置为高优先级")
    assert r == {"title_hint": "ai开发", "priority": Priority.HIGH}


def test_parse_priority_variants():
    assert nlp.parse_priority("把学习ai开发设为低优先级")["priority"] == Priority.LOW
    assert nlp.parse_priority("学习ai开发的优先级改成中")["priority"] == Priority.MEDIUM
    r = nlp.parse_priority("自习优先级调高")
    assert r["title_hint"] == "自习" and r["priority"] == Priority.HIGH


def test_parse_priority_ignores_new_task_with_time():
    """带新时间的句子是「安排新任务」，不能误判成改优先级。"""
    assert nlp.parse_priority("下午3点开会 优先级高") is None
    assert nlp.parse_priority("明天下午3点开会，优先级高") is None


def test_parse_priority_bare_not_hit():
    """没有可匹配的目标时返回空 hint，由上层追问「哪一件」，而不是瞎猜标题。"""
    r = nlp.parse_priority("优先级调高")
    assert r is not None and r["title_hint"] == "" and r["priority"] == Priority.HIGH
    assert nlp.parse_priority("今天有什么安排") is None


# --------------------------- 服务层：修改 ---------------------------

def test_priority_change_does_not_create_task(agent):
    """核心回归：改优先级不能新增任务。"""
    agent.add_task(text="学习ai开发", start=datetime.now().replace(hour=15, minute=0, second=0, microsecond=0))
    before = len(agent.store.all("tester"))

    res = agent.modify_task(title_hint="学习ai开发", priority=Priority.HIGH)

    assert res["ok"] is True
    assert res["action"] == "modify"
    assert len(agent.store.all("tester")) == before, "改优先级不应新增任务"
    assert "高" in res["message"]


def test_priority_change_keeps_time(agent):
    """只改优先级时，时间与提醒状态不能被清空。"""
    start = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    agent.add_task(text="学习ai开发", start=start)

    agent.modify_task(title_hint="学习ai开发", priority=Priority.HIGH)
    t = _find(agent, "学习ai开发")

    assert t.priority == Priority.HIGH
    assert t.start == start, "只改优先级时开始时间必须保持"
    assert t.due is None


def test_priority_only_requires_no_time(agent):
    """改优先级不该再要求「要把它改到几点」。"""
    agent.add_task(text="写作", start=datetime.now().replace(hour=9, minute=0, second=0, microsecond=0))
    res = agent.modify_task(title_hint="写作", priority=Priority.LOW)
    assert res["ok"] is True
    assert "改到" not in res["message"]


def test_match_hint_prefers_exact_title(agent):
    """「ai开发」只应命中同名任务，不牵连《学习ai开发》。"""
    base = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    agent.add_task(text="ai开发", start=base)
    agent.add_task(text="学习ai开发", start=base + timedelta(hours=5))

    ids = agent._match_hint("ai开发")
    titles = [agent.store.get(i, "tester").title for i in ids]
    assert titles == ["ai开发"], titles


def test_match_hint_falls_back_to_substring(agent):
    """没有同名任务时，才回退到子串匹配（「自习」→《去自习》）。"""
    agent.add_task(text="去自习", start=datetime.now().replace(hour=11, minute=0, second=0, microsecond=0))
    ids = agent._match_hint("自习")
    assert [agent.store.get(i, "tester").title for i in ids] == ["去自习"]


# --------------------------- 规则兜底路由 ---------------------------

def test_rule_route_priority_not_add(agent):
    """无大模型时，规则兜底也要走 modify 而不是 add。"""
    agent.add_task(text="学习ai开发", start=datetime.now().replace(hour=15, minute=0, second=0, microsecond=0))
    before = len(agent.store.all("tester"))

    res = agent._chat_rule("ai开发设置为高优先级")

    assert len(agent.store.all("tester")) == before, "规则兜底不应新增任务"
    assert res["action"] == "modify", res


def test_rule_route_priority_target_missing(agent):
    """目标不存在时礼貌提示，而不是新建。"""
    before = len(agent.store.all("tester"))
    res = agent._chat_rule("不存在的任务设为高优先级")
    assert len(agent.store.all("tester")) == before
    assert res["ok"] is False


def test_add_with_priority_still_works(agent):
    """「下午3点开会 优先级高」仍然是新增任务，且优先级生效。"""
    before = len(agent.store.all("tester"))
    agent.add_task(text="明天下午3点开会 优先级高")
    assert len(agent.store.all("tester")) == before + 1
    t = agent.store.all("tester")[-1]
    assert t.priority == Priority.HIGH
    assert t.start is not None and t.start.hour == 15
