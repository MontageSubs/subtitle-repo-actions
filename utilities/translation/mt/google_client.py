#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 2.5.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Batches and translates subtitle units using Google Translate's PA endpoint.
#     Employs concurrent threading for faster translation, wraps text in HTML
#     inline anchors to preserve alignment, and handles retries/fallbacks automatically.
#     For units carrying glossary `term_matches`, wraps each matched span in a
#     translate="no" tag so the provider returns it verbatim inside otherwise
#     fully translated context, then locally substitutes the fixed target term
#     into the result, preserving cross-sentence context that a pre-substituted
#     or placeholder-based send would break. Issues a single isolated retry
#     when the substituted result still looks untranslated.
#     使用 Google Translate PA 接口进行字幕单元批量机器翻译。采用并发线程池
#     加速翻译，通过 HTML 行内标签包裹文本以保留对应关系，并自动处理
#     请求重试与失败回退。对携带词表命中（term_matches）的单元，将命中片段
#     包裹为 translate="no" 标签发送，使供应商在完整上下文中原样保留该片段、
#     正常翻译周围语境，收到结果后在本地将其替换为固定译名——相比预先替换
#     或占位符方案更好地保留跨句上下文；若替换后结果仍疑似未翻译，单独
#     重发一次原句作为质量兜底。
#
# Features:
#     - Concurrent HTTP requests via ThreadPoolExecutor.
#     - Chapter-aware batching: whole chapters (scene/song runs) are packed
#       into a batch under character limits (DEFAULT_BATCH_CHARS), splitting
#       by character count only as a fallback for an oversized chapter.
#     - Protective inline HTML formatting (<span> tags) to isolate units and
#       map results within a chapter's <div>, keeping context continuous
#       inside a scene while still signaling a break between scenes.
#     - Two alignment modes (ALIGNMENT_MODE, switchable via --alignment-mode):
#       "span" trusts the provider's returned <span id> boundaries; "marker"
#       (default) additionally prefixes each unit with an ⟦gID⟧ token and
#       ignores span boundaries on parse, splitting the flattened response
#       purely on these tokens so mid-batch mis-splitting cannot misalign
#       units. Units resolving to punctuation-only text where the source had
#       real content are treated as missing, feeding the existing retry path.
#     - Robust retry mechanism for failed or partially failed translation batches.
#     - Glossary terms sent as translate="no" spans (official untranslatable-
#       text markup) rather than placeholder tokens or pre-substituted target
#       text; matched spans are restored to their fixed target term locally
#       after translation, keeping the sentence intact for the provider.
#     - Single isolated retry (no loop) when the substituted result still
#       looks untranslated; kept only if the retry actually differs.
#     - Cue-level integrity check for multi-cue units (e.g. lyrics carrying
#       several ⟦cNNNN⟧-marked cues merged into one translation unit): any
#       cue marker swallowed by the provider during translation is detected
#       individually, not just whole-unit emptiness. Recovery cascades from
#       the existing windowed retry (unit-boundary markers now use a
#       separate ⟦uN⟧ namespace so they never collide with cues' own ⟦cNNNN⟧
#       markers) to a block-isolated fallback: the missing cue plus its 5
#       neighbors on each side are each wrapped in an independent <div>,
#       bypassing normal span-based batching so the provider cannot fuse
#       them; recovered cues are spliced back in, unrecovered ones are left
#       untouched rather than guessed at.
#     - Oversized atomic items (a single unit/cue whose text alone exceeds
#       batch_chars) are never truncated; they're excluded from batches and
#       reported as skipped with a clear reason instead of being sent as-is
#       or cut mid-cue.
#
# 功能:
#     - 基于 ThreadPoolExecutor 的并发 HTTP 请求。
#     - 章节感知分批：整章节（场景/歌曲片段）在字符数限制内打包进同一批
#       （DEFAULT_BATCH_CHARS），仅当单个章节超限时才按字符数兜底拆分。
#     - 使用 HTML 行内 <span> 标签在章节 <div> 内保护并隔离单元、确保对应
#       关系，令场景内上下文连续，同时场景间仍有边界信号。
#     - 两种对齐模式（ALIGNMENT_MODE，可通过 --alignment-mode 切换）：
#       "span" 信任供应商返回的 <span id> 边界；"marker"（默认）额外为每个
#       单元前置 ⟦gID⟧ 标记，解析时完全无视 span 边界，仅按该标记切分展平
#       后的响应文本，杜绝供应商在批内错误拆分内容导致的错位。译文剥离标点
#       后为空、但原文本身有实际内容的单元一律视为缺失，交由既有重试路径处理。
#     - 针对失败或部分失败请求的健壮重试机制。
#     - 词表命中片段以官方 translate="no" 标签发送（而非占位符或预先替换
#       为目标语译名），命中片段随句子一同送出、供应商正常翻译周围语境，
#       收到结果后在本地原样替换回固定译名。
#     - 替换后结果仍疑似未翻译时单独重发一次（不循环），结果不同才采用。
#     - 针对多cue合并单元（如歌词，多条⟦cNNNN⟧标记的cue合并进同一翻译单元）
#       做cue级完整性校验：任一cue标记被供应商吞并，精确定位到该cue而非仅
#       判断整个unit是否为空。修复按序回退：先复用既有窗口重跑（其自身的
#       unit边界标记已改用独立的⟦uN⟧命名空间，不再与cue自带的⟦cNNNN⟧标记
#       冲突）；仍缺失则对问题cue及前后各5条分别包裹独立<div>发送，绕开
#       span共享批处理以避免供应商融合它们；回收成功的cue原位拼回，回收
#       失败的cue原样保留，不做臆测性改写。
#     - 单个unit/cue自身文本即超出batch_chars时，绝不截断：该项被排除出
#       批次，以明确原因计入skipped上报，而非原样发送或从中截断。
#
# Usage / 用法:
#     python google_client.py --input extract.json --source-lang en --target-lang zh-CN --output translations.json
#
# Output / 输出:
#     Diagnostic logs (stderr) / 诊断日志（标准错误）:
#       - Progress reports, batch completion, error retries, final status.
#       - 进度报告、批次完成情况、错误重试信息、最终状态。
#
#     Result data (stdout) / 结果数据（标准输出）:
#       - A single JSON object containing 'translations' and 'skipped'.
#       - 包含 translations 与 skipped 字段的单个 JSON 对象。
#
# Exit codes / 退出码:
#     0    normal completion / 正常完成
#     130  interrupted by Ctrl+C / 被 Ctrl+C 中断
# ============================================================================
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_NAME = "google_client"

