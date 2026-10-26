# 日程管理 AI Agent

一个用 Python 实现的日程管理智能体：**自然语言增删改待办 → 动态重排当日日程 → 到点提醒**。
现已整理为**分层包结构 + 可打包安装 + 配置/数据外置**，便于部署到虚拟机或容器。

## 功能

1. **随时添加待办**：`明天下午3点开会 1小时 高`、`周五 上午10点 健身 45分钟 低`。
2. **到点提醒**：后台引擎定时扫描，任务开始前（默认 10 分钟）提醒，推送到前端与日志。
3. **动态重排**：新增当日任务或标记完成时，对当天**未完成**事项重新排期（已完成/已取消自动剔除）。
4. **每日定时速览**：每天固定时刻（默认 08:00）自动推送当日排期。
5. **周期性任务**：`每天` / `每周X` / `每隔N天` / `每月D号` 自动在匹配日生成实例。
6. **大模型自然语言理解**：**意图只由 LLM 判断**；模型不可用时默认**明确报错**，不会用关键词规则去猜（规则兜底需显式开启，默认关闭）。
7. **流式输出**：SSE 逐块推送，实现打字机效果。
8. **提示词集中管理**：所有 system prompt 在 `src/schedule_agent/llm/prompts.py`。
9. **异步高并发**：`httpx` 真异步调 LLM，SQLite/排期用 `asyncio.to_thread` 卸载，提醒引擎为 asyncio 后台任务。
10. **多用户隔离**：任务带 `user_id`，SQL 全部按用户过滤，单条操作带 `AND user_id=?`；每个用户独立 Agent（对话历史隔离），提醒只推给本人。
11. **API 鉴权**：除健康检查与 `/api/auth/*` 外全部端点需 Bearer Token；SSE/WS 支持 `?token=`。
12. **第三方渠道可插拔**：企业微信已内置；渠道配置与协议代码都在 `channels/<平台>/` 子包里，
    新增钉钉/飞书/Telegram 只需新建一个子包，**core 一行不用改**。
13. **国际化（i18n）**：中英双语，用户可见文案（回复、排期摘要、界面、通知、CLI、渠道回复、
    错误提示）全部走词典；语言按请求协商，**丢一个 `locales/<locale>.json` 就是一门新语言**。

## 目录结构

```
schedule_agent/
├── src/schedule_agent/          # 主包（可 pip 安装）
│   ├── config.py                # 集中配置：默认 <- 配置文件 <- 环境变量
│   ├── logging_conf.py          # 日志：控制台 + 轮转文件
│   ├── utils.py                 # 通用小工具（时间解析等）
│   ├── domain/                  # 领域层（纯逻辑，无 Web/DB/LLM 依赖）
│   │   ├── models.py            #   任务模型与优先级
│   │   ├── planner.py           #   EDF 排期算法
│   │   ├── recurrence.py        #   周期规则展开
│   │   └── nlp.py               #   规则式兜底解析（中英双语）
│   ├── i18n/                    # ★ 国际化：t() / 语言协商 / 词典回退
│   │   ├── core.py              #   contextvars 语言上下文、插值、复数、回退链
│   │   └── locales/             #   zh-CN.json / en-US.json（放新 JSON 即新语言）
│   ├── storage/                 # 存储层
│   │   ├── db.py                #   任务库（按 user_id 隔离）+ Store 单例
│   │   └── auth.py              #   用户 / Token 库（auth.db）
│   ├── service/                 # 服务层
│   │   ├── agent.py             #   ScheduleAgent（绑定 user_id）
│   │   ├── reminder.py          #   提醒引擎（逐用户扫描）
│   │   ├── registry.py          #   Agent 注册表（共享 store + 单一引擎）
│   │   └── notifier.py          #   通知器
│   ├── llm/                     # 大模型接入
│   │   ├── client.py            #   NLU + 流式回复（同步/异步）
│   │   └── prompts.py           #   提示词集中管理
│   ├── api/                     # 接口层
│   │   ├── app.py               #   应用工厂 create_app() + 启动入口
│   │   ├── deps.py              #   鉴权依赖与共享资源
│   │   ├── events.py            #   事件总线（按用户过滤）
│   │   └── routers/             #   health / auth / chat / tasks / events / admin
│   ├── channels/                # ★ 第三方接入（每个平台一个子包，可插拔）
│   │   ├── base.py              #   Channel 抽象 / 配置基类 / 归一化消息 / 去重器
│   │   ├── registry.py          #   自动发现子包（放进目录即生效，无需改 core）
│   │   ├── config.py            #   渠道配置解析（channels.<name> + 环境变量）
│   │   ├── webhook.py           #   通用回调路由 /api/channels/<name>/callback
│   │   ├── wecom/               #   企业微信：config / crypto / client / messages / channel
│   │   └── _template/           #   新渠道脚手架（复制改名即可）
│   ├── ui/gradio_app.py         # Gradio 聊天窗口（中英可切换）
│   └── cli/main.py              # 命令行 / REPL
├── config/config.example.json   # 配置样例（复制为 config.json 后改）
├── deploy/                      # 部署件：Dockerfile / compose / systemd / nginx
│   └── VM_DEPLOY.md             # ★ 虚拟机部署与运维手册（详细版）
├── scripts/
│   ├── install.sh               # 虚拟机一键安装（含 systemd 注册）
│   └── run_dev.py               # 开发用一键起后端+前端
├── tests/                       # pytest（存储隔离 + 接口鉴权）
└── pyproject.toml               # 打包与依赖（含 console scripts）
```

