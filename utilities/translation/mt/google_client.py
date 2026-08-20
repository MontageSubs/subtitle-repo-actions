#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: google_client.py
# Version: 2.8.0
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
#     - Optional auto source-language detection: when no --source-lang is
#       given, "auto" is sent on the first call and the provider's detected
#       language is pinned from that point on for every subsequent call
#       (including all retries), since short isolated retries are too little
#       context for auto-detect to stay reliable across calls.
#     - Glossary terms and in-text cue markers are sent as translate="no"
#       spans; group/unit markers and the isolated-cue-retry marker stay
#       plain text (empirically more reliable at those positions than
#       protected spans; wrapping them caused deterministic, reproducible
#       per-unit drops against the real provider in testing). Touching or
#       overlapping protected spans within a unit's text are merged into one
#       span before sending, so two translate="no" tags are never placed
#       back-to-back with a zero-character gap between them — that adjacency
#       pattern was the confirmed root cause of those drops. The existing
#       missing-marker detection and windowed/isolated retries remain the
#       primary safety net for whatever protection doesn't catch.
#     - Debug raw request/response logging is append-only JSON Lines, one
#       self-contained line per call carrying a monotonic sequence number
#       (pairing request/response) and a timestamp, written under a lock via
#       O_APPEND so concurrent threads can never interleave or lose a line.
#       The request/response body is stored as native nested JSON (never
#       re-escaped as a string), and the response entry additionally carries
#       the HTTP status and headers for diagnosing provider-side behavior
#       (e.g. locating the detected-language field). Use debug_format.py to
#       render these logs as an indented, line-per-tag human-readable
#       transcript.
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
#     - Optional plain-text --context-file: a short paragraph prepended as
#       its own translatable span at the start of every div sent (batches
#       and chapter groups alike), purely to prime the NMT engine's
#       understanding — never extracted back into cue translations, and
#       never resent during any retry path (a retry exists to fix an error,
#       and the context could be the cause). Truncated at CONTEXT_MAX_CHARS
#       (default 300, --context-max-chars) with a warning, Latin truncation
#       backing off to the nearest word boundary. Verified against the
#       pinned subtitle source language before use: translated into that
#       language via a dedicated auto-source call and, after stripping
#       punctuation/whitespace, compared to the original — an unchanged
#       result means it was already correct and the original is kept
#       (preserving its exact wording), a changed result means the
#       translated version is used instead. When source language is "auto"
#       and not yet pinned, the very first batch is sent without context
#       specifically to obtain the pinned language before this check runs
#       (this probe always uses its own auto-source detection, independent
#       of whatever --source-lang was given for the subtitle content, since
#       the context file's language is a separate, unverified fact). Batch
#       packing reserves room for one context copy per chapter/div so the
#       real payload (content + repeated context) stays within batch_chars,
#       rather than letting per-div repetition silently balloon past it.
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
#     - 支持 auto 源语言：未指定 --source-lang 时首次请求发送 auto，供应商
#       返回的探测语言从此锁定用于后续所有调用（含全部重试）——短文本重试
#       上下文太少，auto 逐次探测容易在同形异义词上判断错误。
#     - 词表术语与嵌在正文中的 cue 标记以 translate="no" 发送；group/unit
#       标记及独立 cue 重试用的标记保持纯文本——实测这些位置纯文本本就
#       可靠，包裹后反而在真实供应商上造成确定性、可复现的逐单元丢失。
#       单元文本内相接触/重叠的保护区间会先合并为一个 span 再发送，杜绝
#       两个 translate="no" 标签零间隔背靠背出现——这正是此次丢失问题
#       的确认根因。既有的标记缺失检测与窗口/隔离重试仍是未被保护部分
#       的主要安全网。
#     - Debug 原始请求/响应日志改为仅追加的 JSON Lines，每行自包含、带
#       单调递增序号（用于请求/响应配对）与时间戳，加锁配合 O_APPEND
#       写入，确保并发线程之间绝不交错或丢行。请求/响应体以原生嵌套 JSON
#       结构存储（不再作为字符串二次转义），响应条目额外记录 HTTP 状态码
#       与响应头，便于排查供应商侧行为（如定位探测语言字段的实际位置）。
#       可用 debug_format.py 把这些日志渲染成缩进展开、每标签一行的
#       人类可读文本。
#     - 针对多cue合并单元（如歌词，多条⟦cNNNN⟧标记的cue合并进同一翻译单元）
#       做cue级完整性校验：任一cue标记被供应商吞并，精确定位到该cue而非仅
#       判断整个unit是否为空。修复按序回退：先复用既有窗口重跑（其自身的
#       unit边界标记已改用独立的⟦uN⟧命名空间，不再与cue自带的⟦cNNNN⟧标记
#       冲突）；仍缺失则对问题cue及前后各5条分别包裹独立<div>发送，绕开
#       span共享批处理以避免供应商融合它们；回收成功的cue原位拼回，回收
#       失败的cue原样保留，不做臆测性改写。
#     - 单个unit/cue自身文本即超出batch_chars时，绝不截断：该项被排除出
#       批次，以明确原因计入skipped上报，而非原样发送或从中截断。
#     - 可选的纯文本 --context-file：一段简短上下文，作为独立可翻译 span
#       置于每次发送的每个 div 最前面（每批、每个章节 div 均如此），纯粹
#       用于启发神经引擎理解——绝不提取回 cue 译文，也绝不在任何重试路径
#       中重发（重试本就是为了排除错误，上下文可能正是错误来源）。按
#       CONTEXT_MAX_CHARS（默认 300，--context-max-chars 可配）截断并给出
#       警告，拉丁文截断回退到最近单词边界。使用前会针对已锁定的字幕源
#       语言做校验：用独立的 auto 源语言调用把上下文翻译成该语言，剥离
#       标点/空白后与原文比对——结果不变说明原文本就正确，保留原文（不
#       损失原始措辞）；结果不同则改用翻译后的版本。若源语言为 auto 且
#       尚未锁定，会先发送不含上下文的首批用于探测锁定语言，再进行该校验
#       （这次探测始终独立用 auto，不受字幕本身 --source-lang 影响——上下文
#       文件的语言是另一件未经验证的独立事实）。批次打包时会为每个章节/div
#       预留一份上下文的字符开销，确保"真实内容+重复上下文"的总量始终在
#       batch_chars 预算内，而不是让按 div 重复的上下文悄悄超出预算。
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
DEFAULT_BATCH_CHARS = 8000
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
CUE_MARKER_TEMPLATE = "\u27e6c{:04d}\u27e7"
CUE_MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7")
CONTENT_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)

