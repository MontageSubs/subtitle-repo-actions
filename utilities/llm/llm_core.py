#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: llm_core.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/llm/
#
# Description / 描述:
#    Minimal, swappable LLM chat-completion core. Reads a messages array,
#    calls the Hugging Face Router chat completions endpoint, and returns
#    the assistant's text. Carries no prompt-engineering logic; the caller
#    owns all prompt content, this module only speaks "messages in, text
#    out". Replacing the provider means swapping this file for another one
#    with the same input/output contract.
#    极简、可替换的LLM对话核心。接收消息数组，调用Hugging Face Router聊天
#    补全接口，返回助手文本。不包含任何提示词工程逻辑，调用方负责全部提示词
#    内容，本模块只负责"输入messages，输出text"。更换供应商只需替换本文件
#    为另一份遵循相同输入输出约定的脚本。
#
# Usage / 用法:
#    echo '{"messages":[{"role":"user","content":"你好"}]}' | python llm_core.py
#    python llm_core.py --token KEY --model MODEL --max-tokens 2048 < input.json
#
#    Token is read from --token, falling back to the HUGGINGFACE_LLM_TOKEN
#    environment variable. Model is read from --model, falling back to the
#    HUGGINGFACE_LLM_MODEL environment variable, falling back to a built-in
#    default.
#    token可通过--token传入，缺省时读取HUGGINGFACE_LLM_TOKEN环境变量。
#    model可通过--model传入，缺省时读取HUGGINGFACE_LLM_MODEL环境变量，
#    再缺省则使用内置默认值。
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - Request model and message count, final status / 请求的模型与消息
#        数量，最终状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object / 单个JSON对象
#
# Example execution / 执行示例:
#    $ echo '{"messages":[{"role":"user","content":"你好，你会中文吗？"}]}' | python llm_core.py
#    request: model=google/gemma-4-31B-it:novita messages=1
#    status: success
#    {"success": true, "content": "你好！是的，我会中文。", "reason": null, "detail": null, "usage": {"prompt_tokens": 19, "completion_tokens": 16, "total_tokens": 35}}
#
# Exit codes / 退出码:
#    0    normal completion, regardless of whether success is true or false
#         正常完成，无论success为true还是false
#    130  interrupted by Ctrl+C / 被Ctrl+C中断
#
# ============================================================================
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_ENV = "HUGGINGFACE_LLM_TOKEN"
MODEL_ENV = "HUGGINGFACE_LLM_MODEL"
DEFAULT_MODEL = "google/gemma-4-31B-it:novita"
ENDPOINT = "https://router.huggingface.co/v1/chat/completions"
REQUEST_TIMEOUT = 120

# reason values surfaced to the caller when success is false:
# no_token, invalid_input, auth_error, bad_request, rate_limit,
# quota_exceeded, server_error, network_error, empty_response
ERROR_NO_TOKEN = "no_token"
ERROR_INVALID_INPUT = "invalid_input"
ERROR_AUTH = "auth_error"
ERROR_BAD_REQUEST = "bad_request"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network_error"
ERROR_EMPTY_RESPONSE = "empty_response"


def log(message):
    print(message, file=sys.stderr)


def fail(reason, detail=None):
    log(f"status: failed ({reason})")
    return {
        "success": False,
        "content": None,
        "reason": reason,
        "detail": detail,
        "usage": None,
    }


def classify_quota_text(text):
    lowered = (text or "").lower()
    return "credit" in lowered or "quota" in lowered or "included usage" in lowered


def call_huggingface(messages, token, model, max_tokens, temperature):
    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        code = e.code
        if code == 401:
            return None, fail(ERROR_AUTH, f"http {code}: {raw}")
        if code == 402 or classify_quota_text(raw):
            return None, fail(ERROR_QUOTA_EXCEEDED, f"http {code}: {raw}")
        if code == 429:
            return None, fail(ERROR_RATE_LIMIT, f"http {code}: {raw}")
        if code in (400, 404, 422):
            return None, fail(ERROR_BAD_REQUEST, f"http {code}: {raw}")
        if code >= 500:
            return None, fail(ERROR_SERVER, f"http {code}: {raw}")
        return None, fail(ERROR_NETWORK, f"http {code}: {raw}")
    except (urllib.error.URLError, TimeoutError) as e:
        return None, fail(ERROR_NETWORK, str(e))
    except json.JSONDecodeError as e:
        return None, fail(ERROR_NETWORK, f"invalid json response: {e}")

    if isinstance(body, dict) and "error" in body:
        detail = json.dumps(body["error"], ensure_ascii=False) if isinstance(body["error"], dict) else str(body["error"])
        if classify_quota_text(detail):
            return None, fail(ERROR_QUOTA_EXCEEDED, detail)
        return None, fail(ERROR_BAD_REQUEST, detail)

    choices = body.get("choices") or []
    if not choices:
        return None, fail(ERROR_EMPTY_RESPONSE, "no choices in response")

    content = (choices[0].get("message") or {}).get("content")
    if not content or not content.strip():
        return None, fail(ERROR_EMPTY_RESPONSE, "empty content in response")

    return {"content": content, "usage": body.get("usage")}, None


def complete(messages, token, model=DEFAULT_MODEL, max_tokens=2048, temperature=0.7):
    if not token:
        return fail(ERROR_NO_TOKEN, "no huggingface token provided")

    if not isinstance(messages, list) or not messages:
        return fail(ERROR_INVALID_INPUT, "messages must be a non-empty list")

    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            return fail(ERROR_INVALID_INPUT, "each message needs role and content")

    log(f"request: model={model} messages={len(messages)}")

    result, error = call_huggingface(messages, token, model, max_tokens, temperature)
    if error:
        return error

    log("status: success")
    usage = result["usage"] or {}
    return {
        "success": True,
        "content": result["content"],
        "reason": None,
        "detail": None,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    }


def resolve_token(cli_value):
    if cli_value:
        return cli_value
    return os.environ.get(TOKEN_ENV)


def resolve_model(cli_value):
    if cli_value:
        return cli_value
    return os.environ.get(MODEL_ENV, DEFAULT_MODEL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.7)
    args = parser.parse_args()

    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps(fail(ERROR_INVALID_INPUT, f"invalid json on stdin: {e}"), ensure_ascii=False))
        return

    result = complete(
        messages=input_data.get("messages"),
        token=resolve_token(args.token),
        model=resolve_model(args.model),
        max_tokens=input_data.get("max_tokens", args.max_tokens),
        temperature=input_data.get("temperature", args.temperature),
    )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
