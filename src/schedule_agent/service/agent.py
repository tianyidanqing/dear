#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日程管理 Agent 的核心服务层（与界面无关）。

把原本在 agent.py 里的 ScheduleAgent 抽出来，所有方法都返回**结构化字典**
（含 ok / action / message / schedule / tasks 等字段），不再直接 print，方便
被 FastAPI、Gradio、CLI 复用，也方便做微服务序列化。

典型返回结构：
  {
    "ok": True/False,
    "action": "add" | "modify" | "done" | "today" | "list" | "templates" | "summary" | "remove" | "chat",
    "message": "面向用户的中文说明",
    "schedule": [任务dict, ...] 或 None,   # 当日排期（有相关动作时才有）
    "tasks":    [任务dict, ...] 或 None,   # 任务列表（list/templates 时）
  }
"""
import asyncio
import re
from datetime import datetime, timedelta

from schedule_agent.config import get_settings
from schedule_agent.domain import planner
from schedule_agent.domain.models import (
    Priority, STATUS_CANCELLED, STATUS_TEMPLATE, Task,
)
from schedule_agent.domain import nlp
from schedule_agent.domain import recurrence as recur
from schedule_agent.i18n import t
from schedule_agent.llm import client as llm_nlp
from schedule_agent.service.notifier import notify

# 规则兜底用：从「删除 吃早茶」「把晨会完成」这类句子里剥掉动词，剩下任务名去匹配
_DONE_VERBS = ("已经完成", "完成", "做完", "搞定", "结束", "标记为完成", "了")
_DEL_VERBS = ("删除", "删掉", "移除", "去掉", "取消任务", "取消", "del", "rm")


def _llm_no_key():
    """「未配置 Key」的说明（按当前语言）。"""
    return t("agent.llm_no_key")

# 意图别名表：把模型可能输出的同义动作名收敛到系统支持的规范动作。
# 为什么需要它：早期用 if 链硬匹配 "done"/"remove"，模型只要输出
# "complete"/"delete" 就会漏到 unknown 分支，再被当成「新增」建成垃圾待办。
# 新增动作时，记得同步 ①本表 ②_dispatch 分支 ③prompts 的枚举 ④规则兜底。
_INTENT_ALIASES = {
    "add": "add", "create": "add", "new": "add", "schedule": "add",
    "remind": "add", "add_task": "add",

    "modify": "modify", "reschedule": "modify", "update": "modify",
    "move": "modify", "postpone": "modify", "delay": "modify",
    "change_time": "modify", "adjust": "modify",

    "priority": "priority", "prioritize": "priority", "set_priority": "priority",
    "reprioritize": "priority", "change_priority": "priority", "update_priority": "priority",

    "done": "done", "complete": "done", "finish": "done", "completed": "done",
    "mark_done": "done", "check": "done",

    "remove": "remove", "delete": "remove", "del": "remove", "cancel": "remove",
    "drop": "remove", "rm": "remove",

    "today": "today", "query_today": "today", "list": "list", "all": "list",
    "templates": "templates", "summary": "summary", "demo": "demo",
}


def _strip_verbs(text, verbs):
    """剥掉动词与常见虚词，得到用于标题匹配的关键词。"""
    out = text or ""
    for v in verbs:
        out = out.replace(v, " ")
    out = re.sub(r"[，,。.、!！?？\s]+", " ", out)
    return out.strip()



def task_to_dict(t: Task) -> dict:
    """把 Task 序列化成 API/JSON 友好的字典。"""
    def fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "status_cn": t.status_cn(),
        "priority": t.priority.cn(),
        "duration_min": t.duration_min,
        "due": fmt(t.due),
        "start": fmt(t.start),
        "scheduled_start": fmt(t.scheduled_start),
        "scheduled_end": fmt(t.scheduled_end),
        # 0=正常 1=超出截止 2=开始时刻已过（保留整数，便于前端区分提示文案）
        "risk": int(t.risk),
        "recurrence": t.recurrence,
        "parent_id": t.parent_id,
        "instance_date": t.instance_date,
    }


class ScheduleAgent:
    """无界面的核心服务：增删待办、排期、提醒、周期任务、聊天意图路由。

    多用户隔离：每个实例绑定一个 user_id，所有读写都按该用户范围，
    因此不同用户即使任务 id 相同也互不干扰（跨用户访问会拿到 None）。

    依赖全部由外部注入（store / notifier_fn / engine），不在本类里读配置、
    也不自己建连接，这样同一份代码既能被 API 服务复用，也能被命令行复用，
    且测试时可以直接注入内存替代品。
    """

    def __init__(self, user_id: str, store, notifier_fn=None, engine=None,
                 cfg=None):
        self.user_id = user_id
        self.store = store
        self.cfg = cfg or {}
        self.notifier_fn = notifier_fn or notify   # 签名：(user_id, title, message)
        self.engine = engine                       # 由 AgentRegistry 统一注入（单进程共用一个）
        self.history = []  # 多轮对话上下文（每个用户实例各自持有，天然隔离）
        self._hist_lock = asyncio.Lock()  # 并发流式请求下保护 history 写

        # 规则兜底开关（默认关闭）。关闭时的原则：**意图只由大模型判断**，
        # 模型不可用就如实报错，绝不用关键词规则去猜——规则是闭集，猜错不报错，
        # 只会静默建出垃圾待办（「ai开发设置为高优先级」被建成新任务就是这么来的）。
        s = get_settings()
        raw = self.cfg.get("use_rule_fallback")
        self.use_rule_fallback = bool(s.use_rule_fallback if raw is None else raw)

    # ---------------- 生命周期 ----------------
    def start_engine(self):
        """启动提醒引擎（无引擎时静默跳过，便于纯计算场景使用）。"""
        if self.engine is not None:
            self.engine.start()

    def stop_engine(self):
        if self.engine is not None:
            self.engine.stop()

    # ---------------- 核心动作 ----------------
    def add_task(self, text, due=None, start=None, duration_min=30,
                 priority=Priority.MEDIUM, title=None, recurrence="",
                 skip_nlu=False):
        """添加一个任务（或周期模板）。text 支持自然语言解析。

        解析优先级：大模型 NLU（understand）-> 规则解析（nlp.parse_natural），
        保证有无 Key 都能跑；CLI 的 `add` 命令也走同一条链路（由大模型理解）。
        skip_nlu=True 表示调用方已自行解析好字段（如聊天路由已拿到大模型决策），
        直接采用传入字段，避免重复调用大模型。
        """
        if (text and (due is None and start is None)) and not skip_nlu:
            parsed = None
            if llm_nlp.llm_enabled():
                dec = llm_nlp.understand(text)
                # 仅当大模型认为这是「新增/未知」时才采用其抽取的字段，
                # 避免把「今日日程」之类的查询误当成任务添加。
                if dec and dec.get("intent") in ("add", "unknown"):
                    parsed = {
                        "title": (dec.get("title") or text).strip(),
                        "due": dec.get("due"),
                        "start": dec.get("start"),
                        "duration_min": int(dec.get("duration_min") or 30),
                        "priority": dec.get("priority") or Priority.MEDIUM,
                        "recurrence": (dec.get("recurrence") or "").strip(),
                    }
                elif dec:
                    # 模型判定这不是新增（查询/修改/删除）——别硬建成待办
                    return {"ok": False, "action": "chat",
                            "message": t("agent.not_add_intent", intent=dec.get("intent")),
                            "schedule": None, "tasks": None}
            if parsed is None:
                if not self.use_rule_fallback:
                    # 规则解析是闭集，会把不认识的说法硬塞成任务——默认不许用
                    return self._fallback_or_error(
                        text, llm_nlp.last_error() or t("agent.no_add_intent"))
                parsed = nlp.parse_natural(text)
            due = parsed.get("due")
            start = parsed.get("start")
            duration_min = parsed.get("duration_min", duration_min)
            priority = parsed.get("priority", priority)
            title = title or parsed.get("title") or text
            recurrence = recurrence or parsed.get("recurrence", "")
        else:
            title = title or text or t("nlp.default_title")

        today = datetime.now().date()

        # ---- 周期性任务：建模板，并按需生成今日实例 ----
        if recurrence:
            tpl = Task(title=title, due=due, start=start, duration_min=duration_min,
                       priority=priority, status=STATUS_TEMPLATE,
                       recurrence=recurrence)
            self.store.add(tpl, self.user_id)
            n = recur.ensure_instances(self.store, today, self.user_id)
            self.replan_today(quiet=True)
            if n > 0:
                msg = t("agent.added_recurring", title=title, rule=recurrence)
            else:
                msg = t("agent.added_recurring_future", title=title, rule=recurrence)
            return {"ok": True, "action": "add",
                    "message": msg,
                    "schedule": self._today_schedule(), "tasks": None}

        # ---- 一次性任务 ----
        task = Task(title=title, due=due, start=start, duration_min=duration_min,
                    priority=priority)
        self.store.add(task, self.user_id)
        belongs_today = (start and start.date() == today) or \
                        (due and due.date() == today) or \
                        (due is None and start is None)
        if belongs_today:
            sched = self._today_schedule()
            return {"ok": True, "action": "add",
                    "message": t("agent.added_today", title=task.title),
                    "schedule": sched, "tasks": None}
        return {"ok": True, "action": "add",
                "message": t("agent.added_future", title=task.title),
                "schedule": None, "tasks": None}

    def replan_today(self, quiet=False):
        """为当天展开周期实例，并对未完成事项重新排期并持久化，返回 (scheduled, summary)。"""
        today = datetime.now().date()
        recur.ensure_instances(self.store, today, self.user_id)
        tasks = self.store.all(self.user_id)
        scheduled = planner.plan_day(tasks)
        self.store.save_schedule(scheduled)
        summary = planner.summarize(scheduled)
        if not quiet:
            print(summary)
        return scheduled, summary

    def _today_schedule(self):
        scheduled, _ = self.replan_today(quiet=True)
        return [task_to_dict(x) for x in scheduled]

    def mark_done(self, task_id):
        task = self.store.get(task_id, self.user_id)
        if not task:
            return {"ok": False, "action": "done",
                    "message": t("agent.task_not_found", id=task_id),
                    "schedule": None, "tasks": None}
        if task.is_template():
            task.status = STATUS_CANCELLED
            self.store.update(task, self.user_id)
            self.store.delete_instances(task.id, self.user_id)
            return {"ok": True, "action": "done",
                    "message": t("agent.stopped_recurring", title=task.title),
                    "schedule": self._today_schedule(), "tasks": None}
        task.status = "done"
        self.store.update(task, self.user_id)
        sched = self._today_schedule()
        return {"ok": True, "action": "done",
                "message": t("agent.marked_done", title=task.title),
                "schedule": sched, "tasks": None}

    def remove(self, task_id):
        task = self.store.get(task_id, self.user_id)
        if not task:
            return {"ok": False, "action": "remove",
                    "message": t("agent.task_not_found", id=task_id),
                    "schedule": None, "tasks": None}
        if task.is_template():
            self.store.delete(task.id, self.user_id)
            self.store.delete_instances(task.id, self.user_id)
            return {"ok": True, "action": "remove",
                    "message": t("agent.deleted_recurring", title=task.title),
                    "schedule": self._today_schedule(), "tasks": None}
        self.store.delete(task_id, self.user_id)
        sched = self._today_schedule()
        return {"ok": True, "action": "remove",
                "message": t("agent.deleted", title=task.title),
                "schedule": sched, "tasks": None}

    def modify_task(self, title_hint=None, start=None, due=None,
                    duration_min=None, task_ids=None, priority=None):
        """修改已有安排的时间或优先级（改到几点 / 时长改成多久 / 设为高优先级）。

        规则兜底与大模型共用：按 `task_ids` 或 `title_hint` 找到任务后改字段并重排。
        语义继承：原任务只有截止时间（due-only）时，改的还是那个截止时刻，不会
        因为一次修改就把「18点前交」变成「18点开始做」。
        priority 可以单独传（不改时间），也可以和时间一起传。
        """
        if start is None and due is None and not duration_min and priority is None:
            return {"ok": False, "action": "chat",
                    "message": t("agent.ask_when_modify"),
                    "schedule": None, "tasks": None}

        ids = list(task_ids or []) or self._match_hint(title_hint)
        if not ids:
            if not title_hint:
                return {"ok": False, "action": "chat",
                        "message": t("agent.ask_which_task"),
                        "schedule": None, "tasks": None}
            return {"ok": False, "action": "chat",
                    "message": t("agent.modify_not_found", hint=title_hint),
                    "schedule": None, "tasks": None}

        changed, when = [], start or due
        for tid in ids:
            task = self.store.get(tid, self.user_id)
            if not task:
                continue
            if task.is_template():
                # 改周期模板 = 改后续所有实例；今日实例重建
                if start is not None:
                    task.start, task.due = start, None
                elif due is not None:
                    task.due, task.start = due, None
                if duration_min:
                    task.duration_min = int(duration_min)
                if priority is not None:
                    task.priority = priority
                self.store.update(task, self.user_id)
                self.store.delete_instances(task.id, self.user_id)
                recur.ensure_instances(self.store, datetime.now().date(), self.user_id)
                changed.append(task.title)
                continue

            if start is not None:
                if task.start is not None or task.due is None:
                    task.start = start
                else:
                    task.due = start      # 原来是「某点前完成」，改的是那个时刻
            elif due is not None:
                task.due = due            # 明确说「X点前」-> 改回截止语义
                task.start = None
            if duration_min:
                task.duration_min = int(duration_min)
            if priority is not None:
                task.priority = priority
            if start is not None or due is not None:
                task.reminded = 0         # 时间变了，重新计时提醒
            self.store.update(task, self.user_id)
            changed.append(task.title)

        if not changed:
            return {"ok": False, "action": "chat",
                    "message": t("agent.modify_nothing_found"),
                    "schedule": None, "tasks": None}

        names = t("common.list_sep").join(
            t("common.title_quote", title=n) for n in changed)

        # 只改优先级：不动时间，但优先级影响 EDF 排期顺序，仍要重排
        if start is None and due is None:
            tail = (t("agent.priority_tail_duration", minutes=int(duration_min))
                    if duration_min else "")
            return {"ok": True, "action": "modify",
                    "message": t("agent.priority_changed", names=names,
                                 priority=(priority or Priority.MEDIUM).label(),
                                 tail=tail),
                    "schedule": self._today_schedule(), "tasks": None}

        stamp = when.strftime("%m-%d %H:%M")
        is_today = when.date() == datetime.now().date()
        prio_note = (t("agent.priority_note", priority=priority.label())
                     if priority is not None else "")
        if is_today:
            return {"ok": True, "action": "modify",
                    "message": t("agent.moved_today", names=names, when=stamp,
                                 priority_note=prio_note),
                    "schedule": self._today_schedule(), "tasks": None}
        return {"ok": True, "action": "modify",
                "message": t("agent.moved_future", names=names, when=stamp,
                             priority_note=prio_note),
                "schedule": None, "tasks": None}

    def list_tasks(self):
        tasks = self.store.all(self.user_id)
        return {"ok": True, "action": "list",
                "message": t("agent.count_tasks", n=len(tasks)),
                "schedule": None, "tasks": [task_to_dict(x) for x in tasks]}

    def list_templates(self):
        tpls = self.store.templates(self.user_id)
        return {"ok": True, "action": "templates",
                "message": t("agent.count_templates", n=len(tpls)),
                "schedule": None, "tasks": [task_to_dict(x) for x in tpls]}

    def show_today(self):
        scheduled, summary = self.replan_today(quiet=True)
        return {"ok": True, "action": "today",
                "message": summary,
                "schedule": [task_to_dict(x) for x in scheduled], "tasks": None}

    def summary(self):
        """当日速览（同时把结果作为提醒推送给通知器）。"""
        scheduled, summary = self.replan_today(quiet=True)
        self.notifier_fn(self.user_id, t("reminder.summary_title"), summary)
        return {"ok": True, "action": "summary",
                "message": summary,
                "schedule": [task_to_dict(x) for x in scheduled], "tasks": None}

    def seed_demo(self):
        """写入示例数据。

        示例任务的标题也走 i18n（英文环境写英文示例），但**时间/时长/优先级
        用结构化参数传入**，不依赖自然语言解析——否则英文标题会被中文规则
        解析器当成纯标题，时间全丢。
        """
        today = datetime.now().date()
        mon = datetime(today.year, today.month, today.day)

        def at(hour, minute=0, day_offset=0):
            return mon.replace(hour=hour, minute=minute) + timedelta(days=day_offset)

        self.add_task(text="", title=t("agent.demo_task_standup"),
                      start=at(9), duration_min=20,
                      priority=Priority.HIGH, recurrence="daily")
        self.add_task(text="", title=t("agent.demo_task_report"),
                      due=at(12), duration_min=40, priority=Priority.MEDIUM)
        self.add_task(text="", title=t("agent.demo_task_review"),
                      start=at(14), duration_min=60, priority=Priority.HIGH)
        self.add_task(text="", title=t("agent.demo_task_gym"),
                      start=at(20), duration_min=45, priority=Priority.LOW)
        self.add_task(text="", title=t("agent.demo_task_call"),
                      start=at(10, day_offset=1), duration_min=30,
                      priority=Priority.HIGH)
        return {"ok": True, "action": "demo",
                "message": t("agent.demo_done"),
                "schedule": self._today_schedule(), "tasks": None}

    # ---------------- 聊天意图路由 ----------------
    def chat(self, message):
        """把一句自然语言路由到对应动作，返回结构化结果。

        **意图只由大模型判断**（llm_nlp.understand）。大模型不可用（未配 Key、
        网络失败、余额不足、返回格式错）时按 `use_rule_fallback` 决定：
          - False（默认）-> 返回 action="error"，把真实原因告诉用户；
          - True         -> 才退化为关键词规则 `_chat_rule`。
        为什么默认不兜底：关键词规则是**闭集**，遇到规则没覆盖的说法不会报错，
        只会硬塞进最接近的意图（历史 bug：「ai开发设置为高优先级」被建成新任务）。
        宁可明确失败，也不要静默猜错。
        """
        text = (message or "").strip()
        if not text:
            return {"ok": False, "action": "chat",
                    "message": t("agent.empty_input"),
                    "schedule": None, "tasks": None}

        if not llm_nlp.llm_enabled():
            return self._fallback_or_error(text, _llm_no_key())

        decision = llm_nlp.understand(text)
        if decision is None:
            reason = llm_nlp.last_error() or t("agent.llm_empty_reply")
            return self._fallback_or_error(text, reason)
        return self._dispatch(decision, text)

    def _fallback_or_error(self, text, reason):
        """大模型不可用时的唯一出口：开关关闭 -> 明确报错；开启 -> 规则兜底。"""
        if self.use_rule_fallback:
            res = self._chat_rule(text)
            res["degraded"] = True
            res["degraded_reason"] = reason
            return res
        return {
            "ok": False,
            "action": "error",
            "message": t("agent.llm_error", reason=reason),
            "schedule": None, "tasks": None,
        }

    @staticmethod
    def _static_events(result):
        """把一次性结果包装成流式事件序列（非流式场景用）。"""
        if result.get("action") == "error":
            return [{"type": "error", "message": result.get("message", "")},
                    {"type": "done"}]
        return [{"type": "result", "action": result.get("action"), "data": result},
                {"type": "token", "text": result.get("message", "")},
                {"type": "done"}]

    def _snapshot_history(self):
        """返回当前对话历史的副本（在锁内读取，供异步回复生成使用）。"""
        return list(self.history)

    async def chat_stream(self, message):
        """流式聊天（异步生成器）：逐块产出事件字典，供 API/UI 做打字机效果。

        并发友好的设计：
          - 大模型的「理解」与「回复生成」用真正的异步网络 I/O（httpx）。
          - 动作执行（读写库、排期）用 asyncio.to_thread 卸载到线程池，
            不阻塞事件循环，从而可被多个并发请求同时服务。
          - history 的写操作受 asyncio.Lock 保护，避免并发流式请求互相踩踏。

        事件类型：
          {"type": "status", "text": ...}   处理阶段提示（如「正在理解…」）
          {"type": "result", "action": ..., "data": {...}}   执行结果（结构化）
          {"type": "token",  "text": ...}   回复文本增量（流式）
          {"type": "done"}                  结束
          {"type": "error",  "message": ...} 输入为空等错误

        当大模型不可用时，按 `use_rule_fallback` 决定是「明确报错」还是「规则兜底」，
        两种情况都不流式（直接一次性产出事件）。
        """
        text = (message or "").strip()
        if not text:
            yield {"type": "error", "message": t("agent.empty_input")}
            return

        # 大模型不可用：开关关闭 -> 报错；开启 -> 规则兜底（都不流式）
        if not llm_nlp.llm_enabled():
            result = self._fallback_or_error(text, _llm_no_key())
            for ev in self._static_events(result):
                yield ev
            return

        yield {"type": "status", "text": t("agent.status_understanding")}
        decision = await llm_nlp.understand_async(text)
        if decision is None:
            reason = llm_nlp.last_error() or t("agent.llm_empty_reply")
            result = self._fallback_or_error(text, reason)
            for ev in self._static_events(result):
                yield ev
            return

        # 动作执行（DB/排期）卸载到线程池，避免阻塞事件循环
        result = await asyncio.to_thread(self._dispatch, decision, text)
        yield {"type": "result", "action": result["action"], "data": result}

        # 用大模型把执行结果说成自然语言，异步流式产出
        yield {"type": "status", "text": t("agent.status_replying")}
        reply = ""
        try:
            hist_snapshot = self._snapshot_history()
            async for tok in llm_nlp.respond_stream_async(text, result, hist_snapshot):
                reply += tok
                yield {"type": "token", "text": tok}
        except Exception as e:  # noqa
            reply = result.get("message", "")
            yield {"type": "token", "text": reply}

        # 维护多轮对话历史（受锁保护，避免并发流式请求互相踩踏）
        async with self._hist_lock:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            if len(self.history) > 20:
                self.history = self.history[-20:]

        yield {"type": "done"}

    def _dispatch(self, d, text):
        """大模型理解结果 -> 执行对应动作。

        意图先经 `_INTENT_ALIASES` 归一化，再分发；**未知动作一律如实提问，
        绝不猜成「新增」**——猜错不会报错，只会多出一条垃圾待办。
        """
        raw_intent = (d.get("intent") or "").strip().lower()
        intent = _INTENT_ALIASES.get(raw_intent, raw_intent)

        if intent == "add":
            title = (d.get("title") or "").strip()
            # 模型说「新增」却既没给标题也没给时间：说明它也没把握，别拿原句硬当标题建任务
            if not title and not (d.get("start") or d.get("due")):
                return {"ok": False, "action": "chat",
                        "message": t("agent.need_title_or_time"),
                        "schedule": None, "tasks": None}
            return self.add_task(
                text="",
                due=d.get("due"),
                start=d.get("start"),
                duration_min=int(d.get("duration_min") or 30),
                priority=d.get("priority") or Priority.MEDIUM,
                title=title or text,
                recurrence=(d.get("recurrence") or "").strip(),
                skip_nlu=True,
            )

        if intent in ("done", "remove"):
            ids = list(d.get("task_ids") or [])
            ids += self._match_hint(d.get("title_hint"))
            if not ids:
                key = ("agent.need_task_ref_done" if intent == "done"
                       else "agent.need_task_ref_remove")
                return {"ok": False, "action": "chat",
                        "message": t(key),
                        "schedule": None, "tasks": None}
            fn = self.mark_done if intent == "done" else self.remove
            return self._multi(fn, ids)

        # ---- 只改已有安排的优先级（「ai开发设置为高优先级」）----
        # 没有这个分支时，模型只能判成 add，凭空多出一条同名待办（历史上的凌晨排期）。
        if intent in ("priority", "prioritize", "set_priority", "reprioritize"):
            hint = (d.get("title_hint") or d.get("title") or "").strip()
            prio = d.get("priority")
            rule = nlp.parse_priority(text)
            if rule:                      # 原文里的「高/中/低」比模型输出可靠
                hint = rule["title_hint"] or hint
                prio = rule["priority"]
            return self.modify_task(title_hint=hint, priority=prio)

        if intent in ("modify", "reschedule", "update"):
            return self.modify_task(
                title_hint=d.get("title_hint") or d.get("title"),
                start=d.get("start"), due=d.get("due"),
                duration_min=d.get("duration_min"),
                task_ids=d.get("task_ids"),
            )

        if intent == "today":
            return self.show_today()
        if intent == "list":
            return self.list_tasks()
        if intent == "templates":
            return self.list_templates()
        if intent == "summary":
            return self.summary()
        if intent == "demo":
            return self.seed_demo()

        # unknown：prompt 约定「无法归类时尽量按 add 处理」，但只认模型真的给了标题；
        # 没给标题就说明模型也没把握，此时**不要猜**。
        if intent == "unknown" and d.get("title"):
            return self.add_task(
                text="",
                due=d.get("due"),
                start=d.get("start"),
                duration_min=int(d.get("duration_min") or 30),
                priority=d.get("priority") or Priority.MEDIUM,
                title=d.get("title"),
                recurrence=(d.get("recurrence") or "").strip(),
                skip_nlu=True,
            )

        # 走到这里说明：意图缺失，或是系统还不支持的动作。
        # 正确做法是如实说明，而不是塞进 add —— 静默猜错比明确失败更糟。
        if self.use_rule_fallback:
            return self._chat_rule(text)
        detail = (t("agent.unknown_intent_detail", intent=raw_intent)
                  if raw_intent not in ("", "unknown") else "")
        return {"ok": False, "action": "chat",
                "message": t("agent.unknown_intent", detail=detail),
                "schedule": None, "tasks": None}

    def _match_hint(self, hint):
        """按标题关键词匹配任务，**分档命中，只取最贴近的一档**。

        早期版本把「包含/被包含」一视同仁，导致说「ai开发」会同时命中
        《ai开发》和《学习ai开发》，一次修改误伤两条。现在的优先级：
          1. 标题完全相同
          2. 标题以关键词开头/结尾（《学习ai开发》≈「学习ai开发」）
          3. 关键词是标题的子串（「自习」→《去自习》）
        且「当天的 pending 实例」优先于「周期模板」（避免随手说『做完晨会』
        就停掉整个周期）。
        """
        if not hint:
            return []
        hint = hint.strip()
        pending = ([], [], [])
        templates = ([], [], [])
        for task in self.store.all(self.user_id):
            if task.status == "done":
                continue
            title = task.title or ""
            if title == hint:
                score = 0
            elif title.startswith(hint) or title.endswith(hint):
                score = 1
            elif hint in title or title in hint:
                score = 2
            else:
                continue
            bucket = templates if task.is_template() else pending
            bucket[score].append(task.id)
        for group in (pending, templates):
            for ids in group:
                if ids:
                    return ids
        return []

    def _chat_rule(self, text):
        """规则兜底（仅在大模型不可用/失败时）：用关键词粗略路由。"""
        low = text.lower()

        if any(k in text for k in ("今日日程", "今天日程", "今日安排", "今天安排",
                                   "今天有什么", "我的日程", "今天干嘛", "今天做啥",
                                   "查看日程", "看看日程", "看下日程", "看一下日程",
                                   "日程安排", "我的安排", "今天的安排")) \
                or low in ("today", "plan", "今日", "今天"):
            return self.show_today()

        if any(k in text for k in ("待办列表", "任务列表", "列出任务", "所有任务")) \
                or low in ("list", "列表", "待办", "任务"):
            return self.list_tasks()

        if any(k in text for k in ("周期任务", "周期模板", "模板列表", " recurring")) \
                or low in ("templates", "模板"):
            return self.list_templates()

        if any(k in text for k in ("速览", "今日总结", "日程总结", "汇总", "总结一下")) \
                or low == "summary":
            return self.summary()

        if text in ("demo", "示例", "灌示例", "示例数据"):
            return self.seed_demo()

        # ---- 只改优先级（「ai开发设置为高优先级」）----
        # 放在 parse_modify 之前：这类句子有时也带「改成」，但右边是优先级不是时间
        pm = nlp.parse_priority(text)
        if pm:
            hint = pm["title_hint"]
            if hint and self._match_hint(hint):
                return self.modify_task(title_hint=hint, priority=pm["priority"])
            if hint:
                return {"ok": False, "action": "chat",
                        "message": t("agent.priority_not_found", hint=hint),
                        "schedule": None, "tasks": None}
            return {"ok": False, "action": "chat",
                    "message": t("agent.ask_which_priority"),
                    "schedule": None, "tasks": None}

        # ---- 修改已有安排的时间（改到/改成/推迟到/提前到…）----
        # 必须放在「兜底守卫」之前：守卫把「改成」当成纠正类闲聊会拦下来
        mod = nlp.parse_modify(text)
        if mod:
            return self.modify_task(
                title_hint=mod["title_hint"], start=mod["start"],
                due=mod["due"], duration_min=mod["duration_min"])

        if re.search(r"(完成|做完|搞定|结束|done)", low) or "完成" in text:
            ids = [int(x) for x in re.findall(r"\d+", text)]
            if not ids:
                ids = self._match_hint(_strip_verbs(text, _DONE_VERBS))
            if ids:
                return self._multi(self.mark_done, ids)
            return {"ok": False, "action": "chat",
                    "message": t("agent.need_done_ref"),
                    "schedule": None, "tasks": None}

        if re.search(r"(删除|删掉|移除|去掉|取消任务|del|rm)", low) or "删" in text:
            ids = [int(x) for x in re.findall(r"\d+", text)]
            if not ids:
                ids = self._match_hint(_strip_verbs(text, _DEL_VERBS))
            if ids:
                return self._multi(self.remove, ids)
            return {"ok": False, "action": "chat",
                    "message": t("agent.need_remove_ref"),
                    "schedule": None, "tasks": None}

        # ---- 兜底守卫：不像新增任务时，不凭空建待办 ----
        if nlp.looks_like_non_task(text):
            return {"ok": False, "action": "chat",
                    "message": t("agent.non_task_hint"),
                    "schedule": None, "tasks": None}

        return self.add_task(text)

    @staticmethod
    def _multi(fn, ids):
        msgs = []
        sched = None
        last = None
        for i in ids:
            res = fn(i)
            msgs.append(res["message"])
            sched = res.get("schedule") or sched
            last = res
        return {"ok": True, "action": last["action"] if last else "done",
                "message": "\n".join(msgs),
                "schedule": sched, "tasks": None}
