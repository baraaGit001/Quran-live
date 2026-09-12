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
import random
import subprocess
from pathlib import Path

from . import config as cfg_mod
from . import overlay as overlay_mod
from . import quran
from .ffmpeg import duration_of, ffmpeg_bin
from .prerender import _fit_chain, resolve_fit

ROOT = Path(__file__).resolve().parent.parent
TRACK_DIR = ROOT / "cache" / "tracks"
CAPTION_DIR = ROOT / "cache" / "captions"
BG_DIR = ROOT / "cache" / "backgrounds"
SEGMENT_DIR = ROOT / "cache" / "segments"


# Opacity steps a verse passes through on its way in and out.
#
# The verses used to cut straight from nothing to fully lit and back, which is
# the one thing on screen that moves -- a hard switch under a still background
# reads as a glitch rather than as the text arriving. A fade is the obvious
# fix, and the obvious way to get one is a time-varying alpha in the filter
# graph. That would mean geq, per pixel per frame, for the length of every
# recitation, on a box with two cores and no hardware encoder.
#
# So the ramp is baked into the caption timeline instead. Each verse is
# rendered once at each opacity and the concat list simply holds the dim copies
# briefly on the way in and on the way out. The fade costs disk and nothing at
# all at render time: it is still one concat of stills, exactly as before.
FADE_LEVELS = (0.18, 0.42, 0.68, 0.88)

# Long enough to read as a fade rather than a flicker, short enough that a
# verse is never late. Both ends together are capped at a fraction of the
# ayah so a two-second verse is not all ramp and no text.
FADE_SECONDS = 0.36
FADE_MIN_HOLD = 0.40


