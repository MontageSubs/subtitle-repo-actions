#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: microsoft_client.py
# Version: 1.3
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
#     - Solo-unit fallback retries are packed up to 5 per request using the native array
#       payload feature, reducing per-unit TCP/TLS handshake overhead.
#     - Automatic source language detection and pinning across retries.
#     - Strips extraneous whitespace on JSON serialization.
#
# 功能:
#     - 多段 Payload 数组特性（--array-size）：单次 POST 请求可同时传输多段满额 8000 字符文本。
#     - 无空格紧凑 Unicode 标记体系（⟦m1⟧, ⟦m2⟧），完美传递跨句上下文。
#     - 样式标签（<b>/<i> 到 ⟦b⟧/⟦i⟧）无损转义，保持术语偏移量绝对精准。
#     - 基于 <mstrans:dictionary> 原生词典标签的术语与人名保护。
#     - 级联重试流采用半径阶梯：滑动窗口（±20 -> ±5 -> ±2）与孤立 Cue（±5 -> ±2 -> 单条），
#       在重试仍失败时逐步收窄范围，而非重复发送同一个已出错的大请求。
#     - marker 解析大小写不敏感，并对截断/畸形 marker（如缺失开括号）启发式修复：
#       通过尾数匹配仍待确认的 marker id 列表定位并重建。
#     - 单元级兜底重试改为最多 5 条打包进一次数组请求，减少逐条握手开销。
#     - 源语言首次自动探测并在后续重试中锁定，避免误判。
#     - JSON 序列化输出时自动剥离首尾冗余空格。
#
# Dependencies / 依赖:
#     - langdetect (pip install langdetect), optional, auto-installed on first use.
#     - langdetect（pip install langdetect），可选，首次用到时自动安装。
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
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_NAME = "microsoft_client"
ENDPOINT = "https://edge.microsoft.com/translate/translatetext"
DEFAULT_BATCH_CHARS = 4000
DEFAULT_ARRAY_SIZE = 1
DEFAULT_CONCURRENCY = 20
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_DELAY = 3
LENGTH_RATIO_MIN = 0.15
LENGTH_RATIO_MAX = 6.0

SOLO_RETRY_ARRAY_SIZE = 5
WINDOW_RADIUS_LADDER = (20, 5, 2)
ISOLATED_RADIUS_LADDER = (5, 2, 0)

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
CUE_MARKER_TEMPLATE = "\u27e6c{:04d}\u27e7"
CUE_MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7", re.IGNORECASE)

ANY_MARKER_PATTERN = re.compile(r"\u27e6[a-zA-Z](\d+)\u27e7")
CORRUPT_MARKER_PATTERN = re.compile(r"\\+[^\u27e6\u27e7]{0,6}?(\d{1,6})\u27e7")
CORRUPT_MARKER_SIGNATURE = re.compile(r"\\+[^\u27e6\u27e7]{0,6}?\d{1,6}\u27e7")

def repair_corrupt_markers(text, prefix_char, expected_ids):
    if not text or not expected_ids:
        return text
    seen = {int(m.group(1)) for m in ANY_MARKER_PATTERN.finditer(text)}
    pending = [cid for cid in expected_ids if cid not in seen]
    if not pending:
        return text

    pieces, cursor, changed = [], 0, False
    for m in CORRUPT_MARKER_PATTERN.finditer(text):
        digits = m.group(1)
        candidates = [cid for cid in pending if cid not in seen and str(cid).endswith(digits)]
        if len(candidates) != 1:
            continue
        cid = candidates[0]
        seen.add(cid)
        pieces.append(text[cursor:m.start()])
        pieces.append(f"\u27e6{prefix_char}{cid}\u27e7")
        cursor = m.end()
        changed = True
    if not changed:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)

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
    return len(SCRIPT_LEAK_PATTERNS[sl].findall(text)) > 1

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

def split_oversized(items, limit):
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
        group_chars = sum(len(item["text"]) for item in group_items)
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

def parse_translated_html(html, marker_pattern, prefix_char=None, expected_ids=None):
    flat = unescape_html(TAG_PATTERN.sub("", html))
    if prefix_char and expected_ids:
        flat = repair_corrupt_markers(flat, prefix_char, expected_ids)
    inner_result = {
        int(k): v for k, v in split_by_marker(flat, marker_pattern).items()
        if k.isdigit() and not CORRUPT_MARKER_SIGNATURE.search(v)
    }
    return inner_result

