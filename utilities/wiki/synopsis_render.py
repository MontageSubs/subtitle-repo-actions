#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: synopsis_render.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/wiki/
#
# Description / 描述:
#    Final rendering step of the wiki-synopsis pipeline. Parses llm_core.py's
#    raw LLM output into the five required sections and two glossary tables,
#    cleans up prose formatting, and fills them into the SYNOPSIS.md /
#    GLOSSARY.md templates. Pure text assembly, no network calls, no LLM
#    judgment involved.
#    wiki剧情摘要流水线的最终渲染步骤。将llm_core.py返回的原始LLM输出解析
#    为五个必需分节与两张术语表，修正排版格式，填入SYNOPSIS.md/GLOSSARY.md
#    模板。纯文本组装，不涉及网络请求，不涉及LLM判断。
#
# Usage / 用法:
#    python synopsis_render.py --title-en "Backrooms" --title-zh 后室 \
#        --year 2026 --wiki-data '{...}' --llm-data '{...}' \
#        --output-dir . --with-glossary
#
#    Without --wiki-data/--llm-data, reads a single merged JSON object
#    {"wiki": {...}, "llm": {...}} from stdin instead.
#    若不传--wiki-data/--llm-data，则从stdin读取合并后的单个JSON对象
#    {"wiki": {...}, "llm": {...}}。
#
#    --output-dir is a convenience that writes SYNOPSIS.md (and GLOSSARY.md
#    when --with-glossary is set) using default filenames; --synopsis-out /
#    --glossary-out override individual paths.
#    --output-dir会以默认文件名写入SYNOPSIS.md（若指定--with-glossary则一并
#    写入GLOSSARY.md）；--synopsis-out/--glossary-out可分别指定具体路径。
#
#    --with-glossary should be skipped on routine manual reruns, since
#    GLOSSARY.md is meant for human maintenance after its first generation.
#    日常人工重跑时应省略--with-glossary，因为GLOSSARY.md首次生成后即转为
#    人工维护。
#
# Dependencies / 依赖:
#    - none beyond the standard library
#
# Output / 输出:
#    Rendered files / 渲染产出文件:
#      - SYNOPSIS.md, and GLOSSARY.md when --with-glossary is set
#
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - section-fallback warnings, written file paths
#        分节回退警告、已写入文件路径
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object with success flag and written file paths
#        单个JSON对象，包含成功标志与已写入文件路径
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
import re
import sys

PROVIDER_DISPLAY_NAMES = {
    "google": "Google Gemini",
    "huggingface": "Hugging Face",
}

REQUIRED_SECTIONS = ("人物与译名对照", "情节线", "背景故事", "剧情", "主题")

