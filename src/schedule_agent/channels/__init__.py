# -*- coding: utf-8 -*-
"""第三方接入层（channels）。

对外只暴露稳定入口：
    from schedule_agent.channels import load_channels, build_router

目录约定（**新增平台就照这个结构加一个子包，core 一行不用改**）：

    channels/
      base.py          # Channel 抽象基类 / ChannelConfig / 归一化消息 / 去重器
      registry.py      # 自动发现 + 实例化
      config.py        # 配置文件与环境变量的通用解析
      webhook.py       # 通用回调路由（/api/channels/<name>/callback）
      wecom/           # 企业微信（自带 config / crypto / client / messages）
      _template/       # 新渠道脚手架：复制改名即可

注意：本模块**不导入 fastapi**，因为 `config.py` 会在加载配置时导入它；
只有真正要挂路由时才懒加载 `webhook`。
"""
from __future__ import annotations

from schedule_agent.channels.base import (
    Channel,
    ChannelAuthError,
    ChannelBadRequest,
    ChannelConfig,
    ChannelError,
    ChannelUnavailable,
    IncomingMessage,
    MessageDeduper,
    strip_markdown,
    truncate_bytes,
)
from schedule_agent.channels.registry import (
    discover,
    get_class,
    load_channels,
    names,
    register,
)

__all__ = [
    "Channel",
    "ChannelConfig",
    "ChannelError",
    "ChannelAuthError",
    "ChannelBadRequest",
    "ChannelUnavailable",
    "IncomingMessage",
    "MessageDeduper",
    "strip_markdown",
    "truncate_bytes",
    "discover",
    "register",
    "get_class",
    "names",
    "load_channels",
    "build_router",
]


def build_router():
    """构造通用回调路由（延迟导入，避免核心安装也必须装 fastapi）。"""
    from schedule_agent.channels.webhook import build_router as _build
    return _build()
