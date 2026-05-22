"""``oracdb$tables`` row model and accessors."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class TableRecord:
    job_id: str
    table_name: str
    status: str
    strategy: str = "direct_copy"
    rows_source: int | None = None
    rows_target: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
    sequences_reset: str | None = None  # JSON-encoded list


def insert_table_records(conn: Any, records: list[TableRecord]) -> None:
    if not records:
        return
    sql = """
    INSERT INTO oracdb$tables (
      job_id, table_name, strategy, status
    ) VALUES (
      :job_id, :table_name, :strategy, :status
    )
    """
    payload = [
        {
            "job_id": r.job_id,
            "table_name": r.table_name,
            "strategy": r.strategy,
            "status": r.status,
        }
        for r in records
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, payload)


def update_table_record(
    conn: Any,
    job_id: str,
    table_name: str,
    *,
    status: str | None = None,
    rows_source: int | None = None,
    rows_target: int | None = None,
    error: str | None = None,
    sequences_reset: str | None = None,
    mark_started: bool = False,
    mark_ended: bool = False,
) -> None:
    """Patch a single ``oracdb$tables`` row. Only non-None fields are set."""
    sets: list[str] = []
    params: dict[str, Any] = {"job_id": job_id, "table_name": table_name}
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if rows_source is not None:
        sets.append("rows_source = :rows_source")
        params["rows_source"] = rows_source
    if rows_target is not None:
        sets.append("rows_target = :rows_target")
        params["rows_target"] = rows_target
    if error is not None:
        sets.append("error = :error")
        params["error"] = error
    if sequences_reset is not None:
        sets.append("sequences_reset = :sequences_reset")
        params["sequences_reset"] = sequences_reset
    if mark_started:
        sets.append("started_at = SYSTIMESTAMP")
    if mark_ended:
        sets.append("ended_at = SYSTIMESTAMP")
    if not sets:
        return
    sql = (
        "UPDATE oracdb$tables SET "
        + ", ".join(sets)
        + " WHERE job_id = :job_id AND table_name = :table_name"
    )
    with conn.cursor() as cur:
        cur.execute(sql, **params)


def get_table_records(conn: Any, job_id: str) -> list[TableRecord]:
    sql = """
    SELECT job_id, table_name, status, rows_source, rows_target,
           error, started_at, ended_at
      FROM oracdb$tables
     WHERE job_id = :job_id
     ORDER BY table_name
    """
    with conn.cursor() as cur:
        cur.execute(sql, job_id=job_id)
        rows = cur.fetchall()
    return [
        TableRecord(
            job_id=row[0],
            table_name=row[1],
            status=row[2],
            rows_source=row[3],
            rows_target=row[4],
            error=row[5],
            started_at=row[6],
            ended_at=row[7],
        )
        for row in rows
    ]
