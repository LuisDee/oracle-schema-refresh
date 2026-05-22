"""Tests for engine.py — RefreshEngine 5-phase orchestrator."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Dataclass tests (no Oracle connection needed)
# ---------------------------------------------------------------------------


def test_table_result_defaults() -> None:
    from oracle_schema_refresh.engine import TableResult

    r = TableResult(table_name="T1", status="ok")
    assert r.rows_loaded == 0
    assert r.duration_seconds == 0.0
    assert r.error is None


def test_refresh_result_summary_structure() -> None:
    from oracle_schema_refresh.engine import RefreshResult, TableResult

    result = RefreshResult(
        success=True,
        tables_requested=["T1"],
        tables_resolved=["T1"],
        table_order=["T1"],
        table_results=[TableResult(table_name="T1", status="ok", rows_loaded=10)],
        total_duration_seconds=1.5,
    )
    summary = result.summary()
    assert summary["success"] is True
    assert summary["total_seconds"] == 1.5
    assert summary["results"][0]["table"] == "T1"
    assert summary["results"][0]["rows"] == 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn() -> MagicMock:
    """Return a mock oracledb connection with a mock cursor."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (0,)  # default row count
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _make_engine(
    tables: list[str] | None = None,
    recreate: bool = False,
    commit_mode: str = "per_table",
) -> object:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    oracle_conn = OracleConnection(username="u", password="p", dsn="h:1521/s")
    config = RefreshConfig(
        source_schema="SRC",
        target_schema="TGT",
        tables=tables or ["T1"],
        recreate_tables=recreate,
        commit_mode=commit_mode,  # type: ignore[arg-type]
        auto_include_fk_parents=False,
    )
    return RefreshEngine(oracle_conn, config)


# ---------------------------------------------------------------------------
# dry_run — must make zero DDL/DML calls
# ---------------------------------------------------------------------------


def test_engine_dry_run_makes_no_execute_calls() -> None:

    engine = _make_engine()
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=False
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        result = engine.run(dry_run=True)  # type: ignore[union-attr]

    mock_conn.cursor().__enter__().execute.assert_not_called()
    assert result.success is True


# ---------------------------------------------------------------------------
# Phase 3 — FK constraints disabled
# ---------------------------------------------------------------------------


def test_engine_phase3_disables_enabled_fk_constraints() -> None:

    engine = _make_engine()
    mock_conn = _make_mock_conn()
    fake_constraints = [
        {"name": "FK_ONE", "table": "T1", "status": "ENABLED"},
    ]

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=fake_constraints,
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=0,
                                ):
                                    result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert "FK_ONE" in result.constraints_disabled


# ---------------------------------------------------------------------------
# Phase 4 — INSERT called for each table
# ---------------------------------------------------------------------------


def test_engine_phase4_inserts_each_table() -> None:

    engine = _make_engine(tables=["T1", "T2"])
    mock_conn = _make_mock_conn()

    executed_sqls: list[str] = []

    def capture_execute(sql: str, *args: object, **kwargs: object) -> None:
        executed_sqls.append(sql)

    mock_conn.cursor().__enter__().execute.side_effect = capture_execute

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1", "T2"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": [], "T2": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=5,
                                ):
                                    engine.run(dry_run=False)  # type: ignore[union-attr]

    insert_sqls = [s for s in executed_sqls if "INSERT" in s.upper()]
    assert any("T1" in s for s in insert_sqls)
    assert any("T2" in s for s in insert_sqls)


# ---------------------------------------------------------------------------
# Phase 4 — per_table commit
# ---------------------------------------------------------------------------


def test_engine_phase4_commits_per_table() -> None:

    engine = _make_engine(tables=["T1", "T2"], commit_mode="per_table")
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1", "T2"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": [], "T2": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=0,
                                ):
                                    engine.run(dry_run=False)  # type: ignore[union-attr]

    # commit should be called at least twice (once per table)
    assert mock_conn.commit.call_count >= 2


# ---------------------------------------------------------------------------
# Phase 5 — FK constraints re-enabled
# ---------------------------------------------------------------------------


def test_engine_phase5_reenables_constraints() -> None:

    engine = _make_engine()
    mock_conn = _make_mock_conn()
    fake_constraints = [{"name": "FK_ONE", "table": "T1", "status": "ENABLED"}]

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=fake_constraints,
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=0,
                                ):
                                    result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert "FK_ONE" in result.constraints_reenabled


# ---------------------------------------------------------------------------
# Skips missing source table
# ---------------------------------------------------------------------------


