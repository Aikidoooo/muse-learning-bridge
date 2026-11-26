"""Playwrightを用いた独立型問題抽出・ページ遷移・解答反映エンジン。

ブラウザ拡張機能に依存せず、Python + Playwright 単体で動作する。
Museが開いたブラウザへは、公式に許可された明示的CDPエンドポイントが設定された場合のみ接続し、
それ以外はセキュリティ機構を回避せず、独立Playwright起動またはMuseからのJSON受け渡しを用いる。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Frame, Page, Playwright, async_playwright
from pydantic import BaseModel, Field

from src.browser.dom_scripts import (
    APPLY_ANSWER_TO_CONTROL_JS,
    EXTRACT_DOCUMENT_QUESTIONS_JS,
    PAGE_SIGNATURE_JS,
)
from src.core.exceptions import (
    MaterialNotFoundError,
    PageStructureChangedError,
    PermissionDeniedError,
)
from src.core.logging_config import get_logger
from src.schemas.enums import MaterialCategory, QuestionFormat
from src.schemas.question import MaterialInput, QuestionImage, QuestionItem
from src.schemas.report import MuseReport


class ExtractedControlBinding(BaseModel):
    """抽出した設問と画面コントロールの対応情報。"""

    question_id: str
    control_ref: str
    control_type: str
    question_format: str
    frame_url: str = ""


class UnsupportedQuestionItem(BaseModel):
    """非対応形式やアクセス制約により自動処理対象外とした要素。"""

    question_id: str
    reason: str


class ExtractedPageResult(BaseModel):
    """1ページ分の構造化抽出結果。"""

    material_input: MaterialInput
    bindings: list[ExtractedControlBinding] = Field(default_factory=list)
    unsupported_items: list[UnsupportedQuestionItem] = Field(default_factory=list)


class PlaywrightLearningExtractor:
    """existing Firefox拡張機能 (essential) の知見を移植したPython側ブラウザ操作クラス。"""

    def __init__(self, *, headless: bool = True) -> None:
        """抽出エンジンを初期化する。"""
        self.headless = headless
        self.logger = get_logger("browser.extractor")

    @staticmethod
    def validate_cdp_endpoint(cdp_url: str) -> str:
        """明示指定されたCDP接続先がローカルループバックまたはセキュア通信であることを検証する。"""
        clean = cdp_url.strip()
        parsed = urlparse(clean)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme in {"http", "ws"} and host in {"127.0.0.1", "localhost", "::1"}:
            return clean
        if scheme in {"https", "wss"} and host:
            return clean
        raise PermissionDeniedError(
            "セキュリティポリシーにより、ローカルループバック以外の非暗号化CDP接続は許可されていません。",
            details={"cdp_url": clean},
        )

    @asynccontextmanager
    async def open_page(
        self,
        *,
        cdp_url: str | None = None,
        viewport: dict[str, int] | None = None,
    ) -> AsyncIterator[Page]:
        """独立Playwrightまたは公式許可CDP接続でPageを提供する。"""
        async with async_playwright() as pw:
            browser: Browser | None = None
            is_cdp = bool(cdp_url and cdp_url.strip())
            if is_cdp:
                safe_url = self.validate_cdp_endpoint(cdp_url or "")
                browser = await pw.chromium.connect_over_cdp(safe_url)
                context: BrowserContext = (
                    browser.contexts[0] if browser.contexts else await browser.new_context()
                )
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await pw.chromium.launch(headless=self.headless)
                page = await browser.new_page(viewport=viewport or {"width": 1280, "height": 900})

            try:
                yield page
            finally:
                # CDP接続時は既存ブラウザプロセス自体を閉じないよう注意し、独立起動時は終了する
                if not is_cdp and browser is not None:
                    await browser.close()

    async def wait_for_dom_stable(
        self,
        page: Page,
        *,
        max_attempts: int = 8,
        interval_seconds: float = 0.035,
    ) -> str:
        """ページ内容署名が連続一致するまで待機し、SPA描画途中の誤抽出を防ぐ。"""
        previous = await page.evaluate(PAGE_SIGNATURE_JS)
        for _ in range(max_attempts):
            await asyncio.sleep(interval_seconds)
            current = await page.evaluate(PAGE_SIGNATURE_JS)
            if current and current == previous:
                return str(current)
            previous = current
        return str(previous or "")

    async def _extract_from_frame(
        self,
        frame: Frame,
    ) -> dict[str, Any] | None:
        """1フレームから抽出スクリプトを実行する。クロスオリジン等で不可ならNoneを返す。"""
        try:
            return await frame.evaluate(EXTRACT_DOCUMENT_QUESTIONS_JS)
        except Exception as exc:
            self.logger.info(
                "フレーム内DOM抽出をスキップしました（生成元制約または未ロード）: url=%s err=%s",
                frame.url,
                type(exc).__name__,
            )
            return None

    async def extract_page_material(
        self,
        page: Page,
        *,
        require_approval_flag: bool = True,
        override_material_id: str | None = None,
        override_material_name: str | None = None,
    ) -> ExtractedPageResult:
        """現在開いているページ（および同一生成元iframe）から問題データを構造化抽出する。"""
        await self.wait_for_dom_stable(page)

        main_data = await self._extract_from_frame(page.main_frame)
        if main_data is None:
            raise PageStructureChangedError("メインフレームのDOM抽出に失敗しました。")

        meta: dict[str, Any] = main_data.get("meta", {})
        all_targets: list[dict[str, Any]] = []
        bindings: list[ExtractedControlBinding] = []
        unsupported: list[UnsupportedQuestionItem] = [
            UnsupportedQuestionItem(**u) for u in main_data.get("unsupported", [])
        ]

        # メインフレームとアクセス可能な同一生成元子フレームを結合
        frames_to_visit: list[tuple[Frame, dict[str, Any]]] = [(page.main_frame, main_data)]
        for child_frame in page.frames:
            if child_frame == page.main_frame:
                continue
            child_data = await self._extract_from_frame(child_frame)
            if child_data is None:
                unsupported.append(
                    UnsupportedQuestionItem(
                        question_id=f"frame-{len(unsupported) + 1}",
                        reason=f"別生成元またはアクセス制限のあるiframe ({child_frame.url}) は安全のため除外しました。",
                    )
                )
                continue
            frames_to_visit.append((child_frame, child_data))

        seen_qids: set[str] = set()
        for frame, data in frames_to_visit:
            for t in data.get("targets", []):
                qid = str(t["question_id"])
                if qid in seen_qids:
                    qid = f"{qid}-{len(all_targets) + 1}"
                    t["question_id"] = qid
                seen_qids.add(qid)
                all_targets.append(t)
                bindings.append(
                    ExtractedControlBinding(
                        question_id=qid,
                        control_ref=str(t["control_ref"]),
                        control_type=str(t["control_type"]),
                        question_format=str(t["question_format"]),
                        frame_url="" if frame == page.main_frame else frame.url,
                    )
                )
            for u in data.get("unsupported", []):
                if frame != page.main_frame:
                    unsupported.append(UnsupportedQuestionItem(**u))

        ai_approved = meta.get("ai_usage_approved")
        if require_approval_flag and ai_approved is not True:
            mat_name = meta.get("material_name") or page.url
            raise PermissionDeniedError(
                f"教材 '{mat_name}' はAI利用許可が確認できないため抽出処理を中止しました。",
                details={"url": page.url},
            )

        if not all_targets:
            raise PageStructureChangedError(
                f"ページ内から解答対象の問題・入力欄を検出できませんでした: {page.url}",
                details={"unsupported_count": len(unsupported)},
            )

        questions: list[QuestionItem] = []
        for t in all_targets:
            supp: dict[str, Any] = {}
            if t.get("blank_marker"):
                supp["blank_marker"] = t["blank_marker"]
            if t.get("math_expressions"):
                supp["math_expressions"] = t["math_expressions"]

            images = [QuestionImage.model_validate(img) for img in t.get("images", [])]
            questions.append(
                QuestionItem(
                    question_id=t["question_id"],
                    question_number=t["question_number"],
                    prompt=t["prompt"],
                    choices=t.get("choices", []),
                    question_format=QuestionFormat(t.get("question_format", "unknown")),
                    images=images,
                    supplementary_info=supp,
                )
            )

        cat_raw = str(meta.get("material_category") or "practice")
        try:
            category = MaterialCategory(cat_raw)
        except ValueError:
            category = MaterialCategory.UNKNOWN

        mat_id = override_material_id or meta.get("material_id") or f"page-{abs(hash(page.url)) % 100000}"
        mat_name = override_material_name or meta.get("material_name") or "抽出教材"
        auto_submit = bool(meta.get("auto_submit_allowed", False))

        material_input = MaterialInput(
            material_id=mat_id,
            material_name=mat_name,
            material_category=category,
            source_url=page.url,
            ai_usage_approved=bool(ai_approved) if ai_approved is not None else False,
            auto_submit_allowed=auto_submit,
            auto_submit_requested=auto_submit,
            permission_note=meta.get("permission_note"),
            questions=questions,
        )
        return ExtractedPageResult(
            material_input=material_input,
            bindings=bindings,
            unsupported_items=unsupported,
        )

    async def apply_answers(
        self,
        page: Page,
        extracted: ExtractedPageResult,
        report: MuseReport,
        *,
        auto_submit_allowed: bool = False,
    ) -> dict[str, Any]:
        """抽出時のバインディングを使って、許可範囲内で画面フォームへ解答を反映する。"""
        binding_map = {b.question_id: b for b in extracted.bindings}
        applied_count = 0
        draft_count = 0
        skipped: list[str] = []

        for res in report.results:
            binding = binding_map.get(res.question_id)
            if binding is None:
                skipped.append(res.question_id)
                continue

            target_frame: Frame = page.main_frame
            if binding.frame_url:
                matched_frame = next(
                    (f for f in page.frames if f.url == binding.frame_url),
                    None,
                )
                if matched_frame is None:
                    skipped.append(res.question_id)
                    continue
                target_frame = matched_frame

            can_fill = auto_submit_allowed and bool(res.answer_candidates) and not res.needs_review
            outcome = await target_frame.evaluate(
                APPLY_ANSWER_TO_CONTROL_JS,
                {
                    "controlRef": binding.control_ref,
                    "controlType": binding.control_type,
                    "kind": binding.question_format,
                    "answerCandidates": res.answer_candidates,
                    "explanation": res.explanation,
                    "autoSubmitAllowed": can_fill,
                },
            )
            if outcome.get("applied"):
                if outcome.get("mode") == "draft":
                    draft_count += 1
                else:
                    applied_count += 1
            else:
                skipped.append(res.question_id)

        return {
            "applied_count": applied_count,
            "draft_count": draft_count,
            "skipped": skipped,
        }

    async def navigate_next_page_if_present(
        self,
        page: Page,
        *,
        next_selector: str = "[data-next-page], #next-page-btn, a[rel='next']",
    ) -> bool:
        """複数ページ教材の次ページリンク/ボタンがあれば遷移し、DOM安定を待つ（提出ボタンは押さない）。"""
        next_loc = page.locator(next_selector).first
        if await next_loc.count() == 0 or not await next_loc.is_visible():
            return False
        prev_sig = await page.evaluate(PAGE_SIGNATURE_JS)
        prev_url = page.url
        await next_loc.click()
        for _ in range(20):
            await asyncio.sleep(0.05)
            cur_sig = await page.evaluate(PAGE_SIGNATURE_JS)
            if page.url != prev_url or (cur_sig and cur_sig != prev_sig):
                await self.wait_for_dom_stable(page)
                return True
        return True
