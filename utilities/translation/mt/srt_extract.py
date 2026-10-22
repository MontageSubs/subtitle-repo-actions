#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: srt_extract.py
# Version: 2.8.2
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
#     - Preserves inline <i>/<b>/<u> tags through folding and dialogue
#       splitting; a cue fully wrapped by one such tag has it stripped
#       before translation and flagged via `style_wrap` on every span it
#       produces, to be re-applied per segment after translation.
#     - A closing style tag at the end of one raw cue line immediately
#       followed by the same opening tag at the start of the next line
#       is collapsed into a single continuous wrap across the join.
#
#     - SDH stripping works per raw line before lines are folded: bracketed
#       cues are removed first, speaker labels second, so a leading SDH line
#       no longer blocks label detection. Style tags are isolated per line
#       beforehand so wrappers never pollute label/uppercase checks.
#     - When labels are stripped and a cue holds two or more speakers, a
#       leading '- ' is restored per turn and handled as a native dash.
#     - Every dash segment detects its own full style wrap.
#     - Segments without any letter or digit (e.g. '♪ ♪', '...') pass through
#       as `resolved` and are never sent for translation.
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
#     - SDH 剥离在折叠前按原生行进行：先移除括号类内容、再移除说话人标签，
#       前导 SDH 行不再阻断标签识别；并预先将样式标签按行隔离，避免包裹
#       标签污染标签/大写判断。
#     - 剥离标签后若同一字幕块存在两位及以上说话人，按发言轮次补回行首
#       '- '，并按原生破折号继续处理。
#     - 每个破折号分段独立识别整句样式包裹。
#     - 不含字母或数字的分段（如 '♪ ♪'、'...'）以 `resolved` 原样透传，
#       不发送翻译。
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
import logging
import re
import subprocess
import sys
import unicodedata

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
STYLE_TAG_PATTERN = re.compile(r"</?(i|b|u)>", re.IGNORECASE)
STYLE_TAG_TOKEN_PATTERN = re.compile(r"<(/?)(i|b|u)>", re.IGNORECASE)
ADJACENT_STYLE_WRAP_PATTERN = re.compile(r"</(i|b|u)>(\s*)<\1>", re.IGNORECASE)
LEADING_STYLE_OPEN_PATTERN = re.compile(r"(?:<(?:i|b|u)>)*", re.IGNORECASE)
STYLE_TAG_PLACEHOLDER_PATTERN = re.compile(r"\u0001(\d+)\u0001")
STYLE_TAG_ALT = r"(?:</?(?:i|b|u)>)"
STYLE_CLOSE_ALT = r"(?:</(?:i|b|u)>)"
FULL_WRAP_PATTERN = re.compile(r"^<(i|b|u)>(.*)</\1>$", re.IGNORECASE | re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"\s+")
DIALOGUE_DASH_PATTERN = re.compile(rf"(?:^|(?<=\s)){STYLE_TAG_ALT}*-(?!-){STYLE_TAG_ALT}*\s?", re.IGNORECASE)
STUTTER_WORD_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-\1(?![A-Za-z])", re.IGNORECASE)
STUTTER_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z])([A-Za-z])-(?=\1[a-z])", re.IGNORECASE)
LATIN_LETTER_CLASS = "A-Za-z\u00aa\u00ba\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02b8\u1e00-\u1eff\u2c60-\u2c7f\ua720-\ua7ff\uff21-\uff3a\uff41-\uff5a"
SHORT_REPLY_TOKEN_PATTERN = re.compile(rf"[{LATIN_LETTER_CLASS}\d]")
SHORT_REPLY_MAX_TOKENS = 3
STUTTER_RESIDUAL_PATTERN = re.compile(rf"[{LATIN_LETTER_CLASS}]")
TRAILING_MARK_PATTERN = re.compile(rf"[!?…]+{STYLE_CLOSE_ALT}*$", re.IGNORECASE)
GAP_THRESHOLD_MS = 200
MAX_OVERLAP_MS = 1000
WORD_TOKEN_PATTERN = re.compile(rf"[{LATIN_LETTER_CLASS}]+(?:['’][{LATIN_LETTER_CLASS}]+)*")
ISOLATED_MERGE_MAX_WORDS = 0
ISOLATED_MAX_CHARS_NON_LATIN = 4
SCENE_ADJACENCY_MS = 1500
SCENE_CHANGE_MS = 30000
MARKER_TEMPLATE = "\u27e6c{}\u27e7"

