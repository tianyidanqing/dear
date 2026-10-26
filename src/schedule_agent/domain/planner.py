"""日程规划器：把当天的待办事项排进时间轴。

核心思路（实时调度中的 EDF —— 最早截止时间优先）：
1. 先放置「固定开始时间」的任务（会议、约水等硬约束）。
2. 再把「只有截止时间」的柔性任务，按截止时间从早到晚，依次塞进
   最早的可用空隙；若空隙不足以在截止前完成，则顺延并标记 risk。
3. 已完成 / 已取消的任务不参与排期，即自动「剔除」。
"""
from datetime import datetime, timedelta

from schedule_agent.i18n import t


def active_today(tasks, today):
    """筛选出「属于今天」且未完成的任务。"""
    out = []
    for task in tasks:
        if task.status != "pending":
            continue
        if task.start is not None and task.start.date() == today:
            out.append(task)
            continue
        if task.due is not None and task.due.date() == today:
            out.append(task)
            continue
        if task.due is None and task.start is None:
            out.append(task)  # 无时间约束，归到今天处理
    return out


def _free_gaps(busy, start, end):
    """在 [start, end] 区间内，扣除已占用时段 busy(sorted)，返回空闲空隙列表。"""
    gaps = []
    cursor = start
    for s, e in busy:
        if s > cursor:
            gaps.append((cursor, min(s, end)))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return [g for g in gaps if g[1] > g[0]]


def plan_day(tasks, now=None, day_end=None):
    """对当天任务进行排期，返回排好序的任务列表（原地修改 scheduled_* 与 risk）。"""
    now = now or datetime.now()
    today = now.date()
    if day_end is None:
        day_end = datetime(today.year, today.month, today.day, 23, 59, 59)

    pending = active_today(tasks, today)

    # 1) 固定开始任务先落位（用户说了明确时刻的事项，必须落在那个时刻）
    fixed = sorted([task for task in pending if task.start is not None],
                   key=lambda task: task.start)
    busy = []
    for task in fixed:
        end = task.start + timedelta(minutes=task.duration_min)
        task.scheduled_start = task.start
        task.scheduled_end = end
        # risk: 0=正常 1=超出截止时间 2=开始时刻已过（可能已经误点）
        task.risk = 2 if end <= now else 0
        busy.append((task.start, end))

    # 2) 柔性任务按 截止时间 -> 优先级 -> 时长 排序
    flexible = [task for task in pending if task.start is None]
    flexible.sort(key=lambda task: (
        task.due if task.due else datetime.max,
        task.priority,
        -task.duration_min,
    ))

    # 计算从 now 开始的可用空隙
    gaps = _free_gaps(sorted(busy), now, day_end)

    for task in flexible:
        dur = timedelta(minutes=task.duration_min)
        deadline = task.due if task.due is not None else day_end
        placed = False
        for i, (gs, ge) in enumerate(gaps):
            if gs + dur <= ge:  # 这段空隙能放下
                if gs + dur <= deadline:
                    start = gs
                    task.risk = False
                else:
                    start = gs  # 放得下但会超过截止 -> 风险
                    task.risk = True
                task.scheduled_start = start
                task.scheduled_end = start + dur
                # 收缩该空隙
                gaps[i] = (start + dur, ge)
                placed = True
                break
        if not placed:
            # 当天已无处安放，放到最后并标记风险（建议改期）
            last_end = gaps[-1][1] if gaps else now
            task.scheduled_start = max(last_end, now)
            task.scheduled_end = task.scheduled_start + dur
            task.risk = True

    # 返回所有被排期的任务，按开始时间排序
    scheduled = [task for task in pending if task.scheduled_start is not None]
    scheduled.sort(key=lambda task: task.scheduled_start)
    return scheduled


def summarize(scheduled, now=None):
    """生成人类可读的当天排期摘要，并给出 AI 建议。

    文案全部来自 i18n 词典：同一个 `scheduled` 在中文环境下是中文摘要，
    在英文环境下是英文摘要，**数据本身不落地任何语言信息**。
    """
    now = now or datetime.now()
    if not scheduled:
        return t("planner.empty")

    lines = [t("planner.header")]
    overloaded = False
    for task in scheduled:
        if task.risk == 2:
            flag = t("planner.flag_started")
        elif task.risk:
            flag = t("planner.flag_risk")
        else:
            flag = ""
        ss = task.scheduled_start.strftime("%H:%M")
        se = task.scheduled_end.strftime("%H:%M")
        lines.append(t("planner.line", start=ss, end=se, title=task.title,
                       priority=task.priority.label(), flag=flag))

    # AI 建议：检测误点 / 过载 / 风险任务
    overdue = [task for task in scheduled if task.risk == 2]
    if overdue:
        lines.append("")
        lines.append(t("planner.overdue_header"))
        for task in overdue:
            ss = task.scheduled_start.strftime("%H:%M")
            lines.append(t("planner.overdue_line", title=task.title, start=ss))

    risks = [task for task in scheduled if task.risk and task.risk != 2]
    if risks:
        overloaded = True
        lines.append("")
        lines.append(t("planner.risk_header"))
        for task in risks:
            due = task.due.strftime("%H:%M") if task.due else t("common.none")
            lines.append(t("planner.risk_line", title=task.title, due=due,
                           minutes=task.duration_min))

    # 检测空闲碎片
    gaps_min = 0
    prev = None
    for task in scheduled:
        if prev is not None:
            gap = (task.scheduled_start - prev).total_seconds() / 60
            if 0 < gap >= 30:
                gaps_min += gap
        prev = task.scheduled_end
    if gaps_min >= 60 and not overloaded:
        lines.append("")
        lines.append(t("planner.gap_hint", minutes=int(gaps_min)))

    return "\n".join(lines)
