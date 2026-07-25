#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: llm_core.py
# Version: 2.3.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/llm/
#
# Description / 描述:
#    Minimal, swappable LLM chat-completion core with provider fallback.
#    Tries Google Gemini first, falls back to Hugging Face Router on
#    failure. Both providers stream; this keeps the connection alive
#    across slow gateways and lets progress be reported by chars received
#    rather than a blind wait. Carries no prompt-engineering logic; the
#    caller owns all prompt content, this module only speaks "messages
#    in, text out".
#    带供应商兜底的极简LLM对话核心。优先调用Google Gemini，失败时回退至
#    Hugging Face Router。两个供应商均采用流式请求，以在网关不稳定时维持
#    连接存活，并按已接收字符数汇报进度而非盲等。不包含任何提示词工程
#    逻辑，调用方负责全部提示词内容，本模块只负责"输入messages，输出text"。
#
# Usage / 用法:
#    echo '{"messages":[{"role":"user","content":"你好"}]}' | python llm_core.py
#
#    Tokens are read from GOOGLE_LLM_TOKEN and HUGGINGFACE_LLM_TOKEN
#    environment variables, or --google-token / --hf-token. A provider
#    with no token is skipped, not treated as an error, unless every
#    provider ends up unavailable.
#    token分别读取GOOGLE_LLM_TOKEN与HUGGINGFACE_LLM_TOKEN环境变量，或通过
#    --google-token/--hf-token传入。缺少token的供应商会被
#    跳过而非报错，除非所有供应商都不可用。
#
#    Pass --debug (or set DEBUG=1) to log each streamed chunk's content
#    alongside the heartbeat; without it, only char counts are logged, to
#    keep CI logs clean.
#    传入--debug（或设置DEBUG=1环境变量）会在心跳日志中附带每次心跳期间
#    新增的实际内容；不传时心跳只汇报字符数，以保持CI日志干净。
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - Per-provider attempt, streaming progress, and final status
#        各供应商尝试情况、流式进度与最终状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object, with "provider" naming which one answered
#        单个JSON对象，"provider"字段标明最终应答的供应商
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
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

