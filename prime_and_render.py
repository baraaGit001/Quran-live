"""Rebuild the track cache after the background-rotation change.

Changing the rotation settings changes the cache key, so every track rendered
before it is unreachable -- ready_tracks() reports 0 and the stream has nothing
queued behind whatever is on air. Rendering in the pool's own order would start
with al-Baqarah and leave an hour of silence, so the shortest recitations are
built first to get a buffer back on disk, and the full run follows.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import config as cfg_mod, track as track_mod
from engine.ffmpeg import duration_of
from engine.stream import ChannelStreamer

PRIME = 10

s = ChannelStreamer("channel01")
cfg = s.cfg
images, _ = cfg_mod.visual_pool(cfg)
images = sorted(images)
if not images:
    print("no stills in the pool -- nothing can be rendered", flush=True)
    raise SystemExit(1)

with_duration = []
for a in cfg_mod.audio_pool(cfg):
    d = duration_of(a)
    if d:
        with_duration.append((d, a))
with_duration.sort(key=lambda pair: pair[0])

print(f"pool: {len(with_duration)} recitations, {len(images)} stills", flush=True)
print(f"ready before: {len(s.ready_tracks())}/114", flush=True)

t0 = time.time()
for i, (d, audio) in enumerate(with_duration[:PRIME]):
    bg = images[i % len(images)]
    print(f"[{i+1}/{PRIME}] {audio.name} ({d/60:.1f} min) over {bg.name}", flush=True)
    try:
        out = track_mod.build_track(cfg, audio, bg, True)
        status = out.name if out else "FAILED"
    except Exception as exc:                      # noqa: BLE001 - keep going
        status = f"FAILED {exc}"
    print(f"    -> {status}   ready={len(s.ready_tracks())}/114 "
          f"elapsed={(time.time()-t0)/60:.1f}m", flush=True)

print("priming done -- handing off to render_all.py", flush=True)
subprocess.run([sys.executable, "render_all.py"], cwd=str(ROOT))
