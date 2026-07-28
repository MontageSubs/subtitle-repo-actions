#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: dispatch_client.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/github/
#
# Description / 描述:
#   向 montagesubs-secure 组织下的隔离桥接仓库发起 repository_dispatch 的
#   公共客户端。调用方仅持有 SECURE_DISPATCH_TOKEN（仅能对桥接仓库写入，
#   不接触 ORG_ADMIN_TOKEN 或 OpenSubtitles 凭证本身），请求发出即返回，
#   不等待桥接仓库执行完成。
#   Shared client for firing repository_dispatch events at the isolated
#   bridge repositories under the montagesubs-secure organization. The
#   caller only ever holds SECURE_DISPATCH_TOKEN (able to write only to the
#   bridge repositories, never touching ORG_ADMIN_TOKEN or the OpenSubtitles
#   credentials themselves); the request is fire-and-forget.
# ============================================================================
import uuid

from github_api import call_api

SECURE_ORG = "montagesubs-secure"
DISPATCH_API = "https://api.github.com/repos/{full_name}/dispatches"


def new_correlation_id():
    return uuid.uuid4().hex


def dispatch(bridge_repo, dispatch_token, event_type, payload):
    if not dispatch_token:
        return False, "missing_secure_dispatch_token"
    full_name = f"{SECURE_ORG}/{bridge_repo}"
    ok, body = call_api(DISPATCH_API.format(full_name=full_name), dispatch_token, "POST", {
        "event_type": event_type,
        "client_payload": payload,
    })
    return ok, (None if ok else body)
