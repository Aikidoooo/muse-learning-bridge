# API 仕様書 (`API_SPEC.md`)

ベース URL（ローカル既定）: `http://127.0.0.1:8765`  
OpenAPI スキーマ: `GET /openapi.json` / Swagger UI: `GET /docs`

---

## 1. 認証方法

### 管理 API 認証（`GET /config`, `PATCH /config`, `POST /providers/test`, `GET /providers/models`, `GET /credentials/status`）

環境変数 `MUSE_BRIDGE_ADMIN_TOKEN` と一致するトークンを以下のいずれかのヘッダで指定します（未設定時・不一致時は HTTP `401` で拒否）。

- `Authorization: Bearer <MUSE_BRIDGE_ADMIN_TOKEN>`
- `X-Bridge-Admin-Token: <MUSE_BRIDGE_ADMIN_TOKEN>`

### 外部 AI API キーのヘッダ注入（任意・Custom Connector 推奨）

`PATCH /config`, `POST /providers/test`, `GET /providers/models`, `POST /jobs` では、以下のヘッダで一時的な API キーを渡せます（ディスクやログには保存されません）。

- `X-Provider-Api-Key: <PROVIDER_API_KEY>`

---

## 2. ジョブステータスと完了判定方法

### ジョブステータス（8 種）

| ステータス | 意味 | 終了状態 |
|---|---|---|
| `WAITING` | ジョブ発行済み・実行待機中 | いいえ |
| `INITIALIZING` | 設定スナップショット・プロバイダー初期化中 | いいえ |
| `READY` | 実行準備完了 | いいえ |
| `PROCESSING` | 外部 AI API 解析実行中 | いいえ |
| `REVIEW_REQUIRED` | 完了（確認が必要な問題が 1 件以上あり） | **はい** |
| `COMPLETED` | 正常完了（全問確定） | **はい** |
| `FAILED` | 異常終了（`error` フィールド参照） | **はい** |
| `CANCELLED` | 中止完了 | **はい** |

### 完了判定方法

1. **HTTP API ポーリング方式**: `POST /jobs`（`wait=false` 既定）で `job_id` を受け取り、`GET /jobs/{job_id}` の `status` が終了状態（`COMPLETED`, `REVIEW_REQUIRED`, `FAILED`, `CANCELLED`）になるまでポーリングします。完了後 `GET /jobs/{job_id}/result` で詳細レポートを取得します（同期待機したい場合は `POST /jobs?wait=true` を使用）。
2. **CLI 方式**: `python main.py status <job_id>` / `python main.py result <job_id>`、または単発実行 `python main.py run --input input.json` の終了コード（`0`: エラーなし、`1`: エラー発生）と標準出力 JSON で判定します。

---

## 3. エンドポイント一覧

### `GET /health`（認証不要）
サーバー稼働状況を確認します。
- **レスポンス (`200 OK`)**: `{"status": "ok", "app_name": "Muse Learning Bridge", "version": "1.0.0", "default_provider": "gemini"}`

### `GET /providers`（認証不要）
登録済みプロバイダー（組み込み 5 種 + カスタム登録分）の一覧を返します。API キー文字列は含みません。
- **レスポンス (`200 OK`)**: `ProviderInfo` の配列（`name`, `display_name`, `enabled`, `default_model`, `models`, `api_key_configured`, `builtin`, `provider_type`, `credential_source`）

### `GET /config`（管理認証必須）
現在の公開設定（`app`, `security`, `retry`, `default_provider`, `providers`）を取得します。API キー値は一切返却しません。

### `PATCH /config`（管理認証必須）
プロバイダー登録・変更・削除、モデル名・デフォルト設定・予算上限・タイムアウト変更を実行します。変更対象プロバイダーへの接続テストが成功した場合のみ `config.yaml` へ保存・反映し、失敗時は自動ロールバックします。実行中のジョブには影響しません。
- **リクエスト JSON (`ConfigPatchRequest`, `extra="forbid"`)**:
  - `default_provider`, `default_model`, `target_provider`
  - `register_or_update_provider`: `{name, display_name, enabled, default_model, base_url, api_key_env, models, input_cost_per_1m_usd, output_cost_per_1m_usd, provider_type}`
  - `delete_provider`
  - `save_header_api_key_in_memory` (`bool`)
  - `max_cost_per_job_usd`, `max_daily_cost_usd`, `max_questions_per_job`, `timeout_seconds`, `max_attempts`, `verify_connection`
- **使用例**:
  ```bash
  curl -X PATCH http://127.0.0.1:8765/config \
    -H "Authorization: Bearer $MUSE_BRIDGE_ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"default_provider":"openai","default_model":"gpt-4o-mini","max_cost_per_job_usd":0.3}'
  ```

