# Paraq

Local OCR, column-aware reading and offline English translation for PDFs and scans
in Russian, Kazakh, English and Turkish, for macOS and Windows. Everything runs on
the computer; nothing is uploaded anywhere.

*Paraq* (парақ) is Kazakh for "page".

## What it does

- **Takes PDFs and scans** (PDF, PNG, JPEG, TIFF incl. multi-page, BMP, WebP). Several
  images can be combined into one document.
- **OCR with Tesseract** in Russian, Kazakh, English and Turkish, several pages in
  parallel. The result is a searchable PDF saved next to the original as
  `name_ocr.pdf`: the original page images stay untouched and an invisible text layer
  is added, so it also works in Preview, Acrobat and Spotlight.
- **Repairs damaged text layers.** Each page's existing text is checked for mojibake,
  broken font encodings, Latin/Cyrillic look-alike mixes, letters split apart and
  consonant noise. Damaged layers are removed and the page is re-read; pages whose
  text is fine are left exactly as they are.
- **Column-aware selection.** Pages are split into columns (1, 2, 3 or more, with
  full-width headings and footnotes handled separately, crooked scans deskewed). A
  selection stays inside the column where it started instead of running across the
  gutter. Copy joins the lines and undoes end-of-line hyphenation.
- **Offline translation to English** with two engines. NLLB-200 (Meta) is fast and
  translates sentence by sentence. TranslateGemma (Google, 2026), running in the free
  Ollama app, reads whole paragraphs and handles context, terms and names better.
  The language of each paragraph is detected automatically; English passages are left
  alone. Original page on the left, English on the right, page by page.
- **Linked sentences.** Point at a sentence in the PDF and its English translation
  lights up (and the reverse); select text in the PDF and the translations of those
  sentences are highlighted; click an English sentence to jump to it in the PDF.
- **Translated PDF that looks like the original.** The English is set in place of
  the original text: graphs, maps, photos, table rules, numbers and untranslated
  passages stay exactly as they are; prose keeps its column, size and alignment;
  table cells, captions and labels are translated one by one in their own place.
  Also as original and translated pages side by side. The English is real text that
  can be selected and searched.
- **Other exports**: English as Word or text, original and English side by side as a
  Word table, recognised text as Word or text.

## Install on Windows 10 / 11

1. Unzip the whole folder (right-click > *Extract All*).
2. Double-click **`Install Paraq (Windows).cmd`**. If Windows SmartScreen warns, click
   *More info* > *Run anyway*.

The installer does everything by itself and needs no administrator rights, with one
exception: Tesseract OCR's official installer asks for permission once (click *Yes*).
It downloads and sets up, in this order:

| Step | Size | Where |
|---|---|---|
| Python 3.12 and the libraries (private copy, does not touch any other Python) | ~250 MB | `%LOCALAPPDATA%\Programs\Paraq` |
| Tesseract OCR (via winget, or the University of Mannheim installer) | ~50 MB | `C:\Program Files\Tesseract-OCR` |
| OCR language data: Russian, Kazakh, English, Turkish | ~50 MB | `%LOCALAPPDATA%\Paraq\tessdata` |
| Translation model NLLB 1.3B | 1.4 GB | `%LOCALAPPDATA%\Paraq\models` |

Then it creates Start-menu and desktop shortcuts, adds Paraq to *Settings > Apps* (for
uninstalling), runs a self-check plus a test translation, and starts Paraq. Expect
10-20 minutes on a normal connection. Running the installer again updates or repairs
the installation; finished downloads are not repeated.

