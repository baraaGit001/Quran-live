"""The 24/7 streamer for one channel.

Each recitation is pre-rendered once into a finished track (background, verses
burned in at the right moments, audio muxed) and remuxed to MPEG-TS. MPEG-TS
concatenates by simply appending bytes, so the live stream is one long-lived
ffmpeg reading a pipe that this process keeps writing tracks into, copying every
packet through untouched:

    track1.ts + track2.ts + ... -> pipe -> ffmpeg -c copy -> RTMP

Nothing is encoded while streaming, so a channel costs a few percent of a core
no matter how long it runs, and there is no reconnect between tracks. The pool
is re-scanned before every pick, so a recitation added today joins the rotation
at the next track change without restarting anything.
"""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg_mod
from . import cache as cache_mod
from . import quran, track as track_mod
from .ffmpeg import ffmpeg_bin
from .selector import Selector

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
TS_DIR = ROOT / "cache" / "ts"

# Ingest endpoints by platform. YouTube is the default and the only one with
# no cap on how long a single stream may run; Facebook ends a stream after
# about 8 hours, so a 24/7 channel there reconnects several times a day (the
# supervisor handles that, but the gap is visible to viewers). Instagram has
# no public RTMP ingest at all, so it cannot be a target.
INGEST = {
    "youtube": "rtmp://a.rtmp.youtube.com/live2",
    "youtube-backup": "rtmp://b.rtmp.youtube.com/live2?backup=1",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp",
    "twitch": "rtmp://live.twitch.tv/app",
}
CHUNK = 188 * 1024  # a whole number of TS packets


