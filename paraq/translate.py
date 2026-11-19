"""Offline translation into English, with two local engines.

* NLLB-200 through CTranslate2: fast, sentence by sentence. The model is
  downloaded once into the app's data folder.
* TranslateGemma (Google, 2026) through the Ollama app: a translation-tuned
  language model that reads a whole paragraph, so it keeps context, terms and
  names consistent. Slower; Ollama uses the Mac's or the NVIDIA card's GPU.

Either way the text is split into sentences first and every English sentence
keeps the character range of the sentence it came from, so the app can link
the two. Each paragraph's language is detected; English passes through unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

from .config import data_dir

# Progress bars from the downloader write to stderr, which does not exist in a
# windowed app (packaged Mac app, pythonw on Windows) and made downloads crash.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# Plain HTTP download: the file grows inside the model folder, so progress can be
# shown and an interrupted download resumes. (The newer "Xet" method assembles the
# file elsewhere and the counter seemed stuck at 6 MB until the very end.)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

log = logging.getLogger("paraq.translate")

TARGET = "eng_Latn"

MODELS = {
    "nllb-600M": {
        "engine": "nllb",
        "label": "NLLB 600M - fastest, 0.6 GB",
        "repo": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "bytes": 630_000_000,
    },
    "nllb-1.3B": {
        "engine": "nllb",
        "label": "NLLB 1.3B - fast, 1.4 GB",
        "repo": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
        "bytes": 1_390_000_000,
    },
    "nllb-3.3B": {
        "engine": "nllb",
        "label": "NLLB 3.3B - 3.4 GB, slower",
        "repo": "OpenNMT/nllb-200-3.3B-ct2-int8",
        "bytes": 3_370_000_000,
    },
    "gemma-4b": {
        "engine": "ollama",
        "label": "TranslateGemma 4B - smarter, 3.3 GB, needs Ollama",
        "tag": "translategemma:4b",
        "bytes": 3_300_000_000,
    },
    "gemma-12b": {
        "engine": "ollama",
        "label": "TranslateGemma 12B - smartest, 8.1 GB, needs Ollama",
        "tag": "translategemma:12b",
        "bytes": 8_100_000_000,
    },
}


def engine_of(key: str) -> str:
    return MODELS.get(key, {}).get("engine", "nllb")


def total_memory_gb() -> float:
    try:
        if sys.platform == "darwin":
            import subprocess

            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout) / 1e9
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.total / 1e9
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except Exception:  # noqa: BLE001
        return 0.0


def recommended_model() -> str:
    """12B only where it fits comfortably: an Apple Silicon Mac with 24 GB or more.

    On a 16 GB Mac the 12B model (8 GB plus working memory) runs at the edge of what
    macOS lets the GPU use and Ollama can fail under memory pressure; on a laptop
    with a 4 GB NVIDIA card it mostly runs on the processor. 4B is the safe choice.
    """
    import platform

    if sys.platform == "darwin" and platform.machine() == "arm64" and total_memory_gb() >= 23:
        return "gemma-12b"
    return "gemma-4b"
SPM_REPO = "facebook/nllb-200-distilled-600M"
SPM_FILE = "sentencepiece.bpe.model"

# NLLB code -> name, for the "translate from" menu.
SOURCE_LANGS = {
    "auto": "Detect automatically",
    "rus_Cyrl": "Russian",
    "kaz_Cyrl": "Kazakh",
    "tur_Latn": "Turkish",
    "kir_Cyrl": "Kyrgyz",
    "uzn_Latn": "Uzbek (Latin)",
    "tat_Cyrl": "Tatar",
    "ukr_Cyrl": "Ukrainian",
    "deu_Latn": "German",
    "fra_Latn": "French",
}


class TranslationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# language detection and sentence splitting
# ---------------------------------------------------------------------------

_KAZ = set("әғқңөұүһӘҒҚҢӨҰҮҺ")
_TUR = set("ğışİĞŞ")
_TUR_SOFT = set("çöüÇÖÜ")
_TUR_WORDS = {"ve", "bir", "bu", "için", "ile", "de", "da", "olarak", "olan", "gibi", "daha", "çok", "ama", "ne", "en"}
_ENG_WORDS = {"the", "and", "of", "to", "in", "is", "that", "was", "for", "with", "as", "by", "on", "were", "which"}


def detect_language(text: str) -> str | None:
    """Return the NLLB code of the paragraph's language, or None if it is English / not text."""
    cyr = sum(1 for c in text if "Ѐ" <= c <= "ԯ")
    lat = sum(1 for c in text if c.isalpha() and c < "ɐ")
    if cyr + lat < 2:
        return None
    if cyr >= lat:
        kaz = sum(1 for c in text if c in _KAZ)
        return "kaz_Cyrl" if kaz / cyr > 0.015 else "rus_Cyrl"
    hard = sum(1 for c in text if c in _TUR)
    soft = sum(1 for c in text if c in _TUR_SOFT)
    words = re.findall(r"[^\W\d_]+", text.lower())
    tur_hits = sum(1 for w in words if w in _TUR_WORDS)
    eng_hits = sum(1 for w in words if w in _ENG_WORDS)
    if hard / lat > 0.01 or (soft / lat > 0.02 and tur_hits >= eng_hits) or (tur_hits > eng_hits and tur_hits >= 2):
        return "tur_Latn"
    return None


