# Muse 導入・運用マニュアル (`MUSE_GUIDE.md`)

本書は、Meta のパーソナル AI エージェント **Muse** が GitHub リポジトリを取得し、導入・設定・動作検証・スケジュール実行・結果報告までを一貫して行うための手順書です。

---

## 1. GitHub リポジトリの取得

```bash
git clone https://github.com/Aikidoooo/muse-learning-bridge.git
cd muse-learning-bridge
```

---

## 2. `MUSE_SPEC.md` および必須文書の確認

作業を開始する前に、リポジトリ直下の以下 4 ファイルを順番に確認してください（[`AGENTS.md`](./AGENTS.md) 参照）。

1. [`MUSE_SPEC.md`](./MUSE_SPEC.md)
2. [`MUSE_GUIDE.md`](./MUSE_GUIDE.md)（本書）
3. [`API_SPEC.md`](./API_SPEC.md)
4. [`SECURITY.md`](./SECURITY.md)

---

## 3. 実行環境の確認

Python **3.12 以上** が利用可能であることを確認します。

```bash
python3 --version
```

---

## 4. Python 依存関係のインストール

自動セットアップスクリプト [`install.sh`](./install.sh) を実行します（`sudo` 不要・既存 `.env` や `config.yaml` は上書きされません）。

```bash
chmod +x install.sh start.sh
./install.sh
```

Windows PowerShell の場合:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\playwright.exe install chromium
```

---

## 5. プログラムの起動

初期状態では外部ネットワークへ管理 API を公開しないよう、ループバックアドレス（`127.0.0.1:8765`）のみで起動します。

```bash
./start.sh
```

（または `source .venv/bin/activate && python main.py serve --host 127.0.0.1 --port 8765`）

単発のバッチ実行を行う場合は CLI サブコマンドを使用します。

```bash
./.venv/bin/python main.py run --input path/to/questions.json --provider gemini --output report.json
```

---

## 6. ヘルスチェック

```bash
./.venv/bin/python main.py health
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8765/providers
```

`"status": "ok"` が返れば起動正常です。

---

## 7. API の安全な登録（管理認証・外部 AI API キー）

API キーやトークンをチャット・平文 JSON・SQLite・ログ・`config.yaml` へ保存しないでください。

1. **管理トークンの設定**: `.env`（`install.sh` により `.env.example` から初期化）に `MUSE_BRIDGE_ADMIN_TOKEN` を設定します。未設定時は管理 API（`/config`, `/providers/test`, `/providers/models`, `/credentials/status`）がすべて `401` で拒否されます。
2. **外部 AI API キーの登録（公式方式）**:
   - **推奨**: Muse の **Secure Credentials Store** へ外部 AI API キーおよび管理トークンを登録し、Custom Connector 経由で `Authorization: Bearer <MUSE_BRIDGE_ADMIN_TOKEN>` と `X-Provider-Api-Key: <API_KEY>` ヘッダを自動注入します。
   - **ローカル環境変数方式**: VM 内の `.env` または OS 環境変数（`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_COMPAT_API_KEY`）へ設定します。
   - ※ Muse のホスト側隔離ストアを Python から直接参照する未公開 API 等は使用・推測していません。公式コネクタから取得できない場合は「未対応」として扱い、ヘッダ注入または VM 内環境変数を使用してください。
3. **登録状態の確認（キー値は返却されません）**:
   ```bash
   curl -s http://127.0.0.1:8765/credentials/status \
     -H "Authorization: Bearer $MUSE_BRIDGE_ADMIN_TOKEN"
   ```

---

## 8. 外部 AI との接続テスト・動的設定

```bash
# 対応モデル一覧の取得
curl -s "http://127.0.0.1:8765/providers/models?provider=gemini&remote=true" \
  -H "Authorization: Bearer $MUSE_BRIDGE_ADMIN_TOKEN"

# 接続テスト
curl -s -X POST http://127.0.0.1:8765/providers/test \
  -H "Authorization: Bearer $MUSE_BRIDGE_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider":"gemini","model":"gemini-2.5-flash"}'

