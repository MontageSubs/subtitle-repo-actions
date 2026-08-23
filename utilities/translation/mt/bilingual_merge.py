#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: bilingual_merge.py
#  Version: 2.6.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p), Joey
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Merges machine-translated JSON payload back with the original extracted
#     subtitle data to generate a bilingual SRT file. Splits merged translation
#     units back into per-cue segments using punctuation/length-ratio boundary
#     estimation (word tokenization via jieba where available), driven by the
#     `spans` list each unit carries.
#     将机器翻译返回的 JSON 数据与最初提取的字幕数据合并，生成双语 SRT 文件。
#     依据每个翻译单元自带的 `spans` 列表（原始片段文本+时间轴），用标点/
#     长度比例边界估计（可用时以结巴分词辅助定位）拆分合并翻译回各原始
#     字幕片段。
#
# Features:
#     - Reconstructs bilingual subtitle blocks maintaining original cue timing.
#     - Splits translation units back into segments using length constraints,
#       sentence boundaries, or jieba-based word tokenization.
#     - Automatically handles missing translations and logs approximation splits.
#     - Fixes missing space between Chinese text and music notes (♪ etc.).
#     - Music-line detection based on the source cue's leading character
#       (tag-tolerant); matched translations get a {\an7} top-position tag,
#       missing leading notes are added back, and interior notes are collapsed
#       into a single space.
#     - Sentence-splitting bracket set covers CJK/French/German quotes and
#       Spanish inverted punctuation.
#
# 功能:
#     - 重构双语字幕块并保持原始时间轴。
#     - 利用长度限制、句尾标点或结巴分词（jieba）将翻译单元重新拆分为字幕段落。
#     - 自动处理缺失的翻译，并记录近似拆分的单元。
#     - 修复中文译文中音符（♪等）与相邻文字间丢失的空格。
#     - 音乐行判定基于原文首字符（兼容 <i> 等前导标签）；命中时译文加 {\an7}
#       置顶标签，缺失的开头音符自动补齐，句中多余音符统一清理为一个空格。
#     - 分句开/关符号集合覆盖中日韩引号、法语/德语引号与西班牙语倒问叹号。
#
# Usage / 用法:
#     python bilingual_merge.py --extract extract.json --translations translations.json --output bilingual.srt
#
# Dependencies / 依赖:
#     - jieba (pip install jieba)
#
# Output / 输出:
#     Diagnostic logs (stderr) / 诊断日志（标准错误）:
#       - Status, cue count, missing count, approx_splits count.
#       - 执行状态、处理的字幕行数、缺失翻译的数量、近似拆分的单元数。
#
#     Result data (stdout/file) / 结果数据（标准输出/文件）:
#       - Bilingual SRT file / 双语 SRT 文本 (stdout or file).
#       - Meta JSON containing approx_splits if outputting to file.
#
# Exit codes / 退出码:
#     0    normal completion / 正常完成
#     130  interrupted by Ctrl+C / 被 Ctrl+C 中断
# ============================================================================

import argparse
import json
import logging
import os
import re
import subprocess
import sys

SCRIPT_NAME = "bilingual_merge"
DEBUG_MODE = False

_jieba_module = None
_jieba_checked = False


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


def is_chinese_target(target_lang):
    return (target_lang or "").split("-")[0].lower() == "zh"


READING_SPEED_LIMITS = {"cjk": {"cps": 9, "max_chars_per_line": 16}, "default": {"cps": 17, "max_chars_per_line": 42}}


