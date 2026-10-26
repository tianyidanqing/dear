#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行 / REPL 入口（薄封装）。

真正的逻辑在 service.ScheduleAgent；本文件只负责装配依赖与打印结果：

  schedule-agent                    # 进入交互式 REPL（默认开启提醒）
  schedule-agent add "明天下午3点开会 1小时 高"
  schedule-agent list / templates / today / summary
  schedule-agent done 3 / rm 3 / demo

多用户：命令行绕过 HTTP 直连本地库，因此**不需要 API Token**；
用 `--user alice`（默认 `default`）指定操作哪个用户的数据即可。

若想用微服务 + 网页聊天窗口，请用 `schedule-agent-api` 与 `schedule-agent-ui`。
"""
import argparse
import asyncio
import sys

from schedule_agent.config import get_settings
from schedule_agent.i18n import set_locale, t
from schedule_agent.logging_conf import get_logger, setup_logging
from schedule_agent.llm import client as llm_nlp
from schedule_agent.service.agent import ScheduleAgent
from schedule_agent.service.notifier import notify
from schedule_agent.service.registry import AgentRegistry
from schedule_agent.storage.db import get_store

log = get_logger(__name__)


def build_agent(user_id: str = "default"):
    """装配命令行所需的最小依赖组合（store -> 引擎 -> 注册表 -> agent）。"""
    settings = get_settings()
    settings.ensure_dirs()
    setup_logging(settings.log_dir, settings.log_level)
    set_locale(settings.language)      # 命令行也跟随配置 / SCHEDULE_LANGUAGE
    store = get_store(settings.db_path)
    registry = AgentRegistry(store, notifier_fn=notify, cfg=settings.as_dict())
    return registry.get(user_id)


def _fmt_schedule(sched):
    if not sched:
        return ""
    lines = [t("cli.schedule_header")]
    for it in sched:
        ss = (it.get("scheduled_start") or it.get("start") or "")[:16]
        se = (it.get("scheduled_end") or "")[:16]
        flag = t("cli.risk_overdue") if it.get("risk") else ""
        lines.append(t("cli.schedule_line", start=ss, end=se,
                       title=it["title"], priority=it["priority"], flag=flag))
    return "\n".join(lines)


def _fmt_tasks(tasks):
    if not tasks:
        return t("cli.none")
    lines = []
    for task in tasks:
        # 优先显示开始时间（按点开始的任务没有截止时间，别显示成「无截止」）
        when = (task.get("start") or "")[:16]
        label = t("cli.label_start") if when else t("cli.label_due")
        if not when:
            when = (task.get("due") or "")[:16] or t("common.none")
        extra = (t("cli.recur_extra", rule=task["recurrence"])
                 if task.get("recurrence") else "")
        risk = (t("cli.risk_passed") if task.get("risk") == 2
                else (t("cli.risk_overdue") if task.get("risk") else ""))
        lines.append(t("cli.task_line", id=task["id"], status=task["status_cn"],
                       title=task["title"], label=label, when=when,
                       minutes=task["duration_min"], priority=task["priority"],
                       extra=extra, risk=risk))
    return "\n".join(lines)


def print_result(res):
    print(res.get("message", ""))
    if res.get("schedule"):
        print(_fmt_schedule(res["schedule"]))
    if res.get("tasks"):
        print(_fmt_tasks(res["tasks"]))


async def _stream_print(agent: ScheduleAgent, line: str):
    """异步消费 chat_stream，逐字打印回复并附上排期。"""
    sched = None
    async for ev in agent.chat_stream(line):
        if ev["type"] == "token":
            print(ev["text"], end="", flush=True)
        elif ev["type"] == "result":
            sched = ev["data"].get("schedule")
    print()
    if sched:
        print(_fmt_schedule(sched))


def repl(agent: ScheduleAgent):
    agent.start_engine()
    print("=" * 50)
    print(t("cli.banner"))
    print("=" * 50)
    print_result(agent.show_today())
    while True:
        try:
            line = input(t("cli.prompt")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n" + t("cli.bye"))
            agent.stop_engine()
            break
        if not line:
            continue
        if line in ("exit", "quit", "退出", "q"):
            agent.stop_engine()
            print(t("cli.bye"))
            break
        # 配置了大模型则走流式（逐字打印回复），否则走普通非流式
        if llm_nlp.llm_enabled():
            asyncio.run(_stream_print(agent, line))
        else:
            print_result(agent.chat(line))


def main(argv=None):
    """命令行入口（console script: schedule-agent）。

    本机直连，不经 HTTP，因此不需要 API Token；用 --user 指定数据归属。
    """
    ap = argparse.ArgumentParser(
        prog="schedule-agent", description=t("cli.desc"))
    ap.add_argument("--user", default="default", help=t("cli.help_user"))
    ap.add_argument("command", nargs="*",
                    help="add / list / templates / today / summary / done / rm / demo / repl")
    ns = ap.parse_args(argv)

    agent = build_agent(ns.user)
    print(t("cli.user_line", user=ns.user))

    args = ns.command
    if not args:
        repl(agent)
        return

    cmd = args[0].lower()
    rest = " ".join(args[1:])

    if cmd == "add":
        print_result(agent.add_task(rest))
    elif cmd == "list":
        print_result(agent.list_tasks())
    elif cmd == "templates":
        print_result(agent.list_templates())
    elif cmd == "summary":
        print_result(agent.summary())
    elif cmd in ("today", "plan"):
        print_result(agent.show_today())
    elif cmd == "done":
        for x in rest.split():
            if x.isdigit():
                print_result(agent.mark_done(int(x)))
    elif cmd in ("rm", "del", "delete"):
        for x in rest.split():
            if x.isdigit():
                print_result(agent.remove(int(x)))
    elif cmd == "demo":
        print_result(agent.seed_demo())
    elif cmd == "repl":
        repl(agent)
    else:
        print(t("cli.unknown_cmd", cmd=cmd))


if __name__ == "__main__":
    main()
