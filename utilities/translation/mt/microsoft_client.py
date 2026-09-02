#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: microsoft_client.py
# Version: 1.6
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p), Joey
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Batch-translates subtitle units through Microsoft Edge Translation
#     endpoint. Units are formatted with zero-space compact stitching and unique
#     Unicode markers (⟦m1⟧, ⟦m2⟧) instead of isolating HTML tags or spaces,
#     preserving full inter-sentence narrative context for accurate coreference
#     and pronoun resolution. HTML styling tags (<b>, <i>) are safely escaped
#     to custom markers (⟦b⟧...⟦/b⟧, ⟦i⟧...⟦/i⟧) before transmission and
#     restored losslessly upon completion. Glossary terms are protected via
#     native <mstrans:dictionary> definitions. Features a cascading retry
#     pipeline (Missing Units -> Windowed Context Retry -> Isolated Cue Retry)
#     with automatic source language pinning.
#     通过 Microsoft Edge 翻译接口批量翻译字幕单元。单元间采用无空格紧凑拼接
#     与唯一 Unicode 标记（⟦m1⟧, ⟦m2⟧）连接，取代造成上下文割裂的 HTML 标签或
#     断句空格，完整保留跨句段落语境，从而实现精准的代词指代与连贯翻译。原始
#     文本中的样式标签（<b>, <i>）在发送前安全转义为自定义闭合标记
#     （⟦b⟧...⟦/b⟧, ⟦i⟧...⟦/i⟧），并在翻译完成后无损还原。词表术语通过原生
#     <mstrans:dictionary> 标签保护。内置多级级联重试系统（缺失单元重试 ->
#     滑动窗口上下文重试 -> 孤立 Cue 重试）并支持源语言自动检测与锁定。
#
# Features:
#     - Multi-segment payload support (--array-size): Send multiple 8k segments in a single POST array.
#     - Zero-space compact marker scheme (⟦m1⟧, ⟦m2⟧) for contextual translation.
#     - Lossless formatting tag escape (<b>/<i> to ⟦b⟧/⟦i⟧) preserving character offsets.
#     - Native glossary protection using <mstrans:dictionary> definitions.
#     - Cascading retry pipeline with radius ladder: Windowed Context (±20 -> ±5 -> ±2) and
#       Isolated Cues (±5 -> ±2 -> solo) shrink context on repeated failure instead of
#       repeating an identical oversized request.
#     - Case-insensitive marker matching and heuristic repair of truncated/malformed markers
#       (e.g. missing opening bracket) via suffix-matching against still-pending marker ids.
#     - Packed Network Payload: Generates payloads for all retry radii simultaneously and packs
#       them efficiently into bulk array requests. Network operations are executed concurrently
#       (up to MAX_CONCURRENCY) and reassembled out-of-order, drastically reducing wall-clock latency.
#     - Automatic source language detection and pinning across retries.
#     - Strips extraneous whitespace on JSON serialization.
#
# Usage / 用法:
#     python microsoft_client.py --input extract.json --source-lang en --target-lang zh-Hans --output translations.json
# ============================================================================
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_NAME = "microsoft_client"
ENDPOINT = "https://edge.microsoft.com/translate/translatetext"
DEFAULT_BATCH_CHARS = 4000
DEFAULT_ARRAY_SIZE = 1
DEFAULT_CONCURRENCY = 6
REQUEST_TIMEOUT = 30
LENGTH_RATIO_MIN = 0.15
LENGTH_RATIO_MAX = 6.0

WINDOW_RADIUS_LADDER = (5, 3, 1, 0)
ISOLATED_RADIUS_LADDER = (5, 3, 1, 0)

DEBUG_MODE = False
DEBUG_SEQUENCE = [0]
DEBUG_LOCK = threading.Lock()
DEBUG_RAW_IN_FILE = None
DEBUG_RAW_OUT_FILE = None

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"

GROUP_MARKER_TEMPLATE = "\u27e6m{}\u27e7"
GROUP_MARKER_PATTERN = re.compile(r"\u27e6m([^\u27e6\u27e7]+)\u27e7", re.IGNORECASE)
UNIT_MARKER_TEMPLATE = "\u27e6u{}\u27e7"
UNIT_MARKER_PATTERN = re.compile(r"\u27e6u([^\u27e6\u27e7]+)\u27e7", re.IGNORECASE)
CUE_MARKER_TEMPLATE = "\u27e6c{}\u27e7"
CUE_MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7", re.IGNORECASE)

