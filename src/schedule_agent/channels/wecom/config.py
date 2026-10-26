# -*- coding: utf-8 -*-
"""企业微信渠道的配置（**只属于这个渠道，不污染全局 Settings**）。

字段来源优先级：旧式文件键 < `channels.wecom` < 旧环境变量 < 新环境变量
  - 旧式文件键：`wecom_corp_id`
  - 新式文件键：`{"channels": {"wecom": {"corp_id": ...}}}`
  - 旧环境变量：`SCHEDULE_WECOM_CORP_ID`
  - 新环境变量：`SCHEDULE_CHANNEL_WECOM_CORP_ID`
"""
from __future__ import annotations

from dataclasses import dataclass, field

from schedule_agent.channels.base import ChannelConfig


@dataclass
class WeComConfig(ChannelConfig):
    """企业微信自建应用配置（五项齐全才启用）。"""

    NAME = "wecom"
    DISPLAY_NAME = "企业微信"
    REQUIRED = ("corp_id", "agent_id", "secret", "token", "aes_key")
    LEGACY_PREFIX = "wecom_"
    LEGACY_ENV_PREFIX = "SCHEDULE_WECOM_"

    corp_id: str = ""           # 企业ID（我的企业 → 企业信息）
    agent_id: str = ""          # 应用ID（自建应用 → AgentId，纯数字）
    secret: str = ""            # 应用Secret
    token: str = ""             # 接收消息 → Token
    aes_key: str = ""           # 接收消息 → EncodingAESKey（43 字符）
    # 企微 UserID -> 本系统 user_id；不配则每人独立空间 wecom:<UserID>
    bind_map: dict = field(default_factory=dict)
    # 私有化部署可改成自己的网关地址
    api_base: str = "https://qyapi.weixin.qq.com/cgi-bin"
    # 企业微信 text.content 上限 2048 字节
    text_limit: int = 2048

    def bind(self) -> dict:
        return dict(self.bind_map or {})

    def agent_id_int(self) -> int:
        try:
            return int(self.agent_id)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"wecom.agent_id 必须是数字（应用详情里的 AgentId），"
                f"当前为 {self.agent_id!r}") from e
