"""回归：明确时刻应按「固定开始时间」排期，而不是被当成截止时间塞到最早空隙。

背景 bug：用户说「今天早上10点吃早茶」，排期却落在 00:17（当时时刻）。
原因是规则解析器只把「会议/电话…」关键词当 start，其余时刻一律当 due，
排期器对只有 due 的任务按「从 now 起最早空隙」放置。
"""
from datetime import datetime, timedelta

from schedule_agent.domain import nlp
from schedule_agent.domain.models import Task
from schedule_agent.domain.planner import plan_day, summarize


def _task(r):
    return Task(title=r["title"], due=r["due"], start=r["start"],
                duration_min=r["duration_min"], priority=r["priority"])


def test_explicit_time_is_start_not_due():
    """说了明确时刻 -> 固定开始时间。"""
    for text in ["今天早上10点吃早茶", "明天下午3点开会 1小时 高",
                 "周五 上午10点 健身 45分钟 低", "晚上7点看电影 2小时",
                 "10月30日 14:00 看牙"]:
        r = nlp.parse_natural(text)
        assert r["start"] is not None, f"{text} 应得到 start"
        assert r["due"] is None, f"{text} 不应得到 due"


def test_explicit_time_is_due_only_when_deadline_wording():
    """出现截止语义 -> 仍是截止时间。"""
    for text in ["今天18点前提交周报", "截止今天17点交方案", "最迟今晚9点交付代码"]:
        r = nlp.parse_natural(text)
        assert r["due"] is not None, f"{text} 应得到 due"
        assert r["start"] is None, f"{text} 不应得到 start"


def test_ten_am_task_scheduled_at_ten_am():
    """核心回归：10 点的安排必须排在 10:00，不能排到当前时刻。"""
    # 模拟现在是当天 00:17（用户遇到问题的时刻）
    now = datetime.now().replace(hour=0, minute=17, second=0, microsecond=0)
    r = nlp.parse_natural("今天早上10点吃早茶")
    t = _task(r)
    planned = plan_day([t], now=now)
    assert len(planned) == 1
    s = planned[0]
    assert (s.scheduled_start.hour, s.scheduled_start.minute) == (10, 0), \
        f"期望排到 10:00，实际 {s.scheduled_start:%H:%M}"
    assert (s.scheduled_end.hour, s.scheduled_end.minute) == (10, 30)
    assert not s.risk


def test_past_fixed_task_flagged_as_overdue():
    """固定开始时间已过 -> 标记误解风险（risk==2），而不是悄悄排过去。"""
    now = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
    past = (now - timedelta(hours=3)).replace(second=0, microsecond=0)
    t = Task(title="早会", start=past, due=None, duration_min=30)
    planned = plan_day([t], now=now)
    assert planned[0].risk == 2
    assert "已过" in summarize(planned, now=now)


def test_flexible_task_still_fills_earliest_gap():
    """只有截止时间的柔性任务仍按最早可用空隙排（ASAP 语义不变）。"""
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    due = now.replace(hour=18, minute=0)
    t = Task(title="写小结", due=due, start=None, duration_min=30)
    planned = plan_day([t], now=now)
    assert (planned[0].scheduled_start.hour, planned[0].scheduled_start.minute) == (9, 0)


def test_title_keeps_leading_char_without_time():
    """无时刻时不做过度清洗，标题里的『前』不被误删。"""
    assert nlp.parse_natural("前门取快递")["title"] == "前门取快递"


def test_deadline_title_cleaned():
    assert nlp.parse_natural("今天18点前提交周报")["title"] == "提交周报"
