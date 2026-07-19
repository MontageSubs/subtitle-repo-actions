#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: tmdb_lookup.py
# Version: 1.1.4
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/tmdb/search/
#
# Description / 描述:
#    Resolves and validates a repository name (format: EnglishTitle_Year)
#    against TMDB, then fetches the title's metadata. Supports both movies
#    and TV series via TMDB's /search/multi endpoint (language=en-US) to
#    validate naming conventions (case-sensitive) against the repository
#    name. Only when the title and year both match does it proceed to fetch
#    full detail (Chinese title, overview, poster, IMDb ID) via
#    /movie/{id} or /tv/{id}, depending on media_type.
#    根据仓库名（格式：英文片名_年份）在TMDB中解析并校验，通过后拉取该
#    影视内容的元数据。同时支持电影与剧集，通过TMDB的/search/multi接口
#    （language=en-US）搜索英文标题，与仓库名做大小写敏感的命名规范校验；
#    仅当标题与年份均一致时，才根据media_type继续调用/movie/{id}或
#    /tv/{id}拉取完整详情（中文标题、简介、海报、IMDb ID）。
#
# Usage / 用法:
#    python tmdb_lookup.py Cosmos_Laundromat_2015
#    python tmdb_lookup.py It_Was_Just_an_Accident_2025 --tmdb-read-access-token KEY
#
#    The key is read from --tmdb-read-access-token, falling back to the TMDB_READ_ACCESS_TOKEN
#    environment variable.
#    密钥可通过--tmdb-read-access-token传入，缺省时读取TMDB_READ_ACCESS_TOKEN环境变量。
#
#    Note: The repository name's title segment must match TMDB's en-US title
#    exactly, including case (e.g. "an" not "An"). A mismatch aborts before
#    any detail request is made, to avoid spending an extra API call on a
#    result that will be discarded. /search/multi may return movies, TV
#    series, and people in the same result list; person results are
#    filtered out before matching.
#    注意：仓库名中的片名部分须与TMDB的en-US标题逐字符（含大小写）一致，
#    例如"an"而非"An"。一旦不匹配，将在发出详情请求前中止，以避免为注定
#    被丢弃的结果多消耗一次API调用。/search/multi可能在同一结果列表中
#    返回电影、剧集与人物，匹配前会先过滤掉人物类结果。
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - The search query, every candidate TMDB returned, and the final
#        success/failure status / 搜索查询语句、TMDB返回的每一条候选结果，
#        以及最终的成功或失败状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object, including "media_type" ("movie" or "tv").
#        On failure, "expected_title"/"expected_year" are only populated
#        for title_mismatch/year_mismatch, so the caller can render the
#        correct repository name. / 单个JSON对象，包含"media_type"
#        （"movie"或"tv"）。仅当reason为title_mismatch或year_mismatch时
#        "expected_title"/"expected_year"才有内容，供调用方渲染正确的
#        仓库名。
#
# Example execution / 执行示例:
#    $ python tmdb_lookup.py Cosmos_Laundromat_2015
#    query (tmdb search): Cosmos Laundromat (2015)
#    tmdb search results: 1
#      [358332] movie: Cosmos Laundromat (2015-08-10)
#    query (tmdb detail): movie/358332
#    status: success
#    {"success": true, "reason": null, "media_type": "movie", "tmdb_id": 358332, "imdb_id": "tt4957236", ...}
#
# Exit codes / 退出码:
#    0    normal completion, regardless of whether success is true or false
#         正常完成，无论success为true还是false
#    130  interrupted by Ctrl+C / 被Ctrl+C中断
#
# ============================================================================
import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

TMDB_READ_ACCESS_TOKEN_ENV = "TMDB_READ_ACCESS_TOKEN"
TMDB_SEARCH_ENDPOINT = "https://api.themoviedb.org/3/search/multi"
TMDB_DETAIL_ENDPOINT = "https://api.themoviedb.org/3/{media_type}/{id}"

REPO_NAME_PATTERN = re.compile(r"^(.+)[_-](\d{4})$")

SUPPORTED_MEDIA_TYPES = ("movie", "tv")

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
    return re.sub(r"[_-]+", " ", title_part).strip(), int(year_part)


LOOSE_YEAR_PATTERN = re.compile(r"(19\d{2}|20\d{2})")


def parse_repo_name_loose(repo_name):
    match = LOOSE_YEAR_PATTERN.search(repo_name)
    if not match:
        return None, None
    year = int(match.group(1))
    remainder = repo_name[:match.start()] + repo_name[match.end():]
    remainder = remainder.strip("_- ")
    title_guess = re.sub(r"[_\-]+", " ", remainder).strip()
    if not title_guess:
        return None, None
    return title_guess, year


def classify_http_error(code):
    if code in (401, 403):
        return ERROR_AUTH
    if code == 429:
        return ERROR_RATE_LIMIT
    if code >= 500:
        return ERROR_SERVER
    return ERROR_NETWORK


def call_tmdb(url, read_access_token):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {read_access_token}",
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


def candidate_title(candidate):
    media_type = candidate.get("media_type")
    if media_type == "movie":
        return candidate.get("title", ""), candidate.get("release_date", "")
    if media_type == "tv":
        return candidate.get("name", ""), candidate.get("first_air_date", "")
    return "", ""

def normalize_title(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if ord(c) < 128)

TITLE_SIMILARITY_THRESHOLD = 0.5


def title_similarity(a, b):
    a, b = normalize_title(a).lower(), normalize_title(b).lower()
    return difflib.SequenceMatcher(None, a, b).ratio()

