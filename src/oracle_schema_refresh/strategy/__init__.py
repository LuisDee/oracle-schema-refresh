"""Strategy interface and registry for table-load strategies.

A ``Strategy`` is responsible for loading one table's data from source
to target. The engine drives table introspection and FK ordering; the
strategy decides how to actually move the rows.

Cut 2 ships three:

* ``direct_copy``     — one ``INSERT … SELECT``. Cut 1b behaviour.
* ``parallel_dml``    — one ``INSERT /*+ APPEND PARALLEL(N) */``.
* ``chunked_staging`` — N staging tables loaded in parallel via
  ``DBMS_PARALLEL_EXECUTE``, then a single merge into the target.

``partition_exchange`` is opt-in via explicit ``--strategy`` flag and
**not** chosen by ``auto`` — see ``docs/redesign.md`` §2.3.
"""
from __future__ import annotations

from oracle_schema_refresh.strategy.base import (
    KNOWN_STRATEGIES,
    Strategy,
    StrategyContext,
    StrategyResult,
)
from oracle_schema_refresh.strategy.picker import pick_strategy

__all__ = [
    "KNOWN_STRATEGIES",
    "Strategy",
    "StrategyContext",
    "StrategyResult",
    "pick_strategy",
]
