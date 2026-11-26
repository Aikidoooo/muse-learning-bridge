# セキュリティポリシー (`SECURITY.md`)

`Muse Learning Bridge` は、学習データの機密性・認証情報の保護・教育機関および利用サービスの規則遵守を最優先に設計されています。

---

## 1. 機密情報・認証情報の保護

1. **API キーと管理トークンの非保存**:
   - API キーおよび `MUSE_BRIDGE_ADMIN_TOKEN` を、ソースコード、チャット履歴、平文 JSON、`config.yaml`、SQLite データベース、実行ログへ保存・出力しません。
   - リクエストボディに `"api_key"` 等の平文キーを含める送信はスキーマ（`extra="forbid"`）により拒否します。
   - 実行時のキー受け渡しは **Muse Secure Credentials Store + Custom Connector による `X-Provider-Api-Key` ヘッダ注入**、プロセス内揮発メモリ（`RuntimeSecretStore` による起動時ランダムパッド XOR 難読化保持）、または OS 環境変数のみを使用します。
2. **認証情報・個人情報の入力遮断**:
   - `password`, `cookie`, `cookies`, `session_token`, `auth_token` 等の認証関連フィールドが入力 JSON に含まれる場合、`CredentialLeakageError`（HTTP 400）で即時拒否します。
   - ログ出力フィルタ（`SecretMaskingFilter`）およびレポート生成処理（`mask_pii` / `mask_secrets`）により、メールアドレス・電話番号・学籍番号・氏名ラベル・API キーパターンを自動マスキングします。
   - 結果画面スクリーンショットは `mask_screenshot_pii` により生徒氏名・ID 領域を黒塗りマスキングしてから保存・登録します。

---

## 2. 管理 API の認証と通信先制限

1. **管理エンドポイントの Fail-Closed 認証**:
   - `GET /config`, `PATCH /config`, `POST /providers/test`, `GET /providers/models`, `GET /credentials/status` はすべて `MUSE_BRIDGE_ADMIN_TOKEN` による `Authorization: Bearer`（または `X-Bridge-Admin-Token`）認証が必須です。
   - 環境変数 `MUSE_BRIDGE_ADMIN_TOKEN` が未設定の場合、安全のためすべての管理リクエストを `401 Unauthorized` で拒否します。
   - `start.sh` の既定バインド先は `127.0.0.1`（ローカルループバック）とし、初期状態で外部ネットワークへ公開しません。
2. **HTTPS 強制と SSRF 防止**:
   - 外部 AI プロバイダーの `base_url` は原則 `https://` のみ許可します（`http://` は `127.0.0.1` / `localhost` / `::1` のローカル検証用に限定）。
   - リンクローカルアドレスおよびクラウドメタデータ IP（`169.254.169.254` 等）への接続は禁止します。
3. **設定変更時の検証とロールバック**:
   - `PATCH /config` 実行時は接続テストが成功した場合のみ `config.yaml` を更新し、失敗した場合はメモリ設定・シークレット状態を自動ロールバックします。実行中ジョブには干渉しません。

---

## 3. 利用許可ゲート・予算管理・プロンプトインジェクション対策

- **利用許可ゲート**: `ai_usage_approved: true` が明示されていない教材、および `school_assessment` カテゴリの自動提出要求は `PermissionDeniedError`（HTTP 403）で即停止します。
- **画像送信制限**: `ai_usage_approved: true` の画像のみ外部 AI へ送信し、未許可画像は自動除外します。
- **予算上限**: `max_cost_per_job_usd`（1 ジョブ上限）および `max_daily_cost_usd`（日次上限）を超過した場合は `BudgetExceededError` で停止します。
- **プロンプトインジェクション対策**: 教材テキストはシステムプロンプトから隔離された `<untrusted_material_data>` ブロックとして渡し、Web ページ内の命令文をプログラムや AI への指示として解釈させません。
- **制限回避の禁止**: CAPTCHA、多要素認証、クロスオリジン `iframe`、ブラウザのセキュリティ制限を回避する機能は一切実装しません。
