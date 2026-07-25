#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: github_api.py
# Version: 1.0.1
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/github/
#
# Description / 描述:
#    ORG_ADMIN_TOKEN 相关调用的公共基础设施：REST 请求封装 + 优雅降级
#    装饰器。私有仓库拿不到组织级 secret（免费组织限制），该 token 在
#    私有仓库的 workflow 运行中天然缺失；被 @requires_org_admin_token
#    装饰的函数遇到缺 token 时直接跳过自身、记录日志、返回预设的空值，
#    而不是抛异常或带着 401 让整个 workflow 失败。
#    Shared infrastructure for ORG_ADMIN_TOKEN-backed calls: a REST request
#    wrapper plus a graceful-degradation decorator. Private repositories
#    cannot access org-level secrets (free-org limitation), so this token
#    is naturally absent from workflow runs on a private repo. Functions
#    decorated with @requires_org_admin_token simply skip themselves, log
#    it, and return a preset empty value when the token is missing —
#    instead of raising or failing the whole workflow on a 401.
#
# Usage / 用法:
#    from github_api import call_api, requires_org_admin_token
#
#    @requires_org_admin_token("仓库重命名", default=(False, None))
#    def rename_repository(github_repository, github_token, new_name):
#        ok, body = call_api(url, github_token, "PATCH", {"name": new_name})
#        ...
#
#    `default` 可以是一个值，也可以是一个零参数可调用对象（如 dict/list），
#    跳过时会以 `default() if callable(default) else default` 的方式取值，
#    避免多个函数共享同一个可变默认值。
#    `default` can be either a plain value or a zero-argument callable
#    (e.g. dict/list); on skip it is resolved via
#    `default() if callable(default) else default`, avoiding multiple
#    functions sharing one mutable default instance.
# ============================================================================
import functools
import inspect
import json
import os
import sys
import urllib.error
import urllib.request

SCRIPT_NAME = "github_api"
DEBUG_ENV = "DEBUG"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def is_debug():
    return os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes")


def call_api(url, github_token, method="GET", payload=None):
    if is_debug():
        log(f"query (github api): {method} {url}")
    else:
        log(f"query (github api): {method}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return True, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return False, {"http_status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:
        return False, {"http_status": None, "body": str(e)}


def requires_org_admin_token(action, default=None):
    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            if not bound.arguments.get("github_token"):
                log(f"no ORG_ADMIN_TOKEN provided (or not accessible to this private repo), skipping {action}")
                return default() if callable(default) else default
            return func(*args, **kwargs)
        return wrapper
    return decorator