_OLD_RU = str.maketrans({"ѣ": "е", "Ѣ": "Е", "і": "и", "І": "И", "ѳ": "ф", "Ѳ": "Ф", "ѵ": "и", "Ѵ": "И"})


def modernize_russian(text: str) -> str:
    """Map pre-1918 Russian spelling to the modern one so the model recognises the words."""
    if not any(c in text for c in "ѣѢѳѲѵѴ") and "ъ " not in text:
        return text
    text = text.translate(_OLD_RU)
    return re.sub(r"(?<=[бвгджзклмнпрстфхцчшщ])ъ(?=[^\w]|$)", "", text)


_ABBR = {
    # Russian / Kazakh
    "г", "гг", "т", "тт", "д", "с", "стр", "см", "ср", "др", "пр", "проч", "им", "ул", "в", "вв", "н", "э", "руб",
    "коп", "тыс", "млн", "млрд", "проф", "акад", "доц", "канд", "ред", "изд", "л", "лл", "оп", "ф", "ч", "п",
    "напр", "обл", "губ", "у", "уезд", "вол", "ж", "б", "қ", "ғ", "т.б", "т.е", "т.д", "т.п", "т.к", "и.о",
    # English / Turkish
    "mr", "mrs", "dr", "prof", "vs", "etc", "e.g", "i.e", "p", "pp", "vol", "no", "ibid", "cf", "st", "fig",
    "ed", "eds", "bkz", "vb", "s", "yy",
}
_split_re = re.compile(r"(?<=[.!?…])([»”\")\]]*)(\s+)(?=[«“\"(\[]?[A-ZА-ЯЁӘҒҚҢӨҰҮҺІÇĞİÖŞÜ0-9])")


