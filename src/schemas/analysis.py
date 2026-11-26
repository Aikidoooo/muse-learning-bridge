"""外部AI API解析結果の検証用Pydanticスキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from src.schemas.enums import CustomProviderName, ProviderName, UncertaintyLevel, parse_provider_ref


class QuestionAnalysisResult(BaseModel):
    """1問あたりのAI解析結果（仕様書STEP 5準拠）。"""

    question_id: str = Field(..., min_length=1, description="対象問題ID")
    answer_candidates: list[str] = Field(
        default_factory=list,
        description="解答候補リスト（複数選択や候補提示に対応）",
    )
    explanation: str = Field(..., min_length=1, description="解答の根拠・解説")
    uncertainty: UncertaintyLevel = Field(
        default=UncertaintyLevel.MEDIUM,
        description="不確実性（low / medium / high）",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="AIの確信度（0.0〜1.0）",
    )
    needs_review: bool = Field(
        default=False,
        description="人間またはMuseによる追加確認が必要か",
    )
    review_reason: str | None = Field(
        default=None,
        description="追加確認が必要な理由",
    )


class AIBatchPayload(BaseModel):
    """外部AI APIからJSONとして返却されるバッチ構造。"""

    results: list[QuestionAnalysisResult] = Field(default_factory=list)


class ProviderCallResult(BaseModel):
    """プロバイダー呼び出し全体の正規化レスポンス。"""

    provider: ProviderName | CustomProviderName | str
    model: str

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v: str | ProviderName) -> ProviderName | CustomProviderName:
        return parse_provider_ref(v)
    results: list[QuestionAnalysisResult]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    thinking_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    api_requests_count: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    local_solved_count: int = Field(default=0, ge=0)
    deduplicated_count: int = Field(default=0, ge=0)
    processing_time_seconds: float = Field(default=0.0, ge=0.0)
    raw_valid: bool = Field(default=True)
    warnings: list[str] = Field(default_factory=list)
