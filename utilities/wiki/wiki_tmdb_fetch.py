#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: wiki_tmdb_fetch.py
# Version: 1.5.0
# Organization: MontageSubs (蒙太奇字幕组)
# Contributors: Meow P (小p)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/utilities/wiki/
#
# Description / 描述:
#    Resolves a film's Wikidata entity from its IMDb ID, follows sitelinks
#    to fetch multilingual Wikipedia pages (Parsoid HTML via REST API), and
#    extracts Plot/Cast/Reception/infobox through DOM structure alone (no
#    LLM judgment involved in extraction). Crew and cast are pulled from
#    TMDB's credits endpoint. Output is the structured payload intended
#    for the prompt-assembly step ahead of the LLM core, not yet the LLM
#    call itself.
#    通过IMDb ID解析出对应的Wikidata词条，沿sitelinks拉取多语言Wikipedia
#    页面（REST API返回的Parsoid HTML），仅凭DOM结构提取剧情/演员表/评价
#    /信息栏（提取阶段不涉及LLM判断）。演职员从TMDB的credits接口获取。
#    输出的是喂给LLM核心之前一步提示词组装阶段的结构化数据，本脚本本身
#    不调用LLM。
#
# Features:
#    - Resolves Wikidata entity via IMDb ID (P345), avoiding ambiguous
#      title-based Wikipedia search entirely.
#    - Plot extracted in five languages by default: en/de/fr/es/zh, plus
#      the film's original language if different.
#    - Cast extracted from the original-language page (completeness) and
#      the Chinese page (naming/translation reference), with per-language
#      actor/role separators (" as " / "饰演" / " als " / " como " / etc.).
#    - Reception extracted from the original-language + English pages
#      only (deduped when identical), scoped to the evaluative subsection
#      alone (Critical response/Critics/等) when Reception is a bucket
#      heading with Box office/Accolades siblings, so box-office figures
#      and award tables never enter the payload.
#    - Infobox extracted from both the original-language and Chinese
#      pages, keyed by a per-language label-to-field map (director/
#      writer/based_on/producers/starring/composer/cinematographer/
#      editor/production_companies only; runtime/country/language/
#      budget/box office are deliberately excluded as unneeded).
#    - Section headings matched exactly first, falling back to keyword-
#      based fuzzy matching when a wiki page uses an unlisted heading
#      variant; unmatched languages are logged rather than guessed at.
#    - Wikipedia pages are fetched once per language and cached, shared
#      across the Plot/Cast/Reception/Infobox extraction passes.
#
# 功能:
#    - 通过IMDb ID (P345) 解析Wikidata词条，完全避免基于片名的模糊
#      Wikipedia搜索。
#    - 默认提取五种语言的剧情：en/de/fr/es/zh，若原始语言不在其中则一并
#      加入。
#    - 演员表提取原始语言版本（信息完整）与中文版本（译名/命名参考），
#      按语言使用不同的演员/角色分隔符（" as " / "饰演" / " als " /
#      " como " 等）。
#    - 评价章节仅抓取原始语言+英文（相同则去重），若Reception为票房/
#      评价/獎項共用的桶状标题，会进一步定位到"评价"子章节本身，避免
#      票房数字与获奖表格混入。
#    - 信息栏同时从原始语言页与中文页提取，按各语言的标签-字段映射表
#      筛选（仅保留导演/编剧/原著/监制/主演/配乐/摄影/剪辑/制片商，
#      片长/产地/语言/预算/票房等一律不提取）。
#    - 章节标题优先精确匹配，未命中时按关键词模糊匹配兜底；未覆盖的
#      语言会记录日志而非强行猜测。
#    - 同一语言的Wikipedia页面只抓取一次并缓存，Plot/Cast/Reception/
#      Infobox四个提取阶段共用。
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

TMDB_READ_ACCESS_TOKEN_ENV = "TMDB_READ_ACCESS_TOKEN"
TMDB_API_BASE = "https://api.themoviedb.org/3"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_REST_HTML = "https://{lang}.wikipedia.org/api/rest_v1/page/html/{title}"
IMDB_PROPERTY = "P345"
USER_AGENT = f"wiki_tmdb_fetch/{VERSION} (+{REPOSITORY}; GitHub Actions)"
REQUEST_TIMEOUT = 20

