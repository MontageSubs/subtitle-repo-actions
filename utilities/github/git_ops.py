#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: git_ops.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   仓库脚本共用的 Git 操作：从 GITHUB_ACTOR/GITHUB_ACTOR_ID 配置提交身份，
#   以及"仅当暂存区确有变更才提交"的幂等提交封装，供各 action 复用。
# ============================================================================
import os
import subprocess


def setup_git_identity():
    actor = os.environ.get("GITHUB_ACTOR")
    actor_id = os.environ.get("GITHUB_ACTOR_ID")
    if actor and actor_id:
        subprocess.run(["git", "config", "user.name", actor], check=True)
        subprocess.run(["git", "config", "user.email", f"{actor_id}+{actor}@users.noreply.github.com"], check=True)


def commit_if_changed(paths, messages, cwd=None):
    subprocess.run(["git", "add", "-A", "--", *paths], cwd=cwd, check=True)
    if subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=cwd).returncode == 0:
        return False
    command = ["git", "commit"]
    for message in messages:
        command += ["-m", message]
    subprocess.run(command, cwd=cwd, check=True)
    return True
