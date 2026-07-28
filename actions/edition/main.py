#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))

from github_api import is_debug
from git_ops import setup_git_identity, commit_if_changed

TEMPLATES_ROOT = Path(REPO_ROOT) / "default-docs" / "templates" / "edition"

SCRIPT_NAME = "edition_main"

SLUG_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9]+")


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def slugify(text):
    return SLUG_INVALID_CHARS_PATTERN.sub("-", text.lower()).strip("-")


def resolve_edition_name(is_web, is_bluray, label):
    if is_web and is_bluray:
        raise ValueError("来源冲突：WEB 与 BluRay 不可同时勾选，请三选一")
    slug = slugify(label) if label else ""
    if is_web:
        return ("web-" + slug if slug else "web"), "WEB", "web"
    if is_bluray:
        return ("bluray-" + slug if slug else "bluray"), "BluRay", "bluray"
    if not slug:
        raise ValueError("未提供来源：请勾选 WEB / BluRay 之一，或在输入框填写完整来源标识")
    return slug, "自定义（见目录标识）", ""


def build_display_name(is_web, is_bluray, label):
    parts = []
    if is_web:
        parts.append("WEB")
    if is_bluray:
        parts.append("BluRay")
    if label:
        parts.append(label.strip().replace("-", " ").title())
    return " ".join(parts)


def copy_edition_templates(source_root, dest_root, context):
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source_root)
        dest_path = dest_root / relative
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(path.read_text(encoding="utf-8").format(**context), encoding="utf-8")
        log(f"created {dest_path}")


def commit_edition(dest_root, edition_name):
    commit_if_changed([str(dest_root)], [f"add: edition {edition_name}"])


def mark_output(key, value):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--bluray", action="store_true")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    log(f"start: web={args.web} bluray={args.bluray} label={args.label!r}")

    try:
        edition_name, source_type_label, source_type_raw = resolve_edition_name(args.web, args.bluray, args.label)
    except ValueError as e:
        log(str(e))
        print(json.dumps({"stage": "resolve", "success": False, "reason": str(e)}, ensure_ascii=False))
        sys.exit(1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    dest_root = workspace_dir / "subtitles" / edition_name

    if dest_root.exists():
        log(f"skip: subtitles/{edition_name} already exists, leaving it untouched")
        mark_output("edition_dir", edition_name)
        mark_output("edition_created", "false")
        print(json.dumps({"stage": "edition", "success": True, "skipped": True, "edition_name": edition_name}, ensure_ascii=False))
        sys.exit(0)

    setup_git_identity()

    context = {
        "edition_name": edition_name,
        "source_type_label": source_type_label,
        "source_type_raw": source_type_raw,
        "label_raw": args.label,
        "label_display": args.label or "无",
        "display_name": build_display_name(args.web, args.bluray, args.label) or edition_name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    copy_edition_templates(TEMPLATES_ROOT, dest_root, context)
    commit_edition(dest_root, edition_name)

    mark_output("edition_dir", edition_name)
    mark_output("edition_created", "true")
    log(f"status: success (edition={edition_name})")
    if is_debug():
        print(json.dumps({"stage": "edition", "success": True, "edition_name": edition_name, **context}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