class ChannelStreamer:
    def __init__(self, channel: str):
        self.cfg = cfg_mod.load_channel(channel)
        self.name = self.cfg["name"]
        self.stop_event = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.started_at = time.time()
        self.current_track: str | None = None
        self.current_ts: Path | None = None
        self.audio_selector = Selector(
            self.name, "audio", self.cfg.get("selection", {}).get("history_size", 10)
        )
        self.visual_selector = Selector(self.name, "visual", 5)

    # ---------- status / logging ----------

    def log(self, message: str) -> None:
        # Written to a file as well as stdout. journald is the natural place,
        # but this host keeps no persistent journal (/var/log/journal does not
        # exist) and creates no user journal at all, so `journalctl --user`
        # returns nothing -- the file is what makes `./live logs` work here
        # without reconfiguring journald for every service on the box.
        # Nothing logged ever includes the stream key.
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {self.name}: {message}"
        print(line, flush=True)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"{self.name}.log"
            # Trimmed rather than rotated: a 24/7 stream logs a line or two per
            # track, so a couple of MB is months of history.
            if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                path.write_text("\n".join(tail) + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _write_status(self, state: str, detail: str = "") -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "channel": self.name,
            "state": state,
            "detail": detail,
            # Our own pid, not ffmpeg's: this is what tells a reader whether
            # the file describes a live run or is left over from a dead one.
            "supervisor_pid": os.getpid(),
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
            "restarts": self.restarts,
            "uptime_seconds": int(time.time() - self.started_at),
            "current_track": self.current_track,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            (STATE_DIR / f"{self.name}.status.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------- track preparation ----------

    def _pick_background(self) -> tuple[Path, bool] | None:
        images, videos = cfg_mod.visual_pool(self.cfg)
        pool = images + videos
        if not pool:
            return None
        chosen = self.visual_selector.pick(pool)
        if chosen is None:
            return None
        return chosen, chosen in images

    def ready_tracks(self) -> list[tuple[Path, Path]]:
        """(audio, rendered .ts) pairs already on disk, ready to play now."""
        images, videos = cfg_mod.visual_pool(self.cfg)
        backgrounds = images + videos
        ready = []
        for audio in cfg_mod.audio_pool(self.cfg):
            for background in backgrounds:
                found = track_mod.existing_track(self.cfg, audio, background)
                if found:
                    ready.append((audio, found))
                    break
        return ready

    def _pending_pair(self) -> tuple[Path, Path, bool] | None:
        """The next (audio, background) combination with no rendered track.

        Each recitation is paired with a background chosen at random rather than
        the first in the list: taking the first would give every track in the
        channel the same picture, since one rendered track per recitation is
        enough for it to count as ready. Randomising means variety emerges
        across the pool without rendering every audio against every visual,
        which would be a combinatorial amount of encoding and disk.
        """
        images, videos = cfg_mod.visual_pool(self.cfg)
        backgrounds = images + videos
        if not backgrounds:
            return None
        for audio in cfg_mod.audio_pool(self.cfg):
            unrendered = [
                b for b in backgrounds
                if not track_mod.existing_track(self.cfg, audio, b)
            ]
            if len(unrendered) == len(backgrounds):
                # Nothing rendered for this recitation yet -- pick any visual.
                chosen = random.choice(unrendered)
                return audio, chosen, chosen in images
        return None

    def build_one(self) -> bool | None:
        """Render a single missing track.

        Three outcomes, because a long batch render has to tell them apart:
        None when there is nothing left to build, True on success, and False
        when this particular track failed. Collapsing the last two into one
        value made a single bad file look like "all done" and silently ended
        the run with a hundred surahs unrendered.
        """
        pending = self._pending_pair()
        if not pending:
            return None
        audio, background, is_image = pending

        # Make room before rendering, never after: the point of the ceiling is
        # that the disk cannot fill, and the currently streaming track is
        # protected from its own cache eviction.
        keep = {self.current_ts} if self.current_ts else set()
        evicted = cache_mod.enforce(
            self.cfg.get("cache", {}).get("max_gb", 60), keep=keep
        )
        if evicted:
            self.log(f"cache over budget -- evicted {len(evicted)} old track(s)")

        self.log(f"rendering {audio.name} over {background.name} (one-off)")
        try:
            mp4 = track_mod.build_track(self.cfg, audio, background, is_image)
        except RuntimeError as exc:
            self.log(str(exc))
            return False
        if not mp4:
            self.log(f"failed to render {audio.name}")
            return False
        self.log(f"ready: {audio.name}")
        return True

    def _builder(self) -> None:
        """Render missing tracks in the background, never in the feeder's way.

        Rendering takes longer than a track plays, so it can never happen while
        the stream is waiting for the next one -- ffmpeg would sit on an empty
        pipe and YouTube would see a stall. Everything is prepared ahead here;
        the feeder only ever picks from what is already finished.
        """
        while not self.stop_event.is_set():
            try:
                outcome = self.build_one()
                if outcome is None:
                    self.stop_event.wait(60)  # nothing pending; check again later
                elif outcome is False:
                    # build_one picks the next unbuilt track itself, so a file
                    # that always fails would be retried in a tight loop.
                    self.stop_event.wait(60)
            except Exception as exc:  # noqa: BLE001 - a bad file must not kill it
                self.log(f"builder error: {exc}")
                self.stop_event.wait(30)

    # ---------- feeding ----------

    def _feed(self, sink) -> None:
        """Write track after track into ffmpeg, forever."""
        while not self.stop_event.is_set():
            # Re-scanned every pick: a recitation rendered since the last track
            # joins the rotation here, with no restart.
            ready = self.ready_tracks()
            if not ready:
                self.log("no rendered tracks yet -- waiting for the builder")
                self.stop_event.wait(10)
                continue

            audio = self.audio_selector.pick([a for a, _ in ready])
            ts = dict(ready).get(audio)
            if not audio or not ts or not ts.exists():
                self.stop_event.wait(5)
                continue
            self.current_ts = ts

            surah = quran.surah_from_filename(audio)
            self.current_track = f"{audio.name}" + (f" (surah {surah})" if surah else "")
            self._write_status("running", "streaming")
            self.log(f"now playing: {self.current_track}")

            try:
                with ts.open("rb") as handle:
                    while not self.stop_event.is_set():
                        chunk = handle.read(CHUNK)
                        if not chunk:
                            break
                        # Blocks once ffmpeg's buffer is full; with -re on the
                        # input that backpressure is what holds the feeder at
                        # real time instead of racing ahead.
                        sink.write(chunk)
                sink.flush()
            except (BrokenPipeError, ValueError, OSError):
                break  # ffmpeg went away; the supervisor will restart it

    # ---------- ffmpeg ----------

    def _rtmp_url(self) -> str:
        key = cfg_mod.stream_key(self.cfg)
        if not key:
            raise RuntimeError(f"{self.cfg['stream_key_env']} is not set")
        stream = self.cfg.get("stream", {}) or {}
        # An explicit URL wins, so an endpoint this table does not know about
        # can still be used without a code change.
        base = stream.get("ingest_url")
        if not base:
            platform = str(stream.get("platform", "youtube")).lower()
            base = INGEST.get(platform)
            if not base:
                raise RuntimeError(
                    f"unknown platform '{platform}' -- use one of "
                    f"{', '.join(sorted(INGEST))}, or set stream.ingest_url"
                )
        return f"{base.rstrip('/')}/{key}"

    def _command(self) -> list[str]:
        return [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "warning",
            # Each track carries its own timestamps starting at zero, so they
            # are regenerated into one continuous timeline as they arrive.
            "-fflags", "+genpts", "-re",
            "-f", "mpegts", "-i", "pipe:0",
            "-c", "copy",
            "-f", "flv", "-flvflags", "no_duration_filesize",
            "-rw_timeout", "15000000",
            self._rtmp_url(),
        ]

    # ---------- supervision ----------

    def run(self) -> int:
        if not ffmpeg_bin():
            self.log("ffmpeg not found -- run ./live validate")
            return 2
        try:
            self._rtmp_url()
        except RuntimeError as exc:
            self.log(str(exc))
            self._write_status("failed", str(exc))
            return 3

        threading.Thread(target=self._builder, daemon=True).start()

        # ffmpeg must never be started with nothing to send it: an empty pipe
        # is a dead stream as far as YouTube is concerned.
        if not self.ready_tracks():
            self.log("no rendered tracks yet -- rendering the first one before going live")
            self._write_status("preparing", "rendering first track")
            while not self.stop_event.is_set() and not self.ready_tracks():
                outcome = self.build_one()
                if outcome is False:
                    # One bad file, not the end of the pool: try the next.
                    continue
                if outcome is None:
                    self.log("nothing could be rendered (check ./live validate)")
                    self._write_status("failed", "no renderable tracks")
                    return 6

        rapid = 0
        max_rapid = self.cfg.get("stream", {}).get("max_rapid_failures", 10)
        delay = self.cfg.get("stream", {}).get("restart_delay", 5)

        while not self.stop_event.is_set():
            began = time.time()
            self._write_status("starting")
            self.log("starting ffmpeg")

            try:
                self.proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE)
            except Exception as exc:  # noqa: BLE001
                self.log(f"failed to launch ffmpeg: {exc}")
                self._write_status("failed", str(exc))
                return 4

            feeder = threading.Thread(target=self._feed, args=(self.proc.stdin,),
                                      daemon=True)
            feeder.start()
            code = self.proc.wait()
            if self.stop_event.is_set():
                break

            lived = time.time() - began
            self.restarts += 1
            # Only rapid failures count toward giving up: a stream that ran for
            # hours before a network blip is healthy, not flapping.
            rapid = rapid + 1 if lived < 60 else 0
            self.log(f"ffmpeg exited ({code}) after {int(lived)}s -- restart #{self.restarts}")
            self._write_status("restarting", f"exit {code}")

            if rapid >= max_rapid:
                self.log(f"{rapid} rapid failures in a row -- giving up")
                self._write_status("failed", "too many rapid failures")
                return 5
            time.sleep(min(delay * max(1, rapid), 60))

        self._write_status("stopped")
        return 0

    def shutdown(self, *_args) -> None:
        self.log("shutting down")
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m engine.stream <channel>", file=sys.stderr)
        return 1
    streamer = ChannelStreamer(argv[1])
    signal.signal(signal.SIGTERM, streamer.shutdown)
    signal.signal(signal.SIGINT, streamer.shutdown)
    return streamer.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
