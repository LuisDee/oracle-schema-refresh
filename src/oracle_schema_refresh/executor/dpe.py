"""Wrapper around ``DBMS_PARALLEL_EXECUTE``.

Submits a task that runs N pre-built INSERT statements in parallel,
one per chunk, on the target session. Waits for completion (or
failure) before returning.

The pattern: ``CREATE_TASK`` + ``CREATE_CHUNKS_BY_NUMBER_COL`` (we
synthesise a chunk-driving query that returns one row per chunk index)
+ ``RUN_TASK`` + ``DROP_TASK``.
"""
from __future__ import annotations

from typing import Any


def run_parallel_task(
    conn: Any,
    *,
    task_name: str,
    chunk_sqls: list[str],
    parallel_level: int,
    scn: int | None = None,
) -> None:
    """Run ``chunk_sqls`` in parallel via DBMS_PARALLEL_EXECUTE.

    Each chunk SQL is one full ``INSERT`` statement — the wrapper does
    not template ``:start_id`` / ``:end_id`` placeholders, because
    Cut 2's strategies pre-build per-chunk SQL with ``ORA_HASH(rowid)``
    bucketing baked in.

    The chunk-driving query returns ``(idx, idx)`` for ``idx = 0..N-1``,
    which DBMS_PARALLEL_EXECUTE feeds back into each chunk's
    ``:start_id`` (== chunk index). Strategies that don't need it just
    don't reference it.

    Implementation: an anonymous PL/SQL block. Cheaper than three
    round-trips.
    """
    n = len(chunk_sqls)
    if n == 0:
        return
    # Build a CASE statement so the parallel-execute task dispatches
    # to the right chunk SQL based on :start_id.
    case_arms = "\n".join(
        f"WHEN {i} THEN q'<{sql}>'" for i, sql in enumerate(chunk_sqls)
    )
    chunk_sql_select = f"""
      SELECT CASE :start_id
        {case_arms}
      END
      FROM dual
    """

    block = f"""
    DECLARE
      v_stmt CLOB;
    BEGIN
      DBMS_PARALLEL_EXECUTE.CREATE_TASK(task_name => '{task_name}');
      DBMS_PARALLEL_EXECUTE.CREATE_CHUNKS_BY_SQL(
        task_name   => '{task_name}',
        sql_stmt    => 'SELECT level - 1 AS start_id, level - 1 AS end_id '
                       'FROM dual CONNECT BY level <= {n}',
        by_rowid    => FALSE
      );
      FOR rec IN (
        SELECT chunk_id, start_id FROM user_parallel_execute_chunks
         WHERE task_name = '{task_name}'
      ) LOOP
        SELECT CASE rec.start_id
          {case_arms}
        END
          INTO v_stmt FROM dual;
        DBMS_PARALLEL_EXECUTE.RUN_TASK(
          task_name      => '{task_name}',
          sql_stmt       => v_stmt,
          language_flag  => DBMS_SQL.NATIVE,
          parallel_level => {parallel_level}
        );
      END LOOP;
      DBMS_PARALLEL_EXECUTE.DROP_TASK(task_name => '{task_name}');
    END;
    """
    with conn.cursor() as cur:
        cur.execute(block)
    # Reference unused var so a linter doesn't strip the helper above
    # in case future refactor needs it.
    _ = chunk_sql_select