NO_TRANSLATE_TEMPLATE = '<span translate="no">{}</span>'
CONTEXT_MAX_CHARS = 300
CONTEXT_GROUP_MARKER = "ctx"
PUNCT_STRIP_PATTERN = re.compile(
    r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~"
    r"。，、；：？！…—～·「」『』（）〈〉《》【】〔〕“”‘’]"
)

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
        marker_text = GROUP_MARKER_TEMPLATE.format(CONTEXT_GROUP_MARKER) if marker else ""
        prefix = f'<span id={CONTEXT_GROUP_MARKER}>{marker_text}{context_html}</span>'
    spans = "".join(
        f'<span id={indices[item["id"]]}>'
        f'{GROUP_MARKER_TEMPLATE.format(indices[item["id"]]) if marker else ""}'
        f'{item.get("html", escape_html(item["text"]))}</span>'
        for item in group
    )
    return f"<div>{prefix}{spans}</div>"


def parse_by_spans(html):
    result = {}
    for match in SPAN_PATTERN.finditer(html):
        if not match.group(1).isdigit():
            continue
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
    html = "".join(build_chapter_html(group, indices, context_html) for group in batch)
    translated_html = post_translate_html(html, lang, target_lang, api_key)
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

    if len(items) > 1:
        by_id = {item["id"]: item for item in items}
        log(f"isolating {len(missing)} unit(s) still missing: {', '.join(sorted({str(i) for i in missing}))}")
        for uid in sorted(missing, key=str):
            solo_result, _solo_missing = translate_batch([[by_id[uid]]], lang, target_lang, api_key)
            if uid in solo_result:
                result[uid] = solo_result[uid]
        missing = expected_ids - result.keys()
    return result, sorted(missing, key=str)


def normalize_for_comparison(text):
    return re.sub(r"\s+", "", PUNCT_STRIP_PATTERN.sub("", text or "")).casefold()


