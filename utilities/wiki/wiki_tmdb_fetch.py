#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: wiki_tmdb_fetch.py
# Version: 1.0.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/wiki/
#
# Description / 描述:
#    Resolves a film's Wikidata entity from its IMDb ID, follows sitelinks
#    to fetch multilingual Wikipedia pages (Parsoid HTML via REST API), and
#    extracts Plot/Cast/lead sections through DOM structure alone (no LLM
#    judgment involved in extraction). Crew and cast are pulled from TMDB's
#    credits endpoint; production companies and distributor are cross-
#    checked against the English infobox. Output is the structured payload
#    intended for the prompt-assembly step ahead of the LLM core, not yet
#    the LLM call itself.
#    通过IMDb ID解析出对应的Wikidata词条，沿sitelinks拉取多语言Wikipedia
#    页面（REST API返回的Parsoid HTML），仅凭DOM结构提取剧情/演员表/开头
#    简介（提取阶段不涉及LLM判断）。演职员从TMDB的credits接口获取，制片
#    公司与发行商则与英文版infobox交叉核对。输出的是喂给LLM核心之前一步
#    提示词组装阶段的结构化数据，本脚本本身不调用LLM。
#
# Features:
#    - Resolves Wikidata entity via IMDb ID (P345), avoiding ambiguous
#      title-based Wikipedia search entirely.
#    - Plot extracted in five languages by default: en/de/fr/es/zh, plus
#      the film's original language if different.
#    - Cast extracted from the original-language page (completeness) and
#      the Chinese page (naming/translation reference), with per-language
#      actor/role separators (" as " / "饰演" / " als " / " como " / etc.).
#    - Section headings matched exactly first, falling back to keyword-
#      based fuzzy matching when a wiki page uses an unlisted heading
#      variant.
#    - Wikipedia pages are fetched once per language and cached, even when
#      needed by both the Plot and Cast extraction passes.
#
# 功能:
#    - 通过IMDb ID (P345) 解析Wikidata词条，完全避免基于片名的模糊
#      Wikipedia搜索。
#    - 默认提取五种语言的剧情：en/de/fr/es/zh，若原始语言不在其中则一并
#      加入。
#    - 演员表提取原始语言版本（信息完整）与中文版本（译名/命名参考），
#      按语言使用不同的演员/角色分隔符（" as " / "饰演" / " als " /
#      " como " 等）。
#    - 章节标题优先精确匹配，未命中时按关键词模糊匹配兜底，应对未收录
#      的标题变体。
#    - 同一语言的Wikipedia页面只抓取一次并缓存，即使Plot与Cast两个阶段
#      都需要用到。
#
# Usage / 用法:
#    python wiki_tmdb_fetch.py --imdb-id tt1234567 --tmdb-id 358332 \
#        --media-type movie --original-language en
#
#    python wiki_tmdb_fetch.py --imdb-id tt1234567 --tmdb-id 358332 \
#        --tmdb-read-access-token KEY
#
#    TMDB token is read from --tmdb-read-access-token, falling back to the
#    TMDB_READ_ACCESS_TOKEN environment variable.
#    TMDB密钥可通过--tmdb-read-access-token传入，缺省时读取
#    TMDB_READ_ACCESS_TOKEN环境变量。
#
#    --send is a placeholder for a future step that pipes the assembled
#    prompt directly into the LLM core; passing it today returns
#    not_implemented, since the prompt-assembly module has not been built
#    yet. Without it (the default), this script only prints the extracted
#    payload for manual preview and tuning.
#    --send是为未来"直接串联LLM核心"预留的参数，由于提示词组装模块尚未
#    开发，目前传入会返回not_implemented。默认（不传）只输出提取到的
#    数据供人工预览和微调。
#
# Dependencies / 依赖:
#    - beautifulsoup4 (pip install beautifulsoup4)
#
# Output / 输出:
#    Diagnostic logs (stderr) / 诊断日志（标准错误）:
#      - Wikidata resolution, each language page fetched or skipped, TMDB
#        calls, final status / Wikidata解析结果、每个语言页面的抓取或
#        跳过情况、TMDB调用、最终状态
#
#    Result data (stdout) / 结果数据（标准输出）:
#      - A single JSON object / 单个JSON对象
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
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError as e:
    print(json.dumps({
        "success": False,
        "reason": "missing_dependency",
        "detail": f"{e}; run: pip install beautifulsoup4 lxml",
    }, ensure_ascii=False))
    sys.exit(0)

VERSION = "1.0.0"
REPOSITORY = "https://github.com/MontageSubs/subtitle-repo-actions"