CORRUPT_MARKER_PATTERN = re.compile(r"\\+[^\u27e6\u27e7]{0,6}?(\d{1,6})\u27e7")
UNCLOSED_MARKER_SIGNATURE = r"\u27e6[a-zA-Z]\d{1,6}(?!\d)(?!\u27e7)"
MISSING_OPEN_MARKER_SIGNATURE = r"(?<!\u27e6)[a-zA-Z]\d{1,6}\u27e7"
CORRUPT_MARKER_SIGNATURE = re.compile(
    r"\\+[^\u27e6\u27e7]{0,6}?\d{1,6}\u27e7|" + UNCLOSED_MARKER_SIGNATURE + "|" + MISSING_OPEN_MARKER_SIGNATURE
)
MARKER_BRACKET_PATTERN = re.compile(r"[\u27e6\u27e7]")
MARKER_DEBRIS_PATTERN = re.compile(r"\\+[0-9\uFFFD]{0,6}[muc](?![a-zA-Z0-9])")

def strip_marker_debris(text):
    return MARKER_DEBRIS_PATTERN.sub("", text)

def has_marker_leak(original_text, translated_text):
    original_count = len(MARKER_BRACKET_PATTERN.findall(original_text or ""))
    translated_count = len(MARKER_BRACKET_PATTERN.findall(translated_text or ""))
    return translated_count > original_count

def repair_corrupt_markers(text, prefix_char, expected_ids):
    if not text or not expected_ids:
        return text

    valid_pattern = re.compile(rf"\u27e6{prefix_char}(\d+)\u27e7")
    seen = {int(m.group(1)) for m in valid_pattern.finditer(text)}
    pending = {cid for cid in expected_ids if cid not in seen}
    if not pending:
        return text

    def replacer(m):
        before, num_str, after = m.group(1), m.group(2), m.group(3)
        cid = int(num_str)
        if cid in pending:
            corrupt_chars = "\u27e6\u27e7\\\ufffd[]{}<> " + prefix_char.lower() + prefix_char.upper()

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
                if any(ch in before + after for ch in "\u27e6\u27e7\\\ufffd[]{}<>"):
                    is_marker = True

            if is_marker:
                pending.discard(cid)
                return f"{clean_before}\u27e6{prefix_char}{cid}\u27e7{clean_after}"
        return m.group(0)

    pattern = re.compile(r"([^\d\s]*\s*)(\d+)(\s*[^\d\s]*)")
    text = pattern.sub(replacer, text)

    if pending:
        empty_pattern = re.compile(rf"(?:[\u27e6\\\ufffd]{{1,3}}{prefix_char}[\u27e7\\\ufffd]{{1,3}}|[\u27e6\u27e7\\\ufffd]{{2,4}})", re.IGNORECASE)
        empty_matches = list(empty_pattern.finditer(text))
        if 0 < len(empty_matches) <= len(pending):
            pending_list = sorted(list(pending))
            def empty_replacer(m):
                if pending_list:
                    return f"\u27e6{prefix_char}{pending_list.pop(0)}\u27e7"
                return m.group(0)
            text = empty_pattern.sub(empty_replacer, text)

    return text

FORMAT_TAG_ESCAPE_PATTERNS = [
    (re.compile(r"\s*<b\b[^>]*>\s*", re.IGNORECASE), "\u27e6b\u27e7"),
    (re.compile(r"\s*</b>\s*", re.IGNORECASE), "\u27e6/b\u27e7"),
    (re.compile(r"\s*<i\b[^>]*>\s*", re.IGNORECASE), "\u27e6i\u27e7"),
    (re.compile(r"\s*</i>\s*", re.IGNORECASE), "\u27e6/i\u27e7"),
]
FORMAT_TAG_RESTORE_PATTERNS = [
    (re.compile(r"\u27e6\s*b\s*\u27e7", re.IGNORECASE), "<b>"),
    (re.compile(r"\u27e6\s*/\s*b\s*\u27e7", re.IGNORECASE), "</b>"),
    (re.compile(r"\u27e6\s*i\s*\u27e7", re.IGNORECASE), "<i>"),
    (re.compile(r"\u27e6\s*/\s*i\s*\u27e7", re.IGNORECASE), "</i>"),
]

