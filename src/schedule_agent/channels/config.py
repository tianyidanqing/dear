# -*- coding: utf-8 -*-
"""渠道配置的收集与解析（**一个渠道的配置只写在那个渠道的包里**）。

以前每个渠道都要往 `Settings` 加 5~6 个字段、往 `_ENV_MAP` 加同样多的映射，
接第二个平台就得再改一遍 core 配置。现在改成约定式：

  配置文件（推荐，可读性好）
    "channels": {
      "wecom": {"corp_id": "ww...", "agent_id": "1000002", ...}
    }

  环境变量（通用写法，容器 / 虚拟机首选）
    SCHEDULE_CHANNEL_WECOM_CORP_ID=ww...
    SCHEDULE_CHANNEL_WECOM_AGENT_ID=1000002

  旧写法（仍然兼容，启动时会提示迁移）
    wecom_corp_id / SCHEDULE_WECOM_CORP_ID

优先级（低 → 高）：
  旧式文件键 < `channels.<name>` 嵌套 < 旧环境变量 < 新环境变量
与全局配置一致：**环境变量永远压过文件**。
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from schedule_agent.logging_conf import get_logger

log = get_logger(__name__)

#: 通用环境变量前缀：SCHEDULE_CHANNEL_<CHANNEL_NAME>_<FIELD>
ENV_PREFIX = "SCHEDULE_CHANNEL_"


def collect(raw_file: Mapping[str, Any] | None,
            classes: Mapping[str, type] | None = None) -> dict[str, dict]:
    """汇总所有渠道的配置，返回 `{渠道名: 原始配置 dict}`。

    只返回「至少填了一个非空值」的渠道——大部分部署一个第三方都不接，
    不该因此产生一堆「配置不完整」的告警。
    """
    raw_file = dict(raw_file or {})
    if classes is None:
        from schedule_agent.channels.registry import discover
        classes = discover()

    nested_all = raw_file.get("channels")
    nested_all = nested_all if isinstance(nested_all, dict) else {}

    result: dict[str, dict] = {}
    for name, cls in classes.items():
        spec = getattr(cls, "CONFIG", None)
        if spec is None:
            continue
        cfg: dict[str, Any] = {}

        # 1) 旧式扁平文件键：wecom_corp_id -> corp_id
        legacy_prefix = getattr(spec, "LEGACY_PREFIX", "") or ""
        if legacy_prefix:
            hits = {k[len(legacy_prefix):]: v for k, v in raw_file.items()
                    if isinstance(k, str) and k.startswith(legacy_prefix)
                    and len(k) > len(legacy_prefix) and not k.startswith("_")}
            if hits:
                cfg.update(hits)
                log.warning("检测到旧式配置项（%s*），仍然兼容；"
                            "建议迁移到配置文件里的 channels.%s，可读性更好。",
                            legacy_prefix, name)

        # 2) 新的嵌套写法（下划线开头的键是给人看的注释，不参与判断）
        nested = nested_all.get(name)
        if isinstance(nested, dict):
            cfg.update({k: v for k, v in nested.items()
                        if not str(k).startswith("_")})

        # 3) 旧式环境变量：SCHEDULE_WECOM_CORP_ID -> corp_id
        legacy_env = getattr(spec, "LEGACY_ENV_PREFIX", "") or ""
        if legacy_env:
            for key, val in os.environ.items():
                if key.startswith(legacy_env) and len(key) > len(legacy_env):
                    cfg[key[len(legacy_env):].lower()] = val

        # 4) 通用环境变量：SCHEDULE_CHANNEL_WECOM_CORP_ID -> corp_id
        generic = f"{ENV_PREFIX}{name.upper()}_"
        for key, val in os.environ.items():
            if key.startswith(generic) and len(key) > len(generic):
                cfg[key[len(generic):].lower()] = val

        if _has_value(cfg):
            result[name] = cfg
    return result


def _has_value(cfg: Mapping[str, Any]) -> bool:
    """至少有一个非空值才算「配过」，否则视作未启用。"""
    for v in cfg.values():
        if isinstance(v, dict):
            if v:
                return True
        elif str(v or "").strip():
            return True
    return False
