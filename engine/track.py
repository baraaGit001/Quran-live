"""Renders one recitation into a finished, self-contained video.

Showing the verse currently being recited means the picture changes during
playback, which normally forces a live H.264 encode and puts five channels well
past what two cores can do. This sidesteps that: each track is encoded ONCE,
captions already burned in at the right moments, muxed with its own audio. The
24/7 stream then plays a sequence of these with -c copy, so the recurring cost
stays near zero and the text is still exactly in sync.

The captions are supplied to ffmpeg as a single extra input -- a concat list of
per-ayah PNGs, each held for that ayah's duration -- rather than one overlay
filter per ayah. A filtergraph with 286 overlay stages (al-Baqarah) would be
unusable; this is one overlay stage no matter how long the surah.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from . import overlay as overlay_mod
from . import quran
from .ffmpeg import duration_of, ffmpeg_bin
from .prerender import _fit_chain, resolve_fit

ROOT = Path(__file__).resolve().parent.parent
TRACK_DIR = ROOT / "cache" / "tracks"
CAPTION_DIR = ROOT / "cache" / "captions"


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _wrap(text: str, max_chars: int) -> str:
    """Break a long verse across lines on word boundaries.

    Line breaks already present in the text (the Basmala split off from verse
    one) are preserved -- each line is wrapped on its own.
    """
    if "\n" in text:
        return "\n".join(_wrap(line, max_chars) for line in text.split("\n"))
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def build_captions(cfg: dict, audio: Path, entries: list[dict]) -> Path | None:
    """A concat list of caption PNGs, one per ayah, timed to the recitation.

    Gaps between ayahs get a fully transparent frame rather than being skipped,
    so the previous verse does not linger on screen through the pause before
    the next one.
    """
    ov = cfg.get("overlay", {}) or {}
    font = cfg.get("font", "")
    size = ov.get("font_size", 54)
    colour = ov.get("color", "#FFFFFF")
    wrap_at = int(ov.get("wrap_chars", 42))

    # The concat demuxer requires every image in the list to share one size --
    # feeding it captions of differing sizes makes it stop producing frames
    # after the first, which looks exactly like "the text never appears". So
    # every caption is padded onto one fixed canvas: the full video width by a
    # band tall enough for the longest verse.
    band_w = int(cfg["video"]["width"])

    key = _hash(str(audio), str(font), str(size), colour, str(wrap_at),
                str(len(entries)), str(band_w))
    out_dir = CAPTION_DIR / key
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = out_dir / "list.txt"
    ff = ffmpeg_bin()

    def _size_of(path: Path) -> tuple[int, int] | None:
        from .ffmpeg import ffprobe_bin
        probe = ffprobe_bin()
        if not probe:
            return None
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True,
        )
        try:
            width, height = result.stdout.strip().split(",")[:2]
            return int(width), int(height)
        except ValueError:
            return None

    # Pass 1: render every verse and find the tallest. The band cannot be
    # guessed from the font size -- a verse that wraps to three lines is several
    # times taller than one that fits on a line, and padding an image that is
    # already bigger than the canvas fails outright.
    raws: list[tuple[int, Path, dict]] = []
    tallest = 0
    for index, entry in enumerate(entries):
        text = quran.display_text(entry["surah"], entry["ayah"])
        if not text:
            continue
        raw = out_dir / f"{index:04d}.raw.png"
        if not raw.exists():
            if not overlay_mod.render(_wrap(text, wrap_at), font, size, colour, raw):
                return None
        dims = _size_of(raw)
        if not dims:
            return None
        tallest = max(tallest, dims[1])
        raws.append((index, raw, entry))

    if not raws:
        return None
    band_h = tallest + 24  # a little breathing room above and below

    blank = out_dir / "blank.png"
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=black@0.0:s={band_w}x{band_h},format=rgba",
         "-frames:v", "1", str(blank)], capture_output=True,
    )

    # Pass 2: centre each verse on the shared canvas.
    #
    # The scale is capped at the image's own size. Left to fit the band,
    # force_original_aspect_ratio=decrease happily enlarges a small image too,
    # so a two-word verse would be blown up to the full band and tower over a
    # long one -- every verse a different size on screen. Capping means the
    # rendered font size is what decides how big text looks, uniformly, and
    # scaling only ever kicks in to rescue a verse too wide for the frame.
    lines: list[str] = []
    cursor = 0.0
    for index, raw, entry in raws:
        png = out_dir / f"{index:04d}.png"
        if not png.exists():
            ok = subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                 "-filter_complex",
                 f"[0:v]format=rgba,"
                 f"scale='min(iw,{band_w})':'min(ih,{band_h})'"
                 f":force_original_aspect_ratio=decrease,"
                 f"pad={band_w}:{band_h}:(ow-iw)/2:(oh-ih)/2:color=#00000000[o]",
                 "-map", "[o]", "-frames:v", "1", str(png)],
                capture_output=True,
            ).returncode == 0 and png.exists()
            if not ok:
                return None
        raw.unlink(missing_ok=True)

        if entry["start"] > cursor + 0.05:
            lines.append(f"file '{blank.as_posix()}'")
            lines.append(f"duration {entry['start'] - cursor:.3f}")
        lines.append(f"file '{png.as_posix()}'")
        lines.append(f"duration {max(0.1, entry['end'] - entry['start']):.3f}")
        cursor = entry["end"]

    if not lines:
        return None
    # The concat demuxer ignores the final entry's duration unless the file is
    # repeated, so the last image is listed twice.
    lines.append(lines[-2])
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def track_key(cfg: dict, audio: Path, background: Path) -> str | None:
    """The cache key for one (recitation, background) pair, without building it.

    Split out from build_track so the streamer can ask "is this already
    rendered?" cheaply. Rendering takes longer than the track itself plays, so
    discovering that mid-stream -- with ffmpeg already waiting on the pipe --
    would stall the broadcast.
    """
    try:
        mtime = audio.stat().st_mtime_ns
    except OSError:
        return None
    entries = quran.load_timings(audio)
    cfg = resolve_fit(cfg, background)
    v = cfg["video"]
    return _hash(
        str(audio), str(mtime), str(background),
        str(v["width"]), str(v["height"]), str(v["fps"]), str(v["bitrate"]),
        str(v.get("fit", "blur")), str(cfg.get("font", "")),
        str((cfg.get("overlay") or {}).get("font_size", 54)),
        str(len(entries or [])),
    )


def existing_track(cfg: dict, audio: Path, background: Path) -> Path | None:
    """The rendered file for this pair if it is already on disk, else None."""
    key = track_key(cfg, audio, background)
    if not key:
        return None
    candidate = TRACK_DIR / f"{key}.ts"
    return candidate if candidate.exists() else None


def build_track(cfg: dict, audio: Path, background: Path, is_image: bool) -> Path | None:
    """Render `audio` + `background` + timed verses into one playable file."""
    ff = ffmpeg_bin()
    if not ff:
        return None

    entries = quran.load_timings(audio)
    duration = duration_of(audio)
    if duration is None:
        return None

    cfg = resolve_fit(cfg, background)
    v = cfg["video"]
    a = cfg["audio"]
    key = _hash(
        str(audio), str(audio.stat().st_mtime_ns), str(background),
        str(v["width"]), str(v["height"]), str(v["fps"]), str(v["bitrate"]),
        str(v.get("fit", "blur")), str(cfg.get("font", "")),
        str((cfg.get("overlay") or {}).get("font_size", 54)),
        str(len(entries or [])),
    )
    # Rendered straight to MPEG-TS, which is the form the streamer feeds into
    # ffmpeg. Going through mp4 first and remuxing would keep two copies of
    # every track on disk -- ~220GB for the full mujawwad Quran instead of
    # ~110GB, on a box with 145GB free.
    out = TRACK_DIR / f"{key}.ts"
    if out.exists():
        return out
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    # Rendered to a side path and renamed into place only once ffmpeg has
    # succeeded. A file at the final path is therefore always complete: a
    # render interrupted by a restart or a reboot leaves a .partial behind
    # rather than a truncated mp4 that later looks ready and fails to play.
    partial = TRACK_DIR / f"{key}.partial.ts"

    captions = None
    if entries:
        captions = build_captions(cfg, audio, entries)
        if captions is None:
            # Falling back to a bare background here would quietly publish a
            # track with no verses on it -- the one thing this whole path
            # exists to produce. Fail loudly instead.
            raise RuntimeError(
                f"timings exist for {audio.name} but the captions could not be "
                f"rendered; refusing to build a track with no verse text"
            )

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    if is_image:
        cmd += ["-loop", "1"]
    else:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(background), "-i", str(audio)]

    margin = (cfg.get("overlay") or {}).get("margin", 70)
    position = (cfg.get("overlay") or {}).get("position", "bottom")
    y = f"H-h-{margin}" if position == "bottom" else str(margin)

    if captions:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(captions)]
        chain = (
            f"[0:v]{_fit_chain(cfg)},fps={v['fps']},format=yuv420p[bg];"
            f"[2:v]format=rgba[cap];"
            f"[bg][cap]overlay=(W-w)/2:{y}:shortest=0:format=auto[vout]"
        )
    else:
        chain = f"[0:v]{_fit_chain(cfg)},fps={v['fps']},format=yuv420p[vout]"

    gop = int(v["fps"]) * int(v.get("keyframe_interval", 2))
    cmd += [
        "-filter_complex", chain,
        "-map", "[vout]", "-map", "1:a:0",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", str(v["bitrate"]), "-maxrate", str(v["bitrate"]),
        "-bufsize", f"{int(str(v['bitrate']).rstrip('k')) * 2}k",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", str(a["bitrate"]), "-ar", str(a["sample_rate"]),
        "-ac", str(a["channels"]),
        "-f", "mpegts",
        str(partial),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not partial.exists():
        partial.unlink(missing_ok=True)
        return None
    os.replace(partial, out)
    return out
