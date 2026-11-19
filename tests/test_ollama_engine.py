"""TranslateGemma engine against the stand-in Ollama server: download, prompts, alignment, fallback."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ollama import FakeOllama


def test_ollama_engine():
    fake = FakeOllama()
    os.environ["OLLAMA_HOST"] = fake.host
    from paraq import translate as T

    T._loaded.clear()
    assert T.ollama_running()
    assert not T.model_installed("gemma-4b")
    seen = []
    T.install_model("gemma-4b", progress=lambda stage, done, total: seen.append((done, total)))
    assert T.model_installed("gemma-4b") and seen[-1] == (3_000_000, 3_000_000), seen
    tr = T.load_translator("gemma-4b")

    ru = "В 1917 г. в Киеве выходило семь газет. Тираж был невелик! Что дальше?"
    en, lang, sents = tr.translate_paragraph(ru)
    assert lang == "rus_Cyrl" and en == "[EN] " + ru, en
    assert [ru[a:b] for a, b, _ in sents] == ["В 1917 г. в Киеве выходило семь газет.", "Тираж был невелик!", "Что дальше?"]
    assert [t for _a, _b, t in sents] == ["[EN] В 1917 г. в Киеве выходило семь газет.", "Тираж был невелик!", "Что дальше?"]
    assert "Russian (ru) to English (en)" in fake.prompts[-1] and len(fake.prompts) == 1  # one request per paragraph
    assert "\n" not in fake.prompts[-1].split("\n\n\n", 1)[1]  # sent as running prose

    kk = "Қазақ даласында көшпелі мал шаруашылығы ғасырлар бойы негізгі кәсіп болды."
    assert tr.translate_paragraph(kk)[1] == "kaz_Cyrl" and "Kazakh (kk)" in fake.prompts[-1]

    n = len(fake.prompts)
    english = "The archive preserves letters. They describe prices."
    en, lang, sents = tr.translate_paragraph(english)
    assert lang is None and en == english and len(sents) == 2 and len(fake.prompts) == n  # not sent to the model

    n = len(fake.prompts)
    merged = "MERGE первое предложение здесь. Второе предложение тут. Третье тоже есть."
    en, lang, sents = tr.translate_paragraph(merged)
    assert len(fake.prompts) == n + 1  # no extra requests when the model merges sentences
    assert len(sents) == 2 and merged[sents[0][0]:sents[0][1]].endswith("тут."), sents  # first two linked as a group
    split = "SPLIT первое, длинное предложение здесь. Второе тут."
    en, lang, sents = tr.translate_paragraph(split)
    assert len(sents) == 2 and sents[0][2].count(".") == 2, sents  # one source sentence, two English ones
    assert " ".join(t for _a, _b, t in sents) == en

    heading = "ОЧЕРКИ ИСТОРИИ СТЕПНОЙ ТОРГОВЛИ"
    tr.translate_paragraph(heading)
    assert fake.prompts[-1].endswith("Очерки истории степной торговли")
    # the model loops once: retried with repetition discouraged, still one paragraph
    en, lang, sents = tr.translate_paragraph("LOOP первое предложение здесь. Второе тут.")
    assert len(sents) == 2 and en.startswith("[EN]"), (en, sents)
    # the model keeps looping on one sentence: the others are translated, that one stays as it was
    para = "Первое предложение здесь. LOOPHARD второе тут. Третье тоже."
    en, lang, sents = tr.translate_paragraph(para)
    texts = [t for _a, _b, t in sents]
    assert texts[0].startswith("[EN]") and texts[1] == "LOOPHARD второе тут." and texts[2].startswith("[EN]"), texts
    # dot leaders are shortened before they reach the model
    tr.translate_paragraph("Введение.......................................5")
    assert "...." not in fake.prompts[-1] and "…" in fake.prompts[-1]
    # a server error once: retried and succeeds
    en, lang, sents = tr.translate_paragraph("FLAKY первое предложение здесь.")
    assert sents and sents[0][2].startswith("[EN]")
    # a server error every time: Ollama's own message reaches the user, with advice
    try:
        tr.translate_paragraph("FAIL500 первое предложение здесь.")
        raise AssertionError("no error raised")
    except T.TranslationError as e:
        msg = str(e)
        assert "HTTP 500" in msg and "more system memory" in msg and "Close other programs" in msg and "0.0-fake" in msg, msg
    fake.close()
    del os.environ["OLLAMA_HOST"]
    T._loaded.clear()
    print("   TranslateGemma engine: download, prompts, alignment and fallback OK")


if __name__ == "__main__":
    test_ollama_engine()
    print("PASS  test_ollama_engine")