def parse_srt_timestamp_ms(value):
    hms, _, millis = value.replace(",", ".").partition(".")
    hours, minutes, seconds = (int(part) for part in hms.split(":"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int((millis or "0").ljust(3, "0")[:3])


def evaluate_reading_speed(text, duration_ms, target_lang):
    limits = READING_SPEED_LIMITS["cjk" if is_chinese_target(target_lang) else "default"]
    lines = [line for line in text.split("\n") if line]
    longest_line = max((effective_length(line) for line in lines), default=0)
    duration_seconds = max(duration_ms / 1000, 0.001)
    cps = effective_length(text.replace("\n", " ")) / duration_seconds
    return {"cps": cps, "over_cps": cps > limits["cps"], "over_length": longest_line > limits["max_chars_per_line"]}


LATIN_PUNCT_SOURCE_LANGS = {
    "en", "es", "fr", "de", "it", "pt", "nl", "sv", "da", "no", "fi",
    "pl", "cs", "hu", "ro", "tr", "id", "vi", "ms", "tl", "ca", "eu", "gl",
}


def uses_latin_punctuation(source_lang):
    return (source_lang or "").split("-")[0].lower() in LATIN_PUNCT_SOURCE_LANGS


def punctuation_anchors_enabled(source_lang, target_lang):
    return is_chinese_target(target_lang) and uses_latin_punctuation(source_lang)


def ensure_jieba():
    global _jieba_module, _jieba_checked
    if _jieba_checked:
        return _jieba_module
    _jieba_checked = True
    try:
        import jieba
    except ImportError:
        if not pip_install("jieba"):
            log("jieba unavailable, falling back to boundary heuristics")
            return None
        try:
            import jieba
        except ImportError as e:
            log(f"jieba unavailable, falling back to boundary heuristics: {e}")
            return None
    jieba.setLogLevel(logging.ERROR)
    _jieba_module = jieba
    return _jieba_module


ELLIPSIS_PATTERN = re.compile(r"\.{2,}|…+")
DASH_ARTIFACT_PATTERN = re.compile(r"—+|-{2,}")
CJK_TERMINATOR_PATTERN = re.compile(r"[。，、]")
HALFWIDTH_COMMA_PATTERN = re.compile(r"(?<!\d)[,](?!\d)")
WHITESPACE_COLLAPSE_PATTERN = re.compile(r"\s+")
NON_WORD_PATTERN = re.compile(r"[^\w]", re.UNICODE)
NO_LINE_END_CHARS = set("“「『（([{＜〈《【〔„‚«‹¿¡'\"‘")
NO_LINE_START_CHARS = set("”」』）)]}＞〉》】〕»›、，,。.！!？?；;：:'\"’")
BOUNDARY_ORDER = ("trail_off", "comma", "period", "colon")
BOUNDARY_CLASSIFY_PATTERNS = {
    "trail_off": re.compile(r"(\.{2,}|-{2,}|—+|…+)\s*$"),
    "comma": re.compile(r"[,，]\s*$"),
    "period": re.compile(r"[.!?！？]['\"”’)\]]*\s*$"),
    "colon": re.compile(r"[:：]\s*$"),
}
BOUNDARY_SEARCH_PATTERNS = {
    "trail_off": re.compile(r"\.{2,}|-{2,}|—+|…+"),
    "comma": re.compile(r"[,，、；;]+"),
    "period": re.compile(r"[.。!?！？]+['\"”’)\]]*"),
    "colon": re.compile(r"[:：]+"),
}
MARKER_PATTERN = re.compile(r"\u27e6c(\d+)\u27e7")
RESIDUAL_MARKER_PATTERN = re.compile(r"\s*\u27e6[^\u27e6\u27e7]*\u27e7\s*")


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def collect_glossary_terms(units):
    return {m["target"] for unit in units for m in (unit.get("term_matches") or []) if m.get("target")}


def register_glossary_terms(terms, target_lang):
    if not is_chinese_target(target_lang):
        return
    jieba_module = ensure_jieba()
    if jieba_module is None:
        return
    for term in terms:
        jieba_module.add_word(term)


def enforce_line_edges(text):
    while text and text[0] in NO_LINE_START_CHARS:
        text = text[1:].lstrip()
    while text and text[-1] in NO_LINE_END_CHARS:
        text = text[:-1].rstrip()
    return text


WORD_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)


def strip_terminator(match):
    return " " if WORD_CHAR_PATTERN.search(match.string, match.end()) else ""


CJK_OPEN_QUOTE, CJK_CLOSE_QUOTE = "“", "”"
TARGET_QUOTE_PAIRS = {"zh": (CJK_OPEN_QUOTE, CJK_CLOSE_QUOTE)}
MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
MUSIC_NOTE_LEADING_GAP_PATTERN = re.compile(f"(?<=\\S)([{MUSIC_NOTE_CHARS}])")
MUSIC_NOTE_TRAILING_GAP_PATTERN = re.compile(f"([{MUSIC_NOTE_CHARS}])(?=\\S)")
MUSIC_INTERIOR_NOTE_PATTERN = re.compile(f"(?<!^)[{MUSIC_NOTE_CHARS}](?!$)")
POSITION_TOP_TAG = "{\\an7}"


def fix_music_spacing(text):
    text = MUSIC_NOTE_LEADING_GAP_PATTERN.sub(r" \1", text)
    return MUSIC_NOTE_TRAILING_GAP_PATTERN.sub(r"\1 ", text)


def format_music_line(text):
    if len(text) > 1:
        text = MUSIC_INTERIOR_NOTE_PATTERN.sub("", text)
    text = WHITESPACE_COLLAPSE_PATTERN.sub(" ", text).strip()
    if not MUSIC_NOTE_PATTERN.match(text):
        text = f"\u266a{text}" if text else MUSIC_NOTE_CHARS[0]
    if text[-1] not in MUSIC_NOTE_CHARS:
        text = f"{text}\u266a"
    return WHITESPACE_COLLAPSE_PATTERN.sub(" ", fix_music_spacing(text)).strip()


def target_quote_pair(target_lang):
    return TARGET_QUOTE_PAIRS.get((target_lang or "").split("-")[0].lower())


def restore_quote_markers(translation, source_text, target_lang):
    quotes = target_quote_pair(target_lang)
    if not quotes or not translation:
        return translation
    open_q, close_q = quotes
    open_count = translation.count(open_q)
    close_count = translation.count(close_q)
    if open_count > close_count:
        translation = translation + close_q * (open_count - close_count)
    elif close_count > open_count:
        translation = open_q * (close_count - open_count) + translation
    return translation


def space_after_ellipsis(match):
    text, end = match.string, match.end()
    if end == len(text) or text[end].isspace() or text[end] in NO_LINE_START_CHARS:
        return "..."
    return "... "


def normalize_translation(text, target_lang):
    text = DASH_ARTIFACT_PATTERN.sub("...", text)
    text = ELLIPSIS_PATTERN.sub(space_after_ellipsis, text)
    if is_chinese_target(target_lang):
        text = CJK_TERMINATOR_PATTERN.sub(strip_terminator, text)
        text = HALFWIDTH_COMMA_PATTERN.sub(strip_terminator, text)
    text = fix_music_spacing(text)
    text = WHITESPACE_COLLAPSE_PATTERN.sub(" ", text).strip()
    return enforce_line_edges(text)


LATIN_WORD_PATTERN = re.compile(r"[a-zA-Z]+(?:['’][a-zA-Z]+)*")
DIGIT_PATTERN = re.compile(r"\d")
OTHER_WORD_PATTERN = re.compile(r"[^\W_a-zA-Z0-9]", re.UNICODE)
PUNCT_WEIGHT_PATTERN = re.compile(r"[，,、；;。.!?！？：:…]")


def effective_length(text):
    latin_words = len(LATIN_WORD_PATTERN.findall(text))
    digits = len(DIGIT_PATTERN.findall(text))
    others = len(OTHER_WORD_PATTERN.findall(text))
    return (latin_words * 2.5) + (digits * 0.5) + others or len(text)


FALLBACK_BOUNDARY_PATTERN = re.compile(r"[，,、；;。.!?…\s]+")


WHITESPACE_TOKEN_PATTERN = re.compile(r"\S+\s*")


def word_boundaries(text, target_lang):
    if is_chinese_target(target_lang):
        jieba_module = ensure_jieba()
        if jieba_module is not None:
            boundaries = [0]
            for word in jieba_module.cut(text):
                boundaries.append(boundaries[-1] + len(word))
            return [b for b in boundaries if b in (0, len(text)) or (text[b - 1] != "·" and text[b] != "·")]
    if re.search(r"\s", text):
        boundaries = [0] + [m.end() for m in WHITESPACE_TOKEN_PATTERN.finditer(text)]
        return sorted(set(boundaries) | {len(text)})
    boundaries = {0, len(text)}
    boundaries.update(m.end() for m in FALLBACK_BOUNDARY_PATTERN.finditer(text))
    return sorted(boundaries)


def nearest_boundary(boundaries, target):
    return min(boundaries, key=lambda b: abs(b - target))


def classify_boundary(text):
    for name in BOUNDARY_ORDER:
        if BOUNDARY_CLASSIFY_PATTERNS[name].search(text):
            return name
    return None


def resolve_marker_anchors(text, spans):
    positions = {}
    for m in MARKER_PATTERN.finditer(text):
        positions.setdefault(int(m.group(1)), m.start())
    return {i: positions[spans[i + 1]["id"]] for i in range(len(spans) - 1)
            if spans[i + 1].get("boundary") == "marker" and spans[i + 1]["id"] in positions}


def compute_expected_positions(translated_text, spans):
    lengths = [effective_length(span["text"]) for span in spans]
    total = sum(lengths) or 1
    weights = [0.0] * len(translated_text)
    for m in LATIN_WORD_PATTERN.finditer(translated_text):
        w = 2.5 / (m.end() - m.start())
        for i in range(m.start(), m.end()):
            weights[i] = w
    for m in DIGIT_PATTERN.finditer(translated_text):
        weights[m.start()] = 0.5
    for m in OTHER_WORD_PATTERN.finditer(translated_text):
        weights[m.start()] = 1.0
    for m in PUNCT_WEIGHT_PATTERN.finditer(translated_text):
        weights[m.start()] = 0.5
    total_weight = sum(weights)
    cumulative, expected = 0, []
    for length in lengths[:-1]:
        cumulative += length
        target_ratio = cumulative / total
        if total_weight > 0:
            target_weight, curr, pos = total_weight * target_ratio, 0, len(translated_text)
            for j, w in enumerate(weights):
                curr += w
                if curr >= target_weight:
                    pos = j
                    break
        else:
            pos = len(translated_text) * target_ratio
        expected.append(pos)
    return expected


def resolve_anchor_cuts(text, spans, boundary_types, protected, expected):
    candidates_by_type = {}
    used = set()
    anchors = {}
    for i, boundary in enumerate(boundary_types):
        if not boundary:
            continue
        if boundary not in candidates_by_type:
            candidates_by_type[boundary] = [m.end() for m in BOUNDARY_SEARCH_PATTERNS[boundary].finditer(text)
                                             if not inside_protected_span(m.end(), protected) and not is_leading_punct_run(text, m.start())]
        available = [c for c in candidates_by_type[boundary] if c not in used]
        if not available:
            continue
        cut = min(available, key=lambda pos: abs(pos - expected[i]))
        anchors[i] = cut
        used.add(cut)
    return anchors


CLOSING_TAIL_CHARS = "'\"”’)\\]}》」』】〕＞〉»›"
GENERAL_STRONG_PUNCT_PATTERN = re.compile(r"[，,、；;。.!?！？：:]+[" + CLOSING_TAIL_CHARS + r"]*")
GENERAL_WEAK_PUNCT_PATTERN = re.compile(r"(?:\.{2,}|—+|…+)[" + CLOSING_TAIL_CHARS + r"]*")
LEFT_CUT_PATTERN = re.compile(r"[“「『（([{＜〈《【〔„‚«‹¿¡]")
BOOK_TITLE_PATTERN = re.compile(r"《[^《》]*》")
EMBEDDED_QUOTE_PATTERN = re.compile(r"“[^“”]*”")
EMBEDDED_QUOTE_MAX_CHARS = 16
ORIGINAL_PUNCT_TOLERANCE = {"trail_off": 0.60, "comma": 0.20, "period": 0.20, "colon": 0.20}
INFERRED_PUNCT_TOLERANCE = 0.15
INFERRED_WEAK_PUNCT_TOLERANCE = 0.06
INFERRED_MIN_SHARE = 0.5
PUNCT_PROXIMITY_CHARS = 8
PUNCT_PROXIMITY_CHARS_WEAK = 3


def is_leading_punct_run(text, match_start):
    return match_start > 0 and text[match_start - 1] in NO_LINE_END_CHARS


def find_protected_spans(text, glossary_terms, target_lang=None):
    spans = [(m.start(), m.end()) for m in BOOK_TITLE_PATTERN.finditer(text)]
    spans.extend((m.start(), m.end()) for m in LATIN_WORD_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in MARKER_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in ELLIPSIS_PATTERN.finditer(text))
    if target_quote_pair(target_lang):
        spans.extend((m.start(), m.end()) for m in EMBEDDED_QUOTE_PATTERN.finditer(text)
                     if m.start() > 0 and m.end() < len(text) and m.end() - m.start() <= EMBEDDED_QUOTE_MAX_CHARS)
    for term in glossary_terms:
        if not term:
            continue
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term)))
            start = idx + len(term)
    return spans


