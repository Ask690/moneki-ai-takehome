#!/usr/bin/env bash
# 一条命令跑起来：建环境 -> 装依赖 -> 重建 -> 起服务。
#   bash start.sh          无 Key 启动（问答进入降级模式）
#   PORT=9000 bash start.sh
# 配 Key 再加三行环境变量（见 README.md 第一节、LLM_SETUP.md）。
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"

if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  PY="$ROOT/.venv/Scripts/python.exe"     # Windows
else
  PY="$ROOT/.venv/bin/python"             # Linux / macOS
fi

if [ ! -x "$PY" ]; then
  echo "[1/3] 建虚拟环境"
  python -m venv "$ROOT/.venv"
  if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
    PY="$ROOT/.venv/Scripts/python.exe"
  else
    PY="$ROOT/.venv/bin/python"
  fi
else
  echo "[1/3] 虚拟环境已存在，跳过"
fi

echo "[2/3] 安装依赖"
"$PY" -m pip install -q --disable-pip-version-check -r "$ROOT/starter/requirements.txt"

echo "[3/3] 重建清洗表与索引"
cd "$ROOT/starter"
"$PY" -m kbqa.rebuild

echo
echo "服务地址：http://127.0.0.1:$PORT"
if [ -n "${LLM_API_KEY:-}" ]; then
  echo "模型：${LLM_MODEL:-未设置}（live 模式）"
else
  echo "模型：未配置 LLM_API_KEY，问答走降级模式（见 README.md 第三节）"
fi
echo "按 Ctrl+C 停止。"
echo
# 不用 --reload：重建过索引之后不重启会让内存里的索引落后于磁盘。
exec "$PY" -m uvicorn kbqa.server:app --host 127.0.0.1 --port "$PORT"
