#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: synopsis_pipeline.py
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/wiki/
#
# Description / 描述:
#   Wikipedia -> LLM 剧情摘要工具的进程内编排层。将 wiki_tmdb_fetch、
#   prompt_build、llm_core（跨目录）、synopsis_render 四个模块直接以函数
#   调用的方式串联，供 actions/init/main.py 与 actions/synopsis/main.py
#   两个入口共用，避免各自重复实现串联逻辑或退化为多进程 subprocess 调用。
#
# Usage / 用法:
#   from synopsis_pipeline import run
#   result = run(tmdb_result, output_dir="docs/synopsis", tmdb_token=...)
# ============================================================================
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "llm"))

import wiki_tmdb_fetch
import prompt_build
import synopsis_render
import llm_core


def log(message):
    print(message, file=sys.stderr)


def run(tmdb_result, output_dir, tmdb_token,
        google_token=None, google_model=None, thinking_budget=None,
        hf_token=None, hf_model=None,
        language_priority=None, language_limit=None,
        max_tokens=None, temperature=None, with_glossary=True, debug=False):
    original_language = tmdb_result.get("original_language") or "en"
    plot_languages = language_priority or wiki_tmdb_fetch.DEFAULT_LANGUAGE_PRIORITY

    wiki_result = wiki_tmdb_fetch.fetch(
        imdb_id=tmdb_result["imdb_id"], tmdb_id=tmdb_result["tmdb_id"],
        media_type=tmdb_result["media_type"], original_language=original_language,
        tmdb_token=tmdb_token, language_priority=plot_languages, language_limit=language_limit,
    )
    if not wiki_result["success"]:
        log(f"synopsis pipeline: wiki fetch failed ({wiki_result['reason']})")
        return {"stage": "wiki", **wiki_result}

    messages = prompt_build.build_messages(wiki_result, original_language, plot_languages, language_limit)
    llm_result = llm_core.complete(
        messages=messages,
        max_tokens=max_tokens or prompt_build.DEFAULT_MAX_TOKENS,
        temperature=prompt_build.DEFAULT_TEMPERATURE if temperature is None else temperature,
        google_token=llm_core.resolve_google_token(google_token),
        google_model=llm_core.resolve_google_model(google_model),
        thinking_budget=llm_core.resolve_google_thinking_budget(thinking_budget),
        hf_token=llm_core.resolve_hf_token(hf_token),
        hf_model=llm_core.resolve_hf_model(hf_model),
        debug=debug,
    )
    if not llm_result["success"]:
        log(f"synopsis pipeline: llm failed ({llm_result['reason']})")
        return {"stage": "llm", **llm_result}

    rendered = synopsis_render.render(
        title_en=tmdb_result["title_en"],
        title_zh=tmdb_result["title_zh"] or tmdb_result["title_en"],
        year=tmdb_result["year"], wiki_result=wiki_result, llm_result=llm_result,
        output_dir=output_dir, with_glossary=with_glossary,
    )
    log(f"synopsis pipeline: {'success' if rendered['success'] else 'render failed (' + str(rendered['reason']) + ')'}")
    return {"stage": "render", **rendered}
