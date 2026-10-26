# -*- coding: utf-8 -*-
"""企业微信「应用消息」客户端（出站）。

只需要两件事：拿 access_token（进程内缓存）、发文本。token 失效（42001/40014）
时自动刷新并重试一次——企业微信的 token 两小时一换，长跑服务必然会遇到。
"""
from __future__ import annotations

import threading
import time

import httpx

from schedule_agent.channels.base import truncate_bytes


class WeComError(RuntimeError):
    """企业微信接口返回错误（errcode != 0）。"""


class WeComClient:
    """企业微信应用消息客户端（access_token 进程内缓存）。"""

    BASE = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, corp_id: str, secret: str, agent_id, timeout: float = 10.0,
                 base: str = "", text_limit: int = 2048):
        if not corp_id or not secret or not str(agent_id or "").strip():
            raise ValueError("企业微信需要 CorpID / Secret / AgentID 三项齐全")
        try:
            self.agent_id = int(agent_id)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"wecom.agent_id 必须是数字（应用详情里的 AgentId），当前为 {agent_id!r}") from e
        self.corp_id = corp_id
        self.secret = secret
        self.timeout = timeout
        self.base = (base or self.BASE).rstrip("/")
        self.text_limit = text_limit
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    # ---------- token ----------
    def access_token(self, force: bool = False) -> str:
        with self._lock:
            now = time.time()
            if not force and self._token and now < self._expire_at:
                return self._token
            try:
                r = httpx.get(f"{self.base}/gettoken",
                              params={"corpid": self.corp_id,
                                      "corpsecret": self.secret},
                              timeout=self.timeout)
                data = r.json()
            except Exception as e:
                raise WeComError(f"获取 access_token 失败：{e}") from e
            if data.get("errcode", 0) != 0:
                raise WeComError(
                    f"获取 access_token 失败：errcode={data.get('errcode')} "
                    f"{data.get('errmsg')}（检查 CorpID / Secret）")
            self._token = data["access_token"]
            # 提前 5 分钟过期，避免边界失效
            self._expire_at = now + int(data.get("expires_in", 7200)) - 300
            return self._token

    def invalidate_token(self):
        with self._lock:
            self._token, self._expire_at = "", 0.0

    # ---------- 发文本 ----------
    def send_text(self, touser: str, content: str) -> dict:
        """给企业成员发应用消息（touser 用 UserID，多个用 | 分隔）。"""
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": truncate_bytes(content, self.text_limit)},
            "safe": 0,
        }
        return self._post_with_retry("message/send", payload)

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        last = None
        for attempt in (1, 2):
            token = self.access_token(force=(attempt == 2))
            try:
                r = httpx.post(f"{self.base}/{path}",
                               params={"access_token": token},
                               json=payload, timeout=self.timeout)
                data = r.json()
            except Exception as e:
                last = WeComError(f"调用 {path} 失败：{e}")
                continue
            errcode = data.get("errcode", 0)
            if errcode == 0:
                return data
            # 42001=token 过期, 40014=token 非法, 40001=Secret 错 -> 刷新重试一次
            if errcode in (42001, 40014, 40001) and attempt == 1:
                self.invalidate_token()
                last = WeComError(f"token 失效（{errcode}），已重试")
                continue
            raise WeComError(f"发送消息失败：errcode={errcode} {data.get('errmsg')}")
        raise last or WeComError("发送消息失败：未知原因")
