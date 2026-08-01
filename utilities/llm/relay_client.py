#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: relay_client.py
# Version: 1.1
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/llm/
#
# Description / 描述:
#   向 llm-translate-relay Worker 发起签名POST请求的公共客户端。签名对象
#   是原始JSON字节（与Worker侧auth.ts的HMAC校验完全对齐），请求发出、
#   收到202即视为成功，不等待Worker完成实际翻译——与dispatch_client.py对
#   montagesubs-secure桥接仓库的fire-and-forget语义一致，区别只是这里
#   直接HTTP POST到Worker URL，而非经GitHub repository_dispatch转发。
# ============================================================================
import hashlib
import hmac
import json
import urllib.error
import urllib.request
import uuid

SIGNATURE_HEADER = "X-Relay-Signature"
REQUEST_TIMEOUT_SECONDS = 20


def new_correlation_id():
    return uuid.uuid4().hex


def send_translate_request(relay_url, signing_secret, payload):
    payload = dict(payload)
    payload.setdefault("correlation_id", new_correlation_id())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = hmac.new(signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        relay_url, data=body, method="POST",
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
        "User-Agent": "MontageSubs-Relay-Client/1.0",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
            return response.status in (200, 202), payload["correlation_id"]
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)
