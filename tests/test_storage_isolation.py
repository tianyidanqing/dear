# -*- coding: utf-8 -*-
"""存储层：多用户数据隔离（不经过 HTTP，直测 Store）。"""
from schedule_agent.domain.models import Task
from schedule_agent.storage.db import get_store


def _mk(store, title, user_id, status="pending"):
    return store.add(Task(title=title, status=status), user_id)


def test_each_user_only_sees_own_tasks(settings):
    store = get_store(settings.db_path)
    _mk(store, "alice-task", "alice")
    _mk(store, "bob-task", "bob")

    assert [t.title for t in store.all("alice")] == ["alice-task"]
    assert [t.title for t in store.all("bob")] == ["bob-task"]


def test_cross_user_read_and_write_are_blocked(settings):
    """跨用户读取应为 None；删除/更新不应影响他人数据。"""
    store = get_store(settings.db_path)
    a = _mk(store, "alice-task", "alice")

    assert store.get(a.id, "bob") is None            # 读不到
    store.delete(a.id, "bob")                        # 删不掉
    assert store.get(a.id, "alice") is not None

    a.title = "改过了"
    store.update(a, "alice")                          # 本人可改
    assert store.get(a.id, "alice").title == "改过了"


def test_distinct_user_ids(settings):
    store = get_store(settings.db_path)
    _mk(store, "t1", "alice")
    _mk(store, "t2", "bob")
    assert set(store.distinct_user_ids()) == {"alice", "bob"}


def test_recurring_instances_belong_to_owner(settings):
    """周期模板展开出的实例必须归属同一用户，且不会污染他人列表。"""
    from schedule_agent.domain.recurrence import ensure_instances
    from datetime import datetime

    store = get_store(settings.db_path)
    tpl = store.add(Task(title="alice 每日站会", recurrence="daily",
                         status="template"), "alice")
    n = ensure_instances(store, datetime.now().date(), "alice")
    assert n == 1
    inst = [t for t in store.all("alice") if t.parent_id == tpl.id]
    assert len(inst) == 1
    assert inst[0].user_id == "alice"
    assert store.all("bob") == []


def test_legacy_rows_are_backfilled_to_default(tmp_path):
    """旧库（无 user_id 列）迁移后历史数据归入 default 用户。"""
    import sqlite3
    p = tmp_path / "legacy.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
        due TEXT, start TEXT, duration_min INTEGER DEFAULT 30, priority INTEGER DEFAULT 2,
        status TEXT DEFAULT 'pending', created_at TEXT, scheduled_start TEXT,
        scheduled_end TEXT, reminded INTEGER DEFAULT 0, risk INTEGER DEFAULT 0,
        recurrence TEXT DEFAULT '', parent_id INTEGER, instance_date TEXT)""")
    c.execute("INSERT INTO tasks(title,status) VALUES('历史任务','pending')")
    c.commit()
    c.close()

    store = get_store(str(p))
    assert all(t.user_id == "default" for t in store.all())
    assert store.distinct_user_ids() == ["default"]
