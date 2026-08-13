#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: bilingual_merge.py
# Version: 2.0.1
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
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
#     v1.7: Spans marked with boundary="marker" (isolated short cues merged
#     cross-cue by srt_extract.py v1.7) are split on the literal ⟦cNNNN⟧
#     token instead of guessed punctuation/ratio boundaries. Token order/
#     identity is verified against the spans before trusting it; any
#     mismatch falls back to the existing estimation path for that unit
#     (with markers treated as protected spans so estimation never cuts
#     through a stray token). Any leftover token is stripped before output
#     regardless of which path was taken.
#     v1.7: 标记为 boundary="marker" 的接缝（由 srt_extract.py v1.7 跨cue
#     合并的孤立短句）按字面 ⟦cNNNN⟧ 标记硬切割，不再靠标点/比例猜测。
#     切割前先核对标记的顺序与编号是否与spans吻合，不吻合则该unit整体
#     回退到原有估计路径（并将标记视为受保护片段，估计逻辑绝不会从中切开）。
#     无论走哪条路径，残留标记在输出前一律清除。
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


def is_chinese_target(target_lang):
    return (target_lang or "").split("-")[0].lower() == "zh"


def ensure_jieba():
    global _jieba_module, _jieba_checked
    if _jieba_checked:
        return _jieba_module
    _jieba_checked = True
    try:
        import jieba
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "jieba"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60
            )
            import jieba
        except Exception as e:
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
MUSIC_MARKER_PATTERN = re.compile(r"\u27e6u(\d+)\u27e7")
ALL_MARKER_PATTERN = re.compile(r"\u27e6[cu](\d+)\u27e7")
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
SOURCE_LEADING_TAG_PATTERN = re.compile(r"^(?:<[^>]+>)*\s*")
POSITION_TOP_TAG = "{\\an7}"


def fix_music_spacing(text):
    text = MUSIC_NOTE_LEADING_GAP_PATTERN.sub(r" \1", text)
    return MUSIC_NOTE_TRAILING_GAP_PATTERN.sub(r"\1 ", text)


def source_is_music(text):
    stripped = SOURCE_LEADING_TAG_PATTERN.sub("", text)
    return bool(stripped) and stripped[0] in MUSIC_NOTE_CHARS


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


def source_starts_with_quote(text):
    stripped = SOURCE_LEADING_TAG_PATTERN.sub("", text)
    return bool(stripped) and stripped[0] in ('"', CJK_OPEN_QUOTE)


def source_ends_with_quote(text):
    return bool(text) and text[-1] in ('"', CJK_CLOSE_QUOTE)


def restore_quote_markers(translation, source_text, target_lang):
    quotes = target_quote_pair(target_lang)
    if not quotes or not translation:
        return translation
    if not (source_starts_with_quote(source_text) and source_ends_with_quote(source_text)):
        return translation
    open_q, close_q = quotes
    if translation[0] not in ('"', open_q):
        translation = open_q + translation
    if translation[-1] not in ('"', close_q):
        translation = translation + close_q
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


CLOSING_TAIL_CHARS = "'\"”’)\\]}》」』】〕＞〉»›"
GENERAL_PUNCT_SEARCH_PATTERN = re.compile(r"[，,、；;。.!?！？：:…]+[" + CLOSING_TAIL_CHARS + r"]*")
LEFT_CUT_PATTERN = re.compile(r"[“「『（([{＜〈《【〔„‚«‹¿¡]")
BOOK_TITLE_PATTERN = re.compile(r"《[^《》]*》")
EMBEDDED_QUOTE_PATTERN = re.compile(r"“[^“”]*”")
EMBEDDED_QUOTE_MAX_CHARS = 16
ORIGINAL_PUNCT_TOLERANCE = {"trail_off": 0.60, "comma": 0.20, "period": 0.20, "colon": 0.20}
INFERRED_PUNCT_TOLERANCE = 0.15
INFERRED_MIN_SHARE = 0.5


def find_protected_spans(text, glossary_terms, target_lang=None):
    spans = [(m.start(), m.end()) for m in BOOK_TITLE_PATTERN.finditer(text)]
    spans.extend((m.start(), m.end()) for m in LATIN_WORD_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in ALL_MARKER_PATTERN.finditer(text))
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


