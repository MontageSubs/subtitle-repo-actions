#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   剧情摘要工具的手动触发总控脚本，串联 wiki_tmdb_fetch/prompt_build/
#   llm_core/synopsis_render（见 synopsis_pipeline.py），生成 SYNOPSIS.md/
#   GLOSSARY.md 后，还会用 AI 生成的简介回写 README.md 的"剧情"段落。
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
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

import tmdb_lookup
import synopsis_pipeline
from repo_vars import load_repo_vars
from github_api import is_debug

README_PLOT_PATTERN = re.compile(
    r'(<h2 id="plot">剧情</h2>\n\n'
    r'<!-- 此"影视内容简介"段落为自动生成，若无错误，请勿手动编辑 -->\n\n)'
    r'.*?(?=<h2 id="notes">)',
    re.DOTALL,
)


SCRIPT_NAME = "synopsis_main"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def update_readme_overview(readme_text, overview):
    if not README_PLOT_PATTERN.search(readme_text):
        return readme_text, False
    updated = README_PLOT_PATTERN.sub(lambda m: f"{m.group(1)}{overview}\n\n\n", readme_text, count=1)
    return updated, True


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--manual-id", default=None,
                         help="手动指定 TMDB/IMDb ID 或其页面 URL；留空则从 README.md 现有信息提取")
    parser.add_argument("--force", action="store_true",
                         help="强制覆盖已有 GLOSSARY.md；不传时若文件已存在则跳过其重新生成")
    parser.add_argument("--readme-path", default="README.md")
    parser.add_argument("--output-dir", default=os.path.join("docs", "synopsis"))
    args = parser.parse_args()

    tmdb_token = os.environ.get("TMDB_READ_ACCESS_TOKEN")
    log(f"start: manual_id={args.manual_id!r} readme_path={args.readme_path} force={args.force}")
    tmdb_result = tmdb_lookup.resolve_entity(args.manual_id, tmdb_token, Path(args.readme_path))

    if not tmdb_result["success"]:
        log(f"tmdb resolution failed ({tmdb_result['reason']})")
        if is_debug():
            print(json.dumps({"stage": "tmdb", "success": False, "reason": tmdb_result["reason"]}, ensure_ascii=False))
        sys.exit(1)

    glossary_path = Path(args.output_dir) / "GLOSSARY.md"
    with_glossary = args.force or not glossary_path.exists()
    if not with_glossary:
        log("GLOSSARY.md already exists and --force not set, skipping its regeneration")

    result = synopsis_pipeline.run(
        tmdb_result, output_dir=args.output_dir,
        tmdb_token=tmdb_token, with_glossary=with_glossary,
        debug=is_debug(),
    )
    log(f"status: {'success' if result.get('success') else 'failed'}")

    if result.get("success") and result.get("overview"):
        readme_file = Path(args.readme_path)
        if readme_file.exists():
            updated_text, patched = update_readme_overview(readme_file.read_text(encoding="utf-8"), result["overview"])
            if patched:
                readme_file.write_text(updated_text, encoding="utf-8")
                log(f"updated: {args.readme_path} (plot section)")
            else:
                log(f"skip: {args.readme_path} has no recognizable plot section marker")

    if is_debug():
        print(json.dumps({"tmdb": tmdb_result, **result}, ensure_ascii=False))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