SECTION_PATTERN = re.compile(r"^## (.+?)\s*$\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
TABLE_PATTERN = re.compile(r"(\|.+\|(?:\n\|.+\|)+)")
EMPHASIS_PATTERN = re.compile(r"(\*\*[^\n*]+?\*\*|\*[^\n*]+?\*)")


def log(message):
    print(message, file=sys.stderr)


def fail(reason, detail=None):
    print(json.dumps({"success": False, "reason": reason, "detail": detail}, ensure_ascii=False))
    sys.exit(0)


def fix_emphasis_spacing(text):
    out = []
    last = 0
    for m in EMPHASIS_PATTERN.finditer(text):
        before = text[last:m.start()]
        if before and re.match(r"\w", before[-1]):
            before += " "
        out.append(before)
        out.append(m.group(0))
        last = m.end()
        next_char = text[last:last + 1]
        if next_char and re.match(r"\w", next_char):
            out.append(" ")
    out.append(text[last:])
    return "".join(out)


def normalize_paragraphs(text):
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        stripped = line.strip()
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else None
        plain = stripped and not stripped.startswith(("#", "|")) and "<br>" not in stripped
        next_plain = next_line and not next_line.startswith(("#", "|")) and "<br>" not in next_line
        if plain and next_plain:
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def clean_prose(text):
    return normalize_paragraphs(fix_emphasis_spacing(text))


def read_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def resolve_templates_dir(cli_value):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if cli_value:
        candidates.append(cli_value)
    candidates.append(os.path.join(script_dir, "..", "..", "default-docs", "templates", "synopsis"))
    candidates.append(os.path.join(os.getcwd(), "default-docs", "templates", "synopsis"))
    candidates.append(os.path.join(script_dir, "default-docs", "templates", "synopsis"))
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if os.path.isfile(os.path.join(normalized, "SYNOPSIS.md")):
            return normalized
    tried = [os.path.normpath(c) for c in candidates]
    fail("templates_not_found", f"none of these contain SYNOPSIS.md: {tried}; pass --templates-dir explicitly")


def parse_llm_sections(content):
    padded = fix_emphasis_spacing(content.strip()) + "\n"
    return {name.strip(): body.strip() for name, body in SECTION_PATTERN.findall(padded)}


def resolve_required_sections(sections):
    resolved = {}
    for name in REQUIRED_SECTIONS:
        if name in sections:
            resolved[name] = sections[name]
            continue
        fallback = next((h for h in sections if name in h), None)
        if fallback is None:
            return resolved, name
        log(f"warning: section header '{name}' not found verbatim, using '{fallback}' instead")
        resolved[name] = sections[fallback]
    return resolved, None


def split_glossary_tables(tables_block):
    tables = TABLE_PATTERN.findall(tables_block)
    if len(tables) != 2:
        return None, None
    return tables[0].strip(), tables[1].strip()


def render_wiki_links_line(title_en, year, wiki_links):
    anchors = " · ".join(f"[{link['label']}]({link['url']})" for link in wiki_links)
    heading = f"**{title_en} ({year})**"
    return f"{heading} · {anchors}" if anchors else heading


def provider_display(provider):
    return PROVIDER_DISPLAY_NAMES.get(provider, provider or "AI")


def load_json_arg(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        fail("invalid_input", str(e))


def resolve_inputs(args):
    if args.wiki_data is not None and args.llm_data is not None:
        return load_json_arg(args.wiki_data), load_json_arg(args.llm_data)
    merged = load_json_arg(sys.stdin.read())
    return merged.get("wiki") or {}, merged.get("llm") or {}


def resolve_output_paths(args):
    synopsis_out = args.synopsis_out
    glossary_out = args.glossary_out
    if synopsis_out is None and args.output_dir:
        synopsis_out = os.path.join(args.output_dir, "SYNOPSIS.md")
    if glossary_out is None and args.output_dir and args.with_glossary:
        glossary_out = os.path.join(args.output_dir, "GLOSSARY.md")
    return synopsis_out, glossary_out


def write_file(path, content):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title-en", required=True)
    parser.add_argument("--title-zh", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--wiki-data", default=None,
                         help="JSON with overview_zh/wiki_links; reads merged stdin if omitted")
    parser.add_argument("--llm-data", default=None,
                         help="JSON output from llm_core.py; reads merged stdin if omitted")
    parser.add_argument("--templates-dir", default=None,
                         help="override auto-detected default-docs/templates/synopsis location")
    parser.add_argument("--output-dir", default=None,
                         help="convenience: write SYNOPSIS.md/GLOSSARY.md here using default filenames")
    parser.add_argument("--synopsis-out", default=None)
    parser.add_argument("--glossary-out", default=None)
    parser.add_argument("--with-glossary", action="store_true",
                         help="also render GLOSSARY.md (skip on routine manual reruns)")
    args = parser.parse_args()

    wiki_result, llm_result = resolve_inputs(args)

    if not llm_result.get("success"):
        fail("upstream_llm_failed", llm_result.get("reason"))
    if not wiki_result.get("success", True):
        fail("upstream_wiki_failed", wiki_result.get("reason"))

    sections = parse_llm_sections(llm_result["content"])
    resolved, missing_name = resolve_required_sections(sections)
    if missing_name:
        fail("malformed_llm_output", f"missing section: {missing_name}")

    table_cast, table_production = split_glossary_tables(resolved["人物与译名对照"])
    if table_cast is None:
        fail("malformed_llm_output", "expected exactly two tables in 人物与译名对照")

    synopsis_out, glossary_out = resolve_output_paths(args)
    if synopsis_out is None:
        fail("invalid_input", "either --synopsis-out or --output-dir is required")

    templates_dir = resolve_templates_dir(args.templates_dir)
    provider = provider_display(llm_result.get("provider"))
    wiki_links_line = render_wiki_links_line(args.title_en, args.year, wiki_result.get("wiki_links") or [])

    write_file(synopsis_out, read_template(os.path.join(templates_dir, "SYNOPSIS.md")).format(
        title_zh=args.title_zh, year=args.year,
        overview_zh=fix_emphasis_spacing(wiki_result.get("overview_zh") or ""),
        table_cast=table_cast, table_production=table_production,
        plot_outline=clean_prose(resolved["情节线"]),
        background=clean_prose(resolved["背景故事"]),
        synopsis=clean_prose(resolved["剧情"]),
        theme=clean_prose(resolved["主题"]),
        wiki_links_line=wiki_links_line, provider=provider,
    ))
    log(f"wrote: {synopsis_out}")

    if glossary_out:
        write_file(glossary_out, read_template(os.path.join(templates_dir, "GLOSSARY.md")).format(
            title_zh=args.title_zh, year=args.year,
            table_cast=table_cast, table_production=table_production,
            wiki_links_line=wiki_links_line, provider=provider,
        ))
        log(f"wrote: {glossary_out}")

    print(json.dumps({
        "success": True, "reason": None,
        "synopsis_path": synopsis_out,
        "glossary_path": glossary_out,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
    except Exception as e:
        print(json.dumps({"success": False, "reason": "exception", "detail": str(e)}, ensure_ascii=False))