def resolve_cut(text, cursor, expected, boundary, max_cut, protected=(), target_lang=None):
    limit = len(text)
    ceiling = min(limit, max_cut)
    chunk = max(expected - cursor, 0)
    if boundary:
        candidates = [m.end() for m in BOUNDARY_SEARCH_PATTERNS[boundary].finditer(text, cursor)
                      if cursor < m.end() < ceiling and not inside_protected_span(m.end(), protected)]
        if candidates:
            cut = min(candidates, key=lambda pos: abs(pos - expected))
            if abs(cut - expected) <= max(ORIGINAL_PUNCT_TOLERANCE.get(boundary, 0.20) * chunk, 3):
                return cut, "original"
    inferred = [m.end() for m in GENERAL_PUNCT_SEARCH_PATTERN.finditer(text, cursor)
                if cursor < m.end() < ceiling and not inside_protected_span(m.end(), protected)]
    inferred += [m.start() for m in LEFT_CUT_PATTERN.finditer(text, cursor)
                 if cursor < m.start() < ceiling and not inside_protected_span(m.start(), protected)]
    if inferred:
        cut = min(inferred, key=lambda pos: abs(pos - expected))
        if abs(cut - expected) <= max(INFERRED_PUNCT_TOLERANCE * chunk, 2):
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


def split_by_boundary(translated_text, spans, protected=(), target_lang=None):
    boundary_types = [classify_boundary(span["text"]) for span in spans[:-1]]
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
    span_count = len(spans)
    cursor, cumulative, parts, tags = 0, 0, [], []
    
    for i, (length, boundary) in enumerate(zip(lengths[:-1], boundary_types)):
        cumulative += length
        target_ratio = cumulative / total
        if total_weight > 0:
            target_weight = total_weight * target_ratio
            curr = 0
            expected = len(translated_text)
            for j, w in enumerate(weights):
                curr += w
                if curr >= target_weight:
                    expected = j
                    break
        else:
            expected = len(translated_text) * target_ratio
            
        max_cut = len(translated_text) - (span_count - 1 - i)
        cut, tag = resolve_cut(translated_text, cursor, expected, boundary, max_cut, protected, target_lang)
        tags.append(tag)
        parts.append(translated_text[cursor:cut].strip())
        cursor = cut
        
    parts.append(translated_text[cursor:].strip())
    if "original" in tags:
        method = "original_boundary"
    elif "inferred" in tags:
        method = "inferred_punctuation"
    else:
        method = "word_boundary"
    return parts, method


def has_content(text):
    return bool(WORD_CHAR_PATTERN.search(text))


def repair_empty_parts(parts, spans, protected=(), target_lang=None):
    parts = list(parts)
    for i, part in enumerate(parts):
        if has_content(part):
            continue
        neighbor = i - 1 if i > 0 else i + 1
        if not (0 <= neighbor < len(parts)):
            continue
        lo, hi = sorted((i, neighbor))
        fixed, _ = split_by_boundary(parts[neighbor], spans[lo:hi + 1], protected, target_lang)
        parts[lo], parts[hi] = fixed
    return parts


def enforce_quote_closure(parts, translated_text, target_lang):
    quotes = target_quote_pair(target_lang)
    if not quotes or len(parts) < 2:
        return parts
    open_q, close_q = quotes
    if not (translated_text.startswith(open_q) and translated_text.endswith(close_q)):
        return parts
    last = len(parts) - 1
    closed = []
    for i, part in enumerate(parts):
        if not part:
            closed.append(part)
            continue
        if i > 0 and not part.startswith(open_q):
            part = open_q + part
        if i < last and not part.endswith(close_q):
            part = part + close_q
        closed.append(part)
    return closed


def split_by_markers(translated_text, spans, protected=(), target_lang=None):
    expected_ids = [span["id"] for span in spans[1:] if span.get("boundary") == "marker"]
    if not expected_ids:
        return None
    found_ids = [int(g) for g in MARKER_PATTERN.findall(translated_text)]
    if found_ids != expected_ids:
        return None
    cut_indices = [i for i, span in enumerate(spans[1:], start=1) if span.get("boundary") == "marker"]
    text_chunks = MARKER_PATTERN.split(translated_text)[0::2]
    parts, cursor = [], 0
    for chunk_index, cut in enumerate(cut_indices):
        sub_spans = spans[cursor:cut]
        sub_text = text_chunks[chunk_index]
        sub_parts = [sub_text.strip()] if len(sub_spans) == 1 else split_by_boundary(sub_text, sub_spans, protected, target_lang)[0]
        parts.extend(sub_parts)
        cursor = cut
    sub_spans = spans[cursor:]
    sub_text = text_chunks[-1]
    parts.extend([sub_text.strip()] if len(sub_spans) == 1 else split_by_boundary(sub_text, sub_spans, protected, target_lang)[0])
    return parts


