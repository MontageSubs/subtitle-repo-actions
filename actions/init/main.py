#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   仓库初始化总控脚本。串联 tmdb_lookup.py 与 douban_id_lookup.py 的结果，
#   按 reason 选择对应 README 模板渲染。仓库改名、description/topics/
#   Discussions 开关、仓库级 secret 下沉均不再由本脚本直接调用 GitHub API
#   完成，而是打包为 admin_request，通过 dispatch_client 转发给
#   montagesubs-secure/org-admin-bridge 异步执行（发出即返回，不等待结果）；
#   ORG_ADMIN_TOKEN 从此只存在于该桥接仓库内，不进入任何字幕仓库。不涉及
#   仓库可见性（private/public），该项由模板仓库初始 README 中的手动前置
#   步骤处理。
#
# Usage / 用法:
#   python actions/init/main.py --repo-name Cosmos_Laundromat_2015 \
#       --github-repository MontageSubs/Cosmos_Laundromat_2015 \
#       --tmdb-read-access-token $TMDB_READ_ACCESS_TOKEN \
#       --tavily-api-key $TAVILY_API_KEY
#
# 设计原则 / Design principle:
#   文案（README 措辞）与逻辑（本脚本）分离：所有面向用户的文本均来自
#   default-docs/templates/ 下的 .md 文件，本脚本只负责“选文件 + 填变量”，
#   不做任何文本拼接或按语言/段落切割的解析。
#
# force_init 语义 / force_init semantics:
#   幂等检查（README 是否已带 INIT_MARKER）在 force_init 下被忽略；一旦进入
#   实际渲染分支（TMDB 校验通过，或 reason=not_found 时的空白模板），会先
#   清空工作区（.git、.github 除外）再按 manifest 重新铺设默认文件，等效于
#   “建新目录填充、删旧目录、把新目录改名为旧名”，但通过 git commit 完成，
#   不重写、不删除任何历史记录。
#   The idempotency check (whether README already carries INIT_MARKER) is
#   bypassed under force_init; once an actual render branch is reached (TMDB
#   validation passed, or reason=not_found with force_init), the workspace is
#   wiped clean (except .git and .github) before manifest files are laid down
#   again — equivalent to "build a new folder, delete the old one, rename the
#   new one to the old name", but done via git commits, never rewriting or
#   deleting history.
# ============================================================================
import argparse
import json
import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "douban", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "wiki"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github", "env"))

import tmdb_lookup
import douban_id_lookup
from github_api import is_debug
from repo_vars import load_repo_vars
import dispatch_client

TEMPLATES_DIR = os.path.join(REPO_ROOT, "default-docs", "templates", "readme")
ERROR_TEMPLATE = os.path.join(TEMPLATES_DIR, "error", "error.md")
ERROR_FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "error", "fragments")
FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "fragments")
HOME_TEMPLATE = os.path.join(TEMPLATES_DIR, "home.md")
HEADER_VERIFIED_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_verified.md")
HEADER_MANUAL_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_manual.md")

INIT_MARKER = "<!-- montagesubs:initialized -->"

NAMING_ERROR_REASONS = {"invalid_repo_name", "not_found", "title_mismatch", "year_mismatch"}

PROVISIONABLE_SECRETS = (
    "TMDB_READ_ACCESS_TOKEN", "TAVILY_API_KEY", "SERPSTACK_API_KEY",
    "GOOGLE_LLM_TOKEN", "HUGGINGFACE_LLM_TOKEN", "GOOGLE_TRANSLATE_API_KEY",
    "SECURE_DISPATCH_TOKEN",
)

PROTECTED_RESET_ENTRIES = {".git", ".github", ".actions"}

GIT_COMMIT_EXCLUDED_ENTRIES = {".actions"}
GIT_COMMIT_EXCLUDE_PATHSPECS = tuple(f":!{entry}" for entry in GIT_COMMIT_EXCLUDED_ENTRIES)


