#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: orchestrate.py
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
#
# Description / 描述:
#   仓库初始化总控脚本。串联 tmdb_lookup.py 与 douban_id_lookup.py 的结果，
#   按 reason 选择对应 README 模板渲染，并通过 GitHub API 回写仓库的
#   description / topics / homepage。
#
# Usage / 用法:
#   python init/orchestrate.py --repo-name Cosmos_Laundromat_2015 \
#       --github-repository MontageSubs/Cosmos_Laundromat_2015 \
#       --github-token $ORG_ADMIN_TOKEN \
#       --tmdb-read-access-token $TMDB_READ_ACCESS_TOKEN \
#       --tavily-api-key $TAVILY_API_KEY
#
# 设计原则 / Design principle:
#   文案（README 措辞）与逻辑（本脚本）分离：所有面向用户的文本均来自
#   readme/templates/ 下的 .md 文件，本脚本只负责“选文件 + 填变量”，
#   不做任何文本拼接或按语言/段落切割的解析。
# ============================================================================
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "douban", "search"))

import tmdb_lookup  # noqa: E402
import douban_id_lookup  # noqa: E402

TEMPLATES_DIR = os.path.join(REPO_ROOT, "readme", "templates")
ERROR_TEMPLATE = os.path.join(TEMPLATES_DIR, "error", "error.md")
FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "fragments")
RELEASE_TEMPLATE = os.path.join(TEMPLATES_DIR, "release.md")

NAMING_ERROR_REASONS = {"invalid_repo_name", "not_found", "title_mismatch", "year_mismatch"}

GITHUB_API_ENDPOINT = "https://api.github.com/repos/{full_name}"

# Base topics per media type. Douban IDs are never included here — see
# discussion in project notes: topics are for discoverability, not for
# tracking cross-referenced IDs.
# 按内容类型划分的基础 topics。豆瓣 ID 不会出现在 topics 中——topics 是为了
# 便于检索，而非用来记录跨站 ID。
TOPIC_MAP = {
    "movie": [
        "movies", "translation", "movie", "subtitles", "subtitle",
        "chinese", "chinese-translation", "film", "films", "filmmaking",
        "movie-translator",
    ],
    "tv": [
        "tv-series", "translation", "series", "subtitles", "subtitle",
        "chinese", "chinese-translation", "tv-show", "tv-shows",
        "tv-translator",
    ],
}

SLUG_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify_title(title_en):
    # GitHub topics must be lowercase, and may contain hyphens but not
    # spaces; this collapses any run of non-alphanumeric characters into a
    # single hyphen. e.g. "The Backrooms" -> "the-backrooms".
    # GitHub topic 要求小写，可含连字符但不能有空格；这里将任意一段非字母
    # 数字字符折叠为单个连字符。例如 "The Backrooms" -> "the-backrooms"。
    slug = SLUG_INVALID_CHARS_PATTERN.sub("-", title_en.lower()).strip("-")
    return slug


def build_topics(tmdb_result):
    media_type = tmdb_result.get("media_type") or "movie"
    topics = list(TOPIC_MAP.get(media_type, TOPIC_MAP["movie"]))
    slug = slugify_title(tmdb_result["title_en"])
    if slug and slug not in topics:
        topics.append(slug)
    return topics

# Matches the leading <!-- ERROR_COPY_JSON ... --> comment block at the top
# of error.md. This is the only place orchestrate.py parses structure out of
# a template file, and it does so via json.loads on a clearly delimited
# block — not by slicing text ad hoc.
# 匹配 error.md 顶部的 <!-- ERROR_COPY_JSON ... --> 注释块。这是本脚本唯一
# 从模板文件中解析结构的地方，且是对一个边界清晰的代码块用 json.loads 解析，
# 而非临时性的文本切割。
ERROR_COPY_JSON_PATTERN = re.compile(
    r"<!--\s*ERROR_COPY_JSON\s*(.*?)-->\s*(.*)", re.DOTALL
)


def log(message):
    print(message, file=sys.stderr)


def read_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_readme(content):
    with open(os.path.join(os.getcwd(), "README.md"), "w", encoding="utf-8") as f:
        f.write(content)


