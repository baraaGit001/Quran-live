#!/usr/bin/env python3
"""Control panel for quran-live: channels, visuals and recitations.

Standard library only -- the box runs two web apps already and this is a small
operator tool, not another dependency to keep alive.

It binds to 127.0.0.1 by default and is meant to be reached over an SSH tunnel:

    ssh -N -L 8770:127.0.0.1:8770 pricelens

Binding anywhere else requires QURAN_WEB_TOKEN to be set, because every action
here spends CPU, writes into the asset pools or restarts a live broadcast.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
LIVE = ROOT / "bin" / "live"

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

TOKEN = os.environ.get("QURAN_WEB_TOKEN", "")

# Long operations (build, sync) outlive a request, so they run detached and the
# page polls this instead of holding a connection open for an hour.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _safe_name(name: str) -> str:
    """A single path component, or "" if the input tried to be more than one."""
    name = unquote(name or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return ""
    if name in (".", "..") or "\x00" in name:
        return ""
    return name


# bin/live colours its output for a terminal; the page is not one.
ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _plain(text: str) -> str:
    return ANSI.sub("", text)


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


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


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


def _status_text() -> str:
    if not LIVE.exists():
        return "bin/live not found"
    code, out = _run([sys.executable, str(LIVE), "status"], timeout=20)
    if code == 127:
        code, out = _run([str(LIVE), "status"], timeout=20)
    return out.strip() or "(no output)"


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
        items.append({
            # Audio is organised one folder per reciter, so the path relative to
            # the pool -- not the bare filename -- is what identifies a file.
            "name": str(p.relative_to(base)),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return items


def _state() -> dict:
    cache = ROOT / "cache"
    try:
        du = shutil.disk_usage(str(ROOT))
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = {"total": 0, "used": 0, "free": 0}
    return {
        "channels": _channels(),
        "status": _status_text(),
        "media": {k: _media_list(k) for k in MEDIA_KINDS},
        "cache": {
            d.name: _dir_size(d)
            for d in sorted(cache.iterdir()) if d.is_dir()
        } if cache.is_dir() else {},
        "disk": disk,
        "jobs": _jobs_snapshot(),
    }


def _jobs_snapshot() -> list[dict]:
    with _jobs_lock:
        return [
            {"id": k, "cmd": v["cmd"], "running": v["running"],
             "code": v["code"], "tail": v["out"][-4000:]}
            for k, v in sorted(_jobs.items())
        ]


def _start_job(job_id: str, args: list[str]) -> None:
    """Run a long command in the background, keeping its output for polling."""
    with _jobs_lock:
        if job_id in _jobs and _jobs[job_id]["running"]:
            return
        _jobs[job_id] = {"cmd": " ".join(args[1:]), "running": True, "code": None, "out": ""}

    def worker():
        try:
            p = subprocess.Popen(
                args, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            assert p.stdout is not None
            for line in p.stdout:
                with _jobs_lock:
                    # Bounded: a full build prints for hours and this is only
                    # ever shown as a tail.
                    _jobs[job_id]["out"] = (_jobs[job_id]["out"] + _plain(line))[-20000:]
            code = p.wait()
        except Exception as e:  # noqa: BLE001 - surfaced to the operator
            code = -1
            with _jobs_lock:
                _jobs[job_id]["out"] += f"\n{e}\n"
        with _jobs_lock:
            _jobs[job_id]["running"] = False
            _jobs[job_id]["code"] = code

    threading.Thread(target=worker, daemon=True).start()


def _live_args(rest: list[str]) -> list[str]:
    return [sys.executable, str(LIVE), *rest] if LIVE.exists() else ["false"]


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
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        sent = self.headers.get("X-Token", "")
        if not sent:
            sent = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return sent == TOKEN

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authed():
            return self._err(401, "token required")
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            page = WEB / "index.html"
            if not page.exists():
                return self._err(500, "index.html missing")
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if path == "/api/state":
            return self._json(_state())

        if path == "/api/jobs":
            return self._json({"jobs": _jobs_snapshot()})

        m = re.fullmatch(r"/api/logs/([\w.-]+)", path)
        if m:
            n = parse_qs(parsed.query).get("n", ["120"])[0]
            n = n if n.isdigit() else "120"
            code, out = _run(_live_args(["logs", m.group(1), "-n", n]), timeout=20)
            return self._json({"code": code, "output": out})

        return self._err(404, "not found")

    def do_POST(self):
        if not self._authed():
            return self._err(401, "token required")
        path = urlparse(self.path).path

        m = re.fullmatch(r"/api/channel/([\w.-]+)/(start|stop|restart|enable|disable)", path)
        if m:
            name, action = m.group(1), m.group(2)
            code, out = _run(_live_args([action, name]), timeout=60)
            return self._json({"code": code, "output": out})

        # build and sync take as long as the recitation itself, so they are
        # jobs, not requests.
        m = re.fullmatch(r"/api/channel/([\w.-]+)/(build|sync|validate)", path)
        if m:
            name, action = m.group(1), m.group(2)
            _start_job(f"{action}:{name}", _live_args([action, name]))
            return self._json({"started": f"{action} {name}"})

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

        free = shutil.disk_usage(str(ROOT)).free
        # The streams share this disk with two web apps; leave the same 10 GB
        # floor the cache respects.
        if free - length < 10 * 1024 * 1024 * 1024:
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

        return self._json({"saved": str(target.relative_to(ROOT)), "size": got})


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

    mimetypes.init()
    srv = ThreadingHTTPServer((host, port), Handler)
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
