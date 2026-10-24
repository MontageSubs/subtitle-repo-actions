#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: bilingual_merge.py
# Version: 2.12.0
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
#     - Spans flagged `style_wrap` by extraction (a cue fully wrapped by one
#       <i>/<b>/<u> tag) get that tag re-applied per split segment after
#       translation; inline <i>/<b>/<u> runs survive alignment cuts intact.
#     - Multi-speaker music cues wrap each dash segment's notes individually
#       instead of enclosing the whole joined line in one pair.
#     - Optional --wrap-lines: balances over-long translations into two lines
#       with the same algorithm as the web app (markup-aware cuts that close and
#       reopen <i>/<b>/<u>, protected ellipses/numbers/names/quotes, pyramid
#       balance with punctuation rewards scaled by line balance).
#
# 功能:
#     - 重构双语字幕块并保持原始时间轴。
#     - 利用长度限制、句尾标点或结巴分词（jieba）将翻译单元重新拆分为字幕段落。
#     - 自动处理缺失的翻译，并记录近似拆分的单元。
#     - 修复中文译文中音符（♪等）与相邻文字间丢失的空格。
#     - 音乐行判定基于原文首字符（兼容 <i> 等前导标签）；命中时译文加 {\an7}
#       置顶标签，缺失的开头音符自动补齐，句中多余音符统一清理为一个空格。
#     - 分句开/关符号集合覆盖中日韩引号、法语/德语引号与西班牙语倒问叹号。
#     - 可选 --wrap-lines：与网页端同一算法，将过长译文均衡拆为两行（标签感知，
#       跨 <i>/<b>/<u> 切分时自动闭合并重开；保护省略号/数字/人名/引号；金字塔
#       平衡，标点奖励按两行均衡度缩放）。
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
import unicodedata
import bisect
import functools
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


def is_japanese_target(target_lang):
    return (target_lang or "").split("-")[0].lower() == "ja"


def is_chinese_target(target_lang):
    return language_key(target_lang).split("-")[0] in ("zh", "yue")


LANGUAGE_ALIASES = {"cantonese": "yue", "lzh": "zh-hant", "iw": "he", "in": "id", "nb": "no", "nn": "no", "fil": "tl"}
TRADITIONAL_REGIONS = {"tw", "hk", "mo"}


@functools.lru_cache(maxsize=256)
def language_key(code):
    language, *subtags = (code or "").strip().lower().replace("_", "-").split("-")
    if language != "zh":
        return LANGUAGE_ALIASES.get(language, language)
    if "hant" in subtags:
        return "zh-hant"
    if "hans" in subtags:
        return "zh-hans"
    return "zh-hant" if TRADITIONAL_REGIONS.intersection(subtags) else "zh-hans"


READING_PROFILES = {
    "zh-hans": (9, 16, "weighted"), "zh-hant": (9, 16, "weighted"), "yue": (9, 16, "weighted"),
    "ja": (4, 13, "weighted"), "ko": (12, 16, "weighted"),
    "en": (20, 42, "plain"), "hi": (22, 42, "plain"), "ru": (17, 39, "plain"), "th": (17, 35, "plain"),
}
DEFAULT_READING_PROFILE = (17, 42, "plain")
MARKUP_PATTERN = re.compile(r"\{[^}]*\}|</?[a-zA-Z][^>]*>")