DEFAULT_LANGUAGE_PRIORITY = ("en", "zh", "fr", "de", "es")
CAST_LANGUAGES = ("zh",)

WIKIPEDIA_PAGE_URL = "https://{lang}.wikipedia.org/wiki/{title}"

LANGUAGE_DISPLAY_NAMES = {
    "en": "English",
    "zh": "中文",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ja": "日本語",
}


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
        "zh": ("劇情", "剧情", "劇情簡介", "剧情简介", "劇情大綱", "故事大綱", "故事簡介", "故事简介", "情節", "情节"),
        "fr": ("Synopsis",),
        "de": ("Handlung",),
        "es": ("Argumento", "Trama"),
        "ja": ("あらすじ", "ストーリー", "概要"),
        "ko": ("줄거리",),
        "it": ("Trama",),
        "pt": ("Enredo", "Sinopse"),
        "ru": ("Сюжет",),
        "tr": ("Konu", "Konusu"),
        "fa": ("خلاصه داستان", "داستان"),
        "ar": ("الحبكة", "القصة"),
        "th": ("เนื้อเรื่อง", "เรื่องย่อ"),
        "hi": ("कथानक", "कहानी"),
        "vi": ("Nội dung", "Cốt truyện"),
        "id": ("Alur cerita", "Sinopsis"),
        "he": ("עלילה",),
        "el": ("Υπόθεση",),
        "ro": ("Intrigă", "Subiect"),
        "pl": ("Fabuła",),
        "cs": ("Děj",),
        "hu": ("Cselekmény",),
        "uk": ("Сюжет",),
        "sv": ("Handling",),
        "no": ("Handling",),
        "da": ("Handling",),
        "fi": ("Juoni",),
        "nl": ("Verhaal", "Plot"),
        "ms": ("Plot", "Jalan cerita"),
        "bn": ("কাহিনী",),
        "ta": ("கதைச்சுருக்கம்",),
    },
    "cast": {
        "en": ("Cast",),
        "zh": ("演員", "演员", "演員表", "演员表", "配音"),
        "fr": ("Distribution",),
        "de": ("Besetzung",),
        "es": ("Reparto",),
        "ja": ("キャスト", "出演者"),
        "ko": ("출연진", "등장인물"),
        "it": ("Cast",),
        "pt": ("Elenco",),
        "ru": ("В ролях",),
        "tr": ("Oyuncular",),
        "fa": ("بازیگران",),
        "ar": ("طاقم التمثيل", "الممثلون"),
        "th": ("นักแสดง",),
        "hi": ("मुख्य कलाकार", "कलाकार"),
        "vi": ("Diễn viên",),
        "id": ("Pemeran",),
        "he": ("שחקנים",),
        "el": ("Πρωταγωνιστές", "Ηθοποιοί"),
        "ro": ("Distribuție",),
        "pl": ("Obsada",),
        "cs": ("Obsazení",),
        "hu": ("Szereplők",),
        "uk": ("У ролях",),
        "sv": ("Rollista", "Medverkande"),
        "no": ("Medvirkende", "Rollebesetning"),
        "da": ("Medvirkende",),
        "fi": ("Näyttelijät",),
        "nl": ("Rolverdeling",),
        "ms": ("Pelakon",),
        "bn": ("অভিনয়ে",),
        "ta": ("நடிகர்கள்",),
    },
    "reception": {
        "en": ("Reception",),
        "zh": ("反響", "反响", "迴響", "回响", "評價", "评价", "評論", "评论"),
        "fr": ("Accueil", "Réception"),
        "de": ("Rezeption",),
        "es": ("Recepción",),
        "ja": ("評価", "反響"),
        "ko": ("평가",),
        "it": ("Accoglienza",),
        "pt": ("Recepção",),
        "ru": ("Реакция", "Восприятие"),
        "tr": ("Alım", "Eleştiriler"),
        "fa": ("بازتاب", "استقبال"),
        "ar": ("الاستقبال",),
        "th": ("การตอบรับ",),
        "hi": ("स्वागत",),
        "vi": ("Đón nhận",),
        "id": ("Penerimaan", "Tanggapan"),
        "he": ("קבלה", "ביקורות"),
        "el": ("Υποδοχή",),
        "ro": ("Receptare", "Primire"),
        "pl": ("Odbiór",),
        "cs": ("Přijetí",),
        "hu": ("Fogadtatás",),
        "uk": ("Сприйняття",),
        "sv": ("Mottagande",),
        "no": ("Mottakelse",),
        "da": ("Modtagelse",),
        "fi": ("Vastaanotto",),
        "nl": ("Ontvangst",),
        "ms": ("Sambutan",),
        "bn": ("সমাদর", "প্রতিক্রিয়া"),
        "ta": ("வரவேற்பு",),
    },
}

