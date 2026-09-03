#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 2.16.2
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p), Joey
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Batch-translates subtitle units through Google Translate's internal PA
#     endpoint (translateHtml), built for the volume and reliability demands
#     of unattended CI runs rather than interactive one-off requests. Units
#     are grouped by chapter (a scene or song run) into HTML documents, sent
#     concurrently, and reassembled through a marker-based alignment scheme
#     that survives the provider occasionally re-splitting or dropping
#     content. Glossary terms and certain structural markers are protected
#     from translation via official untranslatable-text markup; a cascading
#     retry system recovers individual units, context windows, or single
#     cues whenever a batch comes back incomplete. An optional short
#     paragraph of context can be prepended to every request to prime the
#     engine's understanding, verified locally before anything is sent.
#     通过 Google 翻译内部的 PA 接口（translateHtml）批量翻译字幕单元，
#     设计目标是无人值守 CI 环境下的吞吐量与可靠性，而非交互式单次请求。
#     单元按章节（场景/歌曲片段）分组打包成 HTML 文档并发送出，并发处理，
#     通过基于标记的对齐方案重新拼装结果——即便供应商偶尔对内容重新
#     拆分或丢弃，该方案依然能正确对应。词表术语与部分结构性标记通过
#     官方免翻译标记加以保护；批次不完整时，一套级联重试系统会依次
#     恢复单个单元、上下文窗口或单条 cue。可选携带一段简短上下文随每次
#     请求前置发送以启发引擎理解，发送前完全在本地完成校验。
#
# Features:
#     - Concurrent, chapter-aware batching; oversized units are skipped, not truncated.
#     - Marker-based alignment survives the provider re-splitting or dropping content.
#     - Glossary terms sent as translate="no", restored locally after.
#     - Alignment markers sent unwrapped by default (--wrap-markers to opt back in).
#     - Retry cascade recovers missing units, then context windows, then single cues.
#     - Auto-detects and pins the source language so retries don't re-guess.
#     - Optional --context-file primes translation, verified locally via langdetect.
#     - Debug logging as JSON Lines, readable via debug_format.py.
#     - Inline <i>/<b>/<u> tags sent as literal HTML and preserved verbatim
#       on the way back, instead of the provider's own formatting noise.
#     - Dangling/malformed style tags are repaired where the fix is
#       unambiguous, otherwise dropped outright rather than kept broken.
#
# 功能:
#     - 并发、章节感知分批；超长单元直接跳过，绝不截断。
#     - 基于标记的对齐，即使供应商拆错或丢内容也不会错位。
#     - 词表以 translate="no" 发送、原样保留，之后本地替换回译名。
#     - 对齐标记默认不包裹 translate="no"（可用 --wrap-markers 恢复包裹）。
#     - 重试级联：先恢复缺失单元，再带上下文窗口重发，最后隔离重发单条 cue。
#     - 首次调用自动探测并锁定源语言，避免短文本重试反复误判。
#     - 可选 --context-file 携带上下文，发送前用 langdetect 本地校验语言。
#     - Debug 日志为 JSON Lines 格式，可用 debug_format.py 渲染可读。
#
# Dependencies / 依赖:
#     - langdetect (pip install langdetect), optional, auto-installed on first use.
#     - langdetect（pip install langdetect），可选，首次用到时自动安装。
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
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_NAME = "google_client"

ENDPOINT = "https://translate-pa.googleapis.com/v1/translateHtml"
API_KEY_ENV = "GOOGLE_TRANSLATE_API_KEY"
DEFAULT_BATCH_CHARS = 8000
DEFAULT_CONCURRENCY = 8
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_DELAY = 3
PROGRESS_INTERVAL = 20

TAG_PATTERN = re.compile(r"<[^>]+>")
STYLE_TAG_PATTERN = re.compile(r"</?(?:i|b|u)>", re.IGNORECASE)
STYLE_TAG_PLACEHOLDER_PATTERN = re.compile(r"\u0001(\d+)\u0001")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEBUG_MODE = False
DEBUG_RAW_IN_FILE = None
DEBUG_RAW_OUT_FILE = None
DEBUG_LOCK = threading.Lock()
DEBUG_SEQUENCE = [0]


class LanguageResolver:
    def __init__(self, requested, label="source"):
        self.requested = requested
        self.label = label
        self._detected = None
        self._lock = threading.Lock()

    @property
    def is_auto(self):
        return self.requested == "auto"

    @property
    def pinned(self):
        with self._lock:
            return self._detected is not None

    def current(self):
        if not self.is_auto:
            return self.requested
        with self._lock:
            return self._detected or "auto"

    def observe(self, detected):
        if not self.is_auto or not detected:
            return
        with self._lock:
            if self._detected is None:
                self._detected = detected
                log(f"auto-detected {self.label} language: {detected} (pinned for subsequent calls)")

ALIGNMENT_MODE = "marker"
GROUP_MARKER_TEMPLATE = "\u27e6g{}\u27e7"
GROUP_MARKER_PATTERN = re.compile(r"\u27e6g([^\u27e6\u27e7]+)\u27e7")
CUE_MARKER_TEMPLATE = "\u27e6c{}\u27e7"
CUE_MARKER_PATTERN = re.compile(r"\u27e6c(\d+(?:\.\d+)?)\u27e7")
CONTENT_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)

UNCLOSED_MARKER_SIGNATURE = r"\u27e6[a-zA-Z]\d{1,6}(?:\.\d{1,6})?(?![\d.])(?!\u27e7)"
MISSING_OPEN_MARKER_SIGNATURE = r"(?<!\u27e6)[a-zA-Z]\d{1,6}(?:\.\d{1,6})?\u27e7"
CORRUPT_MARKER_SIGNATURE = re.compile(UNCLOSED_MARKER_SIGNATURE + "|" + MISSING_OPEN_MARKER_SIGNATURE)

ANY_MARKER_PATTERN = re.compile(r"\u27e6[^\u27e6\u27e7]*\u27e7")
CORRUPT_MARKER_PATTERN = re.compile(r"\u27e6[a-zA-Z0-9.]+(?!\u27e7)|(?<!\u27e6)[a-zA-Z0-9.]+\u27e7")
MARKER_BRACKET_PATTERN = re.compile(r"[\u27e6\u27e7]")

