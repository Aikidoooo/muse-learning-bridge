"""システム共通の列挙型定義。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


class CustomProviderName(str):
    """動的登録されたカスタムプロバイダー名（ProviderNameと同じ .value インタフェースを提供）。"""

    @property
    def value(self) -> str:
        """文字列表現を返す。"""
        return str(self)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Pydantic v2で文字列スキーマとして検証・生成できるよう定義する。"""
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class JobStatus(StrEnum):
    """ジョブの実行ステータス（仕様書第7章準拠）。"""

    WAITING = "WAITING"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProviderName(StrEnum):
    """対応する組み込み外部AI APIプロバイダー名。"""

    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


def parse_provider_ref(value: str | ProviderName) -> ProviderName | CustomProviderName:
    """プロバイダー名文字列を組み込みProviderNameまたはCustomProviderNameへ正規化する。"""
    if isinstance(value, ProviderName):
        return value
    clean = str(value).strip().lower()
    try:
        return ProviderName(clean)
    except ValueError:
        return CustomProviderName(clean)


class QuestionFormat(StrEnum):
    """問題の出題形式。"""

    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    DROPDOWN = "dropdown"
    SHORT_TEXT = "short_text"
    ESSAY = "essay"
    TRUE_FALSE = "true_false"
    UNKNOWN = "unknown"


class MaterialCategory(StrEnum):
    """教材カテゴリ（自動提出やAI利用ポリシー判定に使用）。"""

    PRACTICE = "practice"
    REVIEW = "review"
    MOCK_TEST = "mock_test"
    SCHOOL_ASSESSMENT = "school_assessment"
    UNKNOWN = "unknown"


class UncertaintyLevel(StrEnum):
    """AI解析結果の不確実性レベル。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
