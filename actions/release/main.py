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

TEMPLATES_ROOT = Path(REPO_ROOT) / "default-docs" / "templates" / "release"

SCRIPT_NAME = "release_main"

SLUG_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9]+")


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def slugify(text):
    return SLUG_INVALID_CHARS_PATTERN.sub("-", text.lower()).strip("-")


def resolve_release_name(is_web, is_bluray, label):
    if is_web and is_bluray:
        raise ValueError("来源冲突：WEB 与 BluRay 不可同时勾选，请二选一或都不选改用自定义标识")
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


def copy_release_templates(source_root, dest_root, context):
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


def commit_release(dest_root, release_name):
    subprocess.run(["git", "add", "-A", "--", str(dest_root)], check=True)
    subprocess.run(f'git diff --staged --quiet || git commit -m "add: release {release_name}"', shell=True, check=True)


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
        release_name, source_type_label, source_type_raw = resolve_release_name(args.web, args.bluray, args.label)
    except ValueError as e:
        log(str(e))
        print(json.dumps({"stage": "resolve", "success": False, "reason": str(e)}, ensure_ascii=False))
        sys.exit(1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    dest_root = workspace_dir / "subtitles" / release_name

    if dest_root.exists():
        log(f"skip: subtitles/{release_name} already exists, leaving it untouched")
        mark_output("release_dir", release_name)
        mark_output("release_created", "false")
        print(json.dumps({"stage": "release", "success": True, "skipped": True, "release_name": release_name}, ensure_ascii=False))
        sys.exit(0)

    setup_git_identity()

    context = {
        "release_name": release_name,
        "source_type_label": source_type_label,
        "source_type_raw": source_type_raw,
        "label_raw": args.label,
        "label_display": args.label or "无",
        "display_name": build_display_name(args.web, args.bluray, args.label) or release_name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    copy_release_templates(TEMPLATES_ROOT, dest_root, context)
    commit_release(dest_root, release_name)

    mark_output("release_dir", release_name)
    mark_output("release_created", "true")
    log(f"status: success (release={release_name})")
    if is_debug():
        print(json.dumps({"stage": "release", "success": True, "release_name": release_name, **context}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