def split_sentence_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges of the sentences of a paragraph (abbreviations do not end a sentence)."""
    end_of_text = len(text.rstrip())
    start = len(text) - len(text.lstrip())
    spans: list[tuple[int, int]] = []
    for m in _split_re.finditer(text):
        end = m.end(1)
        piece = text[start:end]
        words = piece.rstrip("»”\")]").split()
        last = words[-1] if words else ""
        token = last.rstrip(".").lower()
        if last.endswith(".") and (token in _ABBR or (len(token) == 1 and token.isalpha())):
            continue  # "г.", "т. 2", "А. С. Иванов": not the end of a sentence
        if text[start:end].strip():
            spans.append((start, end))
        start = m.end()
    if text[start:end_of_text].strip():
        spans.append((start, end_of_text))
    return spans


def split_sentences(text: str) -> list[str]:
    return [text[a:b] for a, b in split_sentence_spans(text)]


def align_groups(src_lens: list[int], en_lens: list[int]) -> list[tuple[int, int, int, int]]:
    """Pair source sentences with English sentences by length (Gale-Church style).

    Returns (i0, i1, j0, j1): source sentences i0..i1-1 correspond to English sentences
    j0..j1-1. Groups are 1-1, or 2-1 / 1-2 etc. where the translator merged or split
    sentences; every sentence on both sides belongs to exactly one group.
    """
    n, m = len(src_lens), len(en_lens)
    if n == m:
        return [(i, i + 1, i, i + 1) for i in range(n)]
    if n == 0 or m == 0:
        return [(0, n, 0, m)] if n or m else []
    ratio = (sum(en_lens) or 1) / (sum(src_lens) or 1)
    moves = {(1, 1): 0.0, (2, 1): 0.8, (1, 2): 0.8, (3, 1): 1.6, (1, 3): 1.6, (2, 2): 1.6, (1, 0): 3.0, (0, 1): 3.0}
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if cost[i][j] == inf:
                continue
            for (di, dj), penalty in moves.items():
                a, b = i + di, j + dj
                if a > n or b > m:
                    continue
                s_len = sum(src_lens[i:a]) * ratio
                e_len = sum(en_lens[j:b])
                c = cost[i][j] + abs(e_len - s_len) / ((s_len + 10) ** 0.5) + penalty
                if c < cost[a][b]:
                    cost[a][b] = c
                    back[a][b] = (di, dj)
    groups = []
    i, j = n, m
    while i or j:
        di, dj = back[i][j]  # type: ignore[misc]
        groups.append((i - di, i, j - dj, j))
        i, j = i - di, j - dj
    groups.reverse()
    # Fold groups that are empty on one side into a neighbour, so every link has both ends.
    merged: list[list[int]] = []
    for g in groups:
        if merged and (g[0] == g[1] or g[2] == g[3]):
            merged[-1][1], merged[-1][3] = g[1], g[3]
        else:
            merged.append(list(g))
    if len(merged) > 1 and (merged[0][0] == merged[0][1] or merged[0][2] == merged[0][3]):
        first = merged.pop(0)
        merged[0][0], merged[0][2] = first[0], first[2]
    return [tuple(g) for g in merged]  # type: ignore[misc]


def paragraph_language(para: str, source: str = "auto") -> str | None:
    """Language code to translate this paragraph from, or None to leave it as it is."""
    if sum(c.isalpha() for c in para) < 2:
        return None
    if source == "auto":
        return detect_language(para)
    # A fixed source language still skips paragraphs written in the other script
    # (e.g. an English abstract inside a Russian article).
    cyr = sum(1 for c in para if "\u0400" <= c <= "\u052f")
    lat = sum(1 for c in para if c.isalpha() and c < "\u0250")
    same_script = (cyr >= lat) == source.endswith("_Cyrl")
    return source if same_script else None


# ---------------------------------------------------------------------------
# model files
# ---------------------------------------------------------------------------

def models_root() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_dir(key: str, custom_dir: str = "") -> Path:
    if custom_dir:
        return Path(custom_dir)
    return models_root() / key


def find_spm(folder: Path) -> Path | None:
    for name in (SPM_FILE, "flores200_sacrebleu_tokenizer_spm.model", "spm.model"):
        if (folder / name).is_file():
            return folder / name
    found = sorted(folder.glob("*.model"))
    return found[0] if found else None


def model_ready(folder: Path) -> bool:
    return (folder / "model.bin").is_file() and find_spm(folder) is not None and not (folder / ".downloading").exists()


def folder_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _ensure_streams() -> None:
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def download_model(key: str, progress=None, cancel: threading.Event | None = None) -> Path:
    """Download a model into the app data folder. Needs internet once; resumes if interrupted."""
    _ensure_streams()
    from huggingface_hub import hf_hub_download, snapshot_download

    spec = MODELS[key]
    target = model_dir(key)
    target.mkdir(parents=True, exist_ok=True)
    marker = target / ".downloading"
    marker.write_text("1")
    error: list[BaseException] = []
    log.info("downloading %s from %s into %s", key, spec["repo"], target)

    def work():
        try:
            snapshot_download(
                spec["repo"],
                local_dir=str(target),
                allow_patterns=["model.bin", "config.json", "shared_vocabulary.*", SPM_FILE],
            )
            if find_spm(target) is None:
                hf_hub_download(SPM_REPO, SPM_FILE, local_dir=str(target))
        except BaseException as e:  # noqa: BLE001 - reported to the caller below
            log.exception("model download failed")
            error.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if progress:
            progress("Downloading translation model", min(folder_size(target), spec["bytes"]), spec["bytes"])
        if cancel is not None and cancel.is_set():
            raise TranslationError("Download cancelled. It will resume where it stopped next time.")
    if error:
        e = error[0]
        text = f"{type(e).__name__}: {e}"
        hint = ""
        if any(k in text.lower() for k in ("connect", "resolve", "timed out", "timeout", "ssl", "proxy", "network")):
            hint = "\n\nCheck the internet connection; a firewall or proxy may be blocking huggingface.co."
        raise TranslationError(f"Could not download the translation model.\n\n{text}{hint}")
    marker.unlink(missing_ok=True)
    if not model_ready(target):
        raise TranslationError("The downloaded model is incomplete. Please try again.")
    log.info("model %s ready (%.2f GB)", key, folder_size(target) / 1e9)
    return target


# ---------------------------------------------------------------------------
# translator
# ---------------------------------------------------------------------------

MAX_TOKENS = 160


_LANG_TOKEN = re.compile(r"[a-z]{3}_[A-Z][a-z]{3}")  # eng_Latn, rus_Cyrl ... never part of the text


def _degenerate(n_src: int, hyp: list[str]) -> bool:
    """A looping or runaway translation."""
    if len(hyp) > 3 * n_src + 12:
        return True
    grams = [tuple(hyp[i : i + 3]) for i in range(len(hyp) - 2)]
    return any(grams.count(g) >= 4 for g in set(grams))


_VOWELS = set("аеёиоуыэюяәөұүіaeiouıöü")


def soften_caps(text: str) -> str:
    """Headings set in capitals translate badly; write long all-caps runs in sentence case."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8 or sum(c.isupper() for c in letters) < 0.8 * len(letters):
        return text
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        # keep acronyms (СССР, НКВД, США, USA) and Roman numerals: short words with at most one vowel
        core = w.strip(".,;:()«»\"")
        acronym = 1 < len(core) <= 5 and sum(c.lower() in _VOWELS for c in core) <= 1
        if acronym or re.fullmatch(r"[IVXLCDM]+[.,]?", w):
            out.append(w)
        else:
            out.append(w.lower())
    result = " ".join(out)
    for i, c in enumerate(result):
        if c.isalpha():
            return result[:i] + c.upper() + result[i + 1 :]
    return result


