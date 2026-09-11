"""Derives per-ayah timings for a whole-surah recitation.

There is no timing metadata inside an mp3, so boundaries are inferred from the
pauses in the recitation: ffmpeg's silencedetect gives every silent stretch, the
gaps between them are the spoken segments, and those segments map onto the
surah's ayahs in order.

How the detected segments map onto verses depends on how many there are:

  more segments than verses  - the usual case for mujawwad, which pauses
    mid-verse for tajweed and repeats whole verses. Consecutive segments are
    grouped onto verses by the length of each verse's text (a long verse takes
    longer to recite), which is a far better signal than pause length alone.
  as many as verses          - taken as-is.
  fewer segments than verses - the short surahs, recited almost continuously.
    The spoken time is divided across every verse by text length, so each one
    still gets a caption.

The inference is a *draft*, not an answer. Nothing based on silence can tell a
repeated verse from a new one, so every result carries a confidence estimate and
is written as editable JSON. `live sync --show` prints it against the verse text
so a bad boundary is obvious at a glance. Verify before broadcasting.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import quran
from .ffmpeg import duration_of, ffmpeg_bin

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")

# Arabic combining marks: harakat, tanwin, sukun, shadda, superscript alef and
# the Quranic annotation signs Tanzil's Uthmani text carries.
_DIACRITICS = re.compile(r"[ً-ٰٟۖ-ۭـ]")


def _strip_diacritics(text: str) -> str:
    return _DIACRITICS.sub("", text)


def detect_silences(audio: Path, noise_db: int = -35, min_silence: float = 0.6
                    ) -> list[tuple[float, float]]:
    """Every (start, end) silent stretch, in seconds."""
    ff = ffmpeg_bin()
    if not ff:
        return []
    result = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", str(audio),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = result.stderr
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    # A trailing silence has no matching end; pair what we can.
    return list(zip(starts, ends))


def segments_from_silences(silences: list[tuple[float, float]], total: float
                           ) -> list[tuple[float, float]]:
    """Invert the silences into spoken segments spanning the whole track."""
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        segments.append((cursor, total))
    # Drop anything too short to be speech.
    return [(s, e) for s, e in segments if e - s > 0.35]


def align_segments(segments: list[tuple[float, float]], weights: list[float]
                   ) -> list[tuple[float, float]]:
    """Group consecutive segments onto ayahs, guided by how long each ayah is.

    Merging by "shortest pause wins" fails badly on mujawwad recitation, which
    repeats and elongates verses: the extra segments are real pauses, so the
    smallest-gap rule cheerfully merges across a genuine ayah boundary and
    hands a two-word verse half a minute while the longest verse gets seconds.

    Verse length is the far stronger signal -- a long verse takes longer to
    recite. `weights` is each ayah's share of the surah's text, so the expected
    duration of ayah i is that share of the total spoken time. This walks every
    way of cutting the segment list into len(weights) consecutive groups and
    keeps whichever minimises the total deviation from those expectations.
    """
    n_seg, n_ayah = len(segments), len(weights)
    if n_ayah <= 0 or n_seg == 0:
        return []
    if n_seg < n_ayah:
        # Fewer spoken stretches than verses: the short surahs are recited
        # almost continuously, so silencedetect finds two or three pauses for
        # six verses. Returning the segments as-is left the remaining verses
        # with no timing at all -- they simply never appeared on screen. Split
        # the spoken time by verse length instead, which at least gives every
        # verse a caption in roughly the right place.
        return subdivide(segments, weights)
    if n_seg == n_ayah:
        return segments

    speech = sum(end - start for start, end in segments)
    total_weight = sum(weights) or 1.0
    expected = [speech * (w / total_weight) for w in weights]

    # span[i][j] = duration covered by segments i..j-1 taken as one ayah.
    def span(i: int, j: int) -> float:
        return segments[j - 1][1] - segments[i][0]

    INF = float("inf")
    # cost[a][i] = best total deviation assigning ayahs a.. to segments i..
    cost = [[INF] * (n_seg + 1) for _ in range(n_ayah + 1)]
    split = [[0] * (n_seg + 1) for _ in range(n_ayah + 1)]
    cost[n_ayah][n_seg] = 0.0

    for a in range(n_ayah - 1, -1, -1):
        remaining = n_ayah - a
        for i in range(n_seg - remaining, -1, -1):
            best, best_j = INF, i + 1
            # Leave at least one segment for each remaining ayah.
            for j in range(i + 1, n_seg - remaining + 2):
                nxt = cost[a + 1][j]
                if nxt == INF:
                    continue
                total = abs(span(i, j) - expected[a]) + nxt
                if total < best:
                    best, best_j = total, j
            cost[a][i], split[a][i] = best, best_j

    if cost[0][0] == INF:
        return segments[:n_ayah]

    grouped: list[tuple[float, float]] = []
    i = 0
    for a in range(n_ayah):
        j = split[a][i]
        grouped.append((segments[i][0], segments[j - 1][1]))
        i = j
    return grouped


def subdivide(segments: list[tuple[float, float]], weights: list[float]
              ) -> list[tuple[float, float]]:
    """Allocate the spoken time across every ayah, in proportion to its text.

    Silences are skipped rather than divided up: time is handed out only from
    the stretches where something is actually being recited, so a verse never
    starts inside a pause. Boundaries land where the text-length model says
    they should, which is approximate but complete -- and completeness is what
    matters here, since the alternative is verses that never show at all.
    """
    total_weight = sum(weights) or 1.0
    speech = sum(end - start for start, end in segments)
    if speech <= 0:
        return []

    out: list[tuple[float, float]] = []
    index = 0
    seg_start, seg_end = segments[0]
    cursor = seg_start

    for weight in weights:
        need = speech * (weight / total_weight)
        start = cursor
        while need > 0 and index < len(segments):
            available = seg_end - cursor
            if available > need:
                cursor += need
                need = 0
            else:
                need -= available
                index += 1
                if index < len(segments):
                    seg_start, seg_end = segments[index]
                    cursor = seg_start
                else:
                    cursor = seg_end
        out.append((start, max(cursor, start + 0.3)))

    return out


def build_timings(audio: Path, surah: int | None = None, noise_db: int = -35,
                  min_silence: float = 0.6) -> dict:
    """Produce a draft timing table. Returns a report describing what happened."""
    total = duration_of(audio)
    if total is None:
        return {"ok": False, "error": "could not read the audio duration"}

    if surah is None:
        surah = quran.surah_from_filename(audio)
    if surah is None:
        return {"ok": False, "error":
                "could not tell which surah this is -- pass --surah"}

    ayah_count = quran.surah_length(surah)
    if not ayah_count:
        return {"ok": False, "error":
                f"no text for surah {surah} (is data/quran-uthmani.txt present?)"}

    silences = detect_silences(audio, noise_db, min_silence)
    raw = segments_from_silences(silences, total)
    if not raw:
        return {"ok": False, "error": "no speech segments detected"}

    detected = len(raw)
    # Weight each ayah by the length of its text, ignoring the diacritics --
    # they are a large share of the characters in the Uthmani script but take
    # no extra time to recite, so counting them would skew every estimate.
    weights = []
    for index in range(1, ayah_count + 1):
        text = quran.ayah_text(surah, index) or ""
        weights.append(float(len(_strip_diacritics(text))) or 1.0)

    segments = align_segments(raw, weights)

    entries = []
    for index, (start, end) in enumerate(segments[:ayah_count], start=1):
        entries.append({
            "surah": surah, "ayah": index,
            "start": round(start, 3), "end": round(end, 3),
        })

    # Stretch the last entry to the end of the track so the closing verse stays
    # on screen through the final syllable rather than cutting away early.
    if entries:
        entries[-1]["end"] = round(total, 3)

    quran.save_timings(audio, entries)

    # How far each ayah landed from the duration its text predicts. This is the
    # only honest confidence signal available without real forced alignment:
    # a reciter who repeats verses (as mujawwad does) produces more segments
    # than ayahs, and no silence-based method can tell a repeat from a new
    # verse. High deviation means "open the JSON and check", not "broken".
    speech = sum(e["end"] - e["start"] for e in entries) or 1.0
    total_weight = sum(weights) or 1.0
    deviations = []
    for entry, weight in zip(entries, weights):
        actual = entry["end"] - entry["start"]
        expect = speech * (weight / total_weight)
        deviations.append(abs(actual - expect) / max(expect, 0.001))
    worst = max(deviations) if deviations else 0.0
    mean = sum(deviations) / len(deviations) if deviations else 0.0

    return {
        "ok": True,
        "surah": surah,
        "ayahs_expected": ayah_count,
        "segments_detected": detected,
        "segments_used": len(entries),
        "duration": round(total, 2),
        "path": str(quran.timing_path(audio)),
        "grouped": max(0, detected - ayah_count),
        "mean_deviation": round(mean, 2),
        "worst_deviation": round(worst, 2),
        "confidence": "high" if mean < 0.25 else "medium" if mean < 0.6 else "low",
        "suspect_ayahs": [
            entries[i]["ayah"] for i, d in enumerate(deviations) if d > 0.6
        ],
    }
