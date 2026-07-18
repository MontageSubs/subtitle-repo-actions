#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: tmdb_lookup.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tmdb/search/
#
# Description / 描述:
#    Resolves and validates a repository name (format: EnglishTitle_Year)
#    against TMDB, then fetches the movie's metadata. The English title is
#    first searched via TMDB's /search/movie with language=en-US to validate
#    naming conventions (case-sensitive) against the repository name. Only
#    when the title and year both match does it proceed to fetch full detail
#    (Chinese title, overview, poster, IMDb ID) via /movie/{id}.
#    根据仓库名（格式：英文片名_年份）在TMDB中解析并校验，通过后拉取该
#    影片的元数据。先以language=en-US调用TMDB的/search/movie搜索英文标题，
#    与仓库名做大小写敏感的命名规范校验；仅当标题与年份均一致时，才继续
#    调用/movie/{id}拉取完整详情（中文标题、简介、海报、IMDb ID）。
#
# Usage / 用法:
#    python tmdb_lookup.py Cosmos_Laundromat_2015
#    python tmdb_lookup.py It_Was_Just_an_Accident_2025 --tmdb-api-key KEY
#
#    The key is read from --tmdb-api-key, falling back to the TMDB_API_KEY
#    environment variable.
#    密钥可通过--tmdb-api-key传入，缺省时读取TMDB_API_KEY环境变量。
#
#    Note: The repository name's title segment must match TMDB's en-US title
#    exactly, including case (e.g. "an" not "An"). A mismatch aborts before
#    any detail request is made, to avoid spending an extra API call on a
#    result that will be discarded.
#    注意：仓库名中的片名部分须与TMDB的en-US标题逐字符（含大小写）一致，
#    例如"an"而非"An"。一旦不匹配，将在发出详情请求前中止，以避免为注定
#    被丢弃的结果多消耗一次API调用。
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - The search query, every candidate TMDB returned, and the final
#        success/failure status / 搜索查询语句、TMDB返回的每一条候选结果，
#        以及最终的成功或失败状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object. On failure, "expected_title"/"expected_year"
#        are only populated for title_mismatch/year_mismatch, so the caller
#        can render the correct repository name. / 单个JSON对象。仅当
#        reason为title_mismatch或year_mismatch时"expected_title"/
#        "expected_year"才有内容，供调用方渲染正确的仓库名。
#
# Example execution / 执行示例:
#    $ python tmdb_lookup.py Cosmos_Laundromat_2015
#    query (tmdb search): Cosmos Laundromat (2015)
#    tmdb search results: 1
#      [358332] Cosmos Laundromat (2015-08-10)
#    query (tmdb detail): 358332
#    status: success
#    {"success": true, "reason": null, "tmdb_id": 358332, "imdb_id": "tt4957236", ...}
#
# Exit codes / 退出码:
#    0    normal completion, regardless of whether success is true or false
#         正常完成，无论success为true还是false
#    130  interrupted by Ctrl+C / 被Ctrl+C中断
#
# ============================================================================
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TMDB_API_KEY_ENV = "TMDB_API_KEY"
TMDB_SEARCH_ENDPOINT = "https://api.themoviedb.org/3/search/movie"
TMDB_DETAIL_ENDPOINT = "https://api.themoviedb.org/3/movie/{id}"

REPO_NAME_PATTERN = re.compile(r"^(.+)_(\d{4})$")

ERROR_INVALID_REPO_NAME = "invalid_repo_name"
ERROR_NOT_FOUND = "not_found"
ERROR_TITLE_MISMATCH = "title_mismatch"
ERROR_YEAR_MISMATCH = "year_mismatch"
ERROR_AUTH = "auth_error"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network_error"
ERROR_NO_TOKEN = "no_token"


def log(message):
    print(message, file=sys.stderr)


def parse_repo_name(repo_name):
    match = REPO_NAME_PATTERN.match(repo_name)
    if not match:
        return None, None
    title_part, year_part = match.groups()
    return title_part.replace("_", " "), int(year_part)


def classify_http_error(code):
    if code in (401, 403):
        return ERROR_AUTH
    if code == 429:
        return ERROR_RATE_LIMIT
    if code >= 500:
        return ERROR_SERVER
    return ERROR_NETWORK


