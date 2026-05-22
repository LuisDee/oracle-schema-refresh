"""Server-side job state — ``oracdb$jobs`` and ``oracdb$tables`` on the target.

See ``docs/redesign.md`` §2.5. State lives on the target endpoint so that:
- the CLI is stateless (kill it, move machines, resume),
- agents can poll ``status`` from anywhere with target access,
- ``DBMS_PARALLEL_EXECUTE`` chunk state can be ``UNION``-ed in once Cut 2 lands.
"""
from __future__ import annotations

from oracle_schema_refresh.state.job import (
    Job,
    drop_job,
    get_job,
    insert_job,
    list_jobs,
    lock_job,
    update_job_status,
)
from oracle_schema_refresh.state.jobid import job_id_new
from oracle_schema_refresh.state.schema import ensure_schema
from oracle_schema_refresh.state.table_record import (
    TableRecord,
    get_table_records,
    insert_table_records,
    update_table_record,
)

__all__ = [
    "Job",
    "TableRecord",
    "drop_job",
    "ensure_schema",
    "get_job",
    "get_table_records",
    "insert_job",
    "insert_table_records",
    "job_id_new",
    "list_jobs",
    "lock_job",
    "update_job_status",
    "update_table_record",
]


# Valid status values; kept here so the CLI, engine, and state module
# all agree.
JOB_STATUSES = frozenset({"PLANNED", "RUNNING", "DONE", "FAILED", "CANCELLED"})
TABLE_STATUSES = frozenset(
    {"PLANNED", "RUNNING", "DONE", "FAILED", "SKIPPED"}
)
TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "CANCELLED"})
