#!/bin/bash
# 双击在本机运行学习教练演示页（space/app.py）。
# 第一次运行会在 space/.venv 建一个单独的 Python 环境并安装依赖，不动项目原有的 .venv。
# 模型配置读取 agent/.env；
# 登录 Azure 用的是 az login 的账号。
cd "$(dirname "$0")"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
[ -x "$PY" ] || PY=python3
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "需要 Python 3.11 或更新版本。"; read -n 1 -s -r -p "按任意键关闭"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "第一次运行：正在安装依赖，需要几分钟…"
  "$PY" -m venv .venv && .venv/bin/pip install -q -r requirements.txt || {
    rm -rf .venv; read -n 1 -s -r -p "依赖安装失败，按任意键关闭"; exit 1; }
fi
if command -v az >/dev/null && ! az account show >/dev/null 2>&1; then
  echo "还没登录 Azure，浏览器会打开登录页面…"
  az login >/dev/null || { read -n 1 -s -r -p "登录失败，按任意键关闭"; exit 1; }
fi
export PORT=8000
export LEARNING_COACH_DATA_DIR="$HOME/.learning-coach-space"
(sleep 6; open "http://localhost:8000/") &
.venv/bin/python app.py
