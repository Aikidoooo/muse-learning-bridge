"""ジョブのライフサイクル管理・実行・キャンセル・予算・重複制御。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

import httpx

from src.core.config import AppConfig
from src.core.config_manager import ConfigManager
from src.core.exceptions import (
    BudgetExceededError,
    DuplicateJobError,
    JobCancelledError,
    JobNotFoundError,
    MuseBridgeError,
    ProviderNotFoundError,
)
from src.core.logging_config import get_logger
from src.core.security import verify_material_permission
from src.jobs.store import JobStore
from src.providers.factory import create_provider
from src.reports.generator import ReportGenerator
from src.schemas.enums import CustomProviderName, JobStatus, ProviderName, parse_provider_ref
from src.schemas.job import ErrorDetail, JobStatusResponse, TokenUsage
from src.schemas.question import MaterialInput
from src.schemas.report import MuseReport


class JobManager:
    """ジョブの作成・実行・状態遷移・中止・レポート取得を統括するクラス。"""

    def __init__(
        self,
        config: AppConfig | ConfigManager,
        *,
        store: JobStore | None = None,
        report_generator: ReportGenerator | None = None,
    ) -> None:
        """ジョブマネージャーを初期化する。"""
        if isinstance(config, ConfigManager):
            self.config_manager = config
            initial_cfg = config.get_snapshot()
        else:
            self.config_manager = ConfigManager(config)
            initial_cfg = config

        self.store = store or JobStore(initial_cfg.app.db_path)
        self.report_generator = report_generator or ReportGenerator(
            initial_cfg.app.data_dir,
            mask_pii_enabled=initial_cfg.security.mask_pii_in_logs_and_reports,
        )
        self.logger = get_logger("jobs.manager")
        self._running_tasks: dict[str, asyncio.Task[JobStatusResponse]] = {}
        self._cancel_flags: set[str] = set()
        # 実行中のジョブが設定変更の影響を受けないよう、ジョブ発行時の設定スナップショットを保持
        self._job_configs: dict[str, AppConfig] = {}
        # Custom Connectorから注入されたジョブ限りのAPIキー（メモリ内一時保持・永続化禁止）
        self._job_header_keys: dict[str, str] = {}

    @property
    def config(self) -> AppConfig:
        """現在の最新設定スナップショットを返す。"""
        return self.config_manager.get_snapshot()

    @staticmethod
    def compute_idempotency_key(material: MaterialInput, provider: str, model: str) -> str:
        """二重実行防止用の決定論的キーを生成する。"""
        if material.idempotency_key:
            return material.idempotency_key.strip()
        q_payload = [
            {
                "id": q.question_id,
                "num": q.question_number,
                "prompt": q.prompt,
                "choices": q.choices,
            }
            for q in material.questions
        ]
        digest_src = json.dumps(
            {
                "material_id": material.material_id,
                "provider": provider,
                "model": model,
                "questions": q_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
        return f"{material.material_id}:{provider}:{digest}"

    def _resolve_provider_and_model(
        self,
        material: MaterialInput,
        *,
        cfg: AppConfig,
        header_api_key: str | None = None,
    ) -> tuple[ProviderName | CustomProviderName, str]:
        """入力またはスナップショット設定からプロバイダーとモデルを決定する。"""
        raw_provider = (
            material.provider.value
            if isinstance(material.provider, ProviderName)
            else (material.provider or cfg.providers.default_provider)
        ).strip().lower()

        entry_cfg = cfg.providers.get_provider_config(raw_provider)
        if entry_cfg is None or not entry_cfg.enabled:
            raise ProviderNotFoundError(
                f"AIプロバイダー '{raw_provider}' が見つからないか無効化されています。",
                details={"provider": raw_provider},
            )
        model_name = (material.model or entry_cfg.default_model).strip()
        return parse_provider_ref(raw_provider), model_name

    def create_job(
        self,
        material: MaterialInput,
        *,
        header_api_key: str | None = None,
    ) -> JobStatusResponse:
        """事前セキュリティ検証と重複確認を行い、WAITING状態のジョブを発行する。"""
        snapshot_cfg = self.config_manager.get_snapshot()

        if snapshot_cfg.security.require_explicit_ai_approval:
            verify_material_permission(
                ai_usage_approved=material.ai_usage_approved,
                material_category=material.material_category.value,
                auto_submit_requested=material.auto_submit_requested,
                auto_submit_allowed=material.auto_submit_allowed,
            )

        if len(material.questions) > snapshot_cfg.security.max_questions_per_job:
            raise MuseBridgeError(
                f"1ジョブの最大問題数 ({snapshot_cfg.security.max_questions_per_job}) を超過しています。",
                code="TOO_MANY_QUESTIONS",
                retryable=False,
                http_status=400,
            )

        provider_name, model_name = self._resolve_provider_and_model(
            material,
            cfg=snapshot_cfg,
            header_api_key=header_api_key,
        )

        # 日次予算の上限チェック
        today_cost = self.store.get_today_cost_usd()
        if today_cost >= snapshot_cfg.security.max_daily_cost_usd:
            raise BudgetExceededError(
                f"本日のAPI利用料金上限 (${snapshot_cfg.security.max_daily_cost_usd:.2f}) に達しています。",
                details={"today_cost_usd": today_cost},
            )

        idem_key = self.compute_idempotency_key(
            material,
            provider_name.value,
            model_name,
        )
        existing_id = self.store.find_duplicate_job_id(idem_key)
        if existing_id is not None:
            raise DuplicateJobError(
                f"同一教材の重複ジョブが既に存在します (existing_job_id={existing_id})。",
                details={"existing_job_id": existing_id, "idempotency_key": idem_key},
            )

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        self._job_configs[job_id] = snapshot_cfg
        if header_api_key and header_api_key.strip():
            self._job_header_keys[job_id] = header_api_key.strip()

        status_resp = self.store.insert_job(
            job_id=job_id,
            idempotency_key=idem_key,
            material_id=material.material_id,
            material_name=material.material_name,
            provider=provider_name.value,
            model=model_name,
            status=JobStatus.WAITING,
            total_questions=len(material.questions),
            input_json=material.model_dump_json(),
        )
        self.logger.info(
            "ジョブ発行完了: job_id=%s material_id=%s provider=%s model=%s",
            job_id,
            material.material_id,
            provider_name.value,
            model_name,
        )
        return status_resp

    def start_background_job(
        self,
        job_id: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> asyncio.Task[JobStatusResponse]:
        """ジョブをバックグラウンドタスクとして開始する。"""
        task = asyncio.create_task(self.execute_job(job_id, http_client=http_client))
        self._running_tasks[job_id] = task

        def _cleanup(_: asyncio.Task[JobStatusResponse]) -> None:
            self._running_tasks.pop(job_id, None)

        task.add_done_callback(_cleanup)
        return task

    def _check_cancelled(self, job_id: str) -> None:
        """キャンセル要求が出ている場合は JobCancelledError を送出する。"""
        if job_id in self._cancel_flags:
            raise JobCancelledError(f"ジョブ {job_id} はユーザーまたはMuseにより中止されました。")
        current = self.store.get_job(job_id)
        if current and current.status == JobStatus.CANCELLED:
            raise JobCancelledError(f"ジョブ {job_id} は既に中止されています。")

    async def execute_job(
        self,
        job_id: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> JobStatusResponse:
        """ジョブのステータス遷移を管理しながら外部AI解析とレポート生成を実行する。"""
        raw_input = self.store.get_input_json(job_id)
        if raw_input is None:
            raise JobNotFoundError(f"ジョブが見つかりません: {job_id}")

        # 発行時に固定したスナップショット設定を使用（実行中の設定変更による干渉を防止）
        job_cfg = self._job_configs.get(job_id) or self.config_manager.get_snapshot()
        header_key = self._job_header_keys.get(job_id)

        material = MaterialInput.model_validate_json(raw_input)
        provider_name, model_name = self._resolve_provider_and_model(
            material,
            cfg=job_cfg,
            header_api_key=header_key,
        )
        started_monotonic = time.monotonic()

        try:
            # 1. INITIALIZING
            self._check_cancelled(job_id)
            self.store.update_status(job_id, JobStatus.INITIALIZING, mark_started=True)
            provider_kwargs: dict = {
                "provider_name": provider_name.value,
                "model": model_name,
                "http_client": http_client,
            }
            if header_key is not None:
                provider_kwargs["api_key_override"] = header_key
                provider_kwargs["secret_store"] = self.config_manager.secret_store
            provider = create_provider(job_cfg, **provider_kwargs)

            # 2. READY
            self._check_cancelled(job_id)
            self.store.update_status(job_id, JobStatus.READY)

            # 3. PROCESSING
            self._check_cancelled(job_id)
            self.store.update_status(job_id, JobStatus.PROCESSING)

            provider_result = await provider.analyze(material)
            self._check_cancelled(job_id)

            elapsed = time.monotonic() - started_monotonic

            # 4. 事後コスト上限チェック
            if provider_result.estimated_cost_usd > job_cfg.security.max_cost_per_job_usd:
                raise BudgetExceededError(
                    f"1ジョブのAPI利用料金上限 (${job_cfg.security.max_cost_per_job_usd:.2f}) を超過しました。",
                    details={
                        "estimated_cost_usd": provider_result.estimated_cost_usd,
                        "limit_usd": job_cfg.security.max_cost_per_job_usd,
                    },
                )

            review_count = sum(1 for r in provider_result.results if r.needs_review)
            final_status = (
                JobStatus.REVIEW_REQUIRED if review_count > 0 else JobStatus.COMPLETED
            )

            report = self.report_generator.build_report(
                job_id=job_id,
                status=final_status,
                material=material,
                provider=provider_name,
                model=model_name,
                processing_time_seconds=elapsed,
                provider_result=provider_result,
            )

            usage = TokenUsage(
                input_tokens=provider_result.input_tokens,
                output_tokens=provider_result.output_tokens,
                total_tokens=provider_result.total_tokens,
                estimated_cost_usd=provider_result.estimated_cost_usd,
            )
            updated = self.store.update_status(
                job_id,
                final_status,
                mark_completed=True,
                processed_questions=len(provider_result.results),
                review_required_count=review_count,
                token_usage=usage,
                report=report,
            )
            self.logger.info(
                "ジョブ完了: job_id=%s status=%s processed=%d review_required=%d cost=$%.6f",
                job_id,
                final_status.value,
                len(provider_result.results),
                review_count,
                provider_result.estimated_cost_usd,
            )
            assert updated is not None
            return updated

        except (JobCancelledError, asyncio.CancelledError) as exc:
            elapsed = time.monotonic() - started_monotonic
            err = ErrorDetail(
                code="JOB_CANCELLED",
                message=str(exc) or "ジョブが中止されました。",
                retryable=False,
            )
            report = self.report_generator.build_report(
                job_id=job_id,
                status=JobStatus.CANCELLED,
                material=material,
                provider=provider_name,
                model=model_name,
                processing_time_seconds=elapsed,
                error=err,
            )
            updated = self.store.update_status(
                job_id,
                JobStatus.CANCELLED,
                mark_completed=True,
                error=err,
                report=report,
            )
            self.logger.warning("ジョブ中止確定: job_id=%s", job_id)
            assert updated is not None
            return updated

        except MuseBridgeError as exc:
            elapsed = time.monotonic() - started_monotonic
            err = ErrorDetail(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                details=exc.details,
            )
            report = self.report_generator.build_report(
                job_id=job_id,
                status=JobStatus.FAILED,
                material=material,
                provider=provider_name,
                model=model_name,
                processing_time_seconds=elapsed,
                error=err,
            )
            updated = self.store.update_status(
                job_id,
                JobStatus.FAILED,
                mark_completed=True,
                error=err,
                report=report,
            )
            self.logger.error("ジョブ異常終了: job_id=%s code=%s msg=%s", job_id, exc.code, exc.message)
            assert updated is not None
            return updated

        except Exception as exc:
            elapsed = time.monotonic() - started_monotonic
            err = ErrorDetail(
                code="UNEXPECTED_ERROR",
                message=f"予期しないエラーが発生しました: {type(exc).__name__}",
                retryable=False,
            )
            report = self.report_generator.build_report(
                job_id=job_id,
                status=JobStatus.FAILED,
                material=material,
                provider=provider_name,
                model=model_name,
                processing_time_seconds=elapsed,
                error=err,
            )
            updated = self.store.update_status(
                job_id,
                JobStatus.FAILED,
                mark_completed=True,
                error=err,
                report=report,
            )
            self.logger.exception("ジョブ実行中に予期しない例外が発生: job_id=%s", job_id)
            assert updated is not None
            return updated
        finally:
            self._cancel_flags.discard(job_id)
            self._job_configs.pop(job_id, None)
            self._job_header_keys.pop(job_id, None)

    def get_status(self, job_id: str) -> JobStatusResponse:
        """ジョブの現在ステータスを取得する。"""
        job = self.store.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"ジョブが見つかりません: {job_id}")
        return job

    def get_events(self, job_id: str) -> list[dict]:
        """ジョブのステータス遷移履歴を取得する。"""
        self.get_status(job_id)
        return self.store.get_job_events(job_id)

    def list_jobs(self, limit: int = 20) -> list[JobStatusResponse]:
        """最近のジョブ一覧を取得する。"""
        return self.store.list_jobs(limit=limit)

    def get_result(self, job_id: str) -> MuseReport:
        """ジョブの完了レポートを取得する。未生成の場合はエラーまたは簡易チェック。"""
        job = self.get_status(job_id)
        report = self.store.get_report(job_id)
        if report is None:
            raise MuseBridgeError(
                f"ジョブ {job_id} はまだ処理完了していません (status={job.status.value})。",
                code="JOB_NOT_COMPLETED",
                retryable=True,
                http_status=409,
                details={"status": job.status.value},
            )
        return report

    def attach_screenshots(self, job_id: str, screenshot_paths: list[str]) -> MuseReport:
        """Museが撮影した許可済み結果画面スクリーンショットをレポートへ紐付ける。"""
        report = self.get_result(job_id)
        existing = set(report.permitted_screenshot_paths)
        for p in screenshot_paths:
            if p not in existing:
                report.permitted_screenshot_paths.append(p)
                existing.add(p)
        self.store.save_report(job_id, report)
        self.report_generator.write_report_file(report)
        return report

    def cancel_job(self, job_id: str) -> JobStatusResponse:
        """実行中または待機中のジョブを中止する。"""
        job = self.get_status(job_id)
        if job.status in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job

        self._cancel_flags.add(job_id)
        task = self._running_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()

        err = ErrorDetail(
            code="JOB_CANCELLED",
            message="実行中止リクエストを受け付けました。",
            retryable=False,
        )
        updated = self.store.update_status(
            job_id,
            JobStatus.CANCELLED,
            mark_completed=True,
            error=err,
        )
        assert updated is not None
        return updated
