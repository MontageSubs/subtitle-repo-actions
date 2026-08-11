#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: srt_extract.py
# Version: 1.1.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/translation/mt/
#
# Description / 描述:
#     Extracts subtitle cues from SRT files, splits/merges dialogue segments
#     based on sentence boundaries, timing gaps, quotes, and stutter patterns,
#     and protects glossary terms with placeholders to build translation units.
#     从 SRT 字幕文件中提取字幕块，结合句尾标点、时间间隔（GAP）、引号与
#     口吃/残留规则进行对话拆分与单元合并，并通过占位符保护专有名词词表，
#     输出供后续机器翻译脚本使用的结构化 JSON 数据。
#
# Features:
#     - Parses SRT subtitle structures and normalizes text and timestamps.
#     - Splits multi-speaker dialogue lines marked with leading dashes ('- ').
#     - Resolves name stutters and matches terminology against provided glossary.
#     - Groups subtitle cues based on punctuation, pause gaps, and quote continuity.
#     - Protects glossary terms using standardized placeholders (e.g., ⟦G0000⟧).
#
# 功能:
#     - 解析 SRT 字幕结构，标准化时间轴与文本格式。
#     - 识别并拆分双人对话破折号（'- '），处理字母口吃与词表名称修复。
#     - 基于标点、时间间隔（GAP_THRESHOLD_MS）与跨行引号逻辑切分/合并翻译单元。
#     - 解析 Markdown 格式词表，生成双向占位符映射（如 ⟦G0000⟧）保护专有名词。
#     - 标准化输出 JSON 数据，包含原字幕 Cue、翻译 Unit 及词表映射信息。
#     - 默认剥离 SDH（听障辅助）内容，纯 SDH 行整行丢弃，行内 SDH 仅剥离对应片段
#       （--keep-sdh 可关闭）；含音符的行豁免于 SDH 剥离之外。
#     - 音乐歌词行按大小写判断是否跨 cue 续接合并，合并组发送翻译前剥离首尾音符。
#
# Usage / 用法:
#     python srt_extract.py --input en.srt --glossary GLOSSARY.md --output extract.json
#
# Output / 输出:
#     Diagnostic logs (stderr) / 诊断日志（标准错误）:
#       - Status, cue count, unit count / 执行状态、解析字幕行数、生成单元总数
#
#     Result data (stdout) / 结果数据（标准输出）:
#       - A single JSON object / 单个 JSON 对象
#
# Exit codes / 退出码:
#     0    normal completion / 正常完成
#     130  interrupted by Ctrl+C / 被 Ctrl+C 中断
# ============================================================================
import argparse
import json
import re
import sys

SCRIPT_NAME = "srt_extract"

