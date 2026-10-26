#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发用一键启动：先后台拉起 FastAPI 微服务，再启动 Gradio 聊天窗口。

用法：
  python scripts/run_dev.py                     # 默认 API=:8000, UI=:7860
  python scripts/run_dev.py --api-port 9000 --ui-port 8080
  python scripts/run_dev.py --no-ui             # 只启动后端微服务

注意：这是**开发便利脚本**。生产 / 虚拟机部署请分开运行（见 deploy/）：
  终端A:  schedule-agent-api     （或 uvicorn schedule_agent.api.app:app）
  终端B:  schedule-agent-ui
这样前后端可独立重启、独立扩容。
"""
import argparse
import sys
import threading
import time
from pathlib import Path

# 让脚本可以直接以源码方式运行（无需先 pip install）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from schedule_agent.config import get_settings  # noqa: E402
from schedule_agent.api.app import serve  # noqa: E402


def wait_for_api(base_url, timeout=20):
    """等 API 健康检查通过。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(base_url + "/api/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    settings = get_settings()
    ap = argparse.ArgumentParser(description="启动日程管理 Agent（后端+前端）")
    ap.add_argument("--api-port", type=int, default=settings.api_port)
    ap.add_argument("--ui-port", type=int, default=settings.ui_port)
    ap.add_argument("--no-ui", action="store_true", help="只启动后端微服务")
    ap.add_argument("--share", action="store_true", help="Gradio 生成公网分享链接")
    args = ap.parse_args()

    # 1) 后台启动 FastAPI 微服务
    t = threading.Thread(
        target=serve, kwargs={"port": args.api_port}, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{args.api_port}"
    print(f"[run] 正在等待后端微服务就绪（{base}）…")
    if not wait_for_api(base):
        print("[run] ⚠️ 后端未在预期时间内就绪，请检查控制台输出。")
    else:
        print(f"[run] 后端已就绪：{base}")

    # 2) 启动 Gradio 前端
    if args.no_ui:
        print(f"[run] 仅后端模式，访问 {base}/docs 查看 API 文档。Ctrl+C 退出。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[run] 已退出。")
        return

    from schedule_agent.ui.gradio_app import launch
    print(f"[run] 启动 Gradio 聊天窗口（将连接 {base}）…")
    launch(port=args.ui_port, share=args.share)


if __name__ == "__main__":
    main()