def build_context(raw_context, subtitle_source_lang, api_key):
    probe = LanguageResolver("auto", label="context")
    html = f"<div>{escape_html(raw_context)}</div>"
    try:
        translated_html = post_translate_html(html, probe, subtitle_source_lang, api_key)
    except Exception as e:
        log(f"context language check failed ({e}), using context as provided")
        return raw_context
    translated = unescape_html(TAG_PATTERN.sub("", ITALIC_PATTERN.sub("", translated_html))).strip()
    if normalize_for_comparison(raw_context) == normalize_for_comparison(translated):
        log("context already in source language, using as provided")
        return raw_context
    log(f"context translated into source language ({subtitle_source_lang})")
    return translated


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
    remaining = batches
    context_html = None
    if raw_context:
        if lang.is_auto and not lang.pinned:
            log("context provided with auto source language: sending first batch without context to detect it first")
            first_result, first_missing = translate_batch(batches[0], lang, target_lang, api_key)
            translations.update(first_result)
            skipped.extend(first_missing)
            remaining = batches[1:]
        context_html = escape_html(build_context(raw_context, lang.current(), api_key))

    start_time = last_report = time.time()
    completed = total_batches - len(remaining)
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(translate_batch, batch, lang, target_lang, api_key, context_html): batch for batch in remaining}
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
    spans = [{"start": m.start(), "end": m.end()} for m in CUE_MARKER_PATTERN.finditer(text)]
    spans.extend({"start": m["start"], "end": m["end"]} for m in term_matches)
    spans.sort(key=lambda s: s["start"])
    merged = []
    for span in spans:
        if merged and span["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append(dict(span))
    return merged


def protect_content_html(text, term_matches):
    pieces, cursor = [], 0
    for span in build_protected_spans(text, term_matches):
        pieces.append(escape_html(text[cursor:span["start"]]))
        pieces.append(NO_TRANSLATE_TEMPLATE.format(escape_html(text[span["start"]:span["end"]])))
        cursor = span["end"]
    pieces.append(escape_html(text[cursor:]))
    return "".join(pieces)


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


def retry_single(text, term_matches, lang, target_lang, api_key):
    if not text or not text.strip():
        return None
    item = {"id": "retry", "text": text, "html": protect_content_html(text, term_matches or [])}
    result, _missing = translate_batch([[item]], lang, target_lang, api_key)
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


def retry_windowed(units, suspect_id, lang, target_lang, api_key, batch_chars):
    index = {unit["id"]: i for i, unit in enumerate(units)}
    i = index[suspect_id]
    window = units[max(0, i - WINDOW_CONTEXT_RADIUS):i + WINDOW_CONTEXT_RADIUS + 1]
    if len(window) < 2:
        return {}
    text_pieces = [window[0]["text"]]
    html_pieces = [protect_content_html(window[0]["text"], window[0].get("term_matches") or [])]
    for unit in window[1:]:
        text_pieces.append(f" {UNIT_MARKER_TEMPLATE.format(unit['id'])} ")
        text_pieces.append(unit["text"])
        html_pieces.append(escape_html(f" {UNIT_MARKER_TEMPLATE.format(unit['id'])} "))
        html_pieces.append(protect_content_html(unit["text"], unit.get("term_matches") or []))
    windowed_text = "".join(text_pieces)
    if not within_budget(windowed_text, batch_chars):
        return {}
    item = {"id": "window", "text": windowed_text, "html": "".join(html_pieces)}
    result, _missing = translate_batch([[item]], lang, target_lang, api_key)
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


def retry_isolated_cues(missing_ids, cue_order, cue_text_by_id, lang, target_lang, api_key, batch_chars):
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
        translated_html = post_translate_html(html, lang, target_lang, api_key)
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


def translate_units(units, chapters, cues, lang, target_lang, api_key, batch_chars, concurrency, raw_context=None):
    resolved = {unit["id"]: unit["resolved"] for unit in units if unit.get("resolved") is not None}
    pending = [unit for unit in units if unit.get("resolved") is None]
    chapter_of_unit = {uid: chapter["id"] for chapter in chapters for uid in chapter["unit_ids"]}
    items, chapter_groups = flatten_units(pending, chapter_of_unit)
    translations_raw, _skipped = translate(items, chapter_groups, lang, target_lang, api_key, batch_chars, concurrency, raw_context) if items else ({}, [])

    results = dict(resolved)
    for unit in pending:
        raw_text = translations_raw.get(unit["id"])
        final_text = apply_term_replacements(raw_text, unit.get("term_matches") or [], target_lang) if raw_text is not None else None
        if final_text is not None and is_untranslated(final_text, lang.current(), target_lang):
            retried = retry_single(unit["text"], unit.get("term_matches"), lang, target_lang, api_key)
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
        recovered = retry_windowed(units, uid, lang, target_lang, api_key, batch_chars)
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
        recovered_cues = retry_isolated_cues(remaining, cue_order, cue_text_by_id, lang, target_lang, api_key, batch_chars)
        if recovered_cues:
            results[uid] = patch_missing_cues(results[uid], expected_cue_ids(unit_by_id[uid]), recovered_cues)
            log(f"isolated cue retry for unit {uid}: recovered cues {sorted(recovered_cues)}")
        else:
            log(f"isolated cue retry for unit {uid}: cues {remaining} still missing, left as-is")

    skipped = [uid for uid, text in results.items() if text is None]
    translations = {str(uid): text for uid, text in results.items() if text is not None}
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
    parser.add_argument("--context-file", default=None)
    parser.add_argument("--context-max-chars", type=int, default=CONTEXT_MAX_CHARS)
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