**分层原则（依赖方向单向，不反向引用）**：
`domain ← storage ← service ← {api, ui, cli}`，`llm` 被 `service` 调用。

## 安装

```bash
cd schedule_agent
python -m venv .venv                      # 本仓库已建好 .venv，可跳过
source .venv/bin/activate                 # Linux/macOS
source .venv/Scripts/activate             # Windows Git Bash
# .venv\Scripts\activate.bat              # Windows CMD

pip install -e ".[all]"      # 全量（API + 前端 + 桌面通知）
# 或按需最小安装
pip install -e ".[api]"      # 只跑后端服务
pip install -e ".[ui]"       # 只要 Gradio 前端
pip install -e ".[dev]"      # 跑测试
```

不激活也可以直接用完整路径，例如 `./.venv/Scripts/python.exe -m schedule_agent`。

> ⚠️ 本机的 `.venv` 是 Windows 环境，**不能拷到 Linux 虚拟机复用**；
> 虚拟机部署请按 `deploy/VM_DEPLOY.md`，脚本会在 VM 上重新建虚拟环境。

安装后得到三个命令：

| 命令 | 作用 |
|------|------|
| `schedule-agent` | 命令行 / REPL（本机直连，不需要 Token，用 `--user` 选用户） |
| `schedule-agent-api` | 启动 FastAPI 微服务（可加 `--host/--port/--reload`） |
| `schedule-agent-ui` | 启动 Gradio 聊天窗口（可加 `--port/--share`） |

未安装也可直接跑源码：`python -m schedule_agent`、`python scripts/run_dev.py`。

## 配置（外置，适合虚拟机）

加载优先级（从高到低）：**真实环境变量 > `.env` > 配置文件 > 内置默认值**。
`Settings` 里的**每个字段**都有对应的 `SCHEDULE_*` 环境变量，所以容器 / 虚拟机可以完全不落配置文件。

- 配置文件查找顺序：`$SCHEDULE_CONFIG` → `./config/config.json` → `./config.json` → `/etc/schedule-agent/config.json`
- 数据目录：`$SCHEDULE_DATA_DIR`（默认 `./data`），`schedule.db` / `auth.db` / 日志都在它下面
- `.env`：项目根目录存在时自动加载，但**不会覆盖已存在的真实环境变量**（`SCHEDULE_ENV_FILE` 可改路径，`SCHEDULE_NO_DOTENV=1` 可关闭）

```bash
cp config/config.example.json config/config.json    # 然后按需修改
cp .env.example .env                                 # 或直接导出环境变量
```

常用环境变量：`SCHEDULE_DATA_DIR`、`SCHEDULE_DB_PATH`、`SCHEDULE_AUTH_DB_PATH`、
`SCHEDULE_LOG_DIR`、`SCHEDULE_CONFIG`、`SCHEDULE_LLM_API_KEY`、`SCHEDULE_LLM_BASE_URL`、
`SCHEDULE_LLM_MODEL`、`SCHEDULE_USE_RULE_FALLBACK`、`SCHEDULE_ADMIN_API_KEY`、
`SCHEDULE_API_HOST`、`SCHEDULE_API_PORT`、`SCHEDULE_UI_PORT`、`SCHEDULE_API_URL`、`SCHEDULE_LOG_LEVEL`、
`SCHEDULE_LANGUAGE`、`SCHEDULE_UI_LANGUAGE`、`SCHEDULE_LOCALES_DIR`。

