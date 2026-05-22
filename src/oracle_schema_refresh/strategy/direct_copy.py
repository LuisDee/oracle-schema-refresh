"""``direct_copy`` — one ``INSERT … SELECT`` per table.

The Cut 1b strategy, made explicit. No within-table parallelism, no
staging — works everywhere, fastest for small tables.
"""
from __future__ import annotations

from oracle_schema_refresh import introspect
from oracle_schema_refresh.strategy.base import StrategyContext, StrategyResult


class DirectCopyStrategy:
    name = "direct_copy"

    def load(self, ctx: StrategyContext) -> StrategyResult:
        cols = ctx.columns or introspect.get_table_columns(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        col_list = ", ".join(f'"{c}"' for c in cols)
        source_ref = f'"{ctx.source_schema}"."{ctx.table}"'
        if ctx.dblink:
            source_ref = f"{source_ref}@{ctx.dblink}"
        scn_clause = " AS OF SCN :scn" if ctx.scn is not None else ""
        hint = f" {ctx.insert_hint}" if ctx.insert_hint else ""
        sql = (
            f'INSERT{hint} INTO "{ctx.target_schema}"."{ctx.table}" ({col_list}) '
            f"SELECT {col_list} FROM {source_ref}{scn_clause}"
        )
        with ctx.target_conn.cursor() as cur:
            if ctx.scn is not None:
                cur.execute(sql, scn=ctx.scn)
            else:
                cur.execute(sql)

        rows_target = introspect.get_table_row_count(
            ctx.target_conn, ctx.target_schema, ctx.table
        )
        return StrategyResult(rows_target=rows_target, chunks_used=1)
