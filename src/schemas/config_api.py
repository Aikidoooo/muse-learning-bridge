"""動的設定管理・プロバイダー接続テスト・認証状態API用スキーマ。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PublicProviderConfigView(BaseModel):
    """外部返却用のプロバイダー設定ビュー（APIキー値は含めない）。"""

    name: str
    builtin: bool
    provider_type: str
    enabled: bool
    display_name: str
    default_model: str
    base_url: str
    api_key_env: str
    models: list[str]
    input_cost_per_1m_usd: float
    output_cost_per_1m_usd: float
    billing_tier: str = "free"
    rpm_limit: int = 15
    tpm_limit: int = 250000
    rpd_limit: int = 1000
    api_key_configured: bool
    credential_source: str


class PublicConfigResponse(BaseModel):
    """GET /config レスポンス（APIキーを含まない公開設定）。"""

    app: dict
    security: dict
    retry: dict
    optimization: dict = Field(default_factory=dict)
    default_provider: str
    providers: dict[str, PublicProviderConfigView]


class ProviderUpsertSpec(BaseModel):
    """プロバイダーの登録・更新用スペック（平文APIキーフィールドを禁止）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9_.-]+$")
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    default_model: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, min_length=8)
    api_key_env: str | None = Field(default=None, max_length=64)
    models: list[str] | None = None
    input_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    output_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    provider_type: Literal["gemini", "deepseek", "openai", "anthropic", "openai_compatible"] | None = None
    billing_tier: Literal["free", "paid"] | None = None
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    rpd_limit: int | None = Field(default=None, ge=0)


class ConfigPatchRequest(BaseModel):
    """PATCH /config リクエスト（平文APIキーのJSON送信を禁止）。"""

    model_config = ConfigDict(extra="forbid")

    default_provider: str | None = Field(default=None, min_length=2, max_length=40)
    default_model: str | None = Field(default=None, min_length=1)
    target_provider: str | None = Field(
        default=None,
        description="モデル変更やヘッダキー登録の対象プロバイダー（未指定時はdefault_provider）",
    )
    register_or_update_provider: ProviderUpsertSpec | None = None
    delete_provider: str | None = Field(default=None, min_length=2, max_length=40)
    save_header_api_key_in_memory: bool = Field(
        default=False,
        description="X-Provider-Api-Key ヘッダで渡されたキーをプロセス内揮発メモリへ保持するか",
    )
    max_cost_per_job_usd: float | None = Field(default=None, ge=0.0)
    max_daily_cost_usd: float | None = Field(default=None, ge=0.0)
    max_questions_per_job: int | None = Field(default=None, ge=1, le=500)
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=300.0)
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    allow_paid_api: bool | None = None
    enforce_free_tier_limits: bool | None = None
    thinking_mode: Literal["auto", "minimal", "balanced", "high"] | None = None
    batch_size: int | None = Field(default=None, ge=1, le=50)
    max_concurrency: int | None = Field(default=None, ge=1, le=10)
    enable_cache: bool | None = None
    verify_connection: bool = Field(
        default=True,
        description="設定確定前にAPI接続テストを実行し、失敗時にロールバックするか",
    )


class ProviderTestRequest(BaseModel):
    """POST /providers/test リクエスト。"""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, description="未指定時は既定プロバイダー")
    model: str | None = Field(default=None, description="未指定時はプロバイダーの既定モデル")
    base_url: str | None = Field(default=None, description="任意の上書きベースURL")


class ProviderTestResult(BaseModel):
    """POST /providers/test レスポンス。"""

    success: bool
    provider: str
    model: str
    base_url: str
    latency_ms: float
    credential_source: str
    discovered_models: list[str] = Field(default_factory=list)
    message: str


class ConfigPatchResponse(BaseModel):
    """PATCH /config レスポンス。"""

    updated: bool
    rolled_back: bool = False
    message: str
    connection_test: ProviderTestResult | None = None
    config: PublicConfigResponse


class ProviderModelsResponse(BaseModel):
    """GET /providers/models レスポンス。"""

    provider: str
    default_model: str
    models: list[str]
    source: Literal["remote", "configured"]
    note: str | None = None


class CredentialStatusItem(BaseModel):
    """プロバイダー別の認証情報登録状態（キー値は絶対に含めない）。"""

    provider: str
    display_name: str
    api_key_env: str
    configured: bool
    source: str
    header_injection_supported: bool = True
    required: bool = True


class CredentialsStatusResponse(BaseModel):
    """GET /credentials/status レスポンス。"""

    secure_storage_policy: str
    recommended_headers: list[str]
    items: list[CredentialStatusItem]
