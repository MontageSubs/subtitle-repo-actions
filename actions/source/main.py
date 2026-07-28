#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   官方字幕获取工具的总控脚本。解析目标 IMDb ID 与目标语言（手动 > README
#   > 仓库名/TMDB），按版次目录调用 opensubtitles_fetch.py 下载候选，
#   落地为 work/source/<lang>.srt（次优候选落地为 <lang>.candidate-N.srt），
#   逐文件提交，commit message 不含原始命名，仅含上传者与字幕页面链接。
#
# Usage / 用法:
#   python actions/source/main.py --edition web --lang fr
#   python actions/source/main.py --best-effort --edition web-uk
# ============================================================================
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "opensubtitles"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

from tmdb_lookup import resolve_entity
from language_codes import to_opensubtitles_code
from git_ops import setup_git_identity, commit_if_changed
from repo_vars import load_repo_vars
from github_api import is_debug

OPENSUBTITLES_FETCH_SCRIPT = os.path.join(REPO_ROOT, "utilities", "opensubtitles", "opensubtitles_fetch.py")
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


def run_fetch(imdb_numeric, lang_code, keywords, candidate_count, output_dir):
    command = [
        sys.executable, OPENSUBTITLES_FETCH_SCRIPT,
        "--imdb-id", imdb_numeric,
        "--lang", lang_code,
        "--release-keyword", keywords,
        "--download",
        "--download-count", str(candidate_count),
        "--output-dir", str(output_dir),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    for line in completed.stderr.splitlines():
        log(f"[opensubtitles_fetch] {line}")
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"success": False, "reason": "fetch_script_no_output"}


def place_candidate(local_path, dest_root, lang_tag, rank):
    dest_name = f"{lang_tag}.srt" if rank == 1 else f"{lang_tag}.candidate-{rank}.srt"
    dest_path = dest_root / dest_name
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local_path), str(dest_path))
    return dest_path


def commit_subtitle(workspace_dir, dest_path, uploader, page_link):
    relative = str(dest_path.relative_to(workspace_dir))
    messages = [f"fetch: official subtitle (uploader: {uploader or 'anonymous'})"]
    if page_link:
        messages.append(f"Source: {page_link}")
    if commit_if_changed([relative], messages, cwd=workspace_dir):
        log(f"committed: {relative}")
    else:
        log(f"skip commit: {relative} unchanged")


def fetch_for_edition(workspace_dir, edition_name, imdb_numeric, lang_tags, candidate_count):
    dest_root = workspace_dir / "subtitles" / edition_name / "work" / "source"
    keywords = release_keywords(edition_name)
    committed_any = False
    for lang_tag in lang_tags:
        lang_code = to_opensubtitles_code(lang_tag)
        if not lang_code:
            log(f"skip: {edition_name}/{lang_tag} (unsupported language code)")
            continue
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = run_fetch(imdb_numeric, lang_code, keywords, candidate_count, tmp_dir)
            if not result.get("success"):
                log(f"skip: {edition_name}/{lang_tag} ({result.get('reason')})")
                continue
            for rank, item in enumerate(result["downloaded"], start=1):
                if not item["verified"]:
                    log(f"skip commit: {item['local_path']} (unverified download)")
                    continue
                dest_path = place_candidate(item["local_path"], dest_root, lang_tag, rank)
                commit_subtitle(workspace_dir, dest_path, item["uploader"], item["subtitles_page_link"])
                committed_any = True
    return committed_any


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

    setup_git_identity()

    committed_any = False
    for edition_name in editions:
        if fetch_for_edition(workspace_dir, edition_name, imdb_numeric, lang_tags, candidate_count):
            committed_any = True

    log(f"status: {'success' if committed_any else 'no_subtitles_committed'}")
    if is_debug():
        print(json.dumps({"success": committed_any, "imdb_id": imdb_numeric, "lang": lang_tags}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
