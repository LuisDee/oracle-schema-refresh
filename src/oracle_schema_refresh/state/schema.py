"""DDL for ``oracdb$jobs`` and ``oracdb$tables``.

``ensure_schema`` is idempotent — re-running ``oracdb plan`` against a
freshly-prepared target schema is the common path.
"""
from __future__ import annotations

from typing import Any

import oracledb
import structlog

log = structlog.get_logger()

_DDL_JOBS = """
CREATE TABLE oracdb$jobs (
  job_id          VARCHAR2(32) NOT NULL,
  created_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
  source_endpoint VARCHAR2(128),
  target_endpoint VARCHAR2(128),
  source_schema   VARCHAR2(128),
  target_schema   VARCHAR2(128),
  scn             NUMBER,
  config_json     CLOB,
  status          VARCHAR2(16) NOT NULL,
  started_at      TIMESTAMP,
  ended_at        TIMESTAMP,
  error           VARCHAR2(4000),
  CONSTRAINT pk_oracdb_jobs PRIMARY KEY (job_id),
  CONSTRAINT ck_oracdb_jobs_status CHECK (
    status IN ('PLANNED','RUNNING','DONE','FAILED','CANCELLED')
  )
)
"""

_DDL_TABLES = """
CREATE TABLE oracdb$tables (
  job_id          VARCHAR2(32)  NOT NULL,
  table_name      VARCHAR2(128) NOT NULL,
  strategy        VARCHAR2(32)  DEFAULT 'direct_copy',
  rows_source     NUMBER,
  rows_target     NUMBER,
  status          VARCHAR2(16),
  started_at      TIMESTAMP,
  ended_at        TIMESTAMP,
  error           VARCHAR2(4000),
  sequences_reset VARCHAR2(4000),
  CONSTRAINT pk_oracdb_tables PRIMARY KEY (job_id, table_name),
  CONSTRAINT fk_oracdb_tables_job FOREIGN KEY (job_id)
    REFERENCES oracdb$jobs (job_id) ON DELETE CASCADE
)
"""

# ORA-00955: name is already used by an existing object. Idempotent CREATE.
_TABLE_EXISTS = 955


def ensure_schema(conn: Any) -> None:
    """Create the state tables if they don't exist. Idempotent."""
    for ddl in (_DDL_JOBS, _DDL_TABLES):
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
        except oracledb.DatabaseError as exc:
            code = getattr(exc.args[0], "code", None) if exc.args else None
            if code == _TABLE_EXISTS:
                log.debug("oracdb_state_table_exists", code=code)
            else:
                raise
