"""Strategy auto-selection from ``TableProfile``.

Thresholds (Cut 2 defaults, easily tunable):

* < 250 MB and < 1M rows   → ``direct_copy`` (no setup overhead).
* < 2 GB and < 50M rows    → ``parallel_dml`` (one statement, PARALLEL hint).
* ≥ 2 GB or ≥ 50M rows     → ``chunked_staging`` (DBMS_PARALLEL_EXECUTE).
* LOB-heavy at any size    → ``parallel_dml`` (LOB shipping is the
  bottleneck; chunking gains little).
* LONG columns at any size → refuse — LONG can't cross a dblink.

§7 q4: chunk-size unit is **MB** (target ~256 MB / chunk). Row-based
sizing is unreliable with variable-width rows.
"""
from __future__ import annotations

from oracle_schema_refresh.introspect import TableProfile
from oracle_schema_refresh.strategy.base import KNOWN_STRATEGIES

DIRECT_COPY_MAX_MB: float = 250.0
DIRECT_COPY_MAX_ROWS: int = 1_000_000
PARALLEL_DML_MAX_MB: float = 2048.0
PARALLEL_DML_MAX_ROWS: int = 50_000_000


def pick_strategy(
    profile: TableProfile,
    *,
    supports_dpe: bool,
    override: str | None = None,
) -> str:
    """Return the strategy name to use for this table.

    Args:
        profile: introspection result.
        supports_dpe: True iff the target session has
            ``EXECUTE ON DBMS_PARALLEL_EXECUTE`` + ``CREATE JOB``.
            When False, ``chunked_staging`` is silently downgraded to
            ``parallel_dml``.
        override: explicit strategy from ``--strategy``. Must be one of
            ``KNOWN_STRATEGIES``. ``"auto"`` falls through to the
            normal heuristic. Raises ``ValueError`` for unknown names
            or for tables with LONG columns (regardless of override).

    Returns:
        The strategy name. Always one of ``KNOWN_STRATEGIES``.
    """
    if profile.has_long:
        raise ValueError(
            f"table {profile.schema}.{profile.table_name} has LONG columns; "
            "LONG cannot be selected over a dblink and chunked strategies "
            "won't work either. Drop or convert the LONG column first."
        )

    if override is not None and override != "auto":
        if override not in KNOWN_STRATEGIES:
            raise ValueError(f"unknown strategy {override!r}")
        return override

    # LOB-heavy: chunking buys little; the dblink is the bottleneck.
    if profile.has_lob:
        return "parallel_dml"

    small = (
        profile.size_mb < DIRECT_COPY_MAX_MB
        and profile.rows < DIRECT_COPY_MAX_ROWS
    )
    if small:
        return "direct_copy"

    medium = (
        profile.size_mb < PARALLEL_DML_MAX_MB
        and profile.rows < PARALLEL_DML_MAX_ROWS
    )
    if medium or not supports_dpe:
        return "parallel_dml"

    return "chunked_staging"
