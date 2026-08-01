#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/actions/llm-translate/
#
# Description / 描述:
#   LLM翻译草稿的请求构建与转发入口。与legacy-translate不同，本action只由
#   llm-translate.yml的workflow_dispatch手动触发，不随work/source/变更
#   自动跑——Gemini API调用为收费项，不适合无条件自动触发。对每个
#   <edition>/work/source/<src_lang>.srt组装translate_request（原文+ 已有
#   的SYNOPSIS/GLOSSARY作为翻译上下文），经relay_client用HMAC签名POST至
#   llm-translate-relay Worker。收到202确认即视为成功，不等待实际翻译
#   完成——译文由Worker自行用其持有的GitHub PAT直接提交回本仓库，本次
#   workflow运行不产生任何commit，也无需git push步骤。默认已存在同名草稿
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
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "llm"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

from relay_client import send_translate_request
from repo_vars import load_repo_vars

SCRIPT_NAME = "llm_translate_main"
SOURCE_PATH_PATTERN = re.compile(r"^subtitles/(?P<edition>[^/]+)/work/source/(?P<lang>[A-Za-z0-9-]+)\.srt$")
SYNOPSIS_PATH = Path("docs") / "synopsis" / "SYNOPSIS.md"
GLOSSARY_PATH = Path("docs") / "synopsis" / "GLOSSARY.md"
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


def read_optional(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", default="", help="目标版次目录，留空则处理仓库内全部版次")
    parser.add_argument("--lang", default="", help="源语言（BCP47），留空则处理该范围下全部source语言")
    parser.add_argument("--target-lang", default=os.environ.get("TRANSLATE_TARGET_LANG", DEFAULT_TARGET_LANG))
    parser.add_argument("--force", action="store_true", help="已存在的LLM草稿也重新请求覆盖")
    args = parser.parse_args()

    relay_url = os.environ.get("LLM_RELAY_URL")
    signing_secret = os.environ.get("RELAY_SIGNING_SECRET")
    if not relay_url or not signing_secret:
        log("skip: missing LLM_RELAY_URL or RELAY_SIGNING_SECRET, nothing to do")
        sys.exit(0)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    targets = discover_targets(workspace_dir, args.edition, args.lang)
    if not targets:
        log("skip: no matching source subtitle found")
        sys.exit(0)

    synopsis_markdown = read_optional(workspace_dir / SYNOPSIS_PATH)
    glossary_markdown = read_optional(workspace_dir / GLOSSARY_PATH)
    repository = os.environ.get("GITHUB_REPOSITORY", "")

    sent = 0
    for edition, src_lang in targets:
        dest_path = workspace_dir / "subtitles" / edition / "work" / "generated" / "llm" / f"{args.target_lang}.{src_lang}.srt"
        if dest_path.exists() and not args.force:
            log(f"skip: {dest_path} already exists (rerun with --force to overwrite)")
            continue

        source_path = workspace_dir / "subtitles" / edition / "work" / "source" / f"{src_lang}.srt"
        payload = {
            "repository": repository,
            "edition": edition,
            "source_lang": src_lang,
            "target_lang": args.target_lang,
            "source_srt": source_path.read_text(encoding="utf-8-sig"),
            "synopsis_markdown": synopsis_markdown,
            "glossary_markdown": glossary_markdown,
        }
        ok, detail = send_translate_request(relay_url, signing_secret, payload)
        if ok:
            log(f"requested: {edition}/{src_lang} -> {args.target_lang} (correlation_id={detail})")
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
