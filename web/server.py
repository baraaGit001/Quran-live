#!/usr/bin/env python3
"""Control panel for quran-live: channels, media and long-running jobs.

Standard library only -- the box runs two web apps already and this is a small
operator tool, not another dependency to keep alive.

It binds to 127.0.0.1 by default and is meant to be reached over an SSH tunnel:

    ssh -N -L 8770:127.0.0.1:8770 pricelens

Binding anywhere else requires QURAN_WEB_TOKEN to be set, because every action
here spends CPU, writes into the asset pools or restarts a live broadcast.

Two rules shape the request handling:

**Nothing expensive happens on a request thread.** The first version answered
one fat /api/state by walking the whole cache tree, rglobbing every media pool
and shelling out to `bin/live status` -- on every poll, and the page polled
every five seconds while a build ran. Cache sizes and readiness are now
computed by a background refresher and served from memory; a request only ever
reads a dict. Anything a caller wants fresh is asked for on its own endpoint.

**A running job is streamed, not polled.** Build and sync print for hours; the
page opens an EventSource and receives lines as they appear. Output is also
appended to logs/jobs/<id>.log, so restarting the panel does not lose the log
of a build that is still running.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
LIVE = ROOT / "bin" / "live"
STATE = ROOT / "state"
JOB_LOGS = ROOT / "logs" / "jobs"

# Where each media kind lives, and what it is allowed to contain. Uploads are
# confined to these directories and to these suffixes; anything else is refused
# rather than written and discovered later by the renderer.
MEDIA_KINDS = {
    "audio": {"dir": ROOT / "assets" / "audio", "ext": {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac"}},
    "images": {"dir": ROOT / "assets" / "visuals" / "images", "ext": {".jpg", ".jpeg", ".png", ".webp"}},
    "videos": {"dir": ROOT / "assets" / "visuals" / "videos", "ext": {".mp4", ".mkv", ".mov", ".webm"}},
    "fonts": {"dir": ROOT / "assets" / "fonts", "ext": {".ttf", ".otf"}},
}

# 2 GB. A single recitation is far smaller; this only stops a runaway upload
# from filling the disk the streams and the two web apps share.
MAX_UPLOAD = 2 * 1024 * 1024 * 1024

# The streams share this disk with two web apps; leave the same 10 GB floor the
# cache respects.
DISK_FLOOR = 10 * 1024 * 1024 * 1024

TOKEN = os.environ.get("QURAN_WEB_TOKEN", "")

# Failed-token tracking. Reachable from the internet, the token is the only
# thing between a stranger and the broadcast controls, so guessing has to cost
# something: after this many failures an address waits.
FAIL_MAX = 8
FAIL_WINDOW = 300.0
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def _throttled(addr: str) -> bool:
    now = time.time()
    with _fails_lock:
        hits = [t for t in _fails.get(addr, []) if now - t < FAIL_WINDOW]
        _fails[addr] = hits
        return len(hits) >= FAIL_MAX


def _note_failure(addr: str) -> None:
    now = time.time()
    with _fails_lock:
        _fails.setdefault(addr, []).append(now)
        if len(_fails) > 1000:  # bounded; this is a small operator tool
            for k in [k for k, v in _fails.items() if not v or now - v[-1] > FAIL_WINDOW]:
                _fails.pop(k, None)


# --------------------------------------------------------------------------
# Background facts
#
# Everything here costs real IO -- walking 6.8 GB of cache, hashing a cache key
# for 114 recitations -- so it is computed on a timer by one thread and read
# from memory by requests. A caller that wants it recomputed now says so, and
# still gets the last known value immediately rather than waiting.
# --------------------------------------------------------------------------

class Fact:
    """One periodically refreshed value, readable without blocking."""

    def __init__(self, name: str, compute, every: float):
        self.name = name
        self._compute = compute
        self.every = every
        self._lock = threading.Lock()
        self._value = None
        self._at = 0.0
        self._error = ""
        self._due = threading.Event()

    def read(self) -> dict:
        with self._lock:
            return {"value": self._value, "at": self._at, "error": self._error,
                    "stale": time.time() - self._at > self.every * 2}

    def refresh_soon(self) -> None:
        self._due.set()

    def _once(self) -> None:
        try:
            v, err = self._compute(), ""
        except Exception as e:                    # noqa: BLE001 - surfaced as text
            v, err = None, f"{type(e).__name__}: {e}"
        with self._lock:
            if v is not None or not self._value:
                self._value = v
            self._at = time.time()
            self._error = err

    def run(self) -> None:
        while True:
            self._once()
            self._due.wait(self.every)
            self._due.clear()


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


def _cache_sizes() -> dict:
    cache = ROOT / "cache"
    if not cache.is_dir():
        return {}
    return {d.name: _dir_size(d) for d in sorted(cache.iterdir()) if d.is_dir()}


def _readiness() -> dict:
    """How many recitations have a rendered track the streamer can play now.

    This is the number that matters during a rebuild -- the channel is only as
    safe as its buffer -- and the engine already knows how to answer it, so the
    panel imports it rather than reimplementing the cache key. Import failure is
    not fatal: the panel still runs the channels, it just cannot show the count.
    """
    sys.path.insert(0, str(ROOT))
    from engine import config as cfg_mod
    from engine.stream import ChannelStreamer

    out = {}
    for f in sorted((ROOT / "channels").glob("*.yaml")):
        try:
            s = ChannelStreamer(f.stem)
            out[f.stem] = {"ready": len(s.ready_tracks()),
                           "total": len(cfg_mod.audio_pool(s.cfg))}
        except Exception as e:                    # noqa: BLE001 - per channel
            out[f.stem] = {"ready": None, "total": None, "error": str(e)}
    return out


CACHE_FACT = Fact("cache", _cache_sizes, 90.0)
READY_FACT = Fact("readiness", _readiness, 45.0)


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------

def _channels() -> list[dict]:
    """Channel YAML read with a deliberately small parser.

    The panel only needs the handful of scalars it displays, and shelling out
    to the engine for every poll would cost more than the page is worth. The
    engine remains the authority; nothing here writes YAML back.
    """
    out = []
    cdir = ROOT / "channels"
    if not cdir.is_dir():
        return out
    for f in sorted(cdir.glob("*.yaml")):
        info = {"file": f.name, "name": f.stem, "title": "", "audio_dir": "",
                "visual_dir": "", "enabled": True, "font": "", "platform": ""}
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.match(r'^\s{0,2}(\w+):\s*"?([^"#]*?)"?\s*(?:#.*)?$', line)
                if not m:
                    continue
                key, val = m.group(1), m.group(2).strip()
                if key in ("name", "title", "audio_dir", "visual_dir", "font") and val:
                    info[key] = val
                elif key == "enabled":
                    info["enabled"] = val.lower() != "false"
                elif key == "platform" and val:
                    info["platform"] = val
        except OSError:
            pass
        out.append(info)
    return out


def _unit_active(name: str) -> str:
    """systemd's own word for the unit: active, inactive, failed, ..."""
    try:
        p = subprocess.run(
            ["systemctl", "--user", "is-active", f"quran-live@{name}.service"],
            capture_output=True, text=True, timeout=5,
        )
        return (p.stdout or p.stderr or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _channel_status(name: str) -> dict:
    """The streamer's own view, written to state/ as it runs.

    Read from the file rather than parsed back out of `live status`: the CLI
    formats these same fields into a fixed-width table for a terminal, and
    scraping that would break the first time a column moved.
    """
    f = STATE / f"{name}.status.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d["unit"] = _unit_active(name)
    try:
        d["age"] = int(time.time() - f.stat().st_mtime)
    except OSError:
        d["age"] = None
    return d


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
# build and sync take as long as the recitation itself, so they outlive the
# request that asked for them. Each keeps its lines in memory for streaming and
# appends them to logs/jobs/<id>.log, which is what survives a panel restart.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
MAX_LINES = 5000          # per job, in memory; the file keeps everything


def _plain(text: str) -> str:
    """bin/live colours its output for a terminal; the page is not one."""
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def _job_public(job_id: str, j: dict) -> dict:
    return {
        "id": job_id, "cmd": j["cmd"], "running": j["running"], "code": j["code"],
        "started": j["started"], "finished": j["finished"], "lines": len(j["lines"]),
    }


def _jobs_snapshot() -> list[dict]:
    with _jobs_lock:
        return [_job_public(k, v) for k, v in sorted(
            _jobs.items(), key=lambda kv: kv[1]["started"], reverse=True)]


def _start_job(job_id: str, args: list[str]) -> bool:
    with _jobs_lock:
        if job_id in _jobs and _jobs[job_id]["running"]:
            return False
        _jobs[job_id] = {
            "cmd": " ".join(args[1:]), "running": True, "code": None,
            "started": time.time(), "finished": None,
            "lines": [], "event": threading.Event(),
        }
        job = _jobs[job_id]

    def emit(text: str) -> None:
        with _jobs_lock:
            job["lines"].append(text)
            del job["lines"][:-MAX_LINES]
            # Everyone waiting on this job wakes; a fresh Event replaces it so
            # the next line can be waited on in turn.
            job["event"].set()
            job["event"] = threading.Event()

    def worker():
        JOB_LOGS.mkdir(parents=True, exist_ok=True)
        log = JOB_LOGS / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', job_id)}.log"
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(args)}\n")
                p = subprocess.Popen(
                    args, cwd=str(ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
                assert p.stdout is not None
                for line in p.stdout:
                    line = _plain(line).rstrip("\n")
                    fh.write(line + "\n")
                    fh.flush()
                    emit(line)
                code = p.wait()
        except Exception as e:                    # noqa: BLE001 - shown to the operator
            code = -1
            emit(str(e))
        with _jobs_lock:
            job["running"] = False
            job["code"] = code
            job["finished"] = time.time()
            job["event"].set()
            job["event"] = threading.Event()
        # A finished build changes both of these; do not make the operator wait
        # out the timer to see it.
        CACHE_FACT.refresh_soon()
        READY_FACT.refresh_soon()

    threading.Thread(target=worker, daemon=True).start()
    return True


def _live_args(rest: list[str]) -> list[str]:
    return [sys.executable, str(LIVE), *rest] if LIVE.exists() else ["false"]


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """A single path component, or "" if the input tried to be more than one."""
    name = unquote(name or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return ""
    if name in (".", "..") or "\x00" in name:
        return ""
    return name


def _media_list(kind: str) -> list[dict]:
    spec = MEDIA_KINDS[kind]
    base: Path = spec["dir"]
    items = []
    if not base.is_dir():
        return items
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(base)
        items.append({
            # Audio is organised one folder per reciter, so the path relative to
            # the pool -- not the bare filename -- is what identifies a file.
            "name": str(rel),
            "folder": str(rel.parent) if rel.parent != Path(".") else "",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return items


def _media_folders(kind: str) -> list[str]:
    base: Path = MEDIA_KINDS[kind]["dir"]
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def _disk() -> dict:
    try:
        du = shutil.disk_usage(str(ROOT))
        return {"total": du.total, "used": du.used, "free": du.free, "floor": DISK_FLOOR}
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "floor": DISK_FLOOR}


class Handler(BaseHTTPRequestHandler):
    server_version = "quran-live-panel"

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write("[panel] %s\n" % (fmt % args))

    # -- helpers ----------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        addr = self.client_address[0]
        if _throttled(addr):
            return False
        sent = self.headers.get("X-Token", "")
        if not sent:
            sent = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        # Constant time: a plain == leaks the token prefix by prefix to anyone
        # who can time the response.
        if hmac.compare_digest(sent, TOKEN):
            return True
        _note_failure(addr)
        sys.stderr.write("[panel] bad token from %s\n" % addr)
        return False

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authed():
            return self._err(401, "token required")
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._page()

        # Cheap: config files, small JSON in state/, one systemctl per channel.
        if path == "/api/overview":
            chans = _channels()
            for c in chans:
                c["status"] = _channel_status(c["name"])
            ready = READY_FACT.read()
            return self._json({
                "channels": chans,
                "disk": _disk(),
                "jobs": _jobs_snapshot(),
                "readiness": ready["value"] or {},
                "readiness_at": ready["at"],
                "server_time": time.time(),
            })

        if path == "/api/cache":
            f = CACHE_FACT.read()
            if query.get("refresh"):
                CACHE_FACT.refresh_soon()
            return self._json({"sizes": f["value"] or {}, "at": f["at"],
                               "stale": f["stale"], "error": f["error"]})

        m = re.fullmatch(r"/api/media/(\w+)", path)
        if m:
            kind = m.group(1)
            if kind not in MEDIA_KINDS:
                return self._err(400, "unknown media kind")
            return self._json({
                "kind": kind,
                "files": _media_list(kind),
                "folders": _media_folders(kind),
                "ext": sorted(MEDIA_KINDS[kind]["ext"]),
            })

        m = re.fullmatch(r"/api/jobs/([\w:.-]+)/events", path)
        if m:
            return self._job_events(m.group(1), query)

        m = re.fullmatch(r"/api/logs/([\w.-]+)", path)
        if m:
            n = query.get("n", ["200"])[0]
            n = n if n.isdigit() else "200"
            code, out = _run(_live_args(["logs", m.group(1), "-n", n]), timeout=20)
            return self._json({"code": code, "output": out})

        return self._err(404, "not found")

    def _page(self):
        page = WEB / "index.html"
        if not page.exists():
            return self._err(500, "index.html missing")
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is self-contained: no CDN, no inline event handlers beyond
        # its own script block. Saying so stops an injected string from ever
        # loading anything remote.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; form-action 'none'; base-uri 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _job_events(self, job_id: str, query: dict):
        """Server-sent events: one message per line of job output.

        The client reconnects with Last-Event-ID (or ?from=) and receives only
        what it has not seen, so a dropped tunnel does not replay an hour of
        build output.
        """
        with _jobs_lock:
            if job_id not in _jobs:
                return self._err(404, "no such job")

        start = self.headers.get("Last-Event-ID") or query.get("from", ["0"])[0]
        cursor = int(start) if str(start).isdigit() else 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(event: str, data: str, ident: int | None = None) -> None:
            chunk = ""
            if ident is not None:
                chunk += f"id: {ident}\n"
            chunk += f"event: {event}\n"
            for line in data.split("\n"):
                chunk += f"data: {line}\n"
            self.wfile.write((chunk + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            while True:
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job is None:
                        return
                    lines = job["lines"]
                    # Lines are dropped from the head once MAX_LINES is passed,
                    # so a cursor from before the trim would re-read the wrong
                    # ones; clamp it to what is still held.
                    total = len(lines)
                    cursor = min(max(cursor, 0), total)
                    pending = lines[cursor:]
                    running = job["running"]
                    code = job["code"]
                    waiter = job["event"]
                for line in pending:
                    cursor += 1
                    send("line", line, cursor)
                if not running:
                    send("end", json.dumps({"code": code}))
                    return
                if not pending:
                    # A comment keeps an idle tunnel from being closed by a
                    # proxy, and surfaces a dead client as a write error.
                    if not waiter.wait(15):
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        if not self._authed():
            return self._err(401, "token required")
        path = urlparse(self.path).path

        m = re.fullmatch(r"/api/channel/([\w.-]+)/(start|stop|restart|enable|disable)", path)
        if m:
            name, action = m.group(1), m.group(2)
            code, out = _run(_live_args([action, name]), timeout=60)
            return self._json({"code": code, "output": out})

        m = re.fullmatch(r"/api/channel/([\w.-]+)/(build|sync|validate)", path)
        if m:
            name, action = m.group(1), m.group(2)
            job_id = f"{action}:{name}"
            started = _start_job(job_id, _live_args([action, name]))
            return self._json({"id": job_id, "started": started,
                               "note": "" if started else "already running"})

        m = re.fullmatch(r"/api/media/(\w+)/(.+)", path)
        if m:
            return self._upload(m.group(1), m.group(2))

        return self._err(404, "not found")

    def do_DELETE(self):
        if not self._authed():
            return self._err(401, "token required")
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/media/(\w+)/(.+)", path)
        if not m:
            return self._err(404, "not found")
        kind, rel = m.group(1), unquote(m.group(2))
        if kind not in MEDIA_KINDS:
            return self._err(400, "unknown media kind")
        base: Path = MEDIA_KINDS[kind]["dir"]
        try:
            target = (base / rel).resolve()
            target.relative_to(base.resolve())
        except (ValueError, OSError):
            return self._err(400, "path outside the media pool")
        if not target.is_file():
            return self._err(404, "no such file")
        try:
            target.unlink()
        except OSError as e:
            return self._err(500, str(e))
        READY_FACT.refresh_soon()
        # Rendered tracks made from this file are now stale, but clearing them
        # is the operator's call: cache/ is expensive to rebuild.
        return self._json({"deleted": rel, "note": "cached tracks using this file are now stale"})

    # -- upload -----------------------------------------------------------
    def _upload(self, kind: str, rel: str):
        if kind not in MEDIA_KINDS:
            return self._err(400, "unknown media kind")
        spec = MEDIA_KINDS[kind]
        base: Path = spec["dir"]

        rel = unquote(rel)
        parts = [p for p in rel.split("/") if p]
        # One optional subfolder: assets/audio is organised per reciter.
        if len(parts) > 2 or not parts:
            return self._err(400, "expected <file> or <folder>/<file>")
        if any(_safe_name(p) == "" for p in parts):
            return self._err(400, "invalid name")

        suffix = Path(parts[-1]).suffix.lower()
        if suffix not in spec["ext"]:
            return self._err(400, f"{kind} accepts {sorted(spec['ext'])}")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._err(400, "bad Content-Length")
        if length <= 0:
            return self._err(400, "empty body")
        if length > MAX_UPLOAD:
            return self._err(413, "file too large")

        if shutil.disk_usage(str(ROOT)).free - length < DISK_FLOOR:
            return self._err(507, "not enough free disk")

        target = base.joinpath(*parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._err(500, str(e))

        # Written beside the destination and renamed, so a dropped connection
        # never leaves a half file for the renderer to pick up.
        tmp = target.with_name(target.name + ".part")
        got = 0
        try:
            with tmp.open("wb") as fh:
                while got < length:
                    chunk = self.rfile.read(min(1 << 20, length - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            if got != length:
                tmp.unlink(missing_ok=True)
                return self._err(400, "upload truncated")
            tmp.replace(target)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return self._err(500, str(e))

        READY_FACT.refresh_soon()
        return self._json({"saved": str(target.relative_to(ROOT)), "size": got})


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, _plain((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except FileNotFoundError as e:
        return 127, str(e)


def main() -> int:
    host = os.environ.get("QURAN_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("QURAN_WEB_PORT", "8770"))

    if host not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        sys.stderr.write(
            "Refusing to bind %s without QURAN_WEB_TOKEN: this panel starts and\n"
            "stops live broadcasts and writes into the asset pools.\n"
            "Either keep the default 127.0.0.1 and tunnel:\n"
            "    ssh -N -L %d:127.0.0.1:%d <host>\n"
            "or set QURAN_WEB_TOKEN to a long random string.\n" % (host, port, port)
        )
        return 2

    for fact in (CACHE_FACT, READY_FACT):
        threading.Thread(target=fact.run, daemon=True).start()

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("[panel] http://%s:%d  (root: %s)\n" % (host, port, ROOT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
