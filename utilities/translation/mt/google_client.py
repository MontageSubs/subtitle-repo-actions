#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 2.3.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Batches and translates subtitle units using Google Translate's PA endpoint.
#     Employs concurrent threading for faster translation, wraps text in HTML
#     inline anchors to preserve alignment, and handles retries/fallbacks automatically.
#     For units carrying glossary `term_matches`, builds an inline-name variant
#     (real target term embedded in the source sentence) and/or a placeholder
#     variant per unit, picks the better result via an untranslated-residue
#     diagnostic, restores placeholders locally, and issues a single isolated
#     retry when the chosen result still looks untranslated.
#     使用 Google Translate PA 接口进行字幕单元批量机器翻译。采用并发线程池
#     加速翻译，通过 HTML 行内标签包裹文本以保留对应关系，并自动处理
#     请求重试与失败回退。对携带词表命中（term_matches）的单元，按单元自身
#     的嵌入比例生成"固定译名直接嵌入原文"与/或"占位符"两个版本分别发送，
#     依据未翻译残留诊断择优采用并在本地回填占位符；若最终结果仍疑似未
#     翻译，单独重发一次原句作为质量兜底。
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
#     - Per-unit inline-name / placeholder dual variants, chosen by an
#       untranslated-residue diagnostic (Latin<->CJK word/char counting).
#     - Single isolated retry (no loop) when the final chosen result still
#       looks untranslated; kept only if the retry actually differs.
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
#     - 按单元自身嵌入比例生成嵌入版/占位符版，依未翻译诊断择优并回填。
#     - 最终结果仍疑似未翻译时单独重发一次（不循环），结果不同才采用。
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
CONTENT_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)

EMBED_RATIO_THRESHOLD = 0.30
TERM_PLACEHOLDER_TEMPLATE = "\u27e6T{:02d}\u27e7"
VARIANT_PRIORITY = ("embedded", "placeholder", "plain")

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


def split_oversized_chapter(items, batch_chars):
    pieces, piece, piece_chars = [], [], 0
    for item in items:
        item_chars = len(item["text"])
        if piece and piece_chars + item_chars > batch_chars:
            pieces.append(piece)
            piece, piece_chars = [], 0
        piece.append(item)
        piece_chars += item_chars
    if piece:
        pieces.append(piece)
    return pieces


def build_batches(items, chapter_groups, batch_chars):
    by_id = {item["id"]: item for item in items}
    batches = []
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
            batches.extend([piece] for piece in split_oversized_chapter(group_items, batch_chars))
        elif current_chars + group_chars > batch_chars:
            flush()
            current, current_chars = [group_items], group_chars
        else:
            current.append(group_items)
            current_chars += group_chars
    flush()
    return batches


def build_chapter_html(group, indices):
    marker = ALIGNMENT_MODE == "marker"
    spans = "".join(
        f'<span id={indices[item["id"]]}>'
        f'{GROUP_MARKER_TEMPLATE.format(indices[item["id"]]) if marker else ""}'
        f'{escape_html(item["text"])}</span>'
        for item in group
    )
    return f"<div>{spans}</div>"


def build_request_body(batch, source_lang, target_lang, indices):
    html = "".join(build_chapter_html(group, indices) for group in batch)
    return json.dumps([[[html], source_lang, target_lang], "te"]).encode("utf-8")


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


def call_google(batch, source_lang, target_lang, api_key):
    items = [item for group in batch for item in group]
    indices = {item["id"]: i for i, item in enumerate(items, start=1)}
    id_by_index = {i: item_id for item_id, i in indices.items()}
    body = build_request_body(batch, source_lang, target_lang, indices)
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
    parsed = parse_translated_html(payload[0][0])
    source_by_id = {item["id"]: item["text"] for item in items}
    result = {}
    for idx, text in parsed.items():
        item_id = id_by_index.get(idx)
        if item_id is None:
            continue
        if has_content(text) or not has_content(source_by_id.get(item_id, "")):
            result[item_id] = text
    return result