ENDPOINT = "https://translate-pa.googleapis.com/v1/translateHtml"
API_KEY_ENV = "GOOGLE_TRANSLATE_API_KEY"
DEFAULT_BATCH_CHARS = 3000
DEFAULT_CONCURRENCY = 8
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_DELAY = 3
PROGRESS_INTERVAL = 20

SPAN_PATTERN = re.compile(r'<span[^>]*id=["\']?([a-zA-Z0-9:]+)["\']?[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE)
ITALIC_PATTERN = re.compile(r"<i>.*?</i>", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEBUG_MODE = False
DEBUG_RAW_IN_FILE = None
DEBUG_RAW_OUT_FILE = None
DEBUG_LOCK = threading.Lock()

ALIGNMENT_MODE = "marker"
GROUP_MARKER_TEMPLATE = "\u27e6g{}\u27e7"
GROUP_MARKER_PATTERN = re.compile(r"\u27e6g([^\u27e6\u27e7]+)\u27e7")
CUE_MARKER_TEMPLATE = "\u27e6c{:04d}\u27e7"
CUE_MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7")
CONTENT_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)

NO_TRANSLATE_TEMPLATE = '<span translate="no">{}</span>'

WORD_BASED_SCRIPTS = {"latin", "cyrillic", "arabic", "devanagari", "hebrew", "greek"}
SCRIPT_CHAR_RANGES = {
    "latin": "A-Za-z",
    "cyrillic": "\u0400-\u04ff",
    "arabic": "\u0600-\u06ff",
    "devanagari": "\u0900-\u097f",
    "hebrew": "\u0590-\u05ff",
    "greek": "\u0370-\u03ff",
    "cjk": "\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af",
    "thai": "\u0e00-\u0e7f",
}
SCRIPT_LEAK_PATTERNS = {
    name: re.compile(f"[{chars}]{{2,}}" if name in WORD_BASED_SCRIPTS else f"[{chars}]")
    for name, chars in SCRIPT_CHAR_RANGES.items()
}
LANGUAGE_SCRIPTS = {
    "en": "latin", "es": "latin", "fr": "latin", "de": "latin", "it": "latin", "pt": "latin",
    "nl": "latin", "pl": "latin", "sv": "latin", "da": "latin", "no": "latin", "fi": "latin",
    "ro": "latin", "cs": "latin", "hu": "latin", "tr": "latin", "id": "latin", "vi": "latin",
    "ms": "latin", "tl": "latin", "ca": "latin", "eu": "latin", "gl": "latin", "la": "latin",
    "zh": "cjk", "ja": "cjk", "ko": "cjk",
    "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic",
    "ar": "arabic", "fa": "arabic", "ur": "arabic",
    "hi": "devanagari", "ne": "devanagari", "mr": "devanagari",
    "th": "thai", "he": "hebrew", "el": "greek",
}


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape_html(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


def has_content(text):
    return bool(text) and bool(CONTENT_CHAR_PATTERN.search(text))


def within_budget(text, limit):
    if len(text) <= limit:
        return True
    log(f"payload of {len(text)} chars exceeds budget ({limit}), refusing to truncate, skipping")
    return False


def split_oversized_chapter(items, batch_chars):
    pieces, piece, piece_chars, oversized = [], [], 0, []
    for item in items:
        item_chars = len(item["text"])
        if item_chars > batch_chars:
            oversized.append(item)
            continue
        if piece and piece_chars + item_chars > batch_chars:
            pieces.append(piece)
            piece, piece_chars = [], 0
        piece.append(item)
        piece_chars += item_chars
    if piece:
        pieces.append(piece)
    return pieces, oversized


def build_batches(items, chapter_groups, batch_chars):
    by_id = {item["id"]: item for item in items}
    batches, oversized = [], []
    current, current_chars = [], 0

    def flush():
        nonlocal current, current_chars
        if current:
            batches.append(current)
        current, current_chars = [], 0

    for group in chapter_groups:
        group_items = [by_id[i] for i in group if i in by_id]
        if not group_items:
            continue
        group_chars = sum(len(item["text"]) for item in group_items)
        if group_chars > batch_chars:
            flush()
            pieces, group_oversized = split_oversized_chapter(group_items, batch_chars)
            batches.extend([piece] for piece in pieces)
            oversized.extend(group_oversized)
        elif current_chars + group_chars > batch_chars:
            flush()
            current, current_chars = [group_items], group_chars
        else:
            current.append(group_items)
            current_chars += group_chars
    flush()
    return batches, oversized


def build_chapter_html(group, indices):
    marker = ALIGNMENT_MODE == "marker"
    spans = "".join(
        f'<span id={indices[item["id"]]}>'
        f'{GROUP_MARKER_TEMPLATE.format(indices[item["id"]]) if marker else ""}'
        f'{item.get("html", escape_html(item["text"]))}</span>'
        for item in group
    )
    return f"<div>{spans}</div>"


def parse_by_spans(html):
    result = {}
    for match in SPAN_PATTERN.finditer(html):
        idx = int(match.group(1))
        text = unescape_html(ITALIC_PATTERN.sub("", match.group(2))).strip()
        result[idx] = f"{result[idx]} {text}" if idx in result else text
    return result


def parse_by_markers(html):
    flat = unescape_html(TAG_PATTERN.sub("", ITALIC_PATTERN.sub("", html)))
    parts = GROUP_MARKER_PATTERN.split(flat)
    result = {}
    for i in range(1, len(parts), 2):
        if parts[i].isdigit():
            result[int(parts[i])] = parts[i + 1].strip()
    return result


def parse_translated_html(html):
    result = parse_by_markers(html) if ALIGNMENT_MODE == "marker" else parse_by_spans(html)
    if DEBUG_MODE and not result:
        with DEBUG_LOCK:
            log(f"debug: parse_translated_html found NO matching blocks in HTML. Head: {html[:200]}")
    return result


def post_translate_html(html, source_lang, target_lang, api_key):
    body = json.dumps([[[html], source_lang, target_lang], "te"]).encode("utf-8")
    if DEBUG_RAW_IN_FILE:
        with DEBUG_LOCK, open(DEBUG_RAW_IN_FILE, "ab") as f:
            f.write(body + b"\n")

    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json+protobuf", "X-goog-api-key": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw_response = response.read()
        if DEBUG_RAW_OUT_FILE:
            with DEBUG_LOCK, open(DEBUG_RAW_OUT_FILE, "ab") as f:
                f.write(raw_response + b"\n")
        payload = json.loads(raw_response.decode("utf-8"))
    return payload[0][0]


def call_google(batch, source_lang, target_lang, api_key):
    items = [item for group in batch for item in group]
    indices = {item["id"]: i for i, item in enumerate(items, start=1)}
    id_by_index = {i: item_id for item_id, i in indices.items()}
    html = "".join(build_chapter_html(group, indices) for group in batch)
    translated_html = post_translate_html(html, source_lang, target_lang, api_key)
    parsed = parse_translated_html(translated_html)
    source_by_id = {item["id"]: item["text"] for item in items}
    result = {}
    for idx, text in parsed.items():
        item_id = id_by_index.get(idx)
        if item_id is None:
            continue
        if has_content(text) or not has_content(source_by_id.get(item_id, "")):
            result[item_id] = text
    return result


def translate_batch(batch, source_lang, target_lang, api_key):
    items = [item for group in batch for item in group]
    expected_ids = {item["id"] for item in items}
    result, missing = {}, expected_ids
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = call_google(batch, source_lang, target_lang, api_key)
        except Exception as e:
            log(f"attempt {attempt} failed: {e}")
            result = {}
        missing = expected_ids - result.keys()
        if not missing:
            return result, []

        missing_units = sorted({str(i) for i in missing})
        log(f"attempt {attempt}: missing {len(missing)} of {len(items)} units: {', '.join(missing_units)}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    if len(items) > 1:
        by_id = {item["id"]: item for item in items}
        log(f"isolating {len(missing)} unit(s) still missing: {', '.join(sorted({str(i) for i in missing}))}")
        for uid in sorted(missing, key=str):
            solo_result, _solo_missing = translate_batch([[by_id[uid]]], source_lang, target_lang, api_key)
            if uid in solo_result:
                result[uid] = solo_result[uid]
        missing = expected_ids - result.keys()
    return result, sorted(missing, key=str)


def translate(items, chapter_groups, source_lang, target_lang, api_key, batch_chars, concurrency=DEFAULT_CONCURRENCY):
    translations, skipped = {}, []
    batches, oversized = build_batches(items, chapter_groups, batch_chars)
    for item in oversized:
        log(f"unit {item['id']}: {len(item['text'])} chars exceeds batch_chars ({batch_chars}), "
            f"cue-level content cannot be split further, skipping without truncation")
        skipped.append(item["id"])
    total_batches = len(batches)
    start_time = last_report = time.time()
    completed = 0
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(translate_batch, batch, source_lang, target_lang, api_key): batch for batch in batches}
        for future in as_completed(futures):
            result, missing = future.result()
            with progress_lock:
                translations.update(result)
                skipped.extend(missing)
                completed += 1
                now = time.time()
                should_log = now - last_report >= PROGRESS_INTERVAL or completed == total_batches
                if should_log:
                    log(f"progress: {len(translations)}/{len(items)} units "
                        f"(batch {completed}/{total_batches}, {now - start_time:.0f}s elapsed)")
                    last_report = now
    return translations, skipped


def script_of(lang):
    return LANGUAGE_SCRIPTS.get((lang or "").split("-")[0].lower())


def is_untranslated(text, source_lang, target_lang):
    if not text:
        return False
    source_script, target_script = script_of(source_lang), script_of(target_lang)
    if not source_script or not target_script or source_script == target_script:
        return False
    return len(SCRIPT_LEAK_PATTERNS[source_script].findall(text)) > 1


def protect_terms_html(text, term_matches):
    pieces, cursor = [], 0
    for match in term_matches:
        pieces.append(escape_html(text[cursor:match["start"]]))
        pieces.append(NO_TRANSLATE_TEMPLATE.format(escape_html(match["matched"])))
        cursor = match["end"]
    pieces.append(escape_html(text[cursor:]))
    return "".join(pieces)


def flatten_units(units, chapter_of_unit):
    items, chapter_items = [], {}
    for unit in units:
        chapter_id = chapter_of_unit.get(unit["id"])
        items.append({"id": unit["id"], "text": unit["text"], "html": protect_terms_html(unit["text"], unit.get("term_matches") or [])})
        chapter_items.setdefault(chapter_id, []).append(unit["id"])
    return items, list(chapter_items.values())


def apply_term_replacements(text, term_matches, target_lang):
    if not text or not term_matches:
        return text
    boundary = r"\s*" if script_of(target_lang) == "cjk" else r"\b"
    seen = {}
    for match in term_matches:
        seen.setdefault(match["matched"], match["target"])
    for source_text, target_text in sorted(seen.items(), key=lambda kv: -len(kv[0])):
        pattern = re.compile(boundary + re.escape(source_text) + boundary)
        text = pattern.sub(lambda _m, t=target_text: t, text)
    return text


def retry_single(text, source_lang, target_lang, api_key):
    if not text or not text.strip():
        return None
    result, _missing = translate_batch([[{"id": "retry", "text": text}]], source_lang, target_lang, api_key)
    return result.get("retry")


UNIT_MARKER_TEMPLATE = "\u27e6u{}\u27e7"
UNIT_MARKER_PATTERN = re.compile(r"\u27e6u([^\u27e6\u27e7]+)\u27e7")
WINDOW_CONTEXT_RADIUS = 20
WINDOW_KEEP_RADIUS = 2
ISOLATED_CUE_RADIUS = 5
LENGTH_RATIO_MIN = 0.15
LENGTH_RATIO_MAX = 6.0


def content_length(text):
    return len(CONTENT_CHAR_PATTERN.findall(text or ""))


def is_length_plausible(source_text, translated_text):
    source_len = content_length(source_text)
    if source_len == 0:
        return True
    ratio = content_length(translated_text) / source_len
    return LENGTH_RATIO_MIN <= ratio <= LENGTH_RATIO_MAX


def retry_windowed(units, suspect_id, source_lang, target_lang, api_key, batch_chars):
    index = {unit["id"]: i for i, unit in enumerate(units)}
    i = index[suspect_id]
    window = units[max(0, i - WINDOW_CONTEXT_RADIUS):i + WINDOW_CONTEXT_RADIUS + 1]
    if len(window) < 2:
        return {}
    pieces = [window[0]["text"]]
    for unit in window[1:]:
        pieces.append(f" {UNIT_MARKER_TEMPLATE.format(unit['id'])} ")
        pieces.append(unit["text"])
    windowed_text = "".join(pieces)
    if not within_budget(windowed_text, batch_chars):
        return {}
    result, _missing = translate_batch([[{"id": "window", "text": windowed_text}]], source_lang, target_lang, api_key)
    response = result.get("window")
    if response is None:
        return {}
    expected_ids = [unit["id"] for unit in window[1:]]
    found_ids = [int(g) for g in UNIT_MARKER_PATTERN.findall(response)]
    if found_ids != expected_ids:
        return {}
    chunks = UNIT_MARKER_PATTERN.split(response)[0::2]
    keep_ids = {unit["id"] for unit in units[max(0, i - WINDOW_KEEP_RADIUS):i + WINDOW_KEEP_RADIUS + 1]}
    return {unit["id"]: chunk.strip() for unit, chunk in zip(window, chunks) if unit["id"] in keep_ids}


def expected_cue_ids(unit):
    return [s["id"] for s in unit["spans"] if s.get("boundary") == "marker"]


def split_cue_chunks(text):
    parts = CUE_MARKER_PATTERN.split(text or "")
    return {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts), 2)}