class BaseTranslator:
    """Shared paragraph handling; engines implement translate_sentences()."""

    name = "translator"

    def translate_sentences(self, sentences: list[str], src: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def translate_paragraph(self, para: str, source: str = "auto"):
        """Return (english paragraph, language or None, [(start, end, english sentence), ...]).

        start/end are character offsets of the source sentence inside `para`.
        """
        spans = split_sentence_spans(para)
        lang = paragraph_language(para, source)
        if lang is None:
            return para, None, [(a, b, para[a:b]) for a, b in spans]
        prepared = []
        for a, b in spans:
            sentence = para[a:b]
            if lang == "rus_Cyrl":
                sentence = modernize_russian(sentence)
            prepared.append(tidy_for_model(soften_caps(sentence)))
        english = self.translate_sentences(prepared, lang)
        sents = [(a, b, en) for (a, b), en in zip(spans, english)]
        return " ".join(en for _a, _b, en in sents if en), lang, sents

    def translate_paragraphs(self, paragraphs: list[str], source: str = "auto", cancel: threading.Event | None = None):
        """Translate paragraphs; returns a list of (english text, detected source code or None)."""
        return [(en, lang) for en, lang, _s in self.translate_paragraphs_full(paragraphs, source, cancel)]

    def translate_paragraphs_full(self, paragraphs: list[str], source: str = "auto", cancel: threading.Event | None = None):
        results = []
        for para in paragraphs:
            if cancel is not None and cancel.is_set():
                break
            results.append(self.translate_paragraph(para, source))
        return results


class Translator(BaseTranslator):
    """A loaded NLLB model. Heavy to create (a few seconds), cheap to reuse."""

    name = "NLLB"

    def __init__(self, folder: Path, threads: int = 0):
        import ctranslate2
        import sentencepiece as spm

        folder = Path(folder)
        spm_path = find_spm(folder)
        if not (folder / "model.bin").is_file() or spm_path is None:
            raise TranslationError(f"No translation model found in {folder}")
        self.sp = spm.SentencePieceProcessor(model_file=str(spm_path))
        cores = os.cpu_count() or 4
        threads = threads or max(1, min(8, cores // 2))
        t0 = time.time()
        self.model = ctranslate2.Translator(str(folder), device="cpu", intra_threads=threads)
        self.lock = threading.Lock()
        log.info("loaded model from %s with %d threads in %.1f s", folder, threads, time.time() - t0)

    def _pieces(self, sentence: str) -> list[list[str]]:
        """Tokenise a sentence; overly long ones are cut at clause boundaries."""
        toks = self.sp.encode(sentence, out_type=str)
        if len(toks) <= MAX_TOKENS:
            return [toks]
        chunks: list[list[str]] = []
        cur: list[str] = []
        for clause in re.split(r"(?<=[,;:—])\s+", sentence):
            t = self.sp.encode(clause, out_type=str)
            while len(t) > MAX_TOKENS:
                chunks.append(t[:MAX_TOKENS])
                t = t[MAX_TOKENS:]
            if cur and len(cur) + len(t) > MAX_TOKENS:
                chunks.append(cur)
                cur = []
            cur = cur + t
        if cur:
            chunks.append(cur)
        return chunks

    def _run(self, batch: list[list[str]], strict: bool) -> list[list[str]]:
        options = dict(
            target_prefix=[[TARGET]] * len(batch),
            beam_size=4,
            max_batch_size=2048,
            batch_type="tokens",
            max_decoding_length=320,
        )
        if strict:
            options.update(repetition_penalty=1.2, no_repeat_ngram_size=3)
        with self.lock:
            results = self.model.translate_batch(batch, **options)
        return [[t for t in r.hypotheses[0] if t != "</s>" and not _LANG_TOKEN.fullmatch(t)] for r in results]

    def translate_sentences(self, sentences: list[str], src: str) -> list[str]:
        if not sentences:
            return []
        batch: list[list[str]] = []
        owner: list[int] = []
        for i, s in enumerate(sentences):
            for toks in self._pieces(s):
                batch.append([src] + toks + ["</s>"])
                owner.append(i)
        hyps = self._run(batch, strict=False)
        # Small models sometimes loop ("the the the ...") or run on far past the
        # source. Those sentences are translated again with repetition blocked.
        bad = [k for k, (b, h) in enumerate(zip(batch, hyps)) if _degenerate(len(b) - 2, h)]
        if bad:
            log.info("retranslating %d degenerate sentence(s)", len(bad))
            for k, h in zip(bad, self._run([batch[k] for k in bad], strict=True)):
                hyps[k] = h
        out = [""] * len(sentences)
        for i, toks in zip(owner, hyps):
            piece = self.sp.decode_pieces(toks).strip()
            out[i] = (out[i] + " " + piece).strip()
        return out


# ---------------------------------------------------------------------------
# TranslateGemma through Ollama
# ---------------------------------------------------------------------------

OLLAMA_LANGS = {  # NLLB code -> (ISO code, name) as TranslateGemma expects them
    "rus_Cyrl": ("ru", "Russian"),
    "kaz_Cyrl": ("kk", "Kazakh"),
    "tur_Latn": ("tr", "Turkish"),
    "kir_Cyrl": ("ky", "Kyrgyz"),
    "uzn_Latn": ("uz", "Uzbek"),
    "tat_Cyrl": ("tt", "Tatar"),
    "ukr_Cyrl": ("uk", "Ukrainian"),
    "deu_Latn": ("de", "German"),
    "fra_Latn": ("fr", "French"),
}

if sys.platform == "darwin":
    OLLAMA_HINT = (
        "TranslateGemma runs in the free Ollama app, which is not installed or not running.\n\n"
        "Download it from https://ollama.com/download, open it once, then choose the model again.\n"
        "(Or in Terminal: brew install --cask ollama)"
    )
elif os.name == "nt":
    OLLAMA_HINT = (
        "TranslateGemma runs in the free Ollama app, which is not installed or not running.\n\n"
        "Run the Paraq installer again with:  \"Install Paraq (Windows).cmd\" -Ai gemma-4b\n"
        "or install Ollama from https://ollama.com/download, then choose the model again."
    )
else:
    OLLAMA_HINT = "TranslateGemma needs Ollama (https://ollama.com/download); install it, start it, and try again."


def _ollama_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[7:]
    if not host.startswith("http"):
        host = "http://" + host
    if host.count(":") == 1:  # no port given
        host += ":11434"
    return host.rstrip("/")


class OllamaError(RuntimeError):
    """Ollama answered with an HTTP error; .message is Ollama's own explanation."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


def _ollama_request(path: str, payload: dict | None = None, timeout: float = 600.0):
    """Call the local Ollama API; returns parsed JSON (or the open response when streaming)."""
    import urllib.error
    import urllib.request

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_ollama_url() + path, data=data, headers={"Content-Type": "application/json"})
    # Local server: never send this through a system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # Ollama explains failures in the body ({"error": "..."}); keep that text.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
            body = json.loads(body).get("error", body)
        except Exception:  # noqa: BLE001
            pass
        log.error("Ollama %s %s -> HTTP %s: %s", "POST" if data else "GET", path, e.code, body)
        raise OllamaError(e.code, body or e.reason) from None
    if payload is not None and payload.get("stream"):
        return resp
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_running() -> bool:
    try:
        _ollama_request("/api/version", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def find_ollama() -> str | None:
    import shutil

    found = shutil.which("ollama")
    if found:
        return found
    candidates = []
    if sys.platform == "darwin":
        candidates = ["/Applications/Ollama.app/Contents/Resources/ollama", "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
    elif os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
            if base:
                candidates += [os.path.join(base, "Programs", "Ollama", "ollama.exe"), os.path.join(base, "Ollama", "ollama.exe")]
    return next((c for c in candidates if os.path.isfile(c)), None)


def ensure_ollama() -> None:
    """Make sure the Ollama server answers, starting it if it is installed but not running."""
    if ollama_running():
        return
    import subprocess

    if sys.platform == "darwin" and os.path.isdir("/Applications/Ollama.app"):
        subprocess.Popen(["open", "-g", "-a", "Ollama"])
    else:
        exe = find_ollama()
        if not exe:
            raise TranslationError(OLLAMA_HINT)
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kwargs)
    for _ in range(60):
        time.sleep(0.5)
        if ollama_running():
            log.info("started Ollama")
            return
    raise TranslationError("Ollama is installed but did not start. Open the Ollama app and try again.")


def ollama_version() -> str:
    try:
        return str(_ollama_request("/api/version", timeout=3).get("version", "?"))
    except Exception:  # noqa: BLE001
        return "not running"


def _ollama_advice(message: str) -> str:
    m = message.lower()
    if "memory" in m or "out of memory" in m or "oom" in m or "allocate" in m:
        return (
            "The model does not fit in the memory that is free right now. Close other programs "
            "(browsers use a lot), or choose the smaller TranslateGemma 4B or an NLLB model in "
            "Tools > Translation Model."
        )
    if "architecture" in m or "unsupported" in m or "not supported" in m or "unknown model" in m or "newer version" in m:
        return "This Ollama is too old for TranslateGemma. Update Ollama (ollama.com/download) and try again."
    if "cuda" in m or "gpu" in m or "terminated" in m or "exit status" in m or "runner" in m:
        return (
            "Ollama's model process crashed, usually a graphics-driver problem. Update the NVIDIA driver "
            "and Ollama, then try again. If it keeps happening, run Ollama on the processor only: set the "
            "environment variable CUDA_VISIBLE_DEVICES=-1 and restart Ollama (slower, but reliable)."
        )
    return "Ollama's own log has details: " + (
        "%LOCALAPPDATA%\\Ollama\\server.log" if os.name == "nt" else "~/.ollama/logs/server.log"
    )


def ollama_has(tag: str) -> bool:
    try:
        models = _ollama_request("/api/tags", timeout=5).get("models", [])
    except Exception:  # noqa: BLE001
        return False
    names = {m.get("name", "") for m in models} | {m.get("model", "") for m in models}
    return tag in names or (tag + ":latest") in names


def ollama_pull(tag: str, progress=None, cancel: threading.Event | None = None) -> None:
    ensure_ollama()
    log.info("pulling %s through Ollama", tag)
    layers: dict[str, tuple[int, int]] = {}
    try:
        resp = _ollama_request("/api/pull", {"model": tag, "stream": True}, timeout=3600)
        with resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    raise TranslationError("Download cancelled. It will resume where it stopped next time.")
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise TranslationError(f"Ollama could not download {tag}: {msg['error']}")
                if msg.get("digest") and msg.get("total"):
                    layers[msg["digest"]] = (int(msg.get("completed", 0)), int(msg["total"]))
                if progress and layers:
                    done = sum(c for c, _t in layers.values())
                    total = sum(t for _c, t in layers.values())
                    progress("Downloading translation model", done, max(total, 1))
    except TranslationError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TranslationError(f"Could not download {tag} through Ollama: {type(e).__name__}: {e}") from e
    if not ollama_has(tag):
        raise TranslationError(f"Ollama finished, but {tag} is not listed. Please try again.")


GEMMA_PROMPT = (
    "You are a professional {name} ({code}) to English (en) translator. Your goal is to accurately "
    "convey the meaning and nuances of the original {name} text while adhering to English grammar, "
    "vocabulary, and cultural sensitivities.\n"
    "Produce only the English translation, without any additional explanations or commentary. "
    "Please translate the following {name} text into English:\n\n\n{text}"
)


class Degenerate(TranslationError):
    """The model got stuck repeating itself (Ollama: "token repeat limit reached")."""


_LEADERS = re.compile(r"(?:[.·•…_\-–—=*]\s*){4,}")  # "Введение..........5", "______", "- - - -"


def tidy_for_model(text: str) -> str:
    """Remove what sends translation models into loops: dot leaders and rules of dashes."""
    return _LEADERS.sub(" … ", text)


class OllamaTranslator(BaseTranslator):
    """TranslateGemma served by Ollama. A paragraph goes in one request as running prose, so
    the model sees the context; its sentences are matched to the original ones afterwards."""

    def __init__(self, tag: str):
        ensure_ollama()
        if not ollama_has(tag):
            raise TranslationError(f"The model {tag} is not downloaded yet.")
        self.tag = tag
        self.name = tag
        self.lock = threading.Lock()

    def _ask(self, text: str, lang: str, strict: bool = False) -> str:
        """One request. strict: discourage repetition and add a little randomness - used after
        the model looped, because at temperature 0 the same request loops the same way again."""
        code, name = OLLAMA_LANGS.get(lang, ("ru", "Russian"))
        options = {"temperature": 0, "num_ctx": 4096, "num_predict": 200 + 2 * len(text)}
        if strict:
            options.update(temperature=0.3, top_p=0.9, repeat_penalty=1.25, repeat_last_n=128)
        payload = {
            "model": self.tag,
            "messages": [{"role": "user", "content": GEMMA_PROMPT.format(name=name, code=code, text=text)}],
            "stream": False,
            "keep_alive": "20m",
            "options": options,
        }
        with self.lock:
            t0 = time.time()
            reply = None
            for attempt in (1, 2):
                try:
                    reply = _ollama_request("/api/chat", payload, timeout=900)
                    break
                except OllamaError as e:
                    if "repeat" in e.message.lower():
                        raise Degenerate(e.message) from None
                    # The first request after loading sometimes fails while the model
                    # runner (re)starts; one retry after a pause clears that.
                    if attempt == 1 and e.status >= 500:
                        log.warning("Ollama error, retrying once: %s", e.message)
                        time.sleep(4)
                        continue
                    raise TranslationError(
                        f"Ollama could not translate with {self.tag} (HTTP {e.status}).\n\n"
                        f"Ollama says: {e.message}\n\n{_ollama_advice(e.message)}\n\n"
                        f"Ollama version: {ollama_version()}"
                    ) from None
                except Exception as e:  # noqa: BLE001
                    raise TranslationError(f"Ollama did not answer: {type(e).__name__}: {e}") from e
        if reply.get("error"):
            if "repeat" in str(reply["error"]).lower():
                raise Degenerate(str(reply["error"]))
            raise TranslationError(f"Ollama: {reply['error']}")
        out = (reply.get("message") or {}).get("content", "").strip()
        if len(out) > 3 * len(text) + 300:  # ran on far past the source
            raise Degenerate("translation much longer than the original")
        log.debug("%s: %d chars in %.1f s", self.tag, len(text), time.time() - t0)
        return out

    def _ask_hard(self, text: str, lang: str) -> str:
        """Normal request; on a loop, once more with repetition discouraged."""
        try:
            return self._ask(text, lang)
        except Degenerate as e:
            log.warning("%s looped (%s); retrying with repetition discouraged", self.tag, e)
            return self._ask(text, lang, strict=True)

    def _one_by_one(self, sentences: list[str], originals: list[str], lang: str) -> list[str]:
        """Last resort: each sentence on its own; a sentence that still loops stays untranslated."""
        out = []
        for s, original in zip(sentences, originals):
            try:
                out.append(self._clean(self._ask_hard(s, lang)))
            except Degenerate:
                log.warning("left untranslated (the model kept repeating itself): %s", original[:120])
                out.append(original)
        return out

    def translate_sentences(self, sentences: list[str], src: str) -> list[str]:
        """Table cells and other short pieces: one per line in a single request."""
        if not sentences:
            return []
        tidy = [tidy_for_model(s.replace("\n", " ")) for s in sentences]
        try:
            if len(tidy) == 1:
                return [self._clean(self._ask_hard(tidy[0], src))]
            lines = [l.strip() for l in self._ask_hard("\n".join(tidy), src).splitlines()]
        except Degenerate:
            return self._one_by_one(tidy, sentences, src)
        lines = [l for l in lines if l]
        if len(lines) == len(sentences):
            return [self._clean(l) for l in lines]
        log.info("%s returned %d lines for %d pieces; translating one by one", self.tag, len(lines), len(sentences))
        return self._one_by_one(tidy, sentences, src)

    def translate_paragraph(self, para: str, source: str = "auto"):
        """The whole paragraph goes to the model as running prose (best context, one request);
        the English sentences are then matched to the original ones by length."""
        spans = split_sentence_spans(para)
        lang = paragraph_language(para, source)
        if lang is None or not spans:
            return para, None, [(a, b, para[a:b]) for a, b in spans]
        prepared = []
        for a, b in spans:
            sentence = para[a:b]
            if lang == "rus_Cyrl":
                sentence = modernize_russian(sentence)
            prepared.append(tidy_for_model(soften_caps(sentence)))
        try:
            english = " ".join(self._clean(self._ask_hard(" ".join(prepared), lang)).split())
        except Degenerate:
            # Still looping: sentence by sentence, so one bad sentence cannot sink the page.
            parts = self._one_by_one(prepared, [para[a:b] for a, b in spans], lang)
            sents = [(a, b, " ".join(t.split())) for (a, b), t in zip(spans, parts)]
            return " ".join(t for _a, _b, t in sents), lang, sents
        en_spans = split_sentence_spans(english)
        en_sents = [english[a:b] for a, b in en_spans] or [english]
        sents = []
        for i0, i1, j0, j1 in align_groups([b - a for a, b in spans], [len(t) for t in en_sents]):
            sents.append((spans[i0][0], spans[i1 - 1][1], " ".join(en_sents[j0:j1])))
        return english, lang, sents

    @staticmethod
    def _clean(text: str) -> str:
        text = text.strip().strip("`").strip()
        return re.sub(r"^(?:English|Translation)\s*:\s*", "", text, flags=re.I)


# ---------------------------------------------------------------------------
# choosing, installing and loading a model
# ---------------------------------------------------------------------------

def model_installed(key: str, custom_dir: str = "") -> bool:
    if custom_dir:
        return model_ready(Path(custom_dir))
    if engine_of(key) == "ollama":
        return ollama_running() and ollama_has(MODELS[key]["tag"])
    return model_ready(model_dir(key))


def install_model(key: str, progress=None, cancel: threading.Event | None = None) -> None:
    if engine_of(key) == "ollama":
        ollama_pull(MODELS[key]["tag"], progress, cancel)
    else:
        download_model(key, progress, cancel)


_loaded: dict[str, BaseTranslator] = {}
_load_lock = threading.Lock()


def load_translator(key: str, custom_dir: str = "") -> BaseTranslator:
    ident = custom_dir or key
    with _load_lock:
        if ident not in _loaded:
            _loaded.clear()  # keep only one model in memory
            if not custom_dir and engine_of(key) == "ollama":
                _loaded[ident] = OllamaTranslator(MODELS[key]["tag"])
            else:
                _loaded[ident] = Translator(model_dir(key, custom_dir))
        return _loaded[ident]


def get_translator(folder: Path) -> BaseTranslator:
    """NLLB model in a given folder (kept for older callers)."""
    return load_translator("", str(folder))


# ---------------------------------------------------------------------------
# translation cache
# ---------------------------------------------------------------------------

class TranslationStore:
    """Per-document cache of translated pages, kept in the app data folder."""

    def __init__(self, pdf_path: Path, model: str, source: str):
        pdf_path = Path(pdf_path)
        digest = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:20]
        folder = data_dir() / "translations"
        folder.mkdir(parents=True, exist_ok=True)
        safe_model = re.sub(r"[^A-Za-z0-9.]+", "-", model)
        self.path = folder / f"{digest}-{safe_model}-{source}.json"
        try:
            st = pdf_path.stat()
            self.signature = [st.st_size, int(st.st_mtime), model, source, 2]
        except OSError:
            self.signature = [0, 0, model, source, 2]
        self.pages: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if data.get("signature") == self.signature:
                self.pages = data.get("pages", {})
        except (OSError, ValueError):
            pass

    def get(self, page: int) -> list[dict] | None:
        return self.pages.get(str(page))

    def put(self, page: int, items: list[dict]) -> None:
        with self._lock:
            self.pages[str(page)] = items

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"signature": self.signature, "pages": self.pages}, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)

    def count(self) -> int:
        return len(self.pages)


# ---------------------------------------------------------------------------
# command line: python -m paraq.translate --download nllb-1.3B | --test
# ---------------------------------------------------------------------------

SAMPLES = {
    "rus_Cyrl": "В начале XIX века торговые пути, связывавшие степь с городами Средней Азии, постепенно меняли своё направление.",
    "kaz_Cyrl": "Қазақ даласында көшпелі мал шаруашылығы ғасырлар бойы халықтың негізгі кәсібі болды.",
    "tur_Latn": "Arşiv belgeleri, bu dönemde nüfusun iki katına çıktığını ve ticaret hacminin önemli ölçüde arttığını göstermektedir.",
}


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from .config import Settings

    ap = argparse.ArgumentParser(description="Paraq offline translation")
    ap.add_argument("--download", metavar="MODEL", choices=list(MODELS), help="download a model: " + ", ".join(MODELS))
    ap.add_argument("--use", metavar="MODEL", choices=list(MODELS), help="make this model the one the app uses")
    ap.add_argument("--test", action="store_true", help="translate a sample sentence in each language")
    args = ap.parse_args(argv)
    settings = Settings()
    if args.download:
        def progress(stage, done, total):
            print(f"\r  {stage}: {done / 1e6:,.0f} of {total / 1e6:,.0f} MB   ", end="", flush=True)

        if model_installed(args.download):
            print(f"  {args.download} is already installed")
        else:
            try:
                install_model(args.download, progress)
            except TranslationError as e:
                print(f"\n  {e}")
                return 1
            print(f"\n  {args.download} installed")
    if args.use or args.download:
        settings.set("translation_model", args.use or args.download)
        settings.set("translation_model_dir", "")
        settings.set("translation_model_chosen", True)
    if args.test:
        key, custom = settings.get("translation_model"), settings.get("translation_model_dir")
        if engine_of(key) == "ollama" and not custom:
            try:
                ensure_ollama()
            except TranslationError as e:
                print(f"  {e}")
                return 1
        if not model_installed(key, custom):
            print(f"  The translation model {custom or key} is not installed - nothing to test.")
            return 1
        t0 = time.time()
        tr = load_translator(key, custom)
        print(f"  Model: {custom or key}  (loaded in {time.time() - t0:.1f} s)")
        for lang, text in SAMPLES.items():
            t0 = time.time()
            (en, detected), = tr.translate_paragraphs([text], "auto")
            ok = "ok" if detected == lang else f"DETECTED AS {detected}"
            print(f"\n  [{lang}, {ok}, {time.time() - t0:.1f} s]\n  {text}\n  -> {en}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