TMDB_READ_ACCESS_TOKEN_ENV = "TMDB_READ_ACCESS_TOKEN"
TMDB_API_BASE = "https://api.themoviedb.org/3"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_REST_HTML = "https://{lang}.wikipedia.org/api/rest_v1/page/html/{title}"
IMDB_PROPERTY = "P345"
USER_AGENT = f"wiki_tmdb_fetch/{VERSION} (+{REPOSITORY}; GitHub Actions)"
REQUEST_TIMEOUT = 20

DEFAULT_LANGUAGE_PRIORITY = ("en", "zh", "fr", "de", "es")
CAST_LANGUAGES = ("zh",)


def parse_language_priority(raw):
    if not raw:
        return DEFAULT_LANGUAGE_PRIORITY
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def resolve_plot_languages(original_language, language_priority, language_limit):
    order = list(dict.fromkeys([original_language, *language_priority]))
    return order[:language_limit] if language_limit else order

SECTION_ALIASES = {
    "plot": {
        "en": ("Plot",),
        "zh": ("劇情", "剧情", "劇情簡介", "剧情简介", "劇情大綱", "故事大綱"),
        "fr": ("Synopsis",),
        "de": ("Handlung",),
        "es": ("Argumento", "Trama"),
        "ja": ("あらすじ", "ストーリー", "概要"),
    },
    "cast": {
        "en": ("Cast",),
        "zh": ("演員", "演员", "演員表", "演员表", "配音"),
        "fr": ("Distribution",),
        "de": ("Besetzung",),
        "es": ("Reparto",),
        "ja": ("キャスト", "出演者"),
    },
}

FUZZY_KEYWORDS = {
    "plot": {
        "en": ("plot",),
        "zh": ("剧情", "劇情", "故事", "大綱", "大纲"),
        "fr": ("synopsis", "intrigue"),
        "de": ("handlung",),
        "es": ("argumento", "trama"),
        "ja": ("あらすじ", "ストーリー"),
    },
    "cast": {
        "en": ("cast",),
        "zh": ("演员", "演員", "配音", "出演"),
        "fr": ("distribution",),
        "de": ("besetzung",),
        "es": ("reparto",),
        "ja": ("キャスト", "出演"),
    },
}

INFOBOX_FIELDS = (
    "Directed by", "Written by", "Produced by", "Starring",
    "Cinematography", "Edited by", "Music by",
    "Production companies", "Distributed by",
)

ACTOR_ROLE_SEPARATORS = {
    "en": (" as ",),
    "zh": ("飾演", "饰演", "飾", "饰"),
    "fr": (" dans le rôle de ", " interprète "),
    "de": (" als ",),
    "es": (" como ",),
}

CREW_JOB_MAP = {
    "Director": "directors",
    "Writer": "writers",
    "Screenplay": "writers",
    "Producer": "producers",
    "Director of Photography": "cinematographers",
    "Editor": "editors",
    "Original Music Composer": "composers",
}

ERROR_NO_TOKEN = "no_token"
ERROR_NOT_FOUND = "not_found"
ERROR_AUTH = "auth_error"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network_error"
ERROR_NOT_IMPLEMENTED = "not_implemented"

CITATION_PATTERN = re.compile(r"\[\s*\d+\s*\]")
WHITESPACE_PATTERN = re.compile(r"\s+")


def log(message):
    print(message, file=sys.stderr)


def clean_text(text):
    text = CITATION_PATTERN.sub("", text or "")
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def classify_http_error(code):
    if code in (401, 403):
        return ERROR_AUTH
    if code == 429:
        return ERROR_RATE_LIMIT
    if code >= 500:
        return ERROR_SERVER
    return ERROR_NETWORK


def http_get(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8"), None
    except urllib.error.HTTPError as e:
        return None, {"type": classify_http_error(e.code), "detail": f"http {e.code}"}
    except Exception as e:
        return None, {"type": ERROR_NETWORK, "detail": str(e)}


def call_tmdb(path, token, params=None):
    query = urllib.parse.urlencode(params or {})
    url = f"{TMDB_API_BASE}{path}" + (f"?{query}" if query else "")
    body_text, error = http_get(url, headers={"Authorization": f"Bearer {token}", "accept": "application/json"})
    if error:
        return None, error
    return json.loads(body_text), None


def resolve_wikidata_entity(imdb_id):
    search_params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": f"haswbstatement:{IMDB_PROPERTY}={imdb_id}",
        "format": "json",
    })
    log(f"query (wikidata search): {IMDB_PROPERTY}={imdb_id}")
    body_text, error = http_get(f"{WIKIDATA_API}?{search_params}")
    if error:
        return None, error
    results = json.loads(body_text).get("query", {}).get("search", [])
    if not results:
        log("wikidata search results: none")
        return None, None

    qid = results[0]["title"]
    log(f"wikidata entity: {qid}")
    entity_params = urllib.parse.urlencode({
        "action": "wbgetentities",
        "ids": qid,
        "props": "sitelinks",
        "format": "json",
    })
    body_text, error = http_get(f"{WIKIDATA_API}?{entity_params}")
    if error:
        return None, error
    entity = json.loads(body_text)["entities"][qid]
    sitelinks = {
        key[:-4]: value["title"]
        for key, value in entity.get("sitelinks", {}).items()
        if key.endswith("wiki") and key not in ("commonswiki", "specieswiki")
    }
    return {"qid": qid, "sitelinks": sitelinks}, None


