#!/usr/bin/env bash
set -euo pipefail

# Muse Learning Bridge 自動セットアップスクリプト（管理者権限不要・冪等実行対応）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Muse Learning Bridge セットアップ ==="
OS_NAME="$(uname -s || echo Unknown)"
echo "OS: $OS_NAME"

if ! command -v python3 >/dev/null 2>&1; then
  echo "エラー: python3 が見つかりません。Python 3.12 以上をインストールしてください。" >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
PY_OK="$(python3 -c 'import sys; print(int(sys.version_info >= (3, 12)))')"
echo "Python: $PY_VER"
if [ "$PY_OK" != "1" ]; then
  echo "エラー: Python 3.12 以上が必要です（現在: $PY_VER）。" >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "[1/5] 仮想環境 (.venv) を作成しています..."
  python3 -m venv .venv
else
  echo "[1/5] 既存の仮想環境 (.venv) を再利用します。"
fi

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "エラー: 仮想環境の Python ($VENV_PY) が見つかりません。" >&2
  exit 1
fi

echo "[2/5] 依存パッケージをインストールしています..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -e ".[dev]" --quiet

echo "[3/5] Playwright Chromium を確認・導入しています..."
if ! "$VENV_PY" -m playwright install chromium; then
  echo "警告: Playwright Chromium の自動導入に失敗しました。オフライン環境または共有ライブラリ不足の場合は手動で確認してください。" >&2
fi

echo "[4/5] 設定ファイルおよび作業ディレクトリを初期化しています..."
mkdir -p data logs
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo ".env を .env.example から新規作成しました（MUSE_BRIDGE_ADMIN_TOKEN 等を設定してください）。"
else
  echo "既存の .env を保持します（上書きしません）。"
fi

echo "[5/5] ヘルスチェックを実行しています..."
"$VENV_PY" main.py health

echo "=== セットアップ完了 ==="
echo "サーバー起動コマンド: ./start.sh"
