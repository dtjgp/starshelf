#!/bin/bash
# 双击打开 Starshelf（本地运行，关闭此窗口即停止）
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/miniconda3/bin:$PATH"
PY="$HOME/miniconda3/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" star_lists_ui.py "$@"
