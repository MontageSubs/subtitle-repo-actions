#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: srt_extract.py
# Version: 1.2.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/translation/mt/
#
# Description / 描述:
#     Extracts subtitle cues from SRT files, splits/merges dialogue segments
#     based on sentence boundaries, timing gaps, quotes, and stutter patterns,
#     and annotates glossary term positions to build translation units.
#     从 SRT 字幕文件中提取字幕块，结合句尾标点、时间间隔（GAP）、引号与
#     口吃/残留规则进行对话拆分与单元合并，并标注词表命中位置，
#     输出供后续机器翻译脚本使用的结构化 JSON 数据。
#
# Features:
#     - Parses SRT subtitle structures and normalizes text and timestamps.
#     - Splits multi-speaker dialogue lines marked with leading dashes ('- ').
#     - Resolves name stutters and matches terminology against provided glossary.
#     - Groups subtitle cues based on punctuation, pause gaps, and quote continuity.
#     - Units carry natural, unmarked merged text plus a `spans` list (original
#       per-piece text/timing) so downstream can reconstruct without markers.
#     - Annotates glossary hits as `term_matches` (position + source + target)
#       and an `embed_ratio`; actual placeholder-vs-inline-name decision is
#       deferred to the translation step, which owns batch/context context.
#
# 功能:
#     - 解析 SRT 字幕结构，标准化时间轴与文本格式。
#     - 识别并拆分双人对话破折号（'- '），处理字母口吃与词表名称修复。
#     - 基于标点、时间间隔（GAP_THRESHOLD_MS）与跨行引号逻辑切分/合并翻译单元。
#     - 单元文本为自然连续原文，不嵌入任何标记符号；随附 `spans`（被吸收的
#       原始片段及各自时间轴/原文）供下游拆分回填。
#     - 标注词表命中的位置、原文与固定译名（`term_matches`）及嵌入比例
#       （`embed_ratio`），是否嵌入原文或改用占位符由翻译脚本决定。
#     - 默认剥离 SDH（听障辅助）内容：整行 SDH 括号内容丢弃、行内 SDH
#       仅剥离对应片段，逐行识别"说话人标签+冒号"前缀（如 MAN:/两人：）
#       并剥离（--keep-sdh 可关闭）；含音符的行豁免于 SDH 剥离之外。
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
GAP_THRESHOLD_MS = 200

MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
SDH_BRACKET_PATTERN = re.compile(r"[\[(\uff08][^\[\]()\uff08\uff09]*[\])\uff09]|[{\uff5b][^{}\uff5b\uff5d]*[}\uff5d]")
LEADING_ELLIPSIS_PATTERN = re.compile(r"^(\.{2,}|\u2026)")
LEADING_NON_LETTER_PATTERN = re.compile(r"^[^A-Za-z]*")
EDGE_NOTE_PATTERN = re.compile(f"^[{MUSIC_NOTE_CHARS}\\s]+|[{MUSIC_NOTE_CHARS}\\s]+$")

SPEAKER_TAG_MAX_CHARS = 24
SPEAKER_TAG_PATTERN = re.compile(rf"^([^:\uff1a]{{1,{SPEAKER_TAG_MAX_CHARS}}})[:\uff1a]\s*(\S.*)$")
UPPERCASE_LETTER_PATTERN = re.compile(r"[A-Z]")
LOWERCASE_LETTER_PATTERN = re.compile(r"[a-z]")
SPEAKER_TAG_LABELS = frozenset({
    "both", "all", "man", "woman", "men", "women", "voice", "voiceover",
    "crowd", "narrator", "group",
    "两人", "众人", "全体", "男声", "女声", "众声", "画外音", "旁白", "二人", "三人", "齐声", "合",
})