FUZZY_KEYWORDS = {
    "plot": {
        "en": ("plot",),
        "zh": ("剧情", "劇情", "故事", "大綱", "大纲", "情節", "情节"),
        "fr": ("synopsis", "intrigue"),
        "de": ("handlung",),
        "es": ("argumento", "trama"),
        "ja": ("あらすじ", "ストーリー"),
        "ko": ("줄거리",),
        "it": ("trama",),
        "pt": ("enredo", "sinopse"),
        "ru": ("сюжет",),
        "tr": ("konu",),
        "fa": ("داستان",),
        "ar": ("حبكة", "قصة"),
        "th": ("เนื้อเรื่อง", "เรื่องย่อ"),
        "hi": ("कथानक", "कहानी"),
        "vi": ("nội dung", "cốt truyện"),
        "id": ("alur cerita", "sinopsis", "plot"),
        "he": ("עלילה",),
        "el": ("υπόθεση",),
        "ro": ("intrigă", "subiect"),
        "pl": ("fabuła",),
        "cs": ("děj",),
        "hu": ("cselekmény",),
        "uk": ("сюжет",),
        "sv": ("handling",),
        "no": ("handling",),
        "da": ("handling",),
        "fi": ("juoni",),
        "nl": ("verhaal", "plot"),
        "ms": ("plot", "jalan cerita"),
        "bn": ("কাহিনী",),
        "ta": ("கதைச்சுருக்கம்",),
    },
    "cast": {
        "en": ("cast",),
        "zh": ("演员", "演員", "配音", "出演"),
        "fr": ("distribution",),
        "de": ("besetzung",),
        "es": ("reparto",),
        "ja": ("キャスト", "出演"),
        "ko": ("출연", "등장인물"),
        "it": ("cast",),
        "pt": ("elenco",),
        "ru": ("в ролях",),
        "tr": ("oyuncular",),
        "fa": ("بازیگران",),
        "ar": ("تمثيل", "الممثلون"),
        "th": ("นักแสดง",),
        "hi": ("कलाकार",),
        "vi": ("diễn viên",),
        "id": ("pemeran",),
        "he": ("שחקנים",),
        "el": ("πρωταγωνιστές", "ηθοποιοί"),
        "ro": ("distribuție",),
        "pl": ("obsada",),
        "cs": ("obsazení",),
        "hu": ("szereplők",),
        "uk": ("у ролях",),
        "sv": ("rollista", "medverkande"),
        "no": ("medvirkende", "rollebesetning"),
        "da": ("medvirkende",),
        "fi": ("näyttelijät",),
        "nl": ("rolverdeling",),
        "ms": ("pelakon",),
        "bn": ("অভিনয়ে",),
        "ta": ("நடிகர்கள்",),
    },
    "reception": {
        "en": ("reception",),
        "zh": ("反响", "反響", "回响", "迴響", "评价", "評價", "评论", "評論"),
        "fr": ("accueil", "réception"),
        "de": ("rezeption",),
        "es": ("recepción",),
        "ja": ("評価", "反響"),
        "ko": ("평가",),
        "it": ("accoglienza",),
        "pt": ("recepção",),
        "ru": ("реакция", "восприятие"),
        "tr": ("alım", "eleştiri"),
        "fa": ("بازتاب", "استقبال"),
        "ar": ("استقبال",),
        "th": ("การตอบรับ",),
        "hi": ("स्वागत",),
        "vi": ("đón nhận",),
        "id": ("penerimaan", "tanggapan"),
        "he": ("קבלה", "ביקורות"),
        "el": ("υποδοχή",),
        "ro": ("receptare", "primire"),
        "pl": ("odbiór",),
        "cs": ("přijetí",),
        "hu": ("fogadtatás",),
        "uk": ("сприйняття",),
        "sv": ("mottagande",),
        "no": ("mottakelse",),
        "da": ("modtagelse",),
        "fi": ("vastaanotto",),
        "nl": ("ontvangst",),
        "ms": ("sambutan",),
        "bn": ("সমাদর", "প্রতিক্রিয়া"),
        "ta": ("வரவேற்பு",),
    },
}

