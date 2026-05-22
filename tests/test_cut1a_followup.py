"""Tests for the Cut 1a follow-up — addresses the self-review holes.

1. Plain-sequence detection via trigger body scan
   (``get_sequence_columns_via_triggers``).
2. Oracle server version detection — skip ALTER SEQUENCE RESTART on <18c.
3. Don't commit a known-bad INSERT — reorder per-table commit to after
   match check; rollback in defer-mode if any table mismatched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests._helpers import make_mock_conn, patched_introspect


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
# Hole 1: plain-sequence detection via trigger body scan
# ---------------------------------------------------------------------------


def test_get_sequence_columns_via_triggers_extracts_assignment() -> None:
    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchall.return_value = [
        (
            "T1_BI",
            "BEGIN\n  IF :new.id IS NULL THEN\n"
            "    :new.id := T1_SEQ.NEXTVAL;\n  END IF;\nEND;",
        )
    ]

    found = introspect.get_sequence_columns_via_triggers(mock_conn, "TGT", "T1")

    assert found == [("ID", "T1_SEQ")]


def test_get_sequence_columns_via_triggers_handles_schema_prefix() -> None:
    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchall.return_value = [
        ("T_BI", "BEGIN :NEW.code := SRC.MY_SEQ.NEXTVAL; END;"),
    ]

    found = introspect.get_sequence_columns_via_triggers(mock_conn, "TGT", "T")
    assert found == [("CODE", "MY_SEQ")]


def test_get_sequence_columns_via_triggers_deduplicates() -> None:
    """Two triggers referencing the same (col, seq) pair → one entry."""
    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    body = "BEGIN :new.id := S.NEXTVAL; END;"
    cur.fetchall.return_value = [("T_BI1", body), ("T_BI2", body)]

    found = introspect.get_sequence_columns_via_triggers(mock_conn, "TGT", "T")
    assert found == [("ID", "S")]


def test_get_sequence_columns_via_triggers_swallows_catalog_error() -> None:
    """If ALL_TRIGGERS isn't accessible, return [] rather than aborting."""
    import oracledb

    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    err = oracledb.DatabaseError()
    err.args = (MagicMock(code=942, message="ORA-00942: table or view not exists"),)
    cur.execute.side_effect = err

    assert introspect.get_sequence_columns_via_triggers(mock_conn, "S", "T") == []


def test_engine_resets_trigger_detected_sequences() -> None:
    """Sequences inferred from trigger bodies must be reset alongside identity
    columns."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (10,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            get_table_columns=["ID"],
            get_identity_columns=[],
            get_sequence_columns_via_triggers=[("ID", "MY_SEQ")],
            get_table_row_count=10,
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    alter_seq = [s for s in seen_sqls if "ALTER SEQUENCE" in s.upper()]
    assert any("MY_SEQ" in s and "RESTART" in s.upper() for s in alter_seq), (
        f"expected MY_SEQ RESTART among: {alter_seq}"
    )
    assert "MY_SEQ" in result.table_results[0].sequences_reset


# ---------------------------------------------------------------------------
# Hole 2: server-version gate on ALTER SEQUENCE RESTART
# ---------------------------------------------------------------------------


def test_get_server_version_uses_dbms_db_version() -> None:
    from oracle_schema_refresh import introspect

    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (19,)

    v = introspect.get_server_version(mock_conn)
    assert v == 19
    sql = cur.execute.call_args.args[0]
    assert "dbms_db_version" in sql.lower()


def test_engine_skips_sequence_reset_on_pre_18c() -> None:
    """On Oracle <18c, ALTER SEQUENCE ... RESTART isn't supported. The engine
    must skip the reset entirely (not even attempt it) and log it."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (5,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            get_table_columns=["ID"],
            get_identity_columns=[("ID", "ISEQ$$_42")],
            get_server_version=11,
            get_table_row_count=5,
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert not any("ALTER SEQUENCE" in s.upper() for s in seen_sqls), (
        f"no ALTER SEQUENCE on pre-18c, but saw: {seen_sqls}"
    )
    assert result.table_results[0].sequences_reset == []


def test_engine_runs_sequence_reset_on_18c_and_later() -> None:
    """On Oracle 18c+, ALTER SEQUENCE RESTART must run as usual."""
    engine = _make_engine()
    mock_conn = make_mock_conn()
    cur = mock_conn.cursor().__enter__()
    cur.fetchone.return_value = (5,)

    seen_sqls: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            get_table_columns=["ID"],
            get_identity_columns=[("ID", "ISEQ$$_42")],
            get_server_version=18,
            get_table_row_count=5,
        ):
            engine.run(dry_run=False)  # type: ignore[union-attr]

    assert any("ALTER SEQUENCE" in s.upper() and "RESTART" in s.upper() for s in seen_sqls)


# ---------------------------------------------------------------------------
# Hole 3: rollback on row-count mismatch
# ---------------------------------------------------------------------------


def test_per_table_mismatch_rolls_back_instead_of_committing() -> None:
    """In per_table mode, a row-count mismatch must roll back that table's
    INSERT — never commit a known-bad load."""
    engine = _make_engine(commit_mode="per_table")
    mock_conn = make_mock_conn()

    def fake_row_count(conn: object, schema: str, table: str, **kw: object) -> int:
        return 100 if schema == "SRC" else 99

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(side_effects={"get_table_row_count": fake_row_count}):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.table_results[0].status == "failed"
    # rollback must have been called for the mismatched table; commit must NOT
    # have been called for it.
    assert mock_conn.rollback.call_count >= 1
    assert mock_conn.commit.call_count == 0


def test_per_table_match_still_commits() -> None:
    """Sanity: a clean match in per_table mode still commits."""
    engine = _make_engine(commit_mode="per_table")
    mock_conn = make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(get_table_row_count=10):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.success is True
    assert mock_conn.commit.call_count == 1
    assert mock_conn.rollback.call_count == 0


def test_defer_mode_mismatch_rolls_back_at_end() -> None:
    """In defer_insert_commits mode, any table mismatch must trigger rollback
    at the end of the run — not commit a half-bad batch."""
    engine = _make_engine(tables=["T1", "T2"], commit_mode="defer_insert_commits")
    mock_conn = make_mock_conn()

    def fake_row_count(conn: object, schema: str, table: str, **kw: object) -> int:
        # T1 matches (5/5), T2 mismatches (7/6)
        if table == "T1":
            return 5
        return 7 if schema == "SRC" else 6

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            discover_fk_parents=["T1", "T2"],
            build_dependency_graph={"T1": [], "T2": []},
            side_effects={"get_table_row_count": fake_row_count},
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.success is False
    # Final action must be a rollback, not a commit.
    assert mock_conn.rollback.call_count >= 1
    assert mock_conn.commit.call_count == 0


def test_defer_mode_all_match_still_commits() -> None:
    """Sanity: defer mode with all matches commits exactly once."""
    engine = _make_engine(tables=["T1", "T2"], commit_mode="defer_insert_commits")
    mock_conn = make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patched_introspect(
            discover_fk_parents=["T1", "T2"],
            build_dependency_graph={"T1": [], "T2": []},
            get_table_row_count=10,
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.success is True
    assert mock_conn.commit.call_count == 1
    assert mock_conn.rollback.call_count == 0
