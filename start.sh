#!/usr/bin/env bash
# Prompt2App 启动脚本：加载 .env -> 后台启动 uvicorn -> 健康检查
set -e

cd "$(dirname "$0")"
PID_FILE=".p2a.pid"

# React 前端构建产物由 FastAPI 提供；缺失时服务会回退到旧静态页。
if [ ! -f "frontend/dist/index.html" ]; then
  echo "⚠️ 未找到 React 前端构建产物。请先运行：cd frontend && npm ci && npm run build"
fi

# 1. 环境检查
if [ ! -d ".venv" ]; then
  echo "❌ 未找到 .venv，请先运行 ./setup.sh"
  exit 1
fi
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "⚠️  服务已在运行 (PID $(cat "$PID_FILE"))，如需重启请先 ./stop.sh"
  exit 0
fi

# 2. 加载 .env（只补缺，绝不覆盖终端已 export 的变量——已 export 的 Key 优先级更高）
if [ -f ".env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="$(echo "${line%%=*}" | tr -d '[:space:]')"
    val="$(echo "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']$//")"
    [ -z "$key" ] && continue
    if [ -z "${!key+x}" ]; then export "$key=$val"; fi
  done < .env
fi

if [ -n "$1" ] && ! [[ "$1" =~ ^[0-9]+$ ]]; then
  echo "用法: ./start.sh [端口号]   例如: ./start.sh 8001"
  exit 1
fi

PORT="${1:-${PORT:-8000}}"

# 3. 后台启动（日志写入 .p2a.log）
echo "🚀 启动 Prompt2App（0.0.0.0:$PORT）..."
nohup .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" \
  > .p2a.log 2>&1 &
echo $! > "$PID_FILE"

# 4. 健康检查（最多等待 15 秒）
for i in $(seq 1 15); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "✅ 服务已就绪:  http://127.0.0.1:$PORT"
    echo "   本机访问:    http://localhost:$PORT"
    echo "   局域网访问:  http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
    echo "   日志:        tail -f .p2a.log    停止: ./stop.sh"
    exit 0
  fi
  sleep 1
done

echo "❌ 服务未能通过健康检查，最近日志："
tail -n 20 .p2a.log
exit 1