MUSIC_NOTE_CHARS = "\u2669\u266a\u266b\u266c"
MUSIC_NOTE_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}]")
INNER_B = r"\[\]\(\)\{\}\uff08\uff09\u3010\u3011"
OPEN_BRACKETS = "[({\uff08\u3010"
CLOSE_BRACKETS = "])}\uff09\u3011"
BRACKET_RESIDUE_PATTERN = re.compile(r"(<[ibu]>)\s+|\s+(</[ibu]>)|\s+(:)", re.IGNORECASE)
SDH_BRACKET_PATTERN = re.compile(r"\[[^" + INNER_B + r"]*\]|\([^" + INNER_B + r"]*\)|\{[^" + INNER_B + r"]*\}|\uff08[^" + INNER_B + r"]*\uff09|\u3010[^" + INNER_B + r"]*\u3011")
EDGE_NOTE_PATTERN = re.compile(f"^[{MUSIC_NOTE_CHARS}\\s]+|[{MUSIC_NOTE_CHARS}\\s]+$")

LATIN_SOURCE_LANGS = {
    "en", "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "da", "no", "fi", "ro", "cs", "hu", "tr", "id", "vi",
    "ms", "tl", "ca", "eu", "gl", "la", "hr", "sk", "sl", "lt", "lv", "et", "sq", "cy", "is", "af",
}
LANGUAGE_ALIASES = {"nb": "no", "nn": "no", "fil": "tl", "in": "id"}
SDH_LANGS = {"en"}


def base_language(lang):
    base = (lang or "en").strip().lower().replace("_", "-").split("-")[0]
    return LANGUAGE_ALIASES.get(base, base)


def is_latin_source(source_lang):
    return base_language(source_lang) in LATIN_SOURCE_LANGS


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
    if "-" not in text:
        return text
    text = STUTTER_WORD_PATTERN.sub(lambda m: m.group(1), text)
    return STUTTER_PREFIX_PATTERN.sub("", text)


SENTENCE_END_CHARS = (".", "!", "?", "\u266a", "\u266b", "--", "\u2014")
LEADING_DASH_PATTERN = re.compile(r"^\s*[-\u2013\u2014]")
LABEL_LEAD_PATTERN = re.compile(r"^[-\u2013\u2014\s]*")


def to_plain(line):
    return STYLE_TAG_PATTERN.sub("", line) if "<" in line else line


def style_tags_of(text):
    return "".join(match.group(0) for match in STYLE_TAG_PATTERN.finditer(text))


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


def should_remove_narrator(label):
    lowered = label.lower()
    if len(label) > 30 or "http" in lowered or ", " in label:
        return False
    return not (len(label) > 15 and any(phrase in lowered for phrase in NARRATOR_BLOCK_PHRASES))


def capitalize_first(text):
    tags = LEADING_STYLE_OPEN_PATTERN.match(text).group(0)
    rest = text[len(tags):]
    return tags + rest[0].upper() + rest[1:] if rest and rest[0].islower() else text


def tag_speaker_line(line, plain_lines, index, previous_speaker):
    plain = plain_lines[index]
    colon_index = plain.find(COLON)
    if colon_index <= 0 or is_inside_colon_brackets(plain, colon_index):
        return line, None
    if index == len(plain_lines) - 1 and is_trailing_colon_only(plain) and plain.count(" ") > 1:
        return line, None
    prefix = plain[:colon_index]
    label = prefix[LABEL_LEAD_PATTERN.match(prefix).end():].strip()
    if not is_all_uppercase(label) or is_between_digits(plain, colon_index) or not should_remove_narrator(label):
        return line, None
    if len(plain_lines) == 2 and index == 1 and previous_speaker is None \
            and not plain_lines[0].rstrip('"').endswith(SENTENCE_END_CHARS):
        return line, None
    raw_colon = line.index(COLON)
    content = style_tags_of(line[:raw_colon]) + line[raw_colon + 1:].strip()
    speaker = " ".join(label.split())
    dash = "- " if LEADING_DASH_PATTERN.match(prefix) else ""
    body = content if speaker == previous_speaker else capitalize_first(content)
    return (dash + body if to_plain(content).strip() else ""), speaker


def insert_turn_dashes(entries):
    turn_starts, kept = [], []
    current, turn_pending = None, True
    for text, speaker in entries:
        if speaker and speaker != current:
            current, turn_pending = speaker, True
        if not text:
            continue
        if turn_pending:
            turn_starts.append(len(kept))
        turn_pending = False
        kept.append(text)
    if len(turn_starts) < 2:
        return kept
    for at in turn_starts:
        if not LEADING_DASH_PATTERN.match(to_plain(kept[at])):
            kept[at] = f"- {kept[at]}"
    return kept


def strip_speaker_tags(lines):
    plain_lines = [to_plain(line) for line in lines]
    joined = "\n".join(plain_lines)
    if len(joined) > 10 and joined.endswith(COLON) and not is_all_uppercase(joined):
        return lines
    entries, previous_speaker = [], None
    for index, line in enumerate(lines):
        text, speaker = tag_speaker_line(line, plain_lines, index, previous_speaker)
        entries.append((text, speaker))
        previous_speaker = speaker or previous_speaker
    return insert_turn_dashes(entries) if any(speaker for _, speaker in entries) else lines