def inside_protected_span(pos, protected):
    return any(start < pos < end for start, end in protected)


def escape_protected_span(pos, protected):
    for start, end in protected:
        if start < pos < end:
            return start if pos - start <= end - pos else end
    return pos


def resolve_cut(text, cursor, expected, boundary, max_cut, protected=(), target_lang=None, anchor=None):
    limit = len(text)
    ceiling = min(limit, max_cut)
    if anchor is not None and cursor < anchor < ceiling:
        return anchor, "original"
    chunk = max(expected - cursor, 0)
    if boundary:
        candidates = [m.end() for m in BOUNDARY_SEARCH_PATTERNS[boundary].finditer(text, cursor)
                      if cursor < m.end() < ceiling and not inside_protected_span(m.end(), protected)
                      and not is_leading_punct_run(text, m.start())]
        if candidates:
            cut = min(candidates, key=lambda pos: abs(pos - expected))
            if abs(cut - expected) <= max(ORIGINAL_PUNCT_TOLERANCE.get(boundary, 0.20) * chunk, PUNCT_PROXIMITY_CHARS):
                return cut, "original"
    strong = [m.end() for m in GENERAL_STRONG_PUNCT_PATTERN.finditer(text, cursor)
              if cursor < m.end() < ceiling and not inside_protected_span(m.end(), protected)
              and not is_leading_punct_run(text, m.start())]
    strong += [m.start() for m in LEFT_CUT_PATTERN.finditer(text, cursor)
               if cursor < m.start() < ceiling and not inside_protected_span(m.start(), protected)]
    if strong:
        cut = min(strong, key=lambda pos: abs(pos - expected))
        if abs(cut - expected) <= max(INFERRED_PUNCT_TOLERANCE * chunk, PUNCT_PROXIMITY_CHARS):
            return cut, "inferred"
    weak = [m.end() for m in GENERAL_WEAK_PUNCT_PATTERN.finditer(text, cursor)
            if cursor < m.end() < ceiling and not inside_protected_span(m.end(), protected)
            and not is_leading_punct_run(text, m.start())]
    if weak:
        cut = min(weak, key=lambda pos: abs(pos - expected))
        if abs(cut - expected) <= max(INFERRED_WEAK_PUNCT_TOLERANCE * chunk, PUNCT_PROXIMITY_CHARS_WEAK):
            return cut, "inferred"
    boundaries = [b for b in (bd + cursor for bd in word_boundaries(text[cursor:], target_lang))
                  if cursor < b < ceiling and not inside_protected_span(b, protected)]
    if boundaries:
        return nearest_boundary(boundaries, expected), None
    return escape_protected_span(max(cursor + 1, min(round(expected), ceiling - 1)), protected), None


