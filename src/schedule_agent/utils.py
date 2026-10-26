# -*- coding: utf-8 -*-
"""通用小工具（跨层复用，故不与某一层绑定）。"""
from datetime import datetime

# 接口层常见的时间字符串写法
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
               "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M")


def parse_dt(s):
    """把多种常见时间字符串解析为 datetime；无法解析或不传则返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
