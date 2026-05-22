"""Job-id generator.

Format: ``j_<YYYYMMDD>_<HHMMSS>_<rand4>``. Sortable chronologically,
human-readable, fits comfortably in a ``VARCHAR2(32)`` column.

§7 q3 resolution.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone


def job_id_new(now: datetime | None = None) -> str:
    """Generate a fresh job id. UTC timestamp + 4 random hex chars."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)  # 4 hex chars
    return f"j_{ts}_{rand}"
