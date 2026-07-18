#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: douban_id_lookup.py
# Version: 1.1.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/douban/search/
#
# Description / 描述:
#    Resolves a Douban movie subject ID from a movie title. Tries Tavily first,
#    scoped to Douban's movie subject pages via its include_domains parameter;
#    falls back to SerpStack, scoped via a site:-restricted query, only when
#    Tavily has no key, errors, or returns nothing.
#    根据电影标题解析豆瓣电影条目ID。优先使用Tavily，通过include_domains参数
#    限定豆瓣电影条目页面；仅当Tavily无key、出错或无结果时，才降级到用
#    site:限定查询的SerpStack。
#
# Usage / 用法:
#    python douban_id_lookup.py "Cosmos Laundromat 2015"
#    python douban_id_lookup.py "Sintel 2010" --tavily-api-key KEY --serpstack-api-key KEY
#
#    Keys are read from --tavily-api-key/--serpstack-api-key, falling back to
#    the TAVILY_API_KEY/SERPSTACK_API_KEY environment variables. Either key
#    may be omitted; at least one of the two is required.
#    密钥可通过--tavily-api-key/--serpstack-api-key传入，缺省时读取
#    TAVILY_API_KEY/SERPSTACK_API_KEY环境变量。两个密钥可以只提供一个，
#    但不能都不提供。
#
#    Note: Use English titles with release year for best results. Chinese
#    titles are not recommended as some search APIs have limited support for
#    Chinese character processing and may return incorrect or no results.
#    注意：为获得最佳结果，请使用英文片名加上映年代。不建议使用中文标题，
#    因为某些搜索API对中文字符处理的支持有限，可能返回错误或无结果。
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - Each provider's query, and every raw result it returned whether or
#        not it matched a Douban subject page / 每个提供者的查询语句，以及
#        其返回的每一条原始结果（无论是否命中豆瓣条目页面）
#      - Final success/failure status / 最终的成功或失败状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object. "candidates" is only populated when reason is
#        "low_confidence", so the caller can pick manually; it is otherwise
#        an empty list. / 单个JSON对象。仅当reason为"low_confidence"时
#        "candidates"才有内容供调用方手动挑选，其余情况均为空列表。
#
# Example execution / 执行示例:
#    $ python douban_id_lookup.py "Cosmos Laundromat 2015"
#    query (tavily): Cosmos Laundromat 2015 [domains: m.douban.com/movie/subject/, movie.douban.com/subject/]
#    tavily raw results: 1
#      [tavily] https://m.douban.com/movie/subject/26798719 | 宇宙自助洗衣店- 电影 score=0.748
#    result: 1 candidate(s) found via tavily
#      - https://m.douban.com/movie/subject/26798719 | 宇宙自助洗衣店- 电影 score=0.748
#    status: success
#    {"success": true, "douban_id": "26798719", "provider": "tavily", "reason": null, "candidates": []}
#
# Exit codes / 退出码:
#    0    normal completion, regardless of whether success is true or false
#         正常完成，无论success为true还是false
#    130  interrupted by Ctrl+C / 被Ctrl+C中断
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

SUBJECT_URL_PATTERN = re.compile(
    r"https?://(?:m\.douban\.com/movie/subject|movie\.douban\.com/subject)/(\d+)"
)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
SERPSTACK_ENDPOINT = "https://api.serpstack.com/search"

# Tavily relevance score (0-1) below which a result is treated as unreliable.
# Tavily相关性分数（0-1）低于此值时视为不可靠。
LOW_CONFIDENCE_SCORE_THRESHOLD = 0.3

# reason values surfaced to the caller when success is false:
# no_token, not_found, low_confidence, auth_error, bad_request,
# rate_limit, quota_exceeded, server_error, network_error, multiple_errors
# success为false时呈现给调用方的reason取值如上
ERROR_NO_TOKEN = "no_token"
ERROR_AUTH = "auth_error"
ERROR_BAD_REQUEST = "bad_request"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network_error"


def log(message):
    print(message, file=sys.stderr)


def build_scoped_query(query_terms, sites):
    if len(sites) == 1:
        return f"site:{sites[0]} {query_terms}"
    site_clause = " OR ".join(f"site:{s}" for s in sites)
    return f"({site_clause}) {query_terms}"


def extract_subject_id(url):
    match = SUBJECT_URL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


def log_raw_result(provider, url, title, score, matched):
    score_part = f" score={score:.3f}" if score is not None else ""
    marker = "" if matched else " (no douban id)"
    log(f"  [{provider}] {url} | {title}{score_part}{marker}")


