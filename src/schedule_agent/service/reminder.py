"""提醒引擎：后台线程 / 异步任务定时扫描。

职责：
  1) 临近开始/截止的任务触发提醒。
  2) 每天固定时刻发送「当日日程速览」（daily summary）。
  3) 自动为当天展开周期性任务的实例。

CLI 单进程使用线程版（start/stop/_loop）；FastAPI 微服务使用异步版
（run_loop，由 lifespan 以 asyncio.create_task 拉起）。
"""
import asyncio
import threading
import time
from datetime import datetime, timedelta

from schedule_agent.domain import planner, recurrence
from schedule_agent.i18n import t


class ReminderEngine:
    """提醒引擎。

    多用户：notifier 签名统一为 `notifier(user_id, title, message)`，
    每次检查会遍历库中出现过的所有 user_id，逐用户展开实例、发速览、发提醒，
    因此每个用户只会收到属于自己的日程提醒。
    """

    def __init__(self, store, notifier, lead_minutes=10, interval=20,
                 daily_summary_time="08:00"):
        self.store = store
        self.notifier = notifier          # 签名：(user_id, title, message)
        self.lead_minutes = lead_minutes
        self.interval = interval
        self.daily_summary_time = daily_summary_time
        self._stop = False
        self._thread = None
        self._last_summary = {}           # {user_id: date} 该用户今天是否已发过速览

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[提醒引擎] 已启动：每 {self.interval}s 检查，提前 {self.lead_minutes} 分钟提醒，"
              f"每日 {self.daily_summary_time} 发送日程速览。")

    def stop(self):
        self._stop = True
        print("[提醒引擎] 已停止。")

    def _loop(self):
        while not self._stop:
            try:
                self._check()
            except Exception as e:
                print(f"[提醒引擎] 检查出错: {e}")
            time.sleep(self.interval)

    async def run_loop(self):
        """异步版主循环：供 FastAPI 的 lifespan 用 asyncio.create_task 启动。

        用 asyncio.sleep 让出事件循环；把阻塞的 _check（含本地 SQLite 读写）
        卸载到线程池，避免阻塞其他并发请求。
        """
        while not self._stop:
            try:
                await asyncio.to_thread(self._check)
            except Exception as e:  # noqa
                print(f"[提醒引擎] 检查出错: {e}")
            await asyncio.sleep(self.interval)

    def _check(self):
        now = datetime.now()
        # 逐用户扫描：保证「只处理自己的数据」且提醒只推给自己
        for uid in self.store.distinct_user_ids():
            try:
                # 先为该用户当天展开周期实例，保证提醒/速览包含它们
                recurrence.ensure_instances(self.store, now.date(), uid)

                # ---- 每日日程速览 ----
                self._maybe_daily_summary_for(uid, now)

                # ---- 临近提醒 ----
                for task in self.store.pending(uid):
                    target = task.scheduled_start or task.start or task.due
                    if target is None or task.reminded:
                        continue
                    delta = target - now
                    if timedelta(0) <= delta <= timedelta(minutes=self.lead_minutes):
                        when = target.strftime("%H:%M")
                        self.notifier(
                            uid,
                            t("reminder.upcoming_title", title=task.title),
                            t("reminder.upcoming_body", time=when,
                              minutes=task.duration_min,
                              priority=task.priority.label()),
                        )
                        self.store.mark_reminded(task.id, uid)
            except Exception as e:  # noqa: 单个用户出错不影响其他用户
                print(f"[提醒引擎] 用户 {uid} 检查出错: {e}")

    def _maybe_daily_summary_for(self, user_id, now):
        if not self.daily_summary_time:
            return
        try:
            hh, mm = (int(x) for x in self.daily_summary_time.split(":"))
        except (ValueError, AttributeError):
            return
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= target and self._last_summary.get(user_id) != now.date():
            self._last_summary[user_id] = now.date()
            scheduled = planner.plan_day(self.store.all(user_id))
            self.store.save_schedule(scheduled)
            self.notifier(user_id, t("reminder.summary_title"),
                          planner.summarize(scheduled))

    def send_summary_now(self, user_id=None):
        """手动触发一次当日速览（不经过 notifier，仅打印/返回）。"""
        now = datetime.now()
        recurrence.ensure_instances(self.store, now.date(), user_id)
        scheduled = planner.plan_day(self.store.all(user_id))
        self.store.save_schedule(scheduled)
        print(planner.summarize(scheduled))
