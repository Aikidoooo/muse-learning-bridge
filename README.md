# Muse Learning Bridge (`v1.0.0`)

Meta のパーソナル AI エージェント **Muse** を司令塔とし、学校・教育機関から AI 利用が許可された練習教材、復習教材、自作模擬テストを対象に、問題データの構造化・外部 AI API による解析・結果報告までを安全に自動化する Python システムです。

- **拡張機能不要**: Firefox / Chrome 拡張機能のインストールを必須とせず、Python + Playwright 単体で動作します。
- **エージェント向け文書構成**: AI エージェント（Muse 等）は実行前に必ず [`AGENTS.md`](./AGENTS.md) を確認してください。

---

## ドキュメント一覧

| ファイル | 用途 |
|---|---|
| [`AGENTS.md`](./AGENTS.md) | AI エージェント向けの必読順序・優先順位・絶対禁止事項 |
| [`MUSE_SPEC.md`](./MUSE_SPEC.md) | システム目的、Muse と Python の役割分担、仕様・制限事項 |
| [`MUSE_GUIDE.md`](./MUSE_GUIDE.md) | GitHub 取得、セットアップ、起動、動的設定、検証、更新マニュアル |
| [`API_SPEC.md`](./API_SPEC.md) | 全 HTTP API エンドポイント、認証、ステータス、エラーコード仕様 |
| [`SECURITY.md`](./SECURITY.md) | 認証情報保護、HTTPS 強制、PII マスキング、利用許可ポリシー |

---

## クイックスタート（Linux / Muse Secure VM）

```bash
git clone https://github.com/<OWNER>/muse-learning-bridge.git
cd muse-learning-bridge
chmod +x install.sh start.sh
./install.sh
./start.sh
```

- `install.sh` は管理者権限不要・冪等実行対応で、既存の `.env` や `config.yaml` を上書きしません。
- `start.sh` は初期状態で `127.0.0.1:8765` のみにバインドし、管理 API を外部ネットワークへ公開しません。

詳細な設定手順・API キー管理・Windows での検証手順は [`MUSE_GUIDE.md`](./MUSE_GUIDE.md) を参照してください。

---

## 対応外部 AI プロバイダー（動的切り替え対応）

- Google Gemini API (`gemini`)
- DeepSeek API (`deepseek`)
- OpenAI API (`openai`)
- Anthropic API (`anthropic`)
- OpenAI 互換 API (`openai_compatible` および動的登録カスタムプロバイダー)

Muse 自体の AI モデルは問題解析に使用せず、登録された外部 AI API のみを使用します。

---

## テストの実行

```bash
./.venv/bin/python -m pytest -v
```

---

## セキュリティと制限事項

- API キー・管理トークン・`.env`・実ログイン情報・Cookie・個人情報・ログ・SQLite DB・ブラウザプロファイルはリポジトリへ一切含みません。
- 学校の評価対象テストや AI 利用未許可教材への自動解答・提出はシステム側で拒否します。
- 実機 Meta Muse 環境および本番 Classi 環境・実外部 AI キーでの本番通信は未検証であり、同梱の自作模擬教材（`mock_test/`）と模擬クライアント（`src/mock_client/`）を用いて検証しています。

---

## ライセンス

[MIT License](./LICENSE)
