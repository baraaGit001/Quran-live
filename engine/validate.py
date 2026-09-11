"""Pre-flight checks: everything that must be true before a channel can go live."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config as cfg_mod
from . import overlay as overlay_mod
from .ffmpeg import ffmpeg_bin, ffprobe_bin

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "ok", "warn", "fail"


def _row(status, label, detail=""):
    return {"status": status, "label": label, "detail": detail}


def check_system() -> list[dict]:
    rows = []
    ff = ffmpeg_bin()
    rows.append(_row(OK, "ffmpeg", ff) if ff else
                _row(FAIL, "ffmpeg", "not found (see scripts/install-ffmpeg.sh)"))
    fp = ffprobe_bin()
    rows.append(_row(OK, "ffprobe", fp) if fp else _row(WARN, "ffprobe", "not found"))

    backend = overlay_mod.available_backend()
    rows.append(_row(OK, "arabic text backend", backend) if backend else
                _row(FAIL, "arabic text backend",
                     "none of pango-view / ImageMagick / Pillow+reshaper available"))

    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024 ** 3)
    rows.append(_row(OK if free_gb > 5 else WARN, "disk free", f"{free_gb:.1f} GB"))

    try:
        load1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        rows.append(_row(
            OK if load1 < cores else WARN,
            "load average",
            f"{load1:.2f} over {cores} core(s)",
        ))
    except OSError:
        pass

    writable = os.access(ROOT / "cache", os.W_OK) and os.access(ROOT / "state", os.W_OK)
    rows.append(_row(OK, "cache/state writable") if writable else
                _row(FAIL, "cache/state writable", "permission denied"))
    return rows


def check_channel(name: str) -> list[dict]:
    rows = []
    try:
        cfg = cfg_mod.load_channel(name)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator verbatim
        return [_row(FAIL, f"{name}: config", str(exc))]

    rows.append(_row(OK, f"{name}: config", Path(cfg["_path"]).name))
    rows.append(_row(OK if cfg.get("enabled", True) else WARN, f"{name}: enabled",
                     str(cfg.get("enabled", True))))

    audio = cfg_mod.audio_pool(cfg)
    rows.append(_row(OK if audio else FAIL, f"{name}: audio files",
                     f"{len(audio)} in {cfg['audio_dir']}"))

    images, videos = cfg_mod.visual_pool(cfg)
    total = len(images) + len(videos)
    rows.append(_row(OK if total else FAIL, f"{name}: visual files",
                     f"{len(images)} image(s), {len(videos)} video(s)"))

    font = cfg.get("font")
    if cfg.get("overlay", {}).get("enabled", True):
        rows.append(_row(OK, f"{name}: font", str(font)) if font and Path(font).exists()
                    else _row(FAIL, f"{name}: font", f"missing: {font}"))

    # Presence only -- the value is never printed.
    has_key = bool(cfg_mod.stream_key(cfg))
    rows.append(_row(OK if has_key else FAIL, f"{name}: stream key",
                     f"{cfg['stream_key_env']} " + ("is set" if has_key else "is NOT set")))
    return rows


def run(channels: list[str] | None = None) -> tuple[list[dict], bool]:
    rows = check_system()
    for name in (channels if channels is not None else cfg_mod.list_channels()):
        rows.extend(check_channel(name))
    return rows, not any(r["status"] == FAIL for r in rows)
