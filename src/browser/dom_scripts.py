"""Playwright page.evaluate() で実行するDOM抽出・数式正規化・解答適用スクリプト群。

既存の essential Firefox 拡張機能 (content.js) の実測DOMロジックを根拠とし、
ブラウザ拡張機能への依存なしに Python + Playwright 単体で動作するよう再構成したもの。
"""

from __future__ import annotations

# ページ内容署名を取得するスクリプト（SPA遷移・DOM描画安定判定用）
PAGE_SIGNATURE_JS = r"""
() => {
  const norm = (v) => String(v || "").replace(/[\t\r ]+/g, " ").replace(/\n+/g, " ").trim();
  const selectors = [
    "#material-root",
    "app-question-answer-layout [data-testid='question-number-container']",
    "tui-section-question > .title",
    "tui-question-content",
    ".spen-ly-question-section",
    ".question-card"
  ];
  const parts = [];
  for (const sel of selectors) {
    for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 16)) {
      const t = norm(el.textContent);
      if (t) parts.push(t);
    }
  }
  return [...new Set(parts)].join("|").slice(0, 6000);
}
"""

# 1つのドキュメント（メインまたは同一生成元iframe）から設問・数式・画像・非対応要素を抽出するスクリプト
EXTRACT_DOCUMENT_QUESTIONS_JS = r"""
(docArg) => {
  const doc = document;
  const win = doc.defaultView || window;
  const norm = (v) => String(v || "").replace(/[\t\r ]+/g, " ").replace(/\n+/g, " ").trim();

  // 数式要素（KaTeX / MathJax / MathML / sup / sub）を重複なく正規化テキストへ変換する
  function extractNormalizedTextWithMath(rootEl) {
    if (!rootEl) return "";
    const clone = rootEl.cloneNode(true);

    // 1. KaTeX: annotation[encoding="application/x-tex"] があれば $...$ に置換し、表示用 .katex-html を除去
    for (const katex of clone.querySelectorAll(".katex")) {
      const ann = katex.querySelector("annotation[encoding='application/x-tex']");
      const tex = ann ? norm(ann.textContent) : "";
      if (tex) {
        const span = doc.createElement("span");
        span.textContent = " $" + tex + "$ ";
        katex.replaceWith(span);
      } else {
        katex.querySelectorAll(".katex-mathml").forEach((n) => n.remove());
      }
    }

    // 2. MathJax v3 / v2
    for (const mjx of clone.querySelectorAll("mjx-container")) {
      const aria = norm(mjx.getAttribute("aria-label") || mjx.getAttribute("data-tex"));
      if (aria) {
        const span = doc.createElement("span");
        span.textContent = " $" + aria + "$ ";
        mjx.replaceWith(span);
      }
    }
    for (const texScript of clone.querySelectorAll("script[type^='math/tex']")) {
      const tex = norm(texScript.textContent);
      if (tex) {
        const span = doc.createElement("span");
        span.textContent = " $" + tex + "$ ";
        texScript.replaceWith(span);
      }
    }

    // 3. 単独の MathML (<math>) の annotation 対応
    for (const mathEl of clone.querySelectorAll("math")) {
      const ann = mathEl.querySelector("annotation[encoding='application/x-tex']");
      if (ann && norm(ann.textContent)) {
        const span = doc.createElement("span");
        span.textContent = " $" + norm(ann.textContent) + "$ ";
        mathEl.replaceWith(span);
      }
    }

    // 4. 上付き文字・下付き文字の明示化（x^2, a_n）
    for (const sup of clone.querySelectorAll("sup")) {
      const t = norm(sup.textContent);
      if (t) sup.textContent = "^" + (t.length > 1 ? "(" + t + ")" : t);
    }
    for (const sub of clone.querySelectorAll("sub")) {
      const t = norm(sub.textContent);
      if (t) sub.textContent = "_" + (t.length > 1 ? "(" + t + ")" : t);
    }

    // 非表示・UI要素の除外
    for (const skip of clone.querySelectorAll("script,style,noscript,nav,header,footer,[aria-hidden='true']")) {
      skip.remove();
    }
    return norm(clone.textContent);
  }

  function extractMathList(rootEl) {
    if (!rootEl) return [];
    const exprs = [];
    for (const ann of rootEl.querySelectorAll("annotation[encoding='application/x-tex'], script[type^='math/tex'], [data-tex]")) {
      const tex = norm(ann.getAttribute?.("data-tex") || ann.textContent);
      if (tex && !exprs.includes(tex)) exprs.push(tex);
    }
    return exprs;
  }

  function openDomRoots(root) {
    const roots = [];
    const visit = (cur) => {
      if (!cur || roots.includes(cur)) return;
      roots.push(cur);
      for (const el of cur.querySelectorAll?.("*") || []) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root);
    return roots;
  }

  function queryAllDeep(root, selector) {
    return openDomRoots(root).flatMap((r) => Array.from(r.querySelectorAll?.(selector) || []));
  }

  function isUsableControl(el) {
    if (!el || el.disabled) return false;
    if (el.closest("[aria-hidden='true']")) return false;
    const style = win.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  }

  function isNonAnswerTextControl(el) {
    const id = `${el.type || ""} ${el.name || ""} ${el.id || ""} ${el.placeholder || ""} ${el.getAttribute("aria-label") || ""}`;
    return /search|検索|ユーザー|user|name|氏名|メール|email|password|パスワード|ログイン|login|filter|絞り込/i.test(id);
  }

  function isWritableTextControl(el) {
    if (el.readOnly || el.getAttribute("aria-readonly") === "true") return false;
    if (el.hasAttribute("contenteditable") && el.getAttribute("contenteditable") === "false") return false;
    return true;
  }

  function findVisibleSelectProxy(select) {
    const selectors = "button,[role='combobox'],[aria-haspopup='listbox'],[class*='select'],[class*='Select'],[class*='dropdown'],[class*='Dropdown']";
    let cur = select.parentElement;
    for (let d = 0; cur && d < 5; d += 1, cur = cur.parentElement) {
      const cands = Array.from(cur.querySelectorAll(selectors)).filter((e) => e !== select && !e.contains(select) && isUsableControl(e));
      const pref = cands.find((e) => /選択してください|選んでください/.test(norm(e.innerText || e.textContent)));
      if (pref) return pref;
      if (cands.length === 1) return cands[0];
    }
    return null;
  }

  function nearestVisibleAncestor(el) {
    for (let cur = el.parentElement, d = 0; cur && d < 5; d += 1, cur = cur.parentElement) {
      if (cur !== doc.body && isUsableControl(cur)) return cur;
    }
    return null;
  }

  function findLabel(input) {
    const root = input.getRootNode();
    return input.closest("label") || (input.id ? root.querySelector?.(`label[for='${CSS.escape(input.id)}']`) || queryAllDeep(doc, `label[for='${CSS.escape(input.id)}']`)[0] : null);
  }

  function optionText(input, label) {
    return extractNormalizedTextWithMath(label) || norm(input.getAttribute("aria-label") || input.parentElement?.innerText || input.value || "選択肢");
  }

  function findChoiceContainer(input) {
    let cur = input.parentElement;
    let best = null;
    for (let d = 0; cur && d < 9; d += 1, cur = cur.parentElement) {
      const count = cur.querySelectorAll("input[type='radio'], input[type='checkbox']").length;
      if (!best && count >= 2 && count <= 20 && norm(cur.innerText).length <= 6000) best = cur;
      if (best && /(question|mondai|problem|answer|kaitou|設問|問題|回答|解答)/i.test(`${cur.id} ${cur.className}`)) return cur;
      if (count > 20) break;
    }
    return best;
  }

  function findAnswerContainer(control, minControls) {
    let best = null;
    for (let cur = control.parentElement, d = 0; cur && d < 9; cur = cur.parentElement, d += 1) {
      if (cur === doc.body) break;
      const cc = cur.querySelectorAll("input,textarea,select,[contenteditable='true'],[role='combobox']").length;
      const tl = norm(cur.innerText || cur.textContent).length;
      if (!best && cc >= minControls && cc <= 20 && tl >= 4 && tl <= 6000) best = cur;
      if (/(question|mondai|problem|answer|kaitou|設問|問題|回答|解答)/i.test(`${cur.id} ${cur.className}`)) return cur;
      if (cc > 20 || tl > 6000) break;
    }
    return best || control.parentElement;
  }

  function extractQuestionImages(container) {
    if (!container) return [];
    const imgs = [];
    let idx = 0;
    for (const img of container.querySelectorAll("img")) {
      const src = img.getAttribute("src") || "";
      const approved = img.getAttribute("data-ai-approved") === "true";
      const imageId = img.getAttribute("data-image-id") || `dom-img-${idx}`;
      const alt = norm(img.getAttribute("alt"));
      let mimeType = "image/png";
      let base64Data = null;
      let url = null;
      if (approved) {
        if (src.startsWith("data:")) {
          const commaIdx = src.indexOf(",");
          const meta = src.slice(5, commaIdx);
          if (meta.includes("svg")) mimeType = "image/svg+xml";
          else if (meta.includes("jpeg") || meta.includes("jpg")) mimeType = "image/jpeg";
          const rawBody = commaIdx >= 0 ? src.slice(commaIdx + 1) : "";
          if (meta.includes(";base64")) {
            base64Data = rawBody;
          } else {
            base64Data = btoa(unescape(encodeURIComponent(decodeURIComponent(rawBody))));
          }
        } else if (src) {
          url = src;
        }
      }
      imgs.push({
        image_id: imageId,
        url,
        base64_data: base64Data,
        mime_type: mimeType,
        alt_text: alt || null,
        ai_usage_approved: approved
      });
      idx += 1;
    }
    return imgs;
  }

  function extractQuestionFallback(container, optionTexts) {
    const promptNode = container?.querySelector?.(".question-prompt, .section-body, tui-question-content > tui-rich-text");
    if (promptNode) {
      const direct = extractNormalizedTextWithMath(promptNode);
      if (direct.length >= 2) return direct.slice(0, 2400);
    }
    const chunks = [];
    if (container) chunks.push(extractNormalizedTextWithMath(container));
    let sib = container?.previousElementSibling;
    for (let c = 0; sib && c < 3 && chunks.join(" ").length < 1500; c += 1, sib = sib.previousElementSibling) {
      const t = extractNormalizedTextWithMath(sib);
      if (t) chunks.unshift(t);
    }
    let q = chunks.join(" ");
    for (const opt of optionTexts) q = q.replaceAll(opt, " ");
    return norm(q).replace(/^(解答|回答)\s*/, "").replace(/選択してください[。.]?|入力してください[。.]?/g, " ").slice(0, 2400) || norm(doc.title);
  }

  // 座標ベースでの設問・共通指示・分離空欄抽出
  function extractQuestionForControl(control, container, optionTexts) {
    const fallback = extractQuestionFallback(container, optionTexts);
    if (container?.querySelector?.(".question-prompt")) return fallback;
    const controlRect = control.getBoundingClientRect();
    const lines = [];
    for (const root of openDomRoots(doc)) {
      const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest("script,style,noscript,svg,nav,header,footer,[aria-hidden='true'],.katex-html")) continue;
        const text = norm(node.nodeValue);
        if (!text || text.length > 600 || optionTexts.includes(text)) continue;
        if (/^(解答|回答|選択してください|ファイルを添付する)$/.test(text)) continue;
        const rect = parent.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0 || rect.bottom > controlRect.top + 8 || rect.bottom < controlRect.top - 1800) continue;
        const style = win.getComputedStyle(parent);
        if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) continue;
        lines.push({ text, top: rect.top, left: rect.left });
      }
    }
    lines.sort((a, b) => a.top - b.top || a.left - b.left);
    const uniqueLines = lines.filter((l, i, all) => all.findIndex((x) => x.text === l.text) === i);
    let secIdx = -1;
    for (let i = 0; i < uniqueLines.length; i += 1) {
      if (/^(?:設問|問)\s*\d+/.test(uniqueLines[i].text)) secIdx = i;
    }
    const secLines = secIdx >= 0 ? uniqueLines.slice(secIdx) : uniqueLines.slice(-8);
    const instrCands = (secIdx >= 0 ? uniqueLines.slice(0, secIdx) : uniqueLines)
      .filter((l) => /(次の|最も|正しい|適当).*(選|答)|選びなさい|答えなさい/.test(l.text));
    const instr = instrCands.at(-1)?.text || "";
    const spatial = norm([instr, ...secLines.map((l) => l.text)].filter(Boolean).join(" "));
    return spatial.length >= 12 ? spatial.slice(0, 2400) : fallback;
  }

  function extractClassiQuestion(container) {
    const page = container.closest(".spen-ly-question");
    const instr = extractNormalizedTextWithMath(page?.querySelector(".question-body"));
    const heading = norm(container.querySelector(".spen-mod-heading.lv-3 .heading-text")?.textContent);
    const body = extractNormalizedTextWithMath(container.querySelector(".section-body"));
    return norm([instr, heading, body].filter(Boolean).join(" ")).slice(0, 2400) || norm(doc.title);
  }

  function findAnswerMarker(control) {
    const directLabel = norm(findLabel(control)?.innerText || control.getAttribute("aria-label"));
    if (/^[ア-ンA-Z]$/.test(directLabel)) return directLabel;
    const controlRect = control.getBoundingClientRect();
    const candidates = [];
    for (const root of openDomRoots(doc)) {
      const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const text = norm(node.nodeValue);
        if (!/^[ア-ンA-Z]$/.test(text)) continue;
        const parent = node.parentElement;
        if (!parent || !isUsableControl(parent)) continue;
        const rect = parent.getBoundingClientRect();
        const vd = Math.abs((rect.top + rect.bottom) / 2 - (controlRect.top + controlRect.bottom) / 2);
        const hd = controlRect.left - rect.right;
        if (vd <= Math.max(18, controlRect.height) && hd >= -8 && hd <= 160) {
          candidates.push({ text, dist: vd + Math.abs(hd) });
        }
      }
    }
    return candidates.sort((a, b) => a.dist - b.dist)[0]?.text || "";
  }

  const targets = [];
  const usedControls = new Set();
  const unsupported = [];
  let autoIdx = 0;

  function assignControlRef(el, preferredId) {
    const refId = preferredId || el.getAttribute("data-mlb-ref") || `mlb-ctrl-${autoIdx++}`;
    el.setAttribute("data-mlb-ref", refId);
    return refId;
  }

  // 1. Classi 学習トレーニング 単一選択 (tui-single-select-form)
  for (const form of queryAllDeep(doc, "tui-answer-form tui-single-select-form")) {
    const layout = form.closest("app-question-answer-layout");
    const qc = layout?.querySelector("tui-question-content");
    if (!qc) continue;
    const sec = qc.closest("tui-section-question");
    const title = norm(sec?.querySelector(":scope > .title")?.textContent);
    const instr = extractNormalizedTextWithMath(sec?.querySelector(":scope > tui-rich-text"));
    const qNum = norm(qc.querySelector(":scope > .question-number")?.textContent) || `問${targets.length + 1}`;
    const body = extractNormalizedTextWithMath(qc.querySelector(":scope > tui-rich-text"));
    const question = norm([title, instr, qNum, body].filter(Boolean).join(" ")).slice(0, 2400);
    const dispOpts = Array.from(qc.querySelectorAll("tui-answer-options .answer-option"))
      .map((o) => extractNormalizedTextWithMath(o.querySelector("tui-rich-text") || o))
      .filter(Boolean);
    const inputs = Array.from(form.querySelectorAll("input[type='radio']")).filter((i) => !i.disabled && !usedControls.has(i));
    if (question.length < 2 || dispOpts.length < 2 || inputs.length !== dispOpts.length) continue;
    inputs.forEach((i) => usedControls.add(i));
    const qid = form.closest("[data-question-id]")?.getAttribute("data-question-id") || `tui-s-${targets.length + 1}`;
    const refId = assignControlRef(form, qid);
    targets.push({
      question_id: qid,
      question_number: qNum,
      question_format: "single_choice",
      control_type: "native-choice",
      control_ref: refId,
      prompt: question,
      choices: dispOpts,
      blank_marker: "",
      images: extractQuestionImages(qc),
      math_expressions: extractMathList(qc),
      dom_order: targets.length
    });
  }

  // 2. Classi 学習トレーニング 空欄順選択 (tui-multiple-select-ordered-form)
  for (const form of queryAllDeep(doc, "tui-answer-form tui-multiple-select-ordered-form")) {
    const layout = form.closest("app-question-answer-layout");
    const qc = layout?.querySelector("tui-question-content");
    if (!qc) continue;
    const sec = qc.closest("tui-section-question");
    const title = norm(sec?.querySelector(":scope > .title")?.textContent);
    const instr = extractNormalizedTextWithMath(sec?.querySelector(":scope > tui-rich-text"));
    const qNumBase = norm(qc.querySelector(":scope > .question-number")?.textContent) || `問${targets.length + 1}`;
    const body = extractNormalizedTextWithMath(qc.querySelector(":scope > tui-rich-text"));
    const question = norm([title, instr, qNumBase, body].filter(Boolean).join(" ")).slice(0, 2400);
    const dispOpts = Array.from(qc.querySelectorAll("tui-answer-options .answer-option"))
      .map((o) => extractNormalizedTextWithMath(o.querySelector("tui-rich-text") || o))
      .filter(Boolean);
    if (question.length < 2 || dispOpts.length < 2) continue;

    for (const bg of form.querySelectorAll(".blank-answer-options")) {
      const inputs = Array.from(bg.querySelectorAll("input[type='radio']")).filter((i) => !i.disabled && !usedControls.has(i));
      if (inputs.length < 2 || inputs.length !== dispOpts.length) continue;
      inputs.forEach((i) => usedControls.add(i));
      const marker = norm(bg.querySelector(":scope > .blank-symbol")?.textContent);
      const qid = bg.getAttribute("data-question-id") || `tui-o-${targets.length + 1}`;
      const refId = assignControlRef(bg, qid);
      targets.push({
        question_id: qid,
        question_number: marker ? `${qNumBase}(${marker})` : qNumBase,
        question_format: "single_choice",
        control_type: "native-choice",
        control_ref: refId,
        prompt: question,
        choices: dispOpts,
        blank_marker: marker,
        images: extractQuestionImages(qc),
        math_expressions: extractMathList(qc),
        dom_order: targets.length
      });
    }
  }

  // 3. Classi Webテスト image-selector (非表示選択肢をDOM直接取得)
  for (const sel of queryAllDeep(doc, ".spen-ly-question-section .question-selector image-selector")) {
    const trigger = sel.querySelector(".image-select-label");
    const container = sel.closest(".spen-ly-question-section");
    if (!trigger || !container || usedControls.has(trigger)) continue;
    const opts = Array.from(sel.querySelectorAll(".image-select-box .option-list > li"))
      .map((el) => extractNormalizedTextWithMath(el))
      .filter((t) => t && !/^(選択してください|選んでください)$/.test(t));
    if (opts.length < 2) continue;
    const answerRow = sel.closest(".answer-selector");
    const marker = norm(answerRow?.querySelector(":scope > .label-text")?.textContent);
    usedControls.add(sel);
    usedControls.add(trigger);
    const qid = container.getAttribute("data-question-id") || `cls-imgsel-${targets.length + 1}`;
    const refId = assignControlRef(sel, qid);
    const qNum = norm(container.querySelector(".spen-mod-heading.lv-3 .heading-text, .question-num")?.textContent) || `問${targets.length + 1}`;
    targets.push({
      question_id: qid,
      question_number: marker ? `${qNum}(${marker})` : qNum,
      question_format: "dropdown",
      control_type: "classi-image-select",
      control_ref: refId,
      prompt: extractClassiQuestion(container),
      choices: opts,
      blank_marker: marker,
      images: extractQuestionImages(container),
      math_expressions: extractMathList(container),
      dom_order: targets.length
    });
  }

  // 4. Classi Webテスト fill-in-text
  for (const ctrl of queryAllDeep(doc, ".spen-ly-question-section fill-in-text .question-selector input.spen-mod-input-text")) {
    if (ctrl.disabled || ctrl.readOnly || usedControls.has(ctrl)) continue;
    const container = ctrl.closest(".spen-ly-question-section");
    if (!container) continue;
    const answerRow = ctrl.closest(".answer-selector");
    const marker = norm(answerRow?.querySelector(":scope > .label-text")?.textContent);
    usedControls.add(ctrl);
    const qid = ctrl.getAttribute("data-question-id") || container.getAttribute("data-question-id") || `cls-txt-${targets.length + 1}`;
    const refId = assignControlRef(ctrl, qid);
    const qNum = norm(container.querySelector(".spen-mod-heading.lv-3 .heading-text, .question-num")?.textContent) || `問${targets.length + 1}`;
    targets.push({
      question_id: qid,
      question_number: marker ? `${qNum}(${marker})` : qNum,
      question_format: "short_text",
      control_type: "text",
      control_ref: refId,
      prompt: extractClassiQuestion(container),
      choices: [],
      blank_marker: marker,
      images: extractQuestionImages(container),
      math_expressions: extractMathList(container),
      dom_order: targets.length
    });
  }

  // 5. Classi Webテスト multi-select
  for (const mc of queryAllDeep(doc, ".spen-ly-question-section multi-select .question-selector")) {
    const sec = mc.closest(".spen-ly-question-section");
    const inputs = Array.from(mc.querySelectorAll("label.answer-selector input[type='checkbox']")).filter((i) => !i.disabled && !usedControls.has(i));
    if (!sec || inputs.length < 2) continue;
    const opts = inputs.map((i) => extractNormalizedTextWithMath(i.closest("label.answer-selector")) || norm(i.value)).filter(Boolean);
    if (opts.length < 2) continue;
    inputs.forEach((i) => usedControls.add(i));
    const qid = sec.getAttribute("data-question-id") || `cls-multi-${targets.length + 1}`;
    const refId = assignControlRef(mc, qid);
    const qNum = norm(sec.querySelector(".spen-mod-heading.lv-3 .heading-text, .question-num")?.textContent) || `問${targets.length + 1}`;
    targets.push({
      question_id: qid,
      question_number: qNum,
      question_format: "multiple_choice",
      control_type: "native-choice",
      control_ref: refId,
      prompt: extractClassiQuestion(sec),
      choices: opts,
      blank_marker: "",
      images: extractQuestionImages(sec),
      math_expressions: extractMathList(sec),
      dom_order: targets.length
    });
  }

  // 6. 汎用 radio / checkbox
  const nativeInputs = queryAllDeep(doc, "input[type='radio'], input[type='checkbox']").filter(isUsableControl);
  const grouped = new Map();
  for (const inp of nativeInputs) {
    if (usedControls.has(inp)) continue;
    const cont = findChoiceContainer(inp);
    if (!cont) continue;
    if (!grouped.has(cont)) grouped.set(cont, { cont, inputs: [] });
    grouped.get(cont).inputs.push(inp);
  }
  for (const grp of grouped.values()) {
    const grpInputs = grp.inputs.length >= 2
      ? grp.inputs
      : Array.from(grp.cont.querySelectorAll("input[type='radio'], input[type='checkbox']")).filter(isUsableControl);
    if (grpInputs.length < 2 || grpInputs.length > 20 || grpInputs.some((i) => usedControls.has(i))) continue;
    const opts = grpInputs.map((i) => optionText(i, findLabel(i)));
    if (new Set(opts).size < 2) continue;
    grpInputs.forEach((i) => usedControls.add(i));
    const qCard = grp.cont.closest("[data-question-id]") || grp.cont;
    const qid = qCard.getAttribute("data-question-id") || `nat-choice-${targets.length + 1}`;
    const refId = assignControlRef(grp.cont, qid);
    const qNum = qCard.getAttribute("data-question-number") || norm(qCard.querySelector(".question-num")?.textContent) || `問${targets.length + 1}`;
    targets.push({
      question_id: qid,
      question_number: qNum,
      question_format: grpInputs[0].type === "radio" ? "single_choice" : "multiple_choice",
      control_type: "native-choice",
      control_ref: refId,
      prompt: extractQuestionFallback(qCard, opts),
      choices: opts,
      blank_marker: "",
      images: extractQuestionImages(qCard),
      math_expressions: extractMathList(qCard),
      dom_order: targets.length
    });
  }

  // 7. 汎用 select (不可視select + プロキシボタン対応)
  for (const sel of queryAllDeep(doc, "select")) {
    if (sel.disabled || usedControls.has(sel) || sel.options.length < 2) continue;
    const proxy = findVisibleSelectProxy(sel);
    const visibleOk = isUsableControl(sel) || Boolean(proxy || nearestVisibleAncestor(sel));
    if (!visibleOk) continue;
    const opts = Array.from(sel.options)
      .filter((o) => !o.disabled && norm(o.textContent) && !/選択してください|選んでください/.test(o.textContent))
      .map((o) => norm(o.textContent));
    if (opts.length < 2) continue;
    const ctxCtrl = proxy || nearestVisibleAncestor(sel) || sel;
    const cont = findAnswerContainer(ctxCtrl, 1);
    const qCard = sel.closest("[data-question-id]") || cont;
    const promptText = extractQuestionForControl(ctxCtrl, qCard, opts);
    usedControls.add(sel);
    if (proxy) usedControls.add(proxy);
    const qid = qCard.getAttribute?.("data-question-id") || `nat-sel-${targets.length + 1}`;
    const refId = assignControlRef(sel, qid);
    const qNum = qCard.getAttribute?.("data-question-number") || norm(qCard.querySelector?.(".question-num")?.textContent) || `問${targets.length + 1}`;
    targets.push({
      question_id: qid,
      question_number: qNum,
      question_format: sel.multiple ? "multiple_choice" : "dropdown",
      control_type: "native-select",
      control_ref: refId,
      prompt: promptText,
      choices: opts,
      blank_marker: findAnswerMarker(ctxCtrl),
      images: extractQuestionImages(qCard),
      math_expressions: extractMathList(qCard),
      dom_order: targets.length
    });
  }

  // 8. 汎用記述欄 (textarea / text / contenteditable)
  for (const ctrl of queryAllDeep(doc, "textarea, input:not([type]), input[type='text'], [contenteditable='true'], [role='textbox']")) {
    if (!isUsableControl(ctrl) || usedControls.has(ctrl) || isNonAnswerTextControl(ctrl) || !isWritableTextControl(ctrl)) continue;
    const cont = findAnswerContainer(ctrl, 1);
    const qCard = ctrl.closest("[data-question-id]") || cont;
    const hasQContext = Boolean(qCard?.hasAttribute?.("data-question-id")) || /(問|設問|問題|解答|回答|記述|question|answer)/i.test(`${qCard?.id || ""} ${qCard?.className || ""} ${ctrl.placeholder || ""}`);
    if (!hasQContext) continue;
    usedControls.add(ctrl);
    const qid = qCard.getAttribute?.("data-question-id") || `nat-txt-${targets.length + 1}`;
    const refId = assignControlRef(ctrl, qid);
    const qNum = qCard.getAttribute?.("data-question-number") || norm(qCard.querySelector?.(".question-num")?.textContent) || `問${targets.length + 1}`;
    const isEssay = ctrl.tagName === "TEXTAREA" && Number(ctrl.rows || 2) >= 4;
    targets.push({
      question_id: qid,
      question_number: qNum,
      question_format: isEssay ? "essay" : "short_text",
      control_type: "text",
      control_ref: refId,
      prompt: extractQuestionForControl(ctrl, qCard, []),
      choices: [],
      blank_marker: findAnswerMarker(ctrl),
      images: extractQuestionImages(qCard),
      math_expressions: extractMathList(qCard),
      dom_order: targets.length
    });
  }

  // 9. 非対応形式（Canvas / ドラッグ&ドロップ / 音声録音・ファイル必須問題）の検出
  for (const card of queryAllDeep(doc, "[data-question-id], .spen-ly-question-section")) {
    const qid = card.getAttribute("data-question-id") || "";
    if (targets.some((t) => t.question_id === qid)) continue;
    if (card.querySelector("canvas, [draggable='true'], input[type='file']")) {
      unsupported.push({
        question_id: qid || `unsupported-${unsupported.length + 1}`,
        reason: "Canvas描画・ドラッグ&ドロップ・ファイル添付形式は構造化自動解答の対象外です。"
      });
    }
  }

  // メタデータ抽出
  const rootEl = doc.querySelector("#material-root");
  const meta = {
    material_id: rootEl?.getAttribute("data-material-id") || "",
    material_name: rootEl?.getAttribute("data-material-name") || norm(doc.title),
    material_category: rootEl?.getAttribute("data-material-category") || "unknown",
    ai_usage_approved: rootEl ? rootEl.getAttribute("data-ai-allowed") === "true" : null,
    auto_submit_allowed: rootEl ? rootEl.getAttribute("data-auto-submit-allowed") === "true" : false,
    permission_note: rootEl?.getAttribute("data-permission-note") || null
  };

  return { meta, targets, unsupported };
}
"""