EMPTY_STYLE_WRAP_PATTERN = re.compile(r"<(i|b|u)>\s*</\1>", re.IGNORECASE)
DANGLING_DASH_PATTERN = re.compile(r"(?:</?[ibu]>|[-\u2013\u2014\s])*", re.IGNORECASE)


def strip_sdh(text):
    current = text
    while True:
        new_text = SDH_BRACKET_PATTERN.sub("", current)
        if new_text == current:
            break
        current = new_text
    if current != text:
        current = BRACKET_RESIDUE_PATTERN.sub(lambda m: "".join(g or "" for g in m.groups()), current)
    while True:
        new_text = EMPTY_STYLE_WRAP_PATTERN.sub("", current)
        if new_text == current:
            break
        current = new_text
    collapsed = WHITESPACE_PATTERN.sub(" ", current).strip()
    cleaned = "" if DANGLING_DASH_PATTERN.fullmatch(collapsed) else collapsed
    if not cleaned and MUSIC_NOTE_PATTERN.search(text):
        return " ".join(MUSIC_NOTE_PATTERN.findall(text))
    return cleaned


def has_open_bracket(line):
    return any(line.count(open_char) > line.count(close_char) for open_char, close_char in zip(OPEN_BRACKETS, CLOSE_BRACKETS))


def join_open_brackets(lines):
    joined = []
    for line in lines:
        if joined and has_open_bracket(joined[-1]) and any(close_char in line for close_char in CLOSE_BRACKETS):
            joined[-1] = f"{joined[-1]} {line}"
        else:
            joined.append(line)
    return joined


def isolate_style_lines(lines):
    if not any("<" in line for line in lines):
        return lines
    open_tags, isolated = [], []
    for line in lines:
        carried = "".join(f"<{tag}>" for tag in open_tags)
        for match in STYLE_TAG_TOKEN_PATTERN.finditer(line):
            tag = match.group(2).lower()
            if not match.group(1):
                open_tags.append(tag)
            elif tag in open_tags:
                del open_tags[len(open_tags) - 1 - open_tags[::-1].index(tag)]
        pending = "".join(f"</{tag}>" for tag in reversed(open_tags))
        isolated.append(carried + line + pending)
    return isolated


def strip_cue_sdh(lines):
    has_music = any(MUSIC_NOTE_PATTERN.search(line) for line in lines)
    isolated = isolate_style_lines(join_open_brackets(lines))
    bracket_free = [cleaned for cleaned in map(strip_sdh, isolated) if cleaned]
    processed = bracket_free if has_music else strip_speaker_tags(bracket_free)
    return lines if processed == isolated else processed


BLANK_CHARS = frozenset("\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff") | frozenset(chr(code) for code in range(0x2000, 0x200b))
TERMINAL_CHARS = frozenset(".!?\u2026\u22ef\u3002\uff01\uff1f\u203c\u2047\u2048\u2049\u061f\u06d4\u0964\u0965\u0589\u037e\u104b\u1362")
CONTINUATION_CHARS = frozenset(",;:\u3001\uff0c\uff1b\uff1a")
NOTE_CHARS = frozenset("\u266a\u266b\u2669\u266c*\u2020\u2021\u203b\u2605\u2606")
BRACKET_CLOSE_CHARS = frozenset(")]}\uff09\uff3d\uff5d\u3015\u3017\u3019\u301b\u3009\u300b\u3011\u27e9\u27eb")
CLOSER_CHARS = BRACKET_CLOSE_CHARS | frozenset("\uff1e\u201d\u2019\u00bb\u203a\u300d\u300f\u301e\u301f")
QUOTE_CHARS = frozenset("\"'")
BRACKET_OPEN_CHARS = frozenset("([{\uff08\uff3b\uff5b\u3014\u3016\u3018\u301a\u3008\u300a\u3010\u27e8\u27ea")
BOUND_CHARS = frozenset("\u3001\u3002\uff0c\uff0e\uff1b\uff1a\u30fb\u309d\u309e\u30fd\u30fe\u3005\u303b\u300d\u300f\u301e\u301f")
HEAD_BOUND_CHARS = BRACKET_CLOSE_CHARS | BOUND_CHARS
OPENER_CHARS = frozenset("\uff08\uff3b\uff5b\u3014\u3016\u3018\u301a\u3008\u300a\u3010\u27e8\u27ea\u300c\u300e\u301d\u00bf\u00a1\u00a5\uffe5\uff04\u00a3\uff03\u2116\u00a7\uff20")
STYLE_TAG_LETTERS = "ibuIBU"
TAIL_TERMINAL, TAIL_ELLIPSIS, TAIL_CONTINUATION, TAIL_DASH, TAIL_CLOSED, TAIL_OPENER = 1, 2, 4, 8, 16, 32
TAIL_END_SHIFT = 8
HEAD_ELLIPSIS, HEAD_LOWER, HEAD_UPPER, HEAD_CLOSER = 1, 2, 4, 8