def parse_srt_timestamp_ms(value):
    hms, _, millis = value.replace(",", ".").partition(".")
    hours, minutes, seconds = (int(part) for part in hms.split(":"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int((millis or "0").ljust(3, "0")[:3])


def evaluate_reading_speed(text, duration_ms, target_lang):
    max_cps, max_chars_per_line, metric = READING_PROFILES.get(language_key(target_lang), DEFAULT_READING_PROFILE)
    lines = [line for line in (WHITESPACE_COLLAPSE_PATTERN.sub(" ", MARKUP_PATTERN.sub("", raw)).strip() for raw in text.replace("\\N", "\n").split("\n")) if line]
    joined_length = len(" ".join(lines))
    if metric == "weighted":
        weights = [content_weight(line) for line in lines]
        longest_line = max((weight or len(line) for weight, line in zip(weights, lines)), default=0)
        length = sum(weights) or joined_length
    else:
        longest_line = max(map(len, lines), default=0)
        length = joined_length
    cps = length / max(duration_ms / 1000, 0.001)
    return {"cps": cps, "over_cps": cps > max_cps, "over_length": longest_line > max_chars_per_line}


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


_tiny_segmenter = None
_tiny_segmenter_checked = False


def ensure_tiny_segmenter():
    global _tiny_segmenter, _tiny_segmenter_checked
    if _tiny_segmenter_checked:
        return _tiny_segmenter
    _tiny_segmenter_checked = True
    try:
        import tinysegmenter
    except ImportError:
        if not pip_install("tinysegmenter"):
            log("tinysegmenter unavailable, falling back to boundary heuristics")
            return None
        try:
            import tinysegmenter
        except ImportError as e:
            log(f"tinysegmenter unavailable, falling back to boundary heuristics: {e}")
            return None
    _tiny_segmenter = tinysegmenter.TinySegmenter()
    return _tiny_segmenter


def segment_words(text, target_lang):
    if is_chinese_target(target_lang):
        jieba_module = ensure_jieba()
        return list(jieba_module.cut(text)) if jieba_module is not None else None
    if is_japanese_target(target_lang):
        segmenter = ensure_tiny_segmenter()
        return segmenter.tokenize(text) if segmenter is not None else None
    return None


ELLIPSIS_PATTERN = re.compile(r"\.{2,}|…+")
DASH_ARTIFACT_PATTERN = re.compile(r"—+|-{2,}")
WHITESPACE_COLLAPSE_PATTERN = re.compile(r"\s+")
NON_WORD_PATTERN = re.compile(r"[^\w]", re.UNICODE)
NO_LINE_END_CHARS = set("“「『（([{＜〈《【〔„‚«‹¿¡'\"‘")
NO_LINE_START_CHARS = set("”」』）)]}＞〉》】〕»›、，,。.！!？?；;：:·'\"’")
BOUNDARY_ORDER = ("trail_off", "comma", "period", "colon")
BOUNDARY_CLASSIFY_PATTERNS = {
    "trail_off": re.compile(r"(\.{2,}|-{2,}|—+|…+)\s*$"),
    "comma": re.compile(r"[,，、]\s*$"),
    "period": re.compile(r"[.!?！？]['\"”’)\]]*\s*$"),
    "colon": re.compile(r"[:：]\s*$"),
}
BOUNDARY_SEARCH_PATTERNS = {
    "trail_off": (re.compile(r"\.{2,}|-{2,}|—+|…+"),),
    "comma": (re.compile(r"[,，；;]+"), re.compile(r"、+")),
    "period": (re.compile(r"[.。!?！？]+['\"”’)\]]*"),),
    "colon": (re.compile(r"[:：]+"),),
}
MARKER_PATTERN = re.compile(r"\u27e6c(\d+(?:\.\d+)?)\u27e7")
STYLE_TAG_PATTERN = re.compile(r"</?(?:i|b|u)>", re.IGNORECASE)
STYLE_TAG_PAIR_PATTERN = re.compile(r"<([ibu])>([^<]*)</\1>", re.IGNORECASE)
STYLE_TAG_SPAN_PATTERN = re.compile(r"<(i|b|u)>.*?</\1>", re.IGNORECASE | re.DOTALL)
RESIDUAL_MARKER_PATTERN = re.compile(r"(\s*)\u27e6[^\u27e6\u27e7]*\u27e7(\s*)")


def remove_residual_markers(part):
    return RESIDUAL_MARKER_PATTERN.sub(lambda m: " " if m.group(1) and m.group(2) else m.group(1) + m.group(2), part)
VALID_CUE_MARKER_PATTERN = re.compile(r"\u27e6c\d+(?:\.\d+)?\u27e7", re.IGNORECASE)
FOREIGN_MARKER_PATTERN = re.compile(r"\u27e6[^\u27e6\u27e7]*\u27e7|[\u27e6\u27e7]")
MARKER_PLACEHOLDER_PATTERN = re.compile(r"\u0002(\d+)\u0002")


def strip_foreign_markers(text):
    if "\u27e6" not in text and "\u27e7" not in text:
        return text
    preserved = []

    def guard(m):
        preserved.append(m.group(0))
        return f"\u0002{len(preserved) - 1}\u0002"

    cleaned = FOREIGN_MARKER_PATTERN.sub("", VALID_CUE_MARKER_PATTERN.sub(guard, text))
    return MARKER_PLACEHOLDER_PATTERN.sub(lambda m: preserved[int(m.group(1))], cleaned)


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


CJK_OPEN_QUOTE, CJK_CLOSE_QUOTE = "“", "”"
CJK_ANGLE_OPEN_QUOTE, CJK_ANGLE_CLOSE_QUOTE = "「", "」"
TARGET_QUOTE_PAIRS = {
    "zh": (CJK_OPEN_QUOTE, CJK_CLOSE_QUOTE),
    "yue": (CJK_ANGLE_OPEN_QUOTE, CJK_ANGLE_CLOSE_QUOTE),
}
MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
MUSIC_NOTE_LEADING_GAP_PATTERN = re.compile(f"(?<=\\S)([{MUSIC_NOTE_CHARS}])")
MUSIC_NOTE_TRAILING_GAP_PATTERN = re.compile(f"([{MUSIC_NOTE_CHARS}])(?=\\S)")
MUSIC_INTERIOR_NOTE_PATTERN = re.compile(f"(?<!^)[{MUSIC_NOTE_CHARS}](?!$)")
POSITION_TOP_TAG = "{\\an7}"


def fix_music_spacing(text):
    if not MUSIC_NOTE_PATTERN.search(text):
        return text
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
    key = language_key(target_lang)
    if key == "zh-hant":
        return CJK_ANGLE_OPEN_QUOTE, CJK_ANGLE_CLOSE_QUOTE
    return TARGET_QUOTE_PAIRS.get(key) or TARGET_QUOTE_PAIRS.get(key.split("-")[0])


def rectify_translation_quotes(translated_text, original_text, target_lang):
    quotes = target_quote_pair(target_lang)
    if not quotes or not translated_text:
        return translated_text
        
    open_q, close_q = quotes
    if open_q not in translated_text and close_q not in translated_text:
        return translated_text
    source_has_quote = bool(re.search(r'["”“]', original_text))
    
    rep_close = close_q if source_has_quote else ""
    rep_open = open_q if source_has_quote else ""
    
    phrase_pattern = r'([^' + open_q + close_q + r'。！？…\n]+)'
    translated_text = re.sub(close_q + phrase_pattern + open_q, rep_open + r'\1' + rep_close, translated_text)
    translated_text = re.sub(close_q + phrase_pattern + close_q, rep_open + r'\1' + rep_close, translated_text)
    translated_text = re.sub(open_q + phrase_pattern + open_q, rep_open + r'\1' + rep_close, translated_text)
    
    translated_text = re.sub(open_q + r'(\s*)$', rep_close + r'\1', translated_text)
    translated_text = re.sub(open_q + r'(\s*[。！？…])', rep_close + r'\1', translated_text)
    translated_text = re.sub(r'^(\s*)' + close_q, r'\1' + rep_open, translated_text)
    
    return translated_text


def space_after_ellipsis(match):
    text, end = match.string, match.end()
    if end == len(text) or text[end].isspace() or text[end] in NO_LINE_START_CHARS:
        return "..."
    return "... "


def defers_punctuation_style(target_lang):
    return is_chinese_target(target_lang) or is_japanese_target(target_lang)


def is_marker_only(content):
    return all(c.isspace() or unicodedata.category(c)[0] in "PS" for c in content)


def drop_marker_only_style_pairs(translated, source_text):
    if "</" not in translated or re.search(r"\s", STYLE_TAG_PATTERN.sub("", source_text).strip()):
        return translated
    return STYLE_TAG_PAIR_PATTERN.sub(lambda m: "" if is_marker_only(m.group(2)) else m.group(), translated)


def normalize_translation(text, target_lang):
    if "—" in text or "--" in text:
        text = DASH_ARTIFACT_PATTERN.sub("...", text)
    if (".." in text or "…" in text) and not defers_punctuation_style(target_lang):
        text = ELLIPSIS_PATTERN.sub(space_after_ellipsis, text)
    text = fix_music_spacing(text)
    text = WHITESPACE_COLLAPSE_PATTERN.sub(" ", text).strip()
    return enforce_line_edges(text)


LATIN_WORD_PATTERN = re.compile(r"[a-zA-Z]+(?:['’][a-zA-Z]+)*")
DIGIT_PATTERN = re.compile(r"\d")
OTHER_WORD_PATTERN = re.compile(r"[^\W_a-zA-Z0-9]", re.UNICODE)
PUNCT_WEIGHT_PATTERN = re.compile(r"[，,、；;。.!?！？：:…]")


def content_weight(text):
    return len(LATIN_WORD_PATTERN.findall(text)) * 2.5 + len(DIGIT_PATTERN.findall(text)) * 0.5 + len(OTHER_WORD_PATTERN.findall(text))


def effective_length(text):
    text = STYLE_TAG_PATTERN.sub("", text)
    return content_weight(text) or len(text)


FALLBACK_BOUNDARY_PATTERN = re.compile(r"[，,、；;。.!?…\s]+")


WHITESPACE_TOKEN_PATTERN = re.compile(r"\S+\s*")


NAME_JOINERS = frozenset("·•‧")
LONG_NAME_CHARS = 10
CHINESE_BREAK_RULES = {
    "zh-hans": (
        "的 地 得 了 着 过 吗 呢 吧 啊 呀 哇 啦 罢 哩 呗 嘞 咯 哈 哟 哎 耶 们 等等",
        "在 从 向 往 对 给 把 被 与 和 跟 同 随 自 于 由 按 按照 根据 为 为了 朝 凭 借 沿 沿着 趁 趁着 这 这个 这些 那 那个 那些 哪 各个 每 以及 关于 有关 对于 并且 而且 但是 然而 或者 还是 因而 所以 虽然 尽管 不仅 不但 如果 要是 即使 哪怕 只有 只要 因为 既然",
    ),
    "zh-hant": (
        "的 地 得 了 著 過 嗎 呢 吧 啊 呀 哇 啦 罷 哩 唄 嘞 咯 哈 喲 哎 耶 們 等等",
        "在 從 向 往 對 給 把 被 與 和 跟 同 隨 自 於 由 按 按照 根據 為 為了 朝 憑 藉 沿 沿著 趁 趁著 這 這個 這些 那 那個 那些 哪 各 每 以及 關於 有關 對於 並且 而且 但是 然而 或者 還是 因而 所以 雖然 儘管 不僅 不但 如果 要是 即使 哪怕 只有 只要 因為 既然",
    ),
    "yue": (
        "嘅 咗 緊 過 喇 咩 呢 咪 呀 喎 㗎 啫 囉 哩 晒 埋 掂 倒 翻 返 添 咋 嘞 啩 啝 先 定 哋",
        "喺 喺度 由 向 畀 同 將 自 跟 到 由得 為咗 趁 照 順 呢 呢個 呢啲 嗰 嗰個 嗰啲 邊個 邊度 點解 關於 同埋 而且 但係 不過 或者 所以 如果 同埋 抑或 因為 既然 縱使 雖然 就算 只要",
    ),
}


def break_rule(target_lang):
    rule = CHINESE_BREAK_RULES.get(language_key(target_lang))
    return tuple(set(words.split()) for words in rule) if rule else None


def dotted_name_runs(pieces):
    runs, previous_start, at, index = [], 0, len(pieces[0]) if pieces else 0, 1
    while index + 1 < len(pieces):
        piece = pieces[index]
        if piece not in NAME_JOINERS:
            previous_start, at, index = at, at + len(piece), index + 1
            continue
        last = index + 1
        end = at + len(piece) + len(pieces[last])
        while last + 2 < len(pieces) and pieces[last + 1] in NAME_JOINERS:
            end += len(pieces[last + 1]) + len(pieces[last + 2])
            last += 2
        runs.append((previous_start, end))
        previous_start, at, index = end - len(pieces[last]), end, last + 1
    return runs


def next_content_piece(pieces, start):
    return next((piece for piece in pieces[start:] if piece.strip()), "")


def allowed_boundaries(pieces, rule, text_length):
    no_start, no_end = rule if rule else (frozenset(), frozenset())
    runs = dotted_name_runs(pieces) if rule else []
    inside = lambda position, long: any(lo < position < hi and (hi - lo > LONG_NAME_CHARS) == long for lo, hi in runs)
    open_cuts, ruled_cuts, position, before = [], [], 0, ""
    for index, piece in enumerate(pieces):
        position += len(piece)
        if piece.strip():
            before = piece
        if position >= text_length:
            continue
        if runs and (inside(position, False) or (piece not in NAME_JOINERS and inside(position, True))):
            continue
        open_cuts.append(position)
        if rule and before not in no_end and next_content_piece(pieces, index + 1) not in no_start:
            ruled_cuts.append(position)
    return ruled_cuts if rule and ruled_cuts else open_cuts


def word_boundaries(text, target_lang):
    words = segment_words(text, target_lang)
    if words is not None:
        return [0, *allowed_boundaries(words, break_rule(target_lang), len(text)), len(text)]
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
        positions.setdefault(m.group(1), m.start())
    return {i: positions[spans[i + 1]["marker_id"]] for i in range(len(spans) - 1)
            if spans[i + 1].get("boundary") == "marker" and spans[i + 1]["marker_id"] in positions}


def build_weight_prefix(text):
    weights = [0.0] * len(text)
    for m in LATIN_WORD_PATTERN.finditer(text):
        w = 2.5 / (m.end() - m.start())
        for i in range(m.start(), m.end()):
            weights[i] = w
    for m in DIGIT_PATTERN.finditer(text):
        weights[m.start()] = 0.5
    for m in OTHER_WORD_PATTERN.finditer(text):
        weights[m.start()] = 1.0
    for m in PUNCT_WEIGHT_PATTERN.finditer(text):
        weights[m.start()] = 0.5
    prefix = [0.0] * (len(text) + 1)
    for i, w in enumerate(weights):
        prefix[i + 1] = prefix[i] + w
    return prefix


def compute_expected_positions(translated_text, spans):
    lengths = [effective_length(span["text"]) for span in spans]
    total = sum(lengths) or 1
    prefix = build_weight_prefix(translated_text)
    total_weight = prefix[-1]
    cumulative, expected = 0, []
    for length in lengths[:-1]:
        cumulative += length
        target_ratio = cumulative / total
        if total_weight > 0:
            pos = max(0, bisect.bisect_left(prefix, total_weight * target_ratio) - 1)
            pos = min(pos, len(translated_text))
        else:
            pos = len(translated_text) * target_ratio
        expected.append(pos)
    return expected


def align_cuts_to_candidates(order, expected, candidates, tolerance_of):
    n, m = len(order), len(candidates)
    if not n or not m:
        return {}
    penalty = max((tolerance_of(k) for k in range(n)), default=0) + 1
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            deviation = abs(candidates[j - 1] - expected[order[i - 1]])
            if deviation <= tolerance_of(i - 1):
                best = max(best, dp[i - 1][j - 1] + penalty - deviation)
            dp[i][j] = best
    assignment, i, j = {}, n, m
    while i > 0 and j > 0:
        if dp[i][j] == dp[i - 1][j]:
            i -= 1
        elif dp[i][j] == dp[i][j - 1]:
            j -= 1
        else:
            assignment[order[i - 1]] = candidates[j - 1]
            i, j = i - 1, j - 1
    return assignment


class CandidateIndex:
    def __init__(self, text, protected):
        self.text = text
        self.protected = protected
        self._ends = {}
        self._left_cuts = None

    def ends(self, pattern):
        ends = self._ends.get(pattern)
        if ends is None:
            ends = [m.end() for m in pattern.finditer(self.text)
                    if not inside_protected_span(m.end(), self.protected) and not is_leading_punct_run(self.text, m.start())]
            self._ends[pattern] = ends
        return ends

    def ends_between(self, pattern, lower, upper):
        ends = self.ends(pattern)
        return ends[bisect.bisect_right(ends, lower):bisect.bisect_left(ends, upper)]

    def left_cuts_between(self, lower, upper):
        if self._left_cuts is None:
            self._left_cuts = [m.start() for m in LEFT_CUT_PATTERN.finditer(self.text)
                               if not inside_protected_span(m.start(), self.protected)]
        cuts = self._left_cuts
        return cuts[bisect.bisect_right(cuts, lower):bisect.bisect_left(cuts, upper)]


def resolve_anchor_cuts(index, boundary_types, expected):
    anchors = {}
    for boundary in {bt for bt in boundary_types if bt}:
        order_all = [i for i, bt in enumerate(boundary_types) if bt == boundary]
        used = set()
        for pattern in BOUNDARY_SEARCH_PATTERNS[boundary]:
            pending = [i for i in order_all if i not in anchors]
            if not pending:
                break
            candidates = [end for end in index.ends(pattern) if end not in used]
            def tolerance_of(k, pending=pending):
                i = pending[k]
                chunk = expected[i] - (expected[i - 1] if i > 0 else 0)
                return max(ORIGINAL_PUNCT_TOLERANCE.get(boundary, 0.20) * chunk, PUNCT_PROXIMITY_CHARS)
            assignment = align_cuts_to_candidates(pending, expected, candidates, tolerance_of)
            for i, cut in assignment.items():
                anchors[i] = cut
                used.add(cut)
    return anchors


CLOSING_TAIL_CHARS = "'\"”’)\\]}》」』】〕＞〉»›"
GENERAL_STRONG_PUNCT_PATTERN = re.compile(r"[，,、；;。.!?！？：:]+[" + CLOSING_TAIL_CHARS + r"]*")
GENERAL_WEAK_PUNCT_PATTERN = re.compile(r"(?:\.{2,}|—+|…+)[" + CLOSING_TAIL_CHARS + r"]*")
LEFT_CUT_PATTERN = re.compile(r"[“「『（([{＜〈《【〔„‚«‹¿¡]")
BOOK_TITLE_PATTERN = re.compile(r"《[^《》]*》")
EMBEDDED_QUOTE_MAX_CHARS = 16
EMBEDDED_QUOTE_PATTERN_CACHE = {}


def embedded_quote_pattern(open_q, close_q):
    pattern = EMBEDDED_QUOTE_PATTERN_CACHE.get((open_q, close_q))
    if pattern is None:
        pattern = re.compile(f"{open_q}[^{open_q}{close_q}]*{close_q}")
        EMBEDDED_QUOTE_PATTERN_CACHE[(open_q, close_q)] = pattern
    return pattern
ORIGINAL_PUNCT_TOLERANCE = {"trail_off": 0.60, "comma": 0.30, "period": 0.25, "colon": 0.25}
INFERRED_PUNCT_TOLERANCE = 0.15
INFERRED_WEAK_PUNCT_TOLERANCE = 0.06
INFERRED_MIN_SHARE = 0.5
PUNCT_PROXIMITY_CHARS = 8
PUNCT_PROXIMITY_CHARS_WEAK = 3


def is_leading_punct_run(text, match_start):
    return match_start > 0 and text[match_start - 1] in NO_LINE_END_CHARS


BRACKET_PAIR_PATTERN = re.compile(r"（[^（）]{0,38}）|\([^()]{0,38}\)|【[^【】]{0,38}】|\[[^\[\]]{0,38}\]|〔[^〔〕]{0,38}〕")
BRACKET_OPEN_CHARS = frozenset("([{\uff08\uff3b\uff5b\u3014\u3016\u3018\u301a\u3008\u300a\u3010\u27e8\u27ea")
BRACKET_CLOSE_CHARS = frozenset(")]}\uff09\uff3d\uff5d\u3015\u3017\u3019\u301b\u3009\u300b\u3011\u27e9\u27eb")
MISSING_BRACKET_COST = 1
EXTRA_BRACKET_COST = 0.5
UNBALANCED_COST = 0.5
BRACKET_MISMATCH_WEIGHT = 0.25


def bracket_profile(text):
    depth, count, crossing = 0, 0, False
    for char in text:
        if char in BRACKET_OPEN_CHARS:
            count += 1
            depth += 1
        elif char in BRACKET_CLOSE_CHARS:
            count += 1
            depth -= 1
            if depth < 0:
                crossing, depth = True, 0
    return count, crossing or depth != 0


def bracket_edges(text):
    edges = []
    for index, char in enumerate(text):
        if char in BRACKET_OPEN_CHARS:
            edges.append(index)
        elif char in BRACKET_CLOSE_CHARS:
            edges.append(index + 1)
    return edges


def bracket_mismatch(part, source):
    target = bracket_profile(part)
    if (source[0] > 0) == (target[0] > 0):
        presence = 0
    else:
        presence = MISSING_BRACKET_COST if source[0] > 0 else EXTRA_BRACKET_COST
    return presence + (0 if source[1] == target[1] else UNBALANCED_COST)


def align_cuts_to_brackets(text, source_texts, cuts, locked, protected):
    sources = [bracket_profile(source) for source in source_texts]
    if all(profile[0] == 0 for profile in sources):
        return list(cuts)
    edges = bracket_edges(text)
    aligned = list(cuts)
    for i in range(len(cuts)):
        if i in locked:
            continue
        low = 0 if i == 0 else aligned[i - 1]
        high = len(text) if i == len(cuts) - 1 else aligned[i + 1]
        scale = max(1, high - low)

        def cost_at(position):
            return abs(position - cuts[i]) / scale + BRACKET_MISMATCH_WEIGHT * (
                bracket_mismatch(text[low:position], sources[i]) + bracket_mismatch(text[position:high], sources[i + 1]))

        best = aligned[i]
        best_cost = cost_at(best)
        for edge in edges:
            if edge <= low or edge >= high or edge == best or inside_protected_span(edge, protected):
                continue
            cost = cost_at(edge)
            if cost < best_cost:
                best, best_cost = edge, cost
        aligned[i] = best
    return aligned


def find_protected_spans(text, glossary_terms, target_lang=None, protect_brackets=False):
    spans = [(m.start(), m.end()) for m in BRACKET_PAIR_PATTERN.finditer(text)] if protect_brackets else []
    spans.extend((m.start(), m.end()) for m in BOOK_TITLE_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in STYLE_TAG_SPAN_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in LATIN_WORD_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in MARKER_PATTERN.finditer(text))
    spans.extend((m.start(), m.end()) for m in ELLIPSIS_PATTERN.finditer(text))
    quotes = target_quote_pair(target_lang)
    if quotes:
        pattern = embedded_quote_pattern(*quotes)
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text)
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


HARD_BREAK_PUNCT_TOLERANCE = 0.25
HARD_BREAK_PROXIMITY_CHARS = 4


def resolve_cut(index, cursor, expected, boundary, max_cut, target_lang=None, anchor=None):
    text, protected = index.text, index.protected
    limit = len(text)
    ceiling = min(limit, max_cut)
    if anchor is not None and cursor < anchor < ceiling:
        return anchor, "original"
    chunk = max(expected - cursor, 0)
    if boundary:
        cut = None
        for pattern in BOUNDARY_SEARCH_PATTERNS[boundary]:
            candidates = index.ends_between(pattern, cursor, ceiling)
            if candidates:
                cut = min(candidates, key=lambda pos: abs(pos - expected))
                break
        if cut is not None and abs(cut - expected) <= max(ORIGINAL_PUNCT_TOLERANCE.get(boundary, 0.20) * chunk, PUNCT_PROXIMITY_CHARS):
            return cut, "original"
    strong = index.ends_between(GENERAL_STRONG_PUNCT_PATTERN, cursor, ceiling) + index.left_cuts_between(cursor, ceiling)
    if strong:
        cut = min(strong, key=lambda pos: abs(pos - expected))
        strong_tol = max(HARD_BREAK_PUNCT_TOLERANCE * chunk, HARD_BREAK_PROXIMITY_CHARS) if boundary is None \
            else max(INFERRED_PUNCT_TOLERANCE * chunk, PUNCT_PROXIMITY_CHARS)
        if abs(cut - expected) <= strong_tol:
            return cut, "inferred"
    weak = index.ends_between(GENERAL_WEAK_PUNCT_PATTERN, cursor, ceiling)
    if weak:
        cut = min(weak, key=lambda pos: abs(pos - expected))
        weak_tol = max(HARD_BREAK_PUNCT_TOLERANCE * chunk, HARD_BREAK_PROXIMITY_CHARS) if boundary is None \
            else max(INFERRED_WEAK_PUNCT_TOLERANCE * chunk, PUNCT_PROXIMITY_CHARS_WEAK)
        if abs(cut - expected) <= weak_tol:
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


def refine_expected_positions(spans, anchors, expected, text_len):
    if not anchors:
        return expected
    lengths = [effective_length(span["text"]) for span in spans]
    checkpoints = sorted(anchors.items())
    bounds = [(-1, 0)] + checkpoints + [(len(expected), text_len)]
    refined = list(expected)
    for (lo_idx, lo_pos), (hi_idx, hi_pos) in zip(bounds, bounds[1:]):
        span_total = sum(lengths[lo_idx + 1:hi_idx + 1])
        if span_total <= 0:
            continue
        cumulative = 0
        for i in range(lo_idx + 1, hi_idx):
            cumulative += lengths[i]
            refined[i] = lo_pos + (hi_pos - lo_pos) * (cumulative / span_total)
    return refined


DISPROPORTION_MIN_RATIO = 0.55
DISPROPORTION_MAX_RATIO = 1.85
REBALANCE_SNAP_TOLERANCE = 0.12
REBALANCE_SNAP_FLOOR = 3


def merge_bad_runs(bad, count):
    runs = []
    for idx in sorted(bad):
        lo, hi = max(0, idx - 1), min(count - 1, idx + 1)
        if runs and lo <= runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], max(runs[-1][1], hi))
        else:
            runs.append((lo, hi))
    return runs


