#!/usr/bin/env bash
set -euo pipefail

# Muse Learning Bridge 起動スクリプト（初期状態では 127.0.0.1 のみにバインドし外部公開しない）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "エラー: 仮想環境が見つかりません。先に ./install.sh を実行してください。" >&2
  exit 1
fi

HOST="${MUSE_BRIDGE_HOST:-127.0.0.1}"
PORT="${MUSE_BRIDGE_PORT:-8765}"

if [ $# -gt 0 ]; then
  exec "$VENV_PY" main.py "$@"
fi

echo "Muse Learning Bridge API サーバーを起動します (http://${HOST}:${PORT})..."
exec "$VENV_PY" main.py serve --host "$HOST" --port "$PORT"
