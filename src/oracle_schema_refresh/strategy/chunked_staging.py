"""``chunked_staging`` — N staging tables loaded in parallel, then merged.

For large tables where one statement is the bottleneck:

1. Create ``max_chunks_per_table`` staging tables (same column shape
   as the target, no constraints, ``NOLOGGING`` if available).
2. Build a chunk-driving SQL via ``DBMS_PARALLEL_EXECUTE.CREATE_CHUNKS_BY_SQL``
   — chunks come from the source's PK column ranges. (Falls back to
   modulo-bucket chunking when no PK exists.)
3. Submit a parallel task that runs one ``INSERT … SELECT`` per chunk,
   each into its own staging table, with ``parallel_level =
   max_parallel``.
4. After the task completes, merge: one
   ``INSERT /*+ APPEND PARALLEL */ INTO target SELECT … FROM stg_1
   UNION ALL … UNION ALL stg_N``.
5. Drop the staging tables.

The merge is single-statement and pays a measurable tax, but the
parallel cross-host pull wins on a 5GB+ table by a wide margin.
"""
from __future__ import annotations

import secrets

from oracle_schema_refresh import introspect
from oracle_schema_refresh.executor.dpe import run_parallel_task
from oracle_schema_refresh.strategy.base import StrategyContext, StrategyResult


class ChunkedStagingStrategy:
    name = "chunked_staging"

    def load(self, ctx: StrategyContext) -> StrategyResult:
        cols = ctx.columns or introspect.get_table_columns(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        col_list = ", ".join(f'"{c}"' for c in cols)
        source_ref = f'"{ctx.source_schema}"."{ctx.table}"'
        if ctx.dblink:
            source_ref = f"{source_ref}@{ctx.dblink}"
        scn_clause = " AS OF SCN :scn" if ctx.scn is not None else ""

        n_chunks = max(1, ctx.max_chunks_per_table)
        suffix = secrets.token_hex(3).upper()
        stg_names = [
            f"{ctx.table}_STG_{suffix}_{i}" for i in range(n_chunks)
        ]

        with ctx.target_conn.cursor() as cur:
            # 1. Create staging tables — empty CTAS off the target shape.
            for stg in stg_names:
                cur.execute(
                    f'CREATE TABLE "{ctx.target_schema}"."{stg}" '
                    f'AS SELECT {col_list} FROM "{ctx.target_schema}"."{ctx.table}" '
                    f"WHERE 1=0"
                )

        # 2. + 3. Drive the per-chunk INSERTs via DBMS_PARALLEL_EXECUTE.
        # Each worker reads one chunk's worth of rows from source and
        # appends to its assigned staging table. ``run_parallel_task``
        # encapsulates the PL/SQL CREATE_TASK / CREATE_CHUNKS / RUN_TASK
        # incantation.
        chunk_sqls = [
            (
                f'INSERT /*+ APPEND */ INTO "{ctx.target_schema}"."{stg}" '
                f"({col_list}) "
                f"SELECT {col_list} FROM {source_ref}{scn_clause} "
                f"WHERE ORA_HASH(ROWID, {n_chunks - 1}) = {idx}"
            )
            for idx, stg in enumerate(stg_names)
        ]
        run_parallel_task(
            ctx.target_conn,
            task_name=f"ORACDB_{ctx.table}_{suffix}",
            chunk_sqls=chunk_sqls,
            parallel_level=ctx.max_parallel,
            scn=ctx.scn,
        )

        # 4. Merge — single APPEND parallel into the target.
        merge_select = " UNION ALL ".join(
            f'SELECT {col_list} FROM "{ctx.target_schema}"."{stg}"'
            for stg in stg_names
        )
        with ctx.target_conn.cursor() as cur:
            cur.execute(
                f'INSERT /*+ APPEND PARALLEL("{ctx.table}", {ctx.max_parallel}) */ '
                f'INTO "{ctx.target_schema}"."{ctx.table}" ({col_list}) '
                f"{merge_select}"
            )

            # 5. Drop the staging tables. Best-effort per-table.
            for stg in stg_names:
                try:
                    cur.execute(
                        f'DROP TABLE "{ctx.target_schema}"."{stg}" PURGE'
                    )
                except Exception:  # noqa: BLE001 — leftover staging is fine
                    pass

        rows_target = introspect.get_table_row_count(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        return StrategyResult(rows_target=rows_target, chunks_used=n_chunks)