def snap_or_interpolate(index, ideal, scope, target_lang, tolerance):
    text, protected = index.text, index.protected
    lo, hi = scope
    candidates = index.ends_between(GENERAL_STRONG_PUNCT_PATTERN, lo, hi)
    near = [c for c in candidates if abs(c - ideal) <= tolerance]
    if near:
        return min(near, key=lambda c: abs(c - ideal))
    boundaries = [b for b in (bd + lo for bd in word_boundaries(text[lo:hi], target_lang))
                  if lo < b < hi and not inside_protected_span(b, protected)]
    if boundaries:
        return nearest_boundary(boundaries, ideal)
    return escape_protected_span(round(ideal), protected)


def rebalance_disproportionate_cuts(index, spans, cuts, locked, target_lang):
    text = index.text
    if len(spans) < 2:
        return cuts
    lengths = [effective_length(s["text"]) for s in spans]
    total_len = sum(lengths) or 1
    prefix = build_weight_prefix(text)
    total_weight = prefix[-1]
    if total_weight <= 0:
        return cuts
    boundaries = [0] + list(cuts) + [len(text)]
    ratios = []
    for i in range(len(spans)):
        expected_w = total_weight * (lengths[i] / total_len)
        actual_w = prefix[boundaries[i + 1]] - prefix[boundaries[i]]
        ratios.append(actual_w / expected_w if expected_w > 0 else 1.0)
    bad = {i for i, r in enumerate(ratios) if r < DISPROPORTION_MIN_RATIO or r > DISPROPORTION_MAX_RATIO}
    if not bad:
        return cuts
    new_cuts = list(cuts)
    for lo, hi in merge_bad_runs(bad, len(spans)):
        internal = [k for k in range(lo, hi) if k not in locked]
        if not internal:
            continue
        start_pos, end_pos = boundaries[lo], boundaries[hi + 1]
        sub_lengths = lengths[lo:hi + 1]
        sub_total = sum(sub_lengths) or 1
        sub_weight = prefix[end_pos] - prefix[start_pos]
        cumulative, cursor = 0, start_pos
        for k in range(lo, hi):
            cumulative += sub_lengths[k - lo]
            if k in locked:
                cursor = new_cuts[k]
                continue
            target_weight = prefix[start_pos] + sub_weight * (cumulative / sub_total)
            ideal = max(0, min(bisect.bisect_left(prefix, target_weight) - 1, len(text)))
            span_slots = max(hi - k, 1)
            ideal = max(cursor + 1, min(ideal, end_pos - span_slots))
            tol = max(REBALANCE_SNAP_TOLERANCE * (end_pos - start_pos) / (hi - lo), REBALANCE_SNAP_FLOOR)
            cut = snap_or_interpolate(index, ideal, (cursor, end_pos), target_lang, tol)
            cut = max(cursor + 1, min(cut, end_pos - span_slots))
            new_cuts[k] = cut
            cursor = cut
    return new_cuts