def escape_formatting_tags(text):
    if not text:
        return text
    for pattern, repl in FORMAT_TAG_ESCAPE_PATTERNS:
        text = pattern.sub(repl, text)
    return text

def restore_formatting_tags(text):
    if not text:
        return text
    for pattern, repl in FORMAT_TAG_RESTORE_PATTERNS:
        text = pattern.sub(repl, text)
    return text

CONTENT_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)
TAG_PATTERN = re.compile(r"<[^>]+>")

NO_TRANSLATE_TEMPLATE = '<mstrans:dictionary translation="{0}">{0}</mstrans:dictionary>'
WRAP_MARKERS = False

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

_LANGDETECT_MODULE = None
_LANGDETECT_CHECKED = False

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

def ensure_langdetect():
    global _LANGDETECT_MODULE, _LANGDETECT_CHECKED
    if _LANGDETECT_CHECKED: return _LANGDETECT_MODULE
    _LANGDETECT_CHECKED = True
    try: import langdetect
    except ImportError:
        if not pip_install("langdetect"):
            log("langdetect unavailable, context language check disabled")
            return None
        try: import langdetect
        except ImportError as e:
            log(f"langdetect unavailable, context language check disabled: {e}")
            return None
    langdetect.DetectorFactory.seed = 0
    _LANGDETECT_MODULE = langdetect
    return _LANGDETECT_MODULE

def detect_language(text):
    langdetect = ensure_langdetect()
    if langdetect is None: return None
    try: return langdetect.detect(text)
    except Exception: return None

def primary_subtag(lang_code):
    return (lang_code or "").split("-")[0].lower()

def sample_subtitle_text(units, max_chars=500):
    pieces, total = [], 0
    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text: continue
        pieces.append(text)
        total += len(text)
        if total >= max_chars: break
    return " ".join(pieces)[:max_chars]

def resolve_context_language(raw_context, requested_source_lang, subtitle_sample):
    if not raw_context: return None
    context_detected = detect_language(raw_context)
    if context_detected is None:
        log("langdetect unavailable or inconclusive on context text, dropping context")
        return None
    reference = requested_source_lang if requested_source_lang and requested_source_lang != "auto" else detect_language(subtitle_sample)
    if reference is None:
        log("could not determine subtitle source language locally, dropping context")
        return None
    if primary_subtag(context_detected) != primary_subtag(reference):
        log(f"context language ({context_detected}) does not match subtitle language ({reference}), dropping context")
        return None
    log(f"context language ({context_detected}) matches subtitle language, sending as provided")
    return raw_context

def truncate_context(text, max_chars):
    if len(text) <= max_chars: return text, False
    cut = text[:max_chars]
    boundary_ok = not (cut[-1:].isascii() and cut[-1:].isalpha() and text[max_chars:max_chars + 1].isascii() and text[max_chars:max_chars + 1].isalpha())
    if not boundary_ok:
        last_space = cut.rfind(" ")
        if last_space > 0: cut = cut[:last_space]
    return cut.rstrip(), True

def normalize_microsoft_lang(lang_code):
    lc = (lang_code or "").lower()
    if lc in ("zh", "zh-cn", "zh-hans", "zh-sg"): return "zh-Hans"
    if lc in ("zh-tw", "zh-hk", "zh-mo", "zh-hant"): return "zh-Hant"
    if lc == "auto": return ""
    return lang_code

class LanguageResolver:
    def __init__(self, requested):
        self.requested = requested
        self._detected = None
        self._lock = threading.Lock()

    @property
    def is_auto(self):
        return self.requested.lower() == "auto"

    def current(self):
        if not self.is_auto:
            return normalize_microsoft_lang(self.requested)
        with self._lock:
            return normalize_microsoft_lang(self._detected) if self._detected else ""

    def observe(self, detected):
        if not self.is_auto or not detected: return
        with self._lock:
            if self._detected is None:
                self._detected = detected
                log(f"auto-detected language: {detected} (pinned)")

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def unescape_html(text):
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")

def has_content(text):
    return bool(text) and bool(CONTENT_CHAR_PATTERN.search(text))