### `POST /providers/test`（管理認証必須）
指定プロバイダー・モデル・任意 `base_url` への疎通テストを実行します。
- **リクエスト JSON**: `{"provider": "gemini", "model": "gemini-2.5-flash", "base_url": null}`
- **レスポンス (`200 OK`)**: `{"success": true, "provider": "gemini", "model": "gemini-2.5-flash", "base_url": "...", "latency_ms": 120.5, "credential_source": "environment", "discovered_models": [...], "message": "..."}`

### `GET /providers/models`（管理認証必須）
プロバイダーが対応するモデル一覧を取得します（クエリ: `?provider=gemini&remote=true`）。リモート API 取得不可時は設定済みモデルへフォールバックし `source: "configured"` を返します。

### `GET /credentials/status`（管理認証必須）
各プロバイダーの認証情報登録有無（`configured: bool`）と取得元（`source`: `"request_header"` / `"runtime_secret"` / `"environment"` / `"none"`）を返します。キー文字列・プレフィックスは返却しません。

### `POST /jobs`（認証不要・`wait=false` 既定）
問題データ（`MaterialInput`）を受け取り、ジョブを発行（`202 Accepted`）します。`cookies` や `password` 等の禁止フィールドが含まれる場合や `ai_usage_approved != true` の場合は拒否します。
- **リクエスト JSON**: `material_id`, `material_name`, `material_category`, `ai_usage_approved`, `auto_submit_allowed`, `auto_submit_requested`, `provider`, `model`, `idempotency_key`, `questions`

### `GET /jobs`（認証不要）
最近のジョブ一覧（クエリ: `?limit=20`, 最大 100）を返します。

### `GET /jobs/{job_id}`（認証不要）
ジョブの現在ステータス（`JobStatusResponse`）を返します。

### `GET /jobs/{job_id}/events`（認証不要）
ジョブのステータス遷移履歴（`[{"status": "...", "detail": ..., "created_at": "..."}]`）を時系列で返します。

### `GET /jobs/{job_id}/result`（認証不要）
完了済みジョブの結果レポート（`MuseReport`: 教材名、実行日時、モデル、問題数、解析結果配列、要確認数、エラー、処理時間、API 使用量、マスク済みスクリーンショットパス等）を返します。未完了時は HTTP `409` (`JOB_NOT_COMPLETED`)。

### `POST /jobs/{job_id}/cancel`（認証不要）
実行中または待機中ジョブを `CANCELLED` へ移行します。

### `POST /jobs/{job_id}/screenshots`（認証不要）
許可された結果画面（マスキング済み）のスクリーンショットパスをレポートへ登録します。
- **リクエスト JSON**: `{"screenshot_paths": ["data/screenshots/job-xxx_masked.png"]}`

---

## 4. エラーレスポンス形式とエラーコード

```json
{
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "学校によるAI利用が許可されていない教材のため処理を中止しました。",
    "retryable": false,
    "details": {}
  }
}
```

| エラーコード | HTTP | 再試行 | 意味 |
|---|---|---|---|
| `ADMIN_TOKEN_NOT_CONFIGURED` / `INVALID_ADMIN_TOKEN` | 401 | 不可 | 管理 API 認証未設定・トークン不正 |
| `LOGIN_FAILED` | 401 | 不可 | ポータルログイン失敗 |
| `PROVIDER_AUTH_ERROR` | 401 | 不可 | 外部 AI API キー無効・認証失敗 |
| `PERMISSION_DENIED` | 403 | 不可 | AI 利用未許可、評価テスト自動提出要求、外部 HTTP URL 拒否 |
| `MATERIAL_NOT_FOUND` / `JOB_NOT_FOUND` | 404 | 不可 | 対象教材またはジョブ ID 不在 |
| `CREDENTIAL_DATA_REJECTED` | 400 | 不可 | 認証情報・Cookie フィールドの混入を検出 |
| `PROVIDER_NOT_FOUND` / `INVALID_PROVIDER_URL` / `INVALID_PROVIDER_SPEC` / `TOO_MANY_QUESTIONS` | 400 | 不可 | 無効なプロバイダー・URL・設定・問題数超過 |
| `DUPLICATE_JOB` / `JOB_NOT_COMPLETED` / `JOB_CANCELLED` | 409 | 条件付 | 二重実行拒否、未完了結果取得、ジョブ中止 |
| `PAGE_STRUCTURE_CHANGED` | 422 | 不可 | DOM 構造変更または未対応問題形式のみで抽出不能 |
| `BUDGET_EXCEEDED` | 429 | 不可 | 1 ジョブまたは日次の API 料金上限超過 |
| `PROVIDER_RATE_LIMIT` | 429 | 可 (最大3回) | 外部 AI API レート制限 |
| `INVALID_JSON_FORMAT` / `INVALID_AI_RESPONSE` / `MUSE_COMMUNICATION_ERROR` | 502 | 条件付 | AI 応答 JSON 破損・通信切断 |
| `NETWORK_CONNECTION_ERROR` | 503 | 可 (最大3回) | ネットワーク・外部 AI サーバーエラー |
| `PROVIDER_TIMEOUT` | 504 | 可 (最大3回) | 外部 AI 通信タイムアウト |
