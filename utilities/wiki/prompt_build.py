#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: prompt_build.py
# Version: 1.7.1
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/wiki/
#
# Description / 描述:
#    LLM prompt engineering and construction module.
#    Combines fetched Wikipedia and TMDB metadata into structured message
#    arrays for the LLM core. Separates prompt logic from API execution.
#    LLM 提示词工程与构建模块。负责将抓取到的 Wikipedia 与 TMDB 元数据
#    组装为结构化的 messages 数组供 LLM 核心调用。实现提示词业务逻辑与 
#    API 执行层（llm_core）的代码分离。
#
# Usage / 用法:
#    from prompt_build import build_metadata_prompt
#    
#    messages = build_metadata_prompt(wiki_data, tmdb_data)
#
# Output / 输出:
#    Returns a list of message dictionaries compatible with llm_core.py:
#    返回完全兼容 llm_core.py 格式的消息字典列表：
#    [
#      {"role": "system", "content": "..."},
#      {"role": "user", "content": "..."}
#    ]
# ============================================================================
import argparse
import json
import os
import re
import sys

WHITESPACE_COLLAPSE_PATTERN = re.compile(r"\s+")

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.7
DEBUG_ENV = "DEBUG"

TOKEN_BUDGET_TARGET = 10000
TOKEN_BUDGET_HARD_LIMIT = 14000
CHARS_PER_TOKEN_LATIN = 4
ZH_LANGUAGE_CODE = "zh"
ALWAYS_PROTECTED_LANGUAGES = ("en",)
REDUCIBLE_PLOT_LANGUAGES_ORDER = ("es", "de", "fr")

CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF))

