"""Mechanical test of the offline translation path with a tiny random stand-in model.

The real NLLB model is 0.6-3.4 GB and is downloaded on first use, so this test
builds a miniature model with the same interface (CTranslate2 Transformer +
SentencePiece vocabulary + NLLB language tokens) and runs it through
paraq.translate.Translator. The "translations" are random; the point is that
loading, tokenising, batching, language routing, long-sentence splitting and
detokenising all work with the real libraries.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import make_samples as ms

LANGS = ["rus_Cyrl", "kaz_Cyrl", "tur_Latn", "eng_Latn", "kir_Cyrl", "uzn_Latn", "tat_Cyrl", "ukr_Cyrl", "deu_Latn", "fra_Latn"]


def build_tiny_model(folder: Path, d: int = 16, ffn: int = 32) -> None:
    import sentencepiece as spm
    from ctranslate2.specs import transformer_spec

    corpus = folder / "corpus.txt"
    corpus.write_text("\n".join(ms.RU + ms.KK + ms.TR + ms.EN) * 3, "utf-8")
    spm.SentencePieceTrainer.train(
        input=str(corpus), model_prefix=str(folder / "sentencepiece.bpe"), vocab_size=420,
        model_type="bpe", character_coverage=1.0, bos_id=0, pad_id=1, eos_id=2, unk_id=3,
    )
    sp = spm.SentencePieceProcessor(model_file=str(folder / "sentencepiece.bpe.model"))
    tokens = [sp.id_to_piece(i) for i in range(sp.get_piece_size())] + LANGS
    vocab = len(tokens)
    rng = np.random.default_rng(0)

    def rand(*shape):
        return (rng.standard_normal(shape) * 0.3).astype(np.float32)

    def norm(ln):
        ln.gamma, ln.beta = np.ones(d, np.float32), np.zeros(d, np.float32)

    def linear(lin, out, inp):
        lin.weight, lin.bias = rand(out, inp), np.zeros(out, np.float32)

    def ffn_(f):
        norm(f.layer_norm)
        linear(f.linear_0, ffn, d)
        linear(f.linear_1, d, ffn)

    spec = transformer_spec.TransformerSpec.from_config(num_layers=(1, 1), num_heads=2, pre_norm=True)
    enc, dec = spec.encoder, spec.decoder
    enc.embeddings[0].weight = rand(vocab, d)
    norm(enc.layer_norm)
    for layer in enc.layer:
        norm(layer.self_attention.layer_norm)
        linear(layer.self_attention.linear[0], 3 * d, d)
        linear(layer.self_attention.linear[1], d, d)
        ffn_(layer.ffn)
    dec.embeddings.weight = rand(vocab, d)
    norm(dec.layer_norm)
    linear(dec.projection, vocab, d)
    for layer in dec.layer:
        norm(layer.self_attention.layer_norm)
        linear(layer.self_attention.linear[0], 3 * d, d)
        linear(layer.self_attention.linear[1], d, d)
        norm(layer.attention.layer_norm)
        linear(layer.attention.linear[0], d, d)
        linear(layer.attention.linear[1], 2 * d, d)
        linear(layer.attention.linear[2], d, d)
        ffn_(layer.ffn)
    spec.register_source_vocabulary(tokens)
    spec.register_target_vocabulary(tokens)
    spec.validate()
    spec.optimize(quantization="int8")
    spec.save(str(folder))
    corpus.unlink()


def test_translator_mechanics():
    from paraq.translate import TranslationStore, Translator, model_ready

    folder = Path(tempfile.mkdtemp(prefix="paraq_tinymodel_"))
    build_tiny_model(folder)
    assert model_ready(folder)
    tr = Translator(folder)
    long_sentence = ", ".join(ms.RU[0].rstrip(".") for _ in range(12)) + "."  # forces clause splitting
    paras = [ms.RU[0], ms.EN[0], ms.KK[1], ms.TR[0], "— 27 —", long_sentence]
    out = tr.translate_paragraphs(paras, "auto")
    assert len(out) == len(paras)
    langs = [lang for _text, lang in out]
    assert langs == ["rus_Cyrl", None, "kaz_Cyrl", "tur_Latn", None, "rus_Cyrl"], langs
    assert out[1][0] == ms.EN[0] and out[4][0] == "— 27 —"  # English and non-text pass through untouched
    for (text, lang), src in zip(out, paras):
        assert isinstance(text, str)
        if lang:
            assert "eng_Latn" not in text and "</s>" not in text and lang not in text
    assert len(tr.sp.encode(long_sentence, out_type=str)) > 160 and all(len(c) <= 160 for c in tr._pieces(long_sentence))
    # forced source language still leaves the English paragraph alone
    forced = tr.translate_paragraphs([ms.RU[1], ms.EN[1]], "rus_Cyrl")
    assert forced[0][1] == "rus_Cyrl" and forced[1] == (ms.EN[1], None)
    # cache round trip
    pdf = folder / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    store = TranslationStore(pdf, "tiny", "auto")
    store.put(0, [{"src": s, "en": en, "lang": lang} for s, (en, lang) in zip(paras, out)])
    store.save()
    again = TranslationStore(pdf, "tiny", "auto")
    assert again.get(0) and again.get(0)[1]["en"] == ms.EN[0] and again.get(3)is None
    assert TranslationStore(pdf, "other-model", "auto").get(0) is None  # another model: cache not reused
    store.path.unlink()
    print("   tiny-model translation path OK:", len(out), "paragraphs,", sum(1 for l in langs if l), "sent through the model")


if __name__ == "__main__":
    test_translator_mechanics()
    print("PASS  test_translator_mechanics")
