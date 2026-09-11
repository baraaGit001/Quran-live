"""Keeps rendered tracks from filling the disk.

The full mujawwad Quran is ~81 hours of recitation. Rendered at 2 Mbps that is
~73 GB of video, on a box that also runs two web applications and their
databases -- so the cache needs a hard ceiling, not good intentions. Tracks are
evicted least-recently-used first; an evicted track is simply re-rendered if it
comes up again, so eviction costs CPU later but can never cost a full disk now.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACK_DIR = ROOT / "cache" / "tracks"


def finished_tracks() -> list[Path]:
    """Every fully rendered track.

    Deliberately excludes the .partial.ts files a render writes before its
    atomic rename: a plain *.ts glob matches those too, so eviction could
    delete a track that is still being written -- destroying hours of encoding
    and leaving a half-file behind.
    """
    if not TRACK_DIR.exists():
        return []
    return [p for p in TRACK_DIR.glob("*.ts")
            if p.is_file() and not p.name.endswith(".partial.ts")]


def cache_bytes() -> int:
    return sum(p.stat().st_size for p in finished_tracks())


def free_bytes() -> int:
    import shutil
    return shutil.disk_usage(ROOT).free


def enforce(max_gb: float, keep: set[Path] | None = None,
            min_free_gb: float = 10.0) -> list[Path]:
    """Evict tracks until the cache is under `max_gb` and the disk has headroom.

    `keep` is whatever must not be deleted -- above all the track currently
    being streamed, which would otherwise be a candidate the moment the cache
    goes over budget.
    """
    if not TRACK_DIR.exists():
        return []
    protected = {p.resolve() for p in (keep or set())}
    limit = int(max_gb * 1024 ** 3)
    floor = int(min_free_gb * 1024 ** 3)

    files = finished_tracks()
    # Least recently *used*, not least recently created: a track played often
    # should survive, and playing it updates atime.
    files.sort(key=lambda p: p.stat().st_atime)

    evicted: list[Path] = []
    total = sum(p.stat().st_size for p in files)
    for path in files:
        if total <= limit and free_bytes() >= floor:
            break
        if path.resolve() in protected:
            continue
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        evicted.append(path)
    return evicted


def projected_gb(seconds: float, bitrate_kbps: int) -> float:
    """How much disk a given amount of recitation will occupy once rendered."""
    return seconds * bitrate_kbps * 1000 / 8 / 1024 ** 3