def enforce_punctuation_placement(parts):
    parts = list(parts)
    for i in range(len(parts) - 1):
        while parts[i] and parts[i][-1] in NO_LINE_END_CHARS:
            parts[i + 1] = parts[i][-1] + parts[i + 1]
            parts[i] = parts[i][:-1]
        while parts[i + 1] and parts[i + 1][0] in NO_LINE_START_CHARS:
            parts[i] = parts[i] + parts[i + 1][0]
            parts[i + 1] = parts[i + 1][1:]
    return [p.strip() for p in parts]


def split_by_boundary(translated_text, spans, protected=(), target_lang=None, source_lang=None):
    boundary_types = [classify_boundary(span["text"]) for span in spans[:-1]]
    expected_positions = compute_expected_positions(translated_text, spans)
    anchors = resolve_anchor_cuts(translated_text, spans, boundary_types, protected, expected_positions) \
        if punctuation_anchors_enabled(source_lang, target_lang) else {}
    marker_anchors = resolve_marker_anchors(translated_text, spans)
    anchors.update(marker_anchors)
    cursor, parts, tags = 0, [], []

    for i, boundary in enumerate(boundary_types):
        expected = expected_positions[i]
        max_cut = len(translated_text) - (len(spans) - 1 - i)
        cut, tag = resolve_cut(translated_text, cursor, expected, boundary, max_cut, protected, target_lang, anchors.get(i))
        tags.append("marker" if marker_anchors.get(i) == cut else tag)
        parts.append(translated_text[cursor:cut].strip())
        cursor = cut

    parts.append(translated_text[cursor:].strip())
    if "marker" in tags:
        method = "marker_boundary" if all(t in (None, "marker") for t in tags) else "mixed_boundary"
    elif "original" in tags:
        method = "original_boundary"
    elif "inferred" in tags:
        method = "inferred_punctuation"
    else:
        method = "word_boundary"
    return parts, method