def call_tmdb(url, api_key):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, {"type": classify_http_error(e.code), "detail": f"http {e.code}"}
    except Exception as e:
        return None, {"type": ERROR_NETWORK, "detail": str(e)}


def search_movie(title, year, api_key):
    params = urllib.parse.urlencode({
        "query": title,
        "year": year,
        "language": "en-US",
    })
    url = f"{TMDB_SEARCH_ENDPOINT}?{params}"
    log(f"query (tmdb search): {title} ({year})")
    body, error = call_tmdb(url, api_key)
    if error:
        return None, error
    results = body.get("results", [])
    log(f"tmdb search results: {len(results)}")
    for r in results:
        log(f"  [{r.get('id')}] {r.get('title')} ({r.get('release_date')})")
    if not results:
        return None, None
    return results[0], None


def get_movie_detail(tmdb_id, api_key):
    params = urllib.parse.urlencode({
        "language": "zh-CN",
        "append_to_response": "external_ids",
    })
    url = f"{TMDB_DETAIL_ENDPOINT.format(id=tmdb_id)}?{params}"
    log(f"query (tmdb detail): {tmdb_id}")
    return call_tmdb(url, api_key)


def empty_result(reason, **extra):
    result = {
        "success": False,
        "reason": reason,
        "tmdb_id": None,
        "imdb_id": None,
        "title_en": None,
        "title_zh": None,
        "year": None,
        "overview_zh": None,
        "poster_path": None,
    }
    result.update(extra)
    return result


def resolve(repo_name, tmdb_api_key=None):
    if not tmdb_api_key:
        log(f"status: failed ({ERROR_NO_TOKEN})")
        return empty_result(ERROR_NO_TOKEN, input_repo_name=repo_name)

    title, year = parse_repo_name(repo_name)
    if title is None:
        log(f"status: failed ({ERROR_INVALID_REPO_NAME})")
        return empty_result(ERROR_INVALID_REPO_NAME, input_repo_name=repo_name)

    candidate, error = search_movie(title, year, tmdb_api_key)
    if error:
        log(f"tmdb error: {error['type']} ({error['detail']})")
        return empty_result(error["type"], input_repo_name=repo_name)

    if not candidate:
        log(f"status: failed ({ERROR_NOT_FOUND})")
        return empty_result(ERROR_NOT_FOUND, input_repo_name=repo_name)

    tmdb_title = candidate.get("title", "")
    tmdb_year = int(candidate.get("release_date", "0000")[:4] or 0)

    if tmdb_title != title:
        log(f"status: failed ({ERROR_TITLE_MISMATCH})")
        return empty_result(
            ERROR_TITLE_MISMATCH,
            input_repo_name=repo_name,
            expected_title=tmdb_title,
            expected_year=tmdb_year,
        )

    if tmdb_year != year:
        log(f"status: failed ({ERROR_YEAR_MISMATCH})")
        return empty_result(
            ERROR_YEAR_MISMATCH,
            input_repo_name=repo_name,
            expected_title=tmdb_title,
            expected_year=tmdb_year,
        )

    tmdb_id = candidate["id"]
    detail, error = get_movie_detail(tmdb_id, tmdb_api_key)
    if error:
        log(f"tmdb error: {error['type']} ({error['detail']})")
        return empty_result(error["type"], input_repo_name=repo_name)

    log("status: success")
    return {
        "success": True,
        "reason": None,
        "tmdb_id": tmdb_id,
        "imdb_id": detail.get("external_ids", {}).get("imdb_id"),
        "title_en": tmdb_title,
        "title_zh": detail.get("title"),
        "year": tmdb_year,
        "overview_zh": detail.get("overview"),
        "poster_path": detail.get("poster_path"),
    }


def resolve_api_key(cli_value):
    if cli_value:
        return cli_value
    return os.environ.get(TMDB_API_KEY_ENV)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_name")
    parser.add_argument("--tmdb-api-key", default=None)
    args = parser.parse_args()

    result = resolve(
        repo_name=args.repo_name,
        tmdb_api_key=resolve_api_key(args.tmdb_api_key),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
