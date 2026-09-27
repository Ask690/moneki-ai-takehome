#!/usr/bin/env bash
# 开发辅助脚本：重启服务、重建、跑评测。
# 只给开发用，评测方不需要它；正式流程见仓库根目录的 README.md。
#
#   scripts/dev.sh stop      停掉占用 8000 端口的服务
#   scripts/dev.sh start     后台起服务，日志写到 server.log
#   scripts/dev.sh restart   停掉再起
#   scripts/dev.sh rebuild   停服务 -> make rebuild -> 起服务
#   scripts/dev.sh eval [类别]  跑公开题库（可带 --only 类别）
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/Scripts/python.exe"
PORT="${PORT:-8000}"

stop() {
  # 只杀监听本端口的进程，不动别的 python
  local pids
  pids="$(netstat -ano 2>/dev/null | grep -E "LISTENING" | grep ":$PORT " | awk '{print $NF}' | sort -u)"
  for pid in $pids; do
    [ -n "$pid" ] && taskkill //PID "$pid" //F >/dev/null 2>&1 && echo "已停止 PID $pid"
  done
  sleep 1
}

start() {
  cd "$ROOT/starter" || exit 1
  PYTHONIOENCODING=utf-8 "$PY" -m uvicorn kbqa.server:app --host 127.0.0.1 --port "$PORT" \
    > "$ROOT/server.log" 2>&1 &
  for _ in $(seq 1 30); do
    sleep 0.5
    if curl -s "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      echo "服务已就绪：http://127.0.0.1:$PORT"
      return 0
    fi
  done
  echo "服务启动失败，见 server.log" >&2
  tail -20 "$ROOT/server.log" >&2
  return 1
}

rebuild() {
  stop
  cd "$ROOT/starter" || exit 1
  PYTHONIOENCODING=utf-8 "$PY" -m kbqa.rebuild || exit 1
  start
}

run_eval() {
  cd "$ROOT" || exit 1
  local extra=()
  if [ "${1:-}" != "" ]; then extra=(--only "$1"); fi
  PYTHONIOENCODING=utf-8 "$PY" eval/run_eval.py \
    --base-url "http://127.0.0.1:$PORT" \
    --questions eval/public_questions.jsonl \
    --out "$ROOT/run_${1:-all}" "${extra[@]+"${extra[@]}"}"
}

case "${1:-}" in
  stop) stop ;;
  start) start ;;
  restart) stop; start ;;
  rebuild) rebuild ;;
  eval) run_eval "${2:-}" ;;
  *) echo "用法: $0 {stop|start|restart|rebuild|eval [类别]}" >&2; exit 2 ;;
esac