def call_tavily(query, api_key, domains):
    if not api_key:
        return [], {"provider": "tavily", "type": ERROR_NO_TOKEN, "detail": "no api key provided"}

    payload = json.dumps({
        "query": query,
        "search_depth": "advanced",
        "include_domains": domains,
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
        code = e.code
        if code == 401:
            error_type = ERROR_AUTH
        elif code in (400, 403):
            error_type = ERROR_BAD_REQUEST
        elif code == 429:
            error_type = ERROR_RATE_LIMIT
        elif code in (432, 433):
            error_type = ERROR_QUOTA_EXCEEDED
        elif code == 500:
            error_type = ERROR_SERVER
        else:
            error_type = ERROR_NETWORK
        return [], {"provider": "tavily", "type": error_type, "detail": f"http {code}"}
    except Exception as e:
        return [], {"provider": "tavily", "type": ERROR_NETWORK, "detail": str(e)}

    raw_results = body.get("results", [])
    log(f"tavily raw results: {len(raw_results)}")
    candidates = []
    for result in raw_results:
        url = result.get("url", "")
        title = result.get("title", "")
        score = result.get("score")
        subject_id = extract_subject_id(url)
        log_raw_result("tavily", url, title, score, subject_id is not None)
        if subject_id is None:
            continue
        candidates.append({
            "id": subject_id,
            "url": url,
            "title": title,
            "score": score,
        })
    return candidates, None


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
        code = e.code
        if code in (401, 403):
            error_type = ERROR_AUTH
        elif code in (400, 422):
            error_type = ERROR_BAD_REQUEST
        elif code == 429:
            error_type = ERROR_RATE_LIMIT
        elif code == 500:
            error_type = ERROR_SERVER
        else:
            error_type = ERROR_NETWORK
        return [], {"provider": "serpstack", "type": error_type, "detail": f"http {code}"}
    except Exception as e:
        return [], {"provider": "serpstack", "type": ERROR_NETWORK, "detail": str(e)}

    request_meta = body.get("request", {})
    if not request_meta.get("success"):
        error_meta = body.get("error", {})
        error_type_text = error_meta.get("type", "")
        if "key" in error_type_text or "access" in error_type_text:
            error_type = ERROR_AUTH
        elif "limit" in error_type_text or "rate" in error_type_text:
            error_type = ERROR_RATE_LIMIT
        else:
            error_type = ERROR_BAD_REQUEST
        return [], {"provider": "serpstack", "type": error_type, "detail": error_type_text}

    raw_items = []

    featured = body.get("answer_box", {}).get("featured_snippets", [])
    for snippet in featured:
        link = snippet.get("link", "") or snippet.get("display_link", "")
        raw_items.append((link, snippet.get("link_title", "")))

    for result in body.get("organic_results", []):
        raw_items.append((result.get("url", ""), result.get("title", "")))

    log(f"serpstack raw results: {len(raw_items)}")
    candidates = []
    for url, title in raw_items:
        subject_id = extract_subject_id(url)
        log_raw_result("serpstack", url, title, None, subject_id is not None)
        if subject_id is None:
            continue
        candidates.append({
            "id": subject_id,
            "url": url,
            "title": title,
            "score": None,
        })

    deduped = {}
    for c in candidates:
        if c["id"] not in deduped:
            deduped[c["id"]] = c
    return list(deduped.values()), None


# Ranks by score first (unscored SerpStack results sort last), then by how
# many providers agreed on the same id.
# 先按分数排序（SerpStack结果无分数，排在最后），再按被多少提供者同时命中排序。
def summarize_candidates(candidates):
    counts = collections.Counter(c["id"] for c in candidates)
    return sorted(
        candidates,
        key=lambda c: (c["score"] if c["score"] is not None else 0, counts[c["id"]]),
        reverse=True,
    )


# Trusts a single high-scoring result outright; without a score, only trusts
# the result if every candidate agrees on the same id.
# 单个高分结果可直接信任；没有分数时，只有全部候选一致指向同一ID才可信任。
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


def determine_error_reason(errors):
    types = {e["type"] for e in errors}
    if len(types) == 1:
        return next(iter(types))
    return "multiple_errors"


def resolve(query_terms, tavily_api_key=None, serpstack_api_key=None):
    if not tavily_api_key and not serpstack_api_key:
        log(f"status: failed ({ERROR_NO_TOKEN})")
        return {
            "success": False,
            "douban_id": None,
            "provider": None,
            "reason": ERROR_NO_TOKEN,
            "candidates": [],
        }

    errors = []
    candidates = []
    provider_used = None

    if tavily_api_key:
        log(f"query (tavily): {query_terms} [domains: {', '.join(DOUBAN_SITES)}]")
        candidates, error = call_tavily(query_terms, tavily_api_key, DOUBAN_SITES)
        provider_used = "tavily"
        if error:
            log(f"tavily error: {error['type']} ({error['detail']})")
            errors.append(error)

    if not candidates and serpstack_api_key:
        serpstack_query = build_scoped_query(query_terms, DOUBAN_SITES)
        log(f"query (serpstack): {serpstack_query}")
        candidates, error = call_serpstack(serpstack_query, serpstack_api_key)
        provider_used = "serpstack"
        if error:
            log(f"serpstack error: {error['type']} ({error['detail']})")
            errors.append(error)

    if not candidates:
        reason = determine_error_reason(errors) if errors else "not_found"
        log(f"status: failed ({reason})")
        return {
            "success": False,
            "douban_id": None,
            "provider": None,
            "reason": reason,
            "candidates": [],
        }

    ranked = summarize_candidates(candidates)

    log(f"result: {len(ranked)} candidate(s) found via {provider_used}")
    for c in ranked:
        score_part = f" score={c['score']:.3f}" if c["score"] is not None else ""
        log(f"  - {c['url']} | {c['title']}{score_part}")

    confidence = determine_confidence_status(ranked)
    success = confidence == "success"
    log(f"status: {'success' if success else 'failed (low_confidence)'}")

    return {
        "success": success,
        "douban_id": ranked[0]["id"] if success else None,
        "provider": provider_used if success else None,
        "reason": None if success else "low_confidence",
        "candidates": [] if success else [
            {"id": c["id"], "url": c["url"], "title": c["title"], "score": c["score"]}
            for c in ranked
        ],
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
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
