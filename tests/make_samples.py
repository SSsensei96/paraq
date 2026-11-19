"""Generate synthetic test documents: scans with 1 and 2 columns, a damaged text layer, a born-digital PDF.

Usage: python tests/make_samples.py OUTPUT_DIR
"""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
SERIF_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
SANS = str(Path(__file__).resolve().parents[1] / "paraq" / "resources" / "fonts" / "DejaVuSans.ttf")

RU = [
    "В начале XIX века торговые пути, связывавшие степные районы с городами Средней Азии, постепенно меняли своё направление. Караваны, которые прежде шли через Сырдарью к Бухаре, всё чаще поворачивали на север, к ярмаркам Оренбурга и Троицка.",
    "Купцы везли хлопок, шёлк и сушёные фрукты, а обратно доставляли железо, сукно и чай. Вместе с товарами по этим дорогам распространялись новости, письма и слухи о событиях в далёких столицах.",
    "Местные жители нанимались проводниками и погонщиками, что давало им дополнительный заработок в зимние месяцы. Постепенно вдоль дорог возникали постоялые дворы, колодцы и небольшие укрепления, вокруг которых позднее выросли города.",
    "Архивные документы показывают, что объём торговли за два десятилетия увеличился почти втрое. Однако выгоды распределялись неравномерно: крупные торговые дома получали основную прибыль, тогда как мелкие перекупщики часто разорялись.",
    "Историки по-разному оценивают последствия этих перемен. Одни подчёркивают рост благосостояния и появление новых ремёсел, другие обращают внимание на усиление зависимости степных хозяйств от внешнего рынка.",
]

KK = [
    "Қазақ даласында көшпелі мал шаруашылығы ғасырлар бойы халықтың негізгі кәсібі болды. Ауылдар жаз айларында жайлауға көшіп, күзде қыстауларына қайта оралатын.",
    "Әр рудың өз жайылымы мен су көздері болғандықтан, көш жолдары алдын ала келісілетін еді. Үлкен жәрмеңкелерде мал, тері, жүн және киіз саудаланды, ал қалалардан мата, шай, қант пен темір бұйымдар әкелінді.",
    "Ақсақалдар дауларды шешіп, жас ұрпаққа ата-бабаларының тарихын ауызша жеткізіп отырды. Жыраулар мен ақындар халықтың қуанышы мен қайғысын өлең жолдарына түсірді.",
    "Темір жол салынғаннан кейін өлкенің өмірі тез өзгере бастады. Жаңа қалалар бой көтеріп, мектептер мен баспаханалар ашылды, газеттер шыға бастады.",
    "Мұрағат құжаттары осы кезеңдегі халық санының өсуін және сауда көлемінің ұлғайғанын көрсетеді. Зерттеушілер бұл өзгерістердің себептері туралы әлі күнге дейін пікір таластыруда.",
]

TR = [
    "On dokuzuncu yüzyılın ikinci yarısında bozkır şehirleri hızla büyümeye başladı. Demiryolunun gelişiyle birlikte tüccarlar, zanaatkârlar ve memurlar bölgeye yerleşti.",
    "Yeni okullar ve matbaalar açıldı; gazeteler hem yerel haberleri hem de uzak ülkelerdeki gelişmeleri okuyuculara ulaştırdı. Çarşılarda farklı dillerin konuşulduğu, çeşitli malların satıldığı canlı bir ticaret hayatı vardı.",
    "Arşiv belgeleri, bu dönemde nüfusun iki katına çıktığını ve ticaret hacminin önemli ölçüde arttığını göstermektedir. Araştırmacılar bu değişimin nedenlerini hâlâ tartışmaktadır.",
]

EN = [
    "The archive preserves several hundred letters written by merchants who travelled between the river ports and the inland markets. Most of them describe prices, weather and the condition of the roads, but a few also record local customs in remarkable detail.",
    "Taken together, these sources allow historians to reconstruct the rhythm of commercial life with unusual precision, from the spring departure of the caravans to the closing of the autumn fairs.",
]

W, H = 1654, 2339  # A4 at 200 dpi


def _wrap(draw, words, font, width):
    lines, cur = [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and draw.textlength(trial, font=font) > width:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def draw_text(draw, paragraphs, box, font, leading, indent=40, justify=True, para_gap=0):
    """Draw justified paragraphs inside box=(x0, y0, x1); return the y after the last line."""
    x0, y, x1 = box
    for para in paragraphs:
        words = para.split()
        first = True
        # wrap with a first-line indent
        lines = []
        cur = []
        for w in words:
            avail = (x1 - x0) - (indent if not lines else 0)
            trial = " ".join(cur + [w])
            if cur and draw.textlength(trial, font=font) > avail:
                lines.append(cur)
                cur = [w]
            else:
                cur.append(w)
        if cur:
            lines.append(cur)
        for i, ln in enumerate(lines):
            lx = x0 + (indent if first else 0)
            avail = x1 - lx
            last = i == len(lines) - 1
            if justify and not last and len(ln) > 1:
                total = sum(draw.textlength(w, font=font) for w in ln)
                gap = (avail - total) / (len(ln) - 1)
                cx = lx
                for w in ln:
                    draw.text((cx, y), w, font=font, fill=20)
                    cx += draw.textlength(w, font=font) + gap
            else:
                draw.text((lx, y), " ".join(ln), font=font, fill=20)
            y += leading
            first = False
        y += para_gap
    return y


def scan_effect(img: Image.Image, seed: int, angle: float = 0.35) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=245)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    arr = np.asarray(img, dtype=np.float32)
    arr += rng.normal(0, 7, arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "L")


