"""設定ファイル（config.yaml）および環境変数（.env）の読み込み・検証・保存。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from src.core.secret_store import GLOBAL_RUNTIME_SECRETS, CredentialSource


class AppSectionConfig(BaseModel):
    """アプリケーション基本設定。"""

    name: str = "Muse Learning Bridge"
    version: str = "1.0.0"
    environment: str = "local"
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: str = "data"
    logs_dir: str = "logs"
    db_path: str = "data/muse_bridge.db"
    log_level: str = "INFO"


class SecuritySectionConfig(BaseModel):
    """セキュリティおよび予算上限設定。"""

    require_explicit_ai_approval: bool = True
    allow_auto_submit_only_for_permitted: bool = True
    reject_credential_fields: bool = True
    allow_http_for_localhost: bool = True
    max_cost_per_job_usd: float = Field(default=0.50, ge=0.0)
    max_daily_cost_usd: float = Field(default=5.00, ge=0.0)
    max_questions_per_job: int = Field(default=100, ge=1)
    mask_pii_in_logs_and_reports: bool = True


class RetrySectionConfig(BaseModel):
    """リトライ・タイムアウト設定。"""

    max_attempts: int = Field(default=3, ge=1, le=5)
    base_delay_seconds: float = Field(default=1.0, gt=0.0)
    max_delay_seconds: float = Field(default=8.0, gt=0.0)
    timeout_seconds: float = Field(default=45.0, gt=0.0)


class ProviderEntryConfig(BaseModel):
    """各AIプロバイダーの設定（APIキー値自体はここへ保存しない）。"""

    enabled: bool = True
    display_name: str
    default_model: str
    base_url: str
    base_url_env: str | None = None
    api_key_env: str = ""
    models: list[str] = Field(default_factory=list)
    input_cost_per_1m_usd: float = Field(default=0.0, ge=0.0)
    output_cost_per_1m_usd: float = Field(default=0.0, ge=0.0)
    provider_type: str | None = None

    def resolve_base_url(self) -> str:
        """環境変数オーバーライドを考慮したベースURLを返す。"""
        if self.base_url_env:
            env_url = os.getenv(self.base_url_env, "").strip()
            if env_url:
                return env_url.rstrip("/")
        return self.base_url.rstrip("/")

    def resolve_api_key(
        self,
        provider_name: str = "",
        *,
        header_api_key: str | None = None,
        secret_store: Any = None,
    ) -> str:
        """ヘッダ -> 実行時セキュアメモリ -> 環境変数 の優先順でAPIキーを取得する。"""
        store = secret_store or GLOBAL_RUNTIME_SECRETS
        if provider_name:
            key, _ = store.resolve_with_source(
                provider_name,
                env_var=self.api_key_env,
                header_api_key=header_api_key,
            )
            return key
        if header_api_key and header_api_key.strip():
            return header_api_key.strip()
        if not self.api_key_env:
            return ""
        return os.getenv(self.api_key_env, "").strip()

    def resolve_credential_status(
        self,
        provider_name: str,
        *,
        header_api_key: str | None = None,
        secret_store: Any = None,
    ) -> tuple[bool, CredentialSource]:
        """キー文字列を返さずに、認証情報の登録有無と取得元のみを返す。"""
        store = secret_store or GLOBAL_RUNTIME_SECRETS
        key, source = store.resolve_with_source(
            provider_name,
            env_var=self.api_key_env,
            header_api_key=header_api_key,
        )
        return bool(key), source


class ProvidersSectionConfig(BaseModel):
    """全AIプロバイダー設定のコンテナ（組み込み5種 + 動的登録カスタムプロバイダー）。"""

    default_provider: str = "gemini"
    gemini: ProviderEntryConfig
    deepseek: ProviderEntryConfig
    openai: ProviderEntryConfig
    anthropic: ProviderEntryConfig
    openai_compatible: ProviderEntryConfig
    custom_providers: dict[str, ProviderEntryConfig] = Field(default_factory=dict)

    def get_provider_config(self, name: str) -> ProviderEntryConfig | None:
        """名前からプロバイダー設定を取得する。"""
        normalized = name.strip().lower()
        builtins: dict[str, ProviderEntryConfig] = {
            "gemini": self.gemini,
            "deepseek": self.deepseek,
            "openai": self.openai,
            "anthropic": self.anthropic,
            "openai_compatible": self.openai_compatible,
        }
        if normalized in builtins:
            return builtins[normalized]
        return self.custom_providers.get(normalized)

    def all_providers(self) -> dict[str, ProviderEntryConfig]:
        """全プロバイダー設定の辞書を返す。"""
        merged: dict[str, ProviderEntryConfig] = {
            "gemini": self.gemini,
            "deepseek": self.deepseek,
            "openai": self.openai,
            "anthropic": self.anthropic,
            "openai_compatible": self.openai_compatible,
        }
        for name, entry in self.custom_providers.items():
            merged[name] = entry
        return merged

    def is_builtin(self, name: str) -> bool:
        """組み込みプロバイダーかどうかを返す。"""
        return name.strip().lower() in {
            "gemini",
            "deepseek",
            "openai",
            "anthropic",
            "openai_compatible",
        }


class AppConfig(BaseModel):
    """アプリケーション全体設定モデル。"""

    app: AppSectionConfig
    security: SecuritySectionConfig
    retry: RetrySectionConfig
    providers: ProvidersSectionConfig


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
) -> AppConfig:
    """設定YAMLと.envを読み込み、検証済みAppConfigを返す。"""
    if env_path is not None:
        env_file = Path(env_path)
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=False)

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {cfg_file}")

    raw_text = cfg_file.read_text(encoding="utf-8")
    raw_data: dict[str, Any] = yaml.safe_load(raw_text) or {}

    config = AppConfig.model_validate(raw_data)

    # 環境変数によるサーバー設定の上書き
    env_host = os.getenv("MUSE_BRIDGE_HOST", "").strip()
    if env_host:
        config.app.host = env_host

    env_port = os.getenv("MUSE_BRIDGE_PORT", "").strip()
    if env_port.isdigit():
        config.app.port = int(env_port)

    env_log_level = os.getenv("MUSE_BRIDGE_LOG_LEVEL", "").strip()
    if env_log_level:
        config.app.log_level = env_log_level.upper()

    # 必要なデータ・ログディレクトリを事前に作成
    Path(config.app.data_dir).mkdir(parents=True, exist_ok=True)
    Path(config.app.logs_dir).mkdir(parents=True, exist_ok=True)
    Path(config.app.db_path).parent.mkdir(parents=True, exist_ok=True)

    return config


def save_config(config: AppConfig, config_path: str | Path = "config.yaml") -> Path:
    """非機密設定のみをYAMLファイルへ原子的に永続化する（APIキーは一切書き込まない）。"""
    target = Path(config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    yaml_text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    tmp_file = target.with_suffix(f"{target.suffix}.tmp")
    tmp_file.write_text(yaml_text, encoding="utf-8")
    tmp_file.replace(target)
    return target