### 接大模型（以 DeepSeek 为例）

任何 **OpenAI Chat Completions 兼容**的服务都能直连，只改三个字段：

```json
{
  "llm_api_key": "sk-xxxxxxxx",
  "llm_base_url": "https://api.deepseek.com/v1",
  "llm_model": "deepseek-chat"
}
```

改完**重启后端**（`schedule-agent-api`）生效。验证是否真的连上了：

```bash
curl -s localhost:8000/api/health | python -m json.tool
# "llm": {"enabled": true, "model": "deepseek-chat", "last_error": null, ...}
```

`enabled=true` 只说明**读到了 Key**；`last_error` 才是真实调用结果，会直接给出
`HTTP 402 余额不足` / `HTTP 401 Key 无效` / `HTTP 429 触发限流` 这类可读原因。

**模型不可用时的行为由 `use_rule_fallback` 决定（默认 `false`）**：

| 场景 | `use_rule_fallback=false`（默认，推荐） | `use_rule_fallback=true`（离线应急） |
|---|---|---|
| 模型不可用 | 返回 `action="error"`，把真实原因告诉用户，**不建任何任务** | 退化为关键词规则（闭集，可能猜错意图） |

```bash
SCHEDULE_USE_RULE_FALLBACK=1   # 只有确实需要离线应急时才开
```

> 为什么默认关闭：关键词规则是**闭集**，遇到规则没覆盖的说法不会报错，只会把话
> 硬塞进最接近的意图（例如「ai开发设置为高优先级」被当成新增任务）。**静默猜错比明确失败更糟。**

> `config/config.json` 已在 `.gitignore` 中（含真实 Key，别提交）。
> 生产环境更推荐用环境变量 `SCHEDULE_LLM_API_KEY` 注入，避免密钥落盘。

## 快速开始（本地）

```bash
# 方式 A：一条命令起后端 + 前端（开发用）
python scripts/run_dev.py

# 方式 B：分开跑（推荐，也更贴近生产）
schedule-agent-api                       # 终端 A，后端 :8000
schedule-agent-ui                        # 终端 B，前端 :7860
```

浏览器打开 Gradio 地址，界面上是**三个独立页面**：

| 页面 | 用途 |
|------|------|
| 🔐 登录 | 注册 / 登录拿 Token（用户名随意，密码可留空），退出；顶部常驻显示当前身份 |
| 💬 对话 | 自然语言对话，流式打字机输出；后端提醒以气泡插入；下方有常用示例 |
| 📅 日程 | 今日排期表 + 全部任务表 + 按 ID「标记完成 / 删除」；切到本页会自动刷新 |

登录成功后自动跳到「对话」页；顶部状态栏始终显示当前用户，换用户请回登录页退出重登。

命令行直接操作（不经过 HTTP，无需 Token）：

```bash
schedule-agent --user alice add "明天下午3点开会 1小时 高"
schedule-agent --user alice list
schedule-agent --user alice today
```

## 部署到虚拟机

> 📖 **完整手册见 [`deploy/VM_DEPLOY.md`](deploy/VM_DEPLOY.md)**：前提条件、代码上传、
> systemd / Docker / 手动三种部署方式、防火墙与安全组、Nginx + HTTPS、日常运维
> （启停 / 日志 / 备份 / 升级 / 卸载）、环境变量速查、故障排查。此处为速览。

### 方式一：systemd（推荐）

```bash
sudo bash scripts/install.sh            # 加 --with-ui 同时部署前端
sudo systemctl status schedule-agent
journalctl -u schedule-agent -f
```

默认落到：`/opt/schedule-agent`（代码+venv）、`/var/lib/schedule-agent`（数据+日志）、
`/etc/schedule-agent/config.json`（配置）。单元文件见 `deploy/schedule-agent.service`，
已带 `NoNewPrivileges` / `ProtectSystem=strict` 等加固项。

### 方式二：Docker / Compose