def page_one_column(paragraphs, header, number, seed):
    img = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(img)
    body = ImageFont.truetype(SERIF, 31)
    small = ImageFont.truetype(SERIF, 25)
    d.text((W / 2 - d.textlength(header, font=small) / 2, 150), header, font=small, fill=30)
    draw_text(d, paragraphs, (200, 260, W - 200), body, 46)
    d.text((W / 2 - d.textlength(number, font=small) / 2, H - 190), number, font=small, fill=30)
    return scan_effect(img, seed)


def page_two_columns(title, left, right, footnote, seed, rule=False, angle=-0.25):
    img = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(img)
    head = ImageFont.truetype(SERIF_BOLD, 44)
    body = ImageFont.truetype(SERIF, 27)
    small = ImageFont.truetype(SERIF, 22)
    d.text((W / 2 - d.textlength(title, font=head) / 2, 170), title, font=head, fill=20)
    gutter = 56
    col_w = (W - 2 * 170 - gutter) / 2
    xl = 170
    xr = xl + col_w + gutter
    # Different leading offsets so that the lines of the two columns do not line up exactly.
    yl = draw_text(d, left, (xl, 300, xl + col_w), body, 40, indent=34)
    yr = draw_text(d, right, (xr, 300, xr + col_w), body, 40, indent=34)
    if rule:
        d.line((xl + col_w + gutter / 2, 300, xl + col_w + gutter / 2, max(yl, yr)), fill=60, width=2)
    fy = max(yl, yr) + 70
    d.line((xl, fy - 18, xl + 380, fy - 18), fill=60, width=2)
    draw_text(d, [footnote], (xl, fy, W - 170), small, 32, indent=0)
    return scan_effect(img, seed, angle=angle)


def images_to_pdf(images, path: Path):
    doc = pymupdf.open()
    for img in images:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(str(path), deflate=True)
    doc.close()


def mojibake(s: str) -> str:
    """What cp1251 Cyrillic looks like when a PDF maps it as Latin-1."""
    return s.encode("cp1251", "replace").decode("latin-1")


def build(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    random.seed(7)

    # 1. single-column Russian scan
    p1 = page_one_column(RU, "ОЧЕРКИ ИСТОРИИ СТЕПНОЙ ТОРГОВЛИ", "— 27 —", 1)
    images_to_pdf([p1], out / "scan_1col_ru.pdf")
    p1.save(out / "scan_page.png")

    # 2. two-column scans: Russian with a column rule, Kazakh, Turkish + English
    ru2 = page_two_columns(
        "Торговые пути и ярмарки", RU[:3] + RU[3:4], RU[4:] + RU[1:3] + RU[0:1],
        "1 Сведения приводятся по материалам областного архива; подробнее см. в приложении к настоящему тому.", 2,
    )
    kk2 = page_two_columns(
        "Дала өмірі және сауда", KK[:3], KK[3:] + KK[1:2],
        "2 Деректер облыстық мұрағат қорларынан алынды.", 3, rule=True,
    )
    tr2 = page_two_columns(
        "Bozkır şehirleri", TR + TR[:1], EN + EN[:1],
        "3 Kaynak: bölge arşivi; see also the appendix to this volume.", 4,
    )
    images_to_pdf([ru2, kk2, tr2], out / "scan_2col_mixed.pdf")

    # 2b. the same kind of page scanned crooked (2 degrees one way, 1.5 the other)
    sk1 = page_two_columns("Торговые пути и ярмарки", RU[:4], RU[4:] + RU[1:3] + RU[0:1],
                           "1 Сведения приводятся по материалам областного архива; подробнее см. в приложении к настоящему тому.", 5, angle=2.0)
    sk2 = page_two_columns("Дала өмірі және сауда", KK[:3], KK[3:] + KK[1:2],
                           "2 Деректер облыстық мұрағат қорларынан алынды.", 6, rule=True, angle=-1.5)
    images_to_pdf([sk1, sk2], out / "scan_2col_skewed.pdf")

    # 3. scan with a damaged (mojibake) invisible text layer
    doc = pymupdf.open(str(out / "scan_1col_ru.pdf"))
    page = doc[0]
    y = 120
    for para in RU:
        words = mojibake(para).split()
        for i in range(0, len(words), 9):
            page.insert_text((70, y), " ".join(words[i : i + 9]), fontsize=9, fontname="helv", render_mode=3)
            y += 14
    doc.save(str(out / "damaged_layer.pdf"), deflate=True)
    doc.close()

    # 4. born-digital two-column PDF with a correct text layer
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="F0", fontfile=SANS)
    page.insert_textbox(pymupdf.Rect(60, 60, 535, 100), "Торговые пути и ярмарки степного края", fontname="F0", fontsize=18, align=1)
    gut = 22
    cw = (475 - gut) / 2
    page.insert_textbox(pymupdf.Rect(60, 110, 60 + cw, 700), "\n".join(RU[:4]), fontname="F0", fontsize=10, align=3)
    page.insert_textbox(pymupdf.Rect(60 + cw + gut, 110, 535, 700), "\n".join(RU[4:] + KK[:3]), fontname="F0", fontsize=10, align=3)
    page.insert_textbox(pymupdf.Rect(60, 720, 535, 760), "1 Сведения приводятся по материалам областного архива; подробнее см. в приложении к настоящему тому.", fontname="F0", fontsize=8)
    doc.save(str(out / "digital_2col.pdf"), deflate=True)
    doc.close()

    # 5. PDF whose *visible* text is garbled (stands in for a broken font encoding)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="F0", fontfile=SANS)
    page.insert_textbox(pymupdf.Rect(70, 80, 525, 760), "\n".join(mojibake(p) for p in RU), fontname="F0", fontsize=11, align=3)
    doc.save(str(out / "digital_garbled.pdf"), deflate=True)
    doc.close()
    print("samples written to", out)


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "samples"))
