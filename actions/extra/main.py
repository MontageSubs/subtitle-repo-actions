#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))

from github_api import is_debug

ITEM_TEMPLATES_ROOT = Path(REPO_ROOT) / "default-docs" / "templates" / "extra"
OVERVIEW_TEMPLATE = Path(REPO_ROOT) / "default-docs" / "templates" / "extras_overview.md"

SCRIPT_NAME = "extra_main"

SLUG_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9]+")


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def slugify(text):
    return SLUG_INVALID_CHARS_PATTERN.sub("-", text.lower()).strip("-")


def copy_templates(source_root, dest_root, context):
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source_root)
        dest_path = dest_root / relative
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(path.read_text(encoding="utf-8").format(**context), encoding="utf-8")
        log(f"created {dest_path}")


def setup_git_identity():
    actor = os.environ.get("GITHUB_ACTOR")
    actor_id = os.environ.get("GITHUB_ACTOR_ID")
    if actor and actor_id:
        subprocess.run(f'git config user.name "{actor}"', shell=True, check=True)
        subprocess.run(f'git config user.email "{actor_id}+{actor}@users.noreply.github.com"', shell=True, check=True)


def commit_extra(paths, message):
    subprocess.run(["git", "add", "-A", "--"] + [str(p) for p in paths], check=True)
    subprocess.run(f'git diff --staged --quiet || git commit -m "{message}"', shell=True, check=True)


def mark_output(key, value):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    slug = slugify(args.title)
    log(f"start: edition={args.edition} title={args.title!r} slug={slug}")

    if not slug:
        reason = "无法从标题生成有效目录标识"
        log(reason)
        print(json.dumps({"stage": "resolve", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    edition_root = workspace_dir / "subtitles" / args.edition

    if not edition_root.is_dir():
        reason = f"版次目录不存在：subtitles/{args.edition}"
        log(reason)
        print(json.dumps({"stage": "resolve", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    extras_root = edition_root / "extras"
    item_root = extras_root / slug

    if item_root.exists():
        log(f"skip: extras/{slug} already exists, leaving it untouched")
        mark_output("extra_dir", slug)
        mark_output("extra_created", "false")
        print(json.dumps({"stage": "extra", "success": True, "skipped": True, "slug": slug}, ensure_ascii=False))
        sys.exit(0)

    setup_git_identity()

    context = {"extra_title": args.title, "edition_name": args.edition, "slug": slug}

    changed_paths = [item_root]
    overview_path = extras_root / "README.md"
    if not overview_path.exists():
        extras_root.mkdir(parents=True, exist_ok=True)
        overview_path.write_text(OVERVIEW_TEMPLATE.read_text(encoding="utf-8").format(**context), encoding="utf-8")
        changed_paths.append(overview_path)
        log(f"created {overview_path}")

    copy_templates(ITEM_TEMPLATES_ROOT, item_root, context)
    commit_extra(changed_paths, f"add: extra {args.edition}/{slug}")

    mark_output("extra_dir", slug)
    mark_output("extra_created", "true")
    log(f"status: success (edition={args.edition}, extra={slug})")
    if is_debug():
        print(json.dumps({"stage": "extra", "success": True, "slug": slug, **context}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