def test_engine_skips_table_missing_from_source() -> None:

    engine = _make_engine(tables=["MISSING"])
    mock_conn = _make_mock_conn()

    def fake_table_exists(conn: object, schema: str, table: str) -> bool:
        # Table exists in target but NOT in source
        return schema != "SRC"

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists",
            side_effect=fake_table_exists,
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["MISSING"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"MISSING": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        result = engine.run(dry_run=False)  # type: ignore[union-attr]

    skipped = [r for r in result.table_results if r.status == "skipped"]
    assert any(r.table_name == "MISSING" for r in skipped)


# ---------------------------------------------------------------------------
# Idempotent ORA error codes swallowed during CREATE TABLE
# ---------------------------------------------------------------------------


def test_engine_swallows_ora_00955_on_create_table() -> None:
    """ORA-00955 (name already exists) must not abort the run."""
    import oracledb


    engine = _make_engine(recreate=False)
    mock_conn = _make_mock_conn()

    ora_err = oracledb.DatabaseError()
    ora_err.args = (MagicMock(code=955, message="ORA-00955"),)

    # table does not exist in target → engine tries CREATE TABLE → ORA-00955
    def fake_table_exists(conn: object, schema: str, table: str) -> bool:
        return schema == "SRC"  # exists in source, not target

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists",
            side_effect=fake_table_exists,
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_table_ddl",
                        return_value='CREATE TABLE "TGT"."T1" (ID NUMBER)',
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_index_ddl",
                            return_value=[],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_columns",
                                    return_value=["C1"],
                                ):
                                    with patch(
                                        "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                        return_value=[],
                                    ):
                                        with patch(
                                            "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                            return_value=0,
                                        ):
                                            def raise_on_create(
                                                sql: str, *a: object, **kw: object
                                            ) -> None:
                                                if (
                                                    isinstance(sql, str)
                                                    and "CREATE TABLE" in sql.upper()
                                                ):
                                                    raise ora_err

                                            cur = mock_conn.cursor().__enter__()
                                            cur.execute.side_effect = raise_on_create
                                            # Should not raise — ORA-00955 is swallowed
                                            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result is not None


# ---------------------------------------------------------------------------
# Phase 5 — ORA-02298 FK validation failure marks run failed
# ---------------------------------------------------------------------------


def test_engine_phase5_ora02298_marks_run_failed() -> None:
    """ORA-02298 on FK re-enable must set success=False but not abort the run."""
    import oracledb


    engine = _make_engine()
    mock_conn = _make_mock_conn()
    fake_constraints = [{"name": "FK_ONE", "table": "T1", "status": "ENABLED"}]

    ora_err = oracledb.DatabaseError()
    ora_err.args = (MagicMock(code=2298, message="ORA-02298: cannot validate"),)

    def raise_on_enable(sql: str, *a: object, **kw: object) -> None:
        if isinstance(sql, str) and "ENABLE" in sql.upper():
            raise ora_err

    mock_conn.cursor().__enter__().execute.side_effect = raise_on_enable

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=fake_constraints,
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=0,
                                ):
                                    result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.success is False
    assert result.constraints_reenabled == []


# ---------------------------------------------------------------------------
# Phase 4 — all_or_nothing commit mode
# ---------------------------------------------------------------------------


def test_engine_all_or_nothing_commits_once_at_end() -> None:
    """all_or_nothing mode must commit exactly once after all tables are inserted."""

    engine = _make_engine(tables=["T1", "T2"], commit_mode="all_or_nothing")
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1", "T2"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": [], "T2": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        with patch(
                            "oracle_schema_refresh.engine.introspect.get_table_columns",
                            return_value=["C1"],
                        ):
                            with patch(
                                "oracle_schema_refresh.engine.introspect.get_identity_columns",
                                return_value=[],
                            ):
                                with patch(
                                    "oracle_schema_refresh.engine.introspect.get_table_row_count",
                                    return_value=0,
                                ):
                                    engine.run(dry_run=False)  # type: ignore[union-attr]

    assert mock_conn.commit.call_count == 1


# ---------------------------------------------------------------------------
# Phase 4 — non-Oracle exceptions propagate (not silently swallowed)
# ---------------------------------------------------------------------------


def test_engine_phase4_non_oracle_exception_propagates() -> None:
    """ValueError inside _insert_table must propagate, not be caught as 'failed' status."""

    engine = _make_engine()
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch(
            "oracle_schema_refresh.engine.introspect.table_exists", return_value=True
        ):
            with patch(
                "oracle_schema_refresh.engine.introspect.discover_fk_parents",
                return_value=["T1"],
            ):
                with patch(
                    "oracle_schema_refresh.engine.introspect.build_dependency_graph",
                    return_value={"T1": []},
                ):
                    with patch(
                        "oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target",
                        return_value=[],
                    ):
                        with patch.object(
                            engine,  # type: ignore[union-attr]
                            "_insert_table",
                            side_effect=ValueError("unexpected programming error"),
                        ):
                            with pytest.raises(ValueError, match="unexpected programming error"):
                                engine.run(dry_run=False)