def style_tag_length(text, index):
    if text[index:index + 1] != "<":
        return 0
    first, second, third = text[index + 1:index + 2], text[index + 2:index + 3], text[index + 3:index + 4]
    if first and first in STYLE_TAG_LETTERS and second == ">":
        return 3
    return 4 if first == "/" and second and second in STYLE_TAG_LETTERS and third == ">" else 0


def skip_leading(text):
    index = 0
    while index < len(text):
        if text[index] in BLANK_CHARS or text[index] in NOTE_CHARS:
            index += 1
            continue
        tag = style_tag_length(text, index)
        if not tag:
            break
        index += tag
    return index


def classify_tail(text):
    content, closed = len(text), False
    while content > 0:
        char = text[content - 1]
        if char in BLANK_CHARS or char in NOTE_CHARS:
            content -= 1
        elif char in CLOSER_CHARS or char in QUOTE_CHARS:
            closed = True
            content -= 1
        elif char == ">" and content >= 4 and style_tag_length(text, content - 4) == 4:
            content -= 4
        else:
            break
    last, before = text[content - 1:content], text[content - 2:content - 1]
    flags = 0
    if last in ("\u2026", "\u22ef"):
        flags = TAIL_ELLIPSIS
    elif last == ".":
        flags = TAIL_ELLIPSIS if before == "." else TAIL_TERMINAL
    elif last == "-":
        flags = TAIL_TERMINAL if before == "-" else TAIL_DASH
    elif last and last in TERMINAL_CHARS:
        flags = TAIL_TERMINAL
    elif last and last in CONTINUATION_CHARS:
        flags = TAIL_CONTINUATION
    if last and last in OPENER_CHARS:
        flags |= TAIL_OPENER
    return (flags | TAIL_CLOSED if closed else flags) | (content << TAIL_END_SHIFT)


def classify_head(text):
    index = skip_leading(text)
    lead = index
    while text[lead:lead + 1] in QUOTE_CHARS and lead < len(text):
        lead += 1
    first = text[lead:lead + 1]
    flags = 0
    if first == "\u2026" or (first == "." and text[lead + 1:lead + 2] == "."):
        flags = HEAD_ELLIPSIS
    elif first and first in HEAD_BOUND_CHARS:
        flags = HEAD_CLOSER
    while index < len(text):
        tag = style_tag_length(text, index)
        if tag:
            index += tag
            continue
        category = unicodedata.category(text[index])
        if category == "Ll":
            return flags | HEAD_LOWER
        if category in ("Lu", "Lt"):
            return flags | HEAD_UPPER
        if category[0] == "L":
            return flags
        index += 1
    return flags


def continues_by_punctuation(previous, current, gap_ms):
    tail, head = previous["tail"], current["head"]
    if tail & TAIL_TERMINAL:
        return False
    contiguous = gap_ms <= GAP_THRESHOLD_MS
    if tail & TAIL_DASH:
        return contiguous and bool(head & HEAD_LOWER)
    if tail & TAIL_ELLIPSIS:
        if head & HEAD_ELLIPSIS:
            return gap_ms <= SCENE_ADJACENCY_MS
        if head & HEAD_UPPER:
            return False
    elif tail & TAIL_CONTINUATION and gap_ms <= SCENE_ADJACENCY_MS:
        return True
    return contiguous or bool(head & HEAD_LOWER)


def index_by_first_char(words):
    by_char = {}
    for word in words:
        by_char.setdefault(word[0], []).append(word)
    return lambda text, index: any(text.startswith(word, index) for word in by_char.get(text[index:index + 1], ()))


def index_by_last_char(words):
    by_char = {}
    for word in words:
        by_char.setdefault(word[-1], []).append(word)
    return lambda text, end: any(text.startswith(word, end - len(word)) for word in by_char.get(text[end - 1:end], ()))


EDGE_WINDOW = 8