SYSTEM_PROMPT = """
You compile subtitle-team background notes from structured Wikipedia/TMDB JSON, covering film/series/documentary alike.

# General Rules

1. Output language: Simplified Chinese, adhering to the punctuation and writing conventions of Mainland Simplified Chinese.
2. Forbidden symbols: no LaTeX, em/en dashes, semicolons, emoji; arrows must be plain text -->, never use → or other special arrow glyphs
3. Source priority: plot[original_language] > plot.en > plot.zh (reference only, may mistranslate or leave terms untranslated) > plot.fr/de/es (gap-fill only)
4. Grounding: every concrete detail (dates, numbers, locations, fates, stated motive/method, critic/outlet identity) must trace to a source; never invent an unstated one — this applies to plot facts and to reception's critics/outlets alike (see Rule 7); never infer a specific year from release-year metadata alone — if the source gives none, match its own vagueness rather than filling in a specific era.
5. Output structure: emit 简介 first, then the two 人物与译名对照 tables, then 情节线, 背景故事, 剧情, 主题, in that order. Exactly six final headers, verbatim, nothing else: ## 简介 / ## 人物与译名对照 / ## 情节线 / ## 背景故事 / ## 剧情 / ## 主题
6. Internal method (never print step labels, drafts, or reasoning trace): draft --> verify the six headers appear exactly, both tables have header row + separator row, no unescaped `|` remains in any cell --> recheck against rules 3 and 4 --> output final only, no English scratch notes
7. reception (if present) is raw Wikipedia critical-reception prose, separate from plot/lead by design; it may still contain leftover box-office or award-ceremony sentences the extraction missed — silently ignore anything that isn't evaluative commentary. Use it only to deepen 主题, never as 剧情 fact, never let it leak into 剧情. Attribute claims to the outlet/critic phrase as the source states it (e.g. 纽约时报影评人认为); **maintain strict 1:1 accuracy for outlets and critics, never conflating similar entities (e.g. The New Yorker is NOT The New York Times), and never present its opinions as the film's own narrative content.** **Any representation of a critic's view, verbatim or paraphrased, must be enclosed in「」so it's visually separated from your own words. Then build your own claim outside the quotes (如XX所指出的「...」，据此可进一步认为...). Never place your inference right after their name with only a comma, unquoted, as if it were their statement.**

# Section Rules

## 简介
A short blurb in Simplified Chinese, roughly 80-200 characters, one paragraph, no bullets. State only the premise/setup (who, where, what situation) that a synopsis on a streaming platform would reveal; never state facts that only appear deep in 剧情/背景故事.
For narrative works (film/series): never state how the story resolves, twists, or ends — stop at the point of conflict/unknown/choice that creates curiosity, not resolution.
For documentaries: there is no plot climax to withhold — instead stop before revealing the specific conclusion, verdict, or findings the documentary arrives at; state only the question/subject/investigation it undertakes.
`human_overview` (if present) is an existing human/TMDB-sourced blurb for the same film — use it not only as a factual reference but also as inspiration for tone and where such summaries conventionally stop; never copy its wording or sentence structure verbatim. If missing, write from `lead`/`infobox`/`tmdb_credits` alone.

## 人物与译名对照

Both tables below require the exact header row + separator row shown, so later sections can reuse the same names consistently.

**演职员与专有名词**

| 原文 | 建议译名 | 身份/关系 | 理由 |
|---|---|---|---|

Include only entities that actually appear in the story: named cast members and their characters, and every story-named entity/org/place/object. **Exclude crew-only roles (director, writer, etc.) as they belong in the production table; however, individuals who are both cast and crew must be listed in both tables.**
- 身份/关系: one phrase covering the character's identity plus their relation to other already-listed characters (e.g. "饰演角色的兄长", "饰演角色的上级"), not identity alone
- 理由 (3-8 chars): 意译/音译/沿用/存疑 etc.

**制作信息**

| 原文 | 建议译名 | 职务 |
|---|---|---|

Include every crew role actually documented in infobox/tmdb_credits, not only the ones named below — if the source lists a role omitted here (选角导演、服装设计、视觉特效等), still include it using the source's own role name. **If a single entity/person holds multiple roles, merge them into one row and list all roles in the '职务' cell separated by '、' (e.g., '发行公司、制作公司'). Do not create multiple rows for the same entity.** Always include the director if present. Order the commonly-known roles by priority, skipping only ones missing from the source: 发行公司 > 制作公司 > 导演 > 制片人 > 编剧 > 摄影 > 剪辑 > 配乐, then append any other documented role after them (series may instead have 出品方/总导演/编剧统筹).

**Shared translation rule for both tables:**
Priority: (1) Established Mainland Simplified Chinese renderings (avoid Traditional/HK/TW conventions), e.g. Walt Disney Pictures --> 华特迪士尼影业, James Wan --> 温子仁; (2) Professional transliteration for names and semantic translation for terms, appending appropriate suffixes (影业/公司/etc.). Ensure complete translation; no mixed-language entities. For acronyms: established rendering --> translatable expansion --> original letters. Treat infobox.zh as a meaning reference only; final output must be Simplified Chinese.

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
4. Critical grounding (when reception is present): let at least one step above be sharpened or complicated by what critics actually said, attributed per General Rule 7 — not restated as-is, but used as raw material for a deeper reading the reviews themselves didn't spell out.
5. Closing synthesis: a second, sharper irony beyond step 1's mechanism — reframe an apparent assumption of the work as its opposite, resolved through one concrete object/action/moment already established in 剧情.

Every abstract claim must anchor to a concrete, already-established detail. No free-floating claims.

# Constraints
- 主题 allows broad interpretation but must stay consistent with the plot; no real names as analogies.
- Exclude marketing/box-office/reception-as-fact from 剧情; reception may only inform 主题, always attributed.
""".strip()

DEFAULT_LANGUAGE_PRIORITY = ("en", "zh", "fr", "de", "es")


def parse_language_priority(raw):
    if not raw:
        return DEFAULT_LANGUAGE_PRIORITY
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def resolve_languages(original_language, language_priority, language_limit):
    order = list(dict.fromkeys([original_language, *language_priority]))
    return order[:language_limit] if language_limit else order


def normalize_name(name):
    return WHITESPACE_COLLAPSE_PATTERN.sub(" ", (name or "").strip()).casefold()


def collect_known_names(cast, infobox):
    names = set()
    for entries in (cast or {}).values():
        for entry in entries:
            if entry.get("actor"):
                names.add(normalize_name(entry["actor"]))
    for fields in (infobox or {}).values():
        for value in fields.values():
            for item in (value if isinstance(value, list) else [value]):
                names.add(normalize_name(item))
    return names


def dedupe_tmdb_credits(tmdb_credits, cast, infobox):
    if not tmdb_credits:
        return tmdb_credits
    known_names = collect_known_names(cast, infobox)
    crew = {
        role: [name for name in names if normalize_name(name) not in known_names]
        for role, names in (tmdb_credits.get("crew") or {}).items()
    }
    crew = {role: names for role, names in crew.items() if names}
    people = [name for name in (tmdb_credits.get("cast") or []) if normalize_name(name) not in known_names]
    return {"crew": crew, "cast": people}


