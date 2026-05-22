"""Strategy Protocol + shared types.

A strategy is a stateless object: ``load(ctx)`` takes a
``StrategyContext`` (connections, schemas, scn, dblink, parallelism
budget) and returns a ``StrategyResult`` (rows loaded, sequences
reset, etc.). The engine's phase 4 calls it once per table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

KNOWN_STRATEGIES: frozenset[str] = frozenset(
    {"direct_copy", "parallel_dml", "chunked_staging", "partition_exchange"}
)


@dataclass
class StrategyContext:
    """Everything a Strategy needs to do its job.

    Connections: ``source_conn`` is for source-side metadata reads
    only; data movement runs server-side on ``target_conn`` (via the
    dblink when cross-host).
    """

    source_conn: Any
    target_conn: Any
    source_schema: str
    target_schema: str
    table: str
    scn: int | None
    dblink: str | None
    insert_hint: str = "/*+ APPEND */"
    max_parallel: int = 4
    max_chunks_per_table: int = 4
    # Best-effort: target column list to use. None → strategy queries.
    columns: list[str] | None = None


@dataclass
class StrategyResult:
    """What a Strategy reports after loading one table."""

    rows_source: int = 0
    rows_target: int = 0
    sequences_reset: list[str] = field(default_factory=list)
    chunks_used: int = 1


class Strategy(Protocol):
    """Stateless loader for one table.

    Implementations live in ``strategy/direct_copy.py``,
    ``strategy/parallel_dml.py``, ``strategy/chunked_staging.py``.
    """

    name: str

    def load(self, ctx: StrategyContext) -> StrategyResult: ...