def cue_ref(item_id):
    return str(item_id).split(":", 1)[0]


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

        missing_cues = sorted({cue_ref(i) for i in missing}, key=str)
        log(f"attempt {attempt}: missing {len(missing)} of {len(items)} units, cues: {', '.join(missing_cues)}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    if len(items) > 1:
        by_id = {item["id"]: item for item in items}
        log(f"isolating {len(missing)} unit(s) still missing, cues: {', '.join(sorted({cue_ref(i) for i in missing}, key=str))}")
        for uid in sorted(missing, key=str):
            solo_result, _solo_missing = translate_batch([[by_id[uid]]], source_lang, target_lang, api_key)
            if uid in solo_result:
                result[uid] = solo_result[uid]
        missing = expected_ids - result.keys()
    return result, sorted(missing, key=str)


def translate(items, chapter_groups, source_lang, target_lang, api_key, batch_chars, concurrency=DEFAULT_CONCURRENCY):
    translations, skipped = {}, []
    batches = build_batches(items, chapter_groups, batch_chars)
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


def apply_term_matches(text, term_matches, variant):
    pieces, cursor, mapping = [], 0, {}
    for idx, match in enumerate(term_matches):
        pieces.append(text[cursor:match["start"]])
        if variant == "embedded":
            pieces.append(match["target"])
        else:
            placeholder = TERM_PLACEHOLDER_TEMPLATE.format(idx)
            mapping[placeholder] = match["target"]
            pieces.append(placeholder)
        cursor = match["end"]
    pieces.append(text[cursor:])
    return "".join(pieces), mapping


def build_variants(unit):
    text, matches, ratio = unit["text"], unit.get("term_matches") or [], unit.get("embed_ratio", 0.0)
    if not matches:
        return {"plain": (text, {})}
    if ratio > EMBED_RATIO_THRESHOLD:
        return {"placeholder": apply_term_matches(text, matches, "placeholder")}
    return {
        "embedded": apply_term_matches(text, matches, "embedded"),
        "placeholder": apply_term_matches(text, matches, "placeholder"),
    }


def flatten_units(units, chapter_of_unit):
    items, chapter_items = [], {}
    for unit in units:
        chapter_id = chapter_of_unit.get(unit["id"])
        for variant, (text, _mapping) in build_variants(unit).items():
            item_id = f"{unit['id']}:{variant}"
            items.append({"id": item_id, "text": text})
            chapter_items.setdefault(chapter_id, []).append(item_id)
    return items, list(chapter_items.values())


def restore_placeholders(text, mapping):
    for placeholder, target in mapping.items():
        text = text.replace(placeholder, target)
    return text


def resolve_translation(unit, translations, source_lang, target_lang):
    variants = build_variants(unit)
    for variant in VARIANT_PRIORITY:
        if variant not in variants:
            continue
        source_text, mapping = variants[variant]
        result = translations.get(f"{unit['id']}:{variant}")
        if result is None:
            continue
        if variant == "embedded" and "placeholder" in variants and is_untranslated(result, source_lang, target_lang):
            continue
        return restore_placeholders(result, mapping), source_text, mapping
    return None, None, None


def retry_single(text, source_lang, target_lang, api_key):
    if not text or not text.strip():
        return None
    result, _missing = translate_batch([[{"id": "retry", "text": text}]], source_lang, target_lang, api_key)
    return result.get("retry")


WINDOW_MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7")
WINDOW_CONTEXT_RADIUS = 20
WINDOW_KEEP_RADIUS = 2
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


def retry_windowed(units, suspect_id, source_lang, target_lang, api_key):
    index = {unit["id"]: i for i, unit in enumerate(units)}
    i = index[suspect_id]
    window = units[max(0, i - WINDOW_CONTEXT_RADIUS):i + WINDOW_CONTEXT_RADIUS + 1]
    if len(window) < 2:
        return {}
    pieces = [window[0]["text"]]
    for unit in window[1:]:
        pieces.append(f" \u27e6c{unit['id']:04d}\u27e7 ")
        pieces.append(unit["text"])
    windowed_text = "".join(pieces)
    result, _missing = translate_batch([[{"id": "window", "text": windowed_text}]], source_lang, target_lang, api_key)
    response = result.get("window")
    if response is None:
        return {}
    expected_ids = [unit["id"] for unit in window[1:]]
    found_ids = [int(g) for g in WINDOW_MARKER_PATTERN.findall(response)]
    if found_ids != expected_ids:
        return {}
    chunks = WINDOW_MARKER_PATTERN.split(response)[0::2]
    keep_ids = {unit["id"] for unit in units[max(0, i - WINDOW_KEEP_RADIUS):i + WINDOW_KEEP_RADIUS + 1]}
    return {unit["id"]: chunk.strip() for unit, chunk in zip(window, chunks) if unit["id"] in keep_ids}


def translate_units(units, chapters, source_lang, target_lang, api_key, batch_chars, concurrency):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    chapter_of_unit = {uid: chapter["id"] for chapter in chapters for uid in chapter["unit_ids"]}
    items, chapter_groups = flatten_units(pending, chapter_of_unit)
    translations_raw, _skipped = translate(items, chapter_groups, source_lang, target_lang, api_key, batch_chars, concurrency) if items else ({}, [])

    results = dict(resolved)
    for unit in pending:
        final_text, source_text, mapping = resolve_translation(unit, translations_raw, source_lang, target_lang)
        if final_text is not None and is_untranslated(final_text, source_lang, target_lang):
            retried = retry_single(source_text, source_lang, target_lang, api_key)
            if retried:
                candidate = restore_placeholders(retried, mapping)
                if candidate != final_text:
                    log(f"unit {unit['id']}: retry changed result")
                    final_text = candidate
        results[unit["id"]] = final_text

    unit_by_id = {unit["id"]: unit for unit in units}
    suspects = [uid for uid, text in results.items()
                if text is not None and has_content(unit_by_id[uid]["text"])
                and (not has_content(text) or not is_length_plausible(unit_by_id[uid]["text"], text))]
    for uid in suspects:
        recovered = retry_windowed(units, uid, source_lang, target_lang, api_key)
        if recovered:
            log(f"windowed retry around unit {uid}: recovered {sorted(recovered)}")
            results.update(recovered)
        else:
            log(f"windowed retry around unit {uid}: markers did not align, left as-is")

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
    source_lang = args.source_lang or payload.get("source_lang", "en")
    target_lang = args.target_lang or payload.get("target_lang", "zh-CN")
    api_key = args.api_key or os.environ.get(API_KEY_ENV)

    if not api_key:
        result = {"success": False, "reason": "missing_api_key", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    elif not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    else:
        translations, skipped = translate_units(units, chapters, source_lang, target_lang, api_key, args.batch_chars, args.concurrency)
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
