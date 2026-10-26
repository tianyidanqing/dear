# -*- coding: utf-8 -*-
"""通用回调路由：**所有第三方共用一套**，与具体平台无关。

一个渠道的回调只有两件事：GET 验址、POST 收消息。这两件事的流程对任何平台
都一样，所以只写一遍：

    GET  /api/channels/<name>/callback   -> channel.verify_url()
    POST /api/channels/<name>/callback   -> channel.parse() -> 后台跑业务 -> channel.send()

平台差异（签名算法、加解密、XML/JSON、用户映射）全部由 `Channel` 子类实现。
新增平台不需要动这个文件，也不需要动 `app.py`。

为什么是「先回空串 + 后台推送」：
  企业微信 / 钉钉这类平台要求回调 **5 秒内**返回，超时就会重试；而一次
  `agent.chat()` 要调两次大模型，很容易超时。所以收到消息后立刻返回空串
  （＝告诉平台「本次不回复」），后台跑完再主动推给用户——既不超时，也不会
  因为重试而重复建任务。

安全：回调端点**不走 Bearer 鉴权**（平台的服务器带不了我们的 Token），每个渠道
自己用签名校验兜底；验签失败的请求一律 403 且不执行任何业务逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from schedule_agent.channels.base import (
    Channel,
    ChannelAuthError,
    ChannelBadRequest,
    ChannelError,
    IncomingMessage,
)
from schedule_agent.channels.registry import discover
from schedule_agent.i18n import t
from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)


def build_router() -> APIRouter:
    """构造通用回调路由（含各渠道声明的旧路径别名）。"""
    router = APIRouter(tags=["channels"])

    router.add_api_route("/api/channels/{name}/callback", _verify_generic,
                         methods=["GET"], summary=t("api.op_verify"))
    router.add_api_route("/api/channels/{name}/callback", _receive_generic,
                         methods=["POST"], summary=t("api.op_receive"))

    # 旧路径（如 /api/wecom/callback）保持可用，避免已配置的平台要重新填地址
    for name, cls in discover().items():
        for path in getattr(cls, "legacy_paths", ()) or ():
            router.add_api_route(path, _verify_for(name), methods=["GET"],
                                 include_in_schema=False)
            router.add_api_route(path, _receive_for(name), methods=["POST"],
                                 include_in_schema=False)
    return router


# ---------------------------------------------------------------------------
# 路由实现
# ---------------------------------------------------------------------------
def _lookup(request: Request, name: str) -> Channel | None:
    channels = getattr(request.app.state, "channels", None) or {}
    return channels.get((name or "").strip().lower())


def _not_enabled(name: str, request: Request) -> PlainTextResponse:
    known = sorted(getattr(request.app.state, "channels", {}) or {})
    missing = t("common.list_sep").join(known) if known else t("channel.none_enabled")
    return PlainTextResponse(
        t("channel.not_configured", name=name, missing=missing) + "\n"
        + t("channel.not_configured_hint", name=name, env=name.upper()),
        status_code=503)


async def _verify_generic(request: Request, name: str):
    return _verify(request, name)


async def _receive_generic(request: Request, background: BackgroundTasks, name: str):
    return await _receive(request, background, name)


def _verify(request: Request, name: str):
    """GET：平台保存回调配置时的 URL 校验。"""
    channel = _lookup(request, name)
    if channel is None:
        return _not_enabled(name, request)
    try:
        return PlainTextResponse(channel.verify_url(_params(request)))
    except ChannelAuthError as e:
        log.warning("%s URL 校验被拒绝：%s", channel.display_name or name, e)
        return PlainTextResponse(f"invalid: {e}", status_code=403)
    except ChannelError as e:
        return PlainTextResponse(str(e), status_code=400)


async def _receive(request: Request, background: BackgroundTasks, name: str):
    """POST：收消息 → 解析 → 后台处理 → 立刻返回空串。"""
    channel = _lookup(request, name)
    if channel is None:
        return _not_enabled(name, request)

    body = await request.body()
    try:
        msg = channel.parse(body, _params(request))
    except ChannelAuthError as e:
        log.warning("%s 回调验签失败：%s", channel.display_name or name, e)
        return PlainTextResponse("", status_code=403)
    except ChannelBadRequest as e:
        log.warning("%s 回调报文异常：%s", channel.display_name or name, e)
        return PlainTextResponse("", status_code=400)

    if msg is None:                     # 渠道主动忽略（他应用消息 / 重复消息）
        return PlainTextResponse("")

    if not msg.is_text or not (msg.text or "").strip():
        background.add_task(channel.send_safe, msg.external_user, channel.non_text_hint)
        return PlainTextResponse("")

    background.add_task(_handle, request.app.state.registry, channel, msg)
    # 空串 = 告诉平台「本次不回复」，避免它以为超时去重试
    return PlainTextResponse("")


def _params(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.query_params.items()}


# ---------------------------------------------------------------------------
# 后台处理：跑业务逻辑，再把结果推回平台
# ---------------------------------------------------------------------------
def _handle(registry, channel: Channel, msg: IncomingMessage):
    try:
        user_id = channel.resolve_user_id(msg.external_user)
        agent = registry.get(user_id)
        result = agent.chat(msg.text)
        reply = channel.format_reply(result)
        channel.send_safe(msg.external_user, reply or t("channel.empty_reply"))
    except Exception as e:      # noqa: BLE001 - 后台任务必须兜住所有异常
        log.exception("%s 消息处理失败", channel.display_name or channel.name)
        channel.send_safe(msg.external_user, t("channel.handle_error", err=e))


# 旧路径的闭包工厂（FastAPI 需要具体路径 + 明确的函数签名）
def _verify_for(name: str):
    async def endpoint(request: Request):
        return _verify(request, name)
    return endpoint


def _receive_for(name: str):
    async def endpoint(request: Request, background: BackgroundTasks):
        return await _receive(request, background, name)
    return endpoint
