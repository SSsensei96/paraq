"""Paths and persistent settings (no Qt dependency so the engine can run headless)."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME

# Languages the app is built around: Tesseract code -> display name.
CORE_LANGS = {"rus": "Russian", "kaz": "Kazakh", "eng": "English", "tur": "Turkish"}

DEFAULTS = {
    "ocr_langs": ["rus", "kaz", "eng", "tur"],
    "ocr_mode": "auto",  # auto | force
    "ocr_fix_russian": True,  # dictionary clean-up of typical OCR letter confusions
    "ocr_second_look": True,  # OCR skipped text (margin headings, notes) on its own
    "ocr_dpi": 300,
    "ocr_workers": 0,  # 0 = automatic
    "output_dir": "",  # "" = next to the original file
    "confine_selection": True,
    "reflow_copy": True,
    "show_columns": False,
    "translation_model": "nllb-1.3B",
    "translation_model_dir": "",  # optional manually chosen model folder
    "translation_source": "auto",
    "translation_model_chosen": False,
    "link_translation": True,
    "documents": [],
}


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        # Local, not Roaming: translation models are gigabytes and must not sync with the profile.
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(rel: str) -> Path:
    """Locate a bundled resource both in a source checkout and in a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "paraq" / "resources" / rel
    return Path(__file__).resolve().parent / "resources" / rel


FONT_PATH = resource_path("fonts/DejaVuSans.ttf")


class Settings:
    """Small JSON-backed settings store."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "settings.json")
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        try:
            self._data.update(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def get(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


if __name__ == "__main__":
    # python -m paraq.config --set KEY VALUE   (used by the Windows installer)
    if len(sys.argv) == 4 and sys.argv[1] == "--set":
        Settings().set(sys.argv[2], sys.argv[3])
        print(f"  {sys.argv[2]} = {sys.argv[3]}")
    else:
        print(f"Settings file: {data_dir() / 'settings.json'}")
