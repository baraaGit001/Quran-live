"""The 24/7 streamer for one channel.

Each recitation is pre-rendered once into a finished track (background, verses
burned in at the right moments, audio muxed) and remuxed to MPEG-TS. The live
stream is one long-lived ffmpeg reading a pipe that this process keeps writing
tracks into, copying every packet through untouched:

    track1.ts + track2.ts + ... -> pipe -> ffmpeg -c copy -> RTMP

MPEG-TS joins by appending bytes, but only the *container* does. Every track is
encoded on its own and therefore starts its clock at zero, so appending the raw
files sends time backwards at each join: the reader reports a corrupt packet and
a timestamp discontinuity, patches the video with a rolling offset and clamps
the audio DTS forward, and the two drift apart. Each track is therefore given a
timestamp offset that continues the one before it as it is written into the
pipe -- a stream copy, so the cost stays where the design needs it.

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
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config as cfg_mod
from . import cache as cache_mod
from . import quran, track as track_mod
from .ffmpeg import duration_of, ffmpeg_bin, span_of
from .selector import Selector

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
TS_DIR = ROOT / "cache" / "ts"
# Where the HLS playlists and segments are published when a channel streams to
# its own website instead of to a platform. Served read-only by nginx.
HLS_ROOT = ROOT / "site" / "live"

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


def _bits(value: object, fallback: int) -> int:
    """Parse an ffmpeg-style rate ("3500k", "2M", 900000) into bits per second."""
    text = str(value).strip().lower()
    scale = 1
    if text.endswith("k"):
        scale, text = 1_000, text[:-1]
    elif text.endswith("m"):
        scale, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * scale)
    except ValueError:
        return fallback


class ChannelStreamer:
    def __init__(self, channel: str):
        self.cfg = cfg_mod.load_channel(channel)
        self.name = self.cfg["name"]
        self.stop_event = threading.Event()
        # audio path -> seconds, filled lazily by _by_render_priority.
        self._durations: dict[Path, float] = {}
        # audio path -> the rendering of it that played last time.
        self._last_variant: dict[Path, Path] = {}
        # Where the next track starts on the stream's clock. Reset on every
        # ffmpeg start: a reconnect is a new timeline for YouTube.
        self.ts_cursor: float | None = None
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
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            (STATE_DIR / f"{self.name}.status.json").write_text(body, encoding="utf-8")
        except OSError:
            pass
        # The website reads the same status, and publishing it beside the
        # playlists means the web server needs no second mount and no access to
        # anything outside the directory it already serves. Written through a
        # temporary file so a poll never reads a half-written object.
        if self._is_hls():
            try:
                directory = self._hls_dir()
                directory.mkdir(parents=True, exist_ok=True)
                tmp = directory / ".status.json.tmp"
                tmp.write_text(body, encoding="utf-8")
                tmp.replace(directory / "status.json")
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
        """Every (audio, rendered .ts) pair on disk, ready to play now.

        A recitation can have more than one rendered track -- the same audio
        over a different background -- and all of them are returned. Stopping
        at the first, as this used to, meant a surah was welded to one picture
        for as long as it stayed in the cache: al-Ikhlas came round every time
        over the same photograph. The extra renders only mean anything if the
        feeder can see them, so it does the choosing.
        """
        images, videos = cfg_mod.visual_pool(self.cfg)
        backgrounds = images + videos
        ready = []
        for audio in cfg_mod.audio_pool(self.cfg):
            for background in backgrounds:
                # playable, not existing: a track rendered under an earlier
                # caption style still plays perfectly well and keeps the
                # channel fed while its replacement is encoded.
                found = track_mod.playable_track(self.cfg, audio, background)
                if found:
                    ready.append((audio, found))
        return ready

    @staticmethod
    def _variants_by_audio(ready: list[tuple[Path, Path]]) -> dict[Path, list[Path]]:
        """Group the ready pairs by recitation, keeping every background."""
        grouped: dict[Path, list[Path]] = {}
        for audio, ts in ready:
            grouped.setdefault(audio, []).append(ts)
        return grouped

    def _pick_variant(self, audio: Path, options: list[Path]) -> Path:
        """Which rendering of `audio` to play, avoiding the one played last.

        Kept per recitation rather than globally: what should not repeat is
        this surah's own picture from its own previous turn, which may have
        been an hour and forty tracks ago. A global history would not remember
        that far back, and would be spent on tracks that are not competing.
        """
        if len(options) == 1:
            return options[0]
        previous = self._last_variant.get(audio)
        fresh = [q for q in options if q != previous] or list(options)
        choice = random.choice(fresh)
        self._last_variant[audio] = choice
        return choice

    def _by_render_priority(self) -> list[Path]:
        """The recitation pool, shortest first.

        Order matters more than it looks. The pool's own order is the order of
        the mushaf, so the first unrendered file after a cache rebuild is
        al-Baqarah -- five hours of recitation that takes hours to encode and
        holds the queue the whole time. The channel meanwhile loops whatever
        few tracks already exist, which is what a listener actually hears: the
        same handful of short surahs coming round every quarter of an hour.

        Building shortest-first turns that around. The same encoder time buys
        dozens of surahs instead of one, so the rotation widens within the hour
        and the long recitations arrive later, when there is already plenty on
        disk to play behind them. Nothing renders less -- it renders in the
        order that keeps the stream worth listening to while it does.

        Durations are memoised because this runs before every single build and
        each miss is an ffprobe per file across the whole pool.
        """
        pool = list(cfg_mod.audio_pool(self.cfg))
        for audio in pool:
            if audio not in self._durations:
                self._durations[audio] = duration_of(audio) or 0.0
        # Unprobeable files sort last rather than first: a zero would put a
        # broken mp3 at the head of the queue on every pass.
        return sorted(pool, key=lambda a: self._durations[a] or float("inf"))

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

        pool = self._by_render_priority()
        missing = {
            audio: [b for b in backgrounds
                    if not track_mod.existing_track(self.cfg, audio, b)]
            for audio in pool
        }

        # Breadth before depth. Every recitation gets its first rendering
        # before any gets a second, so the mushaf is covered as early as the
        # encoder allows and no surah is missing while another already has
        # four pictures. Only once the whole pool is covered does the second
        # pass begin, and the rotation starts varying what it shows.
        for depth in range(len(backgrounds)):
            for audio in pool:
                if len(backgrounds) - len(missing[audio]) == depth and missing[audio]:
                    chosen = random.choice(missing[audio])
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

            grouped = self._variants_by_audio(ready)
            audio = self.audio_selector.pick(list(grouped))
            ts = self._pick_variant(audio, grouped[audio]) if audio else None
            if not audio or not ts or not ts.exists():
                self.stop_event.wait(5)
                continue
            self.current_ts = ts

            surah = quran.surah_from_filename(audio)
            self.current_track = f"{audio.name}" + (f" (surah {surah})" if surah else "")
            self._write_status("running", "streaming")
            self.log(f"now playing: {self.current_track}")

            try:
                self._write_one(ts, sink)
                sink.flush()
            except (BrokenPipeError, ValueError, OSError):
                break  # ffmpeg went away; the supervisor will restart it


    # One AAC frame at 44.1 kHz. Placing the next track exactly at the previous
    # one's last timestamp still leaves the two audio frames touching, and a
    # reader calls that non-monotonic; a single frame of daylight between them
    # is inaudible and makes the join strictly increasing.
    JOIN_GUARD = 1024 / 44100

    def _timeline(self, ts: Path) -> tuple[str, float] | None:
        """The offset that places `ts` after whatever has already been sent.

        Returns (offset, new cursor) or None if the track cannot be probed, in
        which case the caller sends it untouched rather than not at all.
        """
        span = span_of(ts)
        if span is None:
            return None
        start, end = span
        if self.ts_cursor is None:
            # The first track of a connection defines the origin; shifting it
            # to zero would make the offset negative, and ffmpeg then rewrites
            # the timestamps to avoid that and breaks the chain on the spot.
            self.ts_cursor = start
        offset = self.ts_cursor - start
        return f"{offset:.6f}", offset + end + self.JOIN_GUARD

    def _write_one(self, ts: Path, sink) -> None:
        """Copy one track into the pipe on the stream's running clock."""
        ff = ffmpeg_bin()
        plan = self._timeline(ts) if ff else None
        if plan is None:
            self.log(f"could not probe {ts.name} -- sending it unshifted")
            with ts.open("rb") as handle:
                while not self.stop_event.is_set():
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    sink.write(chunk)
            return

        offset, cursor = plan
        proc = subprocess.Popen(
            [ff, "-hide_banner", "-loglevel", "error", "-i", str(ts),
             "-c", "copy", "-muxdelay", "0", "-muxpreload", "0",
             # The continuity counters restart with every track too; saying so
             # is what stops the reader calling the first packet corrupt.
             "-mpegts_flags", "+initial_discontinuity",
             "-output_ts_offset", offset,
             "-f", "mpegts", "pipe:1"],
            stdout=subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            while not self.stop_event.is_set():
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                # Blocks once ffmpeg's buffer is full; with -re on the input
                # that backpressure is what holds the feeder at real time
                # instead of racing ahead.
                sink.write(chunk)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.ts_cursor = cursor

    # ---------- platform ingest ----------

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
        return f"{self._resolved(base).rstrip('/')}/{key}"

    @staticmethod
    def _resolved(base: str) -> str:
        """The ingest URL with its hostname replaced by an A record.

        The vendored ffmpeg is a static build, and static glibc cannot load the
        NSS modules that getaddrinfo needs, so ffmpeg cannot resolve a hostname
        at all -- it fails instantly with "Failed to resolve hostname ...:
        System error" and exits 251. Python's resolver works normally, so the
        lookup happens here and ffmpeg is handed a literal address. Resolving on
        every start (rather than pinning an IP in the channel file) means the
        stream follows YouTube's ingest as it moves. If the lookup fails, the
        hostname is left as-is so the error surfaces as a network problem.
        """
        parts = urlsplit(base)
        host = parts.hostname
        if not host:
            return base
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            return base
        netloc = ip if parts.port is None else f"{ip}:{parts.port}"
        return parts._replace(netloc=netloc).geturl()

    # ---------- HLS (self-hosted) ----------

    def _is_hls(self) -> bool:
        """True when this channel publishes to our own site, not to a platform.

        A strike lands on the channel, not on the software, and there is no
        appeal worth waiting on when the whole point is an uninterrupted
        recitation. Publishing HLS ourselves removes the third party: the same
        finished tracks, the same stream copy, segmented to disk instead of
        pushed to an ingest that can revoke the stream mid-ayah.
        """
        stream = self.cfg.get("stream", {}) or {}
        return str(stream.get("platform", "youtube")).lower() == "hls"

    def _hls_dir(self) -> Path:
        stream = self.cfg.get("stream", {}) or {}
        configured = stream.get("hls_dir")
        base = Path(configured) if configured else HLS_ROOT
        if not base.is_absolute():
            base = ROOT / base
        return base / self.name

    def _write_master(self) -> None:
        """The master playlist, written once rather than by ffmpeg.

        ffmpeg can emit a master itself, but only as part of a var_stream_map
        run that owns both renditions, and it rewrites the file on every
        restart -- so a player holding the old one gets a 404 during the gap.
        Two independent outputs plus a master we control means a reconnect
        replaces only the media playlists, which players already re-poll.
        """
        video = self.cfg.get("video", {}) or {}
        # Advertised bandwidth is the muxed rate, so audio counts too: a number
        # below the real rate makes a player pick a rendition it cannot sustain.
        vbits = _bits(video.get("bitrate", "3500k"), 3_500_000)
        abits = _bits((self.cfg.get("audio", {}) or {}).get("bitrate", "128k"), 128_000)
        width = int(video.get("width", 1280))
        height = int(video.get("height", 720))
        master = self._hls_dir() / "master.m3u8"
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            # Audio first: most of the audience is listening rather than
            # watching, and a player with no measured bandwidth yet starts on
            # the first entry.
            '#EXT-X-STREAM-INF:BANDWIDTH=%d,CODECS="mp4a.40.2"\n' % abits
            + "audio/index.m3u8\n"
            + '#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,'
              'CODECS="avc1.640028,mp4a.40.2"\n' % (vbits + abits, width, height)
            + "video/index.m3u8\n",
            encoding="utf-8",
        )

    def _hls_outputs(self) -> list[str]:
        """One ffmpeg, two renditions, no encoding in either.

        The audio-only rendition is the reason this is worth doing: a listener
        costs ~128 kbps instead of ~3.6 Mbps, and on a server that pays for its
        own egress that is the difference between the stream surviving an
        audience and not. Both are stream copies, so the second output is free.
        """
        directory = self._hls_dir()
        args: list[str] = []
        for kind, maps in (("audio", ["-map", "0:a"]),
                           ("video", ["-map", "0:v", "-map", "0:a"])):
            out = directory / kind
            out.mkdir(parents=True, exist_ok=True)
            args += [
                *maps, "-c", "copy",
                "-f", "hls",
                # Segments are cut on keyframes and keyframe_interval is 2s, so
                # 6s holds three of them: short enough that a viewer joins
                # within a few seconds, long enough that the playlist is not
                # rewritten constantly on a two-core box.
                "-hls_time", "6",
                "-hls_list_size", "8",
                # Segments outlive the playlist by this many, so a player that
                # is a little behind does not 404 on the one it is fetching.
                "-hls_delete_threshold", "4",
                "-hls_flags",
                "delete_segments+append_list+omit_endlist+independent_segments",
                # append_list continues the media sequence across an ffmpeg
                # restart; without an epoch start number two runs would reuse
                # segment names and a cached player would replay old audio.
                "-hls_start_number_source", "epoch",
                "-hls_segment_filename", str(out / "seg_%09d.ts"),
                str(out / "index.m3u8"),
            ]
        return args

    # ---------- ffmpeg ----------

    def _command(self) -> list[str]:
        base = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "warning",
            # Tracks arrive already placed on one continuous timeline by
            # _write_one, so nothing here has to repair them.
            "-re",
            "-f", "mpegts", "-i", "pipe:0",
        ]
        if self._is_hls():
            return base + self._hls_outputs()
        return base + [
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
        if self._is_hls():
            self._write_master()
            self.log(f"publishing HLS to {self._hls_dir()}")
        else:
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
                self.ts_cursor = None
                self.proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE)
            except Exception as exc:  # noqa: BLE001
                self.log(f"failed to launch ffmpeg: {exc}")
                self._write_status("failed", str(exc))
                return 4

            feeder = threading.Thread(target=self._feed, args=(self.proc.stdin,),
                                      daemon=True)
            feeder.start()
            code = self._wait_with_heartbeat()
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

    def _wait_with_heartbeat(self) -> int:
        """Wait for ffmpeg, refreshing the status file while it runs.

        The status used to be written only when something changed, and a single
        track runs for anything up to half an hour. The website decides the
        stream is dead from a stale timestamp -- it has to, because a supervisor
        that was killed leaves its last status behind saying "running" forever.
        So a healthy stream playing a long surah would show as stopped. The
        heartbeat is what separates those two cases.
        """
        while True:
            try:
                return self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._write_status("running", "streaming")

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