def has_content(text):
    return bool(WORD_CHAR_PATTERN.search(text))


def repair_empty_parts(parts, spans, protected=(), target_lang=None, source_lang=None):
    parts = list(parts)
    for i, part in enumerate(parts):
        if has_content(part):
            continue
        neighbor = i - 1 if i > 0 else i + 1
        if not (0 <= neighbor < len(parts)):
            continue
        lo, hi = sorted((i, neighbor))
        fixed, _ = split_by_boundary(parts[neighbor], spans[lo:hi + 1], protected, target_lang, source_lang)
        parts[lo], parts[hi] = fixed
    return parts


def enforce_quote_closure(parts, translated_text, target_lang):
    quotes = target_quote_pair(target_lang)
    if not quotes or len(parts) < 2:
        return parts
    open_q, close_q = quotes
    closed = []
    for part in parts:
        if not part:
            closed.append(part)
            continue
        open_count = part.count(open_q)
        close_count = part.count(close_q)
        if open_count > close_count:
            part = part + close_q * (open_count - close_count)
        elif close_count > open_count:
            part = open_q * (close_count - open_count) + part
        closed.append(part)
    return closed


def split_translation(translated_text, spans, protected=(), target_lang=None, source_lang=None):
    if len(spans) == 1:
        parts, method = [translated_text.strip()], "single"
    else:
        parts, method = split_by_boundary(translated_text, spans, protected, target_lang, source_lang)
    parts = [RESIDUAL_MARKER_PATTERN.sub(" ", p).strip() for p in parts]
    parts = enforce_punctuation_placement(parts)
    parts = repair_empty_parts(parts, spans, protected, target_lang, source_lang)
    return enforce_quote_closure(parts, translated_text, target_lang), method


