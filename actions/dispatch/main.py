#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   通用 repository_dispatch 发起脚本，供 init.yml / fetch-source.yml 等
#   workflow 复用：读取上一步骤产出的 JSON payload，转发给
#   montagesubs-secure 下指定的桥接仓库，发出即退出，不等待处理结果。
#   payload 为空对象（"{}"）时视为无需下发，直接跳过。
#
# Usage / 用法:
#   python actions/dispatch/main.py --bridge-repo org-admin-bridge \
#       --event-type provision-repository --payload "$PAYLOAD_JSON"
# ============================================================================
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))

import dispatch_client

SCRIPT_NAME = "dispatch_main"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-repo", required=True, choices=["org-admin-bridge", "opensubtitles-bridge"])
    parser.add_argument("--event-type", required=True)
    parser.add_argument("--payload", required=True, help="JSON string, empty object skips dispatch")
    args = parser.parse_args()

    payload = json.loads(args.payload)
    if not payload:
        log("empty payload, nothing to dispatch")
        sys.exit(0)

    payload.setdefault("correlation_id", dispatch_client.new_correlation_id())
    token = os.environ.get("SECURE_DISPATCH_TOKEN")
    ok, error = dispatch_client.dispatch(args.bridge_repo, token, args.event_type, payload)
    if ok:
        log(f"dispatched: repo={args.bridge_repo} event={args.event_type} correlation_id={payload['correlation_id']}")
    else:
        log(f"dispatch failed: repo={args.bridge_repo} event={args.event_type} error={error}")
    sys.exit(0)


if __name__ == "__main__":
    main()
