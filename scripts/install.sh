#!/usr/bin/env bash
# 虚拟机一键安装：把本服务装到 /opt/schedule-agent，并注册为 systemd 服务。
#
# 用法（root 或 sudo）：
#   sudo bash scripts/install.sh            # 安装 API 服务
#   sudo bash scripts/install.sh --with-ui  # 同时装 Gradio 前端（额外注册一个服务）
#
# 目录约定（都可用环境变量覆盖）：
#   APP_DIR=/opt/schedule-agent   代码与虚拟环境
#   DATA_DIR=/var/lib/schedule-agent   数据库与日志
#   CONF_DIR=/etc/schedule-agent    配置文件
set -euo pipefail

WITH_UI=0
for a in "$@"; do [[ "$a" == "--with-ui" ]] && WITH_UI=1; done

APP_DIR="${APP_DIR:-/opt/schedule-agent}"
DATA_DIR="${DATA_DIR:-/var/lib/schedule-agent}"
CONF_DIR="${CONF_DIR:-/etc/schedule-agent}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER=schedule

echo "==> 1/6 创建系统用户与目录"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -m -d "$DATA_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"

echo "==> 2/6 同步代码到 $APP_DIR"
mkdir -p "$APP_DIR/src" "$APP_DIR/config" "$APP_DIR/deploy"
cp -r "$SRC_DIR"/src/* "$APP_DIR/src/"
cp -r "$SRC_DIR"/config/* "$APP_DIR/config/" 2>/dev/null || true
cp "$SRC_DIR"/pyproject.toml "$SRC_DIR"/README.md "$APP_DIR/" 2>/dev/null || true
cp "$SRC_DIR"/deploy/schedule-agent.service "$APP_DIR/deploy/"

echo "==> 3/6 创建虚拟环境并安装依赖"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
if [[ "$WITH_UI" == "1" ]]; then
  "$APP_DIR/.venv/bin/pip" install "$APP_DIR[api,ui]"
else
  "$APP_DIR/.venv/bin/pip" install "$APP_DIR[api]"
fi

echo "==> 4/6 准备配置文件（已存在则跳过，避免覆盖你的修改）"
if [[ ! -f "$CONF_DIR/config.json" ]]; then
  cp "$APP_DIR/config/config.example.json" "$CONF_DIR/config.json"
  echo "    已生成 $CONF_DIR/config.json，请按需修改（尤其是 llm_api_key / admin_api_key）"
else
  echo "    $CONF_DIR/config.json 已存在，保持不动"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"

echo "==> 5/6 安装 systemd 服务"
sed -e "s#/opt/schedule-agent#$APP_DIR#g" \
    -e "s#/var/lib/schedule-agent#$DATA_DIR#g" \
    -e "s#/etc/schedule-agent#$CONF_DIR#g" \
    "$APP_DIR/deploy/schedule-agent.service" > /etc/systemd/system/schedule-agent.service
systemctl daemon-reload
systemctl enable schedule-agent

echo "==> 6/6 启动"
systemctl restart schedule-agent
sleep 2
systemctl --no-pager status schedule-agent || true

echo
echo "完成。API 默认监听 8000："
echo "  curl http://127.0.0.1:8000/api/health"
echo "  日志：journalctl -u schedule-agent -f"
echo "  数据：$DATA_DIR     配置：$CONF_DIR/config.json"
