"""Strategy implementations: direct_copy, parallel_dml, chunked_staging."""
from __future__ import annotations

from tests._helpers import make_mock_conn, patched_introspect


def _ctx(
    table: str = "T1",
    *,
    scn: int | None = 12345,
    dblink: str | None = None,
    max_parallel: int = 4,
    columns: list[str] | None = None,
) -> object:
    from oracle_schema_refresh.strategy.base import StrategyContext

    src = make_mock_conn()
    tgt = make_mock_conn()
    return StrategyContext(
        source_conn=src,
        target_conn=tgt,
        source_schema="SRC",
        target_schema="TGT",
        table=table,
        scn=scn,
        dblink=dblink,
        max_parallel=max_parallel,
        columns=columns or ["ID", "V"],
    )


# ---------------------------------------------------------------------------
# DirectCopyStrategy
# ---------------------------------------------------------------------------


def test_direct_copy_emits_insert_select_with_scn_and_columns() -> None:
    from oracle_schema_refresh.strategy.direct_copy import DirectCopyStrategy

    ctx = _ctx()
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )

    with patched_introspect(get_table_row_count=10):
        result = DirectCopyStrategy().load(ctx)

    inserts = [s for s in seen if "INSERT" in s.upper()]
    assert inserts
    sql = inserts[0]
    assert '"ID"' in sql and '"V"' in sql
    assert "AS OF SCN" in sql.upper()
    assert "SELECT *" not in sql.upper()
    assert result.rows_target == 10
    assert result.chunks_used == 1


def test_direct_copy_uses_dblink_when_set() -> None:
    from oracle_schema_refresh.strategy.direct_copy import DirectCopyStrategy

    ctx = _ctx(dblink="SRC_LINK")
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        DirectCopyStrategy().load(ctx)

    inserts = [s for s in seen if "INSERT" in s.upper()]
    assert any("@SRC_LINK" in s for s in inserts)


# ---------------------------------------------------------------------------
# ParallelDmlStrategy
# ---------------------------------------------------------------------------


def test_parallel_dml_enables_parallel_dml_and_uses_hint() -> None:
    from oracle_schema_refresh.strategy.parallel_dml import ParallelDmlStrategy

    ctx = _ctx(max_parallel=8)
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        ParallelDmlStrategy().load(ctx)

    assert any(
        "ALTER SESSION" in s.upper() and "PARALLEL DML" in s.upper()
        for s in seen
    )
    insert = next(s for s in seen if "INSERT" in s.upper())
    assert "PARALLEL" in insert.upper()
    assert "8" in insert  # the configured degree
    assert "APPEND" in insert.upper()


def test_parallel_dml_uses_dblink_and_scn() -> None:
    from oracle_schema_refresh.strategy.parallel_dml import ParallelDmlStrategy

    ctx = _ctx(dblink="L", scn=99)
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        ParallelDmlStrategy().load(ctx)

    insert = next(s for s in seen if "INSERT" in s.upper())
    assert "@L" in insert
    assert "AS OF SCN" in insert.upper()


# ---------------------------------------------------------------------------
# ChunkedStagingStrategy
# ---------------------------------------------------------------------------


def test_chunked_staging_creates_per_chunk_staging_tables() -> None:
    from oracle_schema_refresh.strategy.chunked_staging import (
        ChunkedStagingStrategy,
    )

    ctx = _ctx(max_parallel=4)
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        result = ChunkedStagingStrategy().load(ctx)

    creates = [s for s in seen if "CREATE TABLE" in s.upper() and "STG" in s.upper()]
    assert creates, f"expected staging-table CREATEs; saw: {seen}"
    # chunks_used should match the number of staging tables.
    assert result.chunks_used == len(creates)


def test_chunked_staging_drops_staging_tables_after_merge() -> None:
    from oracle_schema_refresh.strategy.chunked_staging import (
        ChunkedStagingStrategy,
    )

    ctx = _ctx()
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        ChunkedStagingStrategy().load(ctx)

    drops = [s for s in seen if "DROP TABLE" in s.upper() and "STG" in s.upper()]
    # Same count as creates.
    creates = [s for s in seen if "CREATE TABLE" in s.upper() and "STG" in s.upper()]
    assert len(drops) == len(creates)


def test_chunked_staging_calls_dbms_parallel_execute() -> None:
    from oracle_schema_refresh.strategy.chunked_staging import (
        ChunkedStagingStrategy,
    )

    ctx = _ctx()
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        ChunkedStagingStrategy().load(ctx)

    assert any("DBMS_PARALLEL_EXECUTE" in s.upper() for s in seen), (
        f"chunked_staging must drive DBMS_PARALLEL_EXECUTE; saw: {seen}"
    )


def test_chunked_staging_merges_into_target_with_append() -> None:
    from oracle_schema_refresh.strategy.chunked_staging import (
        ChunkedStagingStrategy,
    )

    ctx = _ctx()
    seen: list[str] = []
    ctx.target_conn.cursor().__enter__().execute.side_effect = (  # type: ignore[attr-defined]
        lambda sql, *a, **kw: seen.append(sql)
    )
    with patched_introspect(get_table_row_count=0):
        ChunkedStagingStrategy().load(ctx)

    merges = [
        s for s in seen
        if "INSERT" in s.upper() and "APPEND" in s.upper()
        and "STG" in s.upper() and '"TGT"."T1"' in s
    ]
    assert merges, f"expected APPEND-merge into target; saw: {seen}"
