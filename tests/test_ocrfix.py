"""Dictionary clean-up of Russian OCR text."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paraq import ocrfix


class W:
    def __init__(self, t):
        self.text = t


def run(lines, protected=frozenset()):
    ws = [[W(t) for t in line.split()] for line in lines]
    ocrfix.fix_lines(ws, protected)
    return [" ".join(w.text for w in l) for l in ws]


def test_ocrfix():
    if not ocrfix.available():
        print("   (dictionary not installed - skipped)")
        return
    # typical misreadings of mid-century print
    assert run(["в их рвах зняет могнла родового строя"]) == ["в их рвах зияет могила родового строя"]
    assert run(["пряностн и другне товары эграниченными партиями"]) == ["пряности и другие товары ограниченными партиями"]
    # Roman numerals in Cyrillic look-alikes
    assert run(["в начале ХІХ в. и в ХУІІ веке"]) == ["в начале XIX в. и в XVII веке"]
    # a word broken at the line end is judged whole; its halves are never "corrected" alone
    assert run(["отряд возвратился в продви-", "женне войск оказа-", "лись трудности"]) == ["отряд возвратился в продви-", "жение войск оказа-", "лись трудности"]
    assert run(["конь и ло-", "шаль стояли"]) == ["конь и ло-", "шадь стояли"]
    # names and correct words are left alone; a capitalised unknown word mid-sentence only becomes a place/name
    assert run(["султан Арынгазы Абулгазиев и Каратай"]) == ["султан Арынгазы Абулгазиев и Каратай"]
    assert run(["по словам Полиен у кочевников"]) == ["по словам Полиен у кочевников"]
    assert run(["пределы Россин и Средней Азни"]) == ["пределы России и Средней Азии"]
    # repeated unknown spellings are real terms: only the и/н misreading may still be fixed
    assert run(["памятники андроновских племен"], frozenset({"андроновских"})) == ["памятники андроновских племен"]
    assert run(["памятники андроновских племен"]) == ["памятники андроповских племен"]  # (why protection matters)
    # hyphenated compounds keep their hyphen
    assert ocrfix.keep_hyphen("феодально", "монархические") and not ocrfix.keep_hyphen("продви", "жение")
    print("   dictionary clean-up OK")


if __name__ == "__main__":
    test_ocrfix()
    print("PASS  test_ocrfix")