def missing_cue_ids(unit, text):
    expected = expected_cue_ids(unit)
    if not expected:
        return []
    present = split_cue_chunks(text)
    return [cid for cid in expected if cid not in present]


def patch_missing_cues(text, expected_ids, recovered):
    if not recovered:
        return text
    chunks = split_cue_chunks(text)
    chunks.update(recovered)
    return " ".join(f"{CUE_MARKER_TEMPLATE.format(cid)} {chunks[cid]}" for cid in expected_ids if cid in chunks)


def build_isolated_divs(cue_ids, cue_text_by_id):
    return "".join(
        f"<div>{CUE_MARKER_TEMPLATE.format(cid)} {escape_html(cue_text_by_id[cid])}</div>"
        for cid in cue_ids if cid in cue_text_by_id
    )


def retry_isolated_cues(missing_ids, cue_order, cue_text_by_id, source_lang, target_lang, api_key, batch_chars):
    position = {cid: i for i, cid in enumerate(cue_order)}
    positions = sorted(position[cid] for cid in missing_ids if cid in position)
    if not positions:
        return {}
    lo = max(0, positions[0] - ISOLATED_CUE_RADIUS)
    hi = min(len(cue_order) - 1, positions[-1] + ISOLATED_CUE_RADIUS)
    html = build_isolated_divs(cue_order[lo:hi + 1], cue_text_by_id)
    if not within_budget(html, batch_chars):
        return {}
    try:
        translated_html = post_translate_html(html, source_lang, target_lang, api_key)
    except Exception as e:
        log(f"isolated cue retry failed: {e}")
        return {}
    flat = unescape_html(TAG_PATTERN.sub("", translated_html))
    recovered = split_cue_chunks(flat)
    return {
        cid: text for cid, text in recovered.items()
        if cid in missing_ids and has_content(text)
        and is_length_plausible(cue_text_by_id.get(cid, ""), text)
    }


