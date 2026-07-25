#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: main.py
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
#
# Description / 描述:
#   仓库初始化总控脚本。串联 tmdb_lookup.py 与 douban_id_lookup.py 的结果，
#   按 reason 选择对应 README 模板渲染，并通过 GitHub API 回写仓库的
#   description / topics / Discussions 开关。不涉及仓库可见性（private/
#   public），该项由模板仓库初始 README 中的手动前置步骤处理。
#
# Usage / 用法:
#   python actions/init/main.py --repo-name Cosmos_Laundromat_2015 \
#       --github-repository MontageSubs/Cosmos_Laundromat_2015 \
#       --github-token $ORG_ADMIN_TOKEN \
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
import urllib.error
import urllib.request
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "tmdb", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "douban", "search"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "wiki"))
sys.path.insert(0, os.path.join(REPO_ROOT, "utilities", "github"))

import tmdb_lookup
import douban_id_lookup
import synopsis_pipeline
import secret_provision
from github_api import call_api, requires_org_admin_token

TEMPLATES_DIR = os.path.join(REPO_ROOT, "default-docs", "templates", "readme")
ERROR_TEMPLATE = os.path.join(TEMPLATES_DIR, "error", "error.md")
ERROR_FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "error", "fragments")
FRAGMENTS_DIR = os.path.join(TEMPLATES_DIR, "fragments")
HOME_TEMPLATE = os.path.join(TEMPLATES_DIR, "home.md")
HEADER_VERIFIED_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_verified.md")
HEADER_MANUAL_FRAGMENT = os.path.join(FRAGMENTS_DIR, "header_manual.md")

INIT_MARKER = "<!-- montagesubs:initialized -->"

NAMING_ERROR_REASONS = {"invalid_repo_name", "not_found", "title_mismatch", "year_mismatch"}

GITHUB_API_ENDPOINT = "https://api.github.com/repos/{full_name}"

PROVISIONABLE_SECRETS = (
    "TMDB_READ_ACCESS_TOKEN", "TAVILY_API_KEY", "SERPSTACK_API_KEY",
    "GOOGLE_LLM_TOKEN", "HUGGINGFACE_LLM_TOKEN",
)

PROTECTED_RESET_ENTRIES = {".git", ".github", ".actions"}

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
    subprocess.run(["git", "add", "-A"], check=True)
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


def enable_discussions(node_id, github_token):
    query = """
    mutation($id: ID!) {
      updateRepository(input: {repositoryId: $id, hasDiscussionsEnabled: true}) {
        repository { hasDiscussionsEnabled }
      }
    }
    """
    payload = json.dumps({"query": query, "variables": {"id": node_id}}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("errors"):
            return False, body["errors"]
        return True, None
    except urllib.error.HTTPError as e:
        return False, {"http_status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:
        return False, {"http_status": None, "body": str(e)}


AUTO_RENAME_REASONS = {"title_mismatch", "year_mismatch"}


@requires_org_admin_token("仓库重命名", default=(False, None))
def rename_repository(github_repository, github_token, new_name):
    repo_url = GITHUB_API_ENDPOINT.format(full_name=github_repository)
    ok, body = call_api(repo_url, github_token, "PATCH", {"name": new_name})
    if not ok:
        log(f"auto-rename failed: {body}")
        return False, None
    new_full_name = body.get("full_name")
    log(f"auto-renamed repository: {github_repository} -> {new_full_name}")
    return True, new_full_name


@requires_org_admin_token("仓库元数据更新", default=None)
def update_github_repo_metadata(github_repository, github_token, tmdb_result):
    repo_url = GITHUB_API_ENDPOINT.format(full_name=github_repository)

    description = (
        f"《{tmdb_result['title_zh']}》({tmdb_result['year']}) 中文字幕协作项目 | "
        f"Chinese fansub project for \"{tmdb_result['title_en']}\" ({tmdb_result['year']})"
    )
    ok, body = call_api(repo_url, github_token, "PATCH", {
        "description": description,
        "homepage": "",
        "has_wiki": False,
    })
    if ok:
        log("github repo metadata updated (description/has_wiki)")
    else:
        log(f"failed to update repo metadata: {body}")

    node_id = body.get("node_id") if ok else None
    if node_id:
        discussions_ok, discussions_err = enable_discussions(node_id, github_token)
        if discussions_ok:
            log("discussions enabled (via GraphQL)")
        else:
            log(f"failed to enable discussions: {discussions_err}")
    else:
        log("no node_id available (metadata PATCH failed), skipping discussions enable")

    topics_ok, topics_err = call_api(repo_url + "/topics", github_token, "PUT", {
        "names": build_topics(tmdb_result),
    })
    if topics_ok:
        log("github repo topics updated")
    else:
        log(f"failed to update repo topics: {topics_err}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-name", required=True, help="e.g. Cosmos_Laundromat_2015")
    parser.add_argument("--github-repository", required=True, help="e.g. MontageSubs/Cosmos_Laundromat_2015")
    parser.add_argument("--github-token", default=None, help="PAT with repo admin scope, used for description/topics update")
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
    github_token = args.github_token or os.environ.get("ORG_ADMIN_TOKEN")
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
                secret_provision.provision(
                    args.github_repository, github_token,
                    {name: os.environ.get(name) for name in PROVISIONABLE_SECRETS},
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
                renamed, new_full_name = rename_repository(args.github_repository, github_token, corrected_name)
                if renamed:
                    log(f"naming mismatch auto-corrected: {args.repo_name} -> {corrected_name}, resuming")
                    args.repo_name = corrected_name
                    args.github_repository = new_full_name
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
            renamed, new_full_name = rename_repository(args.github_repository, github_token, canonical_name)
            if renamed:
                log(f"format normalized: {args.repo_name} -> {canonical_name}")
                args.repo_name = canonical_name
                args.github_repository = new_full_name
            else:
                log("format-normalization rename unavailable, proceeding with original repo name")

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

    update_github_repo_metadata(args.github_repository, github_token, tmdb_result)
    secret_provision.provision(
        args.github_repository, github_token,
        {name: os.environ.get(name) for name in PROVISIONABLE_SECRETS},
    )
    header_block = build_verified_header(args.repo_name, tmdb_result, douban_result)
    render_home_readme(args.repo_name, header_block, forced=args.force_init)

    synopsis_result = synopsis_pipeline.run(
        tmdb_result,
        output_dir=str(workspace_dir / "docs" / "synopsis"),
        tmdb_token=tmdb_token,
        with_glossary=True,
    )

    print(json.dumps({
        "stage": "home",
        "success": True,
        "tmdb": tmdb_result,
        "douban": douban_result,
        "synopsis": synopsis_result,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