def git_add_all():
    subprocess.run(["git", "add", "-A", "--", ".", *GIT_COMMIT_EXCLUDE_PATHSPECS], check=True)

TOPIC_MAP = {
    "movie": [
        "movies", "translation", "movie", "subtitles", "subtitle",
        "chinese", "chinese-translation", "film", "films", "filmmaking",
        "movie-translator",
    ],
    "tv": [
        "tv-series", "translation", "series", "subtitles", "subtitle",
        "chinese", "chinese-translation", "tv-show", "tv-shows",
        "tv-translator",
    ],
}

SLUG_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9]+")

BLOCK_EXTRACT_PATTERN = re.compile(r"<!--\s*block:(\w+)\s*-->(.*?)<!--\s*/block:\1\s*-->", re.DOTALL)

CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

def slugify_title(title_en):
    slug = SLUG_INVALID_CHARS_PATTERN.sub("-", title_en.lower()).strip("-")
    return slug


def build_topics(tmdb_result):
    media_type = tmdb_result.get("media_type") or "movie"
    topics = list(TOPIC_MAP.get(media_type, TOPIC_MAP["movie"]))
    slug = slugify_title(tmdb_result["title_en"])
    if slug and slug not in topics:
        topics.append(slug)
    return topics

ERROR_COPY_JSON_PATTERN = re.compile(
    r"<!--\s*ERROR_COPY_JSON\s*(.*?)-->\s*(.*)", re.DOTALL
)


def setup_git_identity():
    actor = os.environ.get("GITHUB_ACTOR")
    actor_id = os.environ.get("GITHUB_ACTOR_ID")
    if actor and actor_id:
        subprocess.run(f'git config user.name "{actor}"', shell=True, check=True)
        subprocess.run(f'git config user.email "{actor_id}+{actor}@users.noreply.github.com"', shell=True, check=True)


def reset_workspace(workspace_dir):
    for entry in workspace_dir.iterdir():
        if entry.name in PROTECTED_RESET_ENTRIES:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        log(f"force_init: removed {entry.relative_to(workspace_dir)}")
    git_add_all()
    subprocess.run(
        'git diff --staged --quiet || git commit -m "reset: force re-initialization"',
        shell=True, check=True,
    )


MANIFEST_TABLE_SEPARATOR_PATTERN = re.compile(r"^\|[\s:|-]+\|$")


def parse_manifest_table(manifest_path):
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    separator_index = next(
        (i for i, line in enumerate(lines) if MANIFEST_TABLE_SEPARATOR_PATTERN.match(line.strip())),
        None,
    )
    if separator_index is None:
        return []

    rows = []
    for line in lines[separator_index + 1:]:
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            break
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 4:
            rows.append(cells[:4])
    return rows


def apply_init_manifest(manifest_path, source_root, dest_root, overwrite):
    if not manifest_path.exists():
        return
    commits = {}
    for action, source, destination, commit_message in parse_manifest_table(manifest_path):
        dest_path = dest_root / destination
        if dest_path.exists() and not overwrite:
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if action == "copy":
            shutil.copy2(source_root / source, dest_path)
            log(f"manifest: copied {source} -> {destination}")
        elif action == "touch":
            dest_path.touch(exist_ok=True)
            log(f"manifest: touched {destination}")
        else:
            log(f"unknown manifest action {action!r} for {destination}, skipping")
            continue
        commits.setdefault(commit_message, []).append(dest_path)
    for msg, files in commits.items():
        for file_path in files:
            subprocess.run(["git", "add", str(file_path)], check=True)
        subprocess.run(f'git diff --staged --quiet || git commit -m "{msg}"', shell=True, check=True)


SCRIPT_NAME = "init_main"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def mark_rendered(github_repository):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        f.write("rendered=true\n")
        f.write(f"github_repository={github_repository}\n")


def read_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def already_initialized():
    readme_path = os.path.join(os.getcwd(), "README.md")
    if not os.path.exists(readme_path):
        return False
    with open(readme_path, "r", encoding="utf-8") as f:
        return INIT_MARKER in f.read()