BRACKET_CHAR_PATTERN = re.compile(r"[()（）\[\]【】{}]")
BRACKET_CONTENT_PATTERN = re.compile(r"[(（\[【{][^()（）\[\]【】{}]*[)）\]】}]")


def strip_unsourced_brackets(original_text, translated_text):
    if BRACKET_CHAR_PATTERN.search(original_text):
        return translated_text
    stripped = translated_text
    while BRACKET_CONTENT_PATTERN.search(stripped):
        stripped = BRACKET_CONTENT_PATTERN.sub("", stripped)
    return stripped


FULLWIDTH_TO_ASCII = {"！": "!", "？": "?"}
EXCLAIM_QUESTION_RUN_PATTERN = re.compile(r"[！？!?]+")


def normalize_exclaim_question(text):
    def substitute(match):
        run = "".join(FULLWIDTH_TO_ASCII.get(c, c) for c in match.group())
        if match.end() == len(text) or text[match.end()] == " ":
            return run
        return run + " "

    return EXCLAIM_QUESTION_RUN_PATTERN.sub(substitute, text)


def determine_dash_style(cues):
    space_count = 0
    nospace_count = 0
    for cue in cues:
        for line in cue["text"].split("\n"):
            line = line.strip()
            if line.startswith("-"):
                if line.startswith("- "):
                    space_count += 1
                else:
                    nospace_count += 1
        if space_count + nospace_count >= 9:
            break
    total = space_count + nospace_count
    if total > 0 and space_count / total >= 2/3:
        return "- "
    return "-"