def translate_batch(batch, lang, target_lang, context_html=None):
    all_items = [item for segment in batch for group in segment for item in group]
    expected_ids = {item["id"] for item in all_items}
    
    result, missing = {}, expected_ids
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload, segment_ids_list = [], []
            for seg_idx, segment in enumerate(batch):
                segment_ids = [item["id"] for group in segment for item in group]
                segment_str = "".join(
                    "".join(f"{wrap_marker(GROUP_MARKER_TEMPLATE.format(item['id']))}{item.get('html', escape_html(item['text']))}" for item in group)
                    for group in segment
                )
                if context_html and attempt == 1 and seg_idx == 0:
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
            missing = expected_ids - result.keys()
            if not missing:
                return result, []
        except Exception as e:
            log(f"attempt {attempt} failed: {e}")
            
        if missing and attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    if len(all_items) > 1 and missing:
        by_id = {item["id"]: item for item in all_items}
        missing_items = [by_id[uid] for uid in sorted(missing)]
        for i in range(0, len(missing_items), SOLO_RETRY_ARRAY_SIZE):
            chunk = missing_items[i:i + SOLO_RETRY_ARRAY_SIZE]
            try:
                payload = [f"{GROUP_MARKER_TEMPLATE.format(item['id'])}{item.get('html', escape_html(item['text']))}" for item in chunk]
                resp = call_microsoft_api(payload, lang.current(), target_lang)
                for entry_idx, item in enumerate(chunk):
                    if entry_idx >= len(resp) or not resp[entry_idx].get("translations"):
                        continue
                    entry_text = resp[entry_idx]["translations"][0]["text"]
                    marker_res = parse_translated_html(entry_text, GROUP_MARKER_PATTERN, "m", [item["id"]])
                    if item["id"] in marker_res:
                        result[item["id"]] = marker_res[item["id"]]
            except Exception as e:
                log(f"solo-array retry failed: {e}")
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
    return [s["id"] for s in unit["spans"] if s.get("boundary") == "marker"]

def missing_cue_ids(unit, text):
    expected = expected_cue_ids(unit)
    if not expected: return []
    present = split_cue_chunks(text)
    return [cid for cid in expected if cid not in present]

def retry_single(text, term_matches, lang, target_lang):
    if not text or not text.strip(): return None
    item = {"id": 1, "text": text, "html": protect_content_html(text, term_matches or [])}
    res, _ = translate_batch([[[item]]], lang, target_lang)
    return res.get(1)

def retry_windowed_at_radius(units, suspect_id, radius, lang, target_lang, batch_chars):
    index = {u["id"]: i for i, u in enumerate(units)}
    i = index[suspect_id]
    window = units[max(0, i - radius):i + radius + 1]
    if len(window) < 2: return {}
    
    windowed_text = "".join(
        f"{wrap_marker(UNIT_MARKER_TEMPLATE.format(unit['id']))}{protect_content_html(unit['text'], unit.get('term_matches') or [])}"
        for unit in window
    )
    if len(windowed_text) > batch_chars: return {}

    payload = [windowed_text]
    try:
        resp = call_microsoft_api(payload, lang.current(), target_lang)
        if not resp or not resp[0].get("translations"): return {}
        translated_html = resp[0]["translations"][0]["text"]
        window_ids = [unit["id"] for unit in window]
        marker_res = parse_translated_html(translated_html, UNIT_MARKER_PATTERN, "u", window_ids)

        keep_radius = min(2, radius)
        keep_ids = {u["id"] for u in units[max(0, i - keep_radius):i + keep_radius + 1]}
        unit_by_id = {u["id"]: u for u in window}
        return {
            uid: text for uid, text in marker_res.items()
            if uid in keep_ids and is_length_plausible(unit_by_id[uid]["text"], text)
        }
    except Exception as e:
        log(f"windowed retry failed: {e}")
        return {}

def retry_windowed(units, suspect_id, lang, target_lang, batch_chars):
    for radius in WINDOW_RADIUS_LADDER:
        recovered = retry_windowed_at_radius(units, suspect_id, radius, lang, target_lang, batch_chars)
        if recovered:
            return recovered
    return {}

def patch_missing_cues(text, expected_ids, recovered):
    if not recovered: return text
    chunks = split_cue_chunks(text)
    chunks.update(recovered)
    return "".join(f"{CUE_MARKER_TEMPLATE.format(cid)}{chunks[cid]}" for cid in expected_ids if cid in chunks)

