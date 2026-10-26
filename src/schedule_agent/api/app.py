#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FastAPI 应用工厂：装配依赖、挂载路由、管理生命周期。

采用**工厂模式**而非模块级全局变量，好处：
  - 测试可用临时目录 / 替身依赖构造独立实例，互不干扰；
  - 部署时可在启动脚本里决定配置来源（文件、环境变量）；
  - 依赖（store / 引擎 / 事件总线 / Agent 注册表）集中装配，关系一目了然。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from schedule_agent import __version__
from schedule_agent.api.events import EventBus
from schedule_agent.api.routers import admin, auth, chat, events, health, tasks
from schedule_agent.channels import build_router as build_channel_router
from schedule_agent.channels import load_channels
from schedule_agent.config import config_source, get_settings
from schedule_agent.i18n import reset_locale, resolve_locale, set_locale, t
from schedule_agent.logging_conf import get_logger, setup_logging
from schedule_agent.service.registry import AgentRegistry
from schedule_agent.storage.db import get_store

log = get_logger(__name__)


def create_app(settings=None, store=None, bus=None, registry=None) -> FastAPI:
    """构造一个 FastAPI 应用实例。

    任何参数为空时按默认配置装配；显式传入则用于测试或特殊部署。
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    setup_logging(settings.log_dir, settings.log_level)

    store = store or get_store(settings.db_path)
    bus = bus or EventBus()
    # 提醒引擎由注册表统一创建并持有（单进程唯一），notifier 直接打进事件总线
    registry = registry or AgentRegistry(
        store, notifier_fn=bus.notifier, cfg=settings.as_dict())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("配置文件：%s", config_source())
        log.info("数据目录：%s（DB=%s）", settings.data_dir, settings.db_path)
        engine_task = asyncio.create_task(registry.engine.run_loop())
        log.info("异步提醒引擎已启动（多用户模式，按 user_id 隔离推送）。")
        if settings.admin_api_key:
            log.info("已启用 admin_api_key（配合 X-User-Id 使用）。")
        try:
            yield
        finally:
            registry.engine.stop()
            engine_task.cancel()
            try:
                await engine_task
            except asyncio.CancelledError:
                pass
            log.info("服务关闭，提醒引擎已停止。")

    # OpenAPI 元数据在**建应用时**求值一次（不是每请求），所以这里取的是配置里的
    # 默认语言；`/docs`、`/openapi.json` 属于开发者文档，不跟随用户语言变化。
    app = FastAPI(
        title=t("api.title"),
        description=t("api.description"),
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials="*" not in settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _locale_scope(request, call_next):
        """按请求决定语言：`?lang=` > `Accept-Language` > 配置默认值。

        语言放在 contextvars 里，因此**并发请求各自独立**——中文用户和英文
        用户同时访问不会互相串味。`asyncio.to_thread` 会复制上下文，
        所以线程池里执行的排期 / 建任务也拿到同一语言。
        """
        loc = resolve_locale(explicit=request.query_params.get("lang"),
                             accept_language=request.headers.get("accept-language"))
        token = set_locale(loc)
        try:
            response = await call_next(request)
        finally:
            reset_locale(token)
        response.headers["Content-Language"] = loc
        return response

    # 共享资源挂到 app.state，路由通过依赖读取
    app.state.settings = settings
    app.state.store = store
    app.state.bus = bus
    app.state.registry = registry
    # 已启用的第三方渠道（未配置的就是空表）。渠道自己管配置与协议，
    # 这里只负责实例化与挂载——新增平台不需要改这个文件。
    app.state.channels = load_channels(settings)

    for r in (health.router, auth.router, chat.router,
              tasks.router, events.router, admin.router):
        app.include_router(r)
    app.include_router(build_channel_router())

    for ch in app.state.channels.values():
        log.info("%s 接入已启用（%s），回调地址：%s", ch.display_name, ch.name,
                 ", ".join((ch.callback_path,) + tuple(ch.legacy_paths)))

    return app


def serve(host: str | None = None, port: int | None = None, reload: bool = False,
          log_level: str | None = None):
    """用 uvicorn 拉起服务（参数为空时取配置）。"""
    import uvicorn
    s = get_settings()
    uvicorn.run(
        "schedule_agent.api.app:app",
        host=host or s.api_host,
        port=int(port or s.api_port),
        reload=reload,
        log_level=(log_level or s.log_level).lower(),
    )


def main(argv=None):
    """console script 入口：schedule-agent-api"""
    import argparse
    s = get_settings()
    ap = argparse.ArgumentParser(description=t("cli.api_desc"))
    ap.add_argument("--host", default=s.api_host)
    ap.add_argument("--port", type=int, default=s.api_port)
    ap.add_argument("--reload", action="store_true", help=t("cli.help_reload"))
    ap.add_argument("--log-level", default=s.log_level)
    ns = ap.parse_args(argv)
    serve(host=ns.host, port=ns.port, reload=ns.reload, log_level=ns.log_level)


# `uvicorn schedule_agent.api.app:app` 需要模块级 app 实例
app = create_app()


if __name__ == "__main__":
    serve()
