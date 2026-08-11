#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: bilingual_merge.py
# Version: 1.1.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Merges machine-translated JSON payload back with the original extracted
#     subtitle data to generate a bilingual SRT file. Handles text splitting
#     for merged translation units using punctuation, tokenization (jieba), or
#     fallback character ratio methods. Restores glossary placeholders to their
#     target terms.
#     将机器翻译返回的 JSON 数据与最初提取的字幕数据合并，生成双语 SRT 文件。
#     针对合并翻译的单元，使用标点、分词（jieba）或后备字符比例等方式进行文本拆分，
#     并将词表占位符还原为目标术语。
#
# Features:
#     - Reconstructs bilingual subtitle blocks maintaining original cue timing.
#     - Splits translation units back into segments using length constraints,
#       sentence boundaries, or jieba-based word tokenization.
#     - Restores protected glossary placeholders (e.g., ⟦G0000⟧).
#     - Automatically handles missing translations and logs approximation splits.
#
# 功能:
#     - 重构双语字幕块并保持原始时间轴。
#     - 利用长度限制、句尾标点或结巴分词（jieba）将翻译单元重新拆分为字幕段落。
#     - 还原受保护的专有名词占位符（如 ⟦G0000⟧）。
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
import re
import subprocess
import sys

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
        print(json.dumps({
            "success": False,
            "reason": "dependency_install_failed",
            "detail": str(e)
        }, ensure_ascii=False))
        sys.exit(0)

SCRIPT_NAME = "bilingual_merge"

SEGMENT_MARKER_PATTERN = re.compile(r"\u27e6S(\d{2})\u27e7")
GLOSSARY_PLACEHOLDER_PATTERN = re.compile(r"\s*(\u27e6G\d{4}\u27e7)\s*")
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


def restore_glossary(text, glossary_map):
    def substitute(match):
        return glossary_map.get(match.group(1), match.group(1))

    return GLOSSARY_PLACEHOLDER_PATTERN.sub(substitute, text)


def register_glossary_terms(glossary_map):
    if jieba is None:
        return
    for target_term in {term for term in glossary_map.values() if term}:
        jieba.add_word(target_term)


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
MARKER_LENGTH_TOLERANCE = 0.4


def resolve_cut(text, cursor, expected, boundary, max_cut):
    limit = len(text)
    ceiling = min(limit, max_cut)
    expected_share = max(expected - cursor, 1)
    if boundary:
        candidates = [m.end() for m in BOUNDARY_SEARCH_PATTERNS[boundary].finditer(text, cursor) if cursor < m.end() < ceiling]
        candidates = [c for c in candidates if abs(c - expected) <= 0.5 * expected_share]
        if candidates:
            return min(candidates, key=lambda pos: abs(pos - expected)), "original"
    inferred = [m.end() for m in GENERAL_PUNCT_SEARCH_PATTERN.finditer(text, cursor) if cursor < m.end() < ceiling]
    inferred += [m.start() for m in LEFT_CUT_PATTERN.finditer(text, cursor) if cursor < m.start() < ceiling]
    inferred = [c for c in inferred
                if abs(c - expected) <= INFERRED_PUNCT_TOLERANCE * expected_share
                and (c - cursor) >= INFERRED_MIN_SHARE * expected_share]
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


def strip_markers(text):
    return SEGMENT_MARKER_PATTERN.sub("", text)


def split_by_markers(translated_text, segment_count):
    matches = list(SEGMENT_MARKER_PATTERN.finditer(translated_text))
    if len(matches) != segment_count:
        return None
    indices = [int(m.group(1)) for m in matches]
    if sorted(indices) != list(range(segment_count)):
        return None
    parts = [None] * segment_count
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(translated_text)
        parts[indices[i]] = translated_text[match.end():end].strip()
    return parts if all(parts) else None


def split_by_boundary(translated_text, segments):
    boundary_types = [classify_boundary(seg["text"]) for seg in segments[:-1]]
    lengths = [effective_length(seg["text"]) for seg in segments]
    total = sum(lengths) or 1
    total_len = len(translated_text)
    segment_count = len(segments)
    cursor, cumulative, parts, tags = 0, 0, [], []
    for i, (length, boundary) in enumerate(zip(lengths[:-1], boundary_types)):
        cumulative += length
        expected = total_len * cumulative / total
        max_cut = total_len - (segment_count - 1 - i)
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


