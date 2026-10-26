# 虚拟机部署与运维手册

本文面向** Linux 虚拟机**（Ubuntu 22.04 / Debian 12 / CentOS Stream 9 等均适用），
讲解如何把日程管理 AI Agent 部署到虚拟机上并长期运行。

> ⚠️ Windows 本地开发用的 `.venv` **不能**直接拷到 Linux 虚拟机上用
> （解释器和二进制依赖不通用）。虚拟机上必须重新创建虚拟环境，
> 本文的三种方式都会自动完成这一步。

---

## 1. 前提条件

| 项目 | 要求 |
|------|------|
| 操作系统 | 主流 Linux x86_64（本文命令以 Ubuntu/Debian 为例） |
| Python | **≥ 3.11**（`python3 --version` 确认；Ubuntu 22.04 自带 3.10 需另行安装 3.11+） |
| 内存 | ≥ 512 MB（启用大模型流式建议 ≥ 1 GB） |
| 磁盘 | ≥ 1 GB（代码 + venv + 数据） |
| 网络 | 出站可访问 pip 源；如需 LLM 解析，出站可访问大模型 API |
| 权限 | root 或 sudo（仅安装阶段需要；服务本身以普通用户运行） |

安装基础组件（Ubuntu/Debian）：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
# CentOS/RHEL: sudo dnf install -y python3 python3-pip git
```

## 2. 把代码放到虚拟机上

任选一种：

```bash
# 方式 A：git（推荐，便于后续升级）
git clone <你的仓库地址> schedule_agent && cd schedule_agent

# 方式 B：scp 从本机推送（在本机执行，把 <本地项目路径> 换成你 clone 下来的目录）
scp -r <本地项目路径> user@<VM_IP>:~/schedule_agent

# 方式 C：rsync（适合反复同步，已排除 venv / 数据 / 缓存）
rsync -avz --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
  <本地项目路径>/ user@<VM_IP>:~/schedule_agent/
```

> 无论哪种方式，都**不要**把本机的 `.venv/`、`data/` 带过去。

## 3. 部署方式一：systemd（推荐生产使用）

一键安装脚本会完成：创建专用用户 → 同步代码到 `/opt` → 建 venv 装依赖 →
生成配置 → 注册并启动 systemd 服务。

```bash
cd ~/schedule_agent
sudo bash scripts/install.sh              # 只装后端 API
sudo bash scripts/install.sh --with-ui    # 同时装 Gradio 前端
```

安装后的布局（与代码目录分离，符合 Linux 惯例）：

| 路径 | 内容 |
|------|------|
| `/opt/schedule-agent/` | 代码 + `.venv`（虚拟环境） |
| `/etc/schedule-agent/config.json` | 配置文件（**安装后第一件事就是改它**） |
| `/var/lib/schedule-agent/` | 数据（`schedule.db` / `auth.db`）+ 日志 |

编辑配置（至少配置管理员 Key；需要 LLM 解析则填 API Key）：

```bash
sudo nano /etc/schedule-agent/config.json
# 重点项：llm_api_key / llm_base_url / llm_model / admin_api_key / daily_summary_time
sudo systemctl restart schedule-agent     # 改完配置重启生效
```

验证：

```bash
curl http://127.0.0.1:8000/api/health
# {"ok":true,"service":"schedule-agent","version":"4.1.0","auth_required":true}
```

## 4. 部署方式二：Docker / Compose

虚拟机上有 Docker 时最省事：

```bash
cd ~/schedule_agent/deploy
# 如需 LLM / 管理员 Key，先编辑 docker-compose.yml 里 api 服务的 environment
docker compose up -d
docker compose ps            # 确认 api 健康检查通过
docker compose logs -f api   # 看日志
```

- API 暴露在 `8000`，Gradio 前端在 `7860`；
- 数据在 Docker 命名卷 `schedule-data`（`docker volume inspect schedule-data` 看实际路径）；
- 升级：`git pull && docker compose up -d --build`。

## 5. 部署方式三：手动安装（不进 /opt，自定义目录）

不想用脚本时手动来一遍（以当前用户运行）：

```bash
cd ~/schedule_agent
python3 -m venv .venv
source .venv/bin/activate
pip install ".[api]"                # 只要后端；加 ui 则 ".[api,ui]"

cp config/config.example.json config/config.json
nano config/config.json             # 改配置

# 用环境变量指定数据目录（不指定则默认 ./data）
export SCHEDULE_DATA_DIR=$HOME/schedule-data
mkdir -p "$SCHEDULE_DATA_DIR"

schedule-agent-api --host 0.0.0.0 --port 8000   # 前台跑，Ctrl+C 停
# 后台跑：nohup schedule-agent-api --host 0.0.0.0 --port 8000 > api.log 2>&1 &
```

## 6. 对外开放与 Nginx 反代

### 6.1 防火墙 / 安全组

```bash
# ufw（Ubuntu）
sudo ufw allow 8000/tcp     # API
sudo ufw allow 7860/tcp     # Gradio 前端（如装了）

# firewalld（CentOS）
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload
```

云虚拟机还要在**控制台安全组**里放行对应端口。只在内网用可不开放，走 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 -L 7860:127.0.0.1:7860 user@<VM_IP>
```

### 6.2 Nginx 反代 + HTTPS（可选但推荐）

```bash
sudo apt install -y nginx
sudo cp deploy/nginx.conf /etc/nginx/conf.d/schedule-agent.conf
sudo nano /etc/nginx/conf.d/schedule-agent.conf   # 改 server_name 为你的域名
sudo nginx -t && sudo systemctl reload nginx
```

