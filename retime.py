"""Replace silence-inferred timings with ones taken from the recitation.

Runs for hours, on the same two cores that encode the tracks and feed the
stream, so it is deliberately unhurried: one surah at a time at the lowest
priority the scheduler offers, and the previous timings are kept rather than
overwritten. A surah whose result looks worse than what it replaces can be put
back from data/timings/_silence without re-deriving anything.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import config as cfg_mod, quran
from engine.stream import ChannelStreamer

TIMINGS = ROOT / "data" / "timings"
BACKUP = TIMINGS / "_silence"

# Surahs whose current timings carry a verse far longer than the surah's own
# rate -- the fingerprint of a repetition the silence pass could not see.
# Shortest first, so the worst of them are corrected within the hour rather
# than behind an hour of al-Kahf.
WORST = [108, 114, 107, 1, 94, 104, 87, 85, 71, 60, 53, 18]


def suspect_count(path: Path) -> int:
    import statistics
    try:
        ayahs = json.loads(path.read_text(encoding="utf-8"))["ayahs"]
    except (OSError, ValueError, KeyError):
        return 0
    spans = [a["end"] - a["start"] for a in ayahs]
    if len(spans) < 2:
        return 0
    median = statistics.median(spans)
    return sum(1 for x in spans if median > 0 and x > 2.2 * median) if median else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("surahs", nargs="*", type=int,
                    help="surah numbers; default is the ones that look wrong")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    cfg = ChannelStreamer("channel01").cfg
    by_surah = {}
    for audio in cfg_mod.audio_pool(cfg):
        n = quran.surah_from_filename(audio)
        if n:
            by_surah[n] = audio

    if args.surahs:
        wanted = args.surahs
    elif args.all:
        wanted = sorted(by_surah)
    else:
        wanted = WORST

    BACKUP.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for i, n in enumerate(wanted, 1):
        audio = by_surah.get(n)
        if not audio:
            print(f"[{i}/{len(wanted)}] {n}: لا يوجد ملف صوت", flush=True)
            continue
        target = TIMINGS / f"{n:03d}.json"
        before = suspect_count(target)
        out = Path("/tmp") / f"retime-{n:03d}.json"
        print(f"[{i}/{len(wanted)}] سورة {n} ({before} آية مشبوهة) ...", flush=True)

        result = subprocess.run(
            ["nice", "-n", "19", sys.executable, "recite_script.py",
             str(audio), "--out", str(out)],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if result.returncode != 0 or not out.exists():
            print(f"    فشل: {result.stderr.strip()[:200]}", flush=True)
            continue

        try:
            new = json.loads(out.read_text(encoding="utf-8"))["ayahs"]
        except (OSError, ValueError, KeyError):
            print("    ناتج غير صالح", flush=True)
            continue
        # A surah that comes back with verses missing is not an improvement on
        # timings that at least name every verse once, so it is left alone.
        total = sum(1 for k in range(1, 300) if quran.ayah_text(n, k))
        seen = {e["ayah"] for e in new}
        if len(seen) < total:
            missing = sorted(set(range(1, total + 1)) - seen)
            print(f"    مرفوض: {len(missing)} آية لم تُسمع {missing[:8]}", flush=True)
            continue

        if target.exists() and not (BACKUP / target.name).exists():
            shutil.copy2(target, BACKUP / target.name)
        shutil.move(str(out), str(target))
        print(f"    ✓ {len(new)} مقطع (كان {total})   "
              f"[{(time.time() - t0) / 60:.0f} دقيقة]", flush=True)

    print(f"انتهى في {(time.time() - t0) / 60:.0f} دقيقة", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