def content_length(text):
    return len(CONTENT_CHAR_PATTERN.findall(text or ""))

def script_of(lang):
    if not lang: return None
    normalized = lang.lower().split("-")[0]
    if normalized == "zh": return "cjk"
    return LANGUAGE_SCRIPTS.get(normalized)

def wrap_marker(text):
    return NO_TRANSLATE_TEMPLATE.format(text) if WRAP_MARKERS else text

def is_untranslated(text, source_lang, target_lang):
    if not text: return False
    sl = script_of(source_lang)
    tl = script_of(target_lang)
    if not sl or not tl or sl == tl: return False
    return len(SCRIPT_LEAK_PATTERNS[sl].findall(text)) >= 1

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
        span_text = {s["id"]: s["text"] for s in unit.get("spans", []) if s.get("boundary") == "marker"}
        return [cid for cid in marker_ids if cid in chunks and is_leaked_untranslated(span_text.get(cid, ""), chunks[cid], source_lang, target_lang)]
    ids = unit_cue_ids(unit)
    return ids if len(ids) == 1 and is_leaked_untranslated(unit["text"], text, source_lang, target_lang) else []

def build_protected_spans(text, term_matches):
    spans = [{"start": m.start(), "end": m.end(), "wrap": WRAP_MARKERS, "target": None} for m in CUE_MARKER_PATTERN.finditer(text)]
    spans.extend({"start": m["start"], "end": m["end"], "wrap": True, "target": m.get("target")} for m in term_matches)
    spans.sort(key=lambda s: s["start"])
    merged = []
    for span in spans:
        if merged and span["start"] <= merged[-1]["end"] and span["wrap"] == merged[-1]["wrap"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
            if not merged[-1].get("target"):
                merged[-1]["target"] = span.get("target")
        else:
            merged.append(dict(span))
    return merged

def protect_content_html(text, term_matches):
    text = escape_formatting_tags(text)
    pieces, cursor = [], 0
    for span in build_protected_spans(text, term_matches):
        pieces.append(escape_html(text[cursor:span["start"]]))
        piece = escape_html(text[span["start"]:span["end"]])
        if span["wrap"]:
            target = escape_html(span["target"]) if span.get("target") else piece
            pieces.append(f'<mstrans:dictionary translation="{target}">{piece}</mstrans:dictionary>')
        else:
            pieces.append(piece)
        cursor = span["end"]
    pieces.append(escape_html(text[cursor:]))
    return "".join(pieces)

def apply_term_replacements(text, term_matches, target_lang):
    return text

def item_wire_chars(item):
    return len(GROUP_MARKER_TEMPLATE.format(item["id"])) + len(item.get("html", item["text"]))

def split_oversized(items, limit):
    pieces, piece, piece_chars, oversized = [], [], 0, []
    for item in items:
        item_chars = item_wire_chars(item)
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

def build_segment_groups(items, chapter_groups, segment_chars):
    by_id = {item["id"]: item for item in items}
    segments, oversized = [], []
    current, current_chars = [], 0

    def flush():
        nonlocal current, current_chars
        if current: segments.append(current)
        current, current_chars = [], 0

    limit = max(segment_chars, 100)

    for group in chapter_groups:
        group_items = [by_id[i] for i in group if i in by_id]
        if not group_items: continue
        group_chars = sum(item_wire_chars(item) for item in group_items)
        if group_chars > limit:
            flush()
            pieces, group_oversized = split_oversized(group_items, limit)
            segments.extend([piece] for piece in pieces)
            oversized.extend(group_oversized)
        elif current_chars + group_chars > limit:
            flush()
            current, current_chars = [group_items], group_chars
        else:
            current.append(group_items)
            current_chars += group_chars
    flush()
    return segments, oversized

def build_requests(segments, array_size):
    requests = []
    chunk_size = max(array_size, 1)
    for i in range(0, len(segments), chunk_size):
        requests.append(segments[i:i + chunk_size])
    return requests

def call_microsoft_api(request_texts, source_lang, target_lang):
    params = f"to={urllib.parse.quote(target_lang)}&isEnterpriseClient=false"
    if source_lang:
        params = f"from={urllib.parse.quote(source_lang)}&{params}"
    url = f"{ENDPOINT}?{params}"

    seq = next_debug_seq()
    debug_log_raw(DEBUG_RAW_IN_FILE, {"seq": seq, "ts": time.time(), "direction": "request", "source_lang": source_lang, "target_lang": target_lang, "body": request_texts})

    req = urllib.request.Request(
        url,
        data=json.dumps(request_texts).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "*/*"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        status = response.status
        headers = dict(response.headers.items())
        raw = response.read().decode("utf-8")

        try:
            payload = json.loads(raw)
        except Exception:
            payload = None

        debug_log_raw(DEBUG_RAW_OUT_FILE, {
            "seq": seq, "ts": time.time(), "direction": "response", "status": status, "headers": headers,
            "body": payload if payload is not None else raw
        })

        if status != 200:
            raise ValueError(f"HTTP {status}: {raw}")
        return payload

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
        if text: result[key] = text
    return result

def extract_marker_free_response(html):
    return restore_formatting_tags(unescape_html(TAG_PATTERN.sub("", html)).strip())

def parse_translated_html(html, marker_pattern, prefix_char=None, expected_ids=None):
    flat = unescape_html(TAG_PATTERN.sub("", html))
    if prefix_char and expected_ids:
        flat = repair_corrupt_markers(flat, prefix_char, expected_ids)
    inner_result = {
        int(k): v for k, v in split_by_marker(flat, marker_pattern).items()
        if k.isdigit()
    }
    return inner_result

def pack_by_chars(payloads, max_chars_per_request):
    chunks = []
    current = []
    current_chars = 0
    for i, payload in enumerate(payloads):
        if current and (current_chars + len(payload) > max_chars_per_request or len(current) >= 50):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(i)
        current_chars += len(payload)
    if current:
        chunks.append(current)
    return chunks

def run_packed_jobs(payloads, max_chars_per_request, lang, target_lang, concurrency):
    results = [None] * len(payloads)
    if not payloads:
        return results
    chunks = pack_by_chars(payloads, max_chars_per_request)

    def process_chunk(indices):
        chunk_payloads = [payloads[i] for i in indices]
        try:
            resp = call_microsoft_api(chunk_payloads, lang.current(), target_lang)
            if resp and isinstance(resp, list):
                for i, r_item in enumerate(resp):
                    if i < len(indices) and r_item.get("translations"):
                        results[indices[i]] = r_item["translations"][0]["text"]
        except Exception as e:
            log(f"packed jobs chunk failed: {e}")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(process_chunk, indices) for indices in chunks]
        for future in as_completed(futures):
            future.result()

    return results

def translate_batch(batch, lang, target_lang, context_html=None):
    all_items = [item for segment in batch for group in segment for item in group]
    expected_ids = {item["id"] for item in all_items}

    result = {}
    try:
        payload, segment_ids_list = [], []
        for seg_idx, segment in enumerate(batch):
            segment_ids = [item["id"] for group in segment for item in group]
            segment_str = "".join(
                "".join(f"{wrap_marker(GROUP_MARKER_TEMPLATE.format(item['id']))}{item.get('html', escape_html(item['text']))}" for item in group)
                for group in segment
            )
            if context_html and seg_idx == 0:
                segment_str = f"{context_html}{segment_str}"
            payload.append(segment_str)
            segment_ids_list.append(segment_ids)

        resp = call_microsoft_api(payload, lang.current(), target_lang)
        if resp and isinstance(resp, list):
            if len(resp) > 0 and "detectedLanguage" in resp[0]:
                lang.observe(resp[0]["detectedLanguage"].get("language"))

            for seg_idx, r_item in enumerate(resp):
                if r_item.get("translations"):
                    html = r_item["translations"][0]["text"]
                    segment_ids = segment_ids_list[seg_idx] if seg_idx < len(segment_ids_list) else list(expected_ids)
                    marker_res = parse_translated_html(html, GROUP_MARKER_PATTERN, "m", segment_ids)
                    for idx, text in marker_res.items():
                        if idx in expected_ids:
                            result[idx] = text
    except Exception as e:
        log(f"batch request failed: {e}, deferring to consolidated recovery pass")

    missing = expected_ids - result.keys()
    return result, sorted(missing)

def is_length_plausible(source_text, translated_text):
    source_len = content_length(source_text)
    if source_len == 0: return True
    ratio = content_length(translated_text) / source_len
    return LENGTH_RATIO_MIN <= ratio <= LENGTH_RATIO_MAX

def split_cue_chunks(text):
    parts = CUE_MARKER_PATTERN.split(text or "")
    result, seen = {}, set()
    for i in range(1, len(parts), 2):
        cid = int(parts[i])
        if cid in seen: continue
        seen.add(cid)
        result[cid] = parts[i + 1].strip()
    return result

def expected_cue_ids(unit):
    return [s["id"] for s in unit.get("spans", []) if s.get("boundary") == "marker"]

def missing_cue_ids(unit, text):
    expected = expected_cue_ids(unit)
    if not expected: return []
    present = split_cue_chunks(text)
    return [cid for cid in expected if cid not in present]

def retry_windowed_all(units, suspect_ids, lang, target_lang, batch_chars, concurrency, ladder=WINDOW_RADIUS_LADDER, strict_marker=False):
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
            payload = "".join(
                f"{'' if is_solo else wrap_marker(UNIT_MARKER_TEMPLATE.format(unit['id']))}{protect_content_html(unit['text'], unit.get('term_matches') or [])}"
                for unit in window
            )
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

    payloads = [job["payload"] for job in jobs]
    html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, concurrency)

    results_by_suspect = {}
    for job, html in zip(jobs, html_results):
        if not html:
            continue
        if job["is_solo"]:
            marker_res = {str(job["window_ids"][0]): extract_marker_free_response(html)}
        else:
            marker_res = parse_translated_html(html, UNIT_MARKER_PATTERN, "u", job["window_ids"])

        if strict_marker and job["radius"] > 0 and str(job["suspect_id"]) not in marker_res:
            continue

        text_raw = marker_res.get(str(job["suspect_id"]))
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

