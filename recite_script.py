"""Derive what was actually recited, in order, from the audio itself.

The timings this replaces were inferred from silence: the gaps in a recitation
were counted and the surah's verses handed out across them in order. That holds
only if the reciter reads each verse once, in sequence. Mujawwad recitation does
not work that way -- a verse is repeated, two are joined under one breath, the
Basmala opens a passage again in the middle of a surah -- so a verse that was
read three times was given one long span and every verse after it in that surah
was pushed out of step with the audio. Measured across the library, that is 3.5
hours of the caption on screen not being the words being recited.

Splitting the audio per verse was the other idea and it does not survive
contact: the reverb of one verse runs past the silence into the next, so a cut
at the gap leaves the tail of the previous verse on the front of the next file.

So the audio is transcribed and the transcript is matched back to the mushaf.
Neither half works alone. The transcript knows the *order* -- which verse was
read, how many times, in what sequence -- and nothing in the written text can
supply that. The mushaf knows the *words*: the transcript of a melodic Quranic
reading is full of errors ("lillahi wasfu mad" for "Allahu as-samad"), but the
candidates it is matched against are the handful of verses of one surah, not
the whole language, and at that narrowness a rough transcript is decisive.

The output is the same shape the pipeline already reads, so a repeated verse is
simply the same verse appearing more than once in the list.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import quran
from engine.ffmpeg import ffmpeg_bin

WHISPER = Path("/tmp/whisper.cpp/build/bin/whisper-cli")
MODEL = Path("/tmp/whisper.cpp/models/ggml-small.bin")

# How far from the last matched verse the next one may be. Repetition in
# recitation is local -- a reciter goes back a verse or two, never twenty -- and
# without this bound every verse of al-Baqarah would be scored against every
# word of it, which is hours of comparison for no gain in accuracy.
WINDOW = 8

# Below this the words on hand do not credibly spell any nearby verse; the word
# is dropped rather than forced onto whichever verse scored least badly.
MIN_SCORE = 0.52

_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def normalise(text: str) -> str:
    """Reduce a verse or a transcript to the letters they can be compared on.

    The mushaf text carries full vocalisation and Quranic annotation; a
    transcript carries none of it, spells the hamza however the model felt, and
    writes alif maqsura for ya. Comparing the two as written would find almost
    nothing in common.
    """
    text = _MARKS.sub("", text)
    for src, dst in (("أإآٱ", "ا"), ("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        for ch in src:
            text = text.replace(ch, dst)
    return re.sub(r"[^ء-ي\s]", " ", text)


def words_of(text: str) -> list[str]:
    return normalise(text).split()


def transcribe(audio: Path, model: Path, threads: int) -> list[tuple[str, float, float]]:
    """(word, start, end) for every word heard, in the order it was heard."""
    ff = ffmpeg_bin()
    wav = Path("/tmp") / f"asr-{audio.stem}.wav"
    subprocess.run([ff, "-y", "-v", "error", "-i", str(audio),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   check=True)
    out = Path("/tmp") / f"asr-{audio.stem}"
    subprocess.run([str(WHISPER), "-m", str(model), "-l", "ar", "-t", str(threads),
                    "-f", str(wav), "-ojf", "-of", str(out)],
                   check=True, capture_output=True)
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    wav.unlink(missing_ok=True)

    # whisper emits sub-word pieces; a piece that begins with a space begins a
    # new word, which is how the pieces are stitched back into words that can
    # be compared with the mushaf.
    words: list[tuple[str, float, float]] = []
    for segment in data.get("transcription", []):
        for token in segment.get("tokens", []):
            raw = token.get("text", "")
            if raw.startswith("[") and raw.endswith("]"):
                continue
            start = token["offsets"]["from"] / 1000.0
            end = token["offsets"]["to"] / 1000.0
            if raw.startswith(" ") or not words:
                piece = normalise(raw).strip()
                if piece:
                    words.append([piece, start, end])
            else:
                piece = normalise(raw).strip()
                if piece and words:
                    words[-1][0] += piece
                    words[-1][2] = end
    return [(w, s, e) for w, s, e in words if w]


def candidates(surah: int, count: int) -> list[tuple[int, list[str]]]:
    """(ayah number, words) for the surah, plus the Basmala as ayah 0.

    Every surah but at-Tawbah is opened with the Basmala, and a reciter often
    returns to it mid-surah when starting a passage again. It is not a verse of
    those surahs, so it is carried as ayah 0 and dropped from the result: it
    exists only so its words are accounted for instead of being mistaken for
    the opening of the verse that follows.
    """
    out = [(0, words_of(quran.ayah_text(1, 1) or ""))]
    for n in range(1, count + 1):
        # display_text is used rather than ayah_text because the mushaf carries
        # the Basmala inside verse 1 of most surahs, and display_text is
        # already the code that knows where it ends. Left in, verse 1 of
        # al-Ikhlas would be ten words to match against the four the reciter
        # says after the Basmala, and it would lose to a shorter verse that
        # happened to fit -- the opening verse of the surah, missing.
        text = quran.display_text(surah, n)
        if text:
            out.append((n, words_of(text.split("\n")[-1])))
    return out


def score(heard: list[str], expected: list[str]) -> float:
    """How well the words heard spell the words expected, letter by letter.

    Comparing the two as lists of words finds only exact word matches, and a
    transcript of melodic recitation rarely produces one: "Allahu as-samad"
    comes back as "lillahi wasfu mad", three words against two with nothing in
    common, scoring zero. The same pair compared as letters is almost the same
    string. Short verses depend entirely on this -- a two-word verse has no
    room to survive one misheard word -- and they are exactly the verses a
    reciter repeats.
    """
    return SequenceMatcher(None, "".join(heard), "".join(expected)).ratio()


def build_sequence(words, cands, surah):
    """Walk the transcript, claiming the nearest verse that explains it."""
    by_ayah = {n: w for n, w in cands}
    order = [n for n, _ in cands if n]
    sequence = []
    pending_basmala = None
    i = 0
    last = 1
    while i < len(words):
        heard = [w for w, _, _ in words]
        # Only verses near the last one matched, plus the Basmala.
        nearby = [n for n in order if abs(n - last) <= WINDOW]
        best = (0.0, 0, None)
        for n in [0] + nearby:
            expected = by_ayah[n]
            if not expected:
                continue
            # The transcript runs long or short against the written verse, so a
            # few lengths around it are tried rather than assuming they match
            # word for word.
            for span in range(max(1, len(expected) - 2), len(expected) + 3):
                segment = heard[i:i + span]
                if not segment:
                    continue
                value = score(segment, expected)
                if value > best[0]:
                    best = (value, span, n)
        value, span, n = best
        if n is None or value < MIN_SCORE:
            i += 1
            continue
        start = words[i][1]
        end = words[min(i + span, len(words)) - 1][2]
        if n:
            # The Basmala that opened this passage belongs on screen with the
            # verse that follows it: the mushaf carries it at the head of verse
            # 1 and the caption already renders it there on its own line. Left
            # unclaimed it would be recited to an empty screen -- and in a
            # mujawwad reading that happens every time the reciter goes back to
            # the beginning, which is often. It is only folded in ahead of
            # verse 1, which is the only verse whose text contains it.
            if pending_basmala is not None and n == 1:
                start = pending_basmala
            pending_basmala = None
            sequence.append({"surah": surah, "ayah": n,
                             "start": round(start, 3), "end": round(end, 3),
                             "confidence": round(value, 3)})
            last = n
        else:
            pending_basmala = start
        i += span
    return sequence


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", type=Path)
    ap.add_argument("--model", type=Path, default=MODEL)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    surah = quran.surah_from_filename(args.audio)
    if not surah:
        print("cannot tell which surah this file is", file=sys.stderr)
        return 1
    count = sum(1 for n in range(1, 300) if quran.ayah_text(surah, n))

    words = transcribe(args.audio, args.model, args.threads)
    print(f"سورة {surah}: {count} آية | {len(words)} كلمة مسموعة", file=sys.stderr)
    sequence = build_sequence(words, candidates(surah, count), surah)

    payload = {"audio": args.audio.name, "ayahs": sequence}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"كُتب: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
