#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Batches and translates subtitle units using Google Translate's PA endpoint.
#     Employs concurrent threading for faster translation, wraps text in HTML 
#     anchors to preserve alignment, and handles retries/fallbacks automatically.
#     使用 Google Translate PA 接口进行字幕单元批量机器翻译。
#     采用并发线程池加速翻译，通过 HTML anchor 标签包裹文本以保留对应关系，
#     并自动处理请求重试与失败回退。
#
# Features:
#     - Concurrent HTTP requests via ThreadPoolExecutor.
#     - Smart text batching based on character limits (DEFAULT_BATCH_CHARS).
#     - Protective HTML formatting (<a> tags) to isolate lines and map results.
#     - Robust retry mechanism for failed or partially failed translation batches.
#
# 功能:
#     - 基于 ThreadPoolExecutor 的并发 HTTP 请求。
#     - 基于字符数限制（DEFAULT_BATCH_CHARS）的智能文本分批。
#     - 使用 HTML <a> 标签保护并隔离行文本，确保原译文精准映射。
#     - 针对失败或部分失败请求的健壮重试机制。
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
DEFAULT_BATCH_CHARS = 1800
DEFAULT_CONCURRENCY = 8
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_DELAY = 3
PROGRESS_INTERVAL = 20

ANCHOR_PATTERN = re.compile(r"<a i=(\d+)>(.*?)</a>", re.DOTALL)
ITALIC_PATTERN = re.compile(r"<i>.*?</i>", re.DOTALL)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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
    html = "\n".join(f"<a i={unit['id']}>{escape_html(unit['text'])}</a>" for unit in batch)
    return json.dumps([[[f"<pre>{html}</pre>"], source_lang, target_lang], "te"]).encode("utf-8")


def parse_translated_html(html):
    result = {}
    for match in ANCHOR_PATTERN.finditer(html):
        idx = int(match.group(1))
        text = unescape_html(ITALIC_PATTERN.sub("", match.group(2)))
        result[idx] = f"{result[idx]} {text}" if idx in result else text
    return result


def call_google(batch, source_lang, target_lang, api_key):
    request = urllib.request.Request(
        ENDPOINT,
        data=build_request_body(batch, source_lang, target_lang),
        headers={"Content-Type": "application/json+protobuf", "X-goog-api-key": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
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
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return result, sorted(missing)


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default=None)
    parser.add_argument("--target-lang", default=None)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    payload = json.loads(raw)
    units = payload.get("units", [])
    source_lang = args.source_lang or payload.get("source_lang", "en")
    target_lang = args.target_lang or payload.get("target_lang", "zh-CN")
    api_key = args.api_key or os.environ.get(API_KEY_ENV)

    if not api_key:
        result = {"success": False, "reason": "missing_api_key", "translations": {}, "skipped": []}
    elif not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": []}
    else:
        resolved = {str(unit["id"]): unit["resolved"] for unit in units if unit.get("resolved")}
        translatable = [unit for unit in units if not unit.get("resolved")]
        translations_raw, skipped = translate(translatable, source_lang, target_lang, api_key, args.batch_chars, args.concurrency) if translatable else ({}, [])
        translations = {str(k): v for k, v in translations_raw.items()}
        translations.update(resolved)
        result = {
            "success": bool(translations),
            "translations": translations,
            "skipped": skipped,
            "provider": "google",
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