def fetch_wiki_page(lang, title):
    url = WIKIPEDIA_REST_HTML.format(lang=lang, title=urllib.parse.quote(title, safe=""))
    headers = {"Accept-Language": "zh-Hans"} if lang == "zh" else {}
    log(f"fetch (wikipedia): {lang}.wikipedia.org/{title}")
    body_text, error = http_get(url, headers=headers)
    if error:
        log(f"  skipped: {error['type']} ({error['detail']})")
        return None
    return BeautifulSoup(body_text, "html.parser")


def find_section(soup, section_type, lang):
    headings = list(soup.find_all(("h2", "h3")))
    exact_names = SECTION_ALIASES.get(section_type, {}).get(lang, ())
    for heading in headings:
        if clean_text(heading.get_text()) in exact_names:
            return heading.find_parent("section") or heading.parent

    keywords = FUZZY_KEYWORDS.get(section_type, {}).get(lang, ())
    for heading in headings:
        title = clean_text(heading.get_text())
        title_lower = title.lower()
        if any(keyword.lower() in title_lower for keyword in keywords):
            log(f"  fuzzy match ({section_type}/{lang}): {title!r}")
            return heading.find_parent("section") or heading.parent
    return None


def extract_paragraphs(container):
    paragraphs = container.find_all("p", recursive=False)
    return clean_text(" ".join(p.get_text(" ", strip=True) for p in paragraphs))


def extract_lead(soup):
    section = soup.find("section", attrs={"data-mw-section-id": "0"})
    if not section:
        return None
    return extract_paragraphs(section)


def split_actor_role(text, lang):
    for separator in ACTOR_ROLE_SEPARATORS.get(lang, ()):
        actor, found, role = text.partition(separator)
        if found:
            return actor.strip(), role.strip()
    return None, text


def extract_cast_list(section, lang):
    entries = []
    for ul in section.find_all("ul", recursive=False):
        for li in ul.find_all("li", recursive=False):
            text = clean_text(li.get_text(" ", strip=True))
            if not text:
                continue
            actor, role = split_actor_role(text, lang)
            entries.append({"actor": actor, "role": role})
    if entries:
        return entries
    for table in section.find_all("table", recursive=False):
        for row in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(("td", "th"))]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2:
                entries.append({"actor": cells[0], "role": cells[1]})
    return entries


def extract_infobox(soup):
    table = soup.select_one("table.infobox")
    if not table:
        return {}
    infobox = {}
    for row in table.find_all("tr"):
        header, cell = row.find("th"), row.find("td")
        if not header or not cell:
            continue
        label = clean_text(header.get_text(" ", strip=True))
        if label in INFOBOX_FIELDS:
            values = [clean_text(li.get_text(" ", strip=True)) for li in cell.find_all("li")]
            infobox[label] = values if values else clean_text(cell.get_text(" ", strip=True))
    return infobox


def fetch_tmdb_credits(tmdb_id, media_type, token):
    log(f"query (tmdb credits): {media_type}/{tmdb_id}/credits")
    body, error = call_tmdb(f"/{media_type}/{tmdb_id}/credits", token)
    if error:
        return None, error
    crew = {}
    for member in body.get("crew", []):
        key = CREW_JOB_MAP.get(member.get("job"))
        if key:
            crew.setdefault(key, []).append(member.get("name"))
    cast = [member.get("name") for member in body.get("cast", [])[:10]]
    return {"crew": crew, "cast": cast}, None


def fetch_tmdb_detail(tmdb_id, media_type, token):
    log(f"query (tmdb detail): {media_type}/{tmdb_id}")
    body, error = call_tmdb(f"/{media_type}/{tmdb_id}", token)
    if error:
        return None, error
    return {
        "production_companies": [c.get("name") for c in body.get("production_companies", [])],
        "runtime": body.get("runtime"),
        "budget": body.get("budget"),
        "revenue": body.get("revenue"),
    }, None


