"""Build the rotation segments: one 5-minute clip per still and per video slice.

Every segment is encoded to the same codec parameters, because the concat
demuxer will only join streams that agree on them. Encoding happens here, once,
rather than inside each track render -- the pool is a few dozen clips, while the
recitations that use them number 114.

Run with no arguments to (re)build everything:

    python3 build_segments.py

Stills come from assets/visuals/images. Source videos come from
cache/source/*.mp4 and are sliced end to end; trim them before dropping them in
if their opening or closing titles should not appear.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import config as cfg_mod
from engine.ffmpeg import duration_of, ffmpeg_bin
from engine.prerender import _fit_chain, resolve_fit

SEGMENT_DIR = ROOT / "cache" / "segments"
SOURCE_DIR = ROOT / "cache" / "source"

cfg = cfg_mod.load_channel("channel01")
V = cfg["video"]
W, H, FPS = int(V["width"]), int(V["height"]), int(V["fps"])
ROTATE = int(V.get("visual_rotate_seconds", 300) or 300)
GOP = FPS * int(V.get("keyframe_interval", 2))
FF = ffmpeg_bin()

# Identical on every segment: the concat demuxer rejects a mismatch, and a
# silent mismatch shows up as a stream that stops after the first clip.
ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
    "-b:v", str(V["bitrate"]), "-maxrate", str(V["bitrate"]),
    "-bufsize", f"{int(str(V['bitrate']).rstrip('k')) * 2}k",
    "-g", str(GOP), "-keyint_min", str(GOP), "-sc_threshold", "0",
    "-pix_fmt", "yuv420p", "-an", "-f", "mpegts",
]


def run(cmd) -> bool:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    ffmpeg failed: {r.stderr.strip()[:300]}", flush=True)
    return r.returncode == 0


def build_still(src: Path, index: int) -> None:
    out = SEGMENT_DIR / f"still-{index:02d}.ts"
    if out.exists():
        print(f"  still-{index:02d}  exists", flush=True)
        return
    local = resolve_fit(cfg, src)
    tmp = out.with_suffix(".partial.ts")
    print(f"  still-{index:02d}  <- {src.name}", flush=True)
    ok = run([FF, "-y", "-hide_banner", "-loglevel", "error",
              "-loop", "1", "-i", str(src), "-t", str(ROTATE),
              "-vf", f"{_fit_chain(local)},fps={FPS},format=yuv420p",
              *ENCODE, str(tmp)])
    if ok and tmp.exists():
        tmp.rename(out)
    else:
        tmp.unlink(missing_ok=True)


def build_video(src: Path, tag: str) -> None:
    total = duration_of(src)
    if not total:
        print(f"  {src.name}: unreadable, skipped", flush=True)
        return
    slices = int(total // ROTATE)
    if slices < 1:
        print(f"  {src.name}: shorter than one window, skipped", flush=True)
        return
    print(f"  {src.name}: {total/60:.1f} min -> {slices} slices", flush=True)
    for i in range(slices):
        out = SEGMENT_DIR / f"{tag}-{i:03d}.ts"
        if out.exists():
            continue
        tmp = out.with_suffix(".partial.ts")
        # -ss before -i seeks without decoding everything ahead of the cut.
        ok = run([FF, "-y", "-hide_banner", "-loglevel", "error",
                  "-ss", str(i * ROTATE), "-i", str(src), "-t", str(ROTATE),
                  "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                         f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p",
                  *ENCODE, str(tmp)])
        if ok and tmp.exists():
            tmp.rename(out)
            print(f"    {out.name}", flush=True)
        else:
            tmp.unlink(missing_ok=True)


def main() -> int:
    if not FF:
        print("ffmpeg not found", flush=True)
        return 1
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"window: {ROTATE}s   frame: {W}x{H}@{FPS}", flush=True)

    print("stills:", flush=True)
    images, _ = cfg_mod.visual_pool(cfg)
    for i, src in enumerate(sorted(images), start=1):
        build_still(src, i)

    print("videos:", flush=True)
    if SOURCE_DIR.is_dir():
        for src in sorted(SOURCE_DIR.glob("*.mp4")):
            build_video(src, src.stem)
    else:
        print("  no cache/source directory", flush=True)

    built = sorted(SEGMENT_DIR.glob("*.ts"))
    total = sum(f.stat().st_size for f in built)
    print(f"\\ndone: {len(built)} segments, {total/1073741824:.2f} GB, "
          f"{len(built)*ROTATE/60:.0f} min of material", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
