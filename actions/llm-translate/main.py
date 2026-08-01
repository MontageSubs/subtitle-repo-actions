#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/actions/llm-translate/
#
# Description / 描述:
#   LLM翻译请求的发现与转发入口。与legacy-translate不同，本action只由
#   llm-translate.yml的workflow_dispatch手动触发，不随work/source/变更
#   自动跑——LLM调用消耗成员个人配额，不适合无条件自动触发。对每个
#   <edition>/work/source/<src_lang>.srt，用DISPATCH_TOKEN向触发者
#   （GITHUB_ACTOR）个人fork的montagesubs-translate-actions仓库发起
#   workflow_dispatch，翻译在成员自己的Actions配额内运行，完成后由该
#   fork侧的Action自行用其持有的GitHub PAT把译文提交回本仓库——本次
#   workflow运行不产生任何commit，也无需git push步骤。DISPATCH_TOKEN
#   未配置视为环境错误，直接报错退出而非静默跳过。默认已存在同名草稿
#   即跳过，仅--force才重新请求覆盖。
#
# Usage / 用法:
#   python actions/llm-translate/main.py --edition web --lang en
#   python actions/llm-translate/main.py --force
# ============================================================================
import argparse
import os
import re
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

from repo_vars import load_repo_vars

SCRIPT_NAME = "llm_translate_main"
SOURCE_PATH_PATTERN = re.compile(r"^subtitles/(?P<edition>[^/]+)/work/source/(?P<lang>[A-Za-z0-9-]+)\.srt$")
DEFAULT_TARGET_LANG = "zh-Hans"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def discover_targets(workspace_dir, edition_filter, lang_filter):
    targets = []
    for path in sorted(workspace_dir.glob("subtitles/*/work/source/*.srt")):
        match = SOURCE_PATH_PATTERN.match(path.relative_to(workspace_dir).as_posix())
        if not match:
            continue
        edition, lang = match.group("edition"), match.group("lang")
        if edition_filter and edition != edition_filter:
            continue
        if lang_filter and lang != lang_filter:
            continue
        targets.append((edition, lang))
    return targets


def dispatch_to_fork(actor, repo, edition, source_lang, target_lang, dispatch_token):
    url = f"https://api.github.com/repos/{actor}/montagesubs-translate-actions/actions/workflows/translate.yml/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "target_repository": repo,
            "edition": edition,
            "source_lang": source_lang,
            "target_lang": target_lang,
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {dispatch_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "MontageSubs-Action"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as response:
            return True, response.status
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8')}"
    except Exception as e:
        return False, str(e)


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", default="", help="目标版次目录，留空则处理仓库内全部版次")
    parser.add_argument("--lang", default="", help="源语言（BCP47），留空则处理该范围下全部source语言")
    parser.add_argument("--target-lang", default=os.environ.get("TRANSLATE_TARGET_LANG", DEFAULT_TARGET_LANG))
    parser.add_argument("--force", action="store_true", help="已存在的LLM草稿也重新请求覆盖")
    args = parser.parse_args()

    dispatch_token = os.environ.get("DISPATCH_TOKEN")
    actor = os.environ.get("GITHUB_ACTOR")
    if not dispatch_token:
        log("error: DISPATCH_TOKEN not configured, cannot dispatch to member's fork "
            "(configure it as an organization or repository secret)")
        sys.exit(1)
    if not actor:
        log("error: GITHUB_ACTOR environment variable is unexpectedly empty")
        sys.exit(1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    targets = discover_targets(workspace_dir, args.edition, args.lang)
    if not targets:
        log("skip: no matching source subtitle found")
        sys.exit(0)

    repository = os.environ.get("GITHUB_REPOSITORY", "")

    sent = 0
    for edition, src_lang in targets:
        dest_path = workspace_dir / "subtitles" / edition / "work" / "generated" / "llm" / f"{args.target_lang}.{src_lang}.srt"
        if dest_path.exists() and not args.force:
            log(f"skip: {dest_path} already exists (rerun with --force to overwrite)")
            continue

        ok, detail = dispatch_to_fork(actor, repository, edition, src_lang, args.target_lang, dispatch_token)
        if ok:
            log(f"requested: {edition}/{src_lang} -> {args.target_lang} on {actor}'s fork")
            sent += 1
        else:
            log(f"request failed: {edition}/{src_lang} ({detail})")

    log(f"status: {'ok' if sent else 'nothing_sent'} ({sent}/{len(targets)} requested)")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)