```bash
cd deploy && docker compose up -d        # 起 API(:8000) + UI(:7860)
docker compose logs -f api
```

镜像非 root 运行，数据在命名卷 `schedule-data`，带健康检查。

### 方式三：Nginx 反代

`deploy/nginx.conf` 提供了 HTTPS 终止 + 反向代理配置。**SSE 要点是必须关掉
`proxy_buffering`**，否则流式回复会被缓冲成一整块、打字机效果失效；WS 需要 `Upgrade` 头。

生产建议：`uvicorn` 前置 Nginx，或多进程用 `gunicorn -k uvicorn.workers.UvicornWorker`。

详细的命令、目录约定、运维操作（备份 / 升级 / 卸载）与故障排查见
**[deploy/VM_DEPLOY.md](deploy/VM_DEPLOY.md)**。

## API 鉴权（多用户）

除 `/api/health` 与 `/api/auth/*` 外，**所有端点都必须携带 Bearer Token**。

```bash
# 1) 注册拿 Token（已注册则用 /api/auth/login）
curl -X POST :8000/api/auth/register -H "Content-Type: application/json" \
     -d '{"user_id":"alice","password":"可选"}'
# -> {"ok":true,"user_id":"alice","api_key":"sZ3f..."}

# 2) 带 Token 调用
curl -H "Authorization: Bearer <api_key>" :8000/api/tasks

# SSE / WebSocket 支持 ?token=（客户端不便设头时）
curl -N -X POST "http://localhost:8000/api/chat/stream?token=<api_key>" \
     -H "Content-Type: application/json" -d '{"message":"今日日程"}'
# ws://localhost:8000/api/ws?token=<api_key>
```

未带 / 带错 Token 一律 `401`。用户库独立存放于 `auth.db`。

### 管理员 Key（可选）

配置 `admin_api_key` 后，可用它作 Bearer 并以 `X-User-Id` 指定身份：

```bash
curl -H "Authorization: Bearer <admin_api_key>" -H "X-User-Id: alice" :8000/api/tasks
curl -H "Authorization: Bearer <admin_api_key>" :8000/api/admin/users
```

不配（默认空）则管理员能力关闭，`/api/admin/*` 返回 `403`。

### 隔离怎么保证

| 层面 | 做法 |
|------|------|
| 数据 | 任务带 `user_id`；`all/get/update/delete/templates` 按用户过滤，单条操作带 `AND user_id=?` |
| 服务 | Token → `user_id` → 该用户专属 Agent（对话历史独立） |
| 提醒 | 引擎遍历 `distinct_user_ids()` 逐用户检查，事件带 `user_id`，`/api/events` 与 WS 只推本人 |
| 前端 | Gradio 会话各自持 Token，切换用户清空本地聊天记录 |

> 命令行 `schedule-agent --user alice` 本机直连、**不走 HTTP 故无需 Token**。

### 主要接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|:----:|------|
| GET | `/api/health` | 否 | 健康检查 |
| POST | `/api/auth/register` | 否 | 注册并返回 `api_key`（已存在 409） |
| POST | `/api/auth/login` | 否 | 登录并返回 `api_key` |
| GET | `/api/me` | 是 | 当前 Token 对应身份 |
| POST | `/api/chat` | 是 | 聊天式入口（非流式） |
| POST | `/api/chat/stream` | 是 | 流式聊天（SSE） |
| GET/POST | `/api/tasks` | 是 | 列出 / 添加任务 |
| GET | `/api/tasks/today` | 是 | 今日排期 |
| POST | `/api/tasks/{id}/done` | 是 | 标记完成（模板=停止周期） |
| DELETE | `/api/tasks/{id}` | 是 | 删除任务 |
| GET | `/api/templates` | 是 | 周期模板 |
| GET | `/api/summary` | 是 | 当日速览 |
| GET | `/api/events?since=ID` | 是 | 属于我的提醒事件 |
| WS | `/api/ws` | 是 | 实时推送我的提醒 |
| GET | `/api/admin/users` | 管理员 | 列出所有注册用户 |
| GET/POST | `/api/channels/{name}/callback` | 渠道签名 | 第三方渠道统一回调（`wecom` / 未来新增平台） |
| GET | `/api/wecom/callback` | 渠道签名 | 企业微信 URL 验证（旧路径，仍可用） |
| POST | `/api/wecom/callback` | 渠道签名 | 企业微信消息回调（旧路径，仍可用） |

