#!/usr/bin/env bash
# Prompt2App 停止脚本
set -e

cd "$(dirname "$0")"
PID_FILE=".p2a.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "⚠️  未找到 $PID_FILE，服务可能未通过 start.sh 启动"
  exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID" 2>/dev/null || true
  # 优雅退出最多等待 5 秒，超时强杀
  for _ in $(seq 1 10); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  kill -9 "$PID" 2>/dev/null || true
  echo "✅ 服务已停止 (PID $PID)"
else
  echo "⚠️  进程 $PID 已不存在"
fi
rm -f "$PID_FILE"