def _fade_for(duration: float) -> float:
    """How long each ramp may be for an ayah of this length."""
    room = (duration - FADE_MIN_HOLD) / 2
    fade = min(FADE_SECONDS, max(0.0, room))
    return fade if fade >= 0.08 else 0.0


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

    # The band is overlaid at `H - band_h - margin`, so a band taller than the
    # frame less its margin lands at a negative y and the top of the verse is
    # cut off by the frame edge -- the tallest verse of a surah decides this,
    # so one long ayah truncates every long ayah in the track. Cap the band at
    # what can actually be shown; pass 2 then scales a verse too tall for the
    # cap down into it, the same rescue it already does for one too wide.
    margin = int((cfg.get("overlay") or {}).get("margin", 70))
    max_band_h = max(1, int(cfg["video"]["height"]) - margin)

    key = _hash(str(audio), str(font), str(size), colour, str(wrap_at),
                str(len(entries)), str(band_w), str(max_band_h),
                # The ramp changes what is on screen, so it has to change the
                # key -- otherwise every caption set already on disk would be
                # reused unfaded and the change would appear to do nothing.
                "fade:" + ",".join(str(x) for x in FADE_LEVELS),
                f"{FADE_SECONDS}:{FADE_MIN_HOLD}")
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
    band_h = min(tallest + 24, max_band_h)  # breathing room, but never off-frame

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
    def _variants(index: int) -> dict[str, Path]:
        """Paths of the full-opacity verse and each of its dimmed copies."""
        out = {"full": out_dir / f"{index:04d}.png"}
        for level in FADE_LEVELS:
            out[level] = out_dir / f"{index:04d}.a{int(level * 100):02d}.png"
        return out

    lines: list[str] = []
    cursor = 0.0
    for index, raw, entry in raws:
        paths = _variants(index)
        if not all(q.exists() for q in paths.values()):
            # One ffmpeg per verse, not one per opacity: the source is decoded
            # and fitted once and the graph splits it into every copy needed.
            # Rendering the levels separately would multiply the number of
            # processes by five across six thousand verses.
            fit = (f"[0:v]format=rgba,"
                   f"scale='min(iw,{band_w})':'min(ih,{band_h})'"
                   f":force_original_aspect_ratio=decrease,"
                   f"pad={band_w}:{band_h}:(ow-iw)/2:(oh-ih)/2:color=#00000000,"
                   f"split={len(FADE_LEVELS) + 1}[full]")
            graph = [fit + "".join(f"[s{i}]" for i in range(len(FADE_LEVELS)))]
            outputs = ["-map", "[full]", "-frames:v", "1", str(paths["full"])]
            for i, level in enumerate(FADE_LEVELS):
                graph.append(f"[s{i}]colorchannelmixer=aa={level}[o{i}]")
                outputs += ["-map", f"[o{i}]", "-frames:v", "1", str(paths[level])]
            ok = subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                 "-filter_complex", ";".join(graph)] + outputs,
                capture_output=True,
            ).returncode == 0 and all(q.exists() for q in paths.values())
            if not ok:
                return None
        raw.unlink(missing_ok=True)

        if entry["start"] > cursor + 0.05:
            lines.append(f"file '{blank.as_posix()}'")
            lines.append(f"duration {entry['start'] - cursor:.3f}")

        span = max(0.1, entry["end"] - entry["start"])
        fade = _fade_for(span)
        if fade:
            step = fade / len(FADE_LEVELS)
            for level in FADE_LEVELS:
                lines.append(f"file '{paths[level].as_posix()}'")
                lines.append(f"duration {step:.3f}")
        lines.append(f"file '{paths['full'].as_posix()}'")
        lines.append(f"duration {span - 2 * fade:.3f}")
        if fade:
            step = fade / len(FADE_LEVELS)
            for level in reversed(FADE_LEVELS):
                lines.append(f"file '{paths[level].as_posix()}'")
                lines.append(f"duration {step:.3f}")
        cursor = entry["end"]

    if not lines:
        return None
    # The concat demuxer ignores the last entry's duration unless the file is
    # repeated, so the list ends on a transparent frame listed twice -- the
    # verse fades to nothing rather than the final image being held.
    lines.append(f"file '{blank.as_posix()}'")
    lines.append("duration 0.500")
    lines.append(f"file '{blank.as_posix()}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def _rotation_seconds(cfg: dict) -> int:
    """How long one still holds before the next, or 0 to hold it all track."""
    try:
        return max(0, int(cfg["video"].get("visual_rotate_seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _pool_signature(cfg: dict) -> str:
    """Names of the still pool, so adding or removing one re-keys the cache."""
    images, _ = cfg_mod.visual_pool(cfg)
    return ",".join(sorted(q.name for q in images))


def segment_pool() -> list[Path]:
    """Every rotation unit on disk, in a stable order.

    Segments are built offline by build_segments.py: one per still, and one per
    slice of each source video. They all share codec parameters, which is what
    lets the concat demuxer hand them to the filter graph as one stream.
    """
    if not SEGMENT_DIR.is_dir():
        return []
    return sorted(SEGMENT_DIR.glob("*.ts"), key=lambda q: q.name)


def build_background_loop(cfg: dict, background: Path, duration: float) -> Path | None:
    """A concat list of shuffled segments long enough to cover `duration`.

    The order is shuffled per track but seeded by the picked background, so the
    same inputs always produce the same running order -- the cache key stays
    meaningful and a re-render reproduces the track it replaces.

    Returns None when rotation is off or no segments have been built, and the
    caller falls back to holding one still for the whole track.
    """
    rotate = _rotation_seconds(cfg)
    if rotate <= 0:
        return None
    segments = segment_pool()
    if len(segments) < 2:
        return None

    order = list(segments)
    random.Random(f"{background.name}|{duration:.0f}|{len(segments)}").shuffle(order)

    needed = max(1, int(duration // rotate) + 1)
    picked = [order[i % len(order)] for i in range(needed)]

    key = _hash(str(background), str(rotate), _segment_signature(), str(needed))
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    listing = BG_DIR / ("loop-" + key + ".txt")
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "\n".join("file '" + f.as_posix() + "'" for f in picked) + "\n",
        encoding="utf-8",
    )
    return listing


def _rotation_signature(cfg: dict) -> str:
    """The segment pool as far as *this* track is concerned.

    Empty when rotation is off: the track is then one still held for its whole
    length and nothing about the pool can change how it looks, so rebuilding
    the pool must not invalidate it. Reading the pool unconditionally is what
    took the live channel off air mid-rebuild.
    """
    if _rotation_seconds(cfg) <= 0:
        return ""
    return _segment_signature()


def _segment_signature() -> str:
    """What the segment pool is right now, names and contents both.

    Names alone are not enough. Rebuilding the pool at a different quality
    writes the same filenames with different pictures inside, and a signature
    that only saw names would report every existing track as still valid --
    the re-render would silently be skipped. Size and mtime move whenever the
    bytes do, which is exactly when a track has to be rendered again.
    """
    parts = []
    for q in segment_pool():
        try:
            st = q.stat()
            parts.append(f"{q.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append(q.name)
    return ",".join(parts)


# How the verses are animated, as far as the cache is concerned. Anything that
# changes what the caption layer looks like belongs here, so a track rendered
# before the change is recognised as out of date. It is kept separate from the
# rest of the key so the previous recipe can still be computed -- see
# playable_track.
CAPTION_STYLE = "fade:" + ",".join(str(x) for x in FADE_LEVELS) + \
                f"|{FADE_SECONDS}:{FADE_MIN_HOLD}"


def _key_parts(cfg: dict, audio: Path, background: Path) -> list[str] | None:
    """Everything about a track except how its captions are animated."""
    try:
        mtime = audio.stat().st_mtime_ns
    except OSError:
        return None
    entries = quran.load_timings(audio)
    cfg = resolve_fit(cfg, background)
    v = cfg["video"]
    return [
        str(audio), str(mtime), str(background),
        str(v["width"]), str(v["height"]), str(v["fps"]), str(v["bitrate"]),
        str(v.get("preset", "faster")),
        str(v.get("fit", "blur")), str(cfg.get("font", "")),
        str((cfg.get("overlay") or {}).get("font_size", 54)),
        str(len(entries or [])),
        str(_rotation_seconds(cfg)), _pool_signature(cfg), _rotation_signature(cfg),
    ]


def track_key(cfg: dict, audio: Path, background: Path) -> str | None:
    """The cache key for one (recitation, background) pair, without building it.

    Split out from build_track so the streamer can ask "is this already
    rendered?" cheaply. Rendering takes longer than the track itself plays, so
    discovering that mid-stream -- with ffmpeg already waiting on the pipe --
    would stall the broadcast.
    """
    parts = _key_parts(cfg, audio, background)
    return _hash(*parts, CAPTION_STYLE) if parts else None


def _superseded_key(cfg: dict, audio: Path, background: Path) -> str | None:
    """The key this pair had before the caption style entered the key.

    Tracks rendered under the old recipe are still perfectly watchable -- they
    simply have verses that cut in and out instead of fading. Being able to
    name them is what makes the change safe to deploy: the channel keeps
    playing them while the replacements are encoded, instead of every track on
    disk becoming unreachable the moment the key moves and the stream going
    quiet with ffmpeg sitting on an empty pipe. That has happened twice; it is
    not a thing to leave to luck.
    """
    parts = _key_parts(cfg, audio, background)
    return _hash(*parts) if parts else None


def existing_track(cfg: dict, audio: Path, background: Path) -> Path | None:
    """The current rendering of this pair, or None if it has yet to be built.

    Deliberately blind to superseded renderings: this is what the builder asks,
    and an out-of-date track has to read as missing or it would never be
    replaced.
    """
    key = track_key(cfg, audio, background)
    if not key:
        return None
    candidate = TRACK_DIR / f"{key}.ts"
    return candidate if candidate.exists() else None


def playable_track(cfg: dict, audio: Path, background: Path) -> Path | None:
    """Anything on disk that can go on air for this pair, current or not.

    This is what the streamer asks. It prefers the current rendering and falls
    back to a superseded one, so a style change costs the viewer nothing while
    it rolls through the cache.
    """
    found = existing_track(cfg, audio, background)
    if found:
        return found
    old = _superseded_key(cfg, audio, background)
    if not old:
        return None
    candidate = TRACK_DIR / f"{old}.ts"
    return candidate if candidate.exists() else None


def drop_superseded(cfg: dict, audio: Path, background: Path) -> None:
    """Delete the old rendering of a pair once the new one is in place."""
    if not existing_track(cfg, audio, background):
        return
    old = _superseded_key(cfg, audio, background)
    if old:
        (TRACK_DIR / f"{old}.ts").unlink(missing_ok=True)


def build_track(cfg: dict, audio: Path, background: Path, is_image: bool) -> Path | None:
    """Render `audio` + `background` + timed verses into one playable file."""
    ff = ffmpeg_bin()
    if not ff:
        return None

    entries = quran.load_timings(audio)
    duration = duration_of(audio)
    if duration is None:
        return None

    # Computed before resolve_fit, which track_key applies itself; calling it
    # twice on the same cfg is harmless but the key must come from the one
    # function so the builder and the streamer can never disagree about it.
    key = track_key(cfg, audio, background)
    if not key:
        return None
    cfg = resolve_fit(cfg, background)
    v = cfg["video"]
    a = cfg["audio"]
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

    # A still pool rotates; a video is left to play out at its own pace.
    bg_loop = build_background_loop(cfg, background, duration) if is_image else None

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    if bg_loop is not None:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(bg_loop)]
    elif is_image:
        cmd += ["-loop", "1", "-i", str(background)]
    else:
        cmd += ["-stream_loop", "-1", "-i", str(background)]
    cmd += ["-i", str(audio)]

    margin = (cfg.get("overlay") or {}).get("margin", 70)
    position = (cfg.get("overlay") or {}).get("position", "bottom")
    y = f"H-h-{margin}" if position == "bottom" else str(margin)

    fit = f"scale={v['width']}:{v['height']},setsar=1" if bg_loop is not None \
        else _fit_chain(cfg)

    if captions:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(captions)]
        chain = (
            f"[0:v]{fit},fps={v['fps']},format=yuv420p[bg];"
            f"[2:v]format=rgba[cap];"
            f"[bg][cap]overlay=(W-w)/2:{y}:shortest=0:format=auto[vout]"
        )
    else:
        chain = f"[0:v]{fit},fps={v['fps']},format=yuv420p[vout]"

    gop = int(v["fps"]) * int(v.get("keyframe_interval", 2))
    cmd += [
        "-filter_complex", chain,
        "-map", "[vout]", "-map", "1:a:0",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", str(v.get("preset", "faster")),
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
    # The superseded rendering of this pair has just been replaced on air, so
    # it is only taking up disk now.
    drop_superseded(cfg, audio, background)
    return out
