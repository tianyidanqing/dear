# -*- coding: utf-8 -*-
"""【脚手架】新渠道配置模板。

复制整个 `_template` 目录为你的渠道名（例如 `feishu`），再改三处：
  1. NAME / DISPLAY_NAME / REQUIRED
  2. 下面的字段
  3. LEGACY_* （没有旧配置就留空字符串）

配置读取顺序由框架统一处理，你不需要写任何解析代码：
  channels.feishu.app_id（配置文件） > SCHEDULE_CHANNEL_FEISHU_APP_ID（环境变量）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from schedule_agent.channels.base import ChannelConfig


@dataclass
class TemplateConfig(ChannelConfig):
    NAME = "template"
    DISPLAY_NAME = "示例渠道"
    REQUIRED = ("app_id", "app_secret")
    LEGACY_PREFIX = ""              # 例如 "feishu_"（旧式扁平配置键）
    LEGACY_ENV_PREFIX = ""          # 例如 "SCHEDULE_FEISHU_"

    app_id: str = ""
    app_secret: str = ""
    # 平台侧用户 -> 本系统 user_id 的绑定表，格式与企微一致
    bind_map: dict = field(default_factory=dict)
    # 若平台有消息长度限制，按字节算（中文 3 字节）
    text_limit: int = 4096
