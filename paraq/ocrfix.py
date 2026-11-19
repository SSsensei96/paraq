"""Clean-up of OCR output for Russian text, with a dictionary.

Old Soviet print is read with a handful of typical letter confusions: и read as н
("могнла"), л/д ("слелало"), ш/щ ("пастбиш"), о/э ("эграниченными")... A word the
dictionary does not know is checked against those confusions; if exactly one
variant is a real word, that variant is used. Known words, names (capitalised
words) and anything that is not plain lowercase Russian are never touched.

Roman numerals read with Cyrillic look-alikes ("ХІХ в.") are written in Latin
letters ("XIX в.").

The dictionary is pymorphy3's Russian dictionary (about 16 MB, offline). Without
it this module does nothing.
"""

from __future__ import annotations

import logging
import re
import threading

log = logging.getLogger("paraq.ocrfix")

# (read as, probably was): the confusions seen in scans of mid-century Russian print
CONFUSIONS = [
    ("н", "и"), ("и", "н"), ("н", "п"), ("п", "н"), ("л", "д"), ("д", "л"), ("ш", "щ"), ("щ", "ш"),
    ("э", "о"), ("о", "э"), ("с", "е"), ("е", "с"), ("т", "г"), ("г", "т"), ("ы", "ь"), ("ь", "ы"),
    ("ц", "щ"), ("й", "и"), ("и", "й"), ("в", "в"),
]
CONFUSIONS = [(a, b) for a, b in CONFUSIONS if a != b]
_RU_WORD = re.compile(r"[а-яё]+")
_ROMAN = re.compile(r"^([ХXІIVУ]+)([.,;:)\]]*)$")
_CENTURY_WORDS = {"в.", "вв.", "в", "век", "века", "веке", "веков", "векa", "столетия", "столетие", "съезд", "съезда"}

_lock = threading.Lock()
_morph = None
_failed = False


def _analyzer():
    global _morph, _failed
    with _lock:
        if _morph is None and not _failed:
            try:
                import pymorphy3
                import pymorphy3_dicts_ru

                # An explicit path works in a packaged app, where package metadata may be missing.
                _morph = pymorphy3.MorphAnalyzer(path=pymorphy3_dicts_ru.get_path())
            except Exception as e:  # noqa: BLE001
                _failed = True
                log.warning("Russian dictionary not available, OCR clean-up off: %s", e)
        return _morph


def available() -> bool:
    return _analyzer() is not None


def known(word: str) -> bool:
    m = _analyzer()
    return bool(m and m.word_is_known(word))


def _swap_in(word: str) -> set[str]:
    return {word[:i] + ("н" if ch == "и" else "и") + word[i + 1 :] for i, ch in enumerate(word) if ch in "ин"}


def _tiers(word: str) -> list[set[str]]:
    """Candidate spellings, most likely first: one и/н swap; any one swap; two swaps."""
    one_in = _swap_in(word)
    one_any = {word[:i] + b + word[i + 1 :] for i, ch in enumerate(word) for a, b in CONFUSIONS if ch == a}
    two = {v2 for v in one_any for v2 in _swap_in(v)}
    tiers = [one_in, one_any - one_in, two - one_any - one_in]
    for t in tiers:
        t.discard(word)
    return tiers


def _variants(word: str) -> set[str]:
    return set().union(*_tiers(word))


_cache: dict[str, str] = {}


def correct_word(word: str, max_tier: int = 3) -> str:
    """The corrected spelling of one lowercase Russian word, or the word itself.

    Candidates are tried from the most likely confusion down; the first level that has
    exactly one dictionary word decides. Two or more at the same level: left alone.
    """
    if len(word) < 4 or not _RU_WORD.fullmatch(word):
        return word
    key = (word, max_tier)
    if key in _cache:
        return _cache[key]
    m = _analyzer()
    result = word
    if m is not None and not m.word_is_known(word):
        for tier in _tiers(word)[:max_tier]:
            hits = [v for v in tier if m.word_is_known(v)]
            if len(hits) == 1:
                result = hits[0]
                break
            if len(hits) > 1:
                break
    _cache[key] = result
    return result


def _is_name(word: str) -> bool:
    m = _analyzer()
    return bool(m) and any({"Geox", "Name", "Surn", "Patr", "Orgn"} & set(str(p.tag).replace(" ", ",").split(",")) for p in m.parse(word))


