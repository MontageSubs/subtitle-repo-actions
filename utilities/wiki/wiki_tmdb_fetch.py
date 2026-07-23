#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "beautifulsoup4", "lxml"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60
        )
        from bs4 import BeautifulSoup
    except Exception as e:
        print(json.dumps({
            "success": False,
            "reason": "dependency_install_failed",
            "detail": str(e)
        }, ensure_ascii=False))
        sys.exit(0)

VERSION = "1.0.1"
REPOSITORY = "https://github.com/MontageSubs/subtitle-repo-actions"

TMDB_READ_ACCESS_TOKEN_ENV = "TMDB_READ_ACCESS_TOKEN"
TMDB_API_BASE = "https://api.themoviedb.org/3"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_REST_HTML = "https://{lang}.wikipedia.org/api/rest_v1/page/html/{title}"
WIKIPEDIA_PAGE_URL = "https://{lang}.wikipedia.org/wiki/{title}"
IMDB_PROPERTY = "P345"
USER_AGENT = f"wiki_tmdb_fetch/{VERSION} (+{REPOSITORY}; GitHub Actions)"
REQUEST_TIMEOUT = 20

DEFAULT_LANGUAGE_PRIORITY = ("en", "zh", "fr", "de", "es")
CAST_LANGUAGES = ("zh",)

LANGUAGE_DISPLAY_NAMES = {
    "en": "English",
    "zh": "中文",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ja": "日本語",
    "pt": "Português",
    "ko": "한국어",
    "ru": "Русский",
}

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


def fetch_tmdb_overview_zh(tmdb_id, media_type, token):
    log(f"query (tmdb overview zh): {media_type}/{tmdb_id}")
    body, error = call_tmdb(f"/{media_type}/{tmdb_id}", token, params={"language": "zh-CN"})
    if error:
        return None, error
    return body.get("overview"), None


def build_wiki_links(plot_languages, plot, sitelinks):
    links = []
    for lang in plot_languages:
        if lang in plot and lang in sitelinks:
            url = WIKIPEDIA_PAGE_URL.format(
                lang=lang,
                title=urllib.parse.quote(sitelinks[lang], safe="")
            )
            links.append({
                "lang": lang,
                "label": LANGUAGE_DISPLAY_NAMES.get(lang, lang),
                "title": sitelinks[lang],
                "url": url,
            })
    return links


def parse_language_priority(raw):
    if not raw:
        return DEFAULT_LANGUAGE_PRIORITY
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def resolve_plot_languages(original_language, language_priority, language_limit):
    order = list(dict.fromkeys([original_language, *language_priority]))
    return order[:language_limit] if language_limit else order


def empty_result(reason, **extra):
    result = {
        "success": False, "reason": reason, "detail": None,
        "wikidata_qid": None, "lead": None, "infobox": None,
        "plot": {}, "cast": {}, "tmdb_credits": None, "tmdb_detail": None,
        "overview_zh": None, "wiki_links": [],
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

    overview_zh, error = fetch_tmdb_overview_zh(tmdb_id, media_type, tmdb_token)
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
        "overview_zh": overview_zh,
        "wiki_links": build_wiki_links(plot_languages, plot, sitelinks),
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
    args = parser.parse_args()

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
