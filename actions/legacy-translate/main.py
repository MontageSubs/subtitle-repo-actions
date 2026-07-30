#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/actions/legacy-translate/
#
# Description / 描述:
#   传统机翻草稿生成入口。由 legacy-translate.yml 在 work/source/ 新增或
#   更新字幕后触发（push 事件按 git diff 定位改动文件；workflow_dispatch
#   则全量扫描），对每个 <edition>/work/source/<src_lang>.srt 调用
#   utilities/translation/mt 下已测试稳定的 srt_extract → google_client →
#   bilingual_merge 流水线，产出 <edition>/work/generated/mt/<target_lang>.
#   <src_lang>.srt。目标语言按源语言各自独立成文件，互不覆盖。默认已存在
#   即跳过，仅 --force 才覆盖重写。
# ============================================================================
import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "translation", "mt"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "opensubtitles"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

from srt_extract import extract, build_glossary_from_markdown
from google_client import translate, API_KEY_ENV, DEFAULT_BATCH_CHARS, DEFAULT_CONCURRENCY
from bilingual_merge import merge
from language_codes import primary_subtag
from git_ops import setup_git_identity, commit_if_changed
from repo_vars import load_repo_vars

SCRIPT_NAME = "legacy_translate_main"
SOURCE_PATH_PATTERN = re.compile(r"^subtitles/(?P<edition>[^/]+)/work/source/(?P<lang>[A-Za-z0-9-]+)\.srt$")
GLOSSARY_PATH = Path("docs") / "synopsis" / "GLOSSARY.md"
DEFAULT_TARGET_LANG = "zh-Hans"
GOOGLE_TARGET_OVERRIDES = {"zh-hans": "zh-CN", "zh-hant": "zh-TW"}


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def google_lang_code(bcp47_tag):
    return GOOGLE_TARGET_OVERRIDES.get(bcp47_tag.lower(), primary_subtag(bcp47_tag))


def parse_targets(lines):
    targets = []
    for line in lines:
        match = SOURCE_PATH_PATTERN.match(line.strip())
        if match:
            targets.append((match.group("edition"), match.group("lang")))
    return targets


def discover_all_targets(workspace_dir):
    paths = sorted(workspace_dir.glob("subtitles/*/work/source/*.srt"))
    relative_paths = (p.relative_to(workspace_dir).as_posix() for p in paths)
    return parse_targets(relative_paths)


def load_glossary(workspace_dir):
    glossary_path = workspace_dir / GLOSSARY_PATH
    if not glossary_path.exists():
        return {}
    return build_glossary_from_markdown(glossary_path.read_text(encoding="utf-8"))


def translate_source(workspace_dir, edition, src_lang, target_lang, glossary, api_key, force):
    src_path = workspace_dir / "subtitles" / edition / "work" / "source" / f"{src_lang}.srt"
    dest_path = workspace_dir / "subtitles" / edition / "work" / "generated" / "mt" / f"{target_lang}.{src_lang}.srt"
    if dest_path.exists() and not force:
        log(f"skip: {dest_path} already exists (rerun with --force to overwrite)")
        return None

    extract_data = extract(src_path.read_text(encoding="utf-8-sig"), glossary)
    if not extract_data["success"]:
        log(f"skip: {src_path} ({extract_data['reason']})")
        return None

    units = extract_data["units"]
    resolved = {str(u["id"]): u["resolved"] for u in units if u.get("resolved")}
    translatable = [u for u in units if not u.get("resolved")]
    translations, skipped = (
        translate(translatable, primary_subtag(src_lang), google_lang_code(target_lang), api_key,
                  DEFAULT_BATCH_CHARS, DEFAULT_CONCURRENCY)
        if translatable else ({}, [])
    )
    translations = {str(k): v for k, v in translations.items()}
    translations.update(resolved)
    if not translations:
        log(f"skip: {src_path} (translation failed, no output)")
        return None

    merged = merge(extract_data, {"translations": translations})
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(merged["srt"], encoding="utf-8")
    log(f"generated: {dest_path} (missing={merged['missing_count']}, skipped={len(skipped)})")
    return dest_path.relative_to(workspace_dir).as_posix()


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files", default="", help="换行分隔的本次推送变更文件路径列表")
    parser.add_argument("--scan-all", action="store_true", help="忽略 --changed-files，全量扫描仓库内全部 source 字幕")
    parser.add_argument("--force", action="store_true", help="已存在的机翻草稿也重新生成覆盖")
    args = parser.parse_args()

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        log(f"skip: missing {API_KEY_ENV}, nothing to do")
        sys.exit(0)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    targets = discover_all_targets(workspace_dir) if args.scan_all else parse_targets(args.changed_files.splitlines())
    if not targets:
        log("skip: no matching source subtitle changes")
        sys.exit(0)

    target_lang = os.environ.get("TRANSLATE_TARGET_LANG", DEFAULT_TARGET_LANG)
    glossary = load_glossary(workspace_dir)

    setup_git_identity()
    committed_paths = []
    for edition, src_lang in targets:
        dest_relative = translate_source(workspace_dir, edition, src_lang, target_lang, glossary, api_key, args.force)
        if dest_relative:
            committed_paths.append(dest_relative)

    if committed_paths and commit_if_changed(committed_paths, ["translate: add legacy MT draft"]):
        log(f"status: committed ({len(committed_paths)} file(s))")
    else:
        log("status: nothing to commit")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
