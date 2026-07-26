#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_NAME = "edition_delete"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def setup_git_identity():
    actor = os.environ.get("GITHUB_ACTOR")
    actor_id = os.environ.get("GITHUB_ACTOR_ID")
    if actor and actor_id:
        subprocess.run(f'git config user.name "{actor}"', shell=True, check=True)
        subprocess.run(f'git config user.email "{actor_id}+{actor}@users.noreply.github.com"', shell=True, check=True)


def commit_deletion(edition_name):
    subprocess.run(["git", "add", "-A", "--", "subtitles"], check=True)
    subprocess.run(f'git diff --staged --quiet || git commit -m "remove: edition {edition_name}"', shell=True, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    log(f"start: name={args.name!r} confirm={args.confirm!r}")

    if args.confirm != args.name:
        reason = "确认输入与目标版次名不一致，已中止删除"
        log(reason)
        print(json.dumps({"stage": "confirm", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    target_root = workspace_dir / "subtitles" / args.name

    if not target_root.is_dir():
        reason = f"版次目录不存在：subtitles/{args.name}"
        log(reason)
        print(json.dumps({"stage": "resolve", "success": False, "reason": reason}, ensure_ascii=False))
        sys.exit(1)

    setup_git_identity()

    shutil.rmtree(target_root)
    commit_deletion(args.name)

    log(f"status: success (removed subtitles/{args.name})")
    print(json.dumps({"stage": "delete", "success": True, "edition_name": args.name}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