FORWARD_SNAP_WINDOW = 3


def snap_cuts_forward_to_punct(text, cuts, locked, tags, protected):
    result = list(cuts)
    for i in range(len(result)):
        if i in locked or tags[i] == "original":
            continue
        cursor = result[i]
        ceiling = result[i + 1] if i + 1 < len(result) else len(text)
        window_end = min(cursor + FORWARD_SNAP_WINDOW, ceiling)
        if window_end <= cursor:
            continue
        candidates = [m.end() for m in GENERAL_STRONG_PUNCT_PATTERN.finditer(text, cursor, window_end)
                      if not inside_protected_span(m.end(), protected)]
        if candidates:
            result[i] = min(candidates)
    return result


def split_by_boundary(translated_text, spans, protected=(), target_lang=None, source_lang=None):
    boundary_types = [classify_boundary(span["text"]) for span in spans[:-1]]
    expected_positions = compute_expected_positions(translated_text, spans)
    index = CandidateIndex(translated_text, protected)
    marker_anchors = resolve_marker_anchors(translated_text, spans)
    needs_punctuation_anchors = punctuation_anchors_enabled(source_lang, target_lang) and len(marker_anchors) < len(spans) - 1
    anchors = resolve_anchor_cuts(index, boundary_types, expected_positions) if needs_punctuation_anchors else {}
    anchors.update(marker_anchors)
    expected_positions = refine_expected_positions(spans, anchors, expected_positions, len(translated_text))
    cursor, cuts, tags = 0, [], []

    for i, boundary in enumerate(boundary_types):
        expected = expected_positions[i]
        max_cut = len(translated_text) - (len(spans) - 1 - i)
        cut, tag = resolve_cut(index, cursor, expected, boundary, max_cut, target_lang, anchors.get(i))
        tags.append("marker" if marker_anchors.get(i) == cut else tag)
        cuts.append(cut)
        cursor = cut

    locked = {i for i in range(len(cuts)) if tags[i] == "marker"}
    cuts = rebalance_disproportionate_cuts(index, spans, cuts, locked, target_lang)
    cuts = align_cuts_to_brackets(translated_text, [span["text"] for span in spans], cuts, locked, protected)
    cuts = snap_cuts_forward_to_punct(translated_text, cuts, locked, tags, protected)
    parts, cursor = [], 0
    for cut in cuts:
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


