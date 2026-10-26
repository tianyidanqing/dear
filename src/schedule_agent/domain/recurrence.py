"""周期性任务：把模板按规则展开成每天的「实例」任务。

规则格式（recurrence 字段）：
  "daily"          每天
  "weekly:0,2,4"   每周一/三/五（0=周一 … 6=周日）
  "every:3"        每 3 天（从模板创建日起算）
  "monthly:5"      每月 5 号

模板本身 status='template'，不参与排期；ensure_instances() 会为指定日期
创建对应的 pending 实例（含 parent_id / instance_date 便于去重与级联删除）。
"""
from datetime import datetime

from schedule_agent.domain.models import STATUS_PENDING, STATUS_TEMPLATE, Task


def rule_matches(rule, day, created):
    """判断规则 rule 在日期 day 是否生效。created 为模板创建日期。"""
    if not rule:
        return False
    rule = rule.strip().lower()
    if rule == "daily":
        return True
    if rule.startswith("weekly:"):
        days = [int(x) for x in rule.split(":", 1)[1].split(",") if x.strip() != ""]
        return day.weekday() in days
    if rule.startswith("every:"):
        try:
            n = int(rule.split(":", 1)[1])
        except ValueError:
            return False
        if n <= 0:
            n = 1
        delta = (day - created).days
        return delta >= 0 and delta % n == 0
    if rule.startswith("monthly:"):
        try:
            d = int(rule.split(":", 1)[1])
        except ValueError:
            return False
        return day.day == d
    return False


def _shift(dt, day):
    """把模板里的时间（仅含时分）平移到指定日期。"""
    if dt is None:
        return None
    return datetime(day.year, day.month, day.day, dt.hour, dt.minute)


def ensure_instances(store, day, user_id=None):
    """为 day 创建某用户所有匹配模板的当日实例（若不存在）。返回新建数量。

    user_id 为 None 时按「全部用户」处理（兼容单用户 / 命令行场景）。
    """
    created_count = 0
    templates = store.templates(user_id)
    existing = store.all(user_id)  # 只比对同一用户下的实例，避免跨用户误判
    for tpl in templates:
        if not rule_matches(tpl.recurrence, day, tpl.created_at.date()):
            continue
        # 是否已存在该日实例
        if any(
            t.parent_id == tpl.id and t.instance_date == day.isoformat()
            for t in existing
        ):
            continue
        inst = Task(
            title=tpl.title,
            description=tpl.description,
            due=_shift(tpl.due, day),
            start=_shift(tpl.start, day),
            duration_min=tpl.duration_min,
            priority=tpl.priority,
            status=STATUS_PENDING,
            recurrence="",
            parent_id=tpl.id,
            instance_date=day.isoformat(),
        )
        store.add(inst, user_id or "")
        created_count += 1
    return created_count
