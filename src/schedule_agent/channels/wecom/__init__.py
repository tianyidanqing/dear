# -*- coding: utf-8 -*-
"""企业微信渠道包。

目录内自查：
  config.py    五项凭据 + 用户绑定（属于这个渠道，不进全局 Settings）
  crypto.py    签名 / AES-256-CBC
  messages.py  XML 解析 / 用户映射 / 纯文本排版
  client.py    access_token + 应用消息
  channel.py   适配成通用 Channel
"""
from __future__ import annotations

from schedule_agent.channels.wecom.channel import CHANNEL, WeComChannel
from schedule_agent.channels.wecom.client import WeComClient, WeComError
from schedule_agent.channels.wecom.config import WeComConfig
from schedule_agent.channels.wecom.crypto import WeComCrypto

__all__ = ["CHANNEL", "WeComChannel", "WeComConfig", "WeComClient",
           "WeComError", "WeComCrypto"]