DASH_REPLACE_PATTERN = re.compile(r"(^|\s)-\s*")


def compute_cue_music_flags(units):
    kinds_by_cue = {}
    for unit in units:
        for span in unit["spans"]:
            kinds_by_cue.setdefault(span["id"], []).append(span.get("kind") == "music")
    return {cue_id: bool(kinds) and all(kinds) for cue_id, kinds in kinds_by_cue.items()}


def build_bilingual_cues(cues, units, translations, target_lang, source_lang=None):
    cue_segments = {}
    approx_splits = []
    quality_warnings = []
    glossary_terms = collect_glossary_terms(units)
    dash_style = determine_dash_style(cues)
    cue_all_music = compute_cue_music_flags(units)

    for unit in units:
        spans = unit["spans"]
        translated = translations.get(str(unit["id"]))
        if translated is None:
            for span in spans:
                cue_segments.setdefault(span["id"], []).append((span.get("dash_index", 0), None))
            continue
        translated = strip_unsourced_brackets("".join(span["text"] for span in spans), translated)
        protected = find_protected_spans(translated, glossary_terms, target_lang)
        parts, method = split_translation(translated, spans, protected, target_lang, source_lang)
        if method not in ("single", "original_boundary", "inferred_punctuation", "marker_boundary", "mixed_boundary"):
            approx_splits.append({"unit_id": unit["id"], "cues": [span["id"] for span in spans], "method": method})
        for span, part in zip(spans, parts):
            part = normalize_translation(part, target_lang)
            if span.get("kind") == "music" and not cue_all_music.get(span["id"], False):
                part = format_music_line(part)
            cue_segments.setdefault(span["id"], []).append((span.get("dash_index", 0), part))

    results = []
    for cue in cues:
        entries = cue_segments.get(cue["id"])
        parts = [p for _, p in sorted(entries, key=lambda e: e[0])] if entries else None
        if not parts or any(p is None for p in parts):
            translation = None
        elif len(parts) > 1:
            translation = " ".join(f"-{p.lstrip('- ')}" for p in parts)
        else:
            translation = parts[0]
        if translation:
            translation = DASH_REPLACE_PATTERN.sub(rf"\1{dash_style}", translation)
            translation = normalize_exclaim_question(translation)
            if cue_all_music.get(cue["id"], False):
                translation = POSITION_TOP_TAG + format_music_line(translation)
            else:
                translation = restore_quote_markers(translation, cue["text"], target_lang)
            duration_ms = parse_srt_timestamp_ms(cue["end"]) - parse_srt_timestamp_ms(cue["start"])
            metrics = evaluate_reading_speed(translation, duration_ms, target_lang)
            if metrics["over_cps"] or metrics["over_length"]:
                quality_warnings.append({"cue_id": cue["id"], **metrics})
        results.append({**cue, "translation": translation})
    return results, approx_splits, quality_warnings


def format_srt_time(value):
    return value.replace(".", ",")


def resolve_output_encoding(requested, source_format):
    if requested and requested != "same":
        return requested, False
    return source_format.get("encoding", "utf-8"), source_format.get("bom", False)


