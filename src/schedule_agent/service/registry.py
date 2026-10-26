# -*- coding: utf-8 -*-
"""Agent 注册表：按用户缓存 ScheduleAgent，并统一持有共享依赖。

为什么需要它：
  - **提醒引擎必须单进程唯一**。如果每个用户各建一个引擎，同一条待办会被
    多个引擎扫描到，从而重复提醒。因此引擎由注册表创建并注入给每个 Agent。
  - **Store 必须共享**。多用户共用一个 SQLite 连接与一把锁，避免多个连接
    各自持锁导致跨连接写冲突。
  - **每个用户一个 Agent**，多轮对话历史随实例天然隔离。
"""
from __future__ import annotations

import threading

from schedule_agent.config import get_settings
from schedule_agent.service.agent import ScheduleAgent
from schedule_agent.service.reminder import ReminderEngine


class AgentRegistry:
    """创建并缓存「按用户隔离」的 ScheduleAgent。"""

    def __init__(self, store, notifier_fn, engine=None, cfg=None):
        self.store = store
        self.notifier_fn = notifier_fn
        self.cfg = cfg or {}
        self.engine = engine or self._build_engine()
        self._agents: dict[str, ScheduleAgent] = {}
        self._lock = threading.Lock()

    def _build_engine(self) -> ReminderEngine:
        s = get_settings()
        return ReminderEngine(
            self.store, self.notifier_fn,
            lead_minutes=int(self.cfg.get("lead_minutes", s.lead_minutes)),
            interval=int(self.cfg.get("interval", s.interval)),
            daily_summary_time=self.cfg.get("daily_summary_time", s.daily_summary_time),
        )

    def get(self, user_id: str) -> ScheduleAgent:
        """取（或按需创建）该用户专属的 Agent。"""
        with self._lock:
            agent = self._agents.get(user_id)
            if agent is None:
                agent = ScheduleAgent(
                    user_id=user_id,
                    store=self.store,
                    notifier_fn=self.notifier_fn,
                    engine=self.engine,
                    cfg=self.cfg,
                )
                self._agents[user_id] = agent
            return agent

    def known_users(self):
        return list(self._agents.keys())

    def clear(self):
        with self._lock:
            self._agents.clear()