GLOSSARY_HEADING = "人物与专有名词"
SECTION_END_PATTERN = re.compile(r"^##\s", re.MULTILINE)
TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|\s*$")
SEPARATOR_ROW_PATTERN = re.compile(r"^[\s|:-]+$")
NAME_SEPARATOR_PATTERN = re.compile(r"[·・]")
TERM_BOUNDARY_LEFT = r"(?<![A-Za-z0-9])"
TERM_BOUNDARY_RIGHT = r"(?![A-Za-z0-9])"
EMBED_RATIO_DEFAULT = 0.0


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def time_to_ms(value):
    hh, mm, rest = value.replace(".", ",").split(":", 2)
    ss, ms = rest.split(",")
    return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms)


def strip_letter_stutter(text):
    text = STUTTER_WORD_PATTERN.sub(lambda m: m.group(1), text)
    return STUTTER_PREFIX_PATTERN.sub("", text)


def is_speaker_tag(tag):
    tag = tag.strip()
    if not tag:
        return False
    if UPPERCASE_LETTER_PATTERN.search(tag) and not LOWERCASE_LETTER_PATTERN.search(tag):
        return True
    return tag.lower() in SPEAKER_TAG_LABELS


def strip_speaker_tag(line):
    match = SPEAKER_TAG_PATTERN.match(line)
    if match and is_speaker_tag(match.group(1)):
        return match.group(2)
    return line


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


def fold_text(raw, strip_sdh_enabled=False):
    lines = []
    for raw_line in raw.splitlines():
        line = WHITESPACE_PATTERN.sub(" ", TAG_PATTERN.sub("", raw_line)).strip()
        if strip_sdh_enabled and line and not MUSIC_NOTE_PATTERN.search(line):
            line = strip_speaker_tag(line).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


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
        text = fold_text("\n".join(lines[time_line_idx + 1:]), strip_sdh_enabled)
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
        for part in split_dialogue(cue["text"]):
            resolved = find_pure_glossary_line(part, glossary) or find_stutter_resolution(part, glossary)
            text = part if resolved else strip_letter_stutter(part)
            segments.append({"cue_id": cue["id"], "text": text, "start": cue["start"], "end": cue["end"], "resolved": resolved})
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


def match_glossary_terms(text, glossary):
    matches, claimed = [], []
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        for m in pattern.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in claimed):
                continue
            claimed.append((m.start(), m.end()))
            matches.append({"start": m.start(), "end": m.end(), "source": source_term, "target": target_term})
    matches.sort(key=lambda m: m["start"])
    if not matches or not text:
        return matches, EMBED_RATIO_DEFAULT
    embedded_len = len(text) - sum(m["end"] - m["start"] for m in matches) + sum(len(m["target"]) for m in matches)
    return matches, embedded_len / len(text)


def build_units(cues, glossary):
    units = []
    for unit_id, group in enumerate(group_segments(build_segments(cues, glossary)), start=1):
        spans = [{"id": s["cue_id"], "start": s["start"], "end": s["end"], "text": s["text"]} for s in group]
        if len(group) == 1 and group[0]["resolved"]:
            units.append({"id": unit_id, "spans": spans, "text": "", "term_matches": [], "embed_ratio": EMBED_RATIO_DEFAULT, "resolved": group[0]["resolved"]})
            continue
        is_music_group = len(group) > 1 and any(is_music_segment(seg["text"]) for seg in group)
        text = " ".join(strip_edge_notes(seg["text"]) if is_music_group else seg["text"] for seg in group).strip()
        term_matches, embed_ratio = match_glossary_terms(text, glossary)
        units.append({"id": unit_id, "spans": spans, "text": text, "term_matches": term_matches, "embed_ratio": embed_ratio, "resolved": None})
    return units


def extract(content, glossary, strip_sdh_enabled=True):
    cues, sdh_stats = parse_srt(content, strip_sdh_enabled)
    if not cues:
        return {"success": False, "reason": "no_cues_parsed", "cues": [], "units": [], "sdh_removed": sdh_stats}
    units = build_units(cues, glossary)
    return {"success": True, "cues": cues, "units": units, "sdh_removed": sdh_stats}


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
