"""AI APIを呼ばずにプログラム側で安全・確実に確定できる設問の前処理・ローカル解決モジュール。"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

from src.schemas.analysis import QuestionAnalysisResult
from src.schemas.enums import QuestionFormat, UncertaintyLevel
from src.schemas.question import QuestionItem

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 安全にローカル解決できる純粋算術プレフィックス
_ARITH_PREFIX_PATTERN = re.compile(
    r"^(?:次の計算をしなさい|計算しなさい|次の値を求めよ|計算せよ)\s*[:：]?\s*"
)
_PURE_ARITH_PATTERN = re.compile(r"^[0-9+\-*/^().\s]+$")


def _safe_eval_ast(node: ast.AST) -> float:
    """ASTノードを安全な四則・累乗演算に限定して評価する。"""
    if isinstance(node, ast.Expression):
        return _safe_eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return float(_UNARY_OPS[type(node.op)](_safe_eval_ast(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _safe_eval_ast(node.left)
        right = _safe_eval_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 20:
            raise ValueError("Exponent too large")
        return float(_BIN_OPS[type(node.op)](left, right))
    raise ValueError("Unsupported AST node")


def _try_evaluate_pure_arithmetic(prompt: str) -> str | None:
    """純粋な算術式のみで構成される問題の計算結果を文字列で返す（解釈の余地がある場合はNone）。"""
    text = prompt.strip()
    text = _ARITH_PREFIX_PATTERN.sub("", text).strip()
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-")
    text = re.sub(r"[=＝]\s*[?？□\(\)\s]*$", "", text).strip()
    text = text.lstrip("$").rstrip("$").strip()

    if not text or not _PURE_ARITH_PATTERN.fullmatch(text):
        return None
    # 演算子を1つも含まない単なる数字は算術問題とみなさない
    if not any(op in text for op in "+-*/^"):
        return None

    expr = text.replace("^", "**")
    try:
        parsed = ast.parse(expr, mode="eval")
        value = _safe_eval_ast(parsed)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None

    if not math.isfinite(value):
        return None

    # 整数として表せる場合は整数文字列、そうでなければ不要な末尾ゼロを除いた表記
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    rounded = round(value, 6)
    return f"{rounded:.6f}".rstrip("0").rstrip(".")


def _format_number_variants(value_str: str) -> set[str]:
    """整数・小数の同値表記集合を返す。"""
    variants = {value_str.strip()}
    try:
        num = float(value_str)
        if abs(num - round(num)) < 1e-9:
            variants.add(str(int(round(num))))
    except ValueError:
        pass
    return variants


def try_solve_question_locally(question: QuestionItem) -> QuestionAnalysisResult | None:
    """外部AI APIを使わずに100%確実に判定できる設問のみローカル解決する。

    精度劣化を防ぐため、解釈の余地がある問題（文字式、文章題、複数解釈等）は
    必ず None を返して外部AI APIに委ねる。
    """
    approved_images = [img for img in question.images if img.ai_usage_approved and img.base64_data]
    excluded_images = sum(1 for img in question.images if not img.ai_usage_approved)

    # 1. 利用許可画像がゼロ・未許可画像のみが存在し、かつ問題文が実質空（10文字未満）で解析不能な場合
    clean_prompt = re.sub(r"(?:選択してください|入力してください|回答してください|[\s。．.?!！？])", "", question.prompt)
    if excluded_images > 0 and not approved_images and len(clean_prompt) < 10 and not question.choices:
        return QuestionAnalysisResult(
            question_id=question.question_id,
            answer_candidates=[],
            explanation="利用許可された画像がなくテキスト情報も不足しているため、外部AI送信を省略し確認対象としました。",
            uncertainty=UncertaintyLevel.HIGH,
            confidence=0.0,
            needs_review=True,
            review_reason="未許可画像除外後に解析可能な問題テキストが残っていないため確認が必要です。",
        )

    # 画像付き問題は視覚情報が正解に必須な場合があるためローカル解答しない
    if approved_images or excluded_images > 0:
        return None

    q_fmt = question.question_format

    # 2. 選択肢がちょうど1件しか存在しない単一選択／ドロップダウンは決定論的に確定
    if q_fmt in {QuestionFormat.SINGLE_CHOICE, QuestionFormat.DROPDOWN} and len(question.choices) == 1:
        sole_choice = question.choices[0]
        return QuestionAnalysisResult(
            question_id=question.question_id,
            answer_candidates=[sole_choice],
            explanation="選択肢が1件のみのためプログラム側で確定しました。",
            uncertainty=UncertaintyLevel.LOW,
            confidence=1.0,
            needs_review=False,
            review_reason=None,
        )

    # 3. 純粋な算術式のみの場合（選択肢ありは完全一致時のみ、short_textは厳格一致時のみ確定）
    if q_fmt in {QuestionFormat.SINGLE_CHOICE, QuestionFormat.DROPDOWN, QuestionFormat.SHORT_TEXT}:
        computed = _try_evaluate_pure_arithmetic(question.prompt)
        if computed is not None:
            if q_fmt in {QuestionFormat.SINGLE_CHOICE, QuestionFormat.DROPDOWN} and question.choices:
                comp_variants = _format_number_variants(computed)
                matched = [
                    c for c in question.choices if _format_number_variants(c) & comp_variants
                ]
                if len(matched) == 1:
                    return QuestionAnalysisResult(
                        question_id=question.question_id,
                        answer_candidates=[matched[0]],
                        explanation=f"純粋な算術式のためプログラム側で厳密計算し、一致する選択肢「{matched[0]}」を確定しました。",
                        uncertainty=UncertaintyLevel.LOW,
                        confidence=1.0,
                        needs_review=False,
                        review_reason=None,
                    )
            elif q_fmt == QuestionFormat.SHORT_TEXT and not question.choices:
                return QuestionAnalysisResult(
                    question_id=question.question_id,
                    answer_candidates=[computed],
                    explanation=f"純粋な算術式のためプログラム側で厳密計算し、値「{computed}」を算出しました。",
                    uncertainty=UncertaintyLevel.LOW,
                    confidence=1.0,
                    needs_review=False,
                    review_reason=None,
                )

    return None
