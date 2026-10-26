# -*- coding: utf-8 -*-
"""提醒事件总线：缓冲提醒 / 速览事件，供前端轮询或 WebSocket 推送。

事件带 `user_id`，查询时只返回属于该用户的事件 —— 多用户隔离的最后一环。
（内存缓冲，重启即清空；如需持久化可替换为 Redis 等实现。）
"""
from __future__ import annotations

import threading
import time


class EventBus:
    """线程安全的环形事件缓冲。"""

    def __init__(self, maxlen: int = 200):
        self.maxlen = maxlen
        self._events: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()

    # ---- 写入 ----
    def push(self, etype: str, title: str, message: str, user_id: str = "") -> dict:
        with self._lock:
            self._seq += 1
            ev = {
                "id": self._seq,
                "ts": time.time(),
                "type": etype,
                "title": title,
                "message": message,
                "user_id": user_id,
            }
            self._events.append(ev)
            if len(self._events) > self.maxlen:
                self._events = self._events[-self.maxlen:]
            return ev

    # ---- 读取 ----
    def since(self, since_id: int = 0, user_id: str | None = None) -> list[dict]:
        with self._lock:
            return [e for e in self._events
                    if e["id"] > since_id
                    and (user_id is None or e.get("user_id") == user_id)]

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    # ---- 适配 ReminderEngine / ScheduleAgent 的通知器签名 ----
    def notifier(self, user_id: str, title: str, message: str):
        """通知回调：签名与 `notifier(user_id, title, message)` 一致。"""
        self.push("reminder", title, message, user_id)
