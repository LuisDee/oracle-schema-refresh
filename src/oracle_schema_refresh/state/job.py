"""``oracdb$jobs`` row model and accessors."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Job:
    job_id: str
    source_endpoint: str
    target_endpoint: str
    source_schema: str
    target_schema: str
    scn: int | None
    config_json: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


def insert_job(conn: Any, job: Job) -> None:
    sql = """
    INSERT INTO oracdb$jobs (
      job_id, source_endpoint, target_endpoint, source_schema, target_schema,
      scn, config_json, status
    ) VALUES (
      :job_id, :source_endpoint, :target_endpoint, :source_schema, :target_schema,
      :scn, :config_json, :status
    )
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            job_id=job.job_id,
            source_endpoint=job.source_endpoint,
            target_endpoint=job.target_endpoint,
            source_schema=job.source_schema,
            target_schema=job.target_schema,
            scn=job.scn,
            config_json=job.config_json,
            status=job.status,
        )


def get_job(conn: Any, job_id: str) -> Job | None:
    sql = """
    SELECT job_id, source_endpoint, target_endpoint, source_schema,
           target_schema, scn, config_json, status, started_at,
           ended_at, error
      FROM oracdb$jobs
     WHERE job_id = :job_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, job_id=job_id)
        row = cur.fetchone()
    if row is None:
        return None
    config_json = row[6]
    if hasattr(config_json, "read"):
        config_json = config_json.read()
    return Job(
        job_id=row[0],
        source_endpoint=row[1],
        target_endpoint=row[2],
        source_schema=row[3],
        target_schema=row[4],
        scn=row[5],
        config_json=config_json,
        status=row[7],
        started_at=row[8],
        ended_at=row[9],
        error=row[10],
    )


def update_job_status(
    conn: Any,
    job_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    """Set status (and ``error``) and stamp ``started_at`` / ``ended_at``.

    ``started_at`` is stamped on the first transition to RUNNING;
    ``ended_at`` on any transition to a terminal status.
    """
    from oracle_schema_refresh.state import TERMINAL_STATUSES

    started_set = ", started_at = SYSTIMESTAMP" if status == "RUNNING" else ""
    ended_set = ", ended_at = SYSTIMESTAMP" if status in TERMINAL_STATUSES else ""
    sql = (
        "UPDATE oracdb$jobs "
        f"   SET status = :status{started_set}{ended_set}, error = :error "
        " WHERE job_id = :job_id"
    )
    with conn.cursor() as cur:
        cur.execute(sql, status=status, error=error, job_id=job_id)


def lock_job(conn: Any, job_id: str) -> bool:
    """Acquire a row-level lock on the job using ``SELECT … FOR UPDATE
    NOWAIT``. Returns True on success, False if the row doesn't exist.

    Propagates ``oracledb.DatabaseError`` (ORA-00054) if another session
    already holds the lock — the caller surfaces this as
    "another worker is running this job" rather than silently waiting.
    """
    sql = "SELECT job_id FROM oracdb$jobs WHERE job_id = :job_id FOR UPDATE NOWAIT"
    with conn.cursor() as cur:
        cur.execute(sql, job_id=job_id)
        row = cur.fetchone()
    return row is not None


def drop_job(conn: Any, job_id: str) -> None:
    """Delete the job row; the FK CASCADE on ``oracdb$tables`` clears
    the children too."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM oracdb$jobs WHERE job_id = :job_id", job_id=job_id)


def list_jobs(conn: Any, limit: int = 100) -> list[Job]:
    """Return the most recent ``limit`` jobs, newest first."""
    sql = """
    SELECT job_id, source_endpoint, target_endpoint, source_schema,
           target_schema, scn, config_json, status, started_at,
           ended_at, error
      FROM oracdb$jobs
     ORDER BY created_at DESC
     FETCH FIRST :n ROWS ONLY
    """
    with conn.cursor() as cur:
        cur.execute(sql, n=limit)
        rows = cur.fetchall()
    out: list[Job] = []
    for row in rows:
        config_json = row[6]
        if hasattr(config_json, "read"):
            config_json = config_json.read()
        out.append(
            Job(
                job_id=row[0],
                source_endpoint=row[1],
                target_endpoint=row[2],
                source_schema=row[3],
                target_schema=row[4],
                scn=row[5],
                config_json=config_json,
                status=row[7],
                started_at=row[8],
                ended_at=row[9],
                error=row[10],
            )
        )
    return out
