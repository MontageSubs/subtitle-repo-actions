#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   官方字幕获取的请求构建脚本。解析目标 IMDb ID 与目标语言（手动 > README
#   > 仓库名/TMDB），按版次目录整理出待抓取的任务清单（opensubtitles_request）。
#   实际登录 OpenSubtitles、下载、落盘、提交均不在本仓库内发生，而是由
#   init.yml/fetch-source.yml 的下一步骤通过 dispatch_client 转发给
#   montagesubs-secure/opensubtitles-bridge 异步执行；三项 OpenSubtitles
#   凭证从此只存在于该桥接仓库内，不进入任何字幕仓库。
#
# Usage / 用法:
#   python actions/source/main.py --edition web --lang fr
#   python actions/source/main.py --best-effort --edition web-uk
# ============================================================================
import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "opensubtitles"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

from tmdb_lookup import resolve_entity
from language_codes import to_opensubtitles_code
from repo_vars import load_repo_vars

KEYWORD_SPLIT_PATTERN = re.compile(r"[-_]+")
IMDB_PREFIX_PATTERN = re.compile(r"^tt", re.IGNORECASE)
MIN_CANDIDATE_COUNT = 1
MAX_CANDIDATE_COUNT = 10

SCRIPT_NAME = "source_main"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def imdb_numeric_id(imdb_id):
    if not imdb_id:
        return None
    return IMDB_PREFIX_PATTERN.sub("", imdb_id) or None


def release_keywords(edition_name):
    return ",".join(k for k in KEYWORD_SPLIT_PATTERN.split(edition_name) if k)


def discover_editions(workspace_dir):
    subtitles_root = workspace_dir / "subtitles"
    if not subtitles_root.is_dir():
        return []
    return sorted(p.name for p in subtitles_root.iterdir() if p.is_dir())


def write_github_output(name, value):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def build_jobs(editions, lang_tags):
    jobs = []
    for edition_name in editions:
        supported, skipped = [], []
        for tag in lang_tags:
            code = to_opensubtitles_code(tag)
            (supported if code else skipped).append((tag, code) if code else tag)
        for tag in skipped:
            log(f"skip: {edition_name}/{tag} (unsupported language code)")
        if not supported:
            continue
        jobs.append({
            "edition": edition_name,
            "keywords": release_keywords(edition_name),
            "languages": [{"tag": tag, "code": code} for tag, code in supported],
        })
    return jobs


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", default="", help="目标版次目录，留空则处理仓库内全部版次")
    parser.add_argument("--lang", default="", help="期待下载的语言（BCP47，逗号分隔可指定多个），留空用 TMDB original_language")
    parser.add_argument("--candidate-count", type=int, default=1)
    parser.add_argument("--manual-id", default=None, help="手动指定 TMDB/IMDb ID 或其页面 URL")
    parser.add_argument("--readme-path", default="README.md")
    parser.add_argument("--best-effort", action="store_true",
                         help="init 流程调用时开启：任何环节失败仅记录日志并以0退出，不中断后续步骤")
    args = parser.parse_args()

    def fail(reason):
        log(f"status: failed ({reason})")
        write_github_output("opensubtitles_request", "{}")
        sys.exit(0 if args.best_effort else 1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    candidate_count = max(MIN_CANDIDATE_COUNT, min(MAX_CANDIDATE_COUNT, args.candidate_count))
    tmdb_token = os.environ.get("TMDB_READ_ACCESS_TOKEN")

    entity = resolve_entity(args.manual_id, tmdb_token, Path(args.readme_path))
    if not entity["success"]:
        fail(f"entity_resolution_failed:{entity['reason']}")
        return

    imdb_numeric = imdb_numeric_id(entity.get("imdb_id"))
    if not imdb_numeric:
        fail("no_imdb_id")
        return

    lang_tags = [t.strip() for t in args.lang.split(",") if t.strip()]
    if not lang_tags:
        if entity.get("original_language"):
            lang_tags = [entity["original_language"]]
        else:
            fail("no_resolvable_language")
            return

    editions = [args.edition] if args.edition else discover_editions(workspace_dir)
    if not editions:
        fail("no_editions_found")
        return

    jobs = build_jobs(editions, lang_tags)
    if not jobs:
        fail("no_supported_languages")
        return

    request = {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "imdb_id": imdb_numeric,
        "candidate_count": candidate_count,
        "jobs": jobs,
        "actor": os.environ.get("GITHUB_ACTOR", ""),
        "actor_id": os.environ.get("GITHUB_ACTOR_ID", ""),
    }
    write_github_output("opensubtitles_request", json.dumps(request, ensure_ascii=False))
    log(f"status: request_built (jobs={len(jobs)})")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)