def create_edge_tokenizer(locale):
    cutter = None
    if locale == "zh":
        try:
            import jieba
            jieba.setLogLevel(logging.ERROR)
            cutter = jieba.lcut
        except ImportError:
            pass
    elif locale == "ja":
        try:
            import tinysegmenter
            cutter = tinysegmenter.TinySegmenter().tokenize
        except ImportError:
            pass
    cache = {}

    def tokens_of(window):
        if window not in cache:
            cache[window] = cutter(window) if cutter else list(window)
        return cache[window]

    def first(text, index):
        tokens = tokens_of(text[index:index + EDGE_WINDOW])
        return tokens[0] if tokens else ""

    def last(text, end):
        tokens = tokens_of(text[max(0, end - EDGE_WINDOW):end])
        return tokens[-1] if tokens else ""

    return first, last


def make_cjk_continuation(locale, no_cue_start, no_cue_end, no_cue_end_phrases, complete_endings, sentence_starts, continuation_starts, turn_starts):
    first_token, last_token = create_edge_tokenizer(locale)
    start_tokens, end_tokens, turn_starts = frozenset(no_cue_start), frozenset(no_cue_end), frozenset(turn_starts)
    may_start_bound = index_by_first_char(no_cue_start)
    may_end_unfinished = index_by_last_char(no_cue_end)
    ends_with_phrase = index_by_last_char(no_cue_end_phrases)
    ends_complete = index_by_last_char(complete_endings)
    starts_new_sentence = index_by_first_char(sentence_starts)
    continues_with = index_by_first_char(continuation_starts)

    def is_turn_start(text, index):
        return text[index:index + 1] in turn_starts or starts_new_sentence(text, index)

    def ends_unfinished(text, end):
        return ends_with_phrase(text, end) or (may_end_unfinished(text, end) and not ends_complete(text, end) and last_token(text, end) in end_tokens)

    def starts_bound(text, index):
        return may_start_bound(text, index) and first_token(text, index) in start_tokens

    def continues(previous, current, gap_ms):
        tail = previous["tail"]
        if tail & (TAIL_TERMINAL | TAIL_ELLIPSIS | TAIL_CLOSED | TAIL_DASH) or gap_ms > SCENE_ADJACENCY_MS:
            return False
        index = skip_leading(current["text"])
        if tail & TAIL_CONTINUATION or ends_unfinished(previous["text"], tail >> TAIL_END_SHIFT):
            return not is_turn_start(current["text"], index)
        return starts_bound(current["text"], index) or continues_with(current["text"], index)

    return continues


TURN_STARTS = "\uff08(\u3010[-\uff0d\u2015"
continues_japanese = make_cjk_continuation(
    "ja",
    ["を", "に", "が", "と", "から", "まで", "より", "ので", "のに", "ても", "ば", "たり", "だり", "ながら", "つつ", "ぞ", "ぜ", "かな", "かしら", "です", "ます", "ました", "でした"],
    ["を", "は", "も", "が", "と", "より", "まで", "ば", "ながら", "つつ", "ので", "たり", "だり", "という", "によって", "による", "として", "について", "途端", "とたん"],
    ["ために", "ための", "ような", "ずに", "ないで", "くせに", "ものの", "うちに"],
    ["こんにちは", "こんばんは", "どうも", "これまで", "今まで", "ここまで", "そこまで", "どこまで", "あそこまで", "っと", "ちゃんと", "ほんと", "ふと"],
    ["ええ", "えっ", "あっ", "ああ", "あ", "いや", "うん", "はい", "おい", "ほら", "まあ", "さあ", "ねえ", "なあ", "もう", "でも", "だから", "じゃあ", "じゃ", "それで", "そして", "しかし", "だが", "だけど", "ところで", "つまり", "えーと", "うーん", "ちょっと", "すみません", "ありがとう"],
    [],
    TURN_STARTS,
)

