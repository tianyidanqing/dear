# -*- coding: utf-8 -*-
"""渠道注册表：自动发现 `channels/` 下的子包，按需实例化。

为什么用「自动发现」而不是手写清单：
  手写清单（`CHANNELS = [wecom]`）是这种项目最常见的退化点——新加一个平台，
  代码全写好了，就是忘了往清单里加一行，然后回调 404，排查半天。
  这里用 `pkgutil` 扫描子包，**放进目录就生效**。

外部插件（不在本仓库里的渠道）有两条路：
  - `SCHEDULE_CHANNEL_PLUGINS=my_pkg.feishu,my_pkg.dingtalk`，模块里导出 `CHANNEL`；
  - 打包发行时声明 entry point：`[project.entry-points."schedule_agent.channels"]`。
"""
from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path

from schedule_agent.channels.base import Channel
from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)

_REGISTRY: dict[str, type[Channel]] = {}
_DISCOVERED = False


def discover(refresh: bool = False) -> dict[str, type[Channel]]:
    """扫描并返回 {渠道名: 渠道类}。结果缓存，`refresh=True` 可强制重扫。"""
    global _DISCOVERED
    if _DISCOVERED and not refresh:
        return dict(_REGISTRY)

    base = Path(__file__).parent
    for info in pkgutil.iter_modules([str(base)]):
        # 只认子包；`_template` 之类以下划线开头的是脚手架，不参与发现
        if not info.ispkg or info.name.startswith("_"):
            continue
        _load_module(f"{__package__}.{info.name}")

    for mod_name in (os.environ.get("SCHEDULE_CHANNEL_PLUGINS") or "").split(","):
        mod_name = mod_name.strip()
        if mod_name:
            _load_module(mod_name)

    _load_entry_points()

    _DISCOVERED = True
    return dict(_REGISTRY)


def _load_entry_points():
    """加载以 entry point 形式声明的第三方渠道（打包发行时用）。"""
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = "schedule_agent.channels"
        found = (eps.select(group=group) if hasattr(eps, "select")
                 else eps.get(group, []))
    except Exception:               # noqa: BLE001 - 老版本 Python / 环境异常都不该影响启动
        return
    for ep in found:
        try:
            register(ep.load())
        except Exception as e:      # noqa: BLE001
            log.error("加载插件入口 %s 失败（已跳过）：%s", getattr(ep, "name", ep), e)


def _load_module(mod_name: str):
    """导入模块并登记它导出的 `CHANNEL`。导入失败只记日志，不影响其它渠道。"""
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:          # noqa: BLE001 - 一个渠道坏了不能拖垮整个服务
        log.error("加载渠道模块 %s 失败（已跳过）：%s", mod_name, e)
        return
    cls = getattr(mod, "CHANNEL", None)
    if cls is None:
        log.warning("模块 %s 没有导出 CHANNEL，已跳过", mod_name)
        return
    register(cls)


def register(cls: type[Channel]) -> type[Channel]:
    """手动登记一个渠道类（也可作为装饰器用）。"""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} 缺少 name 属性")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        log.warning("渠道 %s 被重复注册，后注册的生效（%s）", cls.name, cls.__name__)
    _REGISTRY[cls.name] = cls
    return cls


def get_class(name: str) -> type[Channel] | None:
    return discover().get((name or "").strip().lower())


def names() -> list[str]:
    return sorted(discover())


def load_channels(settings=None) -> dict[str, Channel]:
    """按当前配置实例化**已启用**的渠道。

    - 完全没配置：静默跳过（这是绝大多数部署的常态）；
    - 配了但不完整：打一条 warning 说明缺哪几项（比启动后 503 好排查）；
    - 配置有误（如 AESKey 长度不对）：记 error 并跳过，不阻止服务启动。
    """
    if settings is None:
        from schedule_agent.config import get_settings
        settings = get_settings()

    result: dict[str, Channel] = {}
    for name, cls in discover().items():
        raw = settings.channel_config(name)
        if not raw:
            continue
        try:
            channel = cls.from_config(raw)
        except Exception as e:      # noqa: BLE001
            log.error("渠道 %s 配置有误，已跳过：%s", name, e)
            continue
        if not channel.enabled:
            log.warning("渠道 %s（%s）配置不完整，缺少：%s",
                        name, channel.display_name or name,
                        "、".join(channel.missing()))
            continue
        result[name] = channel
    return result
