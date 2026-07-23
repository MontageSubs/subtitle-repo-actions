#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import sys

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.7
DEBUG_ENV = "DEBUG"

SYSTEM_PROMPT = """
You compile subtitle-team background notes from structured Wikipedia/TMDB JSON (lead, infobox, plot per language, cast, tmdb_credits, tmdb_detail), covering film/series/documentary alike.

# General Rules

1. Output language: Simplified Chinese
2. Forbidden symbols: no LaTeX, em/en dashes, semicolons, emoji; arrows must be plain text -->, never use → or other special arrow glyphs
3. Source priority: plot[original_language] > plot.en > plot.zh (reference only, may mistranslate or leave terms untranslated) > plot.fr/de/es (gap-fill only)
4. Grounding: every concrete detail (dates, numbers, locations, fates, stated motive/method) must trace to a source; never invent an unstated motive/method; never infer a specific year from release-year metadata alone — use "当代" if the source gives none; match the source's own vagueness, don't make vague content more specific than the source
5. Output structure: emit the two 人物与译名对照 tables first, then 情节线, 背景故事, 剧情, 主题, in that order. Exactly five final headers, verbatim, nothing else: ## 人物与译名对照 / ## 情节线 / ## 背景故事 / ## 剧情 / ## 主题
6. Internal method (never print step labels, drafts, or reasoning trace): draft --> verify the five headers appear exactly, both tables have header row + separator row, no unescaped `|` remains in any cell --> recheck against rules 3 and 4 --> output final only, no English scratch notes

# Section Rules

## 人物与译名对照

Emit both tables first so later sections reuse the same names consistently.

**Table 1: 演职员与专有名词**

| 原文 | 建议译名 | 身份/关系 | 理由 |
|---|---|---|---|

This exact header row and separator row are required. Include only entities that actually appear in the story: named cast members and their characters, and every story-named entity/org/place/object. Do NOT include director/writer/crew here — they belong to Table 2.
- 身份/关系: one phrase covering the character's identity plus their relation to other already-listed characters (e.g. "饰演角色的兄长", "饰演角色的上级"), not identity alone
- 理由 (3-8 chars): 意译/音译/沿用/存疑 etc.

**Table 2: 制作信息**

| 原文 | 建议译名 | 职务 |
|---|---|---|

This exact header row and separator row are required. Always include the director if present in tmdb_credits. Order by role priority, skipping only roles missing from the source: 发行公司 > 制作公司 > 导演 > 制片人 > 编剧 > 摄影 > 剪辑 > 配乐 > and any other important positions (series may instead have 出品方/总导演/编剧统筹 — map to the source's actual role names).

**Shared translation rule for both tables:**
Prefer an entity's existing common Simplified Chinese rendering (e.g. Walt Disney Pictures --> 华特迪士尼影业, James Wan --> 温子仁, not a literal transliteration). Otherwise, translate meaningful/descriptive words semantically and transliterate proper or mythological names by convention, then append the role suffix (影业/公司/工作室 etc) matching the source. Only keep untranslated a pure brand abbreviation with no descriptive or proper-noun content (e.g. HBO) — never leave a name untranslated merely out of uncertainty.

## 情节线
One line, arrows follow General Rule 2.

## 背景故事
**年代：**/**地点：**/**设定背景：** 1-2 sentences each, based on whatever the source actually provides; if the source doesn't specify one, write a vague placeholder like "来源未特别说明" rather than guessing a specific value.

## 剧情
Continuous prose, no bullets/bold labels. New paragraph only at a genuine act/scene break (or between segments for anthology/multi-part works). Decisive actions only, skip secondary description. Use the tables' names; first mention 中文（Original）, later Chinese only.

## 主题
(internal reasoning — never print step labels; output flowing paragraphs only):
1. Origin: name the specific tradition/movement/convention this premise draws from, precise enough to distinguish it from same-genre peers (approximate era + defining device). Then name the causal mechanism (cognitive/social/historical/formal) behind its audience effect — the "why", not a label.
2. Bridge: one sentence grounding that mechanism in a specific detail this work actually establishes.
3. Structural-personal fusion (mandatory): take one fact about a role/institution/function and one fact about a character's inner experience or choice that the work never explicitly connects; fuse them via a NAMED real theory/concept (psychology/sociology/philosophy/history/media studies/etc.) into a claim neither fact implies alone. Justify the fusion with one specific already-established action or decision as evidence, not an abstract contrast.
4. Closing synthesis: a second, sharper irony beyond step 1's mechanism — reframe an apparent assumption of the work as its opposite, resolved through one concrete object/action/moment already established in 剧情.

Every abstract claim must anchor to a concrete, already-established detail. No free-floating claims.

# Constraints
- 主题 allows broad interpretation but must stay consistent with the plot; no real names as analogies.
- Exclude marketing/box-office/reception from 剧情.
""".strip()