def render_error_readme(tmdb_result, repo_name):
    reason = tmdb_result["reason"]

    raw = read_template(ERROR_TEMPLATE)
    match = ERROR_COPY_JSON_PATTERN.match(raw)
    if not match:
        log("error.md is missing its ERROR_COPY_JSON block, aborting without README change")
        return False

    copy_table = json.loads(match.group(1))
    body_template = match.group(2)

    if reason not in copy_table:
        log(f"no copy entry for reason={reason} in ERROR_COPY_JSON, aborting without README change")
        return False

    suggested_repo_name = None
    if tmdb_result.get("expected_title") and tmdb_result.get("expected_year"):
        suggested_repo_name = "{}_{}".format(
            tmdb_result["expected_title"].replace(" ", "_"),
            tmdb_result["expected_year"],
        )

    format_kwargs = {}
    for lang in ("zh", "zh_tw", "en"):
        entry = copy_table[reason][lang]
        format_kwargs[f"{lang}_heading"] = entry["heading"]
        format_kwargs[f"{lang}_body"] = entry["body"].format(input_repo_name=repo_name)
        format_kwargs[f"{lang}_hint"] = entry["hint"].format(
            input_repo_name=repo_name,
            suggested_repo_name=suggested_repo_name or "",
        )

    content = body_template.format(**format_kwargs)
    write_readme(content)
    log(f"README rendered from error.md (reason={reason})")
    return True


def build_douban_warning_block(douban_result):
    if douban_result["success"]:
        return ""
    if douban_result["reason"] == "low_confidence":
        candidates_list = "\n".join(
            f"> - [{c['title']}]({c['url']}) (id: {c['id']})"
            for c in douban_result["candidates"]
        )
        return read_template(
            os.path.join(FRAGMENTS_DIR, "douban_low_confidence.md")
        ).format(candidates_list=candidates_list)
    return read_template(os.path.join(FRAGMENTS_DIR, "douban_not_found.md"))


def render_release_readme(repo_name, tmdb_result, douban_result, visibility_ok=True):
    douban_id = douban_result["douban_id"] or "待核实"
    visibility_warning_block = (
        ""
        if visibility_ok
        else read_template(os.path.join(FRAGMENTS_DIR, "visibility_warning.md"))
    )
    douban_url = (
        f"https://m.douban.com/movie/subject/{douban_result['douban_id']}"
        if douban_result["success"]
        else "#"
    )
    content = read_template(RELEASE_TEMPLATE).format(
        title_zh=tmdb_result["title_zh"] or tmdb_result["title_en"],
        title_en=tmdb_result["title_en"],
        year=tmdb_result["year"],
        poster_url=(
            f"https://image.tmdb.org/t/p/w500{tmdb_result['poster_path']}"
            if tmdb_result.get("poster_path")
            else ""
        ),
        overview_zh=tmdb_result["overview_zh"] or "（暂无简介）",
        douban_id=douban_id,
        douban_url=douban_url,
        imdb_id=tmdb_result["imdb_id"] or "N/A",
        imdb_url=(
            f"https://www.imdb.com/title/{tmdb_result['imdb_id']}/"
            if tmdb_result.get("imdb_id")
            else "#"
        ),
        tmdb_id=tmdb_result["tmdb_id"],
        tmdb_url=f"https://www.themoviedb.org/movie/{tmdb_result['tmdb_id']}?language=zh-CN",
        repo_name=repo_name,
        status_badge_text="制作中",
        status_badge_color="orange",
        progress_percent=0,
        version="v1.0",
        douban_warning_block=build_douban_warning_block(douban_result),
        visibility_warning_block=visibility_warning_block,
    )
    write_readme(content)
    log("README rendered from release.md")


