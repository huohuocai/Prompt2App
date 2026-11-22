#!/usr/bin/env bash
# Prompt2App 环境初始化：Python 版本检查 -> venv -> 依赖安装 -> .env 生成
set -e

cd "$(dirname "$0")"
echo "🚀 开始配置 Prompt2App 环境..."

# 1. Python 版本检查（需 >= 3.10）
PY=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then PY="$cmd"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 需要 Python >= 3.10，请先从 python.org 安装较新版本。"
  exit 1
fi
echo "✅ 检测到 $PY ($($PY --version | awk '{print $2}'))"

# 2. 创建虚拟环境（若不存在）
if [ ! -d ".venv" ]; then
  "$PY" -m venv .venv
  echo "✅ 已创建虚拟环境 .venv"
fi

# 3. 安装依赖（默认清华源加速，可用 PIP_INDEX_URL 覆盖）
source .venv/bin/activate
python -m pip install --upgrade pip -q
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
pip install -r requirements.txt -i "$PIP_INDEX_URL"
echo "✅ 依赖安装完成"

# 4. 生成 .env（若不存在）
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅ 已从 .env.example 生成 .env（尚未配置模型 Key，系统将运行演示模式）"
fi

echo ""
echo "🎉 环境就绪！下一步："
echo "   配置模型 Key:   vi .env"
echo "   启动服务:       ./start.sh"
echo "   运行自测:       .venv/bin/python -m backend.agents --test"
