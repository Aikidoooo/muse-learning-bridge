"""Muse向けJSONレポートの生成・個人情報マスキング・保存モジュール。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.security import mask_pii
from src.schemas.analysis import ProviderCallResult, QuestionAnalysisResult
from src.schemas.enums import JobStatus, ProviderName
from src.schemas.job import ErrorDetail, TokenUsage
from src.schemas.question import MaterialInput
from src.schemas.report import MuseReport


class ReportGenerator:
    """Muse報告用構造化レポートを生成しファイル出力するクラス。"""

    def __init__(
        self,
        data_dir: str | Path = "data",
        *,
        mask_pii_enabled: bool = True,
    ) -> None:
        """レポート出力先ディレクトリを初期化する。"""
        self.reports_dir = Path(data_dir) / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.mask_pii_enabled = mask_pii_enabled

    def _sanitize_text(self, text: str) -> str:
        """設定に応じて個人情報マスキングを適用する。"""
        return mask_pii(text) if self.mask_pii_enabled else text

    def _sanitize_results(
        self,
        results: list[QuestionAnalysisResult],
    ) -> list[QuestionAnalysisResult]:
        """各問題の解説や確認理由に含まれる個人情報をマスキングする。"""
        if not self.mask_pii_enabled:
            return results
        sanitized: list[QuestionAnalysisResult] = []
        for item in results:
            sanitized.append(
                QuestionAnalysisResult(
                    question_id=item.question_id,
                    answer_candidates=[self._sanitize_text(c) for c in item.answer_candidates],
                    explanation=self._sanitize_text(item.explanation),
                    uncertainty=item.uncertainty,
                    confidence=item.confidence,
                    needs_review=item.needs_review,
                    review_reason=(
                        self._sanitize_text(item.review_reason)
                        if item.review_reason
                        else None
                    ),
                )
            )
        return sanitized

    def build_report(
        self,
        *,
        job_id: str,
        status: JobStatus,
        material: MaterialInput,
        provider: ProviderName,
        model: str,
        processing_time_seconds: float,
        provider_result: ProviderCallResult | None = None,
        error: ErrorDetail | None = None,
        permitted_screenshot_paths: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> MuseReport:
        """実行結果からMuseReportオブジェクトを構築する。"""
        raw_results = provider_result.results if provider_result else []
        clean_results = self._sanitize_results(raw_results)
        review_count = sum(1 for r in clean_results if r.needs_review)

        usage = TokenUsage(
            input_tokens=provider_result.input_tokens if provider_result else 0,
            output_tokens=provider_result.output_tokens if provider_result else 0,
            total_tokens=provider_result.total_tokens if provider_result else 0,
            estimated_cost_usd=provider_result.estimated_cost_usd if provider_result else 0.0,
        )

        combined_notes = list(notes or [])
        if provider_result and provider_result.warnings:
            combined_notes.extend(provider_result.warnings)

        # 未許可画像の除外注記
        excluded_imgs = sum(
            1 for q in material.questions for img in q.images if not img.ai_usage_approved
        )
        if excluded_imgs > 0:
            combined_notes.append(
                f"AI利用許可が確認できない画像 {excluded_imgs} 件は外部送信から除外しました。"
            )

        sanitized_error = error
        if error is not None and self.mask_pii_enabled:
            sanitized_error = ErrorDetail(
                code=error.code,
                message=self._sanitize_text(error.message),
                retryable=error.retryable,
                details=error.details,
            )

        report = MuseReport(
            job_id=job_id,
            status=status,
            material_id=material.material_id,
            material_name=self._sanitize_text(material.material_name),
            executed_at=datetime.now(timezone.utc),
            provider=provider,
            model=model,
            total_questions=len(material.questions),
            processed_questions=len(clean_results),
            results=clean_results,
            review_required_count=review_count,
            has_error=sanitized_error is not None,
            error=sanitized_error,
            processing_time_seconds=round(max(0.0, processing_time_seconds), 4),
            api_usage=usage,
            permitted_screenshot_paths=list(permitted_screenshot_paths or []),
            auto_submit_allowed=(
                material.auto_submit_allowed
                and material.material_category.value != "school_assessment"
            ),
            notes=[self._sanitize_text(n) for n in combined_notes],
        )
        self.write_report_file(report)
        return report

    def write_report_file(self, report: MuseReport) -> Path:
        """レポートJSONをファイルへ書き出す。"""
        out_path = self.reports_dir / f"{report.job_id}.json"
        out_path.write_text(
            report.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return out_path

    @staticmethod
    def format_user_summary(report: MuseReport) -> str:
        """Museがユーザーへ報告するための日本語サマリーテキストを生成する。"""
        lines = [
            f"教材名: {report.material_name}",
            f"実行日時 (UTC): {report.executed_at.isoformat()}",
            f"ステータス: {report.status.value}",
            f"使用AIモデル: {report.provider.value} / {report.model}",
            f"処理済み問題数: {report.processed_questions} / {report.total_questions}",
            f"確認が必要な問題数: {report.review_required_count}",
            f"エラーの有無: {'あり (' + report.error.code + ')' if report.has_error and report.error else 'なし'}",
            f"処理時間: {report.processing_time_seconds:.2f} 秒",
            (
                f"API使用量: 入力 {report.api_usage.input_tokens} / "
                f"出力 {report.api_usage.output_tokens} / "
                f"合計 {report.api_usage.total_tokens} トークン "
                f"(推定 ${report.api_usage.estimated_cost_usd:.6f})"
            ),
            f"許可スクリーンショット数: {len(report.permitted_screenshot_paths)} 件",
        ]
        return "\n".join(lines)