def read_own_version():
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("# Version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "DEV"


VERSION = read_own_version()
REPOSITORY = "https://github.com/MontageSubs/subtitle-repo-actions"
USER_AGENT = f"llm_core/{VERSION} (+{REPOSITORY}; GitHub Actions)"

GOOGLE_TOKEN_ENV = "GOOGLE_LLM_TOKEN"
GOOGLE_MODEL_ENV = "GEMINI_LLM_MODEL"
GOOGLE_THINKING_BUDGET_ENV = "GEMINI_THINKING_BUDGET"
GOOGLE_DEFAULT_MODEL = "gemma-4-31b-it"
GOOGLE_DEFAULT_THINKING_BUDGET = None

HF_TOKEN_ENV = "HUGGINGFACE_LLM_TOKEN"
HF_MODEL_ENV = "HUGGINGFACE_LLM_MODEL"
HF_DEFAULT_MODEL = "google/gemma-4-31B-it"
HF_ENDPOINT = "https://router.huggingface.co/v1/chat/completions"

DEBUG_ENV = "DEBUG"
REQUEST_TIMEOUT = 300
HEARTBEAT_INTERVAL = 20
HF_RETRY_DELAY = 5

ERROR_NO_TOKEN = "no_token"
ERROR_INVALID_INPUT = "invalid_input"
ERROR_AUTH = "auth_error"
ERROR_BAD_REQUEST = "bad_request"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network_error"
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_ALL_PROVIDERS_FAILED = "all_providers_failed"


SCRIPT_NAME = "llm_core"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def is_debug(cli_debug):
    return cli_debug or os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes")


def iter_sse_lines(response):
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        yield data


def start_heartbeat(provider, progress, debug):
    stop_event = threading.Event()

    def beat():
        elapsed = 0
        shown = 0
        while not stop_event.wait(HEARTBEAT_INTERVAL):
            elapsed += HEARTBEAT_INTERVAL
            chars = progress["chars"]
            log(f"[{provider}] still streaming... {elapsed}s elapsed, {chars} chars received")
            if debug and chars > shown:
                log(f"[{provider}] debug chunk: {progress['text'][shown:chars]!r}")
                shown = chars

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return stop_event


def fail(reason, detail=None):
    return {
        "success": False,
        "content": None,
        "reason": reason,
        "detail": detail,
        "truncated": False,
        "usage": None,
    }


def classify_quota_text(text):
    lowered = (text or "").lower()
    return "quota" in lowered or "resource_exhausted" in lowered or "credit" in lowered or "included usage" in lowered


def call_gemini(messages, token, model, max_tokens, temperature, thinking_budget, debug):
    system_parts = []
    raw_contents = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("system", "developer"):
            system_parts.append({"text": content})
        else:
            raw_contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]})

    contents = []
    for item in raw_contents:
        if contents and contents[-1]["role"] == item["role"]:
            contents[-1]["parts"].extend(item["parts"])
        else:
            contents.append(item)

    def build_payload(include_thinking_config):
        config = {}
        if max_tokens is not None:
            config["maxOutputTokens"] = int(max_tokens) + max(thinking_budget or 0, 0)
        if temperature is not None:
            config["temperature"] = float(temperature)
        if include_thinking_config and thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
        payload_dict = {
            "contents": contents if contents else [{"role": "user", "parts": [{"text": ""}]}],
            "generationConfig": config,
        }
        if system_parts:
            payload_dict["systemInstruction"] = {"parts": system_parts}
        return json.dumps(payload_dict).encode("utf-8")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
    include_thinking_config = thinking_budget is not None

    for attempt in range(2):
        payload = build_payload(include_thinking_config)
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": token,
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        log(f"[google] payload: {len(payload)} bytes, streaming (timeout {REQUEST_TIMEOUT}s per chunk)")
        started = time.monotonic()
        progress = {"chars": 0, "text": ""}
        heartbeat = start_heartbeat("google", progress, debug)
        answer_parts = []
        thinking_chars = 0
        finish_reason = None
        usage_meta = {}
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                for data in iter_sse_lines(response):
                    chunk = json.loads(data)
                    candidates = chunk.get("candidates") or []
                    if candidates:
                        cand = candidates[0]
                        for part in (cand.get("content") or {}).get("parts") or []:
                            text_piece = part.get("text")
                            if not text_piece:
                                continue
                            if part.get("thought"):
                                thinking_chars += len(text_piece)
                            else:
                                answer_parts.append(text_piece)
                                progress["text"] += text_piece
                                progress["chars"] = len(progress["text"])
                        if cand.get("finishReason"):
                            finish_reason = cand["finishReason"]
                    if chunk.get("usageMetadata"):
                        usage_meta = chunk["usageMetadata"]
            log(f"[google] stream completed after {time.monotonic() - started:.1f}s, {progress['chars']} chars")
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            code = e.code
            lowered = raw.lower()
            if code == 400 and include_thinking_config and "thinking budget" in lowered and "not supported" in lowered:
                log("[google] model rejects explicit thinkingConfig, retrying without it")
                include_thinking_config = False
                heartbeat.set()
                continue
            if code in (401, 403):
                return None, fail(ERROR_AUTH, f"http {code}: {raw}")
            if code in (402, 429) or classify_quota_text(lowered):
                return None, fail(ERROR_QUOTA_EXCEEDED if classify_quota_text(lowered) else ERROR_RATE_LIMIT, f"http {code}: {raw}")
            if code in (400, 404, 413, 422):
                return None, fail(ERROR_BAD_REQUEST, f"http {code}: {raw}")
            if code >= 500:
                return None, fail(ERROR_SERVER, f"http {code}: {raw}")
            return None, fail(ERROR_NETWORK, f"http {code}: {raw}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            return None, fail(ERROR_NETWORK, str(e) or "connection lost mid-stream")
        except json.JSONDecodeError as e:
            return None, fail(ERROR_NETWORK, f"invalid json chunk: {e}")
        finally:
            heartbeat.set()
    else:
        return None, fail(ERROR_BAD_REQUEST, "thinking budget rejected on retry")

    text = "".join(answer_parts)
    if not text or not text.strip():
        if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST"):
            return None, fail(ERROR_BAD_REQUEST, f"content blocked by google safety filter ({finish_reason})")
        return None, fail(ERROR_EMPTY_RESPONSE, "empty text content in response")

    thoughts_tokens = usage_meta.get("thoughtsTokenCount") or 0
    completion_tokens = usage_meta.get("candidatesTokenCount") or 0
    log(f"[google] tokens: sent={usage_meta.get('promptTokenCount')} received={completion_tokens} thinking={thoughts_tokens} total={usage_meta.get('totalTokenCount')}")
    if finish_reason == "MAX_TOKENS":
        log(f"[google] warning: hit MAX_TOKENS (thinking={thoughts_tokens}, output={completion_tokens})")
    elif thinking_budget and thoughts_tokens >= thinking_budget:
        log(f"[google] warning: thinking used its full budget ({thoughts_tokens}/{thinking_budget})")

    return {
        "content": text,
        "truncated": finish_reason == "MAX_TOKENS",
        "usage": {
            "prompt_tokens": usage_meta.get("promptTokenCount"),
            "completion_tokens": completion_tokens,
            "thinking_tokens": thoughts_tokens,
            "total_tokens": usage_meta.get("totalTokenCount"),
        },
    }, None


def call_huggingface(messages, token, model, max_tokens, temperature, debug):
    formatted_messages = [
        {**m, "role": "system" if m.get("role") == "developer" else m.get("role")}
        for m in messages
    ]

    payload = json.dumps({
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": formatted_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    for attempt in range(2):
        request = urllib.request.Request(
            HF_ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            method="POST",
        )
        log(f"[huggingface] payload: {len(payload)} bytes, streaming (timeout {REQUEST_TIMEOUT}s per chunk)")
        started = time.monotonic()
        progress = {"chars": 0, "text": ""}
        heartbeat = start_heartbeat("huggingface", progress, debug)
        answer_parts = []
        finish_reason = None
        usage = {}
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                for data in iter_sse_lines(response):
                    chunk = json.loads(data)
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        text_piece = delta.get("content")
                        if text_piece:
                            answer_parts.append(text_piece)
                            progress["text"] += text_piece
                            progress["chars"] = len(progress["text"])
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
            log(f"[huggingface] stream completed after {time.monotonic() - started:.1f}s, {progress['chars']} chars")
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            code = e.code
            if code in (502, 503, 504) and attempt == 0:
                log(f"[huggingface] http {code}, provider likely cold-starting, retrying in {HF_RETRY_DELAY}s")
                heartbeat.set()
                time.sleep(HF_RETRY_DELAY)
                continue
            if code == 401:
                return None, fail(ERROR_AUTH, f"http {code}: {raw}")
            if code == 402 or classify_quota_text(raw):
                return None, fail(ERROR_QUOTA_EXCEEDED, f"http {code}: {raw}")
            if code == 429:
                return None, fail(ERROR_RATE_LIMIT, f"http {code}: {raw}")
            if code in (400, 404, 413, 422):
                return None, fail(ERROR_BAD_REQUEST, f"http {code}: {raw}")
            if code >= 500:
                return None, fail(ERROR_SERVER, f"http {code}: {raw}")
            return None, fail(ERROR_NETWORK, f"http {code}: {raw}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if attempt == 0:
                log(f"[huggingface] connection lost mid-stream ({e}), retrying in {HF_RETRY_DELAY}s")
                heartbeat.set()
                time.sleep(HF_RETRY_DELAY)
                continue
            return None, fail(ERROR_NETWORK, str(e) or "connection lost mid-stream")
        except json.JSONDecodeError as e:
            return None, fail(ERROR_NETWORK, f"invalid json chunk: {e}")
        finally:
            heartbeat.set()
    else:
        return None, fail(ERROR_SERVER, "huggingface unavailable after retry")

    content = "".join(answer_parts)
    if not content or not content.strip():
        return None, fail(ERROR_EMPTY_RESPONSE, "empty content in response")

    log(f"[huggingface] tokens: sent={usage.get('prompt_tokens')} received={usage.get('completion_tokens')} total={usage.get('total_tokens')}")
    if finish_reason == "length":
        log(f"[huggingface] warning: output truncated by max_tokens ({max_tokens})")
    elif finish_reason and finish_reason != "stop":
        log(f"[huggingface] warning: unexpected finish_reason={finish_reason}")

    return {
        "content": content,
        "truncated": finish_reason == "length",
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    }, None


def resolve_google_token(cli_value):
    return cli_value or os.environ.get(GOOGLE_TOKEN_ENV)


def resolve_google_model(cli_value):
    return cli_value or os.environ.get(GOOGLE_MODEL_ENV, GOOGLE_DEFAULT_MODEL)


def resolve_google_thinking_budget(cli_value):
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(GOOGLE_THINKING_BUDGET_ENV)
    return int(env_value) if env_value else GOOGLE_DEFAULT_THINKING_BUDGET


def resolve_hf_token(cli_value):
    return cli_value or os.environ.get(HF_TOKEN_ENV)


def resolve_hf_model(cli_value):
    return cli_value or os.environ.get(HF_MODEL_ENV, HF_DEFAULT_MODEL)


def complete(messages, max_tokens=8192, temperature=0.7,
             google_token=None, google_model=None, thinking_budget=None,
             hf_token=None, hf_model=None, debug=False):
    if not isinstance(messages, list) or not messages:
        return fail(ERROR_INVALID_INPUT, "messages must be a non-empty list")
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            return fail(ERROR_INVALID_INPUT, "each message needs role and content")

    attempts = []

    if google_token:
        log(f"[google] request: model={google_model} messages={len(messages)} thinking_budget={thinking_budget}")
        result, error = call_gemini(messages, google_token, google_model, max_tokens, temperature, thinking_budget, debug)
        if result:
            log("status: success (google)")
            return {"success": True, "content": result["content"], "reason": None, "detail": None,
                    "truncated": result["truncated"], "usage": result["usage"], "provider": "google"}
        log(f"[google] failed ({error['reason']}), falling back to huggingface")
        attempts.append({"provider": "google", "reason": error["reason"], "detail": error["detail"]})
    else:
        attempts.append({"provider": "google", "reason": ERROR_NO_TOKEN, "detail": "no google token provided"})

    if hf_token:
        log(f"[huggingface] request: model={hf_model} messages={len(messages)}")
        result, error = call_huggingface(messages, hf_token, hf_model, max_tokens, temperature, debug)
        if result:
            log("status: success (huggingface)")
            return {"success": True, "content": result["content"], "reason": None, "detail": None,
                    "truncated": result["truncated"], "usage": result["usage"], "provider": "huggingface"}
        attempts.append({"provider": "huggingface", "reason": error["reason"], "detail": error["detail"]})
    else:
        attempts.append({"provider": "huggingface", "reason": ERROR_NO_TOKEN, "detail": "no huggingface token provided"})

    log(f"status: failed ({ERROR_ALL_PROVIDERS_FAILED})")
    result = fail(ERROR_ALL_PROVIDERS_FAILED, attempts)
    result["provider"] = None
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--google-token", default=None)
    parser.add_argument("--google-model", default=None)
    parser.add_argument("--thinking-budget", type=int, default=None)
    parser.add_argument("--hf-token", default=None)
    parser.add_argument("--hf-model", default=None)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--prompt-data", default=None,
                         help="JSON output from prompt_build.py; reads stdin if omitted")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        raw_input = args.prompt_data if args.prompt_data is not None else sys.stdin.read()
        input_data = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps(fail(ERROR_INVALID_INPUT, f"invalid json on stdin: {e}"), ensure_ascii=False))
        return

    if not input_data.get("success", True):
        result = fail("prompt_data_failed", input_data.get("reason"))
        result["provider"] = None
        print(json.dumps(result, ensure_ascii=False))
        return

    result = complete(
        messages=input_data.get("messages"),
        max_tokens=input_data.get("max_tokens", args.max_tokens),
        temperature=input_data.get("temperature", args.temperature),
        google_token=resolve_google_token(args.google_token),
        google_model=resolve_google_model(args.google_model),
        thinking_budget=resolve_google_thinking_budget(args.thinking_budget),
        hf_token=resolve_hf_token(args.hf_token),
        hf_model=resolve_hf_model(args.hf_model),
        debug=is_debug(args.debug),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