DEFAULT_LANGUAGE_PRIORITY = ("en", "zh", "fr", "de", "es")


def parse_language_priority(raw):
    if not raw:
        return DEFAULT_LANGUAGE_PRIORITY
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def resolve_languages(original_language, language_priority, language_limit):
    order = list(dict.fromkeys([original_language, *language_priority]))
    return order[:language_limit] if language_limit else order


def build_user_payload(fetch_result, original_language, language_priority, language_limit):
    languages = resolve_languages(original_language, language_priority, language_limit)
    plot = fetch_result.get("plot") or {}
    return {
        "original_language": original_language,
        "lead": fetch_result.get("lead"),
        "infobox": fetch_result.get("infobox"),
        "plot": {lang: plot[lang] for lang in languages if lang in plot},
        "cast": fetch_result.get("cast"),
        "tmdb_credits": fetch_result.get("tmdb_credits"),
        "tmdb_detail": fetch_result.get("tmdb_detail"),
    }


def build_messages(fetch_result, original_language, language_priority, language_limit):
    payload = build_user_payload(fetch_result, original_language, language_priority, language_limit)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def log(message):
    print(message, file=sys.stderr)


def is_debug(cli_debug):
    return cli_debug or os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes")


def debug_dump(messages, language_limit, languages_used):
    log(f"debug: language_limit={language_limit} languages_used={languages_used}")
    for message in messages:
        log(f"debug: [{message['role']}] {len(message['content'])} chars")
    log("debug: assembled messages —")
    log(json.dumps(messages, ensure_ascii=False, indent=2))


def fail(reason, detail=None):
    print(json.dumps({"success": False, "reason": reason, "detail": detail}, ensure_ascii=False), file=sys.stdout)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-data", default=None, help="JSON from wiki_tmdb_fetch.py")
    parser.add_argument("--original-language", default="en")
    parser.add_argument("--language-priority", default=None,
                         help="comma-separated language codes, e.g. en,zh,fr,de,es,ja")
    parser.add_argument("--language-limit", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.wiki_data:
        try:
            fetch_result = json.loads(args.wiki_data)
        except json.JSONDecodeError as e:
            fail("invalid_input", str(e))
    else:
        try:
            raw_input = sys.stdin.read()
            fetch_result = json.loads(raw_input) if raw_input.strip() else {}
        except json.JSONDecodeError as e:
            fail("invalid_input", str(e))

    if not fetch_result.get("success"):
        fail("upstream_failed", fetch_result.get("reason"))

    language_priority = parse_language_priority(args.language_priority)
    messages = build_messages(fetch_result, args.original_language, language_priority, args.language_limit)

    if is_debug(args.debug):
        languages_used = resolve_languages(args.original_language, language_priority, args.language_limit)
        languages_used = [lang for lang in languages_used if lang in (fetch_result.get("plot") or {})]
        debug_dump(messages, args.language_limit, languages_used)

    print(json.dumps({
        "success": True,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