def sanitize_markers_against_source(text, source_text=""):
    if not text:
        return ""
    source = source_text or ""
    allowed = {}
    for m in ANY_MARKER_PATTERN.finditer(source):
        val = m.group(0)
        allowed[val] = allowed.get(val, 0) + 1

    def replace_valid(m):
        val = m.group(0)
        if allowed.get(val, 0) > 0:
            allowed[val] -= 1
            return val
        return ""

    cleaned = ANY_MARKER_PATTERN.sub(replace_valid, text)

    for m in CORRUPT_MARKER_PATTERN.finditer(source):
        val = m.group(0)
        allowed[val] = allowed.get(val, 0) + 1

    def replace_corrupt(m):
        val = m.group(0)
        if allowed.get(val, 0) > 0:
            allowed[val] -= 1
            return val
        return ""

    cleaned = CORRUPT_MARKER_PATTERN.sub(replace_corrupt, cleaned)

    orig_bracket_count = len(MARKER_BRACKET_PATTERN.findall(source))
    curr_brackets = MARKER_BRACKET_PATTERN.findall(cleaned)
    if len(curr_brackets) > orig_bracket_count:
        kept = 0
        def replace_stray_bracket(m):
            nonlocal kept
            if kept < orig_bracket_count:
                kept += 1
                return m.group(0)
            return ""
        cleaned = MARKER_BRACKET_PATTERN.sub(replace_stray_bracket, cleaned)

    return " ".join(cleaned.split())

def has_marker_leak(original_text, translated_text):
    original_count = len(MARKER_BRACKET_PATTERN.findall(original_text or ""))
    translated_count = len(MARKER_BRACKET_PATTERN.findall(translated_text or ""))
    return translated_count > original_count


def _marker_sort_key(marker_id):
    cue, _, sub = marker_id.partition(".")
    return (int(cue), int(sub) if sub else 0)


def repair_corrupt_markers(text, prefix_char, expected_ids, source_text=""):
    if not text or not expected_ids:
        return text

    valid_pattern = re.compile(rf"\u27e6{prefix_char}(\d+(?:\.\d+)?)\u27e7", re.IGNORECASE)
    seen = {m.group(1) for m in valid_pattern.finditer(text)}
    pending = {str(cid) for cid in expected_ids if str(cid) not in seen}
    if not pending:
        return text

    native_source_ids = set()
    if source_text:
        raw_source = re.sub(r"\u27e6[a-zA-Z]\d+(?:\.\d+)?\u27e7", "", source_text)
        for cid in pending:
            pattern = rf"(?:\b|[^a-zA-Z0-9]){re.escape(prefix_char)}\s*{re.escape(cid)}(?:\b|[^a-zA-Z0-9])"
            if re.search(pattern, raw_source, re.IGNORECASE):
                native_source_ids.add(cid)

    corrupt_brackets = "\u27e6\u27e7\\\ufffd/[]{}<>()\u3010\u3011\u3016\u3017\u3014\u3015"
    prefix_chars = prefix_char.lower() + prefix_char.upper()
    corrupt_chars = corrupt_brackets + " " + prefix_chars
    trailing_punctuation = ":,，：、-—.。 "

    def replacer(m):
        before, num_str, after = m.group(1), m.group(2), m.group(3)
        cid = num_str
        if cid in pending and cid not in native_source_ids:
            clean_before = before
            while clean_before and clean_before[-1] in corrupt_chars:
                clean_before = clean_before[:-1]

            clean_after = after
            while clean_after and clean_after[0] in corrupt_chars:
                clean_after = clean_after[1:]

            is_marker = False
            before_str = before.strip().lower()
            if before_str.endswith(prefix_char.lower()):
                is_marker = True
            else:
                if any(ch in before + after for ch in corrupt_brackets):
                    is_marker = True

            if is_marker:
                pending.discard(cid)
                while clean_after and clean_after[0] in trailing_punctuation:
                    clean_after = clean_after[1:]
                return f"{clean_before}\u27e6{prefix_char}{cid}\u27e7{clean_after}"
        return m.group(0)

    pattern = re.compile(r"([^\d\s]*\s*)(\d+(?:\.\d+)?)(\s*[^\d\s]*)")
    text = pattern.sub(replacer, text)

    if pending:
        empty_pattern = re.compile(rf"(?:[\u27e6\\\ufffd]{{1,3}}{prefix_char}[\u27e7\\\ufffd]{{1,3}}|[\u27e6\u27e7\\\ufffd]{{2,4}})", re.IGNORECASE)
        empty_matches = list(empty_pattern.finditer(text))
        if 0 < len(empty_matches) <= len(pending):
            pending_list = sorted((cid for cid in pending if cid not in native_source_ids), key=_marker_sort_key)
            def empty_replacer(m):
                if pending_list:
                    return f"\u27e6{prefix_char}{pending_list.pop(0)}\u27e7"
                return m.group(0)
            text = empty_pattern.sub(empty_replacer, text)

    return text

NO_TRANSLATE_TEMPLATE = '<span translate="no">{}</span>'
WRAP_MARKERS = False
CONTEXT_MAX_CHARS = 300
CONTEXT_GROUP_MARKER = "ctx"
CONTEXT_SAMPLE_CHARS = 500

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


def pip_install(package):
    for extra_args in ([], ["--break-system-packages"]):
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", package, *extra_args],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            return True
        except Exception:
            continue
    return False


_LANGDETECT_MODULE = None
_LANGDETECT_CHECKED = False


