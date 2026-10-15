#!/bin/zsh
# 双击打开派发小工具（关掉这个终端窗口不影响已经打开的小工具）
cd "$(dirname "$0")"
${PYTHON:-python3} touchda_gui.py
