# -*- coding: utf-8 -*-
"""API 层：FastAPI 应用工厂、鉴权依赖、事件总线与路由。"""
from schedule_agent.api.app import create_app  # noqa: F401

__all__ = ["create_app"]
