"""Quran text lookup, and the per-ayah timing that drives the on-screen caption.

Text comes from a Tanzil plain-text file (`surah|ayah|text` per line) kept in
data/. Timings are per audio file, because they describe one particular
recitation of a surah, not the surah itself.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_FILE = ROOT / "data" / "quran-uthmani.txt"
TIMINGS_DIR = ROOT / "data" / "timings"

SURAH_NAMES = {}  # filled lazily from data/surah-names.json when present


@lru_cache(maxsize=1)
def load_text() -> dict[tuple[int, int], str]:
    """{(surah, ayah): text}. Empty if the text file has not been fetched."""
    if not TEXT_FILE.exists():
        return {}
    verses: dict[tuple[int, int], str] = {}
    for line in TEXT_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Tanzil appends licence/comment lines after the verses; skip anything
        # that is not exactly surah|ayah|text.
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        verses[(int(parts[0]), int(parts[1]))] = parts[2].strip()
    return verses


def ayah_text(surah: int, ayah: int) -> str | None:
    return load_text().get((surah, ayah))


def surah_length(surah: int) -> int:
    return sum(1 for (s, _a) in load_text() if s == surah)


@lru_cache(maxsize=1)
def surah_names() -> dict[int, str]:
    path = ROOT / "data" / "surah-names.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


def surah_from_filename(path: Path) -> int | None:
    """Infer the surah number from a file name such as `001.mp3` or `surah-018.mp3`.

    Deliberately conservative: a wrong guess would caption a recitation with
    another surah's verses, so anything ambiguous returns None and the channel
    falls back to its static caption.
    """
    digits = re.findall(r"\d{1,3}", path.stem)
    for group in digits:
        value = int(group)
        if 1 <= value <= 114:
            return value
    return None


def timing_path(audio: Path) -> Path:
    return TIMINGS_DIR / f"{audio.stem}.json"


def load_timings(audio: Path) -> list[dict] | None:
    """[{'surah':n,'ayah':n,'start':float,'end':float}, ...] ordered by start."""
    path = timing_path(audio)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("ayahs") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return None
    cleaned = []
    for item in entries:
        try:
            cleaned.append({
                "surah": int(item["surah"]),
                "ayah": int(item["ayah"]),
                "start": float(item["start"]),
                "end": float(item["end"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    cleaned.sort(key=lambda e: e["start"])
    return cleaned or None


def save_timings(audio: Path, entries: list[dict]) -> Path:
    TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = timing_path(audio)
    path.write_text(
        json.dumps({"audio": audio.name, "ayahs": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# Tanzil's Uthmani edition prepends the Basmala to the first verse of every
# surah except at-Tawbah (9), which has none, and al-Fatihah (1), where it is
# verse 1 in its own right. Textually correct, but run together on one line on
# screen it reads as though the Basmala were part of the verse.

_MARKS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")


def _bare(text: str) -> str:
    """Letters only -- diacritics removed, spacing normalised."""
    return re.sub(r"\s+", " ", _MARKS.sub("", text)).strip()


@lru_cache(maxsize=1)
def _basmala_bare() -> str:
    """The Basmala, taken from the data rather than hard-coded.

    Al-Fatihah's first verse *is* the Basmala, so the text file is its own
    reference. Comparing on letters alone avoids depending on the order the
    combining marks happen to be encoded in, which is what made an
    escape-sequence constant fail to match.
    """
    return _bare(ayah_text(1, 1) or "")


def display_text(surah: int, ayah: int) -> str | None:
    """The verse as it should appear on screen.

    Identical to ayah_text except that a Basmala carried at the head of verse 1
    is put on its own line, so the verse itself starts cleanly beneath it.
    """
    text = ayah_text(surah, ayah)
    if not text or ayah != 1 or surah in (1, 9):
        return text

    basmala = _basmala_bare()
    if not basmala or not _bare(text).startswith(basmala):
        return text

    # Walk the original until as many letters as the Basmala has are consumed,
    # so the cut lands after its final letter with its marks intact.
    target = len(basmala.replace(" ", ""))
    seen = 0
    for index, char in enumerate(text):
        if not _MARKS.match(char) and not char.isspace():
            seen += 1
            if seen == target:
                # The marks belonging to that letter follow it, so the cut has
                # to move past them -- otherwise the Basmala's closing kasra is
                # orphaned onto the start of the next line.
                end = index + 1
                while end < len(text) and _MARKS.match(text[end]):
                    end += 1
                head, rest = text[:end], text[end:].strip()
                return f"{head}\n{rest}" if rest else text
    return text
