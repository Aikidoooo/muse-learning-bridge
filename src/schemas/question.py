"""問題データおよび教材入力用Pydanticスキーマ。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.core.security import scan_forbidden_credentials
from src.schemas.enums import (
    CustomProviderName,
    MaterialCategory,
    ProviderName,
    QuestionFormat,
    parse_provider_ref,
)


class QuestionImage(BaseModel):
    """問題に紐づく画像データ（利用許可済みのみ送信可能）。"""

    image_id: str = Field(..., min_length=1, description="画像識別子")
    url: str | None = Field(default=None, description="画像URL")
    base64_data: str | None = Field(default=None, description="Base64エンコード画像データ")
    mime_type: str = Field(default="image/png", description="MIMEタイプ")
    alt_text: str | None = Field(default=None, description="代替テキスト")
    ai_usage_approved: bool = Field(
        default=False,
        description="この画像を外部AI APIへ送信することが許可されているか",
    )


class QuestionItem(BaseModel):
    """1問分の構造化データ（仕様書STEP 4準拠）。"""

    question_id: str = Field(..., min_length=1, description="問題一意ID")
    question_number: str = Field(..., min_length=1, description="問題番号表示（例: Q1, 問1）")
    prompt: str = Field(..., min_length=1, description="問題文")
    choices: list[str] = Field(default_factory=list, description="選択肢一覧")
    question_format: QuestionFormat = Field(
        default=QuestionFormat.UNKNOWN,
        description="問題形式",
    )
    images: list[QuestionImage] = Field(default_factory=list, description="問題画像")
    supplementary_info: dict[str, Any] = Field(
        default_factory=dict,
        description="共通指示や配点などの補足情報",
    )


class MaterialInput(BaseModel):
    """MuseからPythonへ渡される教材解析リクエスト全体。"""

    material_id: str = Field(..., min_length=1, description="教材識別子")
    material_name: str = Field(..., min_length=1, description="教材名")
    material_category: MaterialCategory = Field(
        default=MaterialCategory.PRACTICE,
        description="教材カテゴリ",
    )
    source_url: str | None = Field(default=None, description="教材ページのURL")
    ai_usage_approved: bool = Field(
        default=False,
        description="学校によりAI利用が許可されているか",
    )
    auto_submit_allowed: bool = Field(
        default=False,
        description="自動解答・提出が明示的に許可されているか",
    )
    auto_submit_requested: bool = Field(
        default=False,
        description="Muse側で結果画面までの自動提出検証を希望するか",
    )
    permission_note: str | None = Field(
        default=None,
        description="利用許可の根拠メモ",
    )
    provider: ProviderName | str | None = Field(
        default=None,
        description="使用する外部AIプロバイダー（未指定時はconfigのデフォルト）",
    )
    model: str | None = Field(
        default=None,
        description="使用するAIモデル名（未指定時はプロバイダーのデフォルト）",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="二重実行防止用キー（未指定時は教材ID+問題ハッシュで自動生成）",
    )
    allow_paid_api: bool | None = Field(
        default=None,
        description="有料API使用をこのジョブ単位で明示許可するか（未指定時はconfig.security.allow_paid_apiに従う）",
    )
    thinking_mode: str | None = Field(
        default=None,
        description="思考レベルのジョブ単位指定（auto / minimal / balanced / high）",
    )
    questions: list[QuestionItem] = Field(
        ...,
        min_length=1,
        description="解析対象の問題リスト",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_credentials(cls, data: Any) -> Any:
        """入力データ内にClassi認証情報やCookieが含まれていないことを検証する。"""
        scan_forbidden_credentials(data)
        return data
