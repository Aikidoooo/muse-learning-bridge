"""Phase 5 & 6: ジョブ状態遷移・重複防止・予算上限・画像/レポート個人情報マスキングの自動テスト。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from src.core.config import AppConfig, load_config
from src.core.exceptions import BudgetExceededError, DuplicateJobError
from src.jobs.manager import JobManager
from src.reports.generator import ReportGenerator
from src.reports.image_masking import MaskRegion, mask_screenshot_pii
from src.schemas.analysis import ProviderCallResult, QuestionAnalysisResult
from src.schemas.enums import JobStatus, ProviderName, UncertaintyLevel
from src.schemas.question import MaterialInput, QuestionItem


def _cfg(tmp_path: Path) -> AppConfig:
    """テスト用設定を返す。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.app.logs_dir = str(tmp_path / "logs")
    cfg.app.db_path = str(tmp_path / "data" / "phase5.db")
    return cfg


def _material(mat_id: str = "mat-01", name: str = "復習テスト") -> MaterialInput:
    """テスト用教材入力を生成する。"""
    return MaterialInput(
        material_id=mat_id,
        material_name=name,
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="q1",
                question_number="問1",
                prompt="日本の首都はどこですか？",
                choices=["大阪", "東京", "京都"],
            )
        ],
    )


@pytest.mark.asyncio
async def test_status_transition_history_and_duplicate_prevention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAITING->INITIALIZING->READY->PROCESSING->COMPLETEDの遷移と二重実行防止を確認。"""

    class FakeProvider:
        provider_name = ProviderName.GEMINI
        model = "gemini-2.5-flash"

        async def analyze(self, material: MaterialInput) -> ProviderCallResult:
            return ProviderCallResult(
                provider=ProviderName.GEMINI,
                model="gemini-2.5-flash",
                results=[
                    QuestionAnalysisResult(
                        question_id="q1",
                        answer_candidates=["東京"],
                        explanation="日本の首都は東京です。",
                        uncertainty=UncertaintyLevel.LOW,
                        confidence=0.99,
                        needs_review=False,
                    )
                ],
                input_tokens=40,
                output_tokens=20,
                total_tokens=60,
                estimated_cost_usd=0.0001,
            )

    monkeypatch.setattr("src.jobs.manager.create_provider", lambda *a, **kw: FakeProvider())
    manager = JobManager(_cfg(tmp_path))

    mat = _material()
    job = manager.create_job(mat)
    assert job.status == JobStatus.WAITING

    # 同一教材の二重実行はDuplicateJobErrorになること
    with pytest.raises(DuplicateJobError):
        manager.create_job(mat)

    done = await manager.execute_job(job.job_id)
    assert done.status == JobStatus.COMPLETED

    events = manager.get_events(job.job_id)
    statuses = [e["status"] for e in events]
    assert statuses == [
        JobStatus.WAITING.value,
        JobStatus.INITIALIZING.value,
        JobStatus.READY.value,
        JobStatus.PROCESSING.value,
        JobStatus.COMPLETED.value,
    ]


@pytest.mark.asyncio
async def test_budget_limit_per_job_and_daily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ジョブ単価上限および日次予算上限の超過が正しくFAILED/BudgetExceededErrorになることを確認。"""

    class ExpensiveProvider:
        provider_name = ProviderName.OPENAI
        model = "gpt-4o"

        async def analyze(self, material: MaterialInput) -> ProviderCallResult:
            return ProviderCallResult(
                provider=ProviderName.OPENAI,
                model="gpt-4o",
                results=[
                    QuestionAnalysisResult(
                        question_id="q1",
                        answer_candidates=["東京"],
                        explanation="解説",
                        uncertainty=UncertaintyLevel.LOW,
                        confidence=0.9,
                        needs_review=False,
                    )
                ],
                input_tokens=100000,
                output_tokens=50000,
                total_tokens=150000,
                estimated_cost_usd=1.25,
            )

    monkeypatch.setattr("src.jobs.manager.create_provider", lambda *a, **kw: ExpensiveProvider())
    cfg = _cfg(tmp_path)
    cfg.security.max_cost_per_job_usd = 0.50
    cfg.security.max_daily_cost_usd = 0.80
    manager = JobManager(cfg)

    job = manager.create_job(_material("mat-exp-1"))
    failed_job = await manager.execute_job(job.job_id)
    assert failed_job.status == JobStatus.FAILED
    assert failed_job.error is not None
    assert failed_job.error.code == "BUDGET_EXCEEDED"

    # 日次累計を増やして事前日次上限超過を検証
    manager.store.insert_job(
        job_id="job-past",
        idempotency_key="past-key",
        material_id="mat-past",
        material_name="過去教材",
        provider="openai",
        model="gpt-4o",
        status=JobStatus.COMPLETED,
        total_questions=1,
        input_json="{}",
    )
    from src.schemas.job import TokenUsage

    manager.store.update_status(
        "job-past",
        JobStatus.COMPLETED,
        token_usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, estimated_cost_usd=0.90),
    )
    with pytest.raises(BudgetExceededError):
        manager.create_job(_material("mat-exp-2"))