def write_readme(content):
    with open(os.path.join(os.getcwd(), "README.md"), "w", encoding="utf-8") as f:
        f.write(content)


def render_error_readme(tmdb_result, repo_name):
    reason = tmdb_result["reason"]

    context = {"input_repo_name": repo_name, "zh_fix": "", "en_fix": ""}
    if tmdb_result.get("expected_title") and tmdb_result.get("expected_year"):
        context["suggested_repo_name"] = tmdb_lookup.to_repo_name(
            tmdb_result["expected_title"], tmdb_result["expected_year"],
        )
        fix_type = "rename"
    else:
        fix_type = "not_found" if reason == "not_found" else "manual_format"

    raw = read_template(ERROR_TEMPLATE)
    match = ERROR_COPY_JSON_PATTERN.match(raw)
    if not match:
        log("error.md is missing its ERROR_COPY_JSON block, aborting without README change")
        return False

    copy_table = json.loads(match.group(1))
    content = match.group(2)

    if reason not in copy_table:
        log(f"no copy entry for reason={reason} in ERROR_COPY_JSON, aborting without README change")
        return False

    for lang, entry in copy_table[reason].items():
        for key, val in entry.items():
            context[f"{lang}_{key}"] = val.format(**context)

    frag_path = os.path.join(ERROR_FRAGMENTS_DIR, f"fix_{fix_type}.md")
    if os.path.exists(frag_path):
        frag_raw = read_template(frag_path)
        for block_match in BLOCK_EXTRACT_PATTERN.finditer(frag_raw):
            lang = block_match.group(1)
            context[f"{lang}_fix"] = block_match.group(2).strip()

    for key, value in context.items():
        content = content.replace(f"{{{key}}}", str(value))

    write_readme(content)
    log(f"README rendered from error.md (reason={reason})")
    return True

def build_douban_warning_block(douban_result):
    if douban_result["success"]:
        return ""
    if douban_result["reason"] == "low_confidence":
        candidates_list = "\n".join(
            f"> - [{c['title']}]({c['url']}) (id: {c['id']})"
            for c in douban_result["candidates"]
        )
        return read_template(
            os.path.join(FRAGMENTS_DIR, "douban_low_confidence.md")
        ).format(candidates_list=candidates_list)
    return read_template(os.path.join(FRAGMENTS_DIR, "douban_not_found.md"))


def build_verified_header(repo_name, tmdb_result, douban_result):
    douban_id = douban_result["douban_id"] or "待核实"
    douban_url = (
        f"https://m.douban.com/movie/subject/{douban_result['douban_id']}"
        if douban_result["success"]
        else "#"
    )
    return read_template(HEADER_VERIFIED_FRAGMENT).format(
        title_zh=tmdb_result["title_zh"] or tmdb_result["title_en"],
        title_en=tmdb_result["title_en"],
        year=tmdb_result["year"],
        poster_url=(
            f"https://image.tmdb.org/t/p/w500{tmdb_result['poster_path']}"
            if tmdb_result.get("poster_path")
            else ""
        ),
        overview_zh=tmdb_result["overview_zh"] or "（暂无简介）",
        douban_id=douban_id,
        douban_url=douban_url,
        imdb_id=tmdb_result["imdb_id"] or "N/A",
        imdb_url=(
            f"https://www.imdb.com/title/{tmdb_result['imdb_id']}/"
            if tmdb_result.get("imdb_id")
            else "#"
        ),
        tmdb_id=tmdb_result["tmdb_id"],
        tmdb_url=f"https://www.themoviedb.org/{tmdb_result['media_type']}/{tmdb_result['tmdb_id']}?language=zh-CN",
        repo_name=repo_name,
        status_badge_text="制作中",
        status_badge_color="orange",
        progress_percent=0,
        version="v1.0",
        douban_warning_block=build_douban_warning_block(douban_result),
    )


