"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> str:
    """Log to <data folder>/paraq.log; give a windowed app (no console) somewhere to write."""
    from .config import data_dir

    path = data_dir() / "paraq.log"
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".log.old"))
    except OSError:
        pass
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    # pythonw on Windows and packaged apps have no stdout/stderr; libraries that
    # print (progress bars, tracebacks) would crash. Send them to the log instead.
    if sys.stdout is None or sys.stderr is None:
        stream = open(path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream

    def hook(exc_type, exc, tb):
        logging.getLogger("paraq").error("unhandled error", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from . import __version__

    logging.getLogger("paraq").info("Paraq %s, Python %s, %s", __version__, sys.version.split()[0], sys.platform)
    return str(path)


def selftest() -> int:
    """Check that every component is importable and installed: Paraq --selftest"""
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"  ok    {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")

    def tesseract():
        from . import ocr

        path = ocr.find_tesseract()
        if not path:
            raise RuntimeError("not installed (brew install tesseract)")
        langs = ocr.available_languages(refresh=True)
        missing = [l for l in ("rus", "kaz", "eng", "tur") if l not in langs]
        if missing:
            raise RuntimeError(f"{path} found, but language data missing for: {', '.join(missing)}")
        return f"{path}, {len(langs)} languages incl. rus, kaz, eng, tur"

    def font():
        import pymupdf

        from .config import FONT_PATH

        return f"{pymupdf.Font(fontfile=str(FONT_PATH)).name} for the text layer"

    def model():
        from .config import Settings
        from .translate import MODELS, engine_of, find_ollama, model_installed, ollama_running

        s = Settings()
        key, custom = s.get("translation_model"), s.get("translation_model_dir")
        name = custom or MODELS.get(key, {}).get("label", key)
        if engine_of(key) == "ollama" and not custom:
            if not (ollama_running() or find_ollama()):
                raise RuntimeError(f"{name}: Ollama is not installed")
            if not ollama_running():
                return f"{name} (Ollama installed, starts when you translate)"
        return f"{name}: " + ("installed" if model_installed(key, custom) else "not downloaded yet (happens on first Translate)")

    check("PDF engine", lambda: __import__("pymupdf").__version__)
    check("Window toolkit", lambda: __import__("PySide6").__version__)
    check("Translation engine", lambda: __import__("ctranslate2").__version__)
    check("Tokenizer", lambda: __import__("sentencepiece").__version__)
    check("Model downloader", lambda: __import__("huggingface_hub").__version__)
    check("Word export", lambda: __import__("docx").__name__ and "python-docx")
    check("Font", font)
    check("Tesseract OCR", tesseract)
    check("Translation model", model)
    print("All good." if ok else "Some components are missing - see above.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--test-translation" in argv:
        from .translate import _cli

        return _cli(["--test"])
    setup_logging()
    try:  # keep the hyphen in compounds split over two lines (needs the Russian dictionary)
        from .ocrfix import install_hyphen_judge

        install_hyphen_judge()
    except Exception:  # noqa: BLE001
        pass
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME, __version__
    from .config import resource_path
    from .ui.mainwindow import MainWindow

    class App(QApplication):
        """Handles files opened from Finder (double-click / drop on the Dock icon)."""

        window = None

        def event(self, e):
            if e.type() == QEvent.FileOpen and self.window is not None:
                self.window.add_paths([e.file()])
                return True
            return super().event(e)

    if sys.platform.startswith("win"):
        try:  # own taskbar group and icon instead of Python's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Paraq.App")
        except Exception:  # noqa: BLE001
            pass
    app = App(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    icon = resource_path("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    app.window = window
    window.show()
    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        window.add_paths(files)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