def patch_missing_cues(text, expected_ids, recovered):
    if not recovered: return text
    chunks = split_cue_chunks(text)
    chunks.update(recovered)
    return "".join(f"{CUE_MARKER_TEMPLATE.format(cid)}{chunks[cid]}" for cid in expected_ids if cid in chunks)

def retry_isolated_cues_all(missing_by_unit, cue_order, cue_text_by_id, cue_term_matches, lang, target_lang, batch_chars, concurrency, extra_valid=None):
    position = {cid: i for i, cid in enumerate(cue_order)}
    jobs = []

    for unit_id, missing_ids in missing_by_unit.items():
        positions = sorted([position[cid] for cid in missing_ids if cid in position])
        if not positions:
            continue

        for radius in ISOLATED_RADIUS_LADDER:
            lo = max(0, positions[0] - radius)
            hi = min(len(cue_order) - 1, positions[-1] + radius)
            is_solo = (lo == hi)
            sent_ids = []
            payload = ""
            for i in range(lo, hi + 1):
                cid = cue_order[i]
                text = cue_text_by_id.get(cid)
                if text is None:
                    continue
                matches = cue_term_matches.get(cid) or []
                marker = "" if is_solo else wrap_marker(CUE_MARKER_TEMPLATE.format(cid))
                payload += f"{marker}{protect_content_html(text, matches)}"
                sent_ids.append(cid)

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
            text_key = cue_text_by_id.get(job["sent_ids"][0])
            if text_key in seen_solo_text:
                job_send_index.append(seen_solo_text[text_key])
                continue
            seen_solo_text[text_key] = len(send_jobs)
        job_send_index.append(len(send_jobs))
        send_jobs.append(job)

    payloads = [job["payload"] for job in send_jobs]
    html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, concurrency)

    results_by_unit = {}
    for job, send_idx in zip(jobs, job_send_index):
        html = html_results[send_idx]
        if not html:
            continue
        if job["is_solo"] and len(job["sent_ids"]) == 1:
            marker_res = {job["sent_ids"][0]: extract_marker_free_response(html)}
        else:
            marker_res = parse_translated_html(html, CUE_MARKER_PATTERN, "c", job["sent_ids"])

        job_recovered = {}
        for cid in job["missing_ids"]:
            cand = marker_res.get(cid)
            if job["is_solo"] and cand:
                cand = repair_corrupt_markers(cand, "c", [cid])
            orig = cue_text_by_id.get(cid, "")
            if cand and not CORRUPT_MARKER_SIGNATURE.search(cand) and is_length_plausible(orig, cand) and (extra_valid is None or extra_valid(orig, cand)):
                job_recovered[cid] = apply_term_replacements(cand, cue_term_matches.get(cid) or [], target_lang)

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