def empty_result(reason, **extra):
    result = {
        "success": False, "reason": reason, "detail": None,
        "wikidata_qid": None, "lead": None, "infobox": None,
        "plot": {}, "cast": {}, "tmdb_credits": None, "tmdb_detail": None,
    }
    result.update(extra)
    return result


def fetch(imdb_id, tmdb_id, media_type, original_language, tmdb_token, language_priority=DEFAULT_LANGUAGE_PRIORITY, language_limit=None):
    if not tmdb_token:
        log(f"status: failed ({ERROR_NO_TOKEN})")
        return empty_result(ERROR_NO_TOKEN)

    entity, error = resolve_wikidata_entity(imdb_id)
    if error:
        log(f"status: failed ({error['type']})")
        return empty_result(error["type"], detail=error["detail"])
    if not entity:
        log(f"status: failed ({ERROR_NOT_FOUND})")
        return empty_result(ERROR_NOT_FOUND, detail="no wikidata entity for this imdb id")

    sitelinks = entity["sitelinks"]
    plot_languages = resolve_plot_languages(original_language, language_priority, language_limit)
    cast_languages = list(dict.fromkeys([original_language, *CAST_LANGUAGES]))
    all_languages = list(dict.fromkeys([*plot_languages, *cast_languages]))

    page_cache = {}

    def get_page(lang):
        if lang not in page_cache:
            page_cache[lang] = fetch_wiki_page(lang, sitelinks[lang]) if lang in sitelinks else None
        return page_cache[lang]

    for lang in all_languages:
        if lang not in sitelinks:
            log(f"skip ({lang}): no sitelink")

    lead, infobox = None, None
    plot = {}
    for lang in plot_languages:
        soup = get_page(lang)
        if soup is None:
            continue
        if lang in ("en", original_language) and lead is None:
            lead = {"lang": lang, "text": extract_lead(soup)}
        if lang == "en" and infobox is None:
            infobox = extract_infobox(soup)
        section = find_section(soup, "plot", lang)
        if section:
            plot[lang] = extract_paragraphs(section)
        else:
            log(f"plot section not found ({lang}), check SECTION_ALIASES/FUZZY_KEYWORDS")

    cast = {}
    for lang in cast_languages:
        soup = get_page(lang)
        if soup is None:
            continue
        section = find_section(soup, "cast", lang)
        if section:
            cast[lang] = extract_cast_list(section, lang)
        else:
            log(f"cast section not found ({lang}), check SECTION_ALIASES/FUZZY_KEYWORDS")

    tmdb_credits, error = fetch_tmdb_credits(tmdb_id, media_type, tmdb_token)
    if error:
        log(f"status: failed ({error['type']})")
        return empty_result(error["type"], detail=error["detail"])

    tmdb_detail, error = fetch_tmdb_detail(tmdb_id, media_type, tmdb_token)
    if error:
        log(f"status: failed ({error['type']})")
        return empty_result(error["type"], detail=error["detail"])

    log("status: success")
    return {
        "success": True, "reason": None, "detail": None,
        "wikidata_qid": entity["qid"],
        "lead": lead, "infobox": infobox,
        "plot": plot, "cast": cast,
        "tmdb_credits": tmdb_credits, "tmdb_detail": tmdb_detail,
    }


def resolve_tmdb_token(cli_value):
    token = cli_value or os.environ.get(TMDB_READ_ACCESS_TOKEN_ENV)
    return token.strip() if token else token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--imdb-id", required=True)
    parser.add_argument("--tmdb-id", required=True, type=int)
    parser.add_argument("--media-type", default="movie", choices=("movie", "tv"))
    parser.add_argument("--original-language", default="en")
    parser.add_argument("--language-priority", default=None,
                         help="comma-separated language codes, e.g. en,zh,fr,de,es,ja")
    parser.add_argument("--language-limit", type=int, default=None)
    parser.add_argument("--tmdb-read-access-token", default=None)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if args.send:
        log(f"status: failed ({ERROR_NOT_IMPLEMENTED})")
        print(json.dumps(empty_result(ERROR_NOT_IMPLEMENTED, detail="prompt-assembly module not built yet"), ensure_ascii=False))
        return

    result = fetch(
        imdb_id=args.imdb_id,
        tmdb_id=args.tmdb_id,
        media_type=args.media_type,
        original_language=args.original_language,
        tmdb_token=resolve_tmdb_token(args.tmdb_read_access_token),
        language_priority=parse_language_priority(args.language_priority),
        language_limit=args.language_limit,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