CHINESE_ORTHOGRAPHY = {
    "zh-hans": {
        "proclitics": ["在", "从", "向", "往", "对", "给", "把", "被", "与", "和", "跟", "同", "随", "自", "于", "由", "按", "按照", "根据", "为", "为了", "朝", "凭", "借", "沿", "沿着", "趁", "趁着", "这", "这个", "这些", "那", "那个", "那些", "哪", "各个", "每", "以及", "关于", "有关", "对于"],
        "enclitics": ["的", "地", "得", "了", "着", "过", "吗", "呢", "吧", "啊", "呀", "哇", "啦", "罢", "哩", "呗", "嘞", "咯", "哈", "哟", "哎", "耶", "们", "等等"],
        "conjunctions": ["并且", "而且", "但是", "然而", "或者", "还是", "因而", "所以", "虽然", "尽管", "不仅", "不但", "如果", "要是", "即使", "哪怕", "只有", "只要", "因为", "既然"],
    },
    "zh-hant": {
        "proclitics": ["在", "從", "向", "往", "對", "給", "把", "被", "與", "和", "跟", "同", "隨", "自", "於", "由", "按", "按照", "根據", "為", "為了", "朝", "憑", "藉", "沿", "沿著", "趁", "趁著", "這", "這個", "這些", "那", "那個", "那些", "哪", "各", "每", "以及", "關於", "有關", "對於"],
        "enclitics": ["的", "地", "得", "了", "著", "過", "嗎", "呢", "吧", "啊", "呀", "哇", "啦", "罷", "哩", "唄", "嘞", "咯", "哈", "喲", "哎", "耶", "們", "等等"],
        "conjunctions": ["並且", "而且", "但是", "然而", "或者", "還是", "因而", "所以", "雖然", "儘管", "不僅", "不但", "如果", "要是", "即使", "哪怕", "只有", "只要", "因為", "既然"],
    },
    "yue": {
        "proclitics": ["喺", "喺度", "由", "向", "畀", "同", "將", "自", "跟", "到", "由得", "為咗", "趁", "照", "順", "呢", "呢個", "呢啲", "嗰", "嗰個", "嗰啲", "邊個", "邊度", "點解", "關於", "同埋"],
        "enclitics": ["嘅", "咗", "緊", "過", "喇", "咩", "呢", "咪", "呀", "喎", "㗎", "啫", "囉", "哩", "晒", "埋", "掂", "倒", "翻", "返", "添", "咋", "嘞", "啩", "啝", "先", "定", "哋"],
        "conjunctions": ["而且", "但係", "不過", "或者", "所以", "如果", "同埋", "抑或", "因為", "既然", "縱使", "雖然", "就算", "只要"],
    },
}
INTERJECTIVE_OR_LEXICAL_ENCLITICS = frozenset(["呢", "啊", "哇", "哈", "哎", "耶", "呀", "哟", "喲", "过", "過", "得", "地", "罢", "罷", "等等", "先", "定", "咪", "咩", "倒", "翻", "返", "掂"])
STANDALONE_PROCLITICS = frozenset(["对", "對", "给", "給", "借", "藉", "按", "这", "這", "这个", "這個", "这些", "這些", "那", "那个", "那個", "那些", "哪", "每", "各", "各个", "呢", "呢個", "呢啲", "嗰", "嗰個", "嗰啲", "邊個", "邊度", "點解"])


def make_chinese_continuation(*variants):
    orthographies = [CHINESE_ORTHOGRAPHY[variant] for variant in variants]
    return make_cjk_continuation(
        "zh",
        [word for orthography in orthographies for word in orthography["enclitics"] if word not in INTERJECTIVE_OR_LEXICAL_ENCLITICS] + ["嘛", "么", "麼", "而已", "罢了", "罷了", "似的"],
        [word for orthography in orthographies for word in orthography["proclitics"] if word not in STANDALONE_PROCLITICS]
        + [word for orthography in orthographies for word in orthography["conjunctions"]]
        + ["然后", "然後", "不过", "不過", "可是", "而是", "以便", "由于", "由於", "除了", "通过", "通過", "随着", "隨著", "除非", "的话", "的話", "之后", "之後", "之前"],
        ["的时候", "的時候"],
        [],
        ["嗯", "啊", "哦", "喔", "噢", "喂", "哎", "唉", "嘿", "呃", "哈", "哇", "哟", "咦", "谢谢", "謝謝", "对不起", "對不起", "你好", "再见", "再見", "好的", "好吧"],
        ["但是", "但係", "可是", "而且", "并且", "並且", "所以", "因此", "然后", "然後", "而是", "于是", "於是", "以及", "或者", "但", "而", "并", "並", "却", "卻"],
        TURN_STARTS,
    )


continues_mandarin = make_chinese_continuation("zh-hans", "zh-hant")
continues_cantonese = make_chinese_continuation("yue")


def continuation_for(source_lang):
    base = base_language(source_lang)
    if base == "ja":
        return continues_japanese
    if base == "zh":
        return continues_mandarin
    return continues_cantonese if base == "yue" else continues_by_punctuation


def is_music_segment(text):
    return bool(MUSIC_NOTE_PATTERN.search(text))


def music_continuation(text):
    head = classify_head(MUSIC_NOTE_PATTERN.sub("", text, count=1).strip())
    return not head & HEAD_ELLIPSIS and bool(head & HEAD_LOWER)


def strip_edge_notes(text):
    return EDGE_NOTE_PATTERN.sub("", text)


def strip_tags_preserving_style(line):
    if "<" not in line and "{" not in line:
        return line
    preserved = []

    def guard(m):
        preserved.append(m.group(0).lower())
        return f"\u0001{len(preserved) - 1}\u0001"

    guarded = STYLE_TAG_PATTERN.sub(guard, line)
    stripped = TAG_PATTERN.sub("", guarded)
    return STYLE_TAG_PLACEHOLDER_PATTERN.sub(lambda m: preserved[int(m.group(1))], stripped)