## 接入企业微信（在微信聊天窗口里直接对话）

配好之后，企业成员在**企业微信 → 工作台 → 你的应用**里发一句「明天10点开会」，
助手就像好友一样回复，到点还会主动推送提醒。

### 1. 前置条件

- 一个**公网可达**的地址，企业微信只接受 `http://` / `https://` + 域名（端口 80/443）
- 服务已部署并能被外网访问（本地开发可用 frp / ngrok 做内网穿透）

### 2. 企业微信后台（5 步）

1. <https://work.weixin.qq.com> → **我的企业 → 企业信息**，记下 **企业ID（CorpID）**
2. **应用管理 → 应用 → 自建 → 创建应用**，记下 **AgentId** 和 **Secret**
3. 进入该应用 → **接收消息 → 设置 API 接收**
4. 填写：
   - **URL**：`https://你的域名/api/channels/wecom/callback`
     （旧地址 `https://你的域名/api/wecom/callback` 仍然可用）
   - **Token**：随便填一串（如 `schedule2026`）
   - **EncodingAESKey**：点「随机获取」，得到 43 个字符
5. 点保存 —— 企业微信会**立即**发 GET 请求校验，所以服务必须此时已在运行

> 顺序很重要：先起服务再点保存，否则会提示「回调地址校验失败」。

### 3. 服务端配置

```bash
pip install -e ".[wecom]"        # 装加密库（cryptography）
```

```json
{
  "channels": {
    "wecom": {
      "corp_id": "ww1234567890abcdef",
      "agent_id": "1000002",
      "secret": "应用详情里的 Secret",
      "token": "schedule2026",
      "aes_key": "43位EncodingAESKey",
      "bind_map": {"zhangsan": "jiyu"}
    }
  }
}
```

对应环境变量：`SCHEDULE_CHANNEL_WECOM_CORP_ID` / `_AGENT_ID` / `_SECRET` / `_TOKEN` / `_AES_KEY` / `_BIND_MAP`。

> **旧写法仍兼容**（升级不会断）：扁平的 `wecom_corp_id` / `SCHEDULE_WECOM_CORP_ID`
> 都能被识别，启动时会提示迁移到 `channels.wecom`。
> 优先级：旧文件键 < `channels.wecom` < 旧环境变量 < 新环境变量。

五项齐全才会启用，启动日志会打印「企业微信 接入已启用（wecom），回调地址：…」。

### 4. 验证

```bash
curl -s localhost:8000/api/health          # 服务正常
# 后台点「保存」不再报错，即说明 URL 验证通过
```

然后在企业微信里找到该应用，发一句 `明天上午10点开会`。

### 5. 身份与数据

- 默认每个企微用户独占一个空间：`wecom:<UserID>`（互不可见）
- 想让某人共用网页端已有数据，在 `channels.wecom.bind_map` 里绑定：`{"zhangsan": "jiyu"}`

### 6. 已知限制

| 限制 | 原因 / 处理 |
|---|---|
| 只支持文字消息 | 图片/语音会提示改用文字 |
| 不是流式输出 | 企微不支持 SSE；结果一次性推送 |
| 回复延后 1–3 秒 | 企微要求 5 秒内响应，因此先回空串、业务跑完再主动推送 |
| 回复最长 2048 字节 | 超出按字节截断（中文一字 3 字节） |
| 表格排版简化 | 纯文本环境，排期压成「15:00-15:30 开会」一行一条 |

> 回调端点**不走 Bearer 鉴权**（微信服务器无法携带 Token），安全性由 `msg_signature`
> 校验保证：签名不符一律 403，且不会解析任何内容。

## 新增一个第三方渠道（钉钉 / 飞书 / Telegram …）

第三方接入是一个**可插拔的包**：每个平台一个子包，配置、协议、加解密都在包内，
core（`app.py` / `config.py` / 路由）一行不用改。

### 四步

```bash
cd src/schedule_agent/channels
cp -r _template dingtalk          # 目录名不以 _ 开头 => 自动被发现
```

1. **改 `dingtalk/config.py`**：`NAME` / `REQUIRED` / 字段。配置读取由框架统一处理。
2. **改 `dingtalk/channel.py`**：只实现 `parse()` 和 `send()`（其余钩子有默认实现）。
3. **`__init__.py` 里 `from .channel import CHANNEL`**。
4. **写配置**即可启用：