def proportional_split(text, spans, target_lang):
    title_spans = [(m.start(), m.end()) for m in BOOK_TITLE_PATTERN.finditer(text)]
    boundaries = [b for b in word_boundaries(text, target_lang) if not inside_protected_span(b, title_spans)]
    if len(boundaries) <= 2:
        return None
    weights = [effective_length(s["text"]) for s in spans]
    total = sum(weights) or len(weights)
    cursor, cuts = 0, []
    for weight in weights[:-1]:
        cursor += weight
        cuts.append(nearest_boundary(boundaries, round(len(text) * cursor / total)))
    cuts = sorted(set(c for c in cuts if 0 < c < len(text)))
    if len(cuts) != len(spans) - 1:
        return None
    parts, cursor = [], 0
    for cut in cuts:
        parts.append(text[cursor:cut].strip())
        cursor = cut
    parts.append(text[cursor:].strip())
    return parts


def repair_empty_parts(parts, spans, get_protected, target_lang=None, source_lang=None):
    parts = list(parts)
    for i, part in enumerate(parts):
        if has_content(part) or not has_content(spans[i]["text"]):
            continue
        neighbor = i - 1 if i > 0 else i + 1
        if not (0 <= neighbor < len(parts)):
            continue
        lo, hi = sorted((i, neighbor))
        fixed, _ = split_by_boundary(parts[neighbor], spans[lo:hi + 1], get_protected(), target_lang, source_lang)
        if not has_content(fixed[i - lo]):
            proportional = proportional_split(parts[neighbor], spans[lo:hi + 1], target_lang)
            if proportional and has_content(proportional[i - lo]):
                fixed = proportional
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


