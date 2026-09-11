"""Builds the looping video a channel streams.

This is the whole reason five channels fit on two cores. Encoding H.264 live,
five times over, would cost 2.5-4 cores on this box -- it has no hardware
encoder, so every frame would go through x264 on the CPU, forever. Instead each
channel's visuals are encoded ONCE into a seamless loop here, and the 24/7
stream copies those packets through untouched (-c:v copy). The recurring cost
per channel drops to AAC audio only, a few percent of one core.

Segments are cached by content hash, so adding one new image or video re-encodes
only that file rather than rebuilding the whole loop from scratch.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import config as cfg_mod
from . import overlay as overlay_mod
from .ffmpeg import ffmpeg_bin

ROOT = Path(__file__).resolve().parent.parent
SEG_DIR = ROOT / "cache" / "segments"
LOOP_DIR = ROOT / "cache" / "loops"


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _fit_chain(cfg: dict) -> str:
    """How a source of any shape is made to fill a 16:9 canvas.

    Phone wallpapers are portrait; dropped straight into a landscape frame they
    leave half the screen black. `fit` picks the trade-off:
      blur - the frame is filled with a blurred, zoomed copy of the same image
             and the whole picture sits on top. Nothing cropped, nothing black,
             but the extra blurred pass makes rendering ~70% slower.
      crop - zoom until the frame is full, losing the edges. Cheapest and
             sharpest, but a portrait source loses most of its height.
      pad  - letterbox/pillarbox on black, showing everything.
      auto - crop when little would be lost, blur when a lot would be
             (see resolve_fit). Sensible when the visual pool mixes shapes.
    """
    w, h = cfg["video"]["width"], cfg["video"]["height"]
    mode = str(cfg["video"].get("fit", "blur")).lower()
    if mode == "auto":
        mode = "crop"  # resolved per-source by resolve_fit(); this is the fallback

    if mode == "crop":
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1"
        )
    if mode == "pad":
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    # blur: split the source, cover the canvas with a blurred zoom of it, then
    # lay the fully-visible image on top.
    return (
        f"split=2[blurbg][fg];"
        f"[blurbg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=28,eq=brightness=-0.06[bgblur];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgblur][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def _video_filter(cfg: dict, overlay_png: Path | None) -> tuple[str, list[str]]:
    """Fit the source to the canvas, then composite the caption if any."""
    chain = (
        f"{_fit_chain(cfg)},"
        f"fps={cfg['video']['fps']},format=yuv420p"
    )
    extra: list[str] = []
    if overlay_png and overlay_png.exists():
        margin = cfg.get("overlay", {}).get("margin", 80)
        position = cfg.get("overlay", {}).get("position", "bottom")
        y = f"H-h-{margin}" if position == "bottom" else str(margin)
        extra = ["-i", str(overlay_png)]
        chain = f"[0:v]{chain}[bg];[bg][1:v]overlay=(W-w)/2:{y}"
    else:
        chain = f"[0:v]{chain}"
    return chain, extra


def build_segment(cfg: dict, source: Path, overlay_png: Path | None, is_image: bool) -> Path | None:
    """Encode one visual into a normalised, GOP-aligned segment (cached)."""
    ff = ffmpeg_bin()
    if not ff:
        return None

    v = cfg["video"]
    duration = cfg["video"].get("image_duration", 20)
    key = _hash(
        str(source), str(source.stat().st_mtime_ns), str(v["width"]), str(v["height"]),
        str(v["fps"]), str(v["bitrate"]), str(duration),
        str(v.get("fit", "blur")),
        # The overlay's *content*, not just its path: the caption PNG is
        # rewritten in place whenever the text or font changes, so keying on the
        # filename alone would silently reuse segments carrying the old caption.
        str(overlay_png or ""),
        str(overlay_png.stat().st_mtime_ns) if overlay_png and overlay_png.exists() else "",
    )
    out = SEG_DIR / f"{key}.mp4"
    if out.exists():
        return out
    SEG_DIR.mkdir(parents=True, exist_ok=True)

    chain, extra = _video_filter(cfg, overlay_png)
    gop = int(v["fps"]) * int(v.get("keyframe_interval", 2))

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    if is_image:
        cmd += ["-loop", "1", "-t", str(duration)]
    cmd += ["-i", str(source)]
    cmd += extra
    cmd += [
        "-filter_complex", chain,
        "-an",
        "-c:v", "libx264",
        # veryfast is the honest ceiling for a one-off encode on Neoverse-N1;
        # slower presets would buy quality this content cannot show off anyway.
        "-preset", "veryfast",
        "-tune", "stillimage" if is_image else "film",
        "-b:v", str(v["bitrate"]),
        "-maxrate", str(v["bitrate"]),
        "-bufsize", f"{int(str(v['bitrate']).rstrip('k')) * 2}k",
        # Fixed GOP with no scene-cut keyframes: every segment starts on an
        # IDR frame, which is what lets them be concatenated and then looped
        # forever without re-encoding.
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        return None
    return out


def build_loop(cfg: dict, force: bool = False) -> Path | None:
    """Concatenate every visual segment into the channel's loop file."""
    ff = ffmpeg_bin()
    if not ff:
        return None

    images, videos = cfg_mod.visual_pool(cfg)
    if not images and not videos:
        return None

    ov = cfg.get("overlay", {}) or {}
    overlay_png = None
    if ov.get("enabled", True) and ov.get("text"):
        overlay_png = overlay_mod.render(
            ov["text"],
            cfg.get("font", ""),
            ov.get("font_size", 54),
            ov.get("color", "#FFFFFF"),
            ROOT / "cache" / "overlays" / f"{cfg['name']}.png",
        )

    segments: list[Path] = []
    for source in images:
        seg = build_segment(cfg, source, overlay_png, is_image=True)
        if seg:
            segments.append(seg)
    for source in videos:
        seg = build_segment(cfg, source, overlay_png, is_image=False)
        if seg:
            segments.append(seg)

    if not segments:
        return None

    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    out = LOOP_DIR / f"{cfg['name']}.mp4"
    listing = LOOP_DIR / f"{cfg['name']}.concat.txt"
    listing.write_text(
        "".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8"
    )

    if out.exists() and not force:
        newest = max(s.stat().st_mtime for s in segments)
        if out.stat().st_mtime >= newest and out.stat().st_mtime >= listing.stat().st_mtime:
            return out

    # Concat by stream copy: the segments already share codec, resolution, fps
    # and GOP structure, so no pixel is re-encoded here.
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]
    if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.exists():
        return None
    return out