def translate_units(units, chapters, cues, source_lang, target_lang, api_key, batch_chars, concurrency):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    chapter_of_unit = {uid: chapter["id"] for chapter in chapters for uid in chapter["unit_ids"]}
    items, chapter_groups = flatten_units(pending, chapter_of_unit)
    translations_raw, _skipped = translate(items, chapter_groups, source_lang, target_lang, api_key, batch_chars, concurrency) if items else ({}, [])

    results = dict(resolved)
    for unit in pending:
        raw_text = translations_raw.get(unit["id"])
        final_text = apply_term_replacements(raw_text, unit.get("term_matches") or [], target_lang) if raw_text is not None else None
        if final_text is not None and is_untranslated(final_text, source_lang, target_lang):
            retried = retry_single(unit["text"], source_lang, target_lang, api_key)
            if retried:
                candidate = apply_term_replacements(retried, unit.get("term_matches") or [], target_lang)
                if candidate != final_text:
                    log(f"unit {unit['id']}: retry changed result")
                    final_text = candidate
        results[unit["id"]] = final_text

    unit_by_id = {unit["id"]: unit for unit in units}
    length_suspects = {uid for uid, text in results.items()
                        if text is not None and has_content(unit_by_id[uid]["text"])
                        and (not has_content(text) or not is_length_plausible(unit_by_id[uid]["text"], text))}
    cue_suspects = {uid for uid, text in results.items()
                    if text is not None and missing_cue_ids(unit_by_id[uid], text)}
    cue_order = [c["id"] for c in cues]
    cue_text_by_id = {c["id"]: c["text"] for c in cues}

    for uid in sorted(length_suspects | cue_suspects):
        recovered = retry_windowed(units, uid, source_lang, target_lang, api_key, batch_chars)
        if recovered:
            recovered = {rid: apply_term_replacements(text, unit_by_id[rid].get("term_matches") or [], target_lang)
                         for rid, text in recovered.items()}
            log(f"windowed retry around unit {uid}: recovered {sorted(recovered)}")
            results.update(recovered)
        else:
            log(f"windowed retry around unit {uid}: markers did not align, left as-is")

        remaining = missing_cue_ids(unit_by_id[uid], results[uid])
        if not remaining:
            continue
        recovered_cues = retry_isolated_cues(remaining, cue_order, cue_text_by_id, source_lang, target_lang, api_key, batch_chars)
        if recovered_cues:
            results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), recovered_cues)
            log(f"isolated cue retry for unit {uid}: recovered cues {sorted(recovered_cues)}")
        else:
            log(f"isolated cue retry for unit {uid}: cues {remaining} still missing, left as-is")

    skipped = [uid for uid, text in results.items() if text is None]
    translations = {str(uid): text for uid, text in results.items() if text is not None}
    return translations, skipped


