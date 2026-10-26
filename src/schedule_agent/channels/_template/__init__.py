# -*- coding: utf-8 -*-
"""【脚手架】复制本目录并改名即可新增一个第三方接入渠道。

注意：本目录以 `_` 开头，**不会被自动发现**，改完名字才生效。
详见 `channel.py` 顶部的四步说明，以及 `channels/wecom/` 的完整实现示例。
"""
from __future__ import annotations

from schedule_agent.channels._template.channel import TemplateChannel

CHANNEL = TemplateChannel

__all__ = ["CHANNEL", "TemplateChannel"]
