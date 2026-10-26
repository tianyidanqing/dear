# -*- coding: utf-8 -*-
"""日志配置：控制台 + 按大小轮转的文件日志。

虚拟机 / 容器部署时通常没有交互终端，日志要落盘便于排查，因此默认同时输出到
`<log_dir>/schedule-agent.log`；轮转避免把磁盘写满。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str = "", level: str = "INFO",
                  max_bytes: int = 5 * 1024 * 1024, backups: int = 3) -> logging.Logger:
    """初始化根日志器。可重复调用（幂等）。"""
    global _CONFIGURED
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    if not _CONFIGURED:
        fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        if log_dir:
            from pathlib import Path
            p = Path(log_dir).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                p / "schedule-agent.log", maxBytes=max_bytes,
                backupCount=backups, encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)

        _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