def build_manual_header(repo_name, tmdb_result):
    return read_template(HEADER_MANUAL_FRAGMENT).format(
        input_title=tmdb_result["input_title"] or repo_name,
        input_year=tmdb_result["input_year"] or "未知",
    )


def render_home_readme(repo_name, header_block, forced, github_repository=""):
    owner = github_repository.split("/")[0] if github_repository else ""
    content = read_template(HOME_TEMPLATE)
    
    context = {
        "header_block": header_block,
        "repo_name": repo_name,
        "用户名": owner
    }
    for key, value in context.items():
        content = content.replace(f"{{{key}}}", str(value))
        
    write_readme(content)
    log(f"README rendered from home.md (forced={forced})")


AUTO_RENAME_REASONS = {"title_mismatch", "year_mismatch"}


def build_repo_description(tmdb_result):
    title_zh_raw = tmdb_result["title_zh"]
    title_display = (
        f"《{title_zh_raw}》({tmdb_result['year']})"
        if title_zh_raw and CJK_PATTERN.search(title_zh_raw)
        else f"{tmdb_result['title_en']} ({tmdb_result['year']})"
    )
    return (
        f"{title_display} 中文字幕协作项目 | "
        f"Chinese fansub project for \"{tmdb_result['title_en']}\" ({tmdb_result['year']})"
    )


def write_github_output(name, value):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def dispatch_admin_request(github_repository, rename_to=None, tmdb_result=None, provision_secrets=None):
    request = {
        "repository": github_repository,
        "correlation_id": dispatch_client.new_correlation_id(),
        "provision_secrets": {k: v for k, v in (provision_secrets or {}).items() if v},
    }
    if rename_to:
        request["rename_to"] = rename_to
    if tmdb_result:
        request["description"] = build_repo_description(tmdb_result)
        request["topics"] = build_topics(tmdb_result)
    write_github_output("admin_request", json.dumps(request, ensure_ascii=False))
    return request