def cue_term_matches_for_unit(unit):
    spans = unit.get("spans") or []
    term_matches = unit.get("term_matches") or []
    if len(spans) <= 1:
        return {span["id"]: term_matches for span in spans}
    text, cursor, result = unit["text"], 0, {}
    for span in spans:
        pos = text.find(span["text"], cursor)
        if pos == -1:
            result[span["id"]] = []
            continue
        start, end = pos, pos + len(span["text"])
        cursor = end
        result[span["id"]] = [
            {**match, "start": match["start"] - start, "end": match["end"] - start}
            for match in term_matches if start <= match["start"] and match["end"] <= end
        ]
    return result

def build_cue_term_matches(units):
    result = {}
    for unit in units:
        result.update(cue_term_matches_for_unit(unit))
    return result

def has_translatable_content(text, term_matches):
    cursor, residue = 0, []
    for match in sorted(term_matches or [], key=lambda m: m["start"]):
        residue.append(text[cursor:match["start"]])
        cursor = match["end"]
    residue.append(text[cursor:])
    return has_content("".join(residue))

def translate_units(units, chapters, cues, lang, target_lang, batch_chars, concurrency, raw_context=None, array_size=1):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    chapter_of_unit = {uid: chapter["id"] for chapter in chapters for uid in chapter["unit_ids"]}

    items, chapter_groups = [], {}
    for unit in pending:
        cid = chapter_of_unit.get(unit["id"])
        html_text = protect_content_html(unit["text"], unit.get("term_matches") or [])
        items.append({"id": unit["id"], "text": unit["text"], "html": html_text})
        chapter_groups.setdefault(cid, []).append(unit["id"])

    segments, oversized = build_segment_groups(items, list(chapter_groups.values()), batch_chars)
    batches = build_requests(segments, array_size)
    translations_raw, skipped = {}, [item["id"] for item in oversized]

    total_batches = len(batches)
    completed = 0
    start_time = time.time()
    progress_lock = threading.Lock()

    context_html = escape_html(raw_context) if raw_context else None

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(translate_batch, batch, lang, target_lang, context_html): batch for batch in batches}
        for future in as_completed(futures):
            res, miss = future.result()
            with progress_lock:
                translations_raw.update(res)
                skipped.extend(miss)
                completed += 1
                now = time.time()
                log(f"progress: {len(translations_raw)}/{len(items)} units (batch {completed}/{total_batches}, {now - start_time:.1f}s elapsed)")

    unit_order = [u["id"] for u in units]
    unit_position = {uid: i for i, uid in enumerate(unit_order)}
    initial_missing_ids = {unit["id"] for unit in pending if unit["id"] not in translations_raw}
    missing_units = [unit for unit in pending if unit["id"] in initial_missing_ids]
    if missing_units:
        payloads = [protect_content_html(u["text"], u.get("term_matches") or []) for u in missing_units]
        html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, concurrency)
        for unit, html in zip(missing_units, html_results):
            if not html:
                continue
            recovered = extract_marker_free_response(html)
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
        payloads = [protect_content_html(u["text"], u.get("term_matches") or []) for u in untranslated_jobs]
        html_results = run_packed_jobs(payloads, batch_chars, lang, target_lang, concurrency)
        for unit, html in zip(untranslated_jobs, html_results):
            if html:
                retried = extract_marker_free_response(html)
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

    unit_by_id = {u["id"]: u for u in units}
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

    markerable_cue_ids = {cid for unit in units for cid in expected_cue_ids(unit)}
    cue_order = [c["id"] for c in cues if c["id"] in markerable_cue_ids]
    cue_text_by_id = {c["id"]: c["text"] for c in cues if c["id"] in markerable_cue_ids}
    cue_term_matches = build_cue_term_matches(units)

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
        recovered = retry_windowed_all(units, sorted(all_suspects), lang, target_lang, batch_chars, concurrency, ladder=WINDOW_RADIUS_LADDER)
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
            if not remaining: continue

            trivial = [cid for cid in remaining if not has_translatable_content(cue_text_by_id.get(cid, ""), cue_term_matches.get(cid))]
            non_trivial = remaining
            if trivial:
                filled = {cid: apply_term_replacements(cue_text_by_id[cid], cue_term_matches.get(cid) or [], target_lang) for cid in trivial}
                results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), filled)
                non_trivial = [cid for cid in remaining if cid not in trivial]

            if non_trivial:
                missing_by_unit[uid] = non_trivial

        if missing_by_unit:
            recovered_cues = retry_isolated_cues_all(missing_by_unit, cue_order, cue_text_by_id, cue_term_matches, lang, target_lang, batch_chars, concurrency)
            for uid, r_cues in recovered_cues.items():
                results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), r_cues)
                log(f"isolated cue retry for unit {uid}: recovered cues {sorted(r_cues)}")

    leak_by_unit = {uid: leaked for uid, text in results.items() if text is not None
                    for leaked in [find_leaked_cue_ids(unit_by_id[uid], text, lang.current(), target_lang)] if leaked}
    if leak_by_unit:
        leak_recovered = retry_isolated_cues_all(
            leak_by_unit, cue_order, cue_text_by_id, cue_term_matches, lang, target_lang, batch_chars, concurrency,
            extra_valid=lambda orig, cand: not is_leaked_untranslated(orig, cand, lang.current(), target_lang),
        )
        for uid, r_cues in leak_recovered.items():
            unit = unit_by_id[uid]
            results[uid] = next(iter(r_cues.values())) if is_single_plain_cue(unit) else patch_missing_cues(results[uid], expected_cue_ids(unit), r_cues)
            log(f"untranslated-leak retry for unit {uid}: recovered cues {sorted(r_cues)}")

    final_skipped = [str(uid) for uid, text in results.items() if text is None]
    final_translations = {str(uid): strip_marker_debris(restore_formatting_tags(text)).strip() for uid, text in results.items() if text is not None}
    return final_translations, final_skipped

