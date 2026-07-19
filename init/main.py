#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
#
# Description / 描述:
#   仓库初始化总控脚本。串联 tmdb_lookup.py 与 douban_id_lookup.py 的结果，
#   按 reason 选择对应 README 模板渲染，并通过 GitHub API 回写仓库的
#   description / topics / Discussions 开关。不涉及仓库可见性（private/
#   public），该项由模板仓库初始 README 中的手动前置步骤处理。
#
# Usage / 用法:
#   python init/main.py --repo-name Cosmos_Laundromat_2015 \
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
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "douban", "search"))

import tmdb_lookup
import douban_id_lookup

TEMPLATES_DIR = os.path.join(REPO_ROOT, "readme", "templates")
ERROR_TEMPLATE = os.path.join(TEMPLATES_DIR, "error", "error.md")
ERROR_FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "error", "fragments")
FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "fragments")
HOME_TEMPLATE = os.path.join(TEMPLATES_DIR, "home.md")
HEADER_VERIFIED_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_verified.md")
HEADER_MANUAL_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_manual.md")

INIT_MARKER = "<!-- montagesubs:initialized -->"

NAMING_ERROR_REASONS = {"invalid_repo_name", "not_found", "title_mismatch", "year_mismatch"}

GITHUB_API_ENDPOINT = "https://api.github.com/repos/{full_name}"

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
    slug = SLUG_INVALID_CHARS_PATTERN.sub("-", title_en.lower()).strip("-")
    return slug


def build_topics(tmdb_result):
    media_type = tmdb_result.get("media_type") or "movie"
    topics = list(TOPIC_MAP.get(media_type, TOPIC_MAP["movie"]))
    slug = slugify_title(tmdb_result["title_en"])
    if slug and slug not in topics:
        topics.append(slug)
    return topics

ERROR_COPY_JSON_PATTERN = re.compile(
    r"<!--\s*ERROR_COPY_JSON\s*(.*?)-->\s*(.*)", re.DOTALL
)


def setup_git_identity():
    actor = os.environ.get("GITHUB_ACTOR")
    actor_id = os.environ.get("GITHUB_ACTOR_ID")
    if actor and actor_id:
        subprocess.run(f'git config user.name "{actor}"', shell=True, check=True)
        subprocess.run(f'git config user.email "{actor_id}+{actor}@users.noreply.github.com"', shell=True, check=True)