def ensure_langdetect():
    global _LANGDETECT_MODULE, _LANGDETECT_CHECKED
    if _LANGDETECT_CHECKED:
        return _LANGDETECT_MODULE
    _LANGDETECT_CHECKED = True
    try:
        import langdetect
    except ImportError:
        if not pip_install("langdetect"):
            log("langdetect unavailable, context language check disabled")
            return None
        try:
            import langdetect
        except ImportError as e:
            log(f"langdetect unavailable, context language check disabled: {e}")
            return None
    langdetect.DetectorFactory.seed = 0
    _LANGDETECT_MODULE = langdetect
    return _LANGDETECT_MODULE


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape_html(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


def escape_html_preserving_style(text):
    pieces, cursor = [], 0
    for m in STYLE_TAG_PATTERN.finditer(text):
        pieces.append(escape_html(text[cursor:m.start()]))
        pieces.append(m.group(0).lower())
        cursor = m.end()
    pieces.append(escape_html(text[cursor:]))
    return "".join(pieces)


def clean_translated_fragment(raw):
    preserved = []

    def guard(m):
        preserved.append(m.group(0).lower())
        return f"\u0001{len(preserved) - 1}\u0001"

    guarded = STYLE_TAG_PATTERN.sub(guard, raw)
    stripped = TAG_PATTERN.sub("", guarded)
    restored = STYLE_TAG_PLACEHOLDER_PATTERN.sub(lambda m: preserved[int(m.group(1))], stripped)
    return unescape_html(restored).strip()


STYLE_DANGLING_OPEN_PATTERN = re.compile(r"<(i|b|u)(?![a-zA-Z>])", re.IGNORECASE)
STYLE_MISSING_OPEN_BRACKET_PATTERN = re.compile(r"(?<!<)/(i|b|u)>", re.IGNORECASE)


def repair_style_tags(text):
    text = STYLE_DANGLING_OPEN_PATTERN.sub(lambda m: f"</{m.group(1).lower()}>", text)
    return STYLE_MISSING_OPEN_BRACKET_PATTERN.sub(lambda m: f"</{m.group(1).lower()}>", text)


def style_tags_balanced(text):
    depth = {"i": 0, "b": 0, "u": 0}
    for m in STYLE_TAG_PATTERN.finditer(text):
        token = m.group(0).lower()
        tag = token.strip("</>")
        depth[tag] += -1 if token[1] == "/" else 1
        if depth[tag] < 0:
            return False
    return all(v == 0 for v in depth.values())


def sanitize_style_tags(text):
    if not text or not STYLE_TAG_PATTERN.search(text):
        return text
    repaired = repair_style_tags(text)
    return repaired if style_tags_balanced(repaired) else STYLE_TAG_PATTERN.sub("", repaired)


def has_content(text):
    return bool(text) and bool(CONTENT_CHAR_PATTERN.search(text))


def has_translatable_content(text, term_matches):
    cursor, residue = 0, []
    for match in sorted(term_matches or [], key=lambda m: m["start"]):
        residue.append(text[cursor:match["start"]])
        cursor = match["end"]
    residue.append(text[cursor:])
    return has_content("".join(residue))


def wrap_marker(text):
    return NO_TRANSLATE_TEMPLATE.format(text) if WRAP_MARKERS else text


def within_budget(text, limit):
    if len(text) <= limit:
        return True
    log(f"payload of {len(text)} chars exceeds budget ({limit}), refusing to truncate, skipping")
    return False


def split_oversized_chapter(items, batch_chars, context_chars=0):
    limit = max(batch_chars - context_chars, 1)
    pieces, piece, piece_chars, oversized = [], [], 0, []
    for item in items:
        item_chars = len(item["text"])
        if item_chars > limit:
            oversized.append(item)
            continue
        if piece and piece_chars + item_chars > limit:
            pieces.append(piece)
            piece, piece_chars = [], 0
        piece.append(item)
        piece_chars += item_chars
    if piece:
        pieces.append(piece)
    return pieces, oversized


def build_batches(items, chapter_groups, batch_chars, context_chars=0):
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
        group_chars = sum(len(item["text"]) for item in group_items) + context_chars
        if group_chars > batch_chars:
            flush()
            pieces, group_oversized = split_oversized_chapter(group_items, batch_chars, context_chars)
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


def build_chapter_html(group, indices, context_html=None):
    marker = ALIGNMENT_MODE == "marker"
    prefix = ""
    if context_html:
        marker_text = wrap_marker(GROUP_MARKER_TEMPLATE.format(CONTEXT_GROUP_MARKER)) if marker else ""
        prefix = f'<span id={CONTEXT_GROUP_MARKER}>{marker_text}{context_html}</span>'
    spans = "".join(
        f'<span id={indices[item["id"]]}>'
        f'{wrap_marker(GROUP_MARKER_TEMPLATE.format(indices[item["id"]])) if marker else ""}'
        f'{item.get("html", escape_html(item["text"]))}'
        f'</span>'
        for item in group
    )
    return f"<div>{prefix}{spans}</div>"


SPAN_OPEN_PATTERN = re.compile(r'<span[^>]*\bid=["\']?([a-zA-Z0-9:]+)["\']?[^>]*>')


def split_by_marker(flat_text, pattern):
    parts = pattern.split(flat_text)
    result, seen = {}, set()
    for i in range(1, len(parts), 2):
        key = parts[i]
        if key in seen:
            result.pop(key, None)
            continue
        seen.add(key)
        text = parts[i + 1].strip()
        if text:
            result[key] = text
    return result


def parse_by_tag_id(html, open_pattern, marker_pattern, source_by_index=None):
    opens = [(m.end(), m.group(1)) for m in open_pattern.finditer(html) if m.group(1).isdigit()]
    result = {}
    for i, (end, idx_str) in enumerate(opens):
        idx = int(idx_str)
        chunk_end = opens[i + 1][0] if i + 1 < len(opens) else len(html)
        raw = html[end:chunk_end]
        text = clean_translated_fragment(raw)
        source_text = source_by_index.get(idx, "") if source_by_index else ""
        text = sanitize_markers_against_source(text, source_text)
        if not text:
            continue
        result[idx] = f"{result[idx]} {text}" if idx in result else text
    return result


def parse_by_spans(html, source_by_index=None):
    return parse_by_tag_id(html, SPAN_OPEN_PATTERN, GROUP_MARKER_PATTERN, source_by_index)


MARKER_OVERREACH_RATIO = 1.3


def choose_candidate(primary_text, marker_text):
    if marker_text is None:
        return primary_text
    if primary_text is None:
        return marker_text
    primary_len = content_length(primary_text)
    if primary_len and content_length(marker_text) / primary_len > MARKER_OVERREACH_RATIO:
        return primary_text
    return marker_text


def extract_first_marker_anchors(html, expected_ids):
    first_markers = {}
    for m in SPAN_OPEN_PATTERN.finditer(html):
        if m.group(1).isdigit():
            idx = int(m.group(1))
            if expected_ids is None or idx in expected_ids:
                if idx not in first_markers or m.start() < first_markers[idx][0]:
                    first_markers[idx] = (m.start(), m.end(), idx)
    for m in GROUP_MARKER_PATTERN.finditer(html):
        if m.group(1).isdigit():
            idx = int(m.group(1))
            if expected_ids is None or idx in expected_ids:
                if idx not in first_markers or m.start() < first_markers[idx][0]:
                    first_markers[idx] = (m.start(), m.end(), idx)
    anchors = list(first_markers.values())
    anchors.sort(key=lambda a: a[0])
    return anchors


def parse_by_reconciled_boundaries(html, boundaries, source_by_index=None):
    result = {}
    for i, (start, end, idx) in enumerate(boundaries):
        next_boundary = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(html)
        if next_boundary < end:
            raw = ""
        else:
            raw = html[end:next_boundary]
        text = clean_translated_fragment(raw)
        source_text = source_by_index.get(idx, "") if source_by_index else ""
        text = sanitize_markers_against_source(text, source_text)
        if not text and not (source_text and not has_content(source_text)):
            continue
        result[idx] = text
    return result


def parse_translated_html(html, expected_ids=None, source_by_index=None):
    if ALIGNMENT_MODE == "marker":
        if expected_ids:
            html = repair_corrupt_markers(html, "g", expected_ids)
        result = parse_by_reconciled_boundaries(html, extract_first_marker_anchors(html, expected_ids), source_by_index)
    else:
        result = parse_by_spans(html, source_by_index)
    if DEBUG_MODE and not result:
        with DEBUG_LOCK:
            log(f"debug: parse_translated_html found NO matching blocks in HTML. Head: {html[:200]}")
    return result


def next_debug_seq():
    with DEBUG_LOCK:
        DEBUG_SEQUENCE[0] += 1
        return DEBUG_SEQUENCE[0]


def debug_log_raw(path, entry):
    if not path:
        return
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with DEBUG_LOCK:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def extract_detected_lang(payload):
    try:
        candidate = payload[1][0]
    except (IndexError, TypeError, KeyError):
        return None
    return candidate if isinstance(candidate, str) and re.fullmatch(r"[a-zA-Z]{2,3}(-[A-Za-z0-9]+)*", candidate) else None


def post_translate_html(html, lang, target_lang, api_key):
    source_lang = lang.current()
    request_body = [[[html], source_lang, target_lang], "te"]
    seq = next_debug_seq()
    debug_log_raw(DEBUG_RAW_IN_FILE, {"seq": seq, "ts": time.time(), "direction": "request", "source_lang": source_lang, "target_lang": target_lang, "body": request_body})

    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json+protobuf", "X-goog-api-key": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw_response = response.read()
        status, headers = response.status, dict(response.headers.items())
        try:
            payload = json.loads(raw_response.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        debug_log_raw(DEBUG_RAW_OUT_FILE, {
            "seq": seq, "ts": time.time(), "direction": "response", "status": status, "headers": headers,
            "body": payload if payload is not None else raw_response.decode("utf-8", errors="replace"),
        })
    if payload is None:
        raise ValueError(f"non-JSON response (status {status})")
    lang.observe(extract_detected_lang(payload))
    return payload[0][0]


def call_google(batch, lang, target_lang, api_key, context_html=None):
    items = [item for group in batch for item in group]
    indices = {item["id"]: i for i, item in enumerate(items, start=1)}
    id_by_index = {i: item_id for item_id, i in indices.items()}
    source_by_index = {i: item["text"] for i, item in enumerate(items, start=1)}
    html = "".join(build_chapter_html(group, indices, context_html) for group in batch)
    translated_html = post_translate_html(html, lang, target_lang, api_key)
    parsed = parse_translated_html(translated_html, set(id_by_index.keys()), source_by_index)
    source_by_id = {item["id"]: item["text"] for item in items}
    result = {}
    for idx, text in parsed.items():
        item_id = id_by_index.get(idx)
        if item_id is None:
            continue
        source_text = source_by_id.get(item_id, "")
        if has_content(text) or not has_content(source_text):
            result[item_id] = text
    return result


def translate_batch(batch, lang, target_lang, api_key, context_html=None):
    items = [item for group in batch for item in group]
    expected_ids = {item["id"] for item in items}
    result, missing = {}, expected_ids
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = call_google(batch, lang, target_lang, api_key, context_html if attempt == 1 else None)
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

    return result, sorted(missing, key=str)


def detect_language(text):
    langdetect = ensure_langdetect()
    if langdetect is None:
        return None
    try:
        return langdetect.detect(text)
    except Exception:
        return None


def primary_subtag(lang_code):
    return (lang_code or "").split("-")[0].lower()


def sample_subtitle_text(units, max_chars=CONTEXT_SAMPLE_CHARS):
    pieces, total = [], 0
    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        pieces.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return " ".join(pieces)[:max_chars]


def resolve_context_language(raw_context, requested_source_lang, subtitle_sample):
    if not raw_context:
        return None
    context_detected = detect_language(raw_context)
    if context_detected is None:
        log("langdetect unavailable or inconclusive on context text, dropping context to be safe "
            "(pip install langdetect to enable this check)")
        return None
    reference = requested_source_lang if requested_source_lang != "auto" else detect_language(subtitle_sample)
    if reference is None:
        log("could not determine subtitle source language locally, dropping context to be safe")
        return None
    if primary_subtag(context_detected) != primary_subtag(reference):
        log(f"context language ({context_detected}) does not match subtitle language ({reference}), "
            f"dropping context")
        return None
    log(f"context language ({context_detected}) matches subtitle language, sending as provided")
    return raw_context


def translate(items, chapter_groups, lang, target_lang, api_key, batch_chars, concurrency=DEFAULT_CONCURRENCY, raw_context=None):
    translations, skipped = {}, []
    context_reserve = len(raw_context) if raw_context else 0
    if context_reserve and context_reserve * 2 > batch_chars:
        log(f"warning: context ({context_reserve} chars) is large relative to batch_chars ({batch_chars}), "
            f"batches will pack very few chapters per div")
    batches, oversized = build_batches(items, chapter_groups, batch_chars, context_reserve)
    for item in oversized:
        log(f"unit {item['id']}: {len(item['text'])} chars exceeds batch_chars ({batch_chars}), "
            f"cue-level content cannot be split further, skipping without truncation")
        skipped.append(item["id"])
    if not batches:
        return translations, skipped

    total_batches = len(batches)
    context_html = escape_html(raw_context) if raw_context else None

    start_time = last_report = time.time()
    completed = 0
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(translate_batch, batch, lang, target_lang, api_key, context_html): batch for batch in batches}
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


def build_protected_spans(text, term_matches):
    spans = [{"start": m.start(), "end": m.end(), "wrap": WRAP_MARKERS} for m in CUE_MARKER_PATTERN.finditer(text)]
    spans.extend({"start": m["start"], "end": m["end"], "wrap": True} for m in term_matches)
    spans.sort(key=lambda s: s["start"])
    merged = []
    for span in spans:
        if merged and span["start"] <= merged[-1]["end"] and span["wrap"] == merged[-1]["wrap"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append(dict(span))
    return merged


def protect_content_html(text, term_matches):
    pieces, cursor = [], 0
    for span in build_protected_spans(text, term_matches):
        pieces.append(escape_html_preserving_style(text[cursor:span["start"]]))
        piece = escape_html_preserving_style(text[span["start"]:span["end"]])
        pieces.append(NO_TRANSLATE_TEMPLATE.format(piece) if span["wrap"] else piece)
        cursor = span["end"]
    pieces.append(escape_html_preserving_style(text[cursor:]))
    return "".join(pieces)


def cue_term_matches_for_unit(unit):
    spans = unit.get("spans") or []
    term_matches = unit.get("term_matches") or []
    if len(spans) <= 1:
        return {span["marker_id"]: term_matches for span in spans}
    text, cursor, result = unit["text"], 0, {}
    for span in spans:
        pos = text.find(span["text"], cursor)
        if pos == -1:
            result[span["marker_id"]] = []
            continue
        start, end = pos, pos + len(span["text"])
        cursor = end
        result[span["marker_id"]] = [
            {**match, "start": match["start"] - start, "end": match["end"] - start}
            for match in term_matches if start <= match["start"] and match["end"] <= end
        ]
    return result


def build_cue_term_matches(units):
    result = {}
    for unit in units:
        result.update(cue_term_matches_for_unit(unit))
    return result


def flatten_units(units, chapter_of_unit):
    items, chapter_items = [], {}
    for unit in units:
        chapter_id = chapter_of_unit.get(unit["id"])
        items.append({"id": unit["id"], "text": unit["text"], "html": protect_content_html(unit["text"], unit.get("term_matches") or [])})
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


UNIT_MARKER_TEMPLATE = "\u27e6u{}\u27e7"
UNIT_MARKER_PATTERN = re.compile(r"\u27e6u([^\u27e6\u27e7]+)\u27e7")
WINDOW_RADIUS_LADDER = (5, 3, 1, 0)
ISOLATED_RADIUS_LADDER = (5, 3, 1, 0)
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


def expected_cue_ids(unit):
    return [s["marker_id"] for s in unit["spans"] if s.get("boundary") == "marker"]


def split_cue_chunks(text):
    parts = CUE_MARKER_PATTERN.split(text or "")
    result, seen = {}, set()
    for i in range(1, len(parts), 2):
        cid = parts[i]
        if cid in seen:
            result.pop(cid, None)
            continue
        seen.add(cid)
        result[cid] = parts[i + 1].strip()
    return result


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


def has_translatable_content(text, term_matches):
    cursor, residue = 0, []
    for match in sorted(term_matches or [], key=lambda m: m["start"]):
        residue.append(text[cursor:match["start"]])
        cursor = match["end"]
    residue.append(text[cursor:])
    return has_content("".join(residue))


NOISE_CATEGORY_PREFIXES = ("P", "N")


def normalize_for_equality(text):
    return "".join(ch for ch in (text or "") if not ch.isspace() and unicodedata.category(ch)[0] not in NOISE_CATEGORY_PREFIXES)


def word_count(text):
    return len(re.findall(r"\w+", text or "", re.UNICODE))


def is_leaked_untranslated(original, translated, source_lang, target_lang):
    if not translated:
        return False
    norm_orig = normalize_for_equality(original)
    if not norm_orig:
        return False

    sl, tl = script_of(source_lang), script_of(target_lang)
    if sl == "latin" and tl == "cjk":
        pass
    elif sl == "cjk" and tl == "latin":
        pass
    else:
        if word_count(original) < 2:
            return False

    return norm_orig == normalize_for_equality(translated)


def unit_cue_ids(unit):
    return [s["id"] for s in unit.get("spans", [])]


def is_single_plain_cue(unit):
    return len(unit_cue_ids(unit)) == 1 and not expected_cue_ids(unit)


def find_leaked_cue_ids(unit, text, source_lang, target_lang):
    marker_ids = expected_cue_ids(unit)
    if marker_ids:
        chunks = split_cue_chunks(text)
        span_text = {s["marker_id"]: s["text"] for s in unit.get("spans", []) if s.get("boundary") == "marker"}
        return [cid for cid in marker_ids if cid in chunks and is_leaked_untranslated(span_text.get(cid, ""), chunks[cid], source_lang, target_lang)]
    ids = unit_cue_ids(unit)
    return [str(ids[0])] if len(ids) == 1 and is_leaked_untranslated(unit["text"], text, source_lang, target_lang) else []


DIV_ID_PATTERN = re.compile(r'<div[^>]*\bid=["\']?([a-zA-Z0-9:]+)["\']?[^>]*>')
PLAIN_DIV_PATTERN = re.compile(r"<div>(.*?)</div>", re.S)


def pack_by_chars(payloads, max_chars_per_request, max_items=50):
    chunks, current, current_chars = [], [], 0
    for i, payload in enumerate(payloads):
        if current and (current_chars + len(payload) > max_chars_per_request or len(current) >= max_items):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(i)
        current_chars += len(payload)
    if current:
        chunks.append(current)
    return chunks


def parse_plain_divs(html):
    return [
        clean_translated_fragment(m.group(1))
        for m in PLAIN_DIV_PATTERN.finditer(html)
    ]


def resolve_chunk_with_binary_fallback(indices, payloads, lang, target_lang, api_key):
    if not indices:
        return {}
    chunk_html = "".join(payloads[i] for i in indices)
    try:
        translated_html = post_translate_html(chunk_html, lang, target_lang, api_key)
        divs = parse_plain_divs(translated_html)
        if len(divs) == len(indices):
            return {i: divs[k] for k, i in enumerate(indices)}
        if len(indices) == 1:
            div_val = divs[0] if divs else clean_translated_fragment(translated_html)
            return {indices[0]: div_val} if div_val else {}
    except Exception as e:
        log(f"packed jobs chunk failed: {e}")
        if len(indices) == 1:
            return {}

    mid = len(indices) // 2
    left_indices = indices[:mid]
    right_indices = indices[mid:]
    recovered = {}
    recovered.update(resolve_chunk_with_binary_fallback(left_indices, payloads, lang, target_lang, api_key))
    recovered.update(resolve_chunk_with_binary_fallback(right_indices, payloads, lang, target_lang, api_key))
    return recovered


def run_packed_jobs(payloads, max_chars_per_request, lang, target_lang, api_key, concurrency):
    results = [None] * len(payloads)
    if not payloads:
        return results
    chunks = pack_by_chars(payloads, max_chars_per_request)

    def process_chunk(indices):
        chunk_recovered = resolve_chunk_with_binary_fallback(indices, payloads, lang, target_lang, api_key)
        for i, text in chunk_recovered.items():
            results[i] = text

    with ThreadPoolExecutor(max_workers=min(len(chunks), concurrency) or 1) as executor:
        futures = [executor.submit(process_chunk, indices) for indices in chunks]
        for future in as_completed(futures):
            future.result()

    return results


def retry_windowed_all(units, suspect_ids, lang, target_lang, api_key, batch_chars, concurrency, ladder=WINDOW_RADIUS_LADDER, strict_marker=False):
    index = {u["id"]: i for i, u in enumerate(units)}
    unit_by_id = {u["id"]: u for u in units}
    jobs = []

    for suspect_id in suspect_ids:
        if suspect_id not in index:
            continue
        i = index[suspect_id]
        for radius in ladder:
            window = units[max(0, i - radius):i + radius + 1]
            if len(window) < 1:
                continue

            is_solo = (len(window) == 1)
            if is_solo:
                payload = f"<div>{protect_content_html(window[0]['text'], window[0].get('term_matches') or [])}</div>"
            else:
                inner = "".join(
                    f"{wrap_marker(UNIT_MARKER_TEMPLATE.format(unit['id']))}{protect_content_html(unit['text'], unit.get('term_matches') or [])}"
                    for unit in window
                )
                payload = f"<div>{inner}</div>"

            if len(payload) > batch_chars:
                continue

            jobs.append({
                "suspect_id": suspect_id,
                "radius": radius,
                "payload": payload,
                "window_ids": [u["id"] for u in window],
                "is_solo": is_solo
            })

    if not jobs:
        return {}

    send_jobs, job_send_index, seen_solo_text = [], [], {}
    for job in jobs:
        if job["is_solo"] and len(job["window_ids"]) == 1:
            text_key = unit_by_id.get(job["window_ids"][0], {}).get("text", "")
            if text_key in seen_solo_text:
                job_send_index.append(seen_solo_text[text_key])
                continue
            seen_solo_text[text_key] = len(send_jobs)
        job_send_index.append(len(send_jobs))
        send_jobs.append(job)

    payloads = [job["payload"] for job in send_jobs]
    html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, api_key, concurrency)

    results_by_suspect = {}
    for job, send_idx in zip(jobs, job_send_index):
        html = html_results[send_idx]
        if not html:
            continue

        if job["is_solo"]:
            marker_res = {job["window_ids"][0]: html}
        else:
            flat = repair_corrupt_markers(html, "u", job["window_ids"])
            marker_res = {
                int(k.strip()): v for k, v in split_by_marker(flat, UNIT_MARKER_PATTERN).items()
                if k.strip().isdigit()
            }
            if not all(wid in marker_res for wid in job["window_ids"]):
                continue

        if strict_marker and job["radius"] > 0 and job["suspect_id"] not in marker_res:
            continue

        text_raw = marker_res.get(job["suspect_id"])
        if text_raw is not None:
            unit = unit_by_id.get(job["suspect_id"])
            if unit:
                text = text_raw
                expected = expected_cue_ids(unit)
                if expected:
                    text = repair_corrupt_markers(text, "c", expected)
                if not CORRUPT_MARKER_SIGNATURE.search(text) and (job["radius"] == 0 or is_length_plausible(unit["text"], text)):
                    results_by_suspect.setdefault(job["suspect_id"], {})[job["radius"]] = text

    recovered = {}
    for suspect_id in suspect_ids:
        for radius in ladder:
            res = results_by_suspect.get(suspect_id, {}).get(radius)
            if res is not None:
                recovered[suspect_id] = res
                break

    return recovered


def retry_isolated_cues_all(missing_by_unit, marker_order, marker_text_by_id, marker_term_matches, lang, target_lang, api_key, batch_chars, concurrency, extra_valid=None):
    position = {cid: i for i, cid in enumerate(marker_order)}
    jobs = []

    for unit_id, missing_ids in missing_by_unit.items():
        positions = sorted([position[cid] for cid in missing_ids if cid in position])
        if not positions:
            continue

        for radius in ISOLATED_RADIUS_LADDER:
            lo = max(0, positions[0] - radius)
            hi = min(len(marker_order) - 1, positions[-1] + radius)
            is_solo = (lo == hi)
            sent_ids = []
            inner_parts = []
            for i in range(lo, hi + 1):
                cid = marker_order[i]
                text = marker_text_by_id.get(cid)
                if text is None:
                    continue
                matches = marker_term_matches.get(cid) or []
                marker = "" if is_solo else wrap_marker(CUE_MARKER_TEMPLATE.format(cid))
                inner_parts.append(f"{marker}{protect_content_html(text, matches)}")
                sent_ids.append(cid)

            payload = f"<div>{''.join(inner_parts)}</div>"
            if not payload or len(payload) > batch_chars:
                continue

            jobs.append({
                "unit_id": unit_id,
                "radius": radius,
                "payload": payload,
                "sent_ids": sent_ids,
                "is_solo": is_solo,
                "missing_ids": missing_ids
            })

    recovered_by_unit = {}
    if not jobs:
        return recovered_by_unit

    send_jobs, job_send_index, seen_solo_text = [], [], {}
    for job in jobs:
        if job["is_solo"] and len(job["sent_ids"]) == 1:
            text_key = marker_text_by_id.get(job["sent_ids"][0])
            if text_key in seen_solo_text:
                job_send_index.append(seen_solo_text[text_key])
                continue
            seen_solo_text[text_key] = len(send_jobs)
        job_send_index.append(len(send_jobs))
        send_jobs.append(job)

    payloads = [job["payload"] for job in send_jobs]
    html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, api_key, concurrency)

    results_by_unit = {}
    for job, send_idx in zip(jobs, job_send_index):
        html = html_results[send_idx]
        if not html:
            continue
        if job["is_solo"] and len(job["sent_ids"]) == 1:
            marker_res = {job["sent_ids"][0]: html}
        else:
            flat = repair_corrupt_markers(html, "c", job["sent_ids"])
            marker_res = split_cue_chunks(flat)

        job_recovered = {}
        for cid in job["missing_ids"]:
            cand = marker_res.get(cid)
            if job["is_solo"] and cand:
                cand = repair_corrupt_markers(cand, "c", [cid])
            orig = marker_text_by_id.get(cid, "")
            if cand and not CORRUPT_MARKER_SIGNATURE.search(cand) and is_length_plausible(orig, cand) and (extra_valid is None or extra_valid(orig, cand)):
                job_recovered[cid] = apply_term_replacements(cand, marker_term_matches.get(cid) or [], target_lang)

        if job_recovered:
            results_by_unit.setdefault(job["unit_id"], {}).setdefault(job["radius"], {}).update(job_recovered)

    for unit_id, missing_ids in missing_by_unit.items():
        current_missing = set(missing_ids)
        final_recovered = {}
        for radius in ISOLATED_RADIUS_LADDER:
            if not current_missing:
                break
            res = results_by_unit.get(unit_id, {}).get(radius)
            if not res:
                continue
            for cid in list(current_missing):
                if cid in res:
                    final_recovered[cid] = res[cid]
                    current_missing.remove(cid)
        if final_recovered:
            recovered_by_unit[unit_id] = final_recovered

    return recovered_by_unit


