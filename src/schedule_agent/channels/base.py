# -*- coding: utf-8 -*-
"""第三方接入的**统一抽象层**（与具体平台无关）。

为什么要有这一层：
  接一个平台（企业微信 / 钉钉 / 飞书 / Telegram …）要干的活儿其实是同一串：
  「验签 → 解密 → 解析消息 → 映射用户 → 调业务 → 排版回复 → 主动发送」。
  如果把这些直接写进 HTTP 路由，接第二个平台就得复制一遍路由和后台任务逻辑，
  还要在 `app.py`、`config.py` 里各加一段 if，越接越乱。

  所以平台差异全部收敛到 `Channel` 子类：路由层只认四个钩子
  `parse / send / format_reply / resolve_user_id`。
  **新增平台 = 新建一个子包**（照 `_template/` 抄），core 一行不改。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Mapping

from schedule_agent.i18n import t
from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# 异常：webhook 层据此决定 HTTP 状态码
# ---------------------------------------------------------------------------
class ChannelError(RuntimeError):
    """渠道通用错误（默认对应 400）。"""


class ChannelAuthError(ChannelError):
    """验签 / 解密失败 —— 一律 403，且**不执行任何业务逻辑**。"""


class ChannelBadRequest(ChannelError):
    """报文不合法（400）。"""


class ChannelUnavailable(ChannelError):
    """渠道未配置 / 未启用（503）。"""


# ---------------------------------------------------------------------------
# 统一消息结构：不管哪个平台，解析后都长这样
# ---------------------------------------------------------------------------
@dataclass
class IncomingMessage:
    """从第三方平台收到的一条消息（已归一化）。"""

    channel: str                      # 渠道名，如 wecom
    external_user: str                # 平台侧用户标识（企微 UserID / 微信 openid …）
    text: str = ""                    # 文本内容
    msg_id: str = ""                  # 平台消息 ID（用于去重）
    is_text: bool = True              # 非文本消息只回一句提示，不进业务逻辑
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 配置基类：每个渠道自己声明字段与必填项
# ---------------------------------------------------------------------------
@dataclass
class ChannelConfig:
    """渠道配置基类。

    子类只要写字段 + 三个类属性，配置解析（文件 / 环境变量 / 类型转换）
    全部由框架完成，不用再往 `Settings` 里加字段：

        @dataclass
        class FeishuConfig(ChannelConfig):
            NAME = "feishu"
            REQUIRED = ("app_id", "app_secret")
            app_id: str = ""
            app_secret: str = ""
    """

    NAME: ClassVar[str] = ""
    DISPLAY_NAME: ClassVar[str] = ""
    REQUIRED: ClassVar[tuple[str, ...]] = ()
    # 兼容旧配置：扁平文件键前缀（wecom_corp_id）与旧环境变量前缀（SCHEDULE_WECOM_）
    LEGACY_PREFIX: ClassVar[str] = ""
    LEGACY_ENV_PREFIX: ClassVar[str] = ""

    # ---------------- 解析 ----------------
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ChannelConfig":
        """把松散 dict（可能全是字符串）转成带类型的配置对象。"""
        raw = dict(raw or {})
        known = {f.name: f for f in fields(cls)}
        data: dict[str, Any] = {}
        for name, f in known.items():
            if name not in raw:
                continue
            data[name] = _coerce(raw[name], _type_name(f.type), name)
        unknown = set(raw) - set(known) - {k for k in raw if str(k).startswith("_")}
        if unknown:
            log.warning("渠道 %s 有无法识别的配置项（已忽略）：%s",
                        cls.NAME, "、".join(sorted(unknown)))
        return cls(**data)

    # ---------------- 状态 ----------------
    def missing(self) -> list[str]:
        """返回还没填的必填项（用于给出可操作的报错）。"""
        return [f for f in self.REQUIRED if not str(getattr(self, f, "") or "").strip()]

    @property
    def enabled(self) -> bool:
        return not self.missing()


def _type_name(tp) -> str:
    """`from __future__ import annotations` 下 field.type 是字符串，这里归一化。"""
    t = str(tp)
    if "bool" in t:
        return "bool"
    if "int" in t:
        return "int"
    if "dict" in t or "Dict" in t:
        return "dict"
    if "float" in t:
        return "float"
    return "str"


def _coerce(value: Any, kind: str, name: str) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        low = str(value).strip().lower()
        return low in {"1", "true", "yes", "on", "y", "t"}
    if kind == "int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            log.warning("配置项 %s 需要整数，收到 %r（按 0 处理）", name, value)
            return 0
    if kind == "float":
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return 0.0
    if kind == "dict":
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        raw = str(value or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("配置项 %s 需要 JSON 对象，解析失败（已忽略）：%s", name, e)
            return {}
        if not isinstance(data, dict):
            log.warning("配置项 %s 需要 JSON 对象，收到 %s（已忽略）", name, type(data).__name__)
            return {}
        return {str(k): str(v) for k, v in data.items()}
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# 去重：平台超时重发同一条消息时只能处理一次
# ---------------------------------------------------------------------------
class MessageDeduper:
    """记录最近处理过的消息 ID（进程内 LRU + TTL），防止重试导致重复建任务。"""

    def __init__(self, ttl_seconds: int = 300, max_items: int = 2000):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_time(self, key: str) -> bool:
        """首次出现返回 True；重复返回 False；无 key 时不去重（返回 True）。"""
        if not key:
            return True
        now = time.time()
        with self._lock:
            self._gc(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True

    def clear(self):
        with self._lock:
            self._seen.clear()

    def _gc(self, now: float):
        for k in [k for k, t in self._seen.items() if now - t > self.ttl]:
            self._seen.pop(k, None)
        if len(self._seen) > self.max_items:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[:len(self._seen) - self.max_items]:
                self._seen.pop(k, None)


# ---------------------------------------------------------------------------
# 文本工具（纯文本渠道都用的上）
# ---------------------------------------------------------------------------
def strip_markdown(text: str) -> str:
    """去掉 Markdown 标记：多数 IM 只显示纯文本，星号会原样露出来。"""
    return (text or "").replace("**", "").replace("__", "").replace("`", "")


def truncate_bytes(text: str, limit: int) -> str:
    """按**字节**截断（中文一个字 3 字节，按字符数算会超限）。"""
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:max(0, limit - 3)].decode("utf-8", errors="ignore") + "…"


# ---------------------------------------------------------------------------
# 渠道抽象基类
# ---------------------------------------------------------------------------
class Channel:
    """一个第三方接入渠道。

    子类必须提供：`name` / `display_name` / `CONFIG`（配置类），
    并实现 `parse`、`send`（其余三个钩子有可用默认实现）。
    """

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    CONFIG: ClassVar[type[ChannelConfig]] = ChannelConfig

    # 旧路径（保持向后兼容，例如 /api/wecom/callback），新路径统一为
    # /api/channels/<name>/callback
    legacy_paths: ClassVar[tuple[str, ...]] = ()

    @property
    def non_text_hint(self) -> str:
        """收到非文本消息时回的话（按当前语言取词，子类可覆盖）。"""
        return t("channel.non_text_hint")

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg
        self.deduper = MessageDeduper()

    # ---------------- 构造 / 状态 ----------------
    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> "Channel":
        return cls(cls.CONFIG.from_dict(raw))

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def missing(self) -> list[str]:
        return self.cfg.missing()

    @property
    def callback_path(self) -> str:
        return f"/api/channels/{self.name}/callback"

    # ---------------- 入站 ----------------
    def verify_url(self, params: Mapping[str, str]) -> str:
        """平台保存回调配置时的 URL 校验（GET）。默认不支持。"""
        raise ChannelUnavailable(
            t("channel.url_verify_unsupported", name=self.display_name or self.name))

    def parse(self, body: bytes, params: Mapping[str, str]) -> IncomingMessage | None:
        """把原始请求变成 `IncomingMessage`。

        返回 `None` 表示「这条消息直接忽略」（例如别的应用的消息 / 重复消息）。
        验签失败请抛 `ChannelAuthError`，报文不合法请抛 `ChannelBadRequest`。
        """
        raise NotImplementedError

    # ---------------- 出站 ----------------
    def send(self, external_user: str, text: str) -> None:
        """主动发一条文本消息给用户（失败请抛异常，由 `send_safe` 兜住）。"""
        raise NotImplementedError

    def send_safe(self, external_user: str, text: str) -> None:
        """发送失败只记日志，不影响其它消息的处理。"""
        try:
            self.send(external_user, text)
        except Exception as e:      # noqa: BLE001 - 后台任务必须兜住所有异常
            log.warning("%s 消息推送失败（%s）：%s", self.display_name or self.name, external_user, e)

    # ---------------- 语义转换 ----------------
    def resolve_user_id(self, external_user: str) -> str:
        """平台用户 -> 本系统 user_id。默认每个平台用户一个独立空间。"""
        uid = (external_user or "").strip()
        if not uid:
            raise ChannelBadRequest(t("channel.no_sender"))
        return f"{self.name}:{uid}"

    def format_reply(self, result: Mapping[str, Any]) -> str:
        """把 `agent.chat()` 的结构化结果排版成这个平台能显示的样子。"""
        return strip_markdown(str(result.get("message") or ""))
