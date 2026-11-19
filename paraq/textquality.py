"""Decide whether the text layer of a PDF page is usable or damaged.

A "damaged" layer is one a reader cannot rely on: mojibake from a wrong encoding,
private-use or replacement characters from broken font maps, Latin/Cyrillic
look-alike soup from a wrong-language OCR pass, letters spaced out one by one,
or consonant noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VOWELS = set("аеёиоуыэюяәөұүіѣєїaeiouyıöüâîûäéèêàáíóú")
_token_re = re.compile(r"\S+")
_strip_chars = ".,;:!?()[]{}«»\"'“”„‘’—–-…/\\*№%§<>|=+°·•"


@dataclass
class Quality:
    status: str  # "none" | "good" | "damaged"
    reason: str = ""
    chars: int = 0


def _is_cyr(c: str) -> bool:
    return "Ѐ" <= c <= "ԯ"


def _is_lat(c: str) -> bool:
    return c.isalpha() and c < "ɐ"


def assess(text: str, min_chars: int = 25) -> Quality:
    compact = "".join(text.split())
    n = len(compact)
    if n < min_chars:
        return Quality("none", "no text layer", n)

    bad = sum(
        1
        for c in compact
        if c == "�" or "" <= c <= "" or c < " " or "\x7f" <= c <= "\x9f"
    )
    if bad / n > 0.02:
        return Quality("damaged", "unreadable characters (broken font encoding)", n)

    letters = [c for c in compact if c.isalpha()]
    if len(letters) < 0.45 * n:
        return Quality("damaged", "mostly symbols instead of letters", n)

    known = sum(1 for c in letters if _is_cyr(c) or _is_lat(c))
    if known / len(letters) < 0.85:
        return Quality("damaged", "characters from unexpected scripts", n)

    hi_latin1 = sum(1 for c in letters if "À" <= c <= "ÿ")
    if hi_latin1 / len(letters) > 0.30:
        return Quality("damaged", "mojibake (wrong text encoding)", n)

    tokens = [t.strip(_strip_chars) for t in _token_re.findall(text)]
    tokens = [t for t in tokens if t]
    alpha_tokens = [t for t in tokens if any(c.isalpha() for c in t)]
    if not alpha_tokens:
        return Quality("damaged", "no words", n)

    mixed = sum(1 for t in alpha_tokens if any(_is_cyr(c) for c in t) and any(_is_lat(c) for c in t))
    if mixed / len(alpha_tokens) > 0.12:
        return Quality("damaged", "Latin and Cyrillic letters mixed inside words", n)

    single = sum(1 for t in alpha_tokens if len(t) == 1)
    if len(alpha_tokens) >= 20 and single / len(alpha_tokens) > 0.45:
        return Quality("damaged", "letters split apart instead of words", n)

    long_words = [t.lower() for t in alpha_tokens if len(t) >= 4 and t.isalpha()]
    if len(long_words) >= 10:
        no_vowel = sum(1 for t in long_words if not (set(t) & VOWELS))
        if no_vowel / len(long_words) > 0.25:
            return Quality("damaged", "words without vowels (garbled recognition)", n)
        # Real prose rarely repeats one letter three times in a row.
        stutter = sum(1 for t in long_words if re.search(r"(.)\1\1", t))
        if stutter / len(long_words) > 0.2:
            return Quality("damaged", "repeated-letter noise", n)

    avg_len = sum(len(t) for t in alpha_tokens) / len(alpha_tokens)
    if avg_len > 18:
        return Quality("damaged", "words run together", n)

    return Quality("good", "", n)
