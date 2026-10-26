"""SQLite 持久化存储层。

并发安全：所有读写都包在 self._lock 中，保证 FastAPI 的并发请求线程与
后台提醒线程共享同一个连接时不会互相踩踏（SQLite 同连接跨线程需串行）。

多用户隔离：tasks 表新增 user_id 列，所有读写操作按 user_id 过滤，
确保不同用户只能看到 / 操作自己的数据。无 user_id（兼容旧库）的历史数据
会在迁移时归入默认用户 'default'。

为支持「一个后端服务多用户」，这里提供 get_store(path) 单例，避免多个
ScheduleAgent 各自打开连接导致跨连接写冲突。
"""
import sqlite3
import threading
from datetime import datetime

from schedule_agent.config import get_settings
from schedule_agent.domain.models import Task


_STORES = {}          # path -> Store 单例
_STORES_LOCK = threading.Lock()


def get_store(path=None):
    """返回指定路径的 Store 单例（多用户共享同一连接与锁）。

    path 为 None 时用配置里的 db_path（部署时可在代码之外指定，便于挂数据盘）。
    """
    if path is None:
        path = get_settings().db_path
    with _STORES_LOCK:
        s = _STORES.get(path)
        if s is None:
            s = Store(path)
            _STORES[path] = s
        return s


class Store:
    """负责任务的增删改查与排期结果的持久化。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        due TEXT,
        start TEXT,
        duration_min INTEGER DEFAULT 30,
        priority INTEGER DEFAULT 2,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        scheduled_start TEXT,
        scheduled_end TEXT,
        reminded INTEGER DEFAULT 0,
        risk INTEGER DEFAULT 0,
        recurrence TEXT DEFAULT '',
        parent_id INTEGER,
        instance_date TEXT,
        user_id TEXT DEFAULT ''
    );
    """

    def __init__(self, path="schedule.db"):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute(self.SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """兼容旧库：补齐新增列；并把无 user_id 的历史数据归入默认用户。"""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "recurrence" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT DEFAULT ''")
        if "parent_id" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN parent_id INTEGER")
        if "instance_date" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN instance_date TEXT")
        if "user_id" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT DEFAULT ''")
        # 兼容旧库：原先单用户的数据统一视作 'default' 用户
        self.conn.execute(
            "UPDATE tasks SET user_id='default' WHERE user_id IS NULL OR user_id=''")

    # ---------- 写 ----------
    def add(self, task: Task, user_id: str = "") -> Task:
        task.user_id = user_id
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO tasks
                   (title, description, due, start, duration_min, priority, status,
                    created_at, scheduled_start, scheduled_end, reminded, risk,
                    recurrence, parent_id, instance_date, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                task.to_row() + (user_id,),
            )
            self.conn.commit()
        task.id = cur.lastrowid
        return task

    def update(self, task: Task, user_id: str = ""):
        with self._lock:
            self.conn.execute(
                """UPDATE tasks SET title=?, description=?, due=?, start=?,
                   duration_min=?, priority=?, status=?, created_at=?,
                   scheduled_start=?, scheduled_end=?, reminded=?, risk=?,
                   recurrence=?, parent_id=?, instance_date=?, user_id=?
                   WHERE id=? AND user_id=?""",
                task.to_row() + (user_id, task.id, user_id),
            )
            self.conn.commit()

    def save_schedule(self, tasks):
        """批量写回规划器算出的排期结果。"""
        with self._lock:
            for t in tasks:
                self.conn.execute(
                    """UPDATE tasks SET scheduled_start=?, scheduled_end=?, risk=?
                       WHERE id=?""",
                    (t.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if t.scheduled_start else None,
                     t.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if t.scheduled_end else None,
                     int(t.risk), t.id),
                )
            self.conn.commit()

    def mark_reminded(self, task_id, user_id: str = ""):
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET reminded=1 WHERE id=? AND user_id=?",
                (task_id, user_id),
            )
            self.conn.commit()

    def delete(self, task_id, user_id: str = ""):
        with self._lock:
            self.conn.execute(
                "DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
            self.conn.commit()

    def delete_instances(self, parent_id, user_id: str = ""):
        """删除某模板生成的所有（未完成）实例。"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM tasks WHERE parent_id=? AND user_id=? AND status=?",
                (parent_id, user_id, "pending"),
            )
            self.conn.commit()

    # ---------- 读 ----------
    def all(self, user_id=None):
        with self._lock:
            if user_id:
                rows = self.conn.execute(
                    "SELECT * FROM tasks WHERE user_id=? ORDER BY id",
                    (user_id,)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [Task.from_row(row) for row in rows]

    def get(self, task_id, user_id=None):
        with self._lock:
            if user_id:
                row = self.conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND user_id=?",
                    (task_id, user_id)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def pending(self, user_id=None):
        return [t for t in self.all(user_id) if t.status == "pending"]

    def templates(self, user_id=None):
        return [t for t in self.all(user_id) if t.is_template()]

    def distinct_user_ids(self):
        """返回库中所有出现过的 user_id（供提醒引擎逐用户扫描）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT user_id FROM tasks "
                "WHERE user_id IS NOT NULL AND user_id <> ''").fetchall()
        return [r[0] for r in rows]
