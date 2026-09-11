"""Randomised, non-repeating selection over a pool that can change at any time."""
from __future__ import annotations

import json
import random
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"


class Selector:
    """Picks from a pool while avoiding anything in a recent-play window.

    The pool is passed in fresh on every pick rather than captured once, so
    files added while the stream is live enter the rotation on the next pick.
    History is persisted so a restart does not immediately replay whatever was
    playing before it.
    """

    def __init__(self, channel: str, kind: str, history_size: int = 10):
        self.channel = channel
        self.kind = kind
        self.history_size = max(0, history_size)
        self._state_file = STATE_DIR / f"{channel}.{kind}.history.json"
        self._history: deque[str] = deque(maxlen=self.history_size or 1)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            for item in data.get("history", [])[-self._history.maxlen:]:
                self._history.append(item)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"history": list(self._history)}), encoding="utf-8"
            )
        except OSError:
            pass

    def pick(self, pool: list[Path]) -> Path | None:
        if not pool:
            return None
        if len(pool) == 1:
            choice = pool[0]
        else:
            recent = set(self._history)
            fresh = [p for p in pool if str(p) not in recent]
            # When history covers the whole (small) pool, fall back to anything
            # except the track that just played -- never a hard repeat.
            if not fresh:
                last = self._history[-1] if self._history else None
                fresh = [p for p in pool if str(p) != last] or list(pool)
            choice = random.choice(fresh)

        if self.history_size:
            self._history.append(str(choice))
            self._save()
        return choice
