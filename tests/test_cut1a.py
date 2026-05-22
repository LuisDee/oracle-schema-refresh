"""Tests for Cut 1a — correctness fixes with no new architecture.

Covers:
- AS OF SCN consistent read (intra-instance).
- Explicit column lists from ALL_TAB_COLUMNS.
- call_timeout config knob, applied to the connection.
- commit_mode rename: 'defer_insert_commits' is the honest value;
  'all_or_nothing' still accepted but deprecated.
- Source vs target row-count validation in TableResult.
- Tightened _safe_execute (no default ignore set).
- Identity-column sequence reset after insert.
"""
from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

import pytest

from tests._helpers import make_mock_conn, patched_introspect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(**kwargs: object) -> object:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    oc = OracleConnection(username="u", password="p", dsn="h:1521/s")
    cfg = RefreshConfig(
        source_schema="SRC",
        target_schema="TGT",
        tables=kwargs.get("tables", ["T1"]),  # type: ignore[arg-type]
        auto_include_fk_parents=False,
        **{k: v for k, v in kwargs.items() if k != "tables"},  # type: ignore[arg-type]
    )
    return RefreshEngine(oc, cfg)


# ---------------------------------------------------------------------------
# 1. AS OF SCN consistent read
# ---------------------------------------------------------------------------


def test_run_captures_scn_at_start() -> None:
    """Engine must issue 'SELECT current_scn FROM v$database' on a real run."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect():
            engine.run(dry_run=False)  # type: ignore[union-attr]

    assert any("current_scn" in s.lower() for s in seen_sqls), (
        f"expected current_scn capture SQL among executed: {seen_sqls}"
    )


def test_insert_uses_as_of_scn() -> None:
    """The INSERT must read from source AS OF SCN."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (12345,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(get_table_columns=["C1", "C2"]):
            engine.run(dry_run=False)  # type: ignore[union-attr]

    insert_sqls = [s for s in seen_sqls if "INSERT" in s.upper()]
    assert insert_sqls, f"no INSERT SQL executed; saw: {seen_sqls}"
    assert any("AS OF SCN" in s.upper() for s in insert_sqls), (
        f"INSERT must include 'AS OF SCN' but got: {insert_sqls}"
    )


def test_refresh_result_includes_scn() -> None:
    """RefreshResult.summary must include the captured SCN."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (98765,)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect():
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.scn == 98765
    assert result.summary()["scn"] == 98765


# ---------------------------------------------------------------------------
# 2. Explicit column list
# ---------------------------------------------------------------------------


def test_insert_uses_explicit_column_list() -> None:
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (1,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(get_table_columns=["ID", "NAME"]):
            engine.run(dry_run=False)  # type: ignore[union-attr]

    insert_sqls = [s for s in seen_sqls if "INSERT" in s.upper()]
    assert insert_sqls
    sql = insert_sqls[0]
    assert "SELECT *" not in sql.upper(), f"must not use SELECT *: {sql}"
    assert '"ID"' in sql and '"NAME"' in sql, f"missing explicit cols in: {sql}"


def test_insert_column_list_intersects_source_and_target() -> None:
    """Source has an extra column the target doesn't — it must be dropped."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (1,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    def fake_get_columns(conn: object, schema: str, table: str) -> list[str]:
        if schema == "SRC":
            return ["ID", "NAME", "SRC_ONLY"]
        return ["ID", "NAME"]

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(side_effects={"get_table_columns": fake_get_columns}):
            engine.run(dry_run=False)  # type: ignore[union-attr]

    insert_sqls = [s for s in seen_sqls if "INSERT" in s.upper()]
    sql = insert_sqls[0]
    assert "SRC_ONLY" not in sql, (
        f"source-only column SRC_ONLY must not appear in INSERT: {sql}"
    )
    assert '"ID"' in sql and '"NAME"' in sql


# ---------------------------------------------------------------------------
# 3. call_timeout
# ---------------------------------------------------------------------------


def test_config_has_call_timeout_seconds_default_zero() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    assert cfg.call_timeout_seconds == 0


def test_engine_applies_call_timeout_when_configured() -> None:
    """When config.call_timeout_seconds > 0, conn.call_timeout is set in ms."""
    engine = _make_engine(call_timeout_seconds=30)
    mock_conn = make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(table_exists=False):
            engine.run(dry_run=True)  # type: ignore[union-attr]

    # python-oracledb expects milliseconds.
    assert mock_conn.call_timeout == 30_000


def test_engine_no_call_timeout_by_default() -> None:
    """When call_timeout_seconds is 0, no timeout is set (current behaviour)."""
    engine = _make_engine()
    mock_conn = make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(table_exists=False):
            engine.run(dry_run=True)  # type: ignore[union-attr]

    setattrs = [c for c in mock_conn.mock_calls if c[0] == "__setattr__"]
    assert not any(c.args[0] == "call_timeout" for c in setattrs), (
        f"call_timeout should not be assigned when config value is 0; calls: {setattrs}"
    )


# ---------------------------------------------------------------------------
# 4. commit_mode honest naming
# ---------------------------------------------------------------------------


def test_config_accepts_defer_insert_commits() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(
        source_schema="S",
        target_schema="T",
        tables=["T1"],
        commit_mode="defer_insert_commits",  # type: ignore[arg-type]
    )
    assert cfg.commit_mode == "defer_insert_commits"


