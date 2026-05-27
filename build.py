"""Build Pub-Streamer into a standalone Windows executable using Nuitka.

Usage (from project root):
    uv run python build.py

On CI the GITHUB_ACTIONS env var is set automatically, which switches the
compiler to MinGW (GCC) — it handles the large generated C files that cause
MSVC to run out of heap on constrained machines.
"""

import os
import subprocess
import sys
import pathlib

ROOT   = pathlib.Path(__file__).parent
DIST   = ROOT / "dist"
LOCALE = ROOT / "locale"
SOUNDS = ROOT / "sounds"

ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"

# Data directories to bundle as-is
data_dirs = []
if LOCALE.exists():
    data_dirs.append(f"--include-data-dir={LOCALE}=locale")
if SOUNDS.exists():
    data_dirs.append(f"--include-data-dir={SOUNDS}=sounds")
native_dir = ROOT / "native"
if native_dir.exists():
    data_dirs.append(f"--include-data-dir={native_dir}=native")

icon = ROOT / "pubstreamer" / "ui" / "icon.ico"

cmd = [
    sys.executable, "-m", "nuitka",
    "--standalone",
    "--windows-console-mode=disable",
    "--assume-yes-for-downloads",

    # MinGW on CI — avoids MSVC heap exhaustion on large generated C files.
    # Remove this line to force MSVC locally if you have enough RAM.
    "--mingw64" if ON_CI else "",

    f"--windows-icon-from-ico={icon}" if icon.exists() else "",

    # Compile everything — no frozen-bytecode shortcuts.
    "--follow-import-to=pubstreamer",
    "--include-package=wx",
    "--include-package=numpy",
    "--include-package=pedalboard",
    "--include-package=win32com",
    "--include-package=win32api",
    "--include-package=win32con",
    "--include-package=pywintypes",
    "--include-package=comtypes",
    "--include-package=httpx",
    "--include-package=httpcore",
    "--include-package=certifi",
    "--include-package=pyaudiowpatch",
    "--include-package=edge_tts",
    "--include-package=gtts",
    "--include-package=google",
    "--include-package=grpc",
    "--include-package=pyasn1",
    "--include-package=pyasn1_modules",
    "--include-package=piper",
    "--include-package=onnxruntime",
    "--include-package=soundfile",
    "--include-package=cffi",
    "--include-package=aiohttp",
    "--include-package=aiofiles",

    f"--output-dir={DIST}",
    "--output-filename=PubStreamer",

    *data_dirs,

    "main.py",
]

cmd = [c for c in cmd if c]

print("Running Nuitka...")
print(" ".join(cmd))
print()

result = subprocess.run(cmd, cwd=ROOT)
sys.exit(result.returncode)
