#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: srt_extract.py
# Version: 2.5.3
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p), Joey
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
#     - Resolves name stutters and matches terminology against provided glossary,
#       case-insensitively by default (toggle via --case-sensitive-glossary).
#     - Groups subtitle cues based on punctuation, pause gaps, and quote continuity.
#     - Units carry natural, unmarked merged text plus a `spans` list (original
#       per-piece text/timing) so downstream can reconstruct without markers.
#     - Annotates glossary hits as `term_matches` (matched substring
#       position + target); the translation step wraps the matched span in
#       a no-translate marker to protect it verbatim.
#     - Groups units into `chapters` (scene/song-level runs) so downstream
#       batching can send whole scenes as translation context instead of
#       splitting arbitrarily by character count.
#     - Scene-change gap threshold (SCENE_CHANGE_MS, default 30000)
#       configurable via --scene-change-ms.
#
# 功能特性：
#     - 解析 SRT 字幕结构，并对文本和时间戳进行标准化。
#     - 拆分以前导破折号 ('- ') 标记的多发言者对话行。
#     - 解决名称结巴问题，默认按大小写不敏感方式匹配术语表（可用
#       --case-sensitive-glossary 切换为精确匹配）。
#     - 根据标点、停顿间隙和引号连续性对字幕句组进行分组。
#     - 单元包含自然的无标记合并文本及 `spans` 列表（原分段
#       文本/时间），以便下游在无需标记的情况下进行重建。
#     - 将术语匹配标注为 `term_matches`（匹配子串位置 + 译文）；翻译步骤
#       将命中片段包裹为免翻译标记以原样保护。
#     - 将单元分组为 `chapters`（场景/歌曲级别），以便下游批处理
#       能将整个场景作为翻译上下文发送，而非简单地按字符数
#       进行随意拆分。
#     - 场景切换判定的间隔阈值（SCENE_CHANGE_MS，默认 30000）可通过
#       --scene-change-ms 配置。
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
import codecs
import json
import re
import subprocess
import sys

