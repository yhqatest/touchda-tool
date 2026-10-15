#!/bin/zsh
# 一键安装：检查 Python + Tk、装依赖、生成个人配置。可重复跑。
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v $PY >/dev/null; then
  echo "✗ 没找到 python3。先装 Python 3.10+（推荐 https://www.python.org/downloads/ 官方安装包，自带 Tk）"; exit 1
fi
$PY - <<'PYEOF' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit(f"✗ Python 太旧（{sys.version.split()[0]}），要 3.10+")
try:
    import tkinter
except ImportError:
    sys.exit("✗ 这个 Python 没带 Tk。Homebrew 的 Python 要再装：brew install python-tk")
print("✓ Python", sys.version.split()[0], "+ Tk", tkinter.TkVersion)
PYEOF

if ! [ -d "/Applications/Google Chrome.app" ]; then
  echo "✗ 没找到 Google Chrome，先装 Chrome"; exit 1
fi

echo "→ 安装依赖（playwright / openpyxl）…"
$PY -m pip install --user -q -r requirements.txt

[ -f config.json ] || { cp config.example.json config.json; echo "✓ 已生成 config.json（在小窗口里填模版名即可）"; }
mkdir -p data
chmod +x 启动派发小工具.command touchda_chrome.sh

echo ""
echo "✓ 装好了。双击「启动派发小工具.command」打开。"
echo "  第一次用：点「启动专用 Chrome」→ 在弹出的 Chrome 里登录后台 → 右边填你的模版名。"