def translate_units(units, chapters, cues, lang, target_lang, api_key, batch_chars, concurrency, raw_context=None):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    chapter_of_unit = {uid: chapter["id"] for chapter in chapters for uid in chapter["unit_ids"]}
    items, chapter_groups = flatten_units(pending, chapter_of_unit)
    translations_raw, _skipped = translate(items, chapter_groups, lang, target_lang, api_key, batch_chars, concurrency, raw_context) if items else ({}, [])

    unit_order = [u["id"] for u in units]
    unit_position = {uid: i for i, uid in enumerate(unit_order)}
    initial_missing_ids = {unit["id"] for unit in pending if unit["id"] not in translations_raw}
    missing_units = [unit for unit in pending if unit["id"] in initial_missing_ids]
    if missing_units:
        payloads = [f"<div>{protect_content_html(u['text'], u.get('term_matches') or [])}</div>" for u in missing_units]
        html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, api_key, concurrency)
        for unit, html in zip(missing_units, html_results):
            if not html:
                continue
            recovered = html
            expected_cues = expected_cue_ids(unit)
            if expected_cues:
                recovered = repair_corrupt_markers(recovered, "c", expected_cues)
            if recovered and not CORRUPT_MARKER_SIGNATURE.search(recovered) and is_length_plausible(unit["text"], recovered):
                translations_raw[unit["id"]] = recovered

    untranslated_jobs = []
    for unit in pending:
        raw_text = translations_raw.get(unit["id"])
        final_text = apply_term_replacements(raw_text, unit.get("term_matches") or [], target_lang) if raw_text is not None else None
        if final_text is not None and is_untranslated(final_text, lang.current(), target_lang):
            untranslated_jobs.append(unit)

    if untranslated_jobs:
        payloads = [f"<div>{protect_content_html(u['text'], u.get('term_matches') or [])}</div>" for u in untranslated_jobs]
        html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, api_key, concurrency)
        for unit, html in zip(untranslated_jobs, html_results):
            if html:
                retried = html
                expected_cues = expected_cue_ids(unit)
                if expected_cues:
                    retried = repair_corrupt_markers(retried, "c", expected_cues)
                if retried and is_length_plausible(unit["text"], retried):
                    translations_raw[unit["id"]] = retried

    results = dict(resolved)
    for unit in pending:
        raw_text = translations_raw.get(unit["id"])
        final_text = apply_term_replacements(raw_text, unit.get("term_matches") or [], target_lang) if raw_text is not None else None
        results[unit["id"]] = final_text

    unit_by_id = {unit["id"]: unit for unit in units}
    for uid, text in results.items():
        if text is None: continue
        expected = expected_cue_ids(unit_by_id[uid])
        if expected:
            results[uid] = repair_corrupt_markers(text, "c", expected)

    length_suspects = {uid for uid, text in results.items()
                        if text is not None and has_content(unit_by_id[uid]["text"])
                        and (not has_content(text) or not is_length_plausible(unit_by_id[uid]["text"], text))}
    cue_suspects = {uid for uid, text in results.items()
                    if text is not None and (missing_cue_ids(unit_by_id[uid], text) or CORRUPT_MARKER_SIGNATURE.search(text)
                                             or has_marker_leak(unit_by_id[uid]["text"], text))}
    marker_order, marker_text_by_id = [], {}
    for unit in units:
        for span in unit.get("spans") or []:
            if span.get("boundary") == "marker":
                marker_order.append(span["marker_id"])
                marker_text_by_id[span["marker_id"]] = span["text"]
    marker_term_matches = build_cue_term_matches(units)

    primary_suspects = length_suspects | cue_suspects | initial_missing_ids
    all_suspects = set()
    for uid in primary_suspects:
        all_suspects.add(uid)
        pos = unit_position.get(uid)
        if pos is not None:
            if pos > 0:
                all_suspects.add(unit_order[pos - 1])
            if pos + 1 < len(unit_order):
                all_suspects.add(unit_order[pos + 1])

    if all_suspects:
        recovered = retry_windowed_all(units, sorted(all_suspects), lang, target_lang, api_key, batch_chars, concurrency, ladder=WINDOW_RADIUS_LADDER)
        if recovered:
            recovered = {rid: apply_term_replacements(text, unit_by_id[rid].get("term_matches") or [], target_lang)
                         for rid, text in recovered.items()}
            log(f"windowed retry: recovered {sorted(recovered)}")
            results.update(recovered)

        missing_by_unit = {}
        for uid in sorted(all_suspects):
            expected = expected_cue_ids(unit_by_id[uid])
            if expected:
                results[uid] = repair_corrupt_markers(results[uid], "c", expected)
            remaining = missing_cue_ids(unit_by_id[uid], results[uid])
            if not remaining:
                continue

            trivial = [cid for cid in remaining
                       if not has_translatable_content(marker_text_by_id.get(cid, ""), marker_term_matches.get(cid))]
            if trivial:
                filled = {cid: apply_term_replacements(marker_text_by_id[cid], marker_term_matches.get(cid) or [], target_lang)
                          for cid in trivial}
                results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), filled)
                log(f"unit {uid}: cues {trivial} have no translatable content beyond glossary terms, filled without retry")
                remaining = [cid for cid in remaining if cid not in trivial]

            if remaining:
                missing_by_unit[uid] = remaining

        if missing_by_unit:
            recovered_cues = retry_isolated_cues_all(
                missing_by_unit, marker_order, marker_text_by_id, marker_term_matches, lang, target_lang, api_key, batch_chars, concurrency)
            for uid, r_cues in recovered_cues.items():
                results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), r_cues)
                log(f"isolated cue retry for unit {uid}: recovered cues {sorted(r_cues, key=_marker_sort_key)}")

    leak_by_unit = {uid: leaked for uid, text in results.items() if text is not None
                    for leaked in [find_leaked_cue_ids(unit_by_id[uid], text, lang.current(), target_lang)] if leaked}
    if leak_by_unit:
        leak_recovered = retry_isolated_cues_all(
            leak_by_unit, marker_order, marker_text_by_id, marker_term_matches, lang, target_lang, api_key, batch_chars, concurrency,
            extra_valid=lambda orig, cand: not is_leaked_untranslated(orig, cand, lang.current(), target_lang),
        )
        for uid, r_cues in leak_recovered.items():
            unit = unit_by_id[uid]
            results[uid] = next(iter(r_cues.values())) if is_single_plain_cue(unit) else patch_missing_cues(results[uid], expected_cue_ids(unit), r_cues)
            log(f"untranslated-leak retry for unit {uid}: recovered cues {sorted(r_cues, key=_marker_sort_key)}")

    skipped = [uid for uid, text in results.items() if text is None]
    translations = {str(uid): sanitize_style_tags(text) for uid, text in results.items() if text is not None}
    return translations, skipped


