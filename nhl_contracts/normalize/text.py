"""Text helpers: markdown/HTML stripping, money and season parsing, name keys."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

FORGE_ENTITY = re.compile(r"<forge-entity[^>]*>(.*?)</forge-entity>", re.S)
HTML_TAG = re.compile(r"<[^>]+>")
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_EMPH = re.compile(r"\*{1,3}|_{1,3}(?=\w)|(?<=\w)_{1,3}")
WS = re.compile(r"[ \t ]+")

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
}

MONEY = re.compile(
    r"\$\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>million|mil\b|M\b|K\b|thousand|billion)?",
    re.I,
)
SEASON = re.compile(r"\b(20\d\d)-(\d\d)\b")


def clean(text: str) -> str:
    """Strip forge-entity, HTML, markdown links/emphasis; collapse spaces per line."""
    t = FORGE_ENTITY.sub(r"\1", text or "")
    t = HTML_TAG.sub("", t)
    t = MD_LINK.sub(r"\1", t)
    t = MD_EMPH.sub("", t)
    t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", t)  # markdown escapes: eight\-year
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-")
    paragraphs = re.split(r"\n\s*\n", t)
    out: list[str] = []
    for para in paragraphs:
        para = WS.sub(" ", para.replace("\n", " ")).strip()
        if not para:
            continue
        # a CMS part boundary can fall mid-sentence: glue it to the previous paragraph
        if out and not re.search(r"[.!?:\"')\]]$", out[-1]) and para[:1].islower():
            out[-1] = f"{out[-1]} {para}"
        else:
            out.append(para)
    return "\n".join(out)


def money_value(num: str, unit: str | None) -> int | None:
    digits = num.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    u = (unit or "").lower()
    if u in ("million", "mil", "m"):
        value *= 1_000_000
    elif u in ("k", "thousand"):
        value *= 1_000
    elif u == "billion":
        value *= 1_000_000_000
    if value < 1000:  # "$5 a ticket" style
        return None
    return int(round(value))


def term_years(text: str) -> int | None:
    m = re.search(r"\b(one|two|three|four|five|six|seven|eight|[1-8])[\s-]+year\b", text, re.I)
    if m:
        return NUMBER_WORDS[m.group(1).lower()]
    return None


def season_from_date(d: date) -> str:
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def shift_season(season: str, n: int) -> str:
    y = int(season[:4]) + n
    return f"{y}-{str(y + 1)[2:]}"


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def name_key(name: str) -> str:
    s = strip_accents(name).lower()
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def split_sentences(text: str) -> list[str]:
    """Rough sentence split that keeps '$1.5 million.' and 'St. Louis' intact."""
    text = re.sub(r"\b(St|Jr|Sr|Mr|Ms|Dr|vs|Nov|Dec|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct)\.",
                  r"\1<DOT>", text)
    text = re.sub(r"\b([A-Z])\.(?=[A-Z]\.)", r"\1<DOT>", text)  # J.R.
    text = re.sub(r"\b([A-Z])\.(?=\s[A-Z][a-z])", r"\1<DOT>", text)  # J.R. Avon
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\n+", text)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]
