# PyInstaller recipe for the macOS app bundle (used by build_mac.sh).
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("paraq/resources", "paraq/resources")]
binaries = []
hiddenimports = []
import importlib.util

for pkg in ("ctranslate2", "sentencepiece", "pymorphy3", "pymorphy3_dicts_ru", "dawg2_python") + (("hf_xet",) if importlib.util.find_spec("hf_xet") else ()):
    if not importlib.util.find_spec(pkg):
        continue
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += collect_data_files("docx")  # Word template used by python-docx
datas += collect_data_files("certifi")

a = Analysis(
    ["run_paraq.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["huggingface_hub", "docx"],
    excludes=["tkinter", "matplotlib", "torch", "transformers", "scipy", "pytest", "_pytest", "pkg_resources", "setuptools", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Paraq",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Paraq")
app = BUNDLE(
    coll,
    name="Paraq.app",
    icon="packaging/Paraq.icns",
    bundle_identifier="local.paraq.app",
    info_plist={
        "CFBundleDisplayName": "Paraq",
        "CFBundleShortVersionString": "0.5.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF or scanned image",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf", "public.png", "public.jpeg", "public.tiff"],
            }
        ],
    },
)
