"""Locating the ffmpeg binaries.

Oracle Linux 9 ships no ffmpeg and enabling EPEL/RPM Fusion would add
third-party package sources to a box that also runs PriceLens and AradoBot, so
a self-contained static build inside the project is preferred and looked for
first. A system ffmpeg is still honoured if one is present.
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "ffmpeg"


def _find(name: str) -> str | None:
    local = VENDOR / name
    if local.is_file():
        return str(local)
    return shutil.which(name)


def ffmpeg_bin() -> str | None:
    return _find("ffmpeg")


def ffprobe_bin() -> str | None:
    return _find("ffprobe")


def duration_of(path: Path) -> float | None:
    """Media duration in seconds, or None if it cannot be determined."""
    import subprocess
    probe = ffprobe_bin()
    if not probe:
        return None
    result = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None
