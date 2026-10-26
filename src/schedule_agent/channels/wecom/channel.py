# -*- coding: utf-8 -*-
"""企业微信渠道：把「企业微信自建应用」适配成通用的 `Channel`。

平台特有的东西全部收在这个包里：
  - config.py   五项凭据 + 用户绑定表
  - crypto.py   签名校验 / AES-256-CBC 加解密
  - messages.py XML 解析 / 用户映射 / 纯文本排版
  - client.py   access_token + 应用消息发送
  - channel.py  ← 本文件：只做适配，不含业务逻辑

业务逻辑一行都没重写——统一调 `agent.chat()`，所以多用户隔离、排期、提醒
全部自动继承。
"""
from __future__ import annotations

from typing import Mapping

from schedule_agent.channels.base import (
    Channel,
    ChannelAuthError,
    ChannelBadRequest,
    IncomingMessage,
)
from schedule_agent.channels.wecom.client import WeComClient, WeComError
from schedule_agent.channels.wecom.config import WeComConfig
from schedule_agent.channels.wecom.crypto import WeComCrypto
from schedule_agent.channels.wecom.messages import (
    extract_encrypt,
    format_reply as _format_reply,
    parse_message,
    resolve_user_id as _resolve_user_id,
)
from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)


class WeComChannel(Channel):
    """企业微信自建应用。"""

    name = "wecom"
    display_name = "企业微信"
    CONFIG = WeComConfig
    legacy_paths = ("/api/wecom/callback",)
    # 非文本提示沿用 Channel 的默认实现（按用户语言取词），此处不再覆盖

    def __init__(self, cfg: WeComConfig):
        super().__init__(cfg)
        self.cfg: WeComConfig = cfg
        # 构造即校验：AESKey 长度 / AgentId 非数字这类问题要在启动时暴露，
        # 而不是等第一条消息进来才报错。
        self.crypto = WeComCrypto(cfg.token, cfg.aes_key, cfg.corp_id)
        self.client = WeComClient(cfg.corp_id, cfg.secret, cfg.agent_id_int(),
                                  base=cfg.api_base, text_limit=cfg.text_limit)

    # ---------------- 入站 ----------------
    def verify_url(self, params: Mapping[str, str]) -> str:
        """URL 验证：解密 echostr 并原样返回明文，企业微信据此判定回调可用。"""
        try:
            return self.crypto.decrypt_echo(
                params.get("msg_signature", ""), params.get("timestamp", ""),
                params.get("nonce", ""), params.get("echostr", ""))
        except ValueError as e:
            raise ChannelAuthError(str(e)) from e

    def parse(self, body: bytes, params: Mapping[str, str]) -> IncomingMessage | None:
        """验签 → 解密 → 过滤 → 去重，最后归一化成 `IncomingMessage`。"""
        raw = body.decode("utf-8", errors="ignore")
        try:
            envelope = extract_encrypt(raw)
        except ValueError as e:
            raise ChannelBadRequest(str(e)) from e

        if not self.crypto.verify(params.get("msg_signature", ""),
                                  params.get("timestamp", ""),
                                  params.get("nonce", ""),
                                  envelope["Encrypt"]):
            raise ChannelAuthError("签名校验失败（Token 与后台配置不一致？）")

        try:
            msg = parse_message(self.crypto.decrypt(envelope["Encrypt"]))
        except Exception as e:
            raise ChannelBadRequest(f"消息解密失败：{e}") from e

        # 一个企业可能配了多个应用到同一台服务器，靠 AgentID 区分
        agent_id = (msg.get("agent_id") or "").strip()
        expected = str(self.cfg.agent_id)
        if agent_id and agent_id != expected:
            log.info("忽略其他应用的消息：AgentID=%s（本应用 %s）", agent_id, expected)
            return None

        # 平台超时会重发同一个 MsgId，只处理第一次
        if not self.deduper.first_time(f"{msg['from_user']}:{msg['msg_id']}"):
            log.info("重复消息已忽略：MsgId=%s", msg["msg_id"])
            return None

        return IncomingMessage(
            channel=self.name,
            external_user=msg["from_user"],
            text=msg["content"],
            msg_id=msg["msg_id"],
            is_text=(msg["msg_type"] == "text"),
            raw=msg,
        )

    # ---------------- 出站 ----------------
    def send(self, external_user: str, text: str) -> None:
        self.client.send_text(external_user, text)

    # ---------------- 语义转换 ----------------
    def resolve_user_id(self, external_user: str) -> str:
        try:
            return _resolve_user_id(external_user, self.cfg.bind())
        except ValueError as e:
            raise ChannelBadRequest(str(e)) from e

    def format_reply(self, result: Mapping) -> str:
        return _format_reply(str(result.get("message") or ""),
                             result.get("schedule"),
                             limit=self.cfg.text_limit)


# 注册表靠这个属性发现渠道（放进子包就生效）
CHANNEL = WeComChannel
__all__ = ["WeComChannel", "WeComConfig", "WeComClient", "WeComError",
           "WeComCrypto", "CHANNEL"]
