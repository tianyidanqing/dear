# -*- coding: utf-8 -*-
"""国际化（i18n）包。

用法：

    from schedule_agent.i18n import t, use_locale

    t("agent.added_today", title="开会")      # 按当前语言返回
    with use_locale("en-US"):                 # 临时切到英文
        print(t("planner.header"))

新增一门语言：在 `locales/` 下放一个 `<locale>.json`（照 `zh-CN.json` 的 key
写全），无需改任何代码，`available_locales()` 会自动发现它。
"""
from schedule_agent.i18n.core import (  # noqa: F401
    DATE_FORMATS,
    DEFAULT_LOCALE,
    LOCALES_DIR,
    available_locales,
    catalog,
    get_locale,
    has,
    keys,
    locale_name,
    missing_keys,
    normalize_locale,
    parse_accept_language,
    reload,
    reset_locale,
    resolve_locale,
    set_locale,
    t,
    use_locale,
)

__all__ = [
    "DATE_FORMATS",
    "DEFAULT_LOCALE",
    "LOCALES_DIR",
    "available_locales",
    "catalog",
    "get_locale",
    "has",
    "keys",
    "locale_name",
    "missing_keys",
    "normalize_locale",
    "parse_accept_language",
    "reload",
    "reset_locale",
    "resolve_locale",
    "set_locale",
    "t",
    "use_locale",
]
