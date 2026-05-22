"""``parallel_dml`` — one ``INSERT /*+ APPEND PARALLEL(N) */`` per table.

The target session enables parallel DML and submits one INSERT
statement that Oracle parallelises server-side. No staging tables,
no DBMS_PARALLEL_EXECUTE — works in any environment where the user
has been granted parallel DML.

The data plane is still a single statement: cross-host pulls are
serial on the dblink, but the *target* write is parallelised, which
is the right trade-off for medium-sized tables.
"""
from __future__ import annotations

from oracle_schema_refresh import introspect
from oracle_schema_refresh.strategy.base import StrategyContext, StrategyResult


class ParallelDmlStrategy:
    name = "parallel_dml"

    def load(self, ctx: StrategyContext) -> StrategyResult:
        cols = ctx.columns or introspect.get_table_columns(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        col_list = ", ".join(f'"{c}"' for c in cols)
        source_ref = f'"{ctx.source_schema}"."{ctx.table}"'
        if ctx.dblink:
            source_ref = f"{source_ref}@{ctx.dblink}"
        scn_clause = " AS OF SCN :scn" if ctx.scn is not None else ""
        hint = (
            f"/*+ APPEND PARALLEL("
            f'"{ctx.table}", {ctx.max_parallel}) */'
        )
        with ctx.target_conn.cursor() as cur:
            cur.execute("ALTER SESSION ENABLE PARALLEL DML")
            sql = (
                f'INSERT {hint} INTO "{ctx.target_schema}"."{ctx.table}" ({col_list}) '
                f"SELECT {col_list} FROM {source_ref}{scn_clause}"
            )
            if ctx.scn is not None:
                cur.execute(sql, scn=ctx.scn)
            else:
                cur.execute(sql)

        rows_target = introspect.get_table_row_count(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        return StrategyResult(rows_target=rows_target, chunks_used=1)