TIME_LINE_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")
TAG_PATTERN = re.compile(r"<[^>]+>|\{[^}]*\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
TERMINAL_PUNCT_PATTERN = re.compile(r"[.!?…”’\"')\]]\s*$")
TRAILING_CONTINUATION_PATTERN = re.compile(r"(\.{2,}|-{2,}|…)\s*$")
LOWERCASE_START_PATTERN = re.compile(r"^[a-z]")
DIALOGUE_DASH_PATTERN = re.compile(r"(?:^|(?<=\s))-(?!-)\s?")
STUTTER_WORD_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-\1(?![A-Za-z])", re.IGNORECASE)
STUTTER_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-(?=\1[a-z])", re.IGNORECASE)
SHORT_REPLY_LETTER_PATTERN = re.compile(r"[A-Za-z]")
SHORT_REPLY_MAX_LETTERS = 3
STUTTER_RESIDUAL_PATTERN = re.compile(r"[A-Za-z]")
TRAILING_MARK_PATTERN = re.compile(r"[!?…]+$")
PLACEHOLDER_TEMPLATE = "\u27e6G{:04d}\u27e7"
SEGMENT_MARKER_TEMPLATE = "\u27e6S{:02d}\u27e7"
GAP_THRESHOLD_MS = 200

MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
SDH_BRACKET_PATTERN = re.compile(r"[\[(\uff08][^\[\]()\uff08\uff09]*[\])\uff09]|[{\uff5b][^{}\uff5b\uff5d]*[}\uff5d]")
LEADING_ELLIPSIS_PATTERN = re.compile(r"^(\.{2,}|\u2026)")
LEADING_NON_LETTER_PATTERN = re.compile(r"^[^A-Za-z]*")
EDGE_NOTE_PATTERN = re.compile(f"^[{MUSIC_NOTE_CHARS}\\s]+|[{MUSIC_NOTE_CHARS}\\s]+$")

GLOSSARY_HEADING = "人物与专有名词"
SECTION_END_PATTERN = re.compile(r"^##\s", re.MULTILINE)
TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|\s*$")
SEPARATOR_ROW_PATTERN = re.compile(r"^[\s|:-]+$")
NAME_SEPARATOR_PATTERN = re.compile(r"[·・]")
TERM_BOUNDARY_LEFT = r"(?<![A-Za-z0-9])"
TERM_BOUNDARY_RIGHT = r"(?![A-Za-z0-9])"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def time_to_ms(value):
    hh, mm, rest = value.replace(".", ",").split(":", 2)
    ss, ms = rest.split(",")
    return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms)


def fold_text(raw):
    lines = [WHITESPACE_PATTERN.sub(" ", TAG_PATTERN.sub("", line)).strip() for line in raw.splitlines()]
    return " ".join(line for line in lines if line)


def strip_letter_stutter(text):
    text = STUTTER_WORD_PATTERN.sub(lambda m: m.group(1), text)
    return STUTTER_PREFIX_PATTERN.sub("", text)


def strip_sdh(text):
    if MUSIC_NOTE_PATTERN.search(text):
        return text
    return WHITESPACE_PATTERN.sub(" ", SDH_BRACKET_PATTERN.sub("", text)).strip()


def is_music_segment(text):
    return bool(MUSIC_NOTE_PATTERN.search(text))


def music_continuation(text):
    remainder = MUSIC_NOTE_PATTERN.sub("", text, count=1).strip()
    if LEADING_ELLIPSIS_PATTERN.match(remainder):
        return False
    letter_start = LEADING_NON_LETTER_PATTERN.match(remainder).end()
    if letter_start >= len(remainder):
        return False
    return remainder[letter_start].islower()


def strip_edge_notes(text):
    return EDGE_NOTE_PATTERN.sub("", text)


def parse_srt(content, strip_sdh_enabled=True):
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    sdh_stats = {"dropped": 0, "stripped": 0}
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        time_line_idx = None
        for idx in (0, 1):
            if idx < len(lines) and TIME_LINE_PATTERN.match(lines[idx].strip()):
                time_line_idx = idx
                break
        if time_line_idx is None:
            continue
        time_match = TIME_LINE_PATTERN.match(lines[time_line_idx].strip())
        cue_id = int(lines[0].strip()) if time_line_idx == 1 and lines[0].strip().isdigit() else len(cues) + 1
        text = fold_text("\n".join(lines[time_line_idx + 1:]))
        if strip_sdh_enabled:
            cleaned = strip_sdh(text)
            if cleaned != text:
                sdh_stats["dropped" if not cleaned else "stripped"] += 1
            text = cleaned
        if text:
            cues.append({"id": cue_id, "start": time_match.group(1), "end": time_match.group(2), "text": text})
    return cues, sdh_stats


def split_dialogue(text):
    matches = list(DIALOGUE_DASH_PATTERN.finditer(text))
    if not matches:
        return [text]
    segments = []
    if matches[0].start() > 0:
        segments.append(text[:matches[0].start()].strip())
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segments.append(text[match.end():end].strip())
    segments = [s for s in segments if s]
    return segments if segments else [text]


