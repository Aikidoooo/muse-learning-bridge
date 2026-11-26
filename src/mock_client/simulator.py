"""Muse連携環境がない場合にPython側と模擬ページをE2E検証する模擬Museクライアント。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page
from pydantic import BaseModel, Field

from src.browser.extractor import ExtractedPageResult, PlaywrightLearningExtractor
from src.core.exceptions import (
    LoginFailedError,
    MaterialNotFoundError,
    MuseBridgeError,
    MuseCommunicationError,
    PageStructureChangedError,
)
from src.core.logging_config import get_logger
from src.reports.image_masking import MaskRegion, mask_screenshot_pii
from src.schemas.enums import JobStatus, ProviderName
from src.schemas.report import MuseReport


class MockMuseRunOutcome(BaseModel):
    """模擬Museクライアントの1実行結果。"""

    report: MuseReport
    auto_submitted: bool = False
    graded_score: str | None = None
    masked_screenshot_path: str | None = None
    draft_applied_count: int = 0
    applied_count: int = 0
    notes: list[str] = Field(default_factory=list)


class MockMuseClient:
    """仕様書第5章（STEP 1〜8）のMuse側の役割をPlaywrightとHTTP APIで再現する模擬クライアント。"""

    def __init__(
        self,
        *,
        portal_base_url: str,
        api_base_url: str = "http://127.0.0.1:8765",
        api_client: httpx.AsyncClient | None = None,
        screenshot_dir: str | Path = "data/screenshots",
        poll_interval_seconds: float = 0.05,
        poll_timeout_seconds: float = 15.0,
        cdp_url: str | None = None,
    ) -> None:
        """模擬クライアントを初期化する。"""
        self.portal_base_url = portal_base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self._external_api_client = api_client
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.cdp_url = cdp_url
        self.extractor = PlaywrightLearningExtractor(headless=True)
        self.logger = get_logger("mock_client")

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """FastAPIサーバーへHTTPリクエストを送信する。"""
        url = f"{self.api_base_url}{path}"
        try:
            if self._external_api_client is not None:
                return await self._external_api_client.request(method, path, json=json_body)
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.request(method, url, json=json_body)
        except httpx.RequestError as exc:
            raise MuseCommunicationError(
                f"Python APIサーバーとの通信が切断されました: {type(exc).__name__}"
            ) from exc

    async def _login_portal(
        self,
        page: Page,
        *,
        student_id: str,
        password: str,
    ) -> None:
        """STEP 2: 模擬ポータルへログインする（認証情報はPython APIへ一切送らない）。"""
        await page.goto(f"{self.portal_base_url}/index.html", wait_until="networkidle")
        await page.fill("#student-id", student_id)
        await page.fill("#student-pass", password)
        await page.click('#login-form button[type="submit"]')

        portal_visible = await page.locator("#portal-section").is_visible()
        if not portal_visible:
            err_text = await page.locator("#login-error").inner_text()
            raise LoginFailedError(
                f"ポータルログインに失敗しました: {err_text or '認証エラー'}"
            )

    async def _extract_material(self, page: Page, target_page: str) -> ExtractedPageResult:
        """STEP 2 & 4: 対象教材を開き、利用許可確認と構造化抽出エンジンで問題データを取得する。"""
        response = await page.goto(f"{self.portal_base_url}/{target_page}", wait_until="networkidle")
        if response is None or response.status >= 400:
            raise MaterialNotFoundError(
                f"対象教材ページが見つかりません: {target_page}",
                details={"target_page": target_page, "status": response.status if response else None},
            )

        if await page.locator("#material-root").count() == 0:
            raise PageStructureChangedError(
                f"ページ構造が変更され、教材ルート(#material-root)を検出できません: {target_page}"
            )

        return await self.extractor.extract_page_material(page, require_approval_flag=True)

    async def _apply_results_and_capture(
        self,
        page: Page,
        extracted: ExtractedPageResult,
        report: MuseReport,
        *,
        auto_submit_allowed: bool,
    ) -> tuple[bool, str | None, str | None, int, int]:
        """STEP 7: 許可範囲内での画面反映・採点確認・PIIマスク済みスクリーンショット撮影。"""
        apply_outcome = await self.extractor.apply_answers(
            page,
            extracted,
            report,
            auto_submit_allowed=auto_submit_allowed,
        )
        applied_count = int(apply_outcome["applied_count"])
        draft_count = int(apply_outcome["draft_count"])

        auto_submitted = False
        graded_score: str | None = None
        if auto_submit_allowed:
            submit_btn = page.locator("#submit-exam-btn")
            if await submit_btn.count() > 0 and await submit_btn.is_enabled():
                await submit_btn.click()
                result_panel = page.locator("#result-screen")
                await result_panel.wait_for(state="visible", timeout=5000)
                graded_score = await page.locator("#score-display").inner_text()
                auto_submitted = True

        # full_page=True 基準の座標を一致させるため最上部へスクロールしてから撮影・マスキングする
        await page.evaluate("window.scrollTo(0, 0)")
        raw_path = self.screenshot_dir / f"{report.job_id}_raw.png"
        await page.screenshot(path=str(raw_path), full_page=True)

        regions: list[MaskRegion] = []
        user_pill = page.locator(".user-pill")
        if await user_pill.count() > 0 and await user_pill.is_visible():
            abs_box = await user_pill.evaluate(
                """(el) => {
                    const r = el.getBoundingClientRect();
                    return {
                        x: Math.max(0, r.left + window.scrollX),
                        y: Math.max(0, r.top + window.scrollY),
                        width: r.width,
                        height: r.height
                    };
                }"""
            )
            if abs_box and abs_box["width"] > 0 and abs_box["height"] > 0:
                regions.append(
                    MaskRegion(
                        x=abs_box["x"],
                        y=abs_box["y"],
                        width=abs_box["width"],
                        height=abs_box["height"],
                        label="STUDENT_PII",
                    )
                )

        masked_path = self.screenshot_dir / f"{report.job_id}_masked.png"
        mask_screenshot_pii(raw_path, regions, output_path=masked_path, mode="solid")
        return auto_submitted, graded_score, str(masked_path), draft_count, applied_count

    async def run_workflow(
        self,
        *,
        target_page: str,
        student_id: str = "A2026042",
        password: str = "demo-pass-2026",
        provider: ProviderName | str | None = None,
        model: str | None = None,
        idempotency_key: str | None = None,
    ) -> MockMuseRunOutcome:
        """STEP 1〜8の模擬E2Eフローを一気通貫で実行する。"""
        async with self.extractor.open_page(cdp_url=self.cdp_url) as page:
            await self._login_portal(page, student_id=student_id, password=password)
            extracted = await self._extract_material(page, target_page)
            payload = extracted.material_input.model_dump(mode="json")
            if provider is not None:
                payload["provider"] = (
                    provider.value if isinstance(provider, ProviderName) else str(provider)
                )
            if model is not None:
                payload["model"] = model
            if idempotency_key is not None:
                payload["idempotency_key"] = idempotency_key

            # STEP 3 & 5: Python APIへジョブ投入
            create_resp = await self._api_request("POST", "/jobs", json_body=payload)
            if create_resp.status_code >= 400:
                err_body = create_resp.json().get("error", {})
                raise MuseBridgeError(
                    err_body.get("message", f"ジョブ作成失敗 (HTTP {create_resp.status_code})"),
                    code=err_body.get("code", "JOB_CREATE_FAILED"),
                    retryable=bool(err_body.get("retryable", False)),
                    http_status=create_resp.status_code,
                    details=err_body.get("details", {}),
                )
            job_id = create_resp.json()["job_id"]

            # STEP 6: 完了までポーリング
            deadline = asyncio.get_running_loop().time() + self.poll_timeout_seconds
            terminal = {
                JobStatus.COMPLETED.value,
                JobStatus.REVIEW_REQUIRED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            }
            while True:
                status_resp = await self._api_request("GET", f"/jobs/{job_id}")
                status_val = status_resp.json()["status"]
                if status_val in terminal:
                    break
                if asyncio.get_running_loop().time() > deadline:
                    raise MuseCommunicationError(f"ジョブ {job_id} の完了待機がタイムアウトしました。")
                await asyncio.sleep(self.poll_interval_seconds)

            result_resp = await self._api_request("GET", f"/jobs/{job_id}/result")
            report = MuseReport.model_validate(result_resp.json())

            auto_submitted = False
            graded_score = None
            masked_path = None
            draft_count = 0
            applied_count = 0

            if not report.has_error and report.status in {
                JobStatus.COMPLETED,
                JobStatus.REVIEW_REQUIRED,
            }:
                auto_submitted, graded_score, masked_path, draft_count, applied_count = (
                    await self._apply_results_and_capture(
                        page,
                        extracted,
                        report,
                        auto_submit_allowed=bool(payload["auto_submit_allowed"]),
                    )
                )
                if masked_path:
                    ss_resp = await self._api_request(
                        "POST",
                        f"/jobs/{job_id}/screenshots",
                        json_body={"screenshot_paths": [masked_path]},
                    )
                    report = MuseReport.model_validate(ss_resp.json())

            return MockMuseRunOutcome(
                report=report,
                auto_submitted=auto_submitted,
                graded_score=graded_score,
                masked_screenshot_path=masked_path,
                draft_applied_count=draft_count,
                applied_count=applied_count,
                notes=report.notes,
            )