def split_by_music_markers(translated_text, spans):
    expected_ids = [span["id"] for span in spans]
    found_ids = [int(g) for g in MUSIC_MARKER_PATTERN.findall(translated_text)]
    if not found_ids or found_ids != expected_ids:
        return None
    chunks = MUSIC_MARKER_PATTERN.split(translated_text)
    parts = [chunks[i * 2 + 2].strip() for i in range(len(spans))]
    if chunks[0].strip() and parts:
        parts[0] = chunks[0].strip() + " " + parts[0]
    return parts


def split_translation(translated_text, spans, protected=(), target_lang=None):
    method = "single"
    if len(spans) == 1:
        parts = [translated_text.strip()]
    else:
        parts = split_by_music_markers(translated_text, spans)
        if parts is not None:
            method = "music_marker"
        else:
            parts = split_by_markers(translated_text, spans, protected, target_lang)
            if parts is not None:
                method = "marker_boundary"
            else:
                parts, method = split_by_boundary(translated_text, spans, protected, target_lang)
                if any(span.get("boundary") == "marker" for span in spans[1:]):
                    method = "marker_mismatch"
    parts = [RESIDUAL_MARKER_PATTERN.sub(" ", p).strip() for p in parts]
    parts = enforce_punctuation_placement(parts)
    if len(spans) > 1:
        parts = repair_empty_parts(parts, spans, protected, target_lang)
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


def build_bilingual_cues(cues, units, translations, target_lang):
    cue_segments = {}
    approx_splits = []
    glossary_terms = collect_glossary_terms(units)
    dash_style = determine_dash_style(cues)
    
    for unit in units:
        spans = unit["spans"]
        translated = translations.get(str(unit["id"]))
        if translated is None:
            for span in spans:
                cue_segments.setdefault(span["id"], []).append(None)
            continue
        translated = strip_unsourced_brackets("".join(span["text"] for span in spans), translated)
        protected = find_protected_spans(translated, glossary_terms, target_lang)
        parts, method = split_translation(translated, spans, protected, target_lang)
        if method not in ("single", "original_boundary", "inferred_punctuation", "marker_boundary"):
            approx_splits.append({"unit_id": unit["id"], "cues": [span["id"] for span in spans], "method": method})
        for span, part in zip(spans, parts):
            cue_segments.setdefault(span["id"], []).append(normalize_translation(part, target_lang))

    results = []
    for cue in cues:
        parts = cue_segments.get(cue["id"])
        if not parts or any(p is None for p in parts):
            translation = None
        elif len(parts) > 1:
            translation = " ".join(f"-{p.lstrip('- ')}" for p in parts)
        else:
            translation = parts[0]
        if translation:
            translation = DASH_REPLACE_PATTERN.sub(rf"\1{dash_style}", translation)
            translation = normalize_exclaim_question(translation)
            if source_is_music(cue["text"]):
                translation = POSITION_TOP_TAG + format_music_line(translation)
            else:
                translation = restore_quote_markers(translation, cue["text"], target_lang)
        results.append({**cue, "translation": translation})
    return results, approx_splits


def format_srt_time(value):
    return value.replace(".", ",")


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
    translations = translate_data.get("translations", {})
    register_glossary_terms(collect_glossary_terms(extract_data["units"]), target_lang)
    cues, approx_splits = build_bilingual_cues(extract_data["cues"], extract_data["units"], translations, target_lang)

    unit_of_cue = {span["id"]: unit["id"] for unit in extract_data["units"] for span in unit["spans"]}
    position_of_cue = {cue["id"]: position for position, cue in enumerate(cues, start=1)}
    missing_cues = [cue["id"] for cue in cues if cue.get("translation") is None]

    if DEBUG_MODE:
        for split in approx_splits:
            positions = [position_of_cue[cid] for cid in split["cues"]]
            log(f"approximate split: unit {split['unit_id']} / cues {split['cues']} / srt #{positions} via {split['method']}")
        for cid in missing_cues:
            log(f"missing translation: cue {cid} / unit {unit_of_cue.get(cid)} / srt #{position_of_cue[cid]}")

    return {
        "success": True,
        "srt": render_srt(cues),
        "approx_splits": approx_splits,
        "missing_count": len(missing_cues),
        "missing_cues": missing_cues,
    }


def main():
    global DEBUG_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"

    extract_data = json.load(open(args.extract, encoding="utf-8"))
    translate_data = json.load(open(args.translations, encoding="utf-8"))

    result = merge(extract_data, translate_data)
    log(f"status: ok (cues={len(extract_data['cues'])}, missing={result['missing_count']}, approx_splits={len(result['approx_splits'])})")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result["srt"])
        with open(f"{args.output}.meta.json", "w", encoding="utf-8") as f:
            json.dump({"approx_splits": result["approx_splits"], "missing_count": result["missing_count"]}, f, ensure_ascii=False)
    else:
        print(result["srt"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
