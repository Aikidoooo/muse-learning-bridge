"""Muse向け最終結果レポートスキーマ（仕様書STEP 8準拠）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.schemas.analysis import QuestionAnalysisResult
from src.schemas.enums import CustomProviderName, JobStatus, ProviderName, parse_provider_ref
from src.schemas.job import ErrorDetail, TokenUsage


class MuseReport(BaseModel):
    """Museが取得しユーザーへ報告するための構造化レポート。"""

    job_id: str
    status: JobStatus
    material_id: str
    material_name: str
    executed_at: datetime
    provider: ProviderName | CustomProviderName | str
    model: str

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v: str | ProviderName) -> ProviderName | CustomProviderName:
        return parse_provider_ref(v)
    total_questions: int
    processed_questions: int
    results: list[QuestionAnalysisResult] = Field(default_factory=list)
    review_required_count: int = 0
    has_error: bool = False
    error: ErrorDetail | None = None
    processing_time_seconds: float = Field(default=0.0, ge=0.0)
    api_usage: TokenUsage = Field(default_factory=TokenUsage)
    permitted_screenshot_paths: list[str] = Field(default_factory=list)
    auto_submit_allowed: bool = False
    notes: list[str] = Field(default_factory=list)
