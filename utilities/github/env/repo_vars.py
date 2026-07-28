#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: repo_vars.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   将 workflow 通过 REPO_VARS_JSON（`toJson(vars)`）透传的仓库变量批量回填
#   进 os.environ，使各 action 脚本可直接用 os.environ.get() 读取任意仓库
#   变量，无需为每个新变量单独修改模板仓库的 yml。仅用于 vars（非敏感配置），
#   不适用于 secrets。
#   Bulk-loads repository variables passed via REPO_VARS_JSON (`toJson(vars)`)
#   into os.environ, so any action script can read a repo variable with
#   os.environ.get() without editing the template repo's yml per new
#   variable. For vars (non-sensitive config) only, not secrets.
# ============================================================================
import json
import os

REPO_VARS_ENV = "REPO_VARS_JSON"


def load_repo_vars():
    raw = os.environ.get(REPO_VARS_ENV)
    if not raw:
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    for key, value in data.items():
        os.environ.setdefault(key, str(value))
