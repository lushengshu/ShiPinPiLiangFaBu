#!/bin/zsh
set -u

ROOT_DIR="${0:A:h:h}"
cd "$ROOT_DIR" || {
  echo "无法进入工具目录：$ROOT_DIR" >&2
  read -r "?按回车关闭此窗口..."
  exit 1
}

PYTHON="$ROOT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
PORT="${CHANNELS_PORT:-8786}"
if ! curl -fsS "http://127.0.0.1:${PORT}/api/state" >/dev/null 2>&1; then
  nohup "$PYTHON" "$ROOT_DIR/channels_app.py" > "$ROOT_DIR/.channels_batch/server.log" 2>&1 &
  for i in {1..30}; do
    curl -fsS "http://127.0.0.1:${PORT}/api/state" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi
open "http://127.0.0.1:${PORT}/"
