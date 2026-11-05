#!/bin/zsh
# Finder/访达双击入口：启动器会复用已有服务、等待接口就绪，并自动打开页面。
set -u

ROOT_DIR="${0:A:h:h}"
cd "$ROOT_DIR" || {
  echo "无法进入工具目录：$ROOT_DIR" >&2
  read -r "?按回车关闭此窗口..."
  exit 1
}

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "没有找到 Python 3，请先安装 Python 3 后再启动。" >&2
  read -r "?按回车关闭此窗口..."
  exit 1
fi

"$PYTHON_BIN" "$ROOT_DIR/launch_local_tool.py" "$@"
STATUS=$?
if (( STATUS != 0 )); then
  echo
  echo "启动失败；详细日志位于：$ROOT_DIR/.douyin_batch/server.log" >&2
  read -r "?按回车关闭此窗口..."
fi
exit "$STATUS"