def resolve_fit(cfg: dict, source: Path) -> dict:
    """Resolve `fit: auto` against one source, returning a cfg to render with.

    Cropping a 16:9 photo to 16:9 loses nothing and is the fastest path, but
    cropping a phone wallpaper to 16:9 throws away roughly two thirds of the
    picture. Rather than force one compromise across a mixed pool, `auto`
    measures what cropping would actually cost this image and only falls back
    to the slower blurred fill when that cost is high.
    """
    import copy
    import subprocess

    if str(cfg["video"].get("fit", "")).lower() != "auto":
        return cfg

    from .ffmpeg import ffprobe_bin
    probe = ffprobe_bin()
    resolved = "blur"
    if probe:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(source)],
            capture_output=True, text=True,
        )
        try:
            sw, sh = (int(x) for x in result.stdout.strip().split(",")[:2])
            target = cfg["video"]["width"] / cfg["video"]["height"]
            source_ar = sw / sh
            # Fraction of the image discarded by a centre crop to `target`.
            loss = 1 - (source_ar / target if source_ar < target else target / source_ar)
            resolved = "crop" if loss <= 0.25 else "blur"
        except (ValueError, ZeroDivisionError):
            pass

    out = copy.deepcopy(cfg)
    out["video"]["fit"] = resolved
    return out
