#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
#
# Description / 描述:
#   剧情摘要工具的手动触发总控脚本。init 阶段已在同一进程内串联过
#   wiki_tmdb_fetch/prompt_build/llm_core/synopsis_render（见 synopsis_
#   pipeline.py），本脚本是其唯一的手动兜底入口：仅当 init 阶段未能正确
#   生成 SYNOPSIS.md/GLOSSARY.md 时才需要运行。
#
# Usage / 用法:
#   python actions/synopsis/main.py --force --manual-id tt1234567
#
#   不传 --manual-id 时，从当前工作目录的 README.md 里已填写的豆瓣/IMDb/
#   TMDB 表格提取 TMDB ID（含 movie/tv 类型）或 IMDb ID。
#   --force 决定 GLOSSARY.md 是否覆盖已有文件；SYNOPSIS.md 始终重新生成。
# ============================================================================
import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "wiki"))

import tmdb_lookup
import synopsis_pipeline

README_TMDB_PATTERN = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")
README_IMDB_PATTERN = re.compile(r"imdb\.com/title/(tt\d+)")


def log(message):
    print(message, file=sys.stderr)


def extract_ids_from_readme(readme_text):
    tmdb_match = README_TMDB_PATTERN.search(readme_text)
    imdb_match = README_IMDB_PATTERN.search(readme_text)
    return {
        "media_type": tmdb_match.group(1) if tmdb_match else None,
        "tmdb_id": tmdb_match.group(2) if tmdb_match else None,
        "imdb_id": imdb_match.group(1) if imdb_match else None,
    }


def resolve_from_tmdb_id(media_type, tmdb_id, imdb_id_hint, tmdb_token):
    detail, error = tmdb_lookup.get_detail(media_type, tmdb_id, tmdb_token)
    if error:
        return tmdb_lookup.empty_result(error["type"])
    title_zh = detail.get("title") if media_type == "movie" else detail.get("name")
    title_en = detail.get("original_title") if media_type == "movie" else detail.get("original_name")
    release_date = detail.get("release_date") if media_type == "movie" else detail.get("first_air_date")
    return {
        "success": True, "reason": None,
        "media_type": media_type, "tmdb_id": tmdb_id,
        "imdb_id": detail.get("external_ids", {}).get("imdb_id") or imdb_id_hint,
        "title_en": title_en, "title_zh": title_zh,
        "year": int((release_date or "0000")[:4] or 0),
        "overview_zh": detail.get("overview"), "poster_path": detail.get("poster_path"),
        "original_language": detail.get("original_language"),
    }


def resolve_tmdb_result(manual_id, tmdb_token, readme_path):
    if manual_id:
        return tmdb_lookup.resolve_manual(manual_id, tmdb_token)

    if not readme_path.exists():
        return tmdb_lookup.empty_result("readme_not_found")

    ids = extract_ids_from_readme(readme_path.read_text(encoding="utf-8"))
    if ids["tmdb_id"] and ids["media_type"]:
        return resolve_from_tmdb_id(ids["media_type"], int(ids["tmdb_id"]), ids["imdb_id"], tmdb_token)
    if ids["imdb_id"]:
        return tmdb_lookup.resolve_manual(ids["imdb_id"], tmdb_token)
    return tmdb_lookup.empty_result("not_found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual-id", default=None,
                         help="手动指定 TMDB/IMDb ID 或其页面 URL；留空则从 README.md 现有信息提取")
    parser.add_argument("--force", action="store_true",
                         help="强制覆盖已有 GLOSSARY.md；不传时若文件已存在则跳过其重新生成")
    parser.add_argument("--readme-path", default="README.md")
    parser.add_argument("--output-dir", default=os.path.join("docs", "synopsis"))
    args = parser.parse_args()

    tmdb_token = os.environ.get("TMDB_READ_ACCESS_TOKEN")
    tmdb_result = resolve_tmdb_result(args.manual_id, tmdb_token, Path(args.readme_path))

    if not tmdb_result["success"]:
        log(f"tmdb resolution failed ({tmdb_result['reason']})")
        print(json.dumps({"stage": "tmdb", "success": False, "reason": tmdb_result["reason"]}, ensure_ascii=False))
        sys.exit(1)

    glossary_path = Path(args.output_dir) / "GLOSSARY.md"
    with_glossary = args.force or not glossary_path.exists()
    if not with_glossary:
        log("GLOSSARY.md already exists and --force not set, skipping its regeneration")

    result = synopsis_pipeline.run(
        tmdb_result, output_dir=args.output_dir,
        tmdb_token=tmdb_token, with_glossary=with_glossary,
    )
    print(json.dumps({"tmdb": tmdb_result, **result}, ensure_ascii=False))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
