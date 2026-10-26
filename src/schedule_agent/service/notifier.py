"""通知模块：控制台提示 + 可选的桌面 Toast（若安装了 plyer）。"""
from schedule_agent.i18n import t


def notify(user_id, title, message, use_toast=True):
    """发送一条提醒。控制台始终输出；桌面通知在可用时附带。

    签名统一为 (user_id, title, message)，与 reminder / core / api 的通知回调一致
    （多用户场景下由 user_id 决定这条提醒归属谁）。
    """
    banner = "-" * 50
    who = f"[{user_id}] " if user_id and user_id != "default" else ""
    print(f"\n{banner}")
    print(t("notify.line", who=who, title=title))
    print(f"         {message}")
    print(f"{banner}\n")

    if use_toast:
        try:
            from plyer import notification
            notification.notify(title=title, message=message, timeout=10)
        except Exception:
            # plyer 未安装 / 不支持当前平台，静默忽略
            pass