CRITICAL_RESPONSE_KEYWORDS = {
    "en": ("critical response", "critical reception", "reviews", "critics"),
    "zh": ("影評", "影评", "劇評", "剧评", "評價", "评价", "評論", "评论", "批評", "批评"),
    "fr": ("critique",),
    "de": ("kritik",),
    "es": ("crítica",),
    "ja": ("評価", "批評"),
    "ko": ("평가", "비평"),
    "it": ("critica",),
    "pt": ("crítica",),
    "ru": ("критика", "отзывы"),
    "tr": ("eleştiri",),
    "fa": ("نقد",),
    "ar": ("نقد", "آراء النقاد"),
    "th": ("คำวิจารณ์",),
    "hi": ("समीक्षा",),
    "vi": ("đánh giá",),
    "id": ("ulasan",),
    "he": ("ביקורות",),
    "el": ("κριτικές",),
    "ro": ("recenzii",),
    "pl": ("recenzje",),
    "cs": ("kritika",),
    "hu": ("kritikák",),
    "uk": ("критика",),
    "sv": ("kritik",),
    "no": ("kritikk",),
    "da": ("anmeldelser",),
    "fi": ("arvostelut",),
    "nl": ("recensies", "kritiek"),
    "ms": ("ulasan",),
    "bn": ("সমালোচনা",),
    "ta": ("விமர்சனம்",),
}