def main():
    global DEBUG_MODE, DEBUG_RAW_IN_FILE, DEBUG_RAW_OUT_FILE, ALIGNMENT_MODE
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default=None)
    parser.add_argument("--target-lang", default=None)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--alignment-mode", choices=["span", "marker"], default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-raw-in", default=None)
    parser.add_argument("--debug-raw-out", default=None)
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"
    DEBUG_RAW_IN_FILE = args.debug_raw_in
    DEBUG_RAW_OUT_FILE = args.debug_raw_out
    if args.alignment_mode:
        ALIGNMENT_MODE = args.alignment_mode

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    payload = json.loads(raw)
    units = payload.get("units", [])
    chapters = payload.get("chapters", [])
    cues = payload.get("cues", [])
    source_lang = args.source_lang or payload.get("source_lang", "en")
    target_lang = args.target_lang or payload.get("target_lang", "zh-CN")
    api_key = args.api_key or os.environ.get(API_KEY_ENV)

    if not api_key:
        result = {"success": False, "reason": "missing_api_key", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    elif not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    else:
        translations, skipped = translate_units(units, chapters, cues, source_lang, target_lang, api_key, args.batch_chars, args.concurrency)
        result = {
            "success": bool(translations),
            "translations": translations,
            "skipped": skipped,
            "provider": "google",
            "source_lang": source_lang,
            "target_lang": target_lang,
        }
    log(f"status: {'ok' if result['success'] else 'failed'} (translated={len(result['translations'])}, skipped={len(result.get('skipped', []))})")

    output = json.dumps(result, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        print(output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
