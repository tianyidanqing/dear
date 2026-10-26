"""数据模型：任务（待办事项）与优先级定义。

展示用的文字（优先级名、状态名）一律走 `i18n.t()`，因此同一份数据在不同
语言环境下会显示成不同文字；**存储里只放数字/枚举**，不做语言相关的落库。
"""
from datetime import datetime
from enum import IntEnum

from schedule_agent.i18n import t

# 任务状态：pending=待办实例, done=已完成, cancelled=已取消,
#           template=周期性任务模板（不参与排期，仅用于生成每日实例）
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_TEMPLATE = "template"

# IntEnum 不允许在类里放 dict 属性（会被当成枚举成员），所以词典放模块级
_PRIORITY_KEYS = {1: "priority.high", 2: "priority.medium", 3: "priority.low"}


class Priority(IntEnum):
    """优先级：数值越小越重要。"""
    HIGH = 1
    MEDIUM = 2
    LOW = 3

    @classmethod
    def from_text(cls, text):
        t_ = (text or "").strip().lower()
        if t_ in ("高", "high", "h", "1"):
            return cls.HIGH
        if t_ in ("低", "low", "l", "3"):
            return cls.LOW
        return cls.MEDIUM

    def label(self):
        """当前语言的优先级名（中文「高」/ 英文「High」）。"""
        return t(_PRIORITY_KEYS[self.value])

    def cn(self):
        """中文优先级名（保留旧接口；现在等同 `label()`，跟随当前语言）。"""
        return self.label()

    def en(self):
        """英文优先级名，与当前界面语言无关（日志 / 对外接口用）。"""
        return {1: "HIGH", 2: "MEDIUM", 3: "LOW"}[self.value]


class Task:
    """单个待办/日程任务，或周期性任务模板。

    字段说明：
    - due:      截止时间（deadline），柔性任务在此时间前应尽量完成。
    - start:    固定开始时间（如会议），有值时该时段被锁定。
    - scheduled_start / scheduled_end: 由规划器计算出的实际排期。
    - risk:     标记该任务可能无法在截止前完成。
    - recurrence: 周期规则（"" 表示一次性）。模板任务用，例如
                  "daily" / "weekly:0,2" / "every:3" / "monthly:5"。
    - parent_id: 实例任务指向其模板 id。
    - instance_date: 实例任务归属的日期（ISO），用于去重。
    """

    def __init__(self, id=None, title="", description="", due=None, start=None,
                 duration_min=30, priority=Priority.MEDIUM, status=STATUS_PENDING,
                 created_at=None, scheduled_start=None, scheduled_end=None,
                 reminded=0, risk=0, recurrence="", parent_id=None, instance_date=None,
                 user_id=""):
        self.id = id
        self.title = title
        self.description = description
        self.due = due
        self.start = start
        self.duration_min = duration_min
        self.priority = priority if isinstance(priority, Priority) else Priority(priority)
        self.status = status
        self.created_at = created_at or datetime.now()
        self.scheduled_start = scheduled_start
        self.scheduled_end = scheduled_end
        self.reminded = reminded
        self.risk = risk
        self.recurrence = recurrence or ""
        self.parent_id = parent_id
        self.instance_date = instance_date
        self.user_id = user_id or ""

    # ---------- 序列化 ----------
    # 注意：to_row() 不含 user_id —— user_id 由 Store.add/update 单独追加，
    # 这样「归属用户」只能由调用方传入，避免任务对象自身携带时被误用。
    def to_row(self):
        def fmt(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
        return (
            self.title, self.description, fmt(self.due), fmt(self.start),
            self.duration_min, int(self.priority), self.status,
            fmt(self.created_at), fmt(self.scheduled_start),
            fmt(self.scheduled_end), int(self.reminded), int(self.risk),
            self.recurrence, self.parent_id, self.instance_date,
        )

    @classmethod
    def from_row(cls, r):
        def parse(s):
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None
        # user_id 是最后一列（第 17 列，下标 16）；旧库迁移后已补齐，做个长度保护。
        uid = r[16] if len(r) > 16 else ""
        return cls(
            id=r[0], title=r[1], description=r[2],
            due=parse(r[3]), start=parse(r[4]),
            duration_min=r[5], priority=Priority(r[6]), status=r[7],
            created_at=parse(r[8]), scheduled_start=parse(r[9]),
            scheduled_end=parse(r[10]), reminded=r[11], risk=r[12],
            recurrence=r[13] or "", parent_id=r[14], instance_date=r[15],
            user_id=uid or "",
        )

    def is_template(self):
        return self.status == STATUS_TEMPLATE

    def status_label(self):
        """当前语言的状态名。"""
        return t({
            STATUS_PENDING: "status.pending",
            STATUS_DONE: "status.done",
            STATUS_CANCELLED: "status.cancelled",
            STATUS_TEMPLATE: "status.template",
        }.get(self.status, "status.pending"))

    def status_cn(self):
        """保留旧接口名（现在跟随当前语言，不再固定中文）。"""
        return self.status_label()

    def __repr__(self):
        due = self.due.strftime("%m-%d %H:%M") if self.due else t("common.none")
        extra = f" recur:{self.recurrence}" if self.recurrence else ""
        return (f"Task#{self.id} [{self.status_label()}] {self.title} | "
                f"due:{due} | {self.duration_min}min | prio:{self.priority.label()}{extra}")