INFOBOX_FIELD_MAP = {
    "en": {
        "Directed by": "director", "Written by": "writer", "Screenplay by": "writer",
        "Based on": "based_on", "Produced by": "producers", "Starring": "starring",
        "Music by": "composer", "Cinematography": "cinematographer", "Edited by": "editor",
        "Production companies": "production_companies", "Production company": "production_companies",
    },
    "zh": {
        "導演": "director", "导演": "director", "編劇": "writer", "编剧": "writer",
        "原著": "based_on", "監製": "producers", "监制": "producers", "製片": "producers", "制片": "producers",
        "主演": "starring", "配樂": "composer", "配乐": "composer", "音樂": "composer", "音乐": "composer",
        "攝影": "cinematographer", "摄影": "cinematographer", "剪輯": "editor", "剪辑": "editor",
        "製片商": "production_companies", "制片商": "production_companies",
        "出品公司": "production_companies", "製作公司": "production_companies", "制作公司": "production_companies",
    },
    "fr": {
        "Réalisation": "director", "Scénario": "writer", "Sociétés de production": "production_companies",
        "Musique": "composer", "Photographie": "cinematographer", "Montage": "editor",
        "Acteurs principaux": "starring", "Production": "producers", "D'après": "based_on",
    },
    "de": {
        "Regie": "director", "Drehbuch": "writer", "Produktion": "producers",
        "Musik": "composer", "Kamera": "cinematographer", "Schnitt": "editor",
        "Besetzung": "starring", "Produktionsunternehmen": "production_companies",
    },
    "es": {
        "Dirección": "director", "Guion": "writer", "Producción": "producers",
        "Música": "composer", "Fotografía": "cinematographer", "Montaje": "editor",
        "Protagonistas": "starring", "Compañías productoras": "production_companies",
        "Basada en": "based_on",
    },
    "ja": {
        "監督": "director", "脚本": "writer", "製作": "producers", "音楽": "composer",
        "撮影": "cinematographer", "編集": "editor", "出演者": "starring",
        "製作会社": "production_companies", "原作": "based_on",
    },
    "ko": {
        "감독": "director", "각본": "writer", "제작": "producers", "출연": "starring",
        "음악": "composer", "촬영": "cinematographer", "편집": "editor",
        "제작사": "production_companies", "원작": "based_on",
    },
    "it": {
        "Regia": "director", "Sceneggiatura": "writer", "Produttore": "producers",
        "Interpreti e personaggi": "starring", "Musiche": "composer",
        "Fotografia": "cinematographer", "Montaggio": "editor",
        "Casa di produzione": "production_companies", "Soggetto": "based_on",
    },
    "pt": {
        "Direção": "director", "Roteiro": "writer", "Produção": "producers",
        "Elenco": "starring", "Música": "composer", "Fotografia": "cinematographer",
        "Edição": "editor", "Companhia(s) produtora(s)": "production_companies",
        "Baseado em": "based_on",
    },
    "ru": {
        "Режиссёр": "director", "Автор сценария": "writer", "Продюсер": "producers",
        "В главных ролях": "starring", "Композитор": "composer", "Оператор": "cinematographer",
        "Монтаж": "editor", "Кинокомпания": "production_companies", "Основано на": "based_on",
    },
    "tr": {
        "Yönetmen": "director", "Senaryo": "writer", "Yapımcı": "producers",
        "Oyuncular": "starring", "Müzik": "composer", "Görüntü yönetmeni": "cinematographer",
        "Kurgu": "editor", "Yapım şirketi": "production_companies", "Dayandığı eser": "based_on",
    },
    "fa": {
        "کارگردان": "director", "نویسنده": "writer", "تهیه‌کننده": "producers",
        "بازیگران": "starring", "موسیقی": "composer", "فیلم‌برداری": "cinematographer",
        "تدوین": "editor", "شرکت تهیه‌کننده": "production_companies", "بر پایه": "based_on",
    },
    "ar": {
        "الإخراج": "director", "تأليف": "writer", "سيناريو": "writer", "إنتاج": "producers",
        "بطولة": "starring", "موسيقى": "composer", "تصوير": "cinematographer",
        "مونتاج": "editor", "شركة الإنتاج": "production_companies", "مأخوذ عن": "based_on",
    },
    "th": {
        "ผู้กำกับ": "director", "บทภาพยนตร์": "writer", "อำนวยการสร้าง": "producers",
        "นำแสดงโดย": "starring", "ดนตรี": "composer", "กำกับภาพ": "cinematographer",
        "ตัดต่อ": "editor", "บริษัทผู้สร้าง": "production_companies", "สร้างจาก": "based_on",
    },
    "hi": {
        "निर्देशक": "director", "लेखक": "writer", "निर्माता": "producers",
        "अभिनीत": "starring", "संगीतकार": "composer", "छायाकार": "cinematographer",
        "संपादक": "editor", "निर्माण कंपनी": "production_companies",
    },
    "vi": {
        "Đạo diễn": "director", "Kịch bản": "writer", "Sản xuất": "producers",
        "Diễn viên chính": "starring", "Âm nhạc": "composer", "Quay phim": "cinematographer",
        "Dựng phim": "editor", "Hãng sản xuất": "production_companies",
    },
    "id": {
        "Sutradara": "director", "Ditulis oleh": "writer", "Produser": "producers",
        "Pemeran": "starring", "Penata musik": "composer", "Sinematografer": "cinematographer",
        "Penyunting": "editor", "Perusahaan produksi": "production_companies",
    },
    "he": {
        "בימוי": "director", "תסריט": "writer", "הפקה": "producers",
        "שחקנים ראשיים": "starring", "מוזיקה": "composer", "צילום": "cinematographer",
        "עריכה": "editor", "חברת הפקה": "production_companies",
    },
    "el": {
        "Σκηνοθεσία": "director", "Σενάριο": "writer", "Παραγωγή": "producers",
        "Πρωταγωνιστούν": "starring", "Μουσική": "composer", "Φωτογραφία": "cinematographer",
        "Μοντάζ": "editor", "Εταιρεία παραγωγής": "production_companies",
    },
    "ro": {
        "Regizat de": "director", "Scenariul de": "writer", "Produs de": "producers",
        "Cu": "starring", "Muzica": "composer", "Imaginea": "cinematographer",
        "Montaj": "editor", "Companie de producție": "production_companies",
    },
    "pl": {
        "Reżyseria": "director", "Scenariusz": "writer", "Produkcja": "producers",
        "Obsada": "starring", "Muzyka": "composer", "Zdjęcia": "cinematographer",
        "Montaż": "editor", "Wytwórnia": "production_companies",
    },
    "cs": {
        "Režie": "director", "Scénář": "writer", "Produkce": "producers",
        "Hrají": "starring", "Hudba": "composer", "Kamera": "cinematographer",
        "Střih": "editor", "Výroba": "production_companies",
    },
    "hu": {
        "Rendező": "director", "Írta": "writer", "Producer": "producers",
        "Főszereplő(k)": "starring", "Zene": "composer", "Operatőr": "cinematographer",
        "Vágó": "editor", "Gyártó": "production_companies",
    },
    "uk": {
        "Режисер": "director", "Сценарист": "writer", "Продюсер": "producers",
        "У головних ролях": "starring", "Композитор": "composer", "Оператор": "cinematographer",
        "Монтаж": "editor", "Кінокомпанія": "production_companies",
    },
    "sv": {
        "Regi": "director", "Manus": "writer", "Produktion": "producers",
        "I rollerna": "starring", "Musik": "composer", "Foto": "cinematographer",
        "Klippning": "editor", "Produktionsbolag": "production_companies",
    },
    "no": {
        "Regi": "director", "Manus": "writer", "Produksjon": "producers",
        "Skuespillere": "starring", "Musikk": "composer", "Foto": "cinematographer",
        "Klipping": "editor", "Produksjonsselskap": "production_companies",
    },
    "da": {
        "Instruktion": "director", "Manuskript": "writer", "Produktion": "producers",
        "Medvirkende": "starring", "Musik": "composer", "Foto": "cinematographer",
        "Klipning": "editor", "Produktionsselskab": "production_companies",
    },
    "fi": {
        "Ohjaus": "director", "Käsikirjoitus": "writer", "Tuotanto": "producers",
        "Pääosissa": "starring", "Musiikki": "composer", "Kuvaus": "cinematographer",
        "Leikkaus": "editor", "Tuotantoyhtiö": "production_companies",
    },
    "nl": {
        "Regie": "director", "Scenario": "writer", "Producent": "producers",
        "Hoofdrollen": "starring", "Muziek": "composer", "Camera": "cinematographer",
        "Montage": "editor", "Productiemaatschappij": "production_companies",
    },
    "ms": {
        "Pengarah": "director", "Penulis": "writer", "Penerbit": "producers",
        "Pelakon": "starring", "Muzik": "composer", "Sinematografi": "cinematographer",
        "Penyuntingan": "editor", "Syarikat produksi": "production_companies",
    },
    "bn": {
        "পরিচালক": "director", "চিত্রনাট্য": "writer", "প্রযোজক": "producers",
        "অভিনয়ে": "starring", "সুরকার": "composer", "চিত্রগ্রাহক": "cinematographer",
        "সম্পাদক": "editor", "প্রযোজনা কোম্পানি": "production_companies",
    },
    "ta": {
        "இயக்கம்": "director", "எழுத்து": "writer", "தயாரிப்பு": "producers",
        "நடிப்பு": "starring", "இசை": "composer", "ஒளிப்பதிவு": "cinematographer",
        "படத்தொகுப்பு": "editor", "தயாரிப்பு நிறுவனம்": "production_companies",
    },
}

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