def split_translation(translated_text, spans, get_protected, target_lang=None, source_lang=None):
    if len(spans) == 1:
        parts, method = [translated_text.strip()], "single"
    else:
        parts, method = split_by_boundary(translated_text, spans, get_protected(), target_lang, source_lang)
    parts = [remove_residual_markers(p).strip() if ("\u27e6" in p or "\u27e7" in p) else p.strip() for p in parts]
    parts = enforce_punctuation_placement(parts)
    parts = repair_empty_parts(parts, spans, get_protected, target_lang, source_lang)
    return enforce_quote_closure(parts, translated_text, target_lang), method


BRACKET_CHAR_PATTERN = re.compile(r"[()（）\[\]【】{}]")
BRACKET_CONTENT_PATTERN = re.compile(r"[(（\[【{][^()（）\[\]【】{}]*[)）\]】}]")


def strip_unsourced_brackets(original_text, translated_text):
    if not BRACKET_CHAR_PATTERN.search(translated_text) or BRACKET_CHAR_PATTERN.search(original_text):
        return translated_text
    stripped = translated_text
    while BRACKET_CONTENT_PATTERN.search(stripped):
        stripped = BRACKET_CONTENT_PATTERN.sub("", stripped)
    return stripped


FULL_TO_HALF = {"！": "!", "？": "?", "，": ",", "。": ".", "、": ","}
CLOSING_MARKUP = r"(?:</(?:i|b|u)>|\{[^}]*\}|['\"”’»›」』）)}\]】♪♫♬♩])"
SPACE_NOT_NEEDED_PATTERN = re.compile(rf"^{CLOSING_MARKUP}*(?:$|\s|[、，。！？；：,.!?;:…])")
WORD_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)
EXCLAIM_QUESTION_RUN_PATTERN = re.compile(r"[！？!?]+")
CHINESE_TERMINATOR_PATTERN = re.compile(r"[，。]|(?<!\d),(?!\d)")
JAPANESE_TERMINATOR_PATTERN = re.compile(r"[、，。]|(?<!\d),(?!\d)")
CHINESE_FULL_TERMINATOR_PATTERN = re.compile(r"[，。]")
JAPANESE_FULL_TERMINATOR_PATTERN = re.compile(r"[、，。]")
REPEATED_SPACES_PATTERN = re.compile(r" {2,}")


def needs_space_after(text, end):
    return end < len(text) and not SPACE_NOT_NEEDED_PATTERN.match(text[end:])


def style_ellipses(text, full_width):
    if ".." not in text and "…" not in text:
        return text

    def substitute(match):
        run = match.group()
        if full_width:
            return "…" if run.startswith(".") else run
        return "... " if needs_space_after(text, match.end()) else "..."

    return ELLIPSIS_PATTERN.sub(substitute, text)


def strip_terminators(text, target_lang):
    pattern = JAPANESE_TERMINATOR_PATTERN if is_japanese_target(target_lang) else CHINESE_TERMINATOR_PATTERN
    last_content = max((m.end() for m in WORD_CHAR_PATTERN.finditer(text)), default=0)
    stripped = pattern.sub(lambda m: " " if last_content >= m.end() else "", text)
    if stripped == text:
        return text
    stripped = REPEATED_SPACES_PATTERN.sub(" ", stripped)
    return "\n".join(line.strip() for line in stripped.split("\n"))


def halve_terminators(text, target_lang):
    pattern = JAPANESE_FULL_TERMINATOR_PATTERN if is_japanese_target(target_lang) else CHINESE_FULL_TERMINATOR_PATTERN
    return pattern.sub(lambda m: FULL_TO_HALF[m.group()] + (" " if needs_space_after(text, m.end()) else ""), text)


def halve_exclaim_question(text):
    def substitute(match):
        run = "".join(FULL_TO_HALF.get(c, c) for c in match.group())
        return run + " " if needs_space_after(text, match.end()) else run

    return EXCLAIM_QUESTION_RUN_PATTERN.sub(substitute, text)


def restyle_punctuation(text, target_lang, full_width=False, keep_terminators=False):
    if not text:
        return text
    cjk = defers_punctuation_style(target_lang)
    full_width = cjk and full_width
    if cjk:
        text = style_ellipses(text, full_width)
        if not keep_terminators:
            text = strip_terminators(text, target_lang)
        elif not full_width:
            text = halve_terminators(text, target_lang)
    return text if full_width else halve_exclaim_question(text)


def determine_dash_style(cues):
    space_count = 0
    nospace_count = 0
    for cue in cues:
        for line in cue["text"].split("\n"):
            line = STYLE_TAG_PATTERN.sub("", line).strip()
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


def build_bilingual_cues(cues, units, translations, target_lang, source_lang=None, wrap_lines=False, punctuation_style=(False, False)):
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
        original_text = "".join(span["text"] for span in spans)
        translated = strip_unsourced_brackets(original_text, strip_foreign_markers(translated))
        translated = rectify_translation_quotes(translated, original_text, target_lang)
        protect_brackets = not any(bracket_profile(span["text"])[1] for span in spans)
        protected_holder = []

        def get_protected(text=translated, protect=protect_brackets):
            if not protected_holder:
                protected_holder.append(find_protected_spans(text, glossary_terms, target_lang, protect))
            return protected_holder[0]

        parts, method = split_translation(translated, spans, get_protected, target_lang, source_lang)
        if method not in ("single", "original_boundary", "inferred_punctuation", "marker_boundary", "mixed_boundary"):
            approx_splits.append({"unit_id": unit["id"], "cues": [span["id"] for span in spans], "method": method})
        for span, part in zip(spans, parts):
            part = normalize_translation(drop_marker_only_style_pairs(part, span["text"]), target_lang)
            if span.get("style_wrap") and part:
                part = f"<{span['style_wrap']}>{part}</{span['style_wrap']}>"
            if span.get("kind") == "music" and not cue_all_music.get(span["id"], False):
                part = format_music_line(part)
            cue_segments.setdefault(span["id"], []).append((span.get("dash_index", 0), part))

    results = []
    for cue in cues:
        entries = cue_segments.get(cue["id"])
        parts = [p for _, p in sorted(entries, key=lambda e: e[0])] if entries else None
        is_all_music = cue_all_music.get(cue["id"], False)
        if not parts or any(p is None for p in parts):
            translation = None
        elif len(parts) > 1 and is_all_music:
            translation = " ".join(f"-{format_music_line(p.lstrip('- '))}" for p in parts)
        elif len(parts) > 1:
            translation = " ".join(f"-{p.lstrip('- ')}" for p in parts)
        else:
            translation = parts[0]
        if translation:
            if "-" in translation:
                translation = DASH_REPLACE_PATTERN.sub(rf"\1{dash_style}", translation)
            if is_all_music and len(parts) == 1:
                translation = format_music_line(translation)
            translation = restyle_punctuation(translation, target_lang, *punctuation_style)
            duration_ms = parse_srt_timestamp_ms(cue["end"]) - parse_srt_timestamp_ms(cue["start"])
            if wrap_lines:
                translation = wrap_translation_lines(translation, target_lang)
            metrics = evaluate_reading_speed(translation, duration_ms, target_lang)
            if metrics["over_cps"] or metrics["over_length"]:
                quality_warnings.append({"cue_id": cue["id"], **metrics})
            if is_all_music:
                translation = POSITION_TOP_TAG + translation
        results.append({**cue, "translation": translation})
    return results, approx_splits, quality_warnings


