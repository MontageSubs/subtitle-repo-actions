#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: debug_format.py
# Version: 1.1.0
# Organization: MontageSubs (蒙太奇字幕社区)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/tree/main/utilities/translation/mt/
#
# Description / 描述:
#     Converts google_client.py's raw debug JSON Lines log (--debug-raw-in /
#     --debug-raw-out) into a human-readable transcript. Each log line already
#     stores the request/response body as native JSON structure (no manual
#     string-escaping layer), plus response HTTP status and headers; this
#     tool renders that structure with indentation and additionally
#     pretty-prints any embedded HTML payload with one tag or text run per
#     line. Optionally pairs request and response files by their shared
#     sequence number so each call's send/receive is shown together.
#     将 google_client.py 的原始 debug JSON Lines 日志（--debug-raw-in /
#     --debug-raw-out）转换为人类可读文本。每行日志本身已经把请求/响应体
#     存成原生 JSON 结构（不含手工字符串转义层），并附带响应的 HTTP 状态码
#     与响应头；本工具将该结构缩进展开，并额外把其中嵌入的 HTML 按
#     标签/文本片段逐行展开。可选按共享的序号将请求与响应两个文件配对，
#     展示每次调用的完整收发过程。
#
# Usage / 用法:
#     python debug_format.py --input DEBUG_RAW_OUT.json
#     python debug_format.py --input DEBUG_RAW_OUT.json --pair DEBUG_RAW_IN.json --output readable.txt
# ============================================================================
import argparse
import json
import re
import sys

TOKEN_PATTERN = re.compile(r"(<[^>]+>)|([^<]+)")


def pretty_html(html):
    lines, depth = [], 0
    for tag, text in TOKEN_PATTERN.findall(html):
        if tag:
            closing = tag.startswith("</")
            self_closing = tag.endswith("/>")
            if closing:
                depth = max(depth - 1, 0)
            lines.append("  " * depth + tag)
            if not closing and not self_closing:
                depth += 1
        else:
            stripped = text.strip()
            if stripped:
                lines.append("  " * depth + stripped)
    return "\n".join(lines)


def find_html_string(node):
    if isinstance(node, str) and "<" in node and ">" in node:
        return node
    if isinstance(node, list):
        for item in node:
            found = find_html_string(item)
            if found:
                return found
    return None


def format_entry(entry):
    seq, ts, direction = entry["seq"], entry["ts"], entry["direction"]
    header = f"--- seq={seq} {direction} ts={ts:.3f}"
    if direction == "request":
        header += f" source={entry.get('source_lang')} target={entry.get('target_lang')}"
    else:
        header += f" status={entry.get('status')}"
    header += " ---"

    parts = [header]
    if direction == "response" and entry.get("headers"):
        parts.extend(["[headers]", json.dumps(entry["headers"], ensure_ascii=False, indent=2)])

    body = entry.get("body")
    parts.extend(["[structure]", json.dumps(body, ensure_ascii=False, indent=2)])
    html = find_html_string(body)
    if html:
        parts.extend(["[html]", pretty_html(html)])
    return "\n".join(parts)


def load_entries(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--pair", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    entries = load_entries(args.input)
    if args.pair:
        entries.extend(load_entries(args.pair))
    entries.sort(key=lambda e: (e["seq"], e["direction"]))

    blocks = [format_entry(entry) for entry in entries]
    output = "\n\n".join(blocks) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