SCRIPT_NAME = "wiki_tmdb_fetch"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


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
    log(f"fetch (wikipedia): {lang}.wikipedia.org/{title}")
    body_text, error = http_get(url)
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


def extract_infobox(soup, lang):
    field_map = INFOBOX_FIELD_MAP.get(lang)
    table = soup.select_one("table.infobox")
    if not field_map or not table:
        return None
    infobox = {}
    for row in table.find_all("tr"):
        header, cell = row.find("th"), row.find("td")
        if not header or not cell:
            continue
        label = clean_text(header.get_text(" ", strip=True))
        key = field_map.get(label)
        if not key:
            continue
        values = [clean_text(li.get_text(" ", strip=True)) for li in cell.find_all("li")]
        infobox[key] = values if values else clean_text(cell.get_text(" ", strip=True))
    return infobox or None


def find_nested_subsection(container, keywords):
    for heading in container.find_all(("h3", "h4")):
        title_lower = clean_text(heading.get_text()).lower()
        if any(keyword.lower() in title_lower for keyword in keywords):
            return heading.find_parent("section") or heading.parent
    return None


def extract_reception(soup, lang):
    container = find_section(soup, "reception", lang)
    if container is None:
        return None
    text = extract_paragraphs(container)
    if text:
        return text
    subsection = find_nested_subsection(container, CRITICAL_RESPONSE_KEYWORDS.get(lang, ()))
    if subsection is None:
        return None
    return extract_paragraphs(subsection) or None


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
            links.append({
                "lang": lang,
                "label": LANGUAGE_DISPLAY_NAMES.get(lang, lang),
                "url": WIKIPEDIA_PAGE_URL.format(lang=lang, title=urllib.parse.quote(sitelinks[lang], safe="")),
            })
    return links


