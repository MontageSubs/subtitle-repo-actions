#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: douban_id_lookup.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/douban/search/douban_id_lookup.py
#
# Description / 描述:
#    A utility script for resolving Douban movie subject IDs from movie titles
#    using a dual-stage fallback strategy. Integrates Tavily API for primary
#    search with confidence scoring and SerpStack API as fallback when Tavily
#    fails or returns no results.
#    用于从电影标题解析豆瓣电影条目ID的工具脚本。采用两级容错策略，使用
#    Tavily API进行主要搜索并返回置信度分数，在Tavily失败或无结果时降级
#    到SerpStack API。
#
# Usage / 用法:
#    python douban_id_lookup.py "Cosmos Laundromat 2015"
#    python douban_id_lookup.py "Sintel 2010"
#
#    Note: Use English titles with release year for best results. Chinese titles
#    are not recommended as some search APIs have limited support for Chinese
#    character processing and may return incorrect or no results.
#    注意：为获得最佳结果，请使用英文片名加上映年代。不建议使用中文标题，
#    因为某些搜索API对中文字符处理的支持有限，可能返回错误或无结果。
#
# Output / 输出:
#
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - Search queries sent to each provider / 发送到各个提供者的搜索查询
#      - Number of candidates found and their sources / 找到的候选结果数量及其来源
#      - Final resolution status (success/failure) / 最终解析状态（成功/失败）
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - JSON object containing douban_id, provider, confidence status,
#        and candidate list / 包含豆瓣ID、提供者、置信度状态和候选列表的JSON对象
#
#    Example execution / 执行示例:
#      $ python douban_id_lookup.py "Cosmos Laundromat 2015"
#      query (tavily): site:m.douban.com/movie/subject/ site:movie.douban.com/subject/ Cosmos Laundromat 2015
#      query (serpstack): site:m.douban.com/movie/subject/ Cosmos Laundromat 2015
#      result: 1 candidate(s) found via serpstack
#        - https://m.douban.com/movie/subject/26798719/ | 宇宙自助洗衣店- 电影
#      status: success
#      {"status": "success", "reason": null, "douban_id": "26798719", "provider": "serpstack", "candidates": [{"id": "26798719", "url": "https://m.douban.com/movie/subject/26798719/", "title": "宇宙自助洗衣店- 电影", "score": null}], "errors": []}
#
# ============================================================================
import argparse
import collections
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
SERPSTACK_API_KEY_ENV = "SERPSTACK_API_KEY"

DOUBAN_SITES = [
    "m.douban.com/movie/subject/",
    "movie.douban.com/subject/",
]

# Extract Douban subject ID from search result URLs using regex pattern.
# 从搜索结果URL中提取豆瓣条目ID。
SUBJECT_URL_PATTERN = re.compile(
    r"https?://(?:m\.douban\.com/movie/subject|movie\.douban\.com/subject)/(\d+)"
)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
SERPSTACK_ENDPOINT = "https://api.serpstack.com/search"

# Confidence threshold for Tavily API search results (0-1 scale).
# Tavily API搜索结果的置信度阈值（0-1范围）。
LOW_CONFIDENCE_SCORE_THRESHOLD = 0.3

ERROR_NO_TOKEN = "no_token"
ERROR_AUTH = "auth_error"
ERROR_NETWORK = "network_error"


def log(message):
    print(message, file=sys.stderr)


def build_scoped_query(query_terms, sites):
    site_clause = " ".join(f"site:{s}" for s in sites)
    return f"{site_clause} {query_terms}"


def extract_subject_id(url):
    match = SUBJECT_URL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None

# Call Tavily API to search for Douban subject IDs with confidence scoring.
# 调用Tavily API搜索豆瓣条目ID并返回置信度分数。
def call_tavily(query, api_key):
    if not api_key:
        return [], {"provider": "tavily", "type": ERROR_NO_TOKEN, "detail": "no api key provided"}

    payload = json.dumps({
        "query": query,
        "search_depth": "advanced",
    }).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return [], {"provider": "tavily", "type": ERROR_AUTH, "detail": f"http {e.code}"}
        return [], {"provider": "tavily", "type": ERROR_NETWORK, "detail": f"http {e.code}"}
    except Exception as e:
        return [], {"provider": "tavily", "type": ERROR_NETWORK, "detail": str(e)}

    candidates = []
    for result in body.get("results", []):
        url = result.get("url", "")
        subject_id = extract_subject_id(url)
        if subject_id is None:
            continue
        candidates.append({
            "id": subject_id,
            "url": url,
            "title": result.get("title", ""),
            "score": result.get("score"),
        })
    return candidates, None