```json
{"channels": {"dingtalk": {"app_key": "...", "app_secret": "..."}}}
```

环境变量写法：`SCHEDULE_CHANNEL_DINGTALK_APP_KEY=...`（大写 + 下划线）。

### 渠道要实现什么

| 钩子 | 必须 | 职责 |
|---|---|---|
| `parse(body, params)` | ✅ | 验签 / 解密 / 解析，返回 `IncomingMessage`；想忽略就 `return None` |
| `send(user, text)` | ✅ | 调平台 API 发消息 |
| `verify_url(params)` | 可选 | 平台保存回调时的 GET 校验 |
| `format_reply(result)` | 可选 | 排版（默认去 Markdown） |
| `resolve_user_id(uid)` | 可选 | 用户映射（默认 `<渠道名>:<uid>` 独立空间） |

### 框架已经替你做掉的

- 挂载 `GET/POST /api/channels/<name>/callback`，并自动挂上渠道声明的旧路径别名；
- 验签失败 → 403（不执行业务）、报文坏了 → 400、未配置 → 503 且**说明缺哪几项**；
- 后台执行 + 异常兜底：平台 5 秒超时不会导致重试建重复任务；
- `MsgId` 去重（`self.deduper.first_time()`）；
- 多用户隔离、排期、提醒——全部复用现有 `agent.chat()`，不用重写业务。

### 外挂渠道（不改本仓库）

已有独立包？设置 `SCHEDULE_CHANNEL_PLUGINS=my_pkg.feishu,my_pkg.dingtalk`（模块里导出
`CHANNEL`），或在包元数据里声明 `[project.entry-points."schedule_agent.channels"]`。

## 国际化（i18n）

内置 **中文（zh-CN）** 与 **英文（en-US）**。用户能看到的一切都走词典：
聊天回复、排期摘要、提醒、登录/鉴权错误、命令行输出、Gradio 界面、渠道回复。

### 怎么指定语言

| 方式 | 写法 | 作用范围 |
|---|---|---|
| 配置文件 | `"language": "en-US"` | 全局默认值 |
| 环境变量 | `SCHEDULE_LANGUAGE=en-US` | 全局默认值（优先级高于配置文件） |
| 请求头 | `Accept-Language: en-US` | **单个请求**，多用户并发互不影响 |
| 查询参数 | `?lang=en-US` | 单个请求，优先于请求头（便于分享固定语言的链接） |
| 前端 | 顶部「语言」下拉 / `SCHEDULE_UI_LANGUAGE` | 界面 + 发往后端的请求 |
| 命令行 | 跟随 `SCHEDULE_LANGUAGE` | CLI 全部输出 |

HTTP 响应会带 `Content-Language` 头，方便网关/客户端确认实际使用的语言：

```bash
curl -s -H "Accept-Language: en-US" http://127.0.0.1:8000/api/tasks?token=$TOK
# {"message": "3 tasks in total.", ...}
# Content-Language: en-US
```

### 新增一门语言

**不用改代码**，三步：

```bash
cd src/schedule_agent/i18n/locales
cp en-US.json ja-JP.json      # 1. 复制一份
# 2. 翻译所有 value（key 不能改；删了也行，缺的会自动回退中文）
```

```bash
# 3. 重启后自动出现在可用列表里
curl -s http://127.0.0.1:8000/api/health | jq .llm.locales
# ["zh-CN", "en-US", "ja-JP"]
```

`SCHEDULE_LOCALES_DIR` 可以指向一个**只放增量**的目录（例如只覆盖几十条文案），
内置词典仍然作为兜底，不会被整体顶掉。

### 只改文案、不改代码

所有文案就是 `locales/*.json` 里的键值对，直接编辑即可，重启生效。
词典缺 key 时**回退到中文**（而不是显示成 `agent.xxx`），并打印一次告警。

### 哪些东西不翻译

- **代码注释、docstring、日志**：面向开发者，保持中文/英文混排可读性更好；
- **规则解析关键词**：那是「输入识别」而不是「输出文案」——中英文关键词**同时启用**，
  所以中英混说（「明天 review 一下 PR 30分钟」）也能被兜底规则解析；