def empty_result(reason, **extra):
    result = {
        "success": False, "reason": reason, "detail": None,
        "wikidata_qid": None, "lead": None, "infobox": {},
        "plot": {}, "cast": {}, "reception": {}, "tmdb_credits": None, "tmdb_detail": None,
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
    reception_languages = list(dict.fromkeys([original_language, "en"]))
    infobox_languages = list(dict.fromkeys([original_language, "zh"]))
    all_languages = list(dict.fromkeys([*plot_languages, *cast_languages, *reception_languages, *infobox_languages]))

    page_cache = {}

    def get_page(lang):
        if lang not in page_cache:
            page_cache[lang] = fetch_wiki_page(lang, sitelinks[lang]) if lang in sitelinks else None
        return page_cache[lang]

    for lang in all_languages:
        if lang not in sitelinks:
            log(f"skip ({lang}): no sitelink")

    lead = None
    plot = {}
    for lang in plot_languages:
        soup = get_page(lang)
        if soup is None:
            continue
        if lang in ("en", original_language) and lead is None:
            lead = {"lang": lang, "text": extract_lead(soup)}
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

    reception = {}
    for lang in reception_languages:
        soup = get_page(lang)
        if soup is None:
            continue
        text = extract_reception(soup, lang)
        if text:
            reception[lang] = text
        else:
            log(f"reception section not found ({lang}), check SECTION_ALIASES/FUZZY_KEYWORDS/CRITICAL_RESPONSE_KEYWORDS")

    infobox = {}
    for lang in infobox_languages:
        soup = get_page(lang)
        if soup is None:
            continue
        fields = extract_infobox(soup, lang)
        if fields:
            infobox[lang] = fields
        else:
            log(f"infobox not found or unmapped ({lang}), check INFOBOX_FIELD_MAP")

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

    log(f"summary: plot={list(plot.keys())} cast={list(cast.keys())} reception={list(reception.keys())} infobox={list(infobox.keys())}")
    log("status: success")
    return {
        "success": True, "reason": None, "detail": None,
        "wikidata_qid": entity["qid"],
        "lead": lead, "infobox": infobox,
        "plot": plot, "cast": cast, "reception": reception,
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
