# -*- coding: utf-8 -*-
"""【脚手架】新渠道适配模板 —— 四步接一个新平台，core 一行不用改。

    1. `cp -r channels/_template channels/<你的平台>`（目录名不以 _ 开头即自动发现）
    2. 改 config.py 里的 NAME / REQUIRED / 字段
    3. 改本文件的 parse() 与 send()（其余钩子有默认实现）
    4. `from .channel import CHANNEL` 写进 `__init__.py`

框架会自动做掉：路由挂载、验签失败 403、去重、后台执行、异常兜底、
未配置时返回 503 并说明缺哪几项。
"""
from __future__ import annotations

import json
from typing import Mapping

from schedule_agent.channels.base import (
    Channel,
    ChannelAuthError,
    ChannelBadRequest,
    IncomingMessage,
    truncate_bytes,
)
from schedule_agent.channels._template.config import TemplateConfig
from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)


class TemplateChannel(Channel):
    name = "template"
    display_name = "示例渠道"
    CONFIG = TemplateConfig
    # 非文本提示沿用 Channel 的默认实现（按用户语言取词）；
    # 想给这个平台定制文案，覆写 `non_text_hint` property 即可。

    # ---------------- 入站 ----------------
    def verify_url(self, params: Mapping[str, str]) -> str:
        """平台保存回调时若发 GET 校验，把明文原样返回；没有就删掉这个方法。"""
        token = params.get("token", "")
        if token != self.cfg.app_secret:
            raise ChannelAuthError("token 不匹配")
        return params.get("challenge", "")

    def parse(self, body: bytes, params: Mapping[str, str]) -> IncomingMessage | None:
        """把平台报文变成 IncomingMessage。

        - 验签失败 → 抛 ChannelAuthError（框架回 403，**不执行任何业务逻辑**）
        - 报文坏了 → 抛 ChannelBadRequest（框架回 400）
        - 想忽略这条 → return None（如别的机器人消息、重复消息）
        """
        if params.get("signature") != self.cfg.app_secret:
            raise ChannelAuthError("签名不匹配")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise ChannelBadRequest(f"报文不是合法 JSON：{e}") from e

        msg_id = str(data.get("msg_id") or "")
        if not self.deduper.first_time(msg_id):     # 平台重发同一条时只处理一次
            return None

        return IncomingMessage(
            channel=self.name,
            external_user=str(data.get("user") or ""),
            text=str(data.get("text") or ""),
            msg_id=msg_id,
            is_text=str(data.get("type") or "text") == "text",
            raw=data,
        )

    # ---------------- 出站 ----------------
    def send(self, external_user: str, text: str) -> None:
        """调平台 API 发消息。示例只写日志，真实实现换成 httpx 调用即可。"""
        log.info("[%s] -> %s: %s", self.display_name, external_user,
                 truncate_bytes(text, self.cfg.text_limit))

    # ---------------- 语义转换 ----------------
    def resolve_user_id(self, external_user: str) -> str:
        """默认每人独立空间；要共用网页端账号就把 bind_map 配起来。"""
        return (self.cfg.bind_map or {}).get(external_user) or super().resolve_user_id(external_user)