def call_github_api(url, github_token, method, payload_dict):
    payload = json.dumps(payload_dict).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        return True, None
    except urllib.error.HTTPError as e:
        return False, {"http_status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:
        return False, {"http_status": None, "body": str(e)}


def update_github_repo_metadata(github_repository, github_token, tmdb_result):
    """Returns visibility_ok: True if the repo is (or was made) public, False
    if it's stuck private and the README should carry a warning.
    返回 visibility_ok：仓库为公开（或已被成功切换为公开）返回 True；若仍为
    私有（自动化未能切换），返回 False，供 README 附带警告。"""
    if not github_token:
        log("no ORG_ADMIN_TOKEN provided, skipping repo metadata update (description/topics/visibility require a PAT with admin scope, not the default GITHUB_TOKEN)")
        return True

    repo_url = GITHUB_API_ENDPOINT.format(full_name=github_repository)

    description = (
        f"{tmdb_result['title_zh']}（{tmdb_result['year']}）中文字幕协作项目 | "
        f"Chinese fansub project for {tmdb_result['title_en']} ({tmdb_result['year']})"
    )
    # No homepage: we intentionally don't link out to TMDB (or anywhere) from
    # the repo's About section.
    # 不设置 homepage：不从仓库 About 区域链接到 TMDB 或任何外部地址。
    ok, err = call_github_api(repo_url, github_token, "PATCH", {
        "description": description,
        "homepage": "",
        "has_discussions": True,
    })
    if ok:
        log("github repo metadata updated (description/has_discussions)")
    else:
        log(f"failed to update repo metadata: {err}")

    topics_ok, topics_err = call_github_api(repo_url + "/topics", github_token, "PUT", {
        "names": build_topics(tmdb_result),
    })
    if topics_ok:
        log("github repo topics updated")
    else:
        log(f"failed to update repo topics: {topics_err}")

    # Attempt to flip the repo to public. Free-plan orgs create repos as
    # private by default; if org policy restricts visibility changes to
    # owners only, GitHub returns 403/422 and we fall back to a README
    # warning instead of failing the whole run.
    # 尝试将仓库切换为公开。免费版组织默认新建为私有；若组织策略将可见性
    # 修改权限限制为仅 owner，GitHub 会返回 403/422，此时不让整个流程失败，
    # 而是改为在 README 中附带警告。
    visibility_ok, vis_err = call_github_api(repo_url, github_token, "PATCH", {
        "private": False,
    })
    if visibility_ok:
        log("repository visibility set to public")
    else:
        log(f"could not switch repository to public, will warn in README: {vis_err}")

    return visibility_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-name", required=True, help="e.g. Cosmos_Laundromat_2015")
    parser.add_argument("--github-repository", required=True, help="e.g. MontageSubs/Cosmos_Laundromat_2015")
    parser.add_argument("--github-token", default=None, help="PAT with repo admin scope, used for description/topics update")
    parser.add_argument("--tmdb-read-access-token", default=None)
    parser.add_argument("--tavily-api-key", default=None)
    parser.add_argument("--serpstack-api-key", default=None)
    args = parser.parse_args()

    tmdb_token = args.tmdb_read_access_token or os.environ.get("TMDB_READ_ACCESS_TOKEN")
    github_token = args.github_token or os.environ.get("ORG_ADMIN_TOKEN")
    tavily_key = args.tavily_api_key or os.environ.get("TAVILY_API_KEY")
    serpstack_key = args.serpstack_api_key or os.environ.get("SERPSTACK_API_KEY")

    tmdb_result = tmdb_lookup.resolve(args.repo_name, tmdb_token)

    if not tmdb_result["success"]:
        reason = tmdb_result["reason"]
        if reason in NAMING_ERROR_REASONS:
            rendered = render_error_readme(tmdb_result, args.repo_name)
            print(json.dumps({"stage": "tmdb", "success": False, "rendered_error_readme": rendered}, ensure_ascii=False))
            sys.exit(0)
        # Infrastructure-level failure (no_token / auth_error / rate_limit /
        # server_error / network_error): this is not the user's naming
        # mistake, so we do NOT touch README.md. Exit non-zero so the Action
        # run shows red and a maintainer notices, instead of silently
        # succeeding with a stale README.
        # 基础设施类错误：不是用户命名问题，不改动 README，非零退出让 Action
        # 显示失败，提醒维护者，而不是静默"成功"。
        log(f"infra-level failure ({reason}), leaving README untouched")
        print(json.dumps({"stage": "tmdb", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    douban_query = f"{tmdb_result['title_en']} {tmdb_result['year']}"
    douban_result = douban_id_lookup.resolve(douban_query, tavily_key, serpstack_key)

    # Metadata (incl. the visibility flip attempt) runs first, so its
    # outcome can be reflected in the README we're about to render.
    # 元数据更新（含尝试切换可见性）先执行，其结果才能反映进即将渲染的
    # README。
    visibility_ok = update_github_repo_metadata(args.github_repository, github_token, tmdb_result)
    render_release_readme(args.repo_name, tmdb_result, douban_result, visibility_ok)

    print(json.dumps({
        "stage": "release",
        "success": True,
        "tmdb": tmdb_result,
        "douban": douban_result,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