# 動的設定変更（接続テスト成功時のみ保存、失敗時は自動ロールバック）
curl -s -X PATCH http://127.0.0.1:8765/config \
  -H "Authorization: Bearer $MUSE_BRIDGE_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"default_provider":"openai","default_model":"gpt-4o-mini","max_cost_per_job_usd":0.3,"timeout_seconds":60}'
```

---

## 9. 自作模擬教材での動作検証

実際の学習サービスへアクセスする前に、同梱の `mock_test/`（模擬ログイン・練習教材・総合模擬テスト・不許可テスト）と自動テストで動作を検証します。

```bash
./.venv/bin/python -m pytest -v
```

または模擬ポータルを手動配信してブラウザ連携を確認します。

```bash
python3 -m http.server 8090 --directory mock_test
```

---

## 10. スケジュール実行の検証

スケジュール時刻・対象教材・使用モデルの管理は **Muse 側** が担当します。Python 側に固定の日時や教材名をハードコードしないでください。

- Muse から指定時刻に `POST /jobs` または `./.venv/bin/python main.py run --input ...` を起動し、`GET /jobs/{job_id}` で `COMPLETED` / `REVIEW_REQUIRED` への遷移を確認します。

---

## 11. 結果報告の検証

```bash
curl -s http://127.0.0.1:8765/jobs/<job_id>/result
```

以下の項目が `MuseReport` JSON および `data/reports/<job_id>.json` に含まれていることを確認し、ユーザーへ報告します。
- `material_name`, `executed_at`, `provider`, `model`, `processed_questions` / `total_questions`, `results`, `review_required_count`, `has_error`, `processing_time_seconds`, `api_usage`, `permitted_screenshot_paths`

---

## 12. エラー発生時の対応

| エラーコード | 原因 | 対処 |
|---|---|---|
| `ADMIN_TOKEN_NOT_CONFIGURED` / `INVALID_ADMIN_TOKEN` (401) | 管理トークン未設定または不一致 | `.env` の `MUSE_BRIDGE_ADMIN_TOKEN` と送信ヘッダを確認 |
| `PROVIDER_AUTH_ERROR` (401) | 外部 AI API キー無効（再試行せず停止） | Secure Credentials Store または環境変数を確認 |
| `PERMISSION_DENIED` (403) | AI 利用未許可教材、評価テスト自動提出要求、非 HTTPS URL | 許可済み教材・`https://` URL のみを使用 |
| `DUPLICATE_JOB` (409) | 同一教材・同一問題の二重実行 | 安全のためブロック済み。必要時は別の `idempotency_key` を指定 |
| `BUDGET_EXCEEDED` (429) | 1 ジョブ / 日次予算上限超過 | `GET /config` で上限値と消費量を確認 |
| `NETWORK_CONNECTION_ERROR` / `PROVIDER_TIMEOUT` (503/504) | 一時的な通信障害（最大 3 回自動再試行済み） | ネットワーク復旧後に再実行 |

---

### 使用量・モデル別処理時間・途中再開の確認（`v1.1.0`）

```bash
# API使用量・入力/出力/思考トークン・モデル別処理時間・無料枠残量の確認
./.venv/bin/python main.py usage
curl -s http://127.0.0.1:8765/usage

# 途中失敗したジョブの保存済み部分結果を再利用して未処理分のみ再開
curl -s -X POST "http://127.0.0.1:8765/jobs/<job_id>/resume?wait=true"
```

---

## 13. プログラムの更新方法

```bash
git pull origin main
./install.sh
./.venv/bin/python -m pytest -v
```
`install.sh` は既存の `.env` や `config.yaml` を上書きしないため、設定を維持したまま更新できます。

---

## 付録：Muse へ送る運用指示文例

```text
GitHubリポジトリ（https://github.com/<OWNER>/muse-learning-bridge）を導入前に確認し、
最初に AGENTS.md、MUSE_SPEC.md、MUSE_GUIDE.md、API_SPEC.md、SECURITY.md をすべて読んでください。
実環境では許可された教材のみを対象とし、パスワードやCookieはPythonへ渡さず、
Museに登録された外部AI API設定を使って解析・結果報告を行ってください。
```