def main():
    load_repo_vars()

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-name", required=True, help="e.g. Cosmos_Laundromat_2015")
    parser.add_argument("--github-repository", required=True, help="e.g. MontageSubs/Cosmos_Laundromat_2015")
    parser.add_argument("--tmdb-read-access-token", default=None)
    parser.add_argument("--tavily-api-key", default=None)
    parser.add_argument("--serpstack-api-key", default=None)
    parser.add_argument(
        "--manual-id", default=None,
        help="手动指定 TMDB/IMDb ID 或其页面 URL，跳过按仓库名的 TMDB 搜索与命名校验，直接按该 ID 拉取详情",
    )
    parser.add_argument(
        "--force-init", action="store_true",
        help=(
            "强制初始化：忽略仓库是否已初始化，清空工作区（.git/.github 除外）"
            "后按 manifest 重新铺设默认文件并重新渲染 README；"
            "reason=not_found 时额外跳过命名校验，生成空白待填写模板"
        ),
    )
    args = parser.parse_args()

    tmdb_token = args.tmdb_read_access_token or os.environ.get("TMDB_READ_ACCESS_TOKEN")
    tavily_key = args.tavily_api_key or os.environ.get("TAVILY_API_KEY")
    serpstack_key = args.serpstack_api_key or os.environ.get("SERPSTACK_API_KEY")

    log(f"start: repo_name={args.repo_name} github_repository={args.github_repository} force_init={args.force_init} manual_id={args.manual_id!r}")

    if already_initialized() and not args.force_init:
        log("README.md already carries the init marker, this repo was initialized before — skipping to avoid overwriting manual edits")
        print(json.dumps({"stage": "idempotency", "success": True, "skipped": True}, ensure_ascii=False))
        sys.exit(0)

    setup_git_identity()
    repo_root = Path(REPO_ROOT)
    workspace_dir = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    manifest_path = Path(__file__).parent / "manifest.md"

    pending_rename_to = None
    if args.manual_id:
        tmdb_result = tmdb_lookup.resolve_manual(args.manual_id, tmdb_token)
        if not tmdb_result["success"]:
            reason = tmdb_result["reason"]
            log(f"manual id resolution failed ({reason}), leaving README untouched")
            print(json.dumps({"stage": "tmdb_manual", "success": False, "reason": reason}, ensure_ascii=False))
            sys.exit(1)
    else:
        tmdb_result = tmdb_lookup.resolve(args.repo_name, tmdb_token)

        if not tmdb_result["success"]:
            reason = tmdb_result["reason"]
            if reason == "not_found" and args.force_init:
                reset_workspace(workspace_dir)
                apply_init_manifest(manifest_path, repo_root, workspace_dir, overwrite=True)
                dispatch_admin_request(
                    args.github_repository,
                    provision_secrets={name: os.environ.get(name) for name in PROVISIONABLE_SECRETS},
                )
                header_block = build_manual_header(args.repo_name, tmdb_result)
                render_home_readme(args.repo_name, header_block, forced=True)
                print(json.dumps({"stage": "manual", "success": True, "forced": True}, ensure_ascii=False))
                sys.exit(0)
            if (
                reason in AUTO_RENAME_REASONS
                and tmdb_result.get("expected_title")
                and tmdb_result.get("expected_year")
            ):
                corrected_name = tmdb_lookup.to_repo_name(
                    tmdb_result["expected_title"], tmdb_result["expected_year"],
                )
                owner = args.github_repository.split("/")[0]
                log(f"naming mismatch auto-corrected: {args.repo_name} -> {corrected_name}, resuming (rename dispatched to org-admin-bridge, not yet applied)")
                pending_rename_to = corrected_name
                args.repo_name = corrected_name
                args.github_repository = f"{owner}/{corrected_name}"
                tmdb_result = tmdb_lookup.resolve(args.repo_name, tmdb_token)
            if not tmdb_result["success"]:
                reason = tmdb_result["reason"]
                if reason in NAMING_ERROR_REASONS:
                    rendered = render_error_readme(tmdb_result, args.repo_name)
                    print(json.dumps({"stage": "tmdb", "success": False, "rendered_error_readme": rendered}, ensure_ascii=False))
                    sys.exit(0)
                log(f"infra-level failure ({reason}), leaving README untouched")
                print(json.dumps({"stage": "tmdb", "success": False, "reason": reason}, ensure_ascii=False))
                sys.exit(1)

        if tmdb_result.get("needs_rename"):
            canonical_name = tmdb_result["canonical_repo_name"]
            owner = args.github_repository.split("/")[0]
            log(f"format normalized: {args.repo_name} -> {canonical_name} (rename dispatched to org-admin-bridge, not yet applied)")
            pending_rename_to = canonical_name
            args.repo_name = canonical_name
            args.github_repository = f"{owner}/{canonical_name}"

    if args.force_init:
        reset_workspace(workspace_dir)
    apply_init_manifest(manifest_path, repo_root, workspace_dir, overwrite=args.force_init)

    douban_query = f"{tmdb_result['title_en']} {tmdb_result['year']}"
    douban_result = douban_id_lookup.resolve(
        douban_query,
        tavily_key,
        serpstack_key,
        title_hints=[tmdb_result["title_zh"], tmdb_result["title_en"]],
    )

    admin_request = dispatch_admin_request(
        args.github_repository,
        rename_to=pending_rename_to,
        tmdb_result=tmdb_result,
        provision_secrets={name: os.environ.get(name) for name in PROVISIONABLE_SECRETS},
    )
    header_block = build_verified_header(args.repo_name, tmdb_result, douban_result)
    render_home_readme(args.repo_name, header_block, forced=args.force_init)
    mark_rendered(args.github_repository)

    log(f"status: success (douban={douban_result.get('success')}, admin_request={admin_request['correlation_id']})")
    if is_debug():
        print(json.dumps({
            "stage": "home",
            "success": True,
            "tmdb": tmdb_result,
            "douban": douban_result,
            "admin_request": admin_request,
        }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
