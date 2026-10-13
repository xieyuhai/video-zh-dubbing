#!/usr/bin/env python3
"""Check the local prerequisites for video-zh-dubbing."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

REQUIRED_MODULES = {
    "faster_whisper": "faster-whisper",
    "edge_tts": "edge-tts",
    "yt_dlp": "yt-dlp",
    "pydub": "pydub",
    "requests": "requests",
    "deep_translator": "deep-translator",
    "dotenv": "python-dotenv",
}


def main() -> int:
    failures = []
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 10):
        failures.append("Python 3.10+ is required")
    for command in ("ffmpeg", "ffprobe", "curl"):
        path = shutil.which(command)
        print(f"{command}: {path or 'MISSING'}")
        if not path:
            failures.append(f"{command} is missing from PATH")
    for module, package in REQUIRED_MODULES.items():
        present = importlib.util.find_spec(module) is not None
        print(f"{package}: {'OK' if present else 'MISSING'}")
        if not present:
            failures.append(f"Install Python dependency: {package}")
    has_key = bool(os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))
    print(f"External API translation: {'configured' if has_key else 'not configured'}")
    ollama = shutil.which("ollama")
    print(f"Local Ollama translation: {ollama or 'not installed'} (model: {os.getenv('OLLAMA_MODEL') or 'not configured'})")
    try:
        print(f"Optional Demucs: {'installed' if importlib.util.find_spec('demucs') else 'not installed (background mode will fall back)'}")
    except Exception:
        pass
    if failures:
        print("\nEnvironment check FAILED:")
        for item in failures:
            print(f"- {item}")
        return 1
    print("\nEnvironment check passed. Whisper model weights are downloaded on first transcription.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
