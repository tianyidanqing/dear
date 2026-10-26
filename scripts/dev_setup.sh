#!/usr/bin/env bash
# 开发者一键准备环境：建虚拟环境 -> 装依赖 -> 自检 -> 跑测试。
#
# 为什么不把 .venv 提交到仓库：虚拟环境里写死了本机绝对路径
# （pyvenv.cfg 的 home/command、__editable__.pth 的项目路径），还含
# .exe / .dll / .pyd 等平台专属二进制。换机器、换系统就不可用。
# 这个脚本在目标机器上原地重建，才是可移植的做法。
#
# 用法：
#   bash scripts/dev_setup.sh            # 建 .venv + 装全部依赖 + 跑测试
#   bash scripts/dev_setup.sh --locked   # 按 requirements.lock 严格复现版本
#   bash scripts/dev_setup.sh --no-test  # 只装环境，不跑测试
#   PYTHON=/path/to/python3.12 bash scripts/dev_setup.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

LOCKED=0
RUN_TEST=1
for a in "$@"; do
  case "$a" in
    --locked)  LOCKED=1 ;;
    --no-test) RUN_TEST=0 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$a（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

# ---------------- 1. 找解释器 ----------------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
[ -n "$PY" ] || { echo "找不到 Python，请先安装 3.10+，或用 PYTHON=/path/to/python 指定" >&2; exit 1; }
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10+，当前是 $("$PY" -V 2>&1)" >&2
  exit 1
fi
echo "==> 解释器：$("$PY" -V 2>&1)  ($PY)"

# ---------------- 2. 虚拟环境 ----------------
if [ -d .venv ]; then
  echo "==> .venv 已存在，复用（要重建请先删掉该目录）"
else
  echo "==> 创建 .venv"
  "$PY" -m venv .venv
fi

if   [ -x .venv/Scripts/python.exe ]; then VPY=.venv/Scripts/python.exe   # Windows
elif [ -x .venv/bin/python ];         then VPY=.venv/bin/python           # Linux / macOS
else
  echo "虚拟环境创建后找不到 python，请检查 .venv 目录" >&2
  exit 1
fi

# ---------------- 3. 装依赖 ----------------
"$VPY" -m pip install --upgrade pip -q
if [ "$LOCKED" = "1" ]; then
  echo "==> 按 requirements.lock 安装精确版本（可复现）"
  "$VPY" -m pip install -q -r requirements.lock
  "$VPY" -m pip install -q -e . --no-deps
else
  echo "==> 安装全部依赖：[all,dev]"
  "$VPY" -m pip install -q -e ".[all,dev]"
fi

# ---------------- 4. 自检 ----------------
echo "==> 自检"
"$VPY" -c "import schedule_agent as m; print('    schedule_agent', m.__version__, '导入正常')"

# ---------------- 5. 测试 ----------------
if [ "$RUN_TEST" = "1" ]; then
  echo "==> 跑测试"
  "$VPY" -m pytest -q
else
  echo "==> 已跳过测试（--no-test）"
fi

echo
echo "完成。下一步："
echo "  $VPY -m schedule_agent             # 命令行 / REPL"
echo "  $VPY -m schedule_agent.api.app     # 起 API 服务（需配置 SCHEDULE_LLM_API_KEY）"
echo "  $VPY -m pytest -q                  # 再跑一次测试"