INLINE_MARKUP_PATTERN = re.compile(r"\{[^}]*\}|<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")
WRAP_HARD_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"\.{2,}|[…‥]+",
    r"[!?！？]{2,}[.…]*",
    r"[—–-]{2,}|[~～]{2,}",
    r"\d+(?:[.,:·/]\d+)+%?",
    r"[$€£¥]\s?\d[\d.,]*|\d[\d.,]*\s?(?:%|‰|°[CF]?)",
    r"[^\W\d_]+(?:[·•‧][^\W\d_]+)+",
    r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    r"[#@][^\W]+",
    r"《[^《》]{1,16}》",
))
WRAP_PAIR_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"“[^“”]*”|‘[^‘’]*’|「[^「」]*」|『[^『』]*』|《[^《》]*》|（[^（）]*）|【[^【】]*】|„[^„“”]*[“”]|«[^«»]*»|‹[^‹›]*›",
    r"\([^()]*\)|\[[^\[\]]*\]|\"[^\"]*\"",
))
WRAP_NO_LINE_START_CHARS = NO_LINE_START_CHARS | set("‥っゃゅょゎッャュョヮーヵヶヽヾゝゞ々﹚﹜﹞…%‰°′″/")
WRAP_NO_LINE_END_CHARS = NO_LINE_END_CHARS | set("$€£¥₩₽₹₺₴¢§№〔〖〘〚﹙﹛﹝")
WRAP_STYLE_SPAN_PENALTY = 12
WRAP_PAIR_SPAN_PENALTY = 40
WRAP_PAIR_HARD_MAX_CHARS = 8
WRAP_CLOSERS = r"['\"”’»›」』）)}\]】]*"
WRAP_TERMINAL_PATTERN = re.compile(r"(?:\.{2,}|[.…‥。۔?!？！؟‽])" + WRAP_CLOSERS + "$")
WRAP_CLAUSE_PATTERN = re.compile(r"[:;：；؛—–]" + WRAP_CLOSERS + "$")
WRAP_MINOR_PATTERN = re.compile(r"[,，、،]" + WRAP_CLOSERS + "$")
WRAP_ABBREVIATION_PATTERN = re.compile(
    r"(?:^|\s)(?:[A-Za-z]{1,3}\.|[A-Z][a-z]{1,4}\.|etc\.|vs\.|e\.g\.|i\.e\.|approx\.|dept\.|fig\.|gen\.|gov\.|inc\.|jr\.|sr\.|ltd\.|st\.|vol\.)"
    + WRAP_CLOSERS + "$", re.IGNORECASE)
WRAP_NUMBER_TAIL_PATTERN = re.compile(r"\d+$")
WRAP_UNIT_HEAD_PATTERN = re.compile(r"^(%|‰|[a-zA-Z]{1,4}|[一-鿿])")
WRAP_ATTACHED_CLOSING_PATTERN = re.compile(r"^['\"”’»›」』）)}\]】]")
WRAP_UNBALANCED_RATIO = 1.35
WRAP_UNBALANCED_PENALTY = 15
WRAP_CLITIC_PENALTY = 70
WRAP_ORPHAN_LETTER_PENALTY = 60
WRAP_NUMBER_UNIT_PENALTY = 60
WRAP_SHORT_LINE_PENALTY = 75
WRAP_CJK_TINY_LINE_PENALTY = 150
WRAP_CJK_SHORT_LINE_SOFT_PENALTY = 20
WRAP_TERMINAL_REWARD = 40
WRAP_CLAUSE_REWARD = 24
WRAP_MINOR_REWARD = 14
WRAP_SPACE_BOUNDARY_REWARD = 20
WRAP_CONJUNCTION_REWARD = 12
WRAP_PYRAMID_WEIGHT = 0.5
WRAP_REWARD_MIN_SHARE = 0.2
WRAP_REWARD_FULL_SHARE = 0.38
CHINESE_CONJUNCTIONS = {
    "zh-hans": "并且 而且 但是 然而 或者 还是 因而 所以 虽然 尽管 不仅 不但 如果 要是 即使 哪怕 只有 只要 因为 既然",
    "zh-hant": "並且 而且 但是 然而 或者 還是 因而 所以 雖然 儘管 不僅 不但 如果 要是 即使 哪怕 只有 只要 因為 既然",
    "yue": "同埋 而且 但係 不過 或者 所以 如果 抑或 因為 既然 縱使 雖然 就算 只要",
}


def separate_markup(text):
    marks, visible, last = [], [], 0
    length = 0
    for match in INLINE_MARKUP_PATTERN.finditer(text):
        chunk = text[last:match.start()]
        visible.append(chunk)
        length += len(chunk)
        marks.append({"at": length, "raw": match.group(0), "name": (match.group(2) or "").lower() or None, "closing": match.group(1) == "/"})
        last = match.end()
    visible.append(text[last:])
    return "".join(visible), marks


def paired_spans(marks):
    open_marks, spans = [], []
    for mark in marks:
        if not mark["name"]:
            continue
        if not mark["closing"]:
            open_marks.append(mark)
            continue
        names = [entry["name"] for entry in open_marks]
        if mark["name"] not in names:
            continue
        index = len(names) - 1 - names[::-1].index(mark["name"])
        spans.append((open_marks[index]["at"], mark["at"]))
        del open_marks[index:]
    return spans


def render_marked(visible, marks, offset):
    out, cursor = [], 0
    for mark in marks:
        at = max(0, mark["at"] - offset)
        out.append(visible[cursor:at] + mark["raw"])
        cursor = at
    out.append(visible[cursor:])
    return "".join(out)


def cut_marked(visible, marks, line_end, next_start):
    first, second = [], []
    for mark in marks:
        to_first = mark["at"] < line_end or (mark["at"] <= next_start and mark["closing"])
        (first if to_first else second).append(mark)
    return (render_marked(visible[:line_end], first, 0),
            render_marked(visible[next_start:], second, next_start))


def match_ranges(text, patterns):
    return [(m.start(), m.end()) for pattern in patterns for m in pattern.finditer(text)]


def strictly_inside(ranges, position):
    return any(start < position < end for start, end in ranges)


def build_wrap_protection(visible, marks):
    pairs = match_ranges(visible, WRAP_PAIR_PATTERNS)
    hard = match_ranges(visible, WRAP_HARD_PATTERNS) + [(a, b) for a, b in pairs if b - a <= WRAP_PAIR_HARD_MAX_CHARS]
    styled = paired_spans(marks)
    return (lambda position: strictly_inside(hard, position),
            lambda position: (WRAP_STYLE_SPAN_PENALTY if strictly_inside(styled, position) else 0)
            + (WRAP_PAIR_SPAN_PENALTY if strictly_inside(pairs, position) else 0))


def wrap_tokens(text, target_lang):
    words = segment_words(text, target_lang)
    if words is None:
        words = re.findall(r"\s+|\S+", text) if re.search(r"\s", text) else list(text)
    return [(word, bool(re.search(r"[^\W_]", word))) for word in words]


WRAP_ORTHOGRAPHY = {
    "ja": (
        "お ご 御 貴 諸 各 毎 元 前 新 超 最 大 小 この その あの どの",
        "は が を に へ と から より で や の ね よ か わ ぞ ぜ な も ば て でも たり だけ ほど ばかり さえ こそ など なら かしら って ちゃ じゃ まで ので のに ても だり ながら つつ です ます ました でした",
        "そして しかし また または だが したがって だから あるいは つまり ところで なお けれども けれど",
    ),
    "en": (
        "a an the to of in for on with at by from into onto than as about upon within without toward towards through during before after above below between under over across along among against around behind beneath beside besides beyond inside outside near past since throughout until via despite my your his her its our their this that these those whose each every either neither some any no both much many several",
        "'s 're 've 'd 'll 'm n't",
        "and but or nor so yet because although while whereas since unless if",
    ),
}


