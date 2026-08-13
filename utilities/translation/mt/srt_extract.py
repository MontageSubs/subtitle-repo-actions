#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: srt_extract.py
# Version: 2.1
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
#     v1.7: Isolated short cues (<=ISOLATED_MAX_WORDS words, e.g. "Shit.")
#     no longer stay stranded without context. They merge cross-cue toward
#     whichever neighbor falls within SCENE_ADJACENCY_MS (next preferred,
#     falling back to prev), bypassing the terminal-punctuation guard that
#     normally blocks merging. The resulting join is marked with an inline
#     ⟦cNNNN⟧ token (also recorded as span["boundary"]="marker") so the
#     paired bilingual_merge.py can split on this hard boundary instead of
#     guessing from punctuation/length ratio alone.
#     v1.7: 孤立短句（≤ISOLATED_MAX_WORDS个单词，如"Shit."）不再因缺乏上下文
#     而独立成句。会跨cue向阈值内最近的邻居合并（优先向后，其次向前），
#     绕开原本阻止合并的句尾标点检查。合并接缝会插入 ⟦cNNNN⟧ 行内标记
#     （同时记录为 span["boundary"]="marker"），供配套的 bilingual_merge.py
#     按此硬边界拆分，而非仅依赖标点/长度比例猜测。
#
#     v1.8: Units are now grouped into `chapters`, each a run of units of the
#     same kind (dialogue/music) with no gap exceeding SCENE_CHANGE_MS between
#     them. Music and dialogue are tracked as two independent timelines, so
#     interleaved music cues (e.g. a song threaded between dialogue lines)
#     collapse into their own chapter instead of being scattered across the
#     surrounding dialogue chapters, while opening/closing songs separated by
#     a real scene gap still land in separate chapters.
#     v1.8: 单元现在按 `chapters` 分组，每个章节是一段同类型（对话/音乐）且
#     彼此间隔不超过 SCENE_CHANGE_MS 的连续单元。音乐与对话各自维护独立时间线，
#     因此穿插在对话中的音乐（如对话间隔中的歌曲）会被归入同一个独立章节，
#     而非散落在前后对话章节里；片头曲与片尾曲之间若确有场景间隔，仍会分属
#     不同章节。
#
#     v1.9: cue id 不再取自源 SRT 文件自身编号（该编号在 SDH 整行被剥离后
#     不再连续可靠），统一改为内部严格递增。音乐章节（同一首歌/连续歌词的
#     整个 chapter）不再按续接规则拆成多个各自独立发送的 unit——那样跨
#     unit 边界发送时（各自一个 `<span>`）翻译引擎仍可能在响应里串位——
#     而是整章合并为一个 unit，每句歌词前都带 ⟦cNNNN⟧，只用一个 `<span>`
#     发送。下游按 cue id 精确回填，即便引擎打乱了歌词行序也能正确归位。
#     v1.9: Cue ids no longer come from the source SRT's own numbering
#     (unreliable once SDH-only lines are dropped entirely); now strictly
#     sequential internally. A music chapter (one song/lyric run) is no
#     longer split into several continuation-grouped units each sent as its
#     own `<span>` — the engine could still scramble content across those
#     span boundaries in its response. It's now merged into a single unit
#     for the whole chapter, every line prefixed with ⟦cNNNN⟧, sent as one
#     `<span>`. Downstream relocates content by cue id, correct even if the
#     engine reorders song lines.
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
#     - Groups units into `chapters` (scene/song-level runs) so downstream
#       batching can send whole scenes as translation context instead of
#       splitting arbitrarily by character count.
#
# 功能:
#     - 解析 SRT 字幕结构，标准化时间轴与文本格式。
#     - 识别并拆分双人对话破折号（'- '），处理字母口吃与词表名称修复。
#     - 基于标点、时间间隔（GAP_THRESHOLD_MS）与跨行引号逻辑切分/合并翻译单元。
#     - 单元文本为自然连续原文，不嵌入任何标记符号；随附 `spans`（被吸收的
#       原始片段及各自时间轴/原文）供下游拆分回填。
#     - 标注词表命中的位置、原文与固定译名（`term_matches`）及嵌入比例
#       （`embed_ratio`），是否嵌入原文或改用占位符由翻译脚本决定。
#     - 将单元归入 `chapters`（场景/歌曲级片段），供下游按整场景批量发送
#       翻译上下文，而非仅按字符数任意切分。
#     - 默认剥离 SDH（听障辅助）内容：整行 SDH 括号内容丢弃；说话人标签
#       （如 "JOHN: text"）剥离逻辑严格取材于 Subtitle Edit 的
#       RemoveTextForHI.cs（RemoveColon，OnlyUppercase=True 默认档位）：
#       仅识别半角冒号，前缀必须整体全大写才剥离，并保留原版的括号内冒号
#       豁免、数字间冒号豁免、叙述性前缀豁免（ShouldRemoveNarrator）、
#       两行 cue 中首行未终止标点时的续行豁免。全角冒号（如中文"："）与
#       非全大写前缀（如 "Both:"）按原版行为一律不剥离——这是源字幕格式
#       不规范，不在本工具修复范围内。（--keep-sdh 可关闭整个 SDH 剥离；
#       含音符的 cue 整体豁免于说话人标签剥离之外。）
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
TERMINAL_PUNCT_PATTERN = re.compile(r"[.!?。！？][’”\"')\]」』】）]*\s*$")
TRAILING_CONTINUATION_PATTERN = re.compile(r"(\.{2,}|-{2,}|…)\s*$")
DIALOGUE_DASH_PATTERN = re.compile(r"(?:^|(?<=\s))-(?!-)\s?")
STUTTER_WORD_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-\1(?![A-Za-z])", re.IGNORECASE)
STUTTER_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-(?=\1[a-z])", re.IGNORECASE)
SHORT_REPLY_LETTER_PATTERN = re.compile(r"[A-Za-z]")
SHORT_REPLY_MAX_LETTERS = 3
STUTTER_RESIDUAL_PATTERN = re.compile(r"[A-Za-z]")
TRAILING_MARK_PATTERN = re.compile(r"[!?…]+$")
GAP_THRESHOLD_MS = 200
WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
ISOLATED_MERGE_MAX_WORDS = 0
ISOLATED_MAX_CHARS_NON_LATIN = 4
SCENE_ADJACENCY_MS = 1500
SCENE_CHANGE_MS = 4000
MARKER_TEMPLATE = "\u27e6c{:04d}\u27e7"

MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
INNER_B = r"\[\]\(\)\{\}\uff08\uff09\u3010\u3011"
SDH_BRACKET_PATTERN = re.compile(r"\[[^" + INNER_B + r"]*\]|\([^" + INNER_B + r"]*\)|\{[^" + INNER_B + r"]*\}|\uff08[^" + INNER_B + r"]*\uff09|\u3010[^" + INNER_B + r"]*\u3011")
LEADING_ELLIPSIS_PATTERN = re.compile(r"^(\.{2,}|\u2026)")
LEADING_NON_LETTER_PATTERN = re.compile(r"^[^A-Za-z]*")
EDGE_NOTE_PATTERN = re.compile(f"^[{MUSIC_NOTE_CHARS}\\s]+|[{MUSIC_NOTE_CHARS}\\s]+$")

LATIN_SOURCE_LANGS = {"en", "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "da", "no", "fi", "ro", "cs", "hu", "tr", "id", "vi", "ms", "tl", "ca", "eu", "gl", "la"}


def is_latin_source(source_lang):
    return (source_lang or "en").split("-")[0].lower() in LATIN_SOURCE_LANGS


COLON = ":"
NARRATOR_BLOCK_PHRASES = (
    "previously on", "improved by", " is ", " are ", " were ", " was ",
    " think ", " guess ", " will ", " believe ", " say ", " said ",
    " do ", " want ", "that's ",
)

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


def is_all_uppercase(text):
    return any(c.isalpha() for c in text) and text.upper() == text


def is_inside_colon_brackets(line, index):
    idx = line.rfind("(", 0, index)
    if idx >= 0 and line.find(")", idx) > index:
        return True
    idx = line.rfind("[", 0, index)
    if idx >= 0 and line.find("]", idx) > index:
        return True
    return False


def is_between_digits(line, index):
    return 0 < index < len(line) - 1 and line[index - 1].isdigit() and line[index + 1].isdigit()


def is_trailing_colon_only(line):
    return COLON not in line.rstrip(COLON)


def should_remove_narrator(pre):
    lowered = pre.lower()
    if len(pre) > 30 or "http" in lowered or ", " in pre:
        return False
    if len(pre) > 15 and any(phrase in lowered for phrase in NARRATOR_BLOCK_PHRASES):
        return False
    return True


def strip_speaker_tag_line(line, lines, index):
    if COLON not in line:
        return line
    index_of_colon = line.index(COLON)
    is_last_line = index == len(lines) - 1
    if index_of_colon <= 0 or is_inside_colon_brackets(line, index_of_colon):
        return line
    if is_last_line and is_trailing_colon_only(line) and line.count(" ") > 1:
        return line
    pre = line[:index_of_colon]
    if not is_all_uppercase(pre):
        return line
    if is_between_digits(line, index_of_colon):
        return line
    if not should_remove_narrator(pre):
        return line
    if len(lines) == 2 and index == 1:
        first_line = lines[0].rstrip('"')
        if not first_line.endswith((".", "!", "?", "\u266a", "\u266b", "--", "\u2014")):
            return line
    content = line[index_of_colon + 1:].strip()
    if not content:
        return ""
    if content[0].islower():
        content = content[0].upper() + content[1:]
    return content


def strip_speaker_tags(lines):
    joined = "\n".join(lines)
    if len(joined) > 10 and joined.endswith(COLON) and not is_all_uppercase(joined):
        return lines
    return [strip_speaker_tag_line(line, lines, i) for i, line in enumerate(lines)]


def strip_sdh(text):
    original = text
    while True:
        new_text = SDH_BRACKET_PATTERN.sub("", text)
        if new_text == text:
            break
        text = new_text
    cleaned = WHITESPACE_PATTERN.sub(" ", text).strip()
    if not cleaned and MUSIC_NOTE_PATTERN.search(original):
        return " ".join(MUSIC_NOTE_PATTERN.findall(original))
    return cleaned


def is_music_segment(text):
    return bool(MUSIC_NOTE_PATTERN.search(text))


def music_continuation(text):
    remainder = MUSIC_NOTE_PATTERN.sub("", text, count=1).strip()
    if LEADING_ELLIPSIS_PATTERN.match(remainder):
        return False
    return first_letter_is_lower(remainder)


def strip_edge_notes(text):
    return EDGE_NOTE_PATTERN.sub("", text)


def first_letter_is_lower(text):
    match = LEADING_NON_LETTER_PATTERN.match(text)
    rest = text[match.end():]
    return bool(rest) and rest[0].islower()


def fold_text(raw, strip_sdh_enabled=False, latin_source=True):
    lines = [WHITESPACE_PATTERN.sub(" ", TAG_PATTERN.sub("", raw_line)).strip() for raw_line in raw.splitlines()]
    lines = [line for line in lines if line]
    if strip_sdh_enabled and latin_source and lines and not any(MUSIC_NOTE_PATTERN.search(line) for line in lines):
        lines = strip_speaker_tags(lines)
    return " ".join(line for line in lines if line)


def parse_srt(content, strip_sdh_enabled=True, latin_source=True):
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
        cue_id = len(cues) + 1
        text = fold_text("\n".join(lines[time_line_idx + 1:]), strip_sdh_enabled, latin_source)
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


def is_short_reply(text, latin_source=True):
    if latin_source:
        return len(SHORT_REPLY_LETTER_PATTERN.findall(text)) <= SHORT_REPLY_MAX_LETTERS
    return len(text.strip()) <= SHORT_REPLY_MAX_LETTERS


def update_quote_state(text, is_pending):
    for index, char in enumerate(text):
        if char == '"':
            if index == 0 and is_pending:
                continue
            is_pending = not is_pending
        elif char in ('“', '「', '«'):
            is_pending = True
        elif char in ('”', '」', '»'):
            is_pending = False
    return is_pending


def is_isolated_short(text, latin_source=True):
    if not ISOLATED_MERGE_MAX_WORDS:
        return False
    if latin_source:
        return len(WORD_TOKEN_PATTERN.findall(text)) <= ISOLATED_MERGE_MAX_WORDS
    return len(text.strip()) <= ISOLATED_MAX_CHARS_NON_LATIN


def assign_merge_sides(segments, latin_source=True):
    for i, seg in enumerate(segments):
        if seg["resolved"] or is_music_segment(seg["text"]) or not is_isolated_short(seg["text"], latin_source):
            continue
        if i + 1 < len(segments):
            gap_next = time_to_ms(segments[i + 1]["start"]) - time_to_ms(seg["end"])
            if gap_next <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "next"
                continue
        if i > 0:
            gap_prev = time_to_ms(seg["start"]) - time_to_ms(segments[i - 1]["end"])
            if gap_prev <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "prev"
    return segments


def merge_reason(prev_seg, curr_seg, latin_source=True):
    if prev_seg["cue_id"] == curr_seg["cue_id"]:
        return "dash" if is_short_reply(curr_seg["text"], latin_source) else None
    if is_music_segment(prev_seg["text"]) and is_music_segment(curr_seg["text"]):
        return "music" if music_continuation(curr_seg["text"]) else None
    if prev_seg.get("merge_side") == "next" or curr_seg.get("merge_side") == "prev":
        return "marker"
    if has_terminal_punct(prev_seg["text"]):
        return None
    gap = time_to_ms(curr_seg["start"]) - time_to_ms(prev_seg["end"])
    return "gap" if gap <= GAP_THRESHOLD_MS or first_letter_is_lower(curr_seg["text"]) else None


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


def has_residual_text(text, latin_source=True):
    if latin_source:
        return bool(STUTTER_RESIDUAL_PATTERN.search(text))
    return bool(text.strip())


def find_pure_glossary_line(text, glossary, latin_source=True):
    stripped, matched_any = text, False
    for source_term in sorted(glossary, key=len, reverse=True):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        if pattern.search(stripped):
            matched_any = True
        stripped = pattern.sub("", stripped)
    if not matched_any or has_residual_text(stripped, latin_source):
        return None
    resolved = text
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        pattern = re.compile(TERM_BOUNDARY_LEFT + re.escape(source_term) + TERM_BOUNDARY_RIGHT)
        resolved = pattern.sub(target_term, resolved)
    return resolved


def build_segments(cues, glossary, latin_source=True):
    segments = []
    for cue in cues:
        for part in split_dialogue(cue["text"]):
            resolved = find_pure_glossary_line(part, glossary, latin_source)
            if not resolved and latin_source:
                resolved = find_stutter_resolution(part, glossary)
            text = part if resolved or not latin_source else strip_letter_stutter(part)
            segments.append({"cue_id": cue["id"], "text": text, "start": cue["start"], "end": cue["end"], "resolved": resolved})
    return assign_merge_sides(segments, latin_source)


QUOTE_PENDING_LIMIT = 10


def group_segments(segments, latin_source=True):
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
        merged = False
        if current:
            if quote_pending:
                gap = time_to_ms(seg["start"]) - time_to_ms(current[-1]["end"])
                merged = gap <= SCENE_ADJACENCY_MS
            if not merged:
                reason = merge_reason(current[-1], seg, latin_source)
                if reason:
                    merged = True
                    if reason == "marker":
                        seg["marker_boundary"] = True
        if merged:
            current.append(seg)
        else:
            if current:
                groups.append(current)
            current = [seg]
            quote_pending = False
            quote_span = 0
        quote_pending = update_quote_state(seg["text"], quote_pending)
        if quote_pending:
            quote_span += 1
            if quote_span >= QUOTE_PENDING_LIMIT:
                quote_pending = False
        else:
            quote_span = 0
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


def join_group_text(group, is_music_group):
    pieces = []
    for i, seg in enumerate(group):
        piece = strip_edge_notes(seg["text"]) if is_music_group else seg["text"]
        if is_music_group:
            marker = f"{MARKER_TEMPLATE.format(seg['cue_id'])} "
            pieces.append(f" {marker}" if i > 0 else marker)
        elif i > 0:
            pieces.append(f" {MARKER_TEMPLATE.format(seg['cue_id'])} " if seg.get("marker_boundary") else " ")
        pieces.append(piece)
    return "".join(pieces).strip()


def unit_kind(group):
    return "music" if any(is_music_segment(seg["text"]) for seg in group) else "dialogue"


def chapterize(groups):
    chapters = []
    open_chapter, thread_end = {}, {}
    for group in groups:
        kind = unit_kind(group)
        start_ms, end_ms = time_to_ms(group[0]["start"]), time_to_ms(group[-1]["end"])
        chapter = open_chapter.get(kind)
        if chapter is None or start_ms - thread_end[kind] > SCENE_CHANGE_MS:
            chapter = {"kind": kind, "groups": []}
            chapters.append(chapter)
            open_chapter[kind] = chapter
        chapter["groups"].append(group)
        thread_end[kind] = end_ms
    return chapters


def build_units(cues, glossary, latin_source=True):
    groups = group_segments(build_segments(cues, glossary, latin_source), latin_source)
    units, chapters, marker_merges, unit_id = [], [], 0, 0
    for chapter_index, raw_chapter in enumerate(chapterize(groups), start=1):
        is_music_chapter = raw_chapter["kind"] == "music"
        member_groups = [[seg for group in raw_chapter["groups"] for seg in group]] if is_music_chapter else raw_chapter["groups"]
        unit_ids = []
        for group in member_groups:
            unit_id += 1
            spans = [{"id": s["cue_id"], "start": s["start"], "end": s["end"], "text": s["text"],
                       "boundary": "marker" if (is_music_chapter or s.get("marker_boundary")) else None} for s in group]
            marker_merges += sum(1 for s in group if s.get("marker_boundary"))
            if len(group) == 1 and group[0]["resolved"]:
                units.append({"id": unit_id, "spans": spans, "text": "", "term_matches": [], "embed_ratio": EMBED_RATIO_DEFAULT, "resolved": group[0]["resolved"]})
            else:
                text = join_group_text(group, is_music_chapter)
                term_matches, embed_ratio = match_glossary_terms(text, glossary)
                units.append({"id": unit_id, "spans": spans, "text": text, "term_matches": term_matches, "embed_ratio": embed_ratio, "resolved": None})
            unit_ids.append(unit_id)
        chapters.append({"id": chapter_index, "kind": raw_chapter["kind"], "unit_ids": unit_ids})
    return units, chapters, marker_merges


def extract(content, glossary, strip_sdh_enabled=True, source_lang="en"):
    latin_source = is_latin_source(source_lang)
    cues, sdh_stats = parse_srt(content, strip_sdh_enabled, latin_source)
    if not cues:
        return {"success": False, "reason": "no_cues_parsed", "cues": [], "units": [], "chapters": [], "sdh_removed": sdh_stats, "marker_merges": 0}
    units, chapters, marker_merges = build_units(cues, glossary, latin_source)
    return {"success": True, "cues": cues, "units": units, "chapters": chapters, "sdh_removed": sdh_stats, "marker_merges": marker_merges}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--glossary", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--isolated-merge-max-words", type=int, default=0)
    parser.add_argument("--keep-sdh", action="store_true")
    args = parser.parse_args()

    global ISOLATED_MERGE_MAX_WORDS
    ISOLATED_MERGE_MAX_WORDS = args.isolated_merge_max_words

    raw = open(args.input, encoding="utf-8-sig").read() if args.input else sys.stdin.read()
    glossary = build_glossary_from_markdown(open(args.glossary, encoding="utf-8").read()) if args.glossary else {}

    result = extract(raw, glossary, strip_sdh_enabled=not args.keep_sdh, source_lang=args.source_lang)
    sdh_note = ""
    if not args.keep_sdh:
        stats = result["sdh_removed"]
        sdh_note = f", sdh_dropped={stats['dropped']}, sdh_stripped={stats['stripped']}"
    log(f"status: {'ok' if result['success'] else 'failed'} (cues={len(result['cues'])}, units={len(result['units'])}, chapters={len(result['chapters'])}{sdh_note}, marker_merges={result['marker_merges']})")

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
