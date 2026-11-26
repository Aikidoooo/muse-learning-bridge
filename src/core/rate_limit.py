"""429/503エラー詳細解析（RPM/TPM/RPD判定・RetryInfo解析）および非同期レートリミッター。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Mapping

from src.core.security import mask_secrets


class RateLimitKind(str, Enum):
    """429レスポンスから判定された制限種別。"""

    RPM = "RPM"
    TPM = "TPM"
    RPD = "RPD"
    CONCURRENT = "CONCURRENT"
    UNKNOWN = "UNKNOWN"


@dataclass
class RateLimitAnalysis:
    """429/503エラーレスポンスの詳細解析結果。"""

    status_code: int
    kind: RateLimitKind = RateLimitKind.UNKNOWN
    retry_after_seconds: float | None = None
    retry_after_source: str | None = None
    quota_metric: str | None = None
    quota_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    is_daily_exhausted: bool = False

    def to_safe_dict(self) -> dict[str, object]:
        """シークレットを含まないログ・例外詳細用辞書へ変換する。"""
        raw = asdict(self)
        raw["kind"] = self.kind.value
        if self.error_message:
            raw["error_message"] = mask_secrets(self.error_message)[:300]
        return raw


def parse_duration_string(value: str | float | int | None) -> float | None:
    """Google RPC RetryInfo等の期間表記（例: '4.5s', '1500ms', '3'）を秒数へ変換する。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().lower()
    if not text:
        return None
    if text.endswith("ms"):
        try:
            return max(0.0, float(text[:-2].strip()) / 1000.0)
        except ValueError:
            return None
    if text.endswith("s"):
        try:
            return max(0.0, float(text[:-1].strip()))
        except ValueError:
            return None
    try:
        return max(0.0, float(text))
    except ValueError:
        return None


def parse_retry_after_header(headers: Mapping[str, str]) -> tuple[float | None, str | None]:
    """Retry-After または X-RateLimit-Reset 系ヘッダから待機秒数を取得する。"""
    lower = {k.lower(): v for k, v in headers.items()}

    raw_retry_after = lower.get("retry-after")
    if raw_retry_after:
        parsed_num = parse_duration_string(raw_retry_after)
        if parsed_num is not None:
            return parsed_num, "header:retry-after"
        try:
            dt = parsedate_to_datetime(raw_retry_after)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta), "header:retry-after-date"
        except (TypeError, ValueError):
            pass

    for hkey in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = lower.get(hkey)
        if val:
            sec = parse_duration_string(val)
            if sec is not None:
                return sec, f"header:{hkey}"

    return None, None


def _classify_limit_text(text: str) -> RateLimitKind:
    """クォータID・メトリック・エラー文言から RPM / TPM / RPD / CONCURRENT を判定する。"""
    low = text.lower()
    if any(
        token in low
        for token in (
            "per_day",
            "perday",
            "requestsperday",
            "rpd",
            "daily",
            "per day",
            "day_limit",
        )
    ):
        return RateLimitKind.RPD
    if any(
        token in low
        for token in (
            "tokens_per_min",
            "tokenspermin",
            "input_tokens_per_minute",
            "output_tokens_per_minute",
            "tpm",
            "tokens per min",
            "token limit",
            "max_tokens_per_minute",
        )
    ):
        return RateLimitKind.TPM
    if any(
        token in low
        for token in (
            "requests_per_min",
            "requestspermin",
            "rpm",
            "requests per min",
            "rate_limit_exceeded",
            "requests_per_minute",
        )
    ):
        return RateLimitKind.RPM
    if any(token in low for token in ("concurrent", "concurrency", "simultaneous")):
        return RateLimitKind.CONCURRENT
    return RateLimitKind.UNKNOWN


