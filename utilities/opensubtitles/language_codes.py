#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: language_codes.py
# Organization: MontageSubs (蒙太奇字幕社区)
# License: MIT License
#
# Description / 描述:
#   BCP47/ISO639-1 主语言子标签（如 en、zh、en-US 中的 en）到 OpenSubtitles
#   SubLanguageID（ISO639-2/B 三字母码，如 eng）的映射表。仅收录实际会在
#   字幕搜索中用到的语言；未收录语言返回 None，由调用方记录跳过。
# ============================================================================
ISO639_1_TO_OPENSUBTITLES = {
    "sq": "alb", "ar": "ara", "hy": "arm", "az": "aze", "eu": "baq",
    "be": "bel", "bn": "ben", "bs": "bos", "bg": "bul", "my": "bur",
    "ca": "cat", "zh": "chi", "hr": "hrv", "cs": "cze", "da": "dan",
    "nl": "dut", "en": "eng", "eo": "epo", "et": "est", "fi": "fin",
    "fr": "fre", "gl": "glg", "ka": "geo", "de": "ger", "el": "gre",
    "gu": "guj", "he": "heb", "hi": "hin", "hu": "hun", "is": "ice",
    "id": "ind", "ga": "gle", "it": "ita", "ja": "jpn", "kn": "kan",
    "kk": "kaz", "km": "khm", "ko": "kor", "ku": "kur", "ky": "kir",
    "lo": "lao", "la": "lat", "lv": "lav", "lt": "lit", "lb": "ltz",
    "mk": "mac", "ms": "may", "ml": "mal", "mt": "mlt", "mr": "mar",
    "mn": "mon", "ne": "nep", "no": "nor", "or": "ori", "fa": "per",
    "pl": "pol", "pt": "por", "pa": "pan", "ro": "rum", "ru": "rus",
    "sr": "srp", "si": "sin", "sk": "slo", "sl": "slv", "so": "som",
    "es": "spa", "sw": "swa", "sv": "swe", "tl": "tgl", "tg": "tgk",
    "ta": "tam", "tt": "tat", "te": "tel", "th": "tha", "bo": "tib",
    "tr": "tur", "tk": "tuk", "uk": "ukr", "ur": "urd", "uz": "uzb",
    "vi": "vie", "cy": "wel", "af": "afr", "am": "amh", "as": "asm",
    "br": "bre", "fy": "fry", "gd": "gla", "ha": "hau", "ig": "ibo",
    "ps": "pus", "sa": "san", "xh": "xho", "yo": "yor", "zu": "zul",
}


def primary_subtag(bcp47_tag):
    return (bcp47_tag or "").split("-")[0].lower()


def to_opensubtitles_code(bcp47_tag):
    return ISO639_1_TO_OPENSUBTITLES.get(primary_subtag(bcp47_tag))