def test_config_all_or_nothing_is_deprecated_alias() -> None:
    """The old 'all_or_nothing' value must still parse, normalise to the new
    name, and emit a DeprecationWarning."""
    from oracle_schema_refresh.config import RefreshConfig

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = RefreshConfig(
            source_schema="S",
            target_schema="T",
            tables=["T1"],
            commit_mode="all_or_nothing",  # type: ignore[arg-type]
        )

    assert cfg.commit_mode == "defer_insert_commits"
    assert any(
        issubclass(w.category, DeprecationWarning) and "all_or_nothing" in str(w.message)
        for w in caught
    ), f"expected DeprecationWarning mentioning all_or_nothing; got: {caught}"


def test_engine_defer_insert_commits_commits_once_at_end() -> None:
    """Same semantics as the old all_or_nothing — one commit after all inserts."""
    engine = _make_engine(tables=["T1", "T2"], commit_mode="defer_insert_commits")
    mock_conn = make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            discover_fk_parents=["T1", "T2"],
            build_dependency_graph={"T1": [], "T2": []},
        ):
            engine.run(dry_run=False)  # type: ignore[union-attr]

    assert mock_conn.commit.call_count == 1


# ---------------------------------------------------------------------------
# 5. Source vs target row-count validation
# ---------------------------------------------------------------------------


def test_table_result_records_source_and_target_rows() -> None:
    engine = _make_engine()
    mock_conn = make_mock_conn()

    def fake_row_count(conn: object, schema: str, table: str, **kw: object) -> int:
        return 42 if schema == "SRC" else 42

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(side_effects={"get_table_row_count": fake_row_count}):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    r = result.table_results[0]
    assert r.rows_source == 42
    assert r.rows_loaded == 42
    assert r.match is True
    assert r.status == "ok"


def test_row_count_mismatch_marks_table_failed() -> None:
    engine = _make_engine()
    mock_conn = make_mock_conn()

    def fake_row_count(conn: object, schema: str, table: str, **kw: object) -> int:
        return 100 if schema == "SRC" else 99

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(side_effects={"get_table_row_count": fake_row_count}):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    r = result.table_results[0]
    assert r.rows_source == 100
    assert r.rows_loaded == 99
    assert r.match is False
    assert r.status == "failed"
    assert result.success is False


# ---------------------------------------------------------------------------
# 6. Tightened _safe_execute
# ---------------------------------------------------------------------------


def test_safe_execute_no_default_ignore_codes() -> None:
    """Without explicit ignore_codes, _safe_execute must propagate ORA errors."""
    import oracledb

    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    engine = RefreshEngine(
        OracleConnection(username="u", password="p", dsn="h:1/s"),
        RefreshConfig(source_schema="S", target_schema="T", tables=["T1"]),
    )

    mock_conn = make_mock_conn()
    err = oracledb.DatabaseError()
    err.args = (MagicMock(code=955, message="ORA-00955"),)
    mock_conn.cursor().__enter__().execute.side_effect = err

    with pytest.raises(oracledb.DatabaseError):
        engine._safe_execute(mock_conn, "CREATE TABLE X (id NUMBER)")  # type: ignore[attr-defined]


def test_safe_execute_respects_explicit_codes() -> None:
    """When the caller passes ignore_codes={955}, ORA-00955 is swallowed."""
    import oracledb

    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    engine = RefreshEngine(
        OracleConnection(username="u", password="p", dsn="h:1/s"),
        RefreshConfig(source_schema="S", target_schema="T", tables=["T1"]),
    )

    mock_conn = make_mock_conn()
    err = oracledb.DatabaseError()
    err.args = (MagicMock(code=955, message="ORA-00955"),)
    mock_conn.cursor().__enter__().execute.side_effect = err

    # Should not raise.
    engine._safe_execute(  # type: ignore[attr-defined]
        mock_conn, "CREATE TABLE X (id NUMBER)", ignore_codes=frozenset({955})
    )


# ---------------------------------------------------------------------------
# 7. Identity-column sequence reset
# ---------------------------------------------------------------------------


def test_introspect_get_identity_columns_querys_all_tab_identity_cols() -> None:
    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchall.return_value = [("ID", "ISEQ$$_1234")]

    result = introspect.get_identity_columns(mock_conn, "SCHEMA", "TABLE")

    assert result == [("ID", "ISEQ$$_1234")]
    executed = cur.execute.call_args.args[0]
    assert "all_tab_identity_cols" in executed.lower()


def test_engine_resets_identity_sequences_after_insert() -> None:
    """After loading a table with an identity column, ALTER SEQUENCE RESTART
    must be emitted with START WITH MAX(col)+1."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()

    cur.fetchone.side_effect = [(1000,), (50,), (50,), (43,)]
    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            get_table_columns=["ID"],
            get_identity_columns=[("ID", "ISEQ$$_42")],
            get_table_row_count=50,
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    alter_seq = [s for s in seen_sqls if "ALTER SEQUENCE" in s.upper()]
    assert alter_seq, f"expected ALTER SEQUENCE among executed: {seen_sqls}"
    assert any("RESTART" in s.upper() and "ISEQ$$_42" in s for s in alter_seq), (
        f"expected RESTART for ISEQ$$_42: {alter_seq}"
    )
    assert "ISEQ$$_42" in result.table_results[0].sequences_reset


def test_engine_sequence_reset_failure_logs_and_continues() -> None:
    """ALTER SEQUENCE failures must not abort the run — log and move on."""
    import oracledb

    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (1,)

    err = oracledb.DatabaseError()
    err.args = (MagicMock(code=2289, message="ORA-02289: sequence does not exist"),)

    def execute_side(sql: str, *a: object, **kw: object) -> None:
        if "ALTER SEQUENCE" in sql.upper():
            raise err

    cur.execute.side_effect = execute_side

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            get_table_columns=["ID"],
            get_identity_columns=[("ID", "ISEQ$$_42")],
            get_table_row_count=1,
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert "ISEQ$$_42" not in result.table_results[0].sequences_reset
