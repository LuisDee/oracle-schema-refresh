"""Local jobs index — ``~/.oracdb/jobs.json`` mapping ``job_id → target``.

The index lets per-job commands (``status``, ``run``, ``verify``,
``cancel``, ``cleanup``, ``wait``, ``logs``) default ``--target`` from
the local file instead of forcing the user to remember which endpoint
they planned against.

The index is **advisory only** — the canonical state lives on the
target DB. Deleting the index never loses a job; it just means the
user has to pass ``--target NAME`` explicitly.

Stored as JSON ``{job_id: {"target": "...", "source": "...",
"created_at": "..."}}``. File permissions ``0o600``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def default_home() -> Path:
    """Return ``$ORACDB_HOME`` if set, else ``~/.oracdb``."""
    env = os.environ.get("ORACDB_HOME")
    return Path(env) if env else Path.home() / ".oracdb"


def default_index_path() -> Path:
    return default_home() / "jobs.json"


class JobsIndex:
    """Loaded on demand; ``save()`` is explicit."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_index_path()
        self._entries: dict[str, dict[str, str]] = {}

    def load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        raw = json.loads(self.path.read_text() or "{}")
        # Accept either the rich shape or a legacy {jid: "target"} form.
        out: dict[str, dict[str, str]] = {}
        for jid, value in raw.items():
            if isinstance(value, str):
                out[jid] = {"target": value, "source": ""}
            else:
                out[jid] = value
        self._entries = out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2, sort_keys=True))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Best-effort; some FSes (e.g. Windows / WSL crossover) don't
            # support chmod. The index isn't a secret — registry is.
            pass

    def add(
        self, job_id: str, *, target: str, source: str | None = None
    ) -> None:
        self._entries[job_id] = {
            "target": target,
            "source": source or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def remove(self, job_id: str) -> None:
        self._entries.pop(job_id, None)

    def target_for(self, job_id: str) -> str | None:
        entry = self._entries.get(job_id)
        if entry is None:
            return None
        return entry.get("target")