def truncate_context(text, max_chars):
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary_ok = not (cut[-1:].isascii() and cut[-1:].isalpha() and text[max_chars:max_chars + 1].isascii() and text[max_chars:max_chars + 1].isalpha())
    if not boundary_ok:
        last_space = cut.rfind(" ")
        if last_space > 0:
            cut = cut[:last_space]
    return cut.rstrip(), True


def main():
    global DEBUG_MODE, DEBUG_RAW_IN_FILE, DEBUG_RAW_OUT_FILE, ALIGNMENT_MODE, WRAP_MARKERS
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default=None)
    parser.add_argument("--target-lang", default=None)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--alignment-mode", choices=["span", "marker"], default=None)
    parser.add_argument("--wrap-markers", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-raw-in", default=None)
    parser.add_argument("--debug-raw-out", default=None)
    parser.add_argument("--context-file", default=None)
    parser.add_argument("--context-max-chars", type=int, default=CONTEXT_MAX_CHARS)
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"
    DEBUG_RAW_IN_FILE = args.debug_raw_in
    DEBUG_RAW_OUT_FILE = args.debug_raw_out
    if args.alignment_mode:
        ALIGNMENT_MODE = args.alignment_mode
    WRAP_MARKERS = args.wrap_markers

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    payload = json.loads(raw)
    units = payload.get("units", [])
    chapters = payload.get("chapters", [])
    cues = payload.get("cues", [])
    requested_lang = (args.source_lang or payload.get("source_lang") or "auto").strip()
    lang = LanguageResolver("auto" if requested_lang.lower() == "auto" else requested_lang)
    target_lang = args.target_lang or payload.get("target_lang", "zh-CN")
    api_key = args.api_key or os.environ.get(API_KEY_ENV)

    raw_context = None
    if args.context_file:
        raw_context = open(args.context_file, encoding="utf-8").read().strip()
        raw_context, truncated = truncate_context(raw_context, args.context_max_chars)
        if truncated:
            log(f"context truncated to {args.context_max_chars} chars (word boundary preserved)")
        raw_context = resolve_context_language(raw_context, lang.requested, sample_subtitle_text(units))

    if not api_key:
        result = {"success": False, "reason": "missing_api_key", "translations": {}, "skipped": [], "source_lang": lang.requested, "target_lang": target_lang}
    elif not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": [], "source_lang": lang.requested, "target_lang": target_lang}
    else:
        translations, skipped = translate_units(units, chapters, cues, lang, target_lang, api_key, args.batch_chars, args.concurrency, raw_context)
        result = {
            "success": bool(translations),
            "translations": translations,
            "skipped": skipped,
            "provider": "google",
            "source_lang": lang.requested,
            "detected_source_lang": lang.current(),
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