def apply_init_manifest(manifest_path, source_root, dest_root):
    if not manifest_path.exists():
        return
    commits = {}
    with open(manifest_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip() or line.strip().startswith('| source') or line.strip().startswith('| :---'):
                continue
            parts = [p.strip() for p in line.split('|') if p.strip()]
            if len(parts) == 3:
                src_path = source_root / parts[0]
                dest_path = dest_root / parts[1]
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
                commits.setdefault(parts[2], []).append(dest_path)
    for msg, files in commits.items():
        for file_path in files:
            subprocess.run(["git", "add", str(file_path)], check=True)
        subprocess.run(f'git diff --staged --quiet || git commit -m "{msg}"', shell=True, check=True)


def log(message):
    print(message, file=sys.stderr)


def read_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def already_initialized():
    readme_path = os.path.join(os.getcwd(), "README.md")
    if not os.path.exists(readme_path):
        return False
    with open(readme_path, "r", encoding="utf-8") as f:
        return INIT_MARKER in f.read()


def write_readme(content):
    with open(os.path.join(os.getcwd(), "README.md"), "w", encoding="utf-8") as f:
        f.write(content)


def build_fix_block(reason, tmdb_result):
    suggested_repo_name = None
    if tmdb_result.get("expected_title") and tmdb_result.get("expected_year"):
        suggested_repo_name = "{}_{}".format(
            tmdb_result["expected_title"].replace(" ", "_"),
            tmdb_result["expected_year"],
        )

    if suggested_repo_name:
        return read_template(
            os.path.join(ERROR_FRAGMENTS_DIR, "fix_rename.md")
        ).format(suggested_repo_name=suggested_repo_name)

    if reason == "not_found":
        return read_template(os.path.join(ERROR_FRAGMENTS_DIR, "fix_not_found.md"))

    return read_template(os.path.join(ERROR_FRAGMENTS_DIR, "fix_delete.md"))


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

    format_kwargs = {}
    for lang in ("zh", "en"):
        entry = copy_table[reason][lang]
        format_kwargs[f"{lang}_heading"] = entry["heading"]
        format_kwargs[f"{lang}_body"] = entry["body"].format(input_repo_name=repo_name)
    format_kwargs["fix_block"] = build_fix_block(reason, tmdb_result)

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


def build_verified_header(repo_name, tmdb_result, douban_result):
    douban_id = douban_result["douban_id"] or "待核实"
    douban_url = (
        f"https://m.douban.com/movie/subject/{douban_result['douban_id']}"
        if douban_result["success"]
        else "#"
    )
    return read_template(HEADER_VERIFIED_FRAGMENT).format(
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


def build_manual_header(repo_name, tmdb_result):
    return read_template(HEADER_MANUAL_FRAGMENT).format(
        input_title=tmdb_result["input_title"] or repo_name,
        input_year=tmdb_result["input_year"] or "未知",
    )


def render_home_readme(repo_name, header_block, forced):
    content = read_template(HOME_TEMPLATE).format(
        header_block=header_block,
        repo_name=repo_name,
    )
    write_readme(content)
    log(f"README rendered from home.md (forced={forced})")


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
            body = json.loads(response.read().decode("utf-8"))
        return True, body
    except urllib.error.HTTPError as e:
        return False, {"http_status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:
        return False, {"http_status": None, "body": str(e)}


def enable_discussions(node_id, github_token):
    query = """
    mutation($id: ID!) {
      updateRepository(input: {repositoryId: $id, hasDiscussionsEnabled: true}) {
        repository { hasDiscussionsEnabled }
      }
    }
    """
    payload = json.dumps({"query": query, "variables": {"id": node_id}}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("errors"):
            return False, body["errors"]
        return True, None
    except urllib.error.HTTPError as e:
        return False, {"http_status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:
        return False, {"http_status": None, "body": str(e)}


def update_github_repo_metadata(github_repository, github_token, tmdb_result):
    if not github_token:
        log("no ORG_ADMIN_TOKEN provided (or not accessible to this private repo), skipping repo metadata update")
        return

    repo_url = GITHUB_API_ENDPOINT.format(full_name=github_repository)

    description = (
        f"《{tmdb_result['title_zh']}》({tmdb_result['year']}) 中文字幕协作项目 | "
        f"Chinese fansub project for \"{tmdb_result['title_en']}\" ({tmdb_result['year']})"
    )
    ok, body = call_github_api(repo_url, github_token, "PATCH", {
        "description": description,
        "homepage": "",
        "has_wiki": False,
    })
    if ok:
        log("github repo metadata updated (description/has_wiki)")
    else:
        log(f"failed to update repo metadata: {body}")

    node_id = body.get("node_id") if ok else None
    if node_id:
        discussions_ok, discussions_err = enable_discussions(node_id, github_token)
        if discussions_ok:
            log("discussions enabled (via GraphQL)")
        else:
            log(f"failed to enable discussions: {discussions_err}")
    else:
        log("no node_id available (metadata PATCH failed), skipping discussions enable")

    topics_ok, topics_err = call_github_api(repo_url + "/topics", github_token, "PUT", {
        "names": build_topics(tmdb_result),
    })
    if topics_ok:
        log("github repo topics updated")
    else:
        log(f"failed to update repo topics: {topics_err}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-name", required=True, help="e.g. Cosmos_Laundromat_2015")
    parser.add_argument("--github-repository", required=True, help="e.g. MontageSubs/Cosmos_Laundromat_2015")
    parser.add_argument("--github-token", default=None, help="PAT with repo admin scope, used for description/topics update")
    parser.add_argument("--tmdb-read-access-token", default=None)
    parser.add_argument("--tavily-api-key", default=None)
    parser.add_argument("--serpstack-api-key", default=None)
    parser.add_argument("--force-init", action="store_true", help="仅在 reason=not_found 时生效：跳过 TMDB 校验，生成空白待填写模板")
    args = parser.parse_args()

    tmdb_token = args.tmdb_read_access_token or os.environ.get("TMDB_READ_ACCESS_TOKEN")
    github_token = args.github_token or os.environ.get("ORG_ADMIN_TOKEN")
    tavily_key = args.tavily_api_key or os.environ.get("TAVILY_API_KEY")
    serpstack_key = args.serpstack_api_key or os.environ.get("SERPSTACK_API_KEY")

    if already_initialized():
        log("README.md already carries the init marker, this repo was initialized before — skipping to avoid overwriting manual edits")
        print(json.dumps({"stage": "idempotency", "success": True, "skipped": True}, ensure_ascii=False))
        sys.exit(0)

    setup_git_identity()
    action_dir = Path(__file__).parent.parent
    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    apply_init_manifest(Path(__file__).parent / "manifest.md", action_dir, workspace_dir)

    tmdb_result = tmdb_lookup.resolve(args.repo_name, tmdb_token)

    if not tmdb_result["success"]:
        reason = tmdb_result["reason"]
        if reason == "not_found" and args.force_init:
            header_block = build_manual_header(args.repo_name, tmdb_result)
            render_home_readme(args.repo_name, header_block, forced=True)
            print(json.dumps({"stage": "manual", "success": True, "forced": True}, ensure_ascii=False))
            sys.exit(0)
        if reason in NAMING_ERROR_REASONS:
            rendered = render_error_readme(tmdb_result, args.repo_name)
            print(json.dumps({"stage": "tmdb", "success": False, "rendered_error_readme": rendered}, ensure_ascii=False))
            sys.exit(0)
        log(f"infra-level failure ({reason}), leaving README untouched")
        print(json.dumps({"stage": "tmdb", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    douban_query = f"{tmdb_result['title_en']} {tmdb_result['year']}"
    douban_result = douban_id_lookup.resolve(douban_query, tavily_key, serpstack_key)

    update_github_repo_metadata(args.github_repository, github_token, tmdb_result)
    header_block = build_verified_header(args.repo_name, tmdb_result, douban_result)
    render_home_readme(args.repo_name, header_block, forced=False)

    print(json.dumps({
        "stage": "home",
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