def collapse_adjacent_style_wraps(text):
    return ADJACENT_STYLE_WRAP_PATTERN.sub(r"\2", text) if "</" in text else text


def split_cue_lines(raw):
    lines = [WHITESPACE_PATTERN.sub(" ", strip_tags_preserving_style(raw_line)).strip() for raw_line in raw.splitlines()]
    return [line for line in lines if line]


EDGE_NOTE_LEADING_PATTERN = re.compile(f"^[{MUSIC_NOTE_CHARS}\\s]+")
EDGE_NOTE_TRAILING_PATTERN = re.compile(f"[{MUSIC_NOTE_CHARS}\\s]+$")


def split_full_wrap(text):
    text = text.strip()
    if "<" not in text:
        return text, None
    leading_match = EDGE_NOTE_LEADING_PATTERN.match(text)
    leading = leading_match.group(0) if leading_match else ""
    remainder = text[len(leading):]
    trailing_match = EDGE_NOTE_TRAILING_PATTERN.search(remainder)
    trailing = trailing_match.group(0) if trailing_match else ""
    core = remainder[:len(remainder) - len(trailing)] if trailing else remainder
    match = FULL_WRAP_PATTERN.match(core)
    if not match or STYLE_TAG_PATTERN.search(match.group(2)):
        return text, None
    return f"{leading}{match.group(2).strip()}{trailing}", match.group(1).lower()


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
        cue_id = len(cues) + 1
        cue_lines = split_cue_lines("\n".join(lines[time_line_idx + 1:]))
        if strip_sdh_enabled:
            cleaned = strip_cue_sdh(cue_lines)
            if cleaned != cue_lines:
                sdh_stats["stripped" if cleaned else "dropped"] += 1
            cue_lines = cleaned
        text = collapse_adjacent_style_wraps(" ".join(cue_lines))
        if text:
            cues.append({"id": cue_id, "start": time_match.group(1), "end": time_match.group(2), "text": text})
    return cues, sdh_stats


def split_dialogue(text):
    if "-" not in text:
        return [text]
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
        elif char in ('”', '」', '»') or char in BRACKET_CLOSE_CHARS:
            is_pending = False
        elif char in BRACKET_OPEN_CHARS:
            is_pending = True
    return is_pending


def is_isolated_short(text, latin_source=True):
    if not ISOLATED_MERGE_MAX_WORDS:
        return False
    if latin_source:
        return len(WORD_TOKEN_PATTERN.findall(text)) <= ISOLATED_MERGE_MAX_WORDS
    return len(text.strip()) <= ISOLATED_MAX_CHARS_NON_LATIN


def assign_merge_sides(segments, latin_source=True):
    for i, seg in enumerate(segments):
        if seg["resolved"] is not None or seg["music"] or not is_isolated_short(seg["text"], latin_source):
            continue
        if i + 1 < len(segments) and not segments[i + 1]["music"]:
            gap_next = time_to_ms(segments[i + 1]["start"]) - time_to_ms(seg["end"])
            if gap_next <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "next"
                continue
        if i > 0 and not segments[i - 1]["music"]:
            gap_prev = time_to_ms(seg["start"]) - time_to_ms(segments[i - 1]["end"])
            if gap_prev <= SCENE_ADJACENCY_MS:
                seg["merge_side"] = "prev"
    return segments


def merge_reason(prev_seg, curr_seg, latin_source=True, continues=continues_by_punctuation):
    if prev_seg["music"] != curr_seg["music"]:
        return None
    if prev_seg["cue_id"] == curr_seg["cue_id"]:
        return "dash" if is_short_reply(curr_seg["text"], latin_source) else None
    if curr_seg["music"]:
        return "music" if music_continuation(curr_seg["text"]) else None
    if prev_seg.get("merge_side") == "next" or curr_seg.get("merge_side") == "prev":
        return "marker"
    gap = time_to_ms(curr_seg["start"]) - time_to_ms(prev_seg["end"])
    if gap < -MAX_OVERLAP_MS:
        return None
    if curr_seg["head"] & HEAD_CLOSER or prev_seg["tail"] & TAIL_OPENER:
        return "gap" if gap <= SCENE_ADJACENCY_MS else None
    return "gap" if continues(prev_seg, curr_seg, gap) else None


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


def has_content_chars(text):
    return any(char.isalnum() or char == "_" for char in text)


def resolve_segment(text, glossary, latin_source):
    if not has_content_chars(text):
        return text
    resolved = find_pure_glossary_line(text, glossary, latin_source)
    if not resolved and latin_source:
        resolved = find_stutter_resolution(text, glossary)
    return resolved or None


