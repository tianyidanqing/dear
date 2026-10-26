# -*- coding: utf-8 -*-
"""按资源拆分的路由模块。"""
from schedule_agent.api.routers import admin, auth, chat, events, health, tasks  # noqa: F401

__all__ = ["health", "auth", "chat", "tasks", "events", "admin"]