配置里已处理好两个关键点：

- **SSE 必须 `proxy_buffering off`**——否则流式回复被缓冲成一整块，打字机效果失效；
- WebSocket 需要 `Upgrade` 头（实时提醒推送用）。

HTTPS 用 certbot 一键签发：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

## 7. 日常使用

### 7.1 浏览器交互（装了前端时）

打开 `http://<VM_IP>:7860`，先在顶部「注册 / 登录」填用户名拿 Token，然后直接说话：
`明天下午3点开会 1小时 高`、`每天 上午9点 晨会 20分钟`、`完成 3`、`今日日程`。

### 7.2 API 调用

```bash
# 注册拿 Token
curl -X POST http://<VM_IP>:8000/api/auth/register \
     -H "Content-Type: application/json" -d '{"user_id":"alice","password":"可选"}'

# 带 Token 使用
TOKEN=sZ3f...
curl -H "Authorization: Bearer $TOKEN" http://<VM_IP>:8000/api/tasks
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"text":"周五 上午10点 健身 45分钟"}' http://<VM_IP>:8000/api/tasks
```

### 7.3 命令行直连（在虚拟机上本机操作，无需 Token）

```bash
source /opt/schedule-agent/.venv/bin/activate   # systemd 方式安装时
export SCHEDULE_DATA_DIR=/var/lib/schedule-agent
export SCHEDULE_CONFIG=/etc/schedule-agent/config.json
schedule-agent --user alice list
schedule-agent --user alice add "明天上午10点 写周报 30分钟"
```

> 注意要带上和 systemd 服务**相同的** `SCHEDULE_DATA_DIR` / `SCHEDULE_CONFIG`，
> 否则 CLI 会读写默认 `./data` 下的另一个库，看到的数据和服务不一致。

## 8. 日常运维（systemd 方式）

```bash
sudo systemctl status schedule-agent     # 状态
sudo systemctl restart schedule-agent    # 重启（改配置后）
sudo systemctl stop schedule-agent       # 停止
journalctl -u schedule-agent -f          # 实时日志
journalctl -u schedule-agent --since "1 hour ago"   # 最近一小时
```

应用日志同时落盘：`/var/lib/schedule-agent/logs/schedule-agent.log`（按大小自动轮转）。

**备份**（SQLite 文件，停不停服都能拷；更稳妥用 `.backup`）：

```bash
sudo sqlite3 /var/lib/schedule-agent/schedule.db ".backup '/backup/schedule-$(date +%F).db'"
sudo sqlite3 /var/lib/schedule-agent/auth.db     ".backup '/backup/auth-$(date +%F).db'"
```

**升级**：

```bash
cd ~/schedule_agent && git pull          # 或重新 rsync
sudo bash scripts/install.sh             # 重跑脚本：同步代码+装新依赖+重启
# 配置文件已存在时脚本不会覆盖；数据库结构如有变化会自动迁移
```

**卸载**：

```bash
sudo systemctl disable --now schedule-agent
sudo rm /etc/systemd/system/schedule-agent.service && sudo systemctl daemon-reload
sudo rm -rf /opt/schedule-agent
# 数据与配置按需保留或删除：/var/lib/schedule-agent、/etc/schedule-agent
```

## 9. 环境变量速查

优先级：**内置默认值 < 配置文件 < 环境变量**。

| 变量 | 默认 | 说明 |
|------|------|------|
| `SCHEDULE_CONFIG` | 自动查找 | 配置文件路径 |
| `SCHEDULE_DATA_DIR` | `./data` | 数据目录（库 + 日志） |
| `SCHEDULE_DB_PATH` / `SCHEDULE_AUTH_DB_PATH` | 数据目录下 | 单独指定两个库的路径 |
| `SCHEDULE_API_HOST` / `SCHEDULE_API_PORT` | `127.0.0.1` / `8000` | 后端监听地址 |
| `SCHEDULE_UI_PORT` | `7860` | 前端端口 |
| `SCHEDULE_API_URL` | `http://localhost:8000` | 前端连后端的地址 |
| `SCHEDULE_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | 空 | 大模型（空则规则兜底解析） |
| `SCHEDULE_ADMIN_API_KEY` | 空（关闭） | 管理员 Key |
| `SCHEDULE_LOG_LEVEL` | `INFO` | 日志级别 |

## 10. 故障排查

| 现象 | 排查 |
|------|------|
| `systemctl status` 显示 failed | `journalctl -u schedule-agent -e` 看报错；多数是配置 JSON 语法错误或端口被占 |
| 健康检查 401/连不上 | 确认服务监听地址：`ss -ltnp \| grep 8000`；`SCHEDULE_API_HOST=127.0.0.1` 时外部访问不到，需改 `0.0.0.0` 或走 Nginx |
| 前端收不到流式回复 | 经过 Nginx 时确认 `proxy_buffering off`；直连时确认中间没有公司代理缓冲 |
| 回复变「规则解析」风格 | LLM Key 未配或调用失败，查日志里的 `llm` 相关行 |
| CLI 和 API 数据不一致 | 两边用的 `SCHEDULE_DATA_DIR` 不同，见 7.3 |
| 权限 denied 写数据 | `sudo chown -R schedule:schedule /var/lib/schedule-agent` |
| pip 安装慢/失败 | 换镜像源：`pip config set global.index-url https://mirrors.tencent.com/pypi/simple/` |