def build_segments(cues, glossary, latin_source=True):
    segments = []
    for cue in cues:
        body, cue_wrap = split_full_wrap(cue["text"])
        parts = split_dialogue(body)
        for dash_index, part in enumerate(parts):
            text, style_wrap = (part, cue_wrap) if cue_wrap or len(parts) == 1 else split_full_wrap(part)
            resolved = resolve_segment(text, glossary, latin_source)
            text = text if resolved is not None or not latin_source else strip_letter_stutter(text)
            segments.append({"cue_id": cue["id"], "text": text, "start": cue["start"], "end": cue["end"],
                              "resolved": resolved, "dash_index": dash_index, "style_wrap": style_wrap,
                              "tail": classify_tail(text), "head": classify_head(text), "music": is_music_segment(text)})
    return assign_merge_sides(segments, latin_source)


QUOTE_PENDING_LIMIT = 10


def group_segments(segments, latin_source=True, continues=continues_by_punctuation):
    groups = []
    current = []
    quote_pending = False
    quote_span = 0
    for seg in segments:
        if seg["resolved"] is not None:
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
                reason = merge_reason(current[-1], seg, latin_source, continues)
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


def assign_marker_ids(spans):
    marked_by_cue = {}
    for i, span in enumerate(spans):
        if span["boundary"] == "marker":
            marked_by_cue.setdefault(span["id"], []).append(i)
    for indices in marked_by_cue.values():
        needs_suffix = len(indices) > 1
        for pos, idx in enumerate(indices):
            spans[idx]["marker_id"] = f"{spans[idx]['id']}.{pos + 1}" if needs_suffix else str(spans[idx]["id"])
    for span in spans:
        span.setdefault("marker_id", str(span["id"]))


def join_group_text(group, spans, is_music_group):
    pieces = []
    for i, seg in enumerate(group):
        piece = strip_edge_notes(seg["text"]) if is_music_group else seg["text"]
        if spans[i]["boundary"] == "marker":
            marker = f"{MARKER_TEMPLATE.format(spans[i]['marker_id'])} "
            pieces.append(f" {marker}" if i > 0 else marker)
        elif i > 0:
            pieces.append(" ")
        pieces.append(piece)
    return "".join(pieces).strip()


def unit_kind(group):
    return "music" if any(seg["music"] for seg in group) else "dialogue"


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


def is_passthrough(group):
    return len(group) == 1 and group[0]["resolved"] is not None


def fuse_music_groups(groups):
    fused, open_group = [], None
    for group in groups:
        if is_passthrough(group):
            fused.append(group)
            open_group = None
        elif open_group is not None:
            open_group.extend(group)
        else:
            open_group = list(group)
            fused.append(open_group)
    return fused


def build_units(cues, glossary, latin_source=True, continues=continues_by_punctuation):
    groups = group_segments(build_segments(cues, glossary, latin_source), latin_source, continues)
    units, chapters, marker_merges, unit_id = [], [], 0, 0
    for chapter_index, raw_chapter in enumerate(chapterize(groups), start=1):
        is_music_chapter = raw_chapter["kind"] == "music"
        member_groups = fuse_music_groups(raw_chapter["groups"]) if is_music_chapter else raw_chapter["groups"]
        unit_ids = []
        for group in member_groups:
            unit_id += 1
            spans = [{"id": s["cue_id"], "start": s["start"], "end": s["end"], "text": s["text"],
                       "boundary": "marker" if (is_music_chapter or s.get("marker_boundary")) else None,
                       "dash_index": s.get("dash_index", 0), "style_wrap": s.get("style_wrap"),
                       "kind": "music" if s["music"] else "dialogue"} for s in group]
            assign_marker_ids(spans)
            marker_merges += sum(1 for s in group if s.get("marker_boundary"))
            if is_passthrough(group):
                units.append({"id": unit_id, "spans": spans, "text": "", "term_matches": [], "resolved": group[0]["resolved"]})
            else:
                text = join_group_text(group, spans, is_music_chapter)
                units.append({"id": unit_id, "spans": spans, "text": text, "term_matches": match_glossary_terms(text, glossary), "resolved": None})
            unit_ids.append(unit_id)
        chapters.append({"id": chapter_index, "kind": raw_chapter["kind"], "unit_ids": unit_ids})
    return units, chapters, marker_merges


def extract(content, glossary, strip_sdh_enabled=True, source_lang="en"):
    latin_source = is_latin_source(source_lang)
    cues, sdh_stats = parse_srt(content, strip_sdh_enabled and base_language(source_lang) in SDH_LANGS)
    if not cues:
        return {"success": False, "reason": "no_cues_parsed", "cues": [], "units": [], "chapters": [], "sdh_removed": sdh_stats, "marker_merges": 0}
    units, chapters, marker_merges = build_units(cues, glossary, latin_source, continuation_for(source_lang))
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
