#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 1.9
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
#     v1.5: Batch payload switched from block-level `<div>` (one per unit,
#     newline-joined) to inline `<span>` (concatenated with no separator).
#     Block elements are a natural segmentation signal to NMT engines even
#     within a single request, silencing cross-unit context; inline elements
#     carry no such signal, letting the whole batch read as one continuous
#     passage. Effect should be confirmed against real API output, not
#     assumed from the tag semantics alone.
#     v1.5: 批量payload由block级`<div>`（每unit一个，换行分隔）改为行内
#     `<span>`（无分隔符直接拼接）。block级元素即便在同一次请求内也是NMT
#     引擎天然的分段信号，会削弱跨unit上下文；行内元素不带这层信号，让整批
#     文本读起来是连续一段。实际效果需拿真实API返回核实，不能仅凭标签语义
#     假定生效。
#
# Features:
#     - Concurrent HTTP requests via ThreadPoolExecutor.
#     - Smart text batching based on character limits (DEFAULT_BATCH_CHARS).
#     - Protective inline HTML formatting (<span> tags) to isolate units and
#       map results, without introducing block-level segmentation signals.
#     - Robust retry mechanism for failed or partially failed translation batches.
#     - Per-unit inline-name / placeholder dual variants, chosen by an
#       untranslated-residue diagnostic (Latin<->CJK word/char counting).
#     - Single isolated retry (no loop) when the final chosen result still
#       looks untranslated; kept only if the retry actually differs.
#
# 功能:
#     - 基于 ThreadPoolExecutor 的并发 HTTP 请求。
#     - 基于字符数限制（DEFAULT_BATCH_CHARS）的智能文本分批。
#     - 使用 HTML 行内 <span> 标签保护并隔离单元、确保对应关系，同时不引入
#       block级分段信号。
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

SPAN_PATTERN = re.compile(r'<span[^>]*id=["\']?([a-zA-Z0-9]+)["\']?[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE)
ITALIC_PATTERN = re.compile(r"<i>.*?</i>", re.DOTALL)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEBUG_MODE = False
DEBUG_RAW_IN_FILE = None
DEBUG_RAW_OUT_FILE = None
DEBUG_LOCK = threading.Lock()

EMBED_RATIO_THRESHOLD = 0.30
TERM_PLACEHOLDER_TEMPLATE = "\u27e6T{:02d}\u27e7"
VARIANT_PRIORITY = ("embedded", "placeholder", "plain")

LATIN_LANGS = {"en", "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "da", "no", "fi", "ro", "cs", "hu", "tr", "id", "vi", "ms", "tl", "ca", "eu", "gl", "la"}
NON_LATIN_LANGS = {"zh", "ja", "ko", "ru", "uk", "ar", "he", "hi", "th", "el", "bg", "fa"}
LATIN_WORD_PATTERN = re.compile(r"[a-zA-Z]{2,}")
NON_LATIN_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]")


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def escape_html(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def unescape_html(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


def build_batches(units, batch_chars):
    batches = []
    current, current_chars = [], 0
    for unit in units:
        unit_chars = len(unit["text"])
        if current and current_chars + unit_chars > batch_chars:
            batches.append(current)
            current, current_chars = [], 0
        current.append(unit)
        current_chars += unit_chars
    if current:
        batches.append(current)
    return batches


def build_request_body(batch, source_lang, target_lang):
    html = "".join(f'<span id="{unit["id"]}">{escape_html(unit["text"])}</span>' for unit in batch)
    return json.dumps([[[html], source_lang, target_lang], "te"]).encode("utf-8")


def parse_translated_html(html):
    result = {}
    for match in SPAN_PATTERN.finditer(html):
        raw_idx = match.group(1)
        idx = int(raw_idx) if raw_idx.isdigit() else raw_idx
        text = unescape_html(ITALIC_PATTERN.sub("", match.group(2))).strip()
        result[idx] = f"{result[idx]} {text}" if idx in result else text
    if DEBUG_MODE and not result:
        with DEBUG_LOCK:
            log(f"debug: parse_translated_html found NO matching blocks in HTML. Head: {html[:200]}")
    return result


def call_google(batch, source_lang, target_lang, api_key):
    body = build_request_body(batch, source_lang, target_lang)
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
    return parse_translated_html(payload[0][0])


def translate_batch(batch, source_lang, target_lang, api_key):
    expected_ids = {unit["id"] for unit in batch}
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
            
        log(f"attempt {attempt}: missing {len(missing)} of {len(batch)} units")
        if DEBUG_MODE:
            with DEBUG_LOCK:
                log(f"debug: Expected IDs: {sorted(list(expected_ids), key=str)}")
                log(f"debug: Received IDs: {sorted(list(result.keys()), key=str)}")
                
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return result, sorted(missing, key=str)


def translate(units, source_lang, target_lang, api_key, batch_chars, concurrency=DEFAULT_CONCURRENCY):
    translations, skipped = {}, []
    batches = build_batches(units, batch_chars)
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
                    log(f"progress: {len(translations)}/{len(units)} units "
                        f"(batch {completed}/{total_batches}, {now - start_time:.0f}s elapsed)")
                    last_report = now
    return translations, skipped


def is_untranslated(text, source_lang, target_lang):
    if not text or not source_lang or not target_lang:
        return False
    s, t = source_lang.split("-")[0].lower(), target_lang.split("-")[0].lower()
    if s in LATIN_LANGS and t in NON_LATIN_LANGS:
        return len(LATIN_WORD_PATTERN.findall(text)) > 1
    if s in NON_LATIN_LANGS and t in LATIN_LANGS:
        return len(NON_LATIN_CHAR_PATTERN.findall(text)) > 1
    return False


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


def flatten_units(units):
    items, index_map = [], {}
    for unit in units:
        for variant, (text, _mapping) in build_variants(unit).items():
            idx = len(items)
            items.append({"id": idx, "text": text})
            index_map[idx] = f"{unit['id']}:{variant}"
    return items, index_map


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
    result, _missing = translate_batch([{"id": "retry", "text": text}], source_lang, target_lang, api_key)
    return result.get("retry")


def translate_units(units, source_lang, target_lang, api_key, batch_chars, concurrency):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    items, index_map = flatten_units(pending)
    indexed_raw, _skipped = translate(items, source_lang, target_lang, api_key, batch_chars, concurrency) if items else ({}, [])
    translations_raw = {index_map[idx]: text for idx, text in indexed_raw.items()}

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

    skipped = [uid for uid, text in results.items() if text is None]
    translations = {str(uid): text for uid, text in results.items() if text is not None}
    return translations, skipped


def main():
    global DEBUG_MODE, DEBUG_RAW_IN_FILE, DEBUG_RAW_OUT_FILE
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default=None)
    parser.add_argument("--target-lang", default=None)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-raw-in", default=None)
    parser.add_argument("--debug-raw-out", default=None)
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"
    DEBUG_RAW_IN_FILE = args.debug_raw_in
    DEBUG_RAW_OUT_FILE = args.debug_raw_out

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    payload = json.loads(raw)
    units = payload.get("units", [])
    source_lang = args.source_lang or payload.get("source_lang", "en")
    target_lang = args.target_lang or payload.get("target_lang", "zh-CN")
    api_key = args.api_key or os.environ.get(API_KEY_ENV)

    if not api_key:
        result = {"success": False, "reason": "missing_api_key", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    elif not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": [], "source_lang": source_lang, "target_lang": target_lang}
    else:
        translations, skipped = translate_units(units, source_lang, target_lang, api_key, args.batch_chars, args.concurrency)
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
