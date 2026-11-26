"""Phase 1: 基盤・設定・ログ・セキュリティ検証の自動テスト。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.core.config import load_config
from src.core.exceptions import CredentialLeakageError, PermissionDeniedError
from src.core.logging_config import SecretMaskingFilter, setup_logging
from src.core.security import mask_pii, mask_secrets, verify_material_permission
from src.schemas import JobStatus, MaterialInput, QuestionAnalysisResult, UncertaintyLevel


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    """config.yaml が正しく読み込まれ、設定モデルへマッピングされることを確認。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    assert cfg.app.name == "Muse Learning Bridge"
    assert cfg.providers.default_provider == "gemini"
    assert cfg.retry.max_attempts == 3
    assert cfg.security.require_explicit_ai_approval is True


def test_mask_secrets_and_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    """APIキー・トークンおよび個人情報が適切にマスキングされることを確認。"""
    dummy_gemini = "AIza" + "SyTestSecretKey1234567890"
    dummy_bearer = "sk-" + "secret-token-abcdef123456"
    monkeypatch.setenv("GEMINI_API_KEY", dummy_gemini)
    raw = (
        f"Authorization: Bearer {dummy_bearer} "
        f"key={dummy_gemini} "
        "mail=student01@example.ac.jp tel=090-1234-5678 学籍番号: A2026001 氏名: 山田太郎"
    )
    masked = mask_pii(mask_secrets(raw))
    assert dummy_gemini not in masked
    assert dummy_bearer not in masked
    assert "student01@example.ac.jp" not in masked
    assert "090-1234-5678" not in masked
    assert "A2026001" not in masked
    assert "山田太郎" not in masked


def test_logging_filter_masks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ロガー経由でAPIキーを出力しようとしてもファイルに平文記録されないことを確認。"""
    dummy_openai = "sk-" + "proj-supersecret999999"
    monkeypatch.setenv("OPENAI_API_KEY", dummy_openai)
    logger = setup_logging(logs_dir=tmp_path, level="INFO")
    logger.info("Using key %s for test", dummy_openai)
    for handler in logger.handlers:
        handler.flush()
        handler.close()

    log_text = (tmp_path / "muse_bridge.log").read_text(encoding="utf-8")
    assert dummy_openai not in log_text
    assert "***MASKED" in log_text


def test_material_input_rejects_forbidden_credentials() -> None:
    """Classi認証情報やCookieを含む入力がスキーマ検証段階で拒否されることを確認。"""
    with pytest.raises(CredentialLeakageError):
        MaterialInput.model_validate(
            {
                "material_id": "mat-001",
                "material_name": "英語練習問題",
                "ai_usage_approved": True,
                "cookies": "session=xyz",
                "questions": [
                    {
                        "question_id": "q1",
                        "question_number": "問1",
                        "prompt": "Choose the correct word.",
                    }
                ],
            }
        )


def test_verify_material_permission_blocks_unapproved_and_assessment() -> None:
    """AI利用未許可の教材や、評価テストの自動提出が拒否されることを確認。"""
    with pytest.raises(PermissionDeniedError):
        verify_material_permission(
            ai_usage_approved=False,
            material_category="practice",
        )

    with pytest.raises(PermissionDeniedError):
        verify_material_permission(
            ai_usage_approved=True,
            material_category="school_assessment",
            auto_submit_requested=True,
            auto_submit_allowed=True,
        )

    # 許可済み練習教材で自動提出なしの場合は通過
    verify_material_permission(
        ai_usage_approved=True,
        material_category="practice",
        auto_submit_requested=False,
        auto_submit_allowed=False,
    )


def test_job_status_enum_and_analysis_schema() -> None:
    """仕様書定義の8ステータスとAI出力スキーマが機能することを確認。"""
    assert len(list(JobStatus)) == 8
    result = QuestionAnalysisResult(
        question_id="q1",
        answer_candidates=["2"],
        explanation="主語が三人称単数現在のため動詞にsが付く。",
        uncertainty=UncertaintyLevel.LOW,
        confidence=0.95,
        needs_review=False,
    )
    assert result.question_id == "q1"
    assert result.needs_review is False
