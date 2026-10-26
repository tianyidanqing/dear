# -*- coding: utf-8 -*-
"""个人日程管理 AI Agent。

分层结构（自底向上）：
    domain/   纯领域逻辑：任务模型、排期算法、周期规则、规则式兜底解析
    storage/  持久化：SQLite 任务库（按 user_id 隔离）与用户/Token 库
    service/  业务服务：ScheduleAgent（按用户绑定）、提醒引擎、通知器
    llm/      大模型接入：NLU、流式回复，以及集中管理的提示词
    api/      对外接口：FastAPI 应用、鉴权依赖、路由、事件总线
    ui/       前端：Gradio 聊天窗口
    cli/      命令行入口

注意：本包**不在 __init__ 里导入 fastapi / gradio 等重依赖**，
以便只跑命令行时无需安装 Web 相关依赖（按需可选安装，见 pyproject 的 extras）。
"""
__version__ = "4.1.0"

__all__ = ["__version__"]
