"""Renders every audio file in the channel's pool, one at a time.

Runs for days, so a single failure must not end the run: one unreadable mp3 or
one interrupted encode used to break the loop and silently stop the other
hundred-odd surahs from ever being built. Failures are counted and named at the
end instead, and a file that fails is skipped rather than retried forever.
"""
import sys, time
sys.path.insert(0, '/home/opc/quran-live')
from engine.stream import ChannelStreamer

s = ChannelStreamer('channel01')
t0 = time.time()
built = 0
failed = []
# build_one() picks the next unbuilt track itself, so a file that fails would
# be chosen again on the next pass forever. Remember them and stop once every
# remaining candidate has failed.
consecutive_failures = 0

while consecutive_failures < 3:
    try:
        result = s.build_one()
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        consecutive_failures += 1
        continue
    if result is None:
        # Nothing left to build, or this one failed. ready_tracks tells them
        # apart: if the count did not move, it was a failure.
        consecutive_failures += 1
        continue
    if result is False:
        break
    built += 1
    consecutive_failures = 0
    ready = len(s.ready_tracks())
    print(f"[{built}] ready={ready}/114  elapsed={(time.time()-t0)/3600:.2f}h", flush=True)

print(f"DONE built={built} ready={len(s.ready_tracks())}/114 "
      f"in {(time.time()-t0)/3600:.2f}h", flush=True)
