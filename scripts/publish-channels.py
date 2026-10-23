#!/usr/bin/env python3
"""Write site/channels.json: the reciter list the website offers.

Adding a reciter should be dropping mp3s in a folder and copying a channel
file -- not editing HTML. This turns the channel configs, which are already the
single source of truth for what exists, into the list the page reads.

Run as ExecStartPre of every channel unit, so the list is rebuilt whenever any
channel starts and never drifts from the configs. It is cheap and idempotent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config as cfg_mod  # noqa: E402


def main() -> int:
    entries = []
    for name in cfg_mod.list_channels():
        cfg = cfg_mod.load_channel(name)
        if not cfg.get("enabled", True):
            continue
        # Only a channel that publishes HLS has anything for the site to play;
        # one pushing to a platform has no local playlist to point at.
        stream = cfg.get("stream", {}) or {}
        if str(stream.get("platform", "youtube")).lower() != "hls":
            continue
        entries.append({
            "id": cfg["name"],
            # overlay.text is the fallback because it already held the reciter's
            # name on every channel written before `reciter` existed.
            "reciter": cfg.get("reciter")
                       or (cfg.get("overlay", {}) or {}).get("text")
                       or cfg["name"],
            "style": cfg.get("style", ""),
        })

    out = ROOT / "site" / "channels.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Through a temporary file: the page polls this, and a half-written array
    # is a parse error that would empty the picker.
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(out)
    # The web server reads this as another user, inside a container.
    out.chmod(0o644)
    print(f"{out}: {len(entries)} channel(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
