#!/bin/sh
# Start the local server without installing dependencies.
set -eu
TOOL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TASK_PYTHON=''
for candidate in "$(command -v python3 2>/dev/null || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        TASK_PYTHON="$candidate"
        break
    fi
done
if [ -z "$TASK_PYTHON" ]; then
    echo "需要 Python 3.10 或更高版本（支持 PATH 或 Homebrew 标准安装位置）。" >&2
    exit 1
fi
cd "$TOOL_DIR"
exec "$TASK_PYTHON" "$TOOL_DIR/manage.py" "$@"