- **大模型提示词**：不是不翻译，而是**按语言提供两份**（`prompts.py`），
  否则英文输入会被中文 prompt 约束，意图判断会变形。

### 已知限制

- Gradio 的 **Tab 标题**无法在运行时更新，切换语言后这部分需刷新页面（按钮、输入框、
  表格列头、提示文字会立即变）；对话内容不受影响，因为语言是按请求决定的。
- 前端语言是**进程级**的（整个 UI 实例一种语言），多用户各自语言请用 API 的
  `Accept-Language`。
- **OpenAPI 元数据**（`/docs` 与 `/openapi.json` 的标题、描述、路由摘要）在建应用时
  求值一次，跟随 `SCHEDULE_LANGUAGE`；它面向开发者，不随请求的 `Accept-Language`
  变化。文案本身仍在词典里（`api.title` / `api.description` / `api.op_*`），改词典即生效。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

覆盖：存储层多用户隔离（含跨用户读写拦截、周期实例归属、旧库迁移）、
接口鉴权（401 拦截、注册/登录/409、伪造 Token）、数据隔离、SSE `?token=`、
事件按用户过滤、管理员 Key、时间语义（开始 vs 截止）、闲聊兜底守卫、
修改已有安排（modify：改时间/改时长/语义继承/找不到任务时追问）、
改优先级（priority）、意图只由模型判断（失败报错不建任务）、配置优先级、
渠道框架（自动发现 / 配置四级优先级 / 旧配置兼容 / 新增渠道零改 core）、
企业微信（加解密往返 / 签名校验 / 去重 / 回调链路）、
国际化（中英词典 key 完全对齐 / 插值与复数 / 缺 key 回退 / 语言归一与
Accept-Language 协商 / 线程上下文隔离 / 同一数据双语输出 / 英文输入可被规则解析 /
HTTP 层按请求切换语言 / 放一个 JSON 即新增语言 / OpenAPI 与 CLI 帮助文案
也来自词典且随配置语言变化）。

## 排期算法与数据

采用 **EDF（最早截止时间优先）**：先放固定开始时间的硬约束（会议），再按截止时间把柔性
任务塞进最早空隙，放不下则顺延并标记风险；已完成/已取消不参与排期。

**时间语义（重要）**：用户说出明确时刻时，默认理解为「**这个点开始做**」（吃早茶、健身、
看电影同样如此），按固定开始时间落位；只有出现截止语义（`18点前`/`截止`/`最晚`/`提交`）
才当作截止时间、由排期器塞进最早空档。若固定时刻已过仍未完成，排期里会标「开始时刻已过」。

**大模型不可用时的行为**：默认（`use_rule_fallback=false`）**直接报错**，不建任务，也不会
用关键词规则去猜意图——规则是闭集，猜错时不会报错，只会静默产生垃圾待办。

**规则兜底（`SCHEDULE_USE_RULE_FALLBACK=1` 才启用）的边界**：规则解析**不会把闲聊天建成任务**。
提问（`为什么…`）、纠正（`只要…`/`不对`）、以及以语气词收尾的长句，一律回一句引导而不是新增；
`查看日程`/`看看日程` 等说法会正确走到查询。宁可答"没听懂"，也不凭空产生垃圾待办。
规则兜底也支持按名称操作：`完成 吃早茶`、`删除 吃早茶`（原来只认数字编号）。

**修改已有安排（modify）**：`自习时间改成11点`、`把吃早茶改到下午3点`、`晨会推迟到9点半`、
`自习提前到8点`、`自习改到11点，时长1小时` 都会**改动原任务**而不是新建一条。
规则兜底与大模型两条链路都支持，判定方式：识别「改到/改成/改为/调到/挪到/推迟到/提前到…」
标记词，左边当任务关键词去匹配、右边当新时间解析，改完自动重排并重置提醒。
语义会继承：原来是「18点前提交」这类只有截止时间的任务，改时间后仍是截止时间，
不会因为一次修改就变成「18点开始做」；周期任务按「先匹配当天实例」的规则只改今天这一次。

数据落在 `$SCHEDULE_DATA_DIR`（默认 `./data`）下的 `schedule.db` 与 `auth.db`。
旧版单用户库会自动迁移：新增 `user_id` 列并把历史数据归入 `default` 用户。
