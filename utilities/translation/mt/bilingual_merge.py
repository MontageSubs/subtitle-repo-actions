#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: bilingual_merge.py
# Version: 1.3.0
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
#     `spans` list each unit carries (no marker syntax involved anywhere).
#     将机器翻译返回的 JSON 数据与最初提取的字幕数据合并，生成双语 SRT 文件。
#     依据每个翻译单元自带的 `spans` 列表（原始片段文本+时间轴），用标点/
#     长度比例边界估计（可用时以结巴分词辅助定位）拆分合并翻译回各原始
#     字幕片段，全程不涉及任何标记符号。
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
import re
import subprocess
import sys

try:
    import jieba
    jieba.setLogLevel(logging.ERROR)
except ImportError:
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "jieba"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60
        )
        import jieba
        jieba.setLogLevel(logging.ERROR)
    except Exception as e:
        print(json.dumps({
            "success": False,
            "reason": "dependency_install_failed",
            "detail": str(e)
        }, ensure_ascii=False))
        sys.exit(0)

SCRIPT_NAME = "bilingual_merge"

ELLIPSIS_PATTERN = re.compile(r"\.{2,}|…+")
DASH_ARTIFACT_PATTERN = re.compile(r"—+|-{2,}")
CJK_TERMINATOR_PATTERN = re.compile(r"[。，、]")
HALFWIDTH_TERMINATOR_PATTERN = re.compile(r"(?<![\d.])[.,](?![\d.])")
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


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def collect_glossary_terms(units):
    return {m["target"] for unit in units for m in (unit.get("term_matches") or []) if m.get("target")}


def register_glossary_terms(terms):
    if jieba is None:
        return
    for term in terms:
        jieba.add_word(term)


def enforce_line_edges(text):
    while text and text[0] in NO_LINE_START_CHARS:
        text = text[1:].lstrip()
    while text and text[-1] in NO_LINE_END_CHARS:
        text = text[:-1].rstrip()
    return text


WORD_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)


def strip_terminator(match):
    return " " if WORD_CHAR_PATTERN.search(match.string, match.end()) else ""


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
    text = fix_music_spacing(text)
    text = WHITESPACE_COLLAPSE_PATTERN.sub(" ", text).strip()
    if not MUSIC_NOTE_PATTERN.match(text):
        text = fix_music_spacing(f"\u266a{text}").strip()
    return text


def normalize_chinese(text):
    text = ELLIPSIS_PATTERN.sub("...", text)
    text = DASH_ARTIFACT_PATTERN.sub("...", text)
    text = CJK_TERMINATOR_PATTERN.sub(strip_terminator, text)
    text = HALFWIDTH_TERMINATOR_PATTERN.sub(strip_terminator, text)
    text = fix_music_spacing(text)
    text = WHITESPACE_COLLAPSE_PATTERN.sub(" ", text).strip()
    return enforce_line_edges(text)


def effective_length(text):
    return len(NON_WORD_PATTERN.sub("", text)) or len(text)


FALLBACK_BOUNDARY_PATTERN = re.compile(r"[，,、；;。.!?…\s]+")


def word_boundaries(text):
    if jieba is not None:
        boundaries = [0]
        for word in jieba.cut(text):
            boundaries.append(boundaries[-1] + len(word))
    else:
        boundaries = {0, len(text)}
        boundaries.update(m.end() for m in FALLBACK_BOUNDARY_PATTERN.finditer(text))
        boundaries = sorted(boundaries)
    return [b for b in boundaries if b in (0, len(text)) or (text[b - 1] != "·" and text[b] != "·")]


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
ORIGINAL_PUNCT_TOLERANCE = 0.20
INFERRED_PUNCT_TOLERANCE = 0.15
INFERRED_MIN_SHARE = 0.5