@functools.lru_cache(maxsize=32)
def wrap_orthography(target_lang):
    key = language_key(target_lang)
    rule = CHINESE_BREAK_RULES.get(key)
    if rule:
        enclitics, proclitics = (set(words.split()) for words in rule)
        return proclitics, enclitics, set(CHINESE_CONJUNCTIONS[key].split())
    proclitics, enclitics, conjunctions = WRAP_ORTHOGRAPHY.get(key, ("", "", ""))
    return set(proclitics.split()), set(enclitics.split()), set(conjunctions.split())


def wrap_balance_factor(line1, line2):
    share = min(len(line1), len(line2)) / (len(line1) + len(line2))
    return min(1.0, max(0.0, (share - WRAP_REWARD_MIN_SHARE) / (WRAP_REWARD_FULL_SHARE - WRAP_REWARD_MIN_SHARE)))


def wrap_short_line_penalty(line1, line2, counts_characters, softened):
    pattern = re.compile(r"[^\W_]") if counts_characters else re.compile(r"[^\W_]+")
    units1, units2 = len(pattern.findall(line1)), len(pattern.findall(line2))
    if counts_characters:
        if units1 < 2 or units2 < 2:
            return WRAP_CJK_TINY_LINE_PENALTY
        if units1 < 3 or units2 < 3:
            return WRAP_CJK_SHORT_LINE_SOFT_PENALTY if softened else WRAP_SHORT_LINE_PENALTY
        return 0
    return WRAP_SHORT_LINE_PENALTY if units1 <= 1 or len(line1) < 4 or units2 <= 1 or len(line2) < 4 else 0


def score_wrap_candidate(offset, total, line1, line2, last_word, next_word, space_boundary, target_lang):
    proclitics, enclitics, conjunctions = wrap_orthography(target_lang)
    counts_characters = is_chinese_target(target_lang) or is_japanese_target(target_lang)
    rewards_space = is_chinese_target(target_lang) and space_boundary
    terminal = bool(WRAP_TERMINAL_PATTERN.search(line1)) and not WRAP_ABBREVIATION_PATTERN.search(line1)
    score = abs(offset - total / 2) + abs(len(line1) - len(line2)) * 0.5 + max(0, len(line1) - len(line2)) * WRAP_PYRAMID_WEIGHT
    if not terminal and not rewards_space and len(line1) > len(line2) * WRAP_UNBALANCED_RATIO:
        score += WRAP_UNBALANCED_PENALTY
    if last_word in proclitics:
        score += WRAP_CLITIC_PENALTY
    if not counts_characters and len(last_word) == 1 and last_word.isalpha():
        score += WRAP_ORPHAN_LETTER_PENALTY
    if next_word in enclitics:
        score += WRAP_CLITIC_PENALTY
    if WRAP_NUMBER_TAIL_PATTERN.search(line1) and WRAP_UNIT_HEAD_PATTERN.search(next_word):
        score += WRAP_NUMBER_UNIT_PENALTY
    score += wrap_short_line_penalty(line1, line2, counts_characters, terminal or rewards_space)
    balance = wrap_balance_factor(line1, line2)
    if terminal:
        reward = WRAP_TERMINAL_REWARD
    elif WRAP_CLAUSE_PATTERN.search(line1):
        reward = WRAP_CLAUSE_REWARD
    elif WRAP_MINOR_PATTERN.search(line1):
        reward = WRAP_MINOR_REWARD
    else:
        reward = WRAP_SPACE_BOUNDARY_REWARD if rewards_space else 0
    score -= reward * balance
    if next_word in conjunctions:
        score -= WRAP_CONJUNCTION_REWARD * balance
    return score


def split_into_two_lines(text, target_lang):
    visible, marks = separate_markup(text)
    tokens = wrap_tokens(visible, target_lang)
    if len(tokens) < 2:
        return None
    forbids, penalty_at = build_wrap_protection(visible, marks)
    proclitics, enclitics, conjunctions = wrap_orthography(target_lang)
    enforces_clitics = is_chinese_target(target_lang) or is_japanese_target(target_lang)
    options, offset, last_word = [], 0, ""
    for index in range(len(tokens) - 1):
        piece, is_word = tokens[index]
        offset += len(piece)
        if is_word:
            last_word = piece.lower()
        rest = visible[offset:]
        if WRAP_ATTACHED_CLOSING_PATTERN.search(rest) or forbids(offset):
            continue
        line1, line2 = visible[:offset].rstrip(), rest.lstrip()
        if not line1 or not line2 or line2[0] in WRAP_NO_LINE_START_CHARS or line1[-1] in WRAP_NO_LINE_END_CHARS:
            continue
        next_word = next((word.lower() for word, flag in tokens[index + 1:] if flag), "")
        space_boundary = bool(re.search(r"\s", visible[len(line1):len(visible) - len(line2)]))
        score = score_wrap_candidate(offset, len(visible), line1, line2, last_word, next_word, space_boundary, target_lang) + penalty_at(offset)
        splits_clitic = next_word in enclitics or last_word in proclitics or last_word in conjunctions
        options.append((offset, score, splits_clitic))
    if enforces_clitics:
        options = [option for option in options if not option[2]] or options
    if not options:
        return None
    best = min(options, key=lambda option: option[1])
    line_end = len(visible[:best[0]].rstrip())
    next_start = len(visible) - len(visible[best[0]:].lstrip())
    first, second = cut_marked(visible, marks, line_end, next_start)
    return f"{first}\n{second}"


DANGLING_CLOSING_PATTERN = re.compile(r"[ \t]*\n[ \t]*((?:</[ibu]>|\{[^}]*\})+)\s*$")
DANGLING_OPENING_PATTERN = re.compile(r"^((?:<[ibu]>|\{[^}]*\})+)[ \t]*\n[ \t]*")


def attach_dangling_markup(text):
    if "\n" not in text or ("<" not in text and "{" not in text):
        return text
    return DANGLING_OPENING_PATTERN.sub(r"\1", DANGLING_CLOSING_PATTERN.sub(r"\1", text))


def wrap_translation_lines(text, target_lang):
    text = attach_dangling_markup(text.strip())
    if not text or "\n" in text:
        return text
    if not evaluate_reading_speed(text, float("inf"), target_lang)["over_length"]:
        return text
    return split_into_two_lines(text, target_lang) or text


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


def merge(extract_data, translate_data, wrap_lines=False, punctuation_style=(False, False)):
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
    cues, approx_splits, quality_warnings = build_bilingual_cues(extract_data["cues"], extract_data["units"], translations, target_lang, source_lang, wrap_lines, punctuation_style)

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
    parser.add_argument("--wrap-lines", action="store_true",
                         help="wrap over-long translations into two balanced lines using the same rules as the web app")
    parser.add_argument("--full-width", action="store_true",
                         help="keep full-width punctuation and ellipses for Chinese and Japanese targets")
    parser.add_argument("--keep-terminators", action="store_true",
                         help="keep periods and commas for Chinese and Japanese targets")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    DEBUG_MODE = args.debug or os.environ.get("DEBUG") == "1"

    extract_data = json.load(open(args.extract, encoding="utf-8"))
    translate_data = json.load(open(args.translations, encoding="utf-8"))

    result = merge(extract_data, translate_data, args.wrap_lines, (args.full_width, args.keep_terminators))
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
