# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPECPATH)
REPO_ROOT = ROOT.parent.parent
APP_NAME = "Astro-Note-AI"
ICON_CANDIDATES = [
    ROOT / "Astro-Note-AI.ico",
    ROOT / "AstroPhLLMWiki.ico",
    REPO_ROOT / "AstroPhLLMWiki.ico",
]


def first_existing_path(paths):
    for path in paths:
        if path.exists():
            return path
    return None


def app_datas():
    entries = []
    include_roots = [
        "config",
        "conversations",
        "data",
        "graphify-out",
        "logs",
        "reports",
        "scripts",
        "src",
        "ui",
        "wiki",
    ]
    skip_parts = {"__pycache__", "build", "exports"}
    for name in include_roots:
        root = ROOT / name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if any(part in skip_parts for part in rel.parts):
                continue
            if path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            entries.append((str(path), str(Path("app") / rel.parent)))
    for name in ["index.html"]:
        path = ROOT / name
        if path.exists():
            entries.append((str(path), "app"))
    return entries


def optional_submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []


def optional_data_files(package):
    try:
        return collect_data_files(package)
    except Exception:
        return []


def optional_metadata(package):
    try:
        return copy_metadata(package)
    except Exception:
        return []


hiddenimports = (
    optional_submodules("webview")
    + optional_submodules("clr_loader")
    + optional_submodules("pythonnet")
    + [
        "_bootstrap",
        "feedparser",
        "fitz",
        "httpx",
        "pymupdf",
        "pymupdf4llm",
        "pydantic",
        "ui_server",
        "yaml",
    ]
)

datas = app_datas()
for package in ["feedparser", "pymupdf", "pymupdf4llm", "pydantic", "webview"]:
    datas += optional_data_files(package)
    datas += optional_metadata(package)


icon_path = first_existing_path(ICON_CANDIDATES)

a = Analysis(
    ["astro_wiki_desktop.py"],
    pathex=[str(ROOT), str(ROOT / "scripts"), str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PIL.ImageQt",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "notebook",
        "numpy.tests",
        "pandas",
        "scipy",
        "sklearn",
        "tensorflow",
        "tkinter",
        "torch",
        "transformers",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)