def resolve_cut(text, cursor, expected, boundary, max_cut):
    limit = len(text)
    ceiling = min(limit, max_cut)
    if boundary:
        candidates = [m.end() for m in BOUNDARY_SEARCH_PATTERNS[boundary].finditer(text, cursor) if cursor < m.end() < ceiling]
        if candidates:
            return min(candidates, key=lambda pos: abs(pos - expected)), "original"
    inferred = [m.end() for m in GENERAL_PUNCT_SEARCH_PATTERN.finditer(text, cursor) if cursor < m.end() < ceiling]
    inferred += [m.start() for m in LEFT_CUT_PATTERN.finditer(text, cursor) if cursor < m.start() < ceiling]
    if inferred:
        return min(inferred, key=lambda pos: abs(pos - expected)), "inferred"
    boundaries = [b for b in (bd + cursor for bd in word_boundaries(text[cursor:])) if cursor < b < ceiling]
    if boundaries:
        return nearest_boundary(boundaries, expected), None
    return max(cursor + 1, min(round(expected), ceiling - 1)), None


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


def split_by_boundary(translated_text, spans):
    boundary_types = [classify_boundary(span["text"]) for span in spans[:-1]]
    lengths = [effective_length(span["text"]) for span in spans]
    total = sum(lengths) or 1
    total_len = len(translated_text)
    span_count = len(spans)
    cursor, cumulative, parts, tags = 0, 0, [], []
    for i, (length, boundary) in enumerate(zip(lengths[:-1], boundary_types)):
        cumulative += length
        expected = total_len * cumulative / total
        max_cut = total_len - (span_count - 1 - i)
        cut, tag = resolve_cut(translated_text, cursor, expected, boundary, max_cut)
        tags.append(tag)
        parts.append(translated_text[cursor:cut].strip())
        cursor = cut
    parts.append(translated_text[cursor:].strip())
    if "original" in tags:
        method = "original_boundary"
    elif "inferred" in tags:
        method = "inferred_punctuation"
    else:
        method = "word_boundary" if jieba is not None else "char_ratio"
    return parts, method


def repair_empty_parts(parts, spans):
    parts = list(parts)
    for i, part in enumerate(parts):
        if part:
            continue
        neighbor = i - 1 if i > 0 else i + 1
        if not (0 <= neighbor < len(parts)):
            continue
        lo, hi = sorted((i, neighbor))
        fixed, _ = split_by_boundary(parts[neighbor], spans[lo:hi + 1])
        parts[lo], parts[hi] = fixed
    return parts


def split_translation(translated_text, spans):
    if len(spans) == 1:
        return [translated_text.strip()], "single"
    parts, method = split_by_boundary(translated_text, spans)
    parts = enforce_punctuation_placement(parts)
    return repair_empty_parts(parts, spans), method


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


def build_bilingual_cues(cues, units, translations):
    cue_segments = {}
    approx_splits = []
    for unit in units:
        spans = unit["spans"]
        translated = translations.get(str(unit["id"]))
        if translated is None:
            for span in spans:
                cue_segments.setdefault(span["id"], []).append(None)
            continue
        translated = strip_unsourced_brackets("".join(span["text"] for span in spans), translated)
        parts, method = split_translation(translated, spans)
        if method not in ("single", "original_boundary", "inferred_punctuation"):
            approx_splits.append({"unit_id": unit["id"], "method": method})
        for span, part in zip(spans, parts):
            cue_segments.setdefault(span["id"], []).append(normalize_chinese(part))

    results = []
    for cue in cues:
        parts = cue_segments.get(cue["id"])
        if not parts or any(p is None for p in parts):
            translation = None
        elif len(parts) > 1:
            translation = " ".join(f"-{p}" for p in parts)
        else:
            translation = parts[0]
        if translation:
            translation = normalize_exclaim_question(translation)
            if source_is_music(cue["text"]):
                translation = POSITION_TOP_TAG + format_music_line(translation)
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
    if jieba is not None:
        log(f"jieba: available (v{getattr(jieba, '__version__', 'unknown')})")
    else:
        log("jieba: unavailable, splitting falls back to punctuation/char boundaries")
    translations = translate_data.get("translations", {})
    register_glossary_terms(collect_glossary_terms(extract_data["units"]))
    cues, approx_splits = build_bilingual_cues(extract_data["cues"], extract_data["units"], translations)
    missing = sum(1 for cue in cues if cue.get("translation") is None)
    return {"success": True, "srt": render_srt(cues), "approx_splits": approx_splits, "missing_count": missing}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

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