def parse_rate_limit_response(
    status_code: int,
    headers: Mapping[str, str],
    body_text: str,
) -> RateLimitAnalysis:
    """429/503等のHTTPレスポンス詳細JSONとヘッダを解析し制限種別・待機秒数を返す。"""
    analysis = RateLimitAnalysis(status_code=status_code)
    header_delay, header_src = parse_retry_after_header(headers)
    if header_delay is not None:
        analysis.retry_after_seconds = header_delay
        analysis.retry_after_source = header_src

    try:
        data = json.loads(body_text) if body_text else {}
    except ValueError:
        data = {}

    collected_hints: list[str] = []
    if isinstance(data, dict):
        err_obj = data.get("error")
        if isinstance(err_obj, dict):
            analysis.error_type = str(err_obj.get("type") or err_obj.get("status") or err_obj.get("code") or "") or None
            msg = err_obj.get("message")
            if msg:
                analysis.error_message = mask_secrets(str(msg))
                collected_hints.append(str(msg))
            if err_obj.get("code"):
                collected_hints.append(str(err_obj.get("code")))
            if err_obj.get("type"):
                collected_hints.append(str(err_obj.get("type")))

            # Google RPC RetryInfo / QuotaFailure 解析
            details = err_obj.get("details")
            if isinstance(details, list):
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    type_name = str(item.get("@type") or "")
                    if "RetryInfo" in type_name or "retryDelay" in item:
                        delay = parse_duration_string(item.get("retryDelay"))
                        if delay is not None:
                            analysis.retry_after_seconds = delay
                            analysis.retry_after_source = "json:RetryInfo"
                    violations = item.get("violations")
                    if isinstance(violations, list):
                        for v in violations:
                            if not isinstance(v, dict):
                                continue
                            qid = str(v.get("quotaId") or "")
                            qmetric = str(v.get("quotaMetric") or "")
                            qdesc = str(v.get("description") or "")
                            if qid and not analysis.quota_id:
                                analysis.quota_id = qid
                            if qmetric and not analysis.quota_metric:
                                analysis.quota_metric = qmetric
                            collected_hints.extend([qid, qmetric, qdesc])
        elif isinstance(err_obj, str):
            analysis.error_message = mask_secrets(err_obj)
            collected_hints.append(err_obj)

        # トップレベル retry_after_seconds 等のフォールバック
        for key in ("retry_after", "retry_after_seconds"):
            if key in data and analysis.retry_after_seconds is None:
                parsed = parse_duration_string(data.get(key))
                if parsed is not None:
                    analysis.retry_after_seconds = parsed
                    analysis.retry_after_source = f"json:{key}"

    if not collected_hints and body_text:
        collected_hints.append(body_text[:500])

    # ヘッダ側の残量ゼロ情報もヒントに加える
    lower_headers = {k.lower(): v for k, v in headers.items()}
    if lower_headers.get("x-ratelimit-remaining-tokens") == "0":
        collected_hints.append("tokens_per_min")
    if lower_headers.get("x-ratelimit-remaining-requests") == "0":
        collected_hints.append("requests_per_min")

    combined = " ".join(h for h in collected_hints if h)
    analysis.kind = _classify_limit_text(combined)
    if analysis.kind == RateLimitKind.RPD:
        analysis.is_daily_exhausted = True

    # JSONメッセージ中に "Please retry in 4.2s" 等があれば抽出（未取得時のみ）
    if analysis.retry_after_seconds is None and combined:
        m = re.search(r"retry\s+(?:in|after)\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|seconds)?", combined, re.IGNORECASE)
        if m:
            num = float(m.group(1))
            unit = (m.group(2) or "s").lower()
            analysis.retry_after_seconds = num / 1000.0 if unit == "ms" else num
            analysis.retry_after_source = "message:pattern"

    return analysis


class AsyncRateLimiter:
    """プロバイダー別のスライディングウィンドウ RPM / TPM 非同期レートリミッター。"""

    def __init__(self) -> None:
        """スライディングウィンドウ管理構造を初期化する。"""
        self._lock = asyncio.Lock()
        self._request_times: dict[str, deque[float]] = {}
        self._token_records: dict[str, deque[tuple[float, int]]] = {}

    async def acquire(
        self,
        provider_id: str,
        *,
        rpm_limit: int,
        tpm_limit: int,
        estimated_tokens: int = 0,
    ) -> float:
        """RPM・TPM上限に収まるまで必要な最小時間だけ非同期待機し、枠を確保する。"""
        if rpm_limit <= 0 and tpm_limit <= 0:
            return 0.0

        waited_total = 0.0
        while True:
            sleep_for = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                req_q = self._request_times.setdefault(provider_id, deque())
                tok_q = self._token_records.setdefault(provider_id, deque())

                while req_q and req_q[0] <= cutoff:
                    req_q.popleft()
                while tok_q and tok_q[0][0] <= cutoff:
                    tok_q.popleft()

                if rpm_limit > 0 and len(req_q) >= rpm_limit:
                    sleep_for = max(sleep_for, (req_q[0] + 60.0) - now + 0.005)

                if tpm_limit > 0 and estimated_tokens > 0:
                    used_tokens = sum(t for _, t in tok_q)
                    if used_tokens > 0 and used_tokens + estimated_tokens > tpm_limit and tok_q:
                        sleep_for = max(sleep_for, (tok_q[0][0] + 60.0) - now + 0.005)

                if sleep_for <= 0.0:
                    req_q.append(now)
                    if estimated_tokens > 0:
                        tok_q.append((now, estimated_tokens))
                    return waited_total

            await asyncio.sleep(min(sleep_for, 5.0))
            waited_total += min(sleep_for, 5.0)