@pytest.mark.asyncio
async def test_mid_execution_cancel_transitions_to_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """処理中ジョブのキャンセル時にCANCELLED状態へ安全に移行することを確認。"""

    class SlowProvider:
        provider_name = ProviderName.GEMINI
        model = "gemini-2.5-flash"

        async def analyze(self, material: MaterialInput) -> ProviderCallResult:
            await asyncio.sleep(0.5)
            return ProviderCallResult(
                provider=ProviderName.GEMINI,
                model="gemini-2.5-flash",
                results=[],
            )

    monkeypatch.setattr("src.jobs.manager.create_provider", lambda *a, **kw: SlowProvider())
    manager = JobManager(_cfg(tmp_path))
    job = manager.create_job(_material("mat-slow"))
    task = manager.start_background_job(job.job_id)
    await asyncio.sleep(0.05)

    cancelled = manager.cancel_job(job.job_id)
    assert cancelled.status == JobStatus.CANCELLED
    final = await task
    assert final.status == JobStatus.CANCELLED


def test_report_pii_masking_and_screenshot_masking(tmp_path: Path) -> None:
    """レポート内PIIマスキングと結果画像の指定領域黒塗りマスキングを確認。"""
    gen = ReportGenerator(tmp_path / "data", mask_pii_enabled=True)
    mat = _material("mat-pii", name="英語テスト 氏名: 佐藤花子 student@school.jp")
    res = ProviderCallResult(
        provider=ProviderName.GEMINI,
        model="gemini-2.5-flash",
        results=[
            QuestionAnalysisResult(
                question_id="q1",
                answer_candidates=["東京"],
                explanation="受験番号: S2026999 の回答根拠。",
                uncertainty=UncertaintyLevel.LOW,
                confidence=0.95,
                needs_review=False,
            )
        ],
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        estimated_cost_usd=0.0001,
    )
    report = gen.build_report(
        job_id="job-pii-1",
        status=JobStatus.COMPLETED,
        material=mat,
        provider=ProviderName.GEMINI,
        model="gemini-2.5-flash",
        processing_time_seconds=0.42,
        provider_result=res,
    )
    assert "佐藤花子" not in report.material_name
    assert "student@school.jp" not in report.material_name
    assert "S2026999" not in report.results[0].explanation
    summary = gen.format_user_summary(report)
    assert "教材名:" in summary

    # 画像マスキング検証（白い画像を生成し、指定座標が黒塗りされるか確認）
    raw_img = tmp_path / "raw_result.png"
    Image.new("RGB", (400, 200), color=(255, 255, 255)).save(raw_img)
    masked_img = mask_screenshot_pii(
        raw_img,
        [MaskRegion(x=250, y=10, width=130, height=30)],
    )
    assert masked_img.exists()
    with Image.open(masked_img) as check:
        # マスク領域内は暗い色、領域外は白のまま
        assert check.getpixel((280, 20)) == (24, 32, 46)
        assert check.getpixel((20, 20)) == (255, 255, 255)