def _fix_token(token: str, prev: str = "", protected: frozenset = frozenset()) -> str:
    """Correct the word inside a token such as "(могнла," and keep the punctuation.

    Capitalised words only get the safest fix (one и/н swap), and in the middle of a
    sentence only when the result is a name or a place ("Россин" -> "России") - a
    capitalised unknown word there is more likely a name the dictionary lacks.
    Words in `protected` (unknown but spelled the same way many times) are left alone.
    """
    m = re.fullmatch(r"([^\w]*)([А-ЯЁ]?[а-яё]+)([^\w]*)", token)
    if not m:
        return token
    word = m.group(2)
    # A word spelled the same way many times is probably a real term ("андроновских"), so only
    # the systematic и/н misreading may still be corrected there ("Россин" -> "России").
    tiers = 1 if word.lower() in protected else 3
    if word[0].isupper():
        fixed = correct_word(word.lower(), max_tier=1)
        if fixed == word.lower():
            return token
        sentence_start = not prev or prev.rstrip("»”\")]")[-1:] in ".!?…"
        if not sentence_start and not _is_name(fixed):
            return token
        fixed = fixed[0].upper() + fixed[1:]
    else:
        fixed = correct_word(word, max_tier=tiers)
    return token if fixed == word else m.group(1) + fixed + m.group(3)


def unknown_words(lines: list[list]) -> list[str]:
    """Lowercase forms of the plain Russian words the dictionary does not know (for counting)."""
    m = _analyzer()
    if m is None:
        return []
    out = []
    for line in lines:
        for w in line:
            mm = re.fullmatch(r"[^\w]*([А-ЯЁ]?[а-яё]{3,})[^\w]*", w.text)
            if mm and not m.word_is_known(mm.group(1).lower()):
                out.append(mm.group(1).lower())
    return out


def roman(token: str, next_token: str = "") -> str:
    """"ХІХ" -> "XIX" when the token can only be a Roman numeral."""
    m = _ROMAN.fullmatch(token)
    if not m:
        return token
    core = m.group(1)
    latin = core.translate(str.maketrans({"Х": "X", "І": "I", "У": "V"}))
    if latin == core:
        return token  # already Latin
    if len(core) >= 2 or next_token.lower() in _CENTURY_WORDS:
        if re.fullmatch(r"[XIV]+", latin):
            return latin + m.group(2)
    return token


def fix_lines(lines: list[list], protected: frozenset = frozenset()) -> int:
    """Correct OCR words in place. `lines` is a list of lines, each a list of objects with
    a `.text` attribute, in reading order. Returns the number of words changed.

    A word broken at the end of a line is only ever judged whole ("оказа-" + "лись"),
    never half by half: its halves are not words, and "correcting" them makes nonsense.
    """
    if not available():
        return 0
    changed = 0
    flat = [w for line in lines for w in line]
    for k, w in enumerate(flat):
        nxt = flat[k + 1].text if k + 1 < len(flat) else ""
        new = roman(w.text, nxt)
        if new != w.text:
            w.text = new
            changed += 1
    fragments: set[int] = set()  # second halves of broken words
    for li, line in enumerate(lines):
        for wi, w in enumerate(line):
            if id(w) in fragments:
                continue
            last_on_line = wi == len(line) - 1
            if re.search(r"[-¬]$", w.text):
                # first half of a broken word (or a hyphenated compound): judge it with its other half
                if last_on_line and li + 1 < len(lines) and lines[li + 1]:
                    nxt = lines[li + 1][0]
                    fragments.add(id(nxt))
                    mh = re.fullmatch(r"([^\w]*)([а-яё]+)[-¬]", w.text)
                    mt = re.fullmatch(r"([а-яё]+)([^\w]*)", nxt.text)
                    if mh and mt:
                        head, tail = mh.group(2), mt.group(1)
                        whole = correct_word(head + tail, max_tier=1 if (head + tail) in protected else 3)
                        if whole != head + tail and len(whole) == len(head) + len(tail):
                            w.text = mh.group(1) + whole[: len(head)] + w.text[-1]
                            nxt.text = whole[len(head) :] + mt.group(2)
                            changed += 1
                continue
            prev = line[wi - 1].text if wi else (lines[li - 1][-1].text if li and lines[li - 1] else "")
            new = _fix_token(w.text, prev, protected)
            if new != w.text:
                w.text = new
                changed += 1
    return changed


def keep_hyphen(left: str, right: str) -> bool:
    """For a word split as "left-" / "right" over two lines: is the hyphen part of the word?

    Yes for compounds ("социально-" + "экономический", "ага-" + "султаны"): the joined word
    is unknown but both halves are words. No for ordinary word breaks ("продви-" + "жение").
    """
    m = _analyzer()
    if m is None:
        return False
    l, r = re.sub(r"^\W+", "", left).lower(), re.sub(r"\W+$", "", right).lower()
    if not l or not r or not _RU_WORD.fullmatch(l) or not _RU_WORD.fullmatch(r):
        return False
    if m.word_is_known(l + r):
        return False
    if m.word_is_known(l + "-" + r):
        return True
    return len(l) >= 3 and m.word_is_known(l) and m.word_is_known(r)


def install_hyphen_judge() -> bool:
    """Let the layout keep hyphens in compound words (needs the dictionary)."""
    if not available():
        return False
    from . import layout

    layout.keep_hyphen = keep_hyphen
    return True
