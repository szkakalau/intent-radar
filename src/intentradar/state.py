"""Seen-post persistence (``data/state.json``).

Shape: ``{project_name: [reddit_id, ...]}``. Keeps the last 2000 ids per project
so a post is never pushed twice. Atomic writes; never raises on a corrupt file
(a fresh start is strictly better than losing a run).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

MAX_IDS_PER_PROJECT = 2000


class StateStore:
    """Read/write the set of already-reported post ids per project."""

    def __init__(self, path: Path) -> None:
        """Initialise with the path to ``state.json``."""
        self.path = Path(path)
        self._data: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        """Load from disk, tolerating a missing or corrupt file."""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("state file unreadable (%s), starting empty", exc)
            return {}
        if not isinstance(raw, dict):
            log.warning("state file malformed, starting empty")
            return {}
        return {str(k): [str(i) for i in v] for k, v in raw.items() if isinstance(v, list)}

    def get_seen(self, project: str) -> set[str]:
        """Return the ids already reported for ``project``."""
        return set(self._data.get(project, []))

    def mark_seen(self, project: str, ids: Iterable[str]) -> None:
        """Merge ``ids`` into the project's seen list and persist (capped)."""
        seen = self._data.get(project, [])
        merged = list(dict.fromkeys([*seen, *[str(i) for i in ids]]))
        self._data[project] = merged[-MAX_IDS_PER_PROJECT:]
        self._persist()

    def clear(self, project: str | None = None) -> None:
        """Drop one project's history, or all of them."""
        if project is None:
            self._data = {}
        else:
            self._data.pop(project, None)
        self._persist()

    def _persist(self) -> None:
        """Atomically write state.json."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        import os

        os.replace(tmp, self.path)
