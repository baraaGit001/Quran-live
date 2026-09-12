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


def span_of(path: Path) -> tuple[float, float] | None:
    """(first timestamp, last timestamp) of a media file, in seconds.

    `duration_of` is not enough to place one track after another. A rendered
    track does not start at zero -- the muxer writes an initial offset -- and
    its audio usually runs a few milliseconds past its video, so the real end
    is later than the reported duration. Joining on duration alone leaves the
    next track's audio overlapping the previous one's by that margin, which is
    a non-monotonic DTS to anything reading the result.

    The end is therefore taken from the last packets themselves. Only the tail
    of the file is read (-read_intervals), so this costs the same on a two-hour
    surah as it does on al-Fatiha.
    """
    import subprocess
    probe = ffprobe_bin()
    if not probe:
        return None
    head = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=start_time",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        start = float(head.stdout.strip())
    except ValueError:
        return None
    tail = subprocess.run(
        [probe, "-v", "error", "-read_intervals", "-99%",
         "-show_entries", "packet=dts_time,duration_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    end = 0.0
    for line in tail.stdout.splitlines():
        parts = [x for x in line.strip().split(",") if x and x != "N/A"]
        if len(parts) < 2:
            continue
        try:
            end = max(end, float(parts[0]) + float(parts[1]))
        except ValueError:
            pass
    if end <= start:
        return None
    return start, end