def has_terminal_punct(text):
    if TRAILING_CONTINUATION_PATTERN.search(text):
        return False
    return bool(TERMINAL_PUNCT_PATTERN.search(text))


def is_short_reply(text):
    return len(SHORT_REPLY_LETTER_PATTERN.findall(text)) <= SHORT_REPLY_MAX_LETTERS


QUOTE_CHARS = frozenset('"“”')


def starts_with_quote(text):
    return bool(text) and text[0] in QUOTE_CHARS


def ends_with_quote(text):
    return bool(text) and text[-1] in QUOTE_CHARS


def should_merge(prev_seg, curr_seg):
    if prev_seg["cue_id"] == curr_seg["cue_id"]:
        return is_short_reply(curr_seg["text"])
    if is_music_segment(prev_seg["text"]) and is_music_segment(curr_seg["text"]):
        return music_continuation(curr_seg["text"])
    if has_terminal_punct(prev_seg["text"]):
        return False
    gap = time_to_ms(curr_seg["start"]) - time_to_ms(prev_seg["end"])
    return gap <= GAP_THRESHOLD_MS or bool(LOWERCASE_START_PATTERN.match(curr_seg["text"]))


def find_stutter_resolution(text, glossary):
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        match = pattern.search(text)
        if not match:
            continue
        residual = STUTTER_RESIDUAL_PATTERN.findall(text[:match.start()] + text[match.end():])
        name_length = len(STUTTER_RESIDUAL_PATTERN.findall(source_term))
        if 0 < len(residual) < name_length:
            trailing = TRAILING_MARK_PATTERN.search(text)
            suffix = trailing.group().replace("?", "？").replace("!", "！") if trailing else ""
            return target_term + suffix
    return None


def find_pure_glossary_line(text, glossary):
    stripped, matched_any = text, False
    for source_term in sorted(glossary, key=len, reverse=True):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        if pattern.search(stripped):
            matched_any = True
        stripped = pattern.sub("", stripped)
    if not matched_any or STUTTER_RESIDUAL_PATTERN.search(stripped):
        return None
    resolved = text
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        resolved = pattern.sub(target_term, resolved)
    return resolved


def build_segments(cues, glossary):
    segments = []
    for cue in cues:
        parts = split_dialogue(cue["text"])
        dialogue = len(parts) > 1
        for seg_idx, part in enumerate(parts):
            resolved = find_pure_glossary_line(part, glossary) or find_stutter_resolution(part, glossary)
            text = part if resolved else strip_letter_stutter(part)
            segments.append({
                "cue_id": cue["id"], "seg_idx": seg_idx, "dialogue": dialogue,
                "text": text, "start": cue["start"], "end": cue["end"], "resolved": resolved,
            })
    return segments


QUOTE_PENDING_LIMIT = 10


def group_segments(segments):
    groups = []
    current = []
    quote_pending = False
    quote_span = 0
    for seg in segments:
        if seg["resolved"]:
            if current:
                groups.append(current)
                current = []
            groups.append([seg])
            quote_pending = False
            quote_span = 0
            continue
        if current and (quote_pending or should_merge(current[-1], seg)):
            current.append(seg)
        else:
            if current:
                groups.append(current)
            current = [seg]
        if quote_pending:
            quote_span += 1
            quote_pending = not ends_with_quote(seg["text"]) and quote_span < QUOTE_PENDING_LIMIT
        else:
            quote_pending = starts_with_quote(seg["text"]) and not ends_with_quote(seg["text"])
            quote_span = 1 if quote_pending else 0
    if current:
        groups.append(current)
    return groups


def extract_markdown_section(content, heading):
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", content, re.MULTILINE)
    if not match:
        return ""
    next_match = SECTION_END_PATTERN.search(content, match.end())
    return content[match.end():next_match.start() if next_match else len(content)]