# 抽出した control_ref に対して解答を適用するスクリプト（React/Angular対応ネイティブsetter + イベント発火）
APPLY_ANSWER_TO_CONTROL_JS = r"""
({ controlRef, controlType, kind, answerCandidates, explanation, autoSubmitAllowed }) => {
  const norm = (v) => String(v || "").replace(/[\t\r ]+/g, " ").replace(/\n+/g, " ").trim();
  const el = document.querySelector(`[data-mlb-ref="${CSS.escape(controlRef)}"]`);
  if (!el) return { applied: false, reason: "対象コントロールが見つかりません" };

  if (!autoSubmitAllowed) {
    // 自動提出が許可されていない教材では、下書き表示のみ（または確認ハイライト）に留める
    const card = el.closest("[data-question-id]") || el;
    const qid = card.getAttribute("data-question-id") || controlRef;
    const draft = document.querySelector(`[data-draft-for="${CSS.escape(qid)}"]`);
    const candStr = (answerCandidates || []).join(", ") || "要確認";
    if (draft) {
      draft.textContent = `AI候補: ${candStr}（${explanation || ""}）`;
      draft.classList.add("visible");
      return { applied: true, mode: "draft" };
    }
    return { applied: false, reason: "自動提出不可教材・下書き欄なし" };
  }

  function dispatchEvents(ctrl) {
    const win = ctrl.ownerDocument.defaultView;
    ctrl.dispatchEvent(new win.Event("input", { bubbles: true }));
    ctrl.dispatchEvent(new win.Event("change", { bubbles: true }));
  }

  if (controlType === "text") {
    const val = norm((answerCandidates || [])[0] || "");
    if (!val) return { applied: false, reason: "記述回答候補が空です" };
    const win = el.ownerDocument.defaultView;
    el.focus();
    if (el.isContentEditable) {
      el.textContent = val;
      el.dispatchEvent(new win.InputEvent("input", { bubbles: true, inputType: "insertText", data: val }));
    } else {
      const proto = el.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, val);
      else el.value = val;
      dispatchEvents(el);
    }
    el.blur();
    return { applied: true, mode: "text" };
  }

  if (controlType === "native-choice") {
    const inputs = Array.from(el.querySelectorAll("input[type='radio'], input[type='checkbox']")).filter((i) => !i.disabled);
    const wanted = (answerCandidates || []).map(norm);
    const items = inputs.map((inp, idx) => {
      const lbl = inp.closest("label") || (inp.id ? document.querySelector(`label[for="${CSS.escape(inp.id)}"]`) : null);
      return { inp, idx, txt: norm(lbl?.innerText || inp.value), val: norm(inp.value) };
    });
    // 選択肢テキスト一致を最優先し、全選択肢と一致しない候補のみ数値インデックス扱いする（数値選択肢との衝突防止）
    const matchedItems = items.filter((item) => wanted.includes(item.txt) || wanted.includes(item.val));
    const finalItems = matchedItems.length > 0
      ? matchedItems
      : items.filter((item) => wanted.includes(String(item.idx)));
    let matched = 0;
    for (const item of finalItems) {
      if (!item.inp.checked) item.inp.click();
      matched += 1;
      if (kind === "single_choice") break;
    }
    return { applied: matched > 0, mode: "native-choice", matched };
  }

  if (controlType === "native-select") {
    const wanted = (answerCandidates || []).map(norm);
    const opts = Array.from(el.options).filter((o) => !o.disabled && norm(o.textContent) && !/選択してください|選んでください/.test(o.textContent));
    let targetOpt = opts.find((o) => wanted.includes(norm(o.textContent)) || wanted.includes(norm(o.value)));
    if (!targetOpt) {
      targetOpt = opts.find((_, idx) => wanted.includes(String(idx)));
    }
    if (!targetOpt) return { applied: false, reason: "一致する選択肢がありません" };
    const win = el.ownerDocument.defaultView;
    const setter = Object.getOwnPropertyDescriptor(win.HTMLSelectElement.prototype, "value")?.set;
    if (setter) setter.call(el, targetOpt.value);
    else el.value = targetOpt.value;
    dispatchEvents(el);
    return { applied: true, mode: "native-select" };
  }

  if (controlType === "classi-image-select") {
    const wanted = (answerCandidates || []).map(norm);
    const items = Array.from(el.querySelectorAll(".image-select-box .option-list > li"))
      .filter((li) => norm(li.textContent) && !/^(選択してください|選んでください)$/.test(norm(li.textContent)));
    let targetLi = items.find((li) => wanted.includes(norm(li.textContent)));
    if (!targetLi) {
      targetLi = items.find((_, idx) => wanted.includes(String(idx)));
    }
    if (!targetLi) return { applied: false, reason: "image-selector選択肢が見つかりません" };
    targetLi.click();
    return { applied: true, mode: "classi-image-select" };
  }

  return { applied: false, reason: `未対応controlType: ${controlType}` };
}
"""