def search_title(title, year, read_access_token):
    params = urllib.parse.urlencode({
        "query": title,
        "year": year,
        "language": "en-US",
    })
    url = f"{TMDB_SEARCH_ENDPOINT}?{params}"
    log(f"query (tmdb search): {title} ({year})")
    body, error = call_tmdb(url, read_access_token)
    if error:
        return None, error
    all_results = body.get("results", [])
    results = [r for r in all_results if r.get("media_type") in SUPPORTED_MEDIA_TYPES]
    log(f"tmdb search results: {len(results)} (of {len(all_results)} total, non movie/tv filtered out)")
    for r in results:
        name, date = candidate_title(r)
        log(f"  [{r.get('id')}] {r.get('media_type')}: {name} ({date})")
    if not results:
        return None, None
    scored = []
    for r in results:
        name, date = candidate_title(r)
        r_year = int((date or "0000")[:4] or 0)
        similarity = title_similarity(title, name)
        if r_year and abs(r_year - year) <= 1 and similarity >= TITLE_SIMILARITY_THRESHOLD:
            scored.append((similarity, abs(r_year - year), r))
    if not scored:
        log("tmdb search results: none within ±1 year AND above title-similarity threshold, treating as not found")
        return None, None
    scored.sort(key=lambda x: (-x[0], x[1]))
    log(f"best candidate: [{scored[0][2].get('id')}] similarity={scored[0][0]:.2f} year_diff={scored[0][1]}")
    return scored[0][2], None


def get_detail(media_type, tmdb_id, read_access_token):
    params = urllib.parse.urlencode({
        "language": "zh-CN",
        "append_to_response": "external_ids",
    })
    url = f"{TMDB_DETAIL_ENDPOINT.format(media_type=media_type, id=tmdb_id)}?{params}"
    log(f"query (tmdb detail): {media_type}/{tmdb_id}")
    return call_tmdb(url, read_access_token)


def empty_result(reason, **extra):
    result = {
        "success": False,
        "reason": reason,
        "needs_rename": False,
        "canonical_repo_name": None,
        "media_type": None,
        "tmdb_id": None,
        "imdb_id": None,
        "title_en": None,
        "title_zh": None,
        "year": None,
        "overview_zh": None,
        "poster_path": None,
        "input_title": None,
        "input_year": None,
    }
    result.update(extra)
    return result


def resolve(repo_name, tmdb_read_access_token=None):
    if not tmdb_read_access_token:
        log(f"status: failed ({ERROR_NO_TOKEN})")
        return empty_result(ERROR_NO_TOKEN, input_repo_name=repo_name)

    title, year = parse_repo_name(repo_name)
    loose = False
    if title is None:
        title, year = parse_repo_name_loose(repo_name)
        loose = True
        if title is None:
            log(f"status: failed ({ERROR_INVALID_REPO_NAME}, no year found even in loose parse)")
            return empty_result(ERROR_INVALID_REPO_NAME, input_repo_name=repo_name)
        log(f"format invalid, attempting loose-parse rename suggestion: {title!r} ({year})")

    candidate, error = search_title(title, year, tmdb_read_access_token)
    if error:
        log(f"tmdb error: {error['type']} ({error['detail']})")
        return empty_result(error["type"], input_repo_name=repo_name)

    if not candidate:
        if loose:
            log(f"status: failed ({ERROR_INVALID_REPO_NAME}, loose-parse search found nothing)")
            return empty_result(ERROR_INVALID_REPO_NAME, input_repo_name=repo_name)
        log(f"status: failed ({ERROR_NOT_FOUND})")
        return empty_result(
            ERROR_NOT_FOUND, input_repo_name=repo_name,
            input_title=title, input_year=year,
        )

    media_type = candidate["media_type"]
    tmdb_title, release_date = candidate_title(candidate)
    tmdb_year = int((release_date or "0000")[:4] or 0)

    if loose:
        log(f"status: failed ({ERROR_INVALID_REPO_NAME}, suggesting rename based on loose-parse match)")
        return empty_result(
            ERROR_INVALID_REPO_NAME,
            input_repo_name=repo_name,
            expected_title=tmdb_title,
            expected_year=tmdb_year,
        )

    if normalize_title(tmdb_title) != normalize_title(title):
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
    detail, error = get_detail(media_type, tmdb_id, tmdb_read_access_token)
    if error:
        log(f"tmdb error: {error['type']} ({error['detail']})")
        return empty_result(error["type"], input_repo_name=repo_name)

    title_zh = detail.get("title") if media_type == "movie" else detail.get("name")

    canonical_repo_name = "{}_{}".format(tmdb_title.replace(" ", "_"), tmdb_year)
    needs_rename = canonical_repo_name != repo_name
    if needs_rename:
        log(f"status: success (needs_rename: {repo_name} -> {canonical_repo_name})")
    else:
        log("status: success")
    return {
        "success": True,
        "reason": None,
        "needs_rename": needs_rename,
        "canonical_repo_name": canonical_repo_name if needs_rename else None,
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "imdb_id": detail.get("external_ids", {}).get("imdb_id"),
        "title_en": tmdb_title,
        "title_zh": title_zh,
        "year": tmdb_year,
        "overview_zh": detail.get("overview"),
        "poster_path": detail.get("poster_path"),
    }


def resolve_read_access_token(cli_value):
    if cli_value:
        return cli_value
    return os.environ.get(TMDB_READ_ACCESS_TOKEN_ENV)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_name")
    parser.add_argument("--tmdb-read-access-token", default=None)
    args = parser.parse_args()

    result = resolve(
        repo_name=args.repo_name,
        tmdb_read_access_token=resolve_read_access_token(args.tmdb_read_access_token),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