def retry_isolated_cues_at_radius(missing_ids, cue_order, cue_text_by_id, cue_term_matches, radius, lang, target_lang, batch_chars):
    position = {cid: i for i, cid in enumerate(cue_order)}
    positions = sorted(position[cid] for cid in missing_ids if cid in position)
    if not positions: return {}
    lo = max(0, positions[0] - radius)
    hi = min(len(cue_order) - 1, positions[-1] + radius)

    sent_ids = [cid for cid in cue_order[lo:hi + 1] if cid in cue_text_by_id]
    html = "".join(
        f"{wrap_marker(CUE_MARKER_TEMPLATE.format(cid))}{protect_content_html(cue_text_by_id[cid], cue_term_matches.get(cid) or [])}"
        for cid in sent_ids
    )
    if len(html) > batch_chars: return {}
    
    try:
        resp = call_microsoft_api([html], lang.current(), target_lang)
        if not resp or not resp[0].get("translations"): return {}
        translated_html = resp[0]["translations"][0]["text"]

        marker_res = parse_translated_html(translated_html, CUE_MARKER_PATTERN, "c", sent_ids)
        recovered = {}
        for cid in missing_ids:
            cand = marker_res.get(cid)
            if cand and is_length_plausible(cue_text_by_id.get(cid, ""), cand):
                recovered[cid] = apply_term_replacements(cand, cue_term_matches.get(cid) or [], target_lang)
        return recovered
    except Exception as e:
        log(f"isolated cue retry failed: {e}")
        return {}

def retry_isolated_cues(missing_ids, cue_order, cue_text_by_id, cue_term_matches, lang, target_lang, batch_chars):
    recovered, remaining = {}, list(missing_ids)
    for radius in ISOLATED_RADIUS_LADDER:
        if not remaining: break
        got = retry_isolated_cues_at_radius(remaining, cue_order, cue_text_by_id, cue_term_matches, radius, lang, target_lang, batch_chars)
        recovered.update(got)
        remaining = [cid for cid in remaining if cid not in got]
    return recovered

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
                    
    results = dict(resolved)
    for unit in pending:
        raw_text = translations_raw.get(unit["id"])
        final_text = apply_term_replacements(raw_text, unit.get("term_matches") or [], target_lang) if raw_text is not None else None

        if final_text is not None and is_untranslated(final_text, lang.current(), target_lang):
            retried = retry_single(unit["text"], unit.get("term_matches"), lang, target_lang)
            if retried:
                final_text = apply_term_replacements(retried, unit.get("term_matches") or [], target_lang)
        results[unit["id"]] = final_text

    unit_by_id = {u["id"]: u for u in units}

    length_suspects = {uid for uid, text in results.items()
                        if text is not None and has_content(unit_by_id[uid]["text"])
                        and (not has_content(text) or not is_length_plausible(unit_by_id[uid]["text"], text))}
    cue_suspects = {uid for uid, text in results.items()
                    if text is not None and (missing_cue_ids(unit_by_id[uid], text) or CORRUPT_MARKER_SIGNATURE.search(text))}
    
    cue_order = [c["id"] for c in cues]
    cue_text_by_id = {c["id"]: c["text"] for c in cues}
    cue_term_matches = build_cue_term_matches(units)

    for uid in sorted(length_suspects | cue_suspects):
        recovered = retry_windowed(units, uid, lang, target_lang, batch_chars)
        if recovered:
            recovered = {rid: apply_term_replacements(text, unit_by_id[rid].get("term_matches") or [], target_lang)
                         for rid, text in recovered.items()}
            log(f"windowed retry around unit {uid}: recovered {sorted(recovered)}")
            results.update(recovered)

        remaining = missing_cue_ids(unit_by_id[uid], results[uid])
        if not remaining: continue
        
        trivial = [cid for cid in remaining if not has_translatable_content(cue_text_by_id.get(cid, ""), cue_term_matches.get(cid))]
        if trivial:
            filled = {cid: apply_term_replacements(cue_text_by_id[cid], cue_term_matches.get(cid) or [], target_lang) for cid in trivial}
            results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), filled)
            remaining = [cid for cid in remaining if cid not in trivial]
            
        if not remaining: continue
        recovered_cues = retry_isolated_cues(remaining, cue_order, cue_text_by_id, cue_term_matches, lang, target_lang, batch_chars)
        if recovered_cues:
            results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), recovered_cues)
            log(f"isolated cue retry for unit {uid}: recovered cues {sorted(recovered_cues)}")

    final_skipped = [str(uid) for uid, text in results.items() if text is None]
    final_translations = {str(uid): restore_formatting_tags(text).strip() for uid, text in results.items() if text is not None}
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