def main():
    global DEBUG_MODE, DEBUG_RAW_IN_FILE, DEBUG_RAW_OUT_FILE, WRAP_MARKERS
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default=None)
    parser.add_argument("--target-lang", default=None)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--array-size", type=int, default=DEFAULT_ARRAY_SIZE)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--alignment-mode", choices=["span", "marker"], default="span")
    parser.add_argument("--wrap-markers", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-raw-in", default=None)
    parser.add_argument("--debug-raw-out", default=None)
    parser.add_argument("--context-file", default=None)
    parser.add_argument("--context-max-chars", type=int, default=3000)
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"
    WRAP_MARKERS = args.wrap_markers

    DEBUG_RAW_IN_FILE = args.debug_raw_in
    DEBUG_RAW_OUT_FILE = args.debug_raw_out

    raw = open(args.input, encoding="utf-8").read() if args.input and args.input != "-" else sys.stdin.read()
    payload = json.loads(raw)
    units = payload.get("units", [])
    chapters = payload.get("chapters", [])
    cues = payload.get("cues", [])

    requested_lang = (args.source_lang or payload.get("source_lang") or "auto").strip()
    target_lang = normalize_microsoft_lang(args.target_lang or payload.get("target_lang"))
    lang_resolver = LanguageResolver(requested_lang)

    raw_context = None
    if args.context_file:
        raw_context = open(args.context_file, encoding="utf-8").read().strip()
        raw_context, truncated = truncate_context(raw_context, args.context_max_chars)
        if truncated:
            log(f"context truncated to {args.context_max_chars} chars (word boundary preserved)")
        raw_context = resolve_context_language(raw_context, requested_lang, sample_subtitle_text(units))

    if not units:
        result = {"success": False, "reason": "no_units", "translations": {}, "skipped": []}
    else:
        translations, skipped = translate_units(units, chapters, cues, lang_resolver, target_lang, args.batch_chars, args.concurrency, raw_context, args.array_size)
        result = {
            "success": bool(translations),
            "translations": translations,
            "skipped": skipped,
            "provider": "microsoft",
            "source_lang": requested_lang,
            "detected_source_lang": lang_resolver._detected or "",
            "target_lang": target_lang
        }

    log(f"status: {'ok' if result['success'] else 'failed'} (translated={len(result.get('translations', {}))}, skipped={len(result.get('skipped', []))})")

    out_str = json.dumps(result, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_str)
    else:
        print(out_str)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