BOM_SIGNATURES = [
    ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
    ("utf-8-sig", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
]
FALLBACK_ENCODINGS = ["cp1252", "gb18030", "big5", "shift_jis", "euc-kr", "iso-8859-1"]

_CHARSET_NORMALIZER_CHECKED = False
_CHARSET_NORMALIZER_MODULE = None


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


def ensure_charset_normalizer():
    global _CHARSET_NORMALIZER_CHECKED, _CHARSET_NORMALIZER_MODULE
    if _CHARSET_NORMALIZER_CHECKED:
        return _CHARSET_NORMALIZER_MODULE
    _CHARSET_NORMALIZER_CHECKED = True
    try:
        import charset_normalizer
    except ImportError:
        if not pip_install("charset-normalizer"):
            log("charset_normalizer unavailable, falling back to a fixed encoding guess list")
            return None
        try:
            import charset_normalizer
        except ImportError as e:
            log(f"charset_normalizer unavailable, falling back to a fixed encoding guess list: {e}")
            return None
    _CHARSET_NORMALIZER_MODULE = charset_normalizer
    return _CHARSET_NORMALIZER_MODULE


def detect_newline_style(text):
    if "\r\n" in text:
        return "crlf"
    if "\r" in text:
        return "cr"
    return "lf"


def decode_srt_bytes(raw_bytes):
    for encoding, signature in BOM_SIGNATURES:
        if raw_bytes.startswith(signature):
            return raw_bytes[len(signature):].decode(encoding.replace("-sig", "")), encoding, True

    try:
        return raw_bytes.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass

    charset_normalizer = ensure_charset_normalizer()
    if charset_normalizer is not None:
        best = charset_normalizer.from_bytes(raw_bytes).best()
        if best is not None:
            return str(best), best.encoding, False

    for encoding in FALLBACK_ENCODINGS:
        try:
            return raw_bytes.decode(encoding), encoding, False
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8", False

SCRIPT_NAME = "srt_extract"

TIME_LINE_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")
TAG_PATTERN = re.compile(r"<[^>]+>|\{[^}]*\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
TERMINAL_PUNCT_PATTERN = re.compile(r"[.!?。！？][’”\"')\]」』】）]*\s*$")
TRAILING_ELLIPSIS_PATTERN = re.compile(r"(\.{2,}|…)\s*$")
TRAILING_CUTOFF_PATTERN = re.compile(r"-{2,}\s*$")
DIALOGUE_DASH_PATTERN = re.compile(r"(?:^|(?<=\s))-(?!-)\s?")
STUTTER_WORD_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-\1(?![A-Za-z])", re.IGNORECASE)
STUTTER_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-(?=\1[a-z])", re.IGNORECASE)
SHORT_REPLY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]")
SHORT_REPLY_MAX_TOKENS = 3
STUTTER_RESIDUAL_PATTERN = re.compile(r"[A-Za-z]")
TRAILING_MARK_PATTERN = re.compile(r"[!?…]+$")
GAP_THRESHOLD_MS = 200
WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
ISOLATED_MERGE_MAX_WORDS = 0
ISOLATED_MAX_CHARS_NON_LATIN = 4
SCENE_ADJACENCY_MS = 1500
SCENE_CHANGE_MS = 30000
MARKER_TEMPLATE = "\u27e6c{}\u27e7"

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

CASE_SENSITIVE_GLOSSARY = False


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def glossary_flags():
    return 0 if CASE_SENSITIVE_GLOSSARY else re.IGNORECASE


def glossary_pattern(term):
    return re.compile(TERM_BOUNDARY_LEFT + re.escape(term) + TERM_BOUNDARY_RIGHT, glossary_flags())


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
    if TRAILING_ELLIPSIS_PATTERN.search(text):
        return False
    if TRAILING_CUTOFF_PATTERN.search(text):
        return True
    return bool(TERMINAL_PUNCT_PATTERN.search(text))


def is_short_reply(text, latin_source=True):
    if latin_source:
        return len(SHORT_REPLY_TOKEN_PATTERN.findall(text)) <= SHORT_REPLY_MAX_TOKENS
    return len(text.strip()) <= SHORT_REPLY_MAX_TOKENS


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
        if i + 1 < len(segments) and not is_music_segment(segments[i + 1]["text"]):
            gap_next = time_to_ms(segments[i + 1]["start"]) - time_to_ms(seg["end"])
            if gap_next <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "next"
                continue
        if i > 0 and not is_music_segment(segments[i - 1]["text"]):
            gap_prev = time_to_ms(seg["start"]) - time_to_ms(segments[i - 1]["end"])
            if gap_prev <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "prev"
    return segments


def merge_reason(prev_seg, curr_seg, latin_source=True):
    prev_is_music = is_music_segment(prev_seg["text"])
    curr_is_music = is_music_segment(curr_seg["text"])
    if prev_is_music != curr_is_music:
        return None
    if prev_seg["cue_id"] == curr_seg["cue_id"]:
        return "dash" if is_short_reply(curr_seg["text"], latin_source) else None
    if prev_is_music and curr_is_music:
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
        pattern = glossary_pattern(source_term)
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
        pattern = glossary_pattern(source_term)
        if pattern.search(stripped):
            matched_any = True
        stripped = pattern.sub("", stripped)
    if not matched_any or has_residual_text(stripped, latin_source):
        return None
    resolved = text
    for source_term, target_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        if not source_term:
            continue
        resolved = glossary_pattern(source_term).sub(target_term, resolved)
    return resolved


def build_segments(cues, glossary, latin_source=True):
    segments = []
    for cue in cues:
        for dash_index, part in enumerate(split_dialogue(cue["text"])):
            resolved = find_pure_glossary_line(part, glossary, latin_source)
            if not resolved and latin_source:
                resolved = find_stutter_resolution(part, glossary)
            text = part if resolved or not latin_source else strip_letter_stutter(part)
            segments.append({"cue_id": cue["id"], "text": text, "start": cue["start"], "end": cue["end"],
                              "resolved": resolved, "dash_index": dash_index})
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
                    if reason in ("marker", "dash"):
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
        for m in glossary_pattern(source_term).finditer(text):
            if any(a < m.end() and m.start() < b for a, b in claimed):
                continue
            claimed.append((m.start(), m.end()))
            matches.append({"start": m.start(), "end": m.end(), "matched": m.group(), "target": target_term})
    matches.sort(key=lambda m: m["start"])
    return matches


def join_group_text(group, is_music_group):
    pieces = []
    is_multi_music = is_music_group and len(group) > 1
    for i, seg in enumerate(group):
        piece = strip_edge_notes(seg["text"]) if is_music_group else seg["text"]
        if is_multi_music:
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
                       "boundary": "marker" if (is_music_chapter or s.get("marker_boundary")) else None,
                       "dash_index": s.get("dash_index", 0),
                       "kind": "music" if is_music_segment(s["text"]) else "dialogue"} for s in group]
            marker_merges += sum(1 for s in group if s.get("marker_boundary"))
            if len(group) == 1 and group[0]["resolved"]:
                units.append({"id": unit_id, "spans": spans, "text": "", "term_matches": [], "resolved": group[0]["resolved"]})
            else:
                text = join_group_text(group, is_music_chapter)
                units.append({"id": unit_id, "spans": spans, "text": text, "term_matches": match_glossary_terms(text, glossary), "resolved": None})
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
    global ISOLATED_MERGE_MAX_WORDS, CASE_SENSITIVE_GLOSSARY, SCENE_CHANGE_MS
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--glossary", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--isolated-merge-max-words", type=int, default=0)
    parser.add_argument("--scene-change-ms", type=int, default=SCENE_CHANGE_MS)
    parser.add_argument("--keep-sdh", action="store_true")
    parser.add_argument("--case-sensitive-glossary", action="store_true")
    args = parser.parse_args()

    ISOLATED_MERGE_MAX_WORDS = args.isolated_merge_max_words
    CASE_SENSITIVE_GLOSSARY = args.case_sensitive_glossary
    SCENE_CHANGE_MS = args.scene_change_ms

    if args.input:
        raw_bytes = open(args.input, "rb").read()
    else:
        raw_bytes = sys.stdin.buffer.read()
    raw, detected_encoding, has_bom = decode_srt_bytes(raw_bytes)
    source_format = {"encoding": detected_encoding, "bom": has_bom, "newline": detect_newline_style(raw)}
    log(f"input decoded as {detected_encoding}{' with BOM' if has_bom else ''}, newline style: {source_format['newline']}")
    glossary = build_glossary_from_markdown(open(args.glossary, encoding="utf-8").read()) if args.glossary else {}

    result = extract(raw, glossary, strip_sdh_enabled=not args.keep_sdh, source_lang=args.source_lang)
    result["source_format"] = source_format
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