def apply_newline_style(text, newline):
    if newline == "crlf":
        return text.replace("\n", "\r\n")
    if newline == "cr":
        return text.replace("\n", "\r")
    return text


def encode_output_text(text, encoding, bom):
    base = encoding.replace("-sig", "").lower()
    try:
        if bom and base == "utf-8":
            return text.encode("utf-8-sig"), "utf-8-sig"
        if bom and base.startswith("utf-16"):
            return text.encode("utf-16"), "utf-16"
        if bom and base.startswith("utf-32"):
            return text.encode("utf-32"), "utf-32"
        return text.encode(base), base
    except (LookupError, UnicodeEncodeError) as e:
        log(f"cannot encode output as {encoding} ({e}), falling back to utf-8")
        return text.encode("utf-8"), "utf-8"


def render_srt(cues):
    blocks = []
    for position, cue in enumerate(cues, start=1):
        translation = cue.get("translation") or ""
        lines = [translation, cue["text"]] if translation else [cue["text"]]
        blocks.append(f"{position}\n{format_srt_time(cue['start'])} --> {format_srt_time(cue['end'])}\n" + "\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def merge(extract_data, translate_data):
    target_lang = translate_data.get("target_lang", "")
    if DEBUG_MODE and is_chinese_target(target_lang):
        jieba_module = ensure_jieba()
        if jieba_module is not None:
            log(f"jieba: available (v{getattr(jieba_module, '__version__', 'unknown')})")
        else:
            log("jieba: unavailable, splitting falls back to punctuation boundaries")
    source_lang = translate_data.get("source_lang", "")
    translations = translate_data.get("translations", {})
    register_glossary_terms(collect_glossary_terms(extract_data["units"]), target_lang)
    cues, approx_splits, quality_warnings = build_bilingual_cues(extract_data["cues"], extract_data["units"], translations, target_lang, source_lang)

    position_of_cue = {cue["id"]: position for position, cue in enumerate(cues, start=1)}
    missing_cues = [cue["id"] for cue in cues if cue.get("translation") is None]

    if DEBUG_MODE:
        for split in approx_splits:
            positions = [position_of_cue[cid] for cid in split["cues"]]
            log(f"approximate split: cues {split['cues']} / srt #{positions} via {split['method']}")
        for cid in missing_cues:
            log(f"missing translation: cue {cid} / srt #{position_of_cue[cid]}")

    return {
        "success": True,
        "srt": render_srt(cues),
        "approx_splits": approx_splits,
        "missing_count": len(missing_cues),
        "missing_cues": missing_cues,
        "quality_warnings": quality_warnings,
    }


def main():
    global DEBUG_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--output-encoding", default=None,
                         help="'same' (default: inherit source_format recorded by srt_extract.py) or an explicit codec name such as utf-8")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"

    extract_data = json.load(open(args.extract, encoding="utf-8"))
    translate_data = json.load(open(args.translations, encoding="utf-8"))

    result = merge(extract_data, translate_data)
    log(f"status: ok (cues={len(extract_data['cues'])}, missing={result['missing_count']}, "
        f"approx_splits={len(result['approx_splits'])}, quality_warnings={len(result['quality_warnings'])})")

    if args.output:
        source_format = extract_data.get("source_format") or {}
        resolved_encoding, use_bom = resolve_output_encoding(args.output_encoding, source_format)
        newline = source_format.get("newline", "lf")
        srt_bytes, actual_encoding = encode_output_text(apply_newline_style(result["srt"], newline), resolved_encoding, use_bom)
        log(f"output written as {actual_encoding}{' with BOM' if use_bom else ''}, newline style: {newline}")
        with open(args.output, "wb") as f:
            f.write(srt_bytes)
        with open(f"{args.output}.meta.json", "w", encoding="utf-8") as f:
            json.dump({"approx_splits": result["approx_splits"], "missing_count": result["missing_count"],
                       "quality_warnings": result["quality_warnings"]}, f, ensure_ascii=False)
    else:
        print(result["srt"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
