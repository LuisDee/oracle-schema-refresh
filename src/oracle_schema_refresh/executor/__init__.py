"""DBMS_PARALLEL_EXECUTE wrapper."""
from __future__ import annotations

from oracle_schema_refresh.executor.dpe import run_parallel_task

__all__ = ["run_parallel_task"]
