"""Channel configuration loading and validation.

Channel files are data, not code: adding a channel means adding a YAML file and
an env var, never editing this package.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = ROOT / "channels"
DEFAULTS_FILE = ROOT / "config" / "defaults.yaml"

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm"}

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class ConfigError(Exception):
    """Raised for a channel file that cannot be used as-is."""


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _safe_path(raw: str, label: str) -> Path:
    """Resolve a config path and refuse anything outside the project root.

    Channel files are the one place a typo (or a copied-in path) could point the
    engine at arbitrary parts of the filesystem, so containment is enforced here
    rather than trusted.
    """
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        raise ConfigError(f"{label} must stay inside {ROOT} (got '{raw}')") from None
    return resolved


def load_defaults() -> dict:
    if not DEFAULTS_FILE.exists():
        return {}
    return yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8")) or {}


def load_channel(name: str) -> dict:
    if not _NAME_RE.match(name):
        raise ConfigError(f"Invalid channel name '{name}'")
    path = CHANNELS_DIR / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"No channel config at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _deep_merge(load_defaults(), raw)
    cfg["name"] = raw.get("name", name)
    cfg["_path"] = str(path)

    for key in ("audio_dir", "visual_dir"):
        if not cfg.get(key):
            raise ConfigError(f"{name}: '{key}' is required")
        cfg[key] = _safe_path(cfg[key], key)

    if cfg.get("font"):
        cfg["font"] = _safe_path(cfg["font"], "font")

    if not cfg.get("stream_key_env"):
        raise ConfigError(f"{name}: 'stream_key_env' is required")

    return cfg


def list_channels() -> list[str]:
    if not CHANNELS_DIR.exists():
        return []
    return sorted(p.stem for p in CHANNELS_DIR.glob("*.yaml"))


def stream_key(cfg: dict) -> str | None:
    """Read the channel's key from the environment. Never logged, never stored."""
    return os.environ.get(cfg["stream_key_env"]) or None


def scan_pool(directory: Path, extensions: set[str]) -> list[Path]:
    """Every matching file under `directory`, rescanned on each call.

    Rescanning per call is what makes dropping a new file into a pool enough --
    the next selection cycle sees it with no restart and no config edit.
    """
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions
    )


def audio_pool(cfg: dict) -> list[Path]:
    return scan_pool(cfg["audio_dir"], AUDIO_EXT)


def visual_pool(cfg: dict) -> tuple[list[Path], list[Path]]:
    base = cfg["visual_dir"]
    return scan_pool(base, IMAGE_EXT), scan_pool(base, VIDEO_EXT)
