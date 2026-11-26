"""SQLiteを用いたジョブ履歴・状態永続化ストア。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.schemas.enums import JobStatus, parse_provider_ref
from src.schemas.job import ErrorDetail, JobStatusResponse, TokenUsage
from src.schemas.report import MuseReport


def _utc_now_iso() -> str:
    """UTC現在時刻のISO8601文字列を返す。"""
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """SQLiteベースのジョブ永続化クラス。"""

    def __init__(self, db_path: str | Path) -> None:
        """データベース接続を初期化し、テーブルを作成する。"""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """SQLite接続を生成する。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        """必要なテーブルおよびインデックスを作成する。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    material_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_questions INTEGER NOT NULL,
                    processed_questions INTEGER NOT NULL DEFAULT 0,
                    review_required_count INTEGER NOT NULL DEFAULT 0,
                    input_json TEXT NOT NULL,
                    report_json TEXT,
                    error_json TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    api_requests_count INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    local_solved_count INTEGER NOT NULL DEFAULT 0,
                    provider_processing_time_seconds REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            # 既存DBとの後方互換マイグレーション（不足カラムのみ安全追加）
            existing_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            migrations = [
                ("thinking_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("api_requests_count", "INTEGER NOT NULL DEFAULT 0"),
                ("cache_hits", "INTEGER NOT NULL DEFAULT 0"),
                ("local_solved_count", "INTEGER NOT NULL DEFAULT 0"),
                ("provider_processing_time_seconds", "REAL NOT NULL DEFAULT 0.0"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_def}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id)"
            )
            conn.commit()

    def insert_job(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        material_id: str,
        material_name: str,
        provider: str,
        model: str,
        status: JobStatus,
        total_questions: int,
        input_json: str,
    ) -> JobStatusResponse:
        """新規ジョブを登録する。"""
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, idempotency_key, material_id, material_name,
                    provider, model, status, total_questions,
                    input_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    material_id,
                    material_name,
                    provider,
                    model,
                    status.value,
                    total_questions,
                    input_json,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO job_events (job_id, status, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, status.value, "ジョブ登録", now),
            )
            conn.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def find_duplicate_job_id(self, idempotency_key: str) -> str | None:
        """同一キーで進行中または完了済みのジョブIDがあれば返す（FAILED/CANCELLEDは除外）。"""
        excluded = (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE idempotency_key = ?
                  AND status NOT IN (?, ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (idempotency_key, excluded[0], excluded[1]),
            ).fetchone()
        return str(row["job_id"]) if row else None

    def get_today_cost_usd(self) -> float:
        """UTC本日分の累計推定APIコスト（USD）を返す。"""
        today_prefix = datetime.now(timezone.utc).date().isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS total_cost
                FROM jobs
                WHERE created_at LIKE ?
                """,
                (f"{today_prefix}%",),
            ).fetchone()
        return float(row["total_cost"]) if row else 0.0

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        mark_started: bool = False,
        mark_completed: bool = False,
        processed_questions: int | None = None,
        review_required_count: int | None = None,
        token_usage: TokenUsage | None = None,
        error: ErrorDetail | None = None,
        report: MuseReport | None = None,
    ) -> JobStatusResponse | None:
        """ジョブのステータスおよび関連メタデータを更新する。"""
        now = _utc_now_iso()
        fields: list[str] = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, now]

        if mark_started:
            fields.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if mark_completed:
            fields.append("completed_at = ?")
            params.append(now)
        if processed_questions is not None:
            fields.append("processed_questions = ?")
            params.append(processed_questions)
        if review_required_count is not None:
            fields.append("review_required_count = ?")
            params.append(review_required_count)
        if token_usage is not None:
            fields.extend(
                [
                    "input_tokens = ?",
                    "output_tokens = ?",
                    "thinking_tokens = ?",
                    "total_tokens = ?",
                    "estimated_cost_usd = ?",
                    "api_requests_count = ?",
                    "cache_hits = ?",
                    "local_solved_count = ?",
                    "provider_processing_time_seconds = ?",
                ]
            )
            params.extend(
                [
                    token_usage.input_tokens,
                    token_usage.output_tokens,
                    token_usage.thinking_tokens,
                    token_usage.total_tokens,
                    token_usage.estimated_cost_usd,
                    token_usage.api_requests_count,
                    token_usage.cache_hits,
                    token_usage.local_solved_count,
                    token_usage.provider_processing_time_seconds,
                ]
            )
        if error is not None:
            fields.append("error_json = ?")
            params.append(error.model_dump_json())
        if report is not None:
            fields.append("report_json = ?")
            params.append(report.model_dump_json())

        params.append(job_id)
        sql = f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?"
        with self._lock, self._connect() as conn:
            conn.execute(sql, tuple(params))
            detail = error.code if error is not None else None
            conn.execute(
                """
                INSERT INTO job_events (job_id, status, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, status.value, detail, now),
            )
            conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobStatusResponse | None:
        """ジョブステータス情報を取得する。"""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None

        error_obj = (
            ErrorDetail.model_validate_json(row["error_json"])
            if row["error_json"]
            else None
        )
        return JobStatusResponse(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            material_id=row["material_id"],
            material_name=row["material_name"],
            provider=parse_provider_ref(row["provider"]),
            model=row["model"],
            total_questions=int(row["total_questions"]),
            processed_questions=int(row["processed_questions"]),
            review_required_count=int(row["review_required_count"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            token_usage=TokenUsage(
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                thinking_tokens=int(row["thinking_tokens"] or 0),
                total_tokens=int(row["total_tokens"]),
                estimated_cost_usd=float(row["estimated_cost_usd"]),
                api_requests_count=int(row["api_requests_count"] or 0),
                cache_hits=int(row["cache_hits"] or 0),
                local_solved_count=int(row["local_solved_count"] or 0),
                provider_processing_time_seconds=float(row["provider_processing_time_seconds"] or 0.0),
            ),
            error=error_obj,
        )

    def get_input_json(self, job_id: str) -> str | None:
        """ジョブ登録時の入力JSON文字列を取得する。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return str(row["input_json"]) if row else None

    def get_report(self, job_id: str) -> MuseReport | None:
        """保存済みのレポートを取得する。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None or not row["report_json"]:
            return None
        return MuseReport.model_validate_json(row["report_json"])

    def save_report(self, job_id: str, report: MuseReport) -> None:
        """レポートJSONのみを更新保存する（スクリーンショット追加入力等）。"""
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET report_json = ?, updated_at = ? WHERE job_id = ?",
                (report.model_dump_json(), now, job_id),
            )
            conn.commit()

    def get_job_events(self, job_id: str) -> list[dict[str, Any]]:
        """指定ジョブのステータス遷移履歴を時系列で返す。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, detail, created_at
                FROM job_events
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "status": str(row["status"]),
                "detail": row["detail"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_jobs(self, limit: int = 20) -> list[JobStatusResponse]:
        """最近のジョブ一覧を作成日時の降順で返す。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results: list[JobStatusResponse] = []
        for row in rows:
            job = self.get_job(str(row["job_id"]))
            if job is not None:
                results.append(job)
        return results