# Call SerpStack API as fallback provider when Tavily fails or returns no results.
# 当Tavily失败或无结果时，调用SerpStack API作为备选方案。
def call_serpstack(query, api_key):
    if not api_key:
        return [], {"provider": "serpstack", "type": ERROR_NO_TOKEN, "detail": "no api key provided"}

    params = urllib.parse.urlencode({
        "access_key": api_key,
        "query": query,
    })
    url = f"{SERPSTACK_ENDPOINT}?{params}"

    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return [], {"provider": "serpstack", "type": ERROR_AUTH, "detail": f"http {e.code}"}
        return [], {"provider": "serpstack", "type": ERROR_NETWORK, "detail": f"http {e.code}"}
    except Exception as e:
        return [], {"provider": "serpstack", "type": ERROR_NETWORK, "detail": str(e)}

    request_meta = body.get("request", {})
    if not request_meta.get("success"):
        error_meta = body.get("error", {})
        error_type = error_meta.get("type", "")
        if "key" in error_type or "access" in error_type:
            return [], {"provider": "serpstack", "type": ERROR_AUTH, "detail": error_type}
        return [], {"provider": "serpstack", "type": ERROR_NETWORK, "detail": error_type}

    candidates = []

    featured = body.get("answer_box", {}).get("featured_snippets", [])
    for snippet in featured:
        link = snippet.get("link", "") or snippet.get("display_link", "")
        subject_id = extract_subject_id(link)
        if subject_id is not None:
            candidates.append({
                "id": subject_id,
                "url": link,
                "title": snippet.get("link_title", ""),
                "score": None,
            })

    for result in body.get("organic_results", []):
        url = result.get("url", "")
        subject_id = extract_subject_id(url)
        if subject_id is None:
            continue
        candidates.append({
            "id": subject_id,
            "url": url,
            "title": result.get("title", ""),
            "score": None,
        })

    deduped = {}
    for c in candidates:
        if c["id"] not in deduped:
            deduped[c["id"]] = c
    return list(deduped.values()), None

# Sort candidates by confidence score (descending) and frequency across providers.
# 按置信度分数（降序）和跨提供者出现频率排序候选结果。
def summarize_candidates(candidates):
    counts = collections.Counter(c["id"] for c in candidates)
    return sorted(
        candidates,
        key=lambda c: (c["score"] if c["score"] is not None else 0, counts[c["id"]]),
        reverse=True,
    )

# Determine confidence level based on top result score and candidate uniqueness.
# 根据顶部结果的分数和候选结果的唯一性判定置信度。
def determine_confidence_status(ranked):
    top = ranked[0]
    if top["score"] is not None:
        if top["score"] < LOW_CONFIDENCE_SCORE_THRESHOLD:
            return "low_confidence"
        return "success"

    unique_ids = {c["id"] for c in ranked}
    if len(unique_ids) > 1:
        return "low_confidence"
    return "success"


def determine_error_status(errors):
    types = {e["type"] for e in errors}
    if types == {ERROR_NO_TOKEN}:
        return "error", ERROR_NO_TOKEN
    if types == {ERROR_AUTH}:
        return "error", ERROR_AUTH
    if types == {ERROR_NETWORK}:
        return "error", ERROR_NETWORK
    return "error", "multiple_errors"

# Two-stage fallback strategy: try Tavily first, then SerpStack if no results found.
# 两级容错策略：先尝试Tavily，若无结果则降级到SerpStack。
def resolve(query_terms, tavily_api_key=None, serpstack_api_key=None):
    errors = []

    tavily_query = build_scoped_query(query_terms, DOUBAN_SITES)
    log(f"query (tavily): {tavily_query}")
    candidates, error = call_tavily(tavily_query, tavily_api_key)
    provider_used = "tavily"
    if error:
        log(f"tavily error: {error['type']} ({error['detail']})")
        errors.append(error)

    if not candidates:
        serpstack_query = build_scoped_query(query_terms, DOUBAN_SITES[:1])
        log(f"query (serpstack): {serpstack_query}")
        candidates, error = call_serpstack(serpstack_query, serpstack_api_key)
        provider_used = "serpstack"
        if error:
            log(f"serpstack error: {error['type']} ({error['detail']})")
            errors.append(error)

    if not candidates:
        if errors:
            status, reason = determine_error_status(errors)
            log(f"status: {status} ({reason})")
            return {
                "status": status,
                "reason": reason,
                "douban_id": None,
                "provider": None,
                "candidates": [],
                "errors": errors,
            }

        log("result: no candidates found")
        return {
            "status": "not_found",
            "reason": None,
            "douban_id": None,
            "provider": None,
            "candidates": [],
            "errors": [],
        }

    ranked = summarize_candidates(candidates)

    log(f"result: {len(ranked)} candidate(s) found via {provider_used}")
    for c in ranked:
        score_part = f" score={c['score']:.3f}" if c["score"] is not None else ""
        log(f"  - {c['url']} | {c['title']}{score_part}")

    top = ranked[0]
    status = determine_confidence_status(ranked)
    log(f"status: {status}")

    return {
        "status": status,
        "reason": None,
        "douban_id": top["id"],
        "provider": provider_used,
        "candidates": [
            {"id": c["id"], "url": c["url"], "title": c["title"], "score": c["score"]}
            for c in ranked
        ],
        "errors": errors,
    }


def resolve_api_key(cli_value, env_name):
    if cli_value:
        return cli_value
    return os.environ.get(env_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--tavily-api-key", default=None)
    parser.add_argument("--serpstack-api-key", default=None)
    args = parser.parse_args()

    tavily_api_key = resolve_api_key(args.tavily_api_key, TAVILY_API_KEY_ENV)
    serpstack_api_key = resolve_api_key(args.serpstack_api_key, SERPSTACK_API_KEY_ENV)

    result = resolve(
        query_terms=args.query,
        tavily_api_key=tavily_api_key,
        serpstack_api_key=serpstack_api_key,
    )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
