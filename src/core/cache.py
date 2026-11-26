"""SQLiteベースの問題単位キャッシュ・途中経過チェックポイント・モデル別利用統計ストア。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.schemas.analysis import QuestionAnalysisResult


def _utc_now() -> datetime:
    """UTC現在時刻を返す。"""
    return datetime.now(timezone.utc)


def _normalize_text_for_cache(text: str) -> str:
    """全半角・問題番号プレフィックス・余分な空白を正規化し、同一設問のキャッシュヒット率を高める。"""
    norm = unicodedata.normalize("NFKC", text or "").strip()
    norm = re.sub(r"^(?:(?:問|設問|Q)\s*\d+[\.\):：．]?\s*|\(\d+\)\s*)", "", norm, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", norm).strip()


def compute_question_cache_key(
    *,
    provider: str,
    model: str,
    material_category: str,
    sanitized_question: dict[str, Any],
    image_digests: list[str] | None = None,
) -> str:
    """設問内容・形式・選択肢・許可画像から表記揺れを吸収した決定論的キャッシュキーを生成する。"""
    # question_id や表記揺れでキャッシュが外れないよう正規化した内容のみをハッシュ化
    raw_choices = sanitized_question.get("choices", []) or []
    canonical = {
        "provider": provider.strip().lower(),
        "model": model.strip(),
        "category": material_category,
        "format": sanitized_question.get("question_format"),
        "prompt": _normalize_text_for_cache(str(sanitized_question.get("prompt", ""))),
        "choices": [_normalize_text_for_cache(str(c)) for c in raw_choices],
        "supplementary_info": sanitized_question.get("supplementary_info", {}),
        "image_digests": image_digests or [],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OptimizationCacheStore:
    """問題解析キャッシュ・途中経過チェックポイント・利用量メトリクスを永続化するストア。"""

    def __init__(self, db_path: str | Path) -> None:
        """データベーススキーマを初期化する。"""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """WAL有効・高速同期なSQLite接続を生成する。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        """キャッシュ・部分結果・モデル利用統計テーブルを作成する。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS question_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_qcache_expires ON question_cache(expires_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_partial_results (
                    job_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, question_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requests_count INTEGER NOT NULL DEFAULT 1,
                    questions_count INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    processing_time_seconds REAL NOT NULL DEFAULT 0.0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    local_solved_count INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER NOT NULL DEFAULT 200,
                    rate_limit_kind TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_metrics(created_at, provider, model)"
            )
            conn.commit()

    def get_cached_result(self, cache_key: str, *, target_question_id: str) -> QuestionAnalysisResult | None:
        """キャッシュヒット時にQuestionAnalysisResultを返す（TTL切れは無効扱い）。"""
        hits = self.get_cached_results_bulk([(cache_key, target_question_id)])
        return hits.get(target_question_id)

    def get_cached_results_bulk(
        self,
        items: list[tuple[str, str]],
    ) -> dict[str, QuestionAnalysisResult]:
        """複数の(cache_key, target_question_id)を1回のDB接続・1クエリで一括検索する。"""
        if not items:
            return {}
        now_iso = _utc_now().isoformat()
        unique_keys = list(dict.fromkeys(k for k, _ in items))
        placeholders = ",".join("?" for _ in unique_keys)

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT cache_key, result_json, expires_at FROM question_cache WHERE cache_key IN ({placeholders})",
                tuple(unique_keys),
            ).fetchall()

            valid_raw: dict[str, str] = {}
            expired_keys: list[str] = []
            for row in rows:
                c_key = str(row["cache_key"])
                exp = row["expires_at"]
                if exp and str(exp) <= now_iso:
                    expired_keys.append(c_key)
                else:
                    valid_raw[c_key] = str(row["result_json"])

            if expired_keys:
                exp_ph = ",".join("?" for _ in expired_keys)
                conn.execute(
                    f"DELETE FROM question_cache WHERE cache_key IN ({exp_ph})",
                    tuple(expired_keys),
                )
            if valid_raw:
                hit_ph = ",".join("?" for _ in valid_raw)
                conn.execute(
                    f"UPDATE question_cache SET hit_count = hit_count + 1 WHERE cache_key IN ({hit_ph})",
                    tuple(valid_raw.keys()),
                )
            if expired_keys or valid_raw:
                conn.commit()

        parsed_by_key = {
            c_key: QuestionAnalysisResult.model_validate_json(raw_json)
            for c_key, raw_json in valid_raw.items()
        }
        out: dict[str, QuestionAnalysisResult] = {}
        for c_key, qid in items:
            base_res = parsed_by_key.get(c_key)
            if base_res is not None:
                out[qid] = base_res.model_copy(update={"question_id": qid})
        return out

    def put_cached_results(
        self,
        entries: list[tuple[str, str, str, QuestionAnalysisResult]],
        *,
        ttl_seconds: int = 86400,
    ) -> None:
        """完了した設問結果をキャッシュへ保存する（needs_review=True や異常候補はキャッシュしない）。"""
        if not entries:
            return
        now = _utc_now()
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat() if ttl_seconds > 0 else None

        rows_to_insert: list[tuple[str, str, str, str, str, str | None]] = []
        for cache_key, provider, model, res in entries:
            if res.needs_review or not res.answer_candidates:
                continue
            rows_to_insert.append(
                (cache_key, provider, model, res.model_dump_json(), now_iso, expires_iso)
            )
        if not rows_to_insert:
            return

        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO question_cache (cache_key, provider, model, result_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                rows_to_insert,
            )
            conn.commit()

    def save_partial_results(self, job_id: str, results: list[QuestionAnalysisResult]) -> int:
        """ジョブ実行途中の完了バッチ結果を保存し、合計保存件数を返す。"""
        if not job_id or not results:
            return 0
        now_iso = _utc_now().isoformat()
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO job_partial_results (job_id, question_id, result_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, question_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    created_at = excluded.created_at
                """,
                [(job_id, r.question_id, r.model_dump_json(), now_iso) for r in results],
            )
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM job_partial_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            conn.commit()
        return int(row["cnt"]) if row else 0

    def get_partial_results(self, job_id: str) -> dict[str, QuestionAnalysisResult]:
        """途中失敗または継続実行用の保存済み設問結果を取得する。"""
        if not job_id:
            return {}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT question_id, result_json FROM job_partial_results WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        out: dict[str, QuestionAnalysisResult] = {}
        for row in rows:
            out[str(row["question_id"])] = QuestionAnalysisResult.model_validate_json(row["result_json"])
        return out

    def record_usage_metric(
        self,
        *,
        job_id: str | None = None,
        provider: str,
        model: str,
        requests_count: int = 1,
        questions_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
        total_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        processing_time_seconds: float = 0.0,
        cache_hits: int = 0,
        local_solved_count: int = 0,
        status_code: int = 200,
        rate_limit_kind: str | None = None,
    ) -> None:
        """APIリクエスト単位およびジョブ単位の使用量・モデル別処理時間を記録する。"""
        now_iso = _utc_now().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_metrics (
                    job_id, provider, model, requests_count, questions_count,
                    input_tokens, output_tokens, thinking_tokens, total_tokens,
                    estimated_cost_usd, processing_time_seconds,
                    cache_hits, local_solved_count, status_code, rate_limit_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    provider,
                    model,
                    requests_count,
                    questions_count,
                    input_tokens,
                    output_tokens,
                    thinking_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    processing_time_seconds,
                    cache_hits,
                    local_solved_count,
                    status_code,
                    rate_limit_kind,
                    now_iso,
                ),
            )
            conn.commit()

    def get_today_provider_requests(self, provider: str) -> tuple[int, int]:
        """UTC本日分の指定プロバイダーの実APIリクエスト数とトークン数を返す。"""
        today_prefix = _utc_now().date().isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(requests_count), 0) AS reqs,
                    COALESCE(SUM(total_tokens), 0) AS toks
                FROM usage_metrics
                WHERE provider = ? AND created_at LIKE ?
                """,
                (provider.strip().lower(), f"{today_prefix}%"),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["reqs"]), int(row["toks"])

    def get_usage_summary(self, *, today_only: bool = True) -> dict[str, Any]:
        """全体・モデル別の利用統計とキャッシュ効率サマリーを返す。"""
        today_prefix = _utc_now().date().isoformat()
        where_clause = "WHERE created_at LIKE ?" if today_only else ""
        params: tuple[Any, ...] = (f"{today_prefix}%",) if today_only else ()

        with self._lock, self._connect() as conn:
            totals_row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(requests_count), 0) AS total_requests,
                    COALESCE(SUM(questions_count), 0) AS total_questions,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(thinking_tokens), 0) AS thinking_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd,
                    COALESCE(SUM(processing_time_seconds), 0.0) AS total_processing_time_seconds,
                    COALESCE(SUM(cache_hits), 0) AS cache_hits,
                    COALESCE(SUM(local_solved_count), 0) AS local_solved_count
                FROM usage_metrics
                {where_clause}
                """,
                params,
            ).fetchone()

            by_model_rows = conn.execute(
                f"""
                SELECT
                    provider,
                    model,
                    COALESCE(SUM(requests_count), 0) AS requests_count,
                    COALESCE(SUM(questions_count), 0) AS questions_count,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(thinking_tokens), 0) AS thinking_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd,
                    COALESCE(SUM(processing_time_seconds), 0.0) AS processing_time_seconds,
                    COALESCE(SUM(cache_hits), 0) AS cache_hits,
                    COALESCE(SUM(local_solved_count), 0) AS local_solved_count
                FROM usage_metrics
                {where_clause}
                GROUP BY provider, model
                ORDER BY provider, model
                """,
                params,
            ).fetchall()

            cache_row = conn.execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hit_count), 0) AS total_hits FROM question_cache"
            ).fetchone()

        by_model = [dict(r) for r in by_model_rows]
        for item in by_model:
            item["estimated_cost_usd"] = round(float(item["estimated_cost_usd"]), 6)
            item["processing_time_seconds"] = round(float(item["processing_time_seconds"]), 4)

        summary = dict(totals_row) if totals_row else {}
        summary["estimated_cost_usd"] = round(float(summary.get("estimated_cost_usd", 0.0)), 6)
        summary["total_processing_time_seconds"] = round(
            float(summary.get("total_processing_time_seconds", 0.0)), 4
        )
        return {
            "scope": "today_utc" if today_only else "all_time",
            "summary": summary,
            "by_model": by_model,
            "cache_store": {
                "cached_entries": int(cache_row["entries"]) if cache_row else 0,
                "cumulative_cache_hits": int(cache_row["total_hits"]) if cache_row else 0,
            },
        }
