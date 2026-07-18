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


def render_release_readme(repo_name, tmdb_result, douban_result):
    douban_id = douban_result["douban_id"] or "待核实"
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
    )
    write_readme(content)
    log("README rendered from release.md")


def update_github_repo_metadata(github_repository, github_token, tmdb_result):
    if not github_token:
        log("no ORG_ADMIN_TOKEN provided, skipping repo metadata update (description/topics require a PAT with admin scope, not the default GITHUB_TOKEN)")
        return
    payload = json.dumps({
        "description": f"{tmdb_result['title_zh']} ({tmdb_result['year']}) 中文字幕 | {tmdb_result['title_en']}",
        "homepage": f"https://www.themoviedb.org/movie/{tmdb_result['tmdb_id']}",
    }).encode("utf-8")
    request = urllib.request.Request(
        GITHUB_API_ENDPOINT.format(full_name=github_repository),
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    topics_payload = json.dumps({
        "names": ["montagesubs", "subtitle", "chinese-subtitle", str(tmdb_result["year"])]
    }).encode("utf-8")
    topics_request = urllib.request.Request(
        GITHUB_API_ENDPOINT.format(full_name=github_repository) + "/topics",
        data=topics_payload,
        method="PUT",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        with urllib.request.urlopen(topics_request, timeout=20) as response:
            response.read()
        log("github repo metadata updated (description/homepage/topics)")
    except urllib.error.HTTPError as e:
        log(f"failed to update repo metadata: http {e.code}")
    except Exception as e:
        log(f"failed to update repo metadata: {e}")


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

    render_release_readme(args.repo_name, tmdb_result, douban_result)
    update_github_repo_metadata(args.github_repository, github_token, tmdb_result)

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
