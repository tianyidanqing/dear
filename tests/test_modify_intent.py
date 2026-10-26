"""回归：修改已有安排的时间（modify 意图）。

背景 bug：用户说「自习时间改成11点」，规则兜底里**没有 modify 能力**，
「改成」又被兜底守卫判成纠正类闲聊，于是回一句「我没把这句话当成新任务」，
既没改时间、也没告诉用户该怎么说。
"""
from datetime import datetime

import pytest

from schedule_agent.domain import nlp
from schedule_agent.service.agent import ScheduleAgent
from schedule_agent.storage.db import Store

from conftest import bearer


# --------------------------- 纯规则解析 ---------------------------
@pytest.mark.parametrize("text,hint,hour,minute", [
    ("自习时间改成11点", "自习", 11, 0),
    ("把吃早茶改到下午3点", "吃早茶", 15, 0),
    ("晨会推迟到9点半", "晨会", 9, 30),
    ("把自习的开始时间改成11点", "自习", 11, 0),
    ("自习调整到晚上8点", "自习", 20, 0),
])
def test_parse_modify(text, hint, hour, minute):
    m = nlp.parse_modify(text)
    assert m is not None, text
    assert m["title_hint"] == hint
    assert m["start"].hour == hour and m["start"].minute == minute


def test_parse_modify_keeps_deadline_semantics():
    """「改到18点前」仍是截止时间，不能变成 18 点开始。"""
    m = nlp.parse_modify("把自习改到18点前")
    assert m["start"] is None
    assert m["due"].hour == 18


def test_parse_modify_ignores_normal_task():
    for text in ["明天下午3点开会 1小时 高", "今天早上10点吃早茶",
                 "删除 吃早茶", "完成 自习", "修改合同 1小时"]:
        assert nlp.parse_modify(text) is None, text


# --------------------------- 服务层（规则兜底） ---------------------------
def _agent(tmp_path, user="alice"):
    store = Store(str(tmp_path / "t.db"))
    return ScheduleAgent(user_id=user, store=store, cfg={"use_rule_fallback": True}), store


def test_chat_modify_reschedules_task(tmp_path, monkeypatch):
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天上午10点 去自习 30分钟")
    assert len(store.all("alice")) == 1

    # 用户原话
    res = agent.chat("自习时间改成11点")
    assert res["ok"] is True
    assert res["action"] == "modify"

    tasks = store.all("alice")
    assert len(tasks) == 1, "改时间不该新增任务"
    assert tasks[0].title == "去自习"
    assert tasks[0].start.hour == 11
    assert tasks[0].due is None
    # 排期同步落到 11:00
    assert any(t["scheduled_start"].endswith("11:00:00") for t in res["schedule"])


def test_modify_does_not_flip_deadline_to_start(tmp_path, monkeypatch):
    """原来只有截止时间的任务，改的是那个截止时刻（语义不跑偏）。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天18点前提交周报")
    t = store.all("alice")[0]
    assert t.due is not None and t.start is None

    agent.chat("周报改到20点")
    t = store.all("alice")[0]
    assert t.start is None and t.due.hour == 20


def test_modify_resets_reminded_flag(tmp_path, monkeypatch):
    """时间改过之后要重新提醒，不能因为之前提醒过就静默。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天上午10点 去自习")
    t = store.all("alice")[0]
    t.reminded = 1
    store.update(t, "alice")

    agent.chat("自习改到11点")
    assert store.all("alice")[0].reminded == 0


def test_modify_unknown_task_asks(tmp_path, monkeypatch):
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天上午10点 去自习")

    res = agent.chat("改到11点")           # 没说改哪件
    assert res["ok"] is False and "哪一件事" in res["message"]
    res = agent.chat("健身改成9点")        # 没有这件事
    assert res["ok"] is False and "没找到" in res["message"]
    assert len(store.all("alice")) == 1 and store.all("alice")[0].start.hour == 10


def test_modify_duration_only(tmp_path, monkeypatch):
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天上午10点 去自习 30分钟")
    agent.chat("自习改到11点，时长1小时")
    t = store.all("alice")[0]
    assert t.start.hour == 11 and t.duration_min == 60


def test_modify_recurring_changes_today_instance(tmp_path, monkeypatch):
    """周期任务：按「先匹配当天实例」的规则，只改今天这一次。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("每天上午9点 晨会 20分钟")
    agent.chat("晨会改到10点")
    instances = [t for t in store.all("alice") if not t.is_template()]
    assert instances and instances[0].start.hour == 10


# --------------------------- 接口层 ---------------------------
def test_api_modify(client, alice):
    client.post("/api/chat", headers=bearer(alice),
                json={"message": "今天上午10点 去自习 30分钟"})
    r = client.post("/api/chat", headers=bearer(alice),
                    json={"message": "自习时间改成11点"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "modify"
    tasks = client.get("/api/tasks", headers=bearer(alice)).json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["start"].endswith("11:00:00")