def repair_empty_parts(parts, segments):
    parts = list(parts)
    for i, part in enumerate(parts):
        if part:
            continue
        neighbor = i - 1 if i > 0 else i + 1
        if not (0 <= neighbor < len(parts)):
            continue
        lo, hi = sorted((i, neighbor))
        fixed, _ = split_by_boundary(parts[neighbor], segments[lo:hi + 1])
        parts[lo], parts[hi] = fixed
    return parts


def validate_marker_lengths(parts, segments):
    lengths = [effective_length(seg["text"]) for seg in segments]
    total = sum(lengths) or 1
    actual_total = sum(len(p) for p in parts) or 1
    for length, part in zip(lengths[1:], parts[1:]):
        expected_share = length / total
        if expected_share <= 0:
            continue
        actual_share = len(part) / actual_total
        if abs(actual_share - expected_share) / expected_share > MARKER_LENGTH_TOLERANCE:
            return False
    return True


def split_translation(translated_text, segments):
    if len(segments) == 1:
        return [strip_markers(translated_text)], "single"
    marker_parts = split_by_markers(translated_text, len(segments))
    if marker_parts and validate_marker_lengths(marker_parts, segments):
        parts, method = marker_parts, "marker"
    else:
        parts, method = split_by_boundary(strip_markers(translated_text), segments)
    parts = enforce_punctuation_placement(parts)
    return repair_empty_parts(parts, segments), method


BRACKET_CHAR_PATTERN = re.compile(r"[()（）\[\]【】{}]")
BRACKET_CONTENT_PATTERN = re.compile(r"[(（\[【{][^()（）\[\]【】{}]*[)）\]】}]")


def strip_unsourced_brackets(original_text, translated_text):
    if BRACKET_CHAR_PATTERN.search(original_text):
        return translated_text
    stripped = translated_text
    while BRACKET_CONTENT_PATTERN.search(stripped):
        stripped = BRACKET_CONTENT_PATTERN.sub("", stripped)
    return stripped


TRAILING_EXCLAIM_QUESTION_PATTERN = re.compile(r"[！？]+$")
FULLWIDTH_TO_ASCII = {"！": "!", "？": "?"}


def convert_trailing_marks(text):
    match = TRAILING_EXCLAIM_QUESTION_PATTERN.search(text)
    if not match:
        return text
    return text[:match.start()] + "".join(FULLWIDTH_TO_ASCII[c] for c in match.group())


def build_bilingual_cues(cues, units, translations, glossary_map):
    cue_segments = {}
    approx_splits = []
    for unit in units:
        translated = translations.get(str(unit["id"]))
        segments = unit["segments"]
        if translated is None:
            for seg in segments:
                cue_segments.setdefault(seg["cue_id"], {})[seg["seg_idx"]] = None
            continue
        translated = restore_glossary(translated, glossary_map)
        translated = strip_unsourced_brackets("".join(seg["text"] for seg in segments), translated)
        parts, method = split_translation(translated, segments)
        if method not in ("single", "marker", "original_boundary", "inferred_punctuation"):
            approx_splits.append({"unit_id": unit["id"], "method": method})
        for seg, part in zip(segments, parts):
            cue_segments.setdefault(seg["cue_id"], {})[seg["seg_idx"]] = normalize_chinese(part)

    results = []
    for cue in cues:
        seg_map = cue_segments.get(cue["id"], {})
        parts = [seg_map[i] for i in sorted(seg_map)] if seg_map else []
        if not parts or any(p is None for p in parts):
            translation = None
        elif len(parts) > 1:
            translation = " ".join(f"-{p}" for p in parts)
        else:
            translation = parts[0]
        if translation:
            translation = convert_trailing_marks(translation)
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
    if jieba is None:
        log("warning: jieba unavailable, splitting falls back to punctuation/char boundaries")
    translations = translate_data.get("translations", {})
    glossary_map = extract_data.get("glossary_map", {})
    register_glossary_terms(glossary_map)
    cues, approx_splits = build_bilingual_cues(extract_data["cues"], extract_data["units"], translations, glossary_map)
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
