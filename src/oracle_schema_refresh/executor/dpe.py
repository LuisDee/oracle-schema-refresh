"""Wrapper around ``DBMS_PARALLEL_EXECUTE``.

The right pattern for "run N independent statements in parallel" is
**one** ``RUN_TASK`` with a single parametrised template — Oracle
dispatches the chunks across ``parallel_level`` slaves. A loop over
chunks would serialise them (``RUN_TASK`` blocks).

Implementation: a CASE-on-``:start_id`` PL/SQL block. Each slave gets
its chunk index, looks up the right INSERT statement, and runs it via
``EXECUTE IMMEDIATE``. The chunk-driving query feeds
``(chunk_id, chunk_id)`` for ``chunk_id ∈ [0, N)``.
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

    ``chunk_sqls[i]`` is the full statement to execute for chunk ``i``.
    Strategies bake all chunk-specific routing (e.g.
    ``ORA_HASH(ROWID, N-1) = i``) into the SQL itself.

    The wrapper:

    * ``CREATE_TASK``
    * ``CREATE_CHUNKS_BY_SQL`` with one row per chunk index
    * ``RUN_TASK`` **once** with a CASE template that dispatches on
      ``:start_id`` to the correct ``EXECUTE IMMEDIATE``
    * ``DROP_TASK`` on the way out

    Best-effort cleanup of ``task_name`` if any earlier step failed.
    """
    _ = scn  # currently unused; reserved for AS OF SCN bind in templates
    n = len(chunk_sqls)
    if n == 0:
        return

    # Each chunk SQL is wrapped in q'[...]' so embedded single quotes and
    # newlines survive PL/SQL string parsing. The delimiter is chosen
    # to be unlikely in INSERT … SELECT SQL.
    case_arms = "\n        ".join(
        f"WHEN {i} THEN EXECUTE IMMEDIATE q'[{sql}]';"
        for i, sql in enumerate(chunk_sqls)
    )

    # Single PL/SQL block does the full lifecycle. The template under
    # RUN_TASK is what each slave executes; outer block creates the task,
    # runs it (blocking until all chunks complete or one errors), and
    # tears it down. DBMS_PARALLEL_EXECUTE re-raises on chunk failure
    # after marking chunks FAILED.
    block = f"""
    DECLARE
      v_template VARCHAR2(32767) := q'[
        DECLARE
          v_idx NUMBER := :start_id;
        BEGIN
          CASE v_idx
            {case_arms}
          END CASE;
          COMMIT;
        END;
      ]';
    BEGIN
      BEGIN
        DBMS_PARALLEL_EXECUTE.DROP_TASK(task_name => '{task_name}');
      EXCEPTION WHEN OTHERS THEN NULL;
      END;
      DBMS_PARALLEL_EXECUTE.CREATE_TASK(task_name => '{task_name}');
      DBMS_PARALLEL_EXECUTE.CREATE_CHUNKS_BY_SQL(
        task_name => '{task_name}',
        sql_stmt  => 'SELECT level - 1 AS start_id, level - 1 AS end_id '
                     'FROM dual CONNECT BY level <= {n}',
        by_rowid  => FALSE
      );
      DBMS_PARALLEL_EXECUTE.RUN_TASK(
        task_name      => '{task_name}',
        sql_stmt       => v_template,
        language_flag  => DBMS_SQL.NATIVE,
        parallel_level => {parallel_level}
      );
      DBMS_PARALLEL_EXECUTE.DROP_TASK(task_name => '{task_name}');
    END;
    """
    with conn.cursor() as cur:
        cur.execute(block)