def build_user_payload(fetch_result, original_language, language_priority, language_limit):
    languages = resolve_languages(original_language, language_priority, language_limit)
    plot = fetch_result.get("plot") or {}
    cast = fetch_result.get("cast")
    infobox = fetch_result.get("infobox")
    return {
        "original_language": original_language,
        "lead": fetch_result.get("lead"),
        "infobox": infobox,
        "plot": {lang: plot[lang] for lang in languages if lang in plot},
        "cast": cast,
        "reception": fetch_result.get("reception"),
        "tmdb_credits": dedupe_tmdb_credits(fetch_result.get("tmdb_credits"), cast, infobox),
        "tmdb_detail": fetch_result.get("tmdb_detail"),
        "human_overview": fetch_result.get("overview_zh") or fetch_result.get("overview_en"),
    }


def is_cjk_char(ch):
    code = ord(ch)
    return any(low <= code <= high for low, high in CJK_RANGES)


def estimate_tokens(text):
    if not text:
        return 0
    cjk_chars = sum(1 for ch in text if is_cjk_char(ch))
    return cjk_chars + (len(text) - cjk_chars) / CHARS_PER_TOKEN_LATIN


def estimate_payload_tokens(payload):
    return estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(json.dumps(payload, ensure_ascii=False))


def enforce_token_budget(payload, original_language):
    protected = {original_language, *ALWAYS_PROTECTED_LANGUAGES}
    reducible = [lang for lang in REDUCIBLE_PLOT_LANGUAGES_ORDER if lang in payload["plot"] and lang not in protected]

    while reducible and estimate_payload_tokens(payload) > TOKEN_BUDGET_TARGET:
        lang = reducible.pop(0)
        del payload["plot"][lang]
        log(f"token budget: dropped plot[{lang}], estimated={estimate_payload_tokens(payload):.0f}")

    if ZH_LANGUAGE_CODE not in protected and ZH_LANGUAGE_CODE in payload["plot"]:
        tokens = estimate_payload_tokens(payload)
        if tokens > TOKEN_BUDGET_HARD_LIMIT:
            del payload["plot"][ZH_LANGUAGE_CODE]
            log(f"token budget: dropped plot[zh] as last resort, estimated={tokens:.0f} exceeded hard limit {TOKEN_BUDGET_HARD_LIMIT}")

    return payload


def build_messages(fetch_result, original_language, language_priority, language_limit):
    payload = build_user_payload(fetch_result, original_language, language_priority, language_limit)
    payload = enforce_token_budget(payload, original_language)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


SCRIPT_NAME = "prompt_build"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def is_debug(cli_debug):
    return cli_debug or os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes")


def debug_dump(messages, language_limit, languages_used):
    log(f"debug: language_limit={language_limit} languages_used={languages_used}")
    for message in messages:
        log(f"debug: [{message['role']}] {len(message['content'])} chars")
    log("debug: assembled messages —")
    log(json.dumps(messages, ensure_ascii=False, indent=2))


def fail(reason, detail=None):
    print(json.dumps({"success": False, "reason": reason, "detail": detail}, ensure_ascii=False))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-language", default="en")
    parser.add_argument("--language-priority", default=None,
                         help="comma-separated language codes, e.g. en,zh,fr,de,es,ja")
    parser.add_argument("--language-limit", type=int, default=None)
    parser.add_argument("--wiki-data", default=None,
                         help="JSON output from wiki_tmdb_fetch.py; reads stdin if omitted")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        fetch_result = json.loads(args.wiki_data) if args.wiki_data is not None else json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        fail("invalid_input", str(e))

    if not fetch_result.get("success"):
        fail("upstream_failed", fetch_result.get("reason"))

    language_priority = parse_language_priority(args.language_priority)
    messages = build_messages(fetch_result, args.original_language, language_priority, args.language_limit)
    total_chars = sum(len(m["content"]) for m in messages)
    estimated_tokens = sum(estimate_tokens(m["content"]) for m in messages)
    log(f"assembled: {len(messages)} messages, {total_chars} chars, ~{estimated_tokens:.0f} tokens (sent), max_tokens={args.max_tokens}")

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
        log("interrupted")
        sys.exit(130)