def parse_glossary_table(section_text):
    rows = []
    for line in section_text.splitlines():
        row_match = TABLE_ROW_PATTERN.match(line.strip())
        if not row_match or SEPARATOR_ROW_PATTERN.match(row_match.group(1)):
            continue
        cells = [c.strip() for c in row_match.group(1).split("|")]
        if len(cells) >= 2 and cells[0] not in ("原文",):
            rows.append((cells[0], cells[1]))
    return rows


def split_name_pair(original, translated):
    orig_tokens = original.split()
    trans_tokens = [t for t in NAME_SEPARATOR_PATTERN.split(translated) if t]
    pairs = [(original, translated)]
    if len(orig_tokens) >= 2 and len(orig_tokens) == len(trans_tokens):
        pairs.append((orig_tokens[0], trans_tokens[0]))
    return pairs


def build_glossary_from_markdown(content):
    glossary = {}
    for original, translated in parse_glossary_table(extract_markdown_section(content, GLOSSARY_HEADING)):
        for term, target in split_name_pair(original, translated):
            if term and term not in glossary:
                glossary[term] = target
    return glossary


def protect_terms(text, glossary, counter):
    mapping = {}
    if not glossary:
        return text, mapping, counter
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + r"(-\d+)?" + TERM_BOUNDARY_RIGHT)

        def substitute(match):
            nonlocal counter
            placeholder = PLACEHOLDER_TEMPLATE.format(counter)
            mapping[placeholder] = target_term + (match.group(1) or "")
            counter += 1
            return placeholder

        text = pattern.sub(substitute, text)
    return text, mapping, counter


def build_units(cues, glossary):
    units = []
    glossary_map = {}
    counter = 0
    for group in group_segments(build_segments(cues, glossary)):
        segments = [{"cue_id": s["cue_id"], "seg_idx": s["seg_idx"], "dialogue": s["dialogue"], "text": s["text"]} for s in group]
        if len(group) == 1 and group[0]["resolved"]:
            units.append({"id": len(units), "segments": segments, "text": "", "resolved": group[0]["resolved"]})
            continue
        is_music_group = len(group) > 1 and any(is_music_segment(seg["text"]) for seg in group)
        marked_text = "".join(
            f"{SEGMENT_MARKER_TEMPLATE.format(i)}{strip_edge_notes(seg['text']) if is_music_group else seg['text']} "
            for i, seg in enumerate(group)
        ).strip()
        protected_text, mapping, counter = protect_terms(marked_text, glossary, counter)
        glossary_map.update(mapping)
        units.append({"id": len(units), "segments": segments, "text": protected_text, "resolved": None})
    return units, glossary_map


def extract(content, glossary, strip_sdh_enabled=True):
    cues, sdh_stats = parse_srt(content, strip_sdh_enabled)
    if not cues:
        return {"success": False, "reason": "no_cues_parsed", "cues": [], "units": [], "glossary_map": {}, "sdh_removed": sdh_stats}
    units, glossary_map = build_units(cues, glossary)
    return {"success": True, "cues": cues, "units": units, "glossary_map": glossary_map, "sdh_removed": sdh_stats}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--glossary", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--keep-sdh", action="store_true")
    args = parser.parse_args()

    raw = open(args.input, encoding="utf-8-sig").read() if args.input else sys.stdin.read()
    glossary = build_glossary_from_markdown(open(args.glossary, encoding="utf-8").read()) if args.glossary else {}

    result = extract(raw, glossary, strip_sdh_enabled=not args.keep_sdh)
    sdh_note = ""
    if not args.keep_sdh:
        stats = result["sdh_removed"]
        sdh_note = f", sdh_dropped={stats['dropped']}, sdh_stripped={stats['stripped']}"
    log(f"status: {'ok' if result['success'] else 'failed'} (cues={len(result['cues'])}, units={len(result['units'])}{sdh_note})")

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
