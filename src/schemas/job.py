"""ジョブ管理およびAPIレスポンス用スキーマ。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.schemas.enums import CustomProviderName, JobStatus, ProviderName, parse_provider_ref
from src.schemas.question import MaterialInput


class TokenUsage(BaseModel):
    """APIトークン使用量・推定コスト・最適化効果メトリクス。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    thinking_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    api_requests_count: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    local_solved_count: int = Field(default=0, ge=0)
    provider_processing_time_seconds: float = Field(default=0.0, ge=0.0)


class ErrorDetail(BaseModel):
    """エラー詳細情報。"""

    code: str
    message: str
    retryable: bool = False
    details: dict = Field(default_factory=dict)


class JobCreateRequest(MaterialInput):
    """POST /jobs リクエストスキーマ（MaterialInputを継承）。"""

    pass


class JobStatusResponse(BaseModel):
    """GET /jobs/{job_id} レスポンススキーマ。"""

    job_id: str
    status: JobStatus
    material_id: str
    material_name: str
    provider: ProviderName | CustomProviderName | str
    model: str

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v: str | ProviderName) -> ProviderName | CustomProviderName:
        return parse_provider_ref(v)
    total_questions: int
    processed_questions: int = 0
    review_required_count: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    error: ErrorDetail | None = None


class ProviderInfo(BaseModel):
    """GET /providers の1プロバイダー情報。"""

    name: ProviderName | CustomProviderName | str
    display_name: str
    enabled: bool
    default_model: str
    models: list[str]
    api_key_configured: bool
    builtin: bool = True
    provider_type: str = "openai_compatible"
    credential_source: str = "none"

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, v: str | ProviderName) -> ProviderName | CustomProviderName:
        return parse_provider_ref(v)