Options: open a terminal in the folder (right-click the folder background > *Open in
Terminal*). In **PowerShell** (the Windows default) put `& .\` in front:

```
& ".\Install Paraq (Windows).cmd" -Ai gemma-4b        also install Ollama + TranslateGemma 4B (3.3 GB)
& ".\Install Paraq (Windows).cmd" -Ai gemma-12b       ... or the 12B model (8.1 GB, slow without a big GPU)
& ".\Install Paraq (Windows).cmd" -Model nllb-600M    smaller, faster model (0.6 GB)
& ".\Install Paraq (Windows).cmd" -Model nllb-3.3B    best NLLB quality (3.4 GB)
& ".\Install Paraq (Windows).cmd" -Model none         skip; Paraq offers it on first Translate
& ".\Install Paraq (Windows).cmd" -FastOcr            faster, slightly less accurate OCR
```

In the old Command Prompt (`cmd`), leave out the `& .\`: `"Install Paraq (Windows).cmd" -Ai gemma-4b`.

A log of the installation is written to `%LOCALAPPDATA%\Programs\Paraq\install.log`.

## Install on a Mac

You need [Homebrew](https://brew.sh). Then, in Terminal:

```bash
cd path/to/paraq
bash build_mac.sh
```

This installs Tesseract, downloads the OCR language data for the four languages
(about 50 MB), creates a private Python environment, and builds `dist/Paraq.app`.
Drag the app into Applications. It takes 5-10 minutes the first time.

To try it without building the app: `bash run_mac.command`.

For the smarter TranslateGemma translator as well: `PARAQ_AI=gemma-4b bash build_mac.sh`
(installs Ollama with Homebrew and downloads the model, 3.3 GB); use `gemma-12b` only
on a Mac with 24 GB of memory or more. Or install Ollama from
ollama.com/download later and pick the model in *Tools > Translation Model*.

`dist/Paraq.app/Contents/MacOS/Paraq --selftest` lists every component and whether
it is in place.

Faster but slightly less accurate OCR: `TESSDATA=fast bash build_mac.sh`.

## Using it

1. **Add Files** (or drag PDFs, images or a folder onto the window). Each document
   shows how many pages need OCR and how many have a damaged text layer.
2. **Run OCR.** Tick the languages that actually occur in the document and pick the
   main one; fewer languages is faster and more accurate. "Only pages with no text
   layer or a damaged one" is the default; "Re-OCR every page" replaces a text layer
   that looks fine but is wrong.
3. **Read, select, copy.** Drag to select; the selection stays in its column.
   Double-click selects a word, ⌘A the whole page, right-click > *Select This
   Column*. *View > Show Detected Columns* (⌘L) draws the columns with their reading
   order. *View > Keep Selection Inside One Column* can be switched off to let a
   selection flow on into the next column.
4. **Translate.** The first time, the app asks which model to use
   (*Tools > Translation Model* changes it later):

   | Model | Download | Speed per page | Notes |
   |---|---|---|---|
   | NLLB 600M / 1.3B / 3.3B | 0.6 / 1.4 / 3.4 GB | seconds | fast; sentence by sentence |
   | TranslateGemma 4B | 3.3 GB | ~1 min with a laptop NVIDIA GPU | smarter; best choice for a 4 GB GPU (e.g. RTX 3050 Ti) |
   | TranslateGemma 12B | 8.1 GB | ~1 min on an M-series Mac | smartest; needs a Mac with 24 GB or more. On 16 GB it runs at the edge of memory and Ollama can fail mid-document (HTTP 500) |

   TranslateGemma needs the free [Ollama](https://ollama.com/download) app; Paraq starts
   it when needed and downloads the model through it. Speeds are estimates; without a
   GPU it is several times slower. After the download, translation works without
   internet. Translations are cached per document and model, so reopening a document
   or switching back to a model shows them immediately. Right-click > *Translate
   Selection* translates just the selected passage.
5. **Follow the links.** In the English panel, every sentence is tied to the sentence
   it was translated from. Hover over either one to highlight both; select text in the
   PDF to highlight its translation; click an English sentence to scroll the PDF to
   it. *View > Link Translation to Original* switches this off.
6. **Export** from the toolbar. *Translated PDF, Layout Kept* writes a copy of the
   document with English in place of the original text; *Original and Translated
   Pages Side by Side* puts each original page next to its translation, for checking.
   If some pages are not translated yet, Paraq offers to translate them first. Table
   cells and labels are translated while the PDF is built, with the current model.

## When translation misbehaves

- **Help > Test Translation** translates one sample sentence each in Russian, Kazakh and
  Turkish and shows the time taken. Use it to tell a broken setup from a hard text.
- **Help > Open Log Folder** opens the folder with `paraq.log`. It records every
  error and, for each translated page, the languages found and the time taken. Send
  this file along with a description of the problem.
- **Tools > Translate This Page Again** and **Forget Translations of This Document**
  throw away a saved translation (translations are cached per document).
- From a terminal: `python run_paraq.py --test-translation` and `--selftest`.

Fixed in 0.2: in the packaged Mac app (and with `pythonw` on Windows) the model
download crashed at once with *'NoneType' object has no attribute 'write'*, because
the downloader's progress bar had no console to write to. Translation quality was
also lowered by a repetition penalty that pushed the model away from words English
must repeat ("the", "of"); that is gone, and only sentences that really loop are
retranslated with stricter settings. Headings in capitals are now translated in
sentence case. A Mac built with 0.1 needs to be rebuilt (`bash build_mac.sh`).

## Which translator?

NLLB is fast but works one sentence at a time with no idea of the surrounding text.
TranslateGemma is a translation-tuned version of Google's Gemma 3 language model; it
sees the whole paragraph, so pronouns, terms and names come out more consistently and
the English reads more naturally. It is slower and larger. In Google's tests the 12B
model beat the general Gemma 3 27B at translation. One independent evaluation
(LoResMT 2026) found it weaker than NLLB for Russian-to-Kazakh, so for Kazakh source
texts it is worth comparing both on a page (switch the model, then *Translate This
Page*; each model's translations are kept separately).

## OCR accuracy on old books

Tested on a 638-page scan of *История Казахской ССР* (1957). The share of words with an
OCR error fell from about 7.5 % to 4.9 % with these steps (the rest are mostly Kazakh and
historical terms that are correct but unknown to the dictionary):

- **Small scans are enlarged** before OCR (up to 2.2x), so the text is as large as on an
  A4 page at 300 dpi; book-page scans of ~1700 px gain the most.
- **A second look** finds printed areas OCR skipped - typically section titles set in the
  margin beside a paragraph - and reads them on their own. The layout recognises such
  margin headings and keeps the paragraph beside them in one piece.
- **Dictionary clean-up for Russian** (Run OCR > Accuracy) fixes the typical letter
  mix-ups of mid-century print (и/н, л/д, ш/щ, о/э...): a word the dictionary does not
  know is changed only if exactly one likely variant is a real word. Names, words split
  at the end of a line (judged only as a whole), and unknown words spelled the same way
  three or more times in the book (real terms) are protected. Roman numerals read in
  Cyrillic ("ХІХ в.") are written in Latin letters.
- **Compounds keep their hyphen** when they are split over two lines
  ("феодально-/монархические" stays "феодально-монархические").

Languages: for a Russian book choose **Russian + English**. English lets OCR read Roman
numerals correctly (with Russian alone "XIX" can come out as "ХХ"); adding Kazakh helps
little and puts Kazakh letters into Russian names ("Қаратай"). Choose Kazakh for Kazakh
texts, and both only for genuinely mixed ones.

## How the translated PDF is made

For every translated page Paraq covers each piece of original text with the colour of
the paper around it and sets the English in the same place. Prose paragraphs get the
cached paragraph translation, fitted to the paragraph box at a font size measured from
the original words (it shrinks only if the English is longer than the space). Tables,
captions, headings in odd positions and columns of labels are handled line by line and
cell by cell, so rules and numbers are never touched. Everything else on the page is
left alone. Limits: the English is set in one serif font (no bold or italics); a page
with a strongly coloured or patterned background shows the covered areas as flat
patches; and text inside photos or maps is only replaced when OCR read it as words.

## Good to know

- **Translation quality.** Both engines are offline models that fit on a laptop. They
  are good enough to understand a text and find the passages that matter, but below
  the large online services, especially for Kazakh, long academic sentences, names and
  historical terms. Check anything you intend to quote.
- **Licences.** NLLB-200: CC BY-NC 4.0 (non-commercial use). TranslateGemma: Google's
  Gemma terms of use.
- **OCR quality** depends on the scan. Tesseract reads clean print well and struggles
  with faint, warped or very small type. Pre-1918 Russian orthography needs extra
  language data: put any additional `.traineddata` file into the folder opened by
  *Tools > Open OCR Language Folder* and it appears in the Run OCR dialog.
- **A page with garbled *visible* text** (a born-digital PDF with a broken font map)
  is replaced by a picture of itself before OCR, so its file size grows.
- **Column detection** is geometric. Dense newspaper pages with irregular layouts can
  still come out in the wrong order; *Show Detected Columns* shows what was found.
- The app needs Homebrew's Tesseract to stay installed on the Mac.
- On Linux: `pip install -r requirements.txt`, install Tesseract, `python run_paraq.py`.

## Where things are kept

| What | Mac | Windows |
|---|---|---|
| Searchable PDFs | next to the original, `name_ocr.pdf` | same |
| Translation models | `~/Library/Application Support/Paraq/models` | `%LOCALAPPDATA%\Paraq\models` |
| Translation cache, settings, `paraq.log` | `~/Library/Application Support/Paraq` | `%LOCALAPPDATA%\Paraq` |
| Extra OCR languages | `~/Library/Application Support/Paraq/tessdata` | `%LOCALAPPDATA%\Paraq\tessdata` |
| The program itself | `Paraq.app` | `%LOCALAPPDATA%\Programs\Paraq` |

## Command line

```bash
.venv/bin/python -m paraq.ocr scan.pdf                 # writes scan_ocr.pdf
.venv/bin/python -m paraq.ocr scan.pdf out.pdf --langs rus+kaz --force
.venv/bin/python -m paraq.translate --download nllb-600M --test
```

On Windows the Python is `%LOCALAPPDATA%\Programs\Paraq\venv\Scripts\python.exe`; run it
from `%LOCALAPPDATA%\Programs\Paraq\app`.

## Tests

```bash
.venv/bin/pip install numpy
.venv/bin/python tests/test_engine.py                 # OCR, columns, damaged layers, skew
.venv/bin/python tests/test_translate_mechanics.py    # translation path with a tiny stand-in model
```

The tests generate their own sample scans (they use the DejaVu Serif fonts found on
Linux; on a Mac, change the two font paths at the top of `tests/make_samples.py`).

## Layout of the code

| File | Purpose |
|---|---|
| `paraq/ocr.py` | page analysis, Tesseract, writing the searchable PDF |
| `paraq/textquality.py` | detects damaged text layers |
| `paraq/layout.py` | skew, columns, lines, paragraphs, selection |
| `paraq/translate.py` | language detection, sentence splitting, NLLB, cache |
| `paraq/export.py` | Word and text export |
| `paraq/ui/` | window, PDF view, background jobs, dialogs |

Bundled font: DejaVu Sans (free licence, see dejavu-fonts.github.io), used only for
the invisible text layer.
