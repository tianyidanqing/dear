"""回归：无大模型时的规则兜底，不能把「闲聊/纠正/提问」凭空建成任务。

背景 bug：用户说「有两个日程了，只要早上9点的」（本意是指出重复），
规则兜底最后一句是 `return self.add_task(text)`，于是建出一条
标题为「有两个日程了，只要 的」的垃圾待办，还占用了 09:00 时段。
"""
from schedule_agent.domain import nlp
from schedule_agent.service.agent import ScheduleAgent
from schedule_agent.storage.db import Store
from schedule_agent.domain import planner

from conftest import bearer


# --------------------------- 纯规则判据 ---------------------------
def test_looks_like_non_task_detects_chat():
    for text in ["有两个日程了，只要早上9点的",
                 "为什么排到了凌晨",
                 "这是怎么回事",
                 "是不是重复了",
                 "改成下午三点"]:
        assert nlp.looks_like_non_task(text) is True, text


def test_looks_like_task_still_passes():
    for text in ["明天下午3点开会 1小时 高",
                 "今天早上10点吃早茶",
                 "每天早上9点晨会 20分钟",
                 "前门取快递",
                 "写季度总结报告初稿交给张总"]:
        assert nlp.looks_like_non_task(text) is False, text


# --------------------------- 服务层（规则兜底） ---------------------------
def _agent(tmp_path, user="alice"):
    store = Store(str(tmp_path / "t.db"))
    return ScheduleAgent(user_id=user, store=store, cfg={"use_rule_fallback": True}), store


def test_chat_does_not_create_task_for_chat_text(tmp_path, monkeypatch):
    """无 LLM 时，这句话不应新增任务。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    res = agent.chat("有两个日程了，只要早上9点的")
    assert res["ok"] is False
    assert res["action"] == "chat"
    assert store.all("alice") == [], "不该凭空建任务"
    assert "没把这句话当成新任务" in res["message"]


def test_chat_still_adds_normal_task(tmp_path, monkeypatch):
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    res = agent.chat("今天早上10点吃早茶")
    assert res["ok"] is True
    tasks = store.all("alice")
    assert len(tasks) == 1
    assert tasks[0].title == "吃早茶"
    assert tasks[0].start is not None and tasks[0].start.hour == 10


def test_rule_remove_by_title(tmp_path, monkeypatch):
    """规则兜底也支持按名称删除（原来只认数字编号）。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    agent.chat("今天早上9点吃早茶")
    agent.chat("今天早上10点吃早茶")
    assert len([t for t in store.all("alice") if t.title == "吃早茶"]) == 2

    res = agent.chat("删掉吃早茶")
    assert res["ok"] is True
    assert store.all("alice") == []


def test_rule_query_variants_not_added_as_task(tmp_path, monkeypatch):
    """「查看日程」这类查询说法要走到查询，而不是被建成任务。"""
    monkeypatch.setattr("schedule_agent.service.agent.llm_nlp.llm_enabled",
                        lambda: False)
    agent, store = _agent(tmp_path)
    for text in ["查看日程", "看看日程", "今日日程"]:
        res = agent.chat(text)
        assert res["action"] in ("today", "chat"), (text, res["action"])
    assert store.all("alice") == []


# --------------------------- 接口层 ---------------------------
def test_api_chat_guard(client, alice):
    """通过接口复现：闲聊不会产生任务。"""
    r = client.post("/api/chat", headers=bearer(alice),
                    json={"message": "有两个日程了，只要早上9点的"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert client.get("/api/tasks", headers=bearer(alice)).json()["tasks"] == []
