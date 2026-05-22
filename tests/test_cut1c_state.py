"""Server-side state — job-id format, schema DDL, accessors.

Most tests assert on SQL strings (we can't run real Oracle in the unit
suite). Integration tests against ``gvenzl/oracle-free:23-slim`` will
verify the SQL actually creates the right tables.
"""
from __future__ import annotations

import re

import pytest

from tests._helpers import make_mock_conn

# ---------------------------------------------------------------------------
# job_id_new
# ---------------------------------------------------------------------------


def test_job_id_new_format() -> None:
    from oracle_schema_refresh.state.jobid import job_id_new

    jid = job_id_new()
    # j_YYYYMMDD_HHMMSS_<4 hex chars>
    assert re.fullmatch(r"j_\d{8}_\d{6}_[0-9a-f]{4}", jid), jid


def test_job_id_new_is_unique() -> None:
    from oracle_schema_refresh.state.jobid import job_id_new

    ids = {job_id_new() for _ in range(50)}
    assert len(ids) == 50


# ---------------------------------------------------------------------------
# ensure_schema — idempotent CREATE TABLE
# ---------------------------------------------------------------------------


def test_ensure_schema_creates_both_tables() -> None:
    from oracle_schema_refresh.state.schema import ensure_schema

    conn = make_mock_conn()
    seen: list[str] = []
    conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen.append(sql)
    )

    ensure_schema(conn)

    creates = [s for s in seen if "CREATE TABLE" in s.upper()]
    upper = " ".join(creates).upper()
    assert "ORACDB$JOBS" in upper
    assert "ORACDB$TABLES" in upper


def test_ensure_schema_swallows_table_exists() -> None:
    """ORA-00955 (object already exists) must be silently absorbed — this
    function is idempotent."""
    import oracledb

    from oracle_schema_refresh.state.schema import ensure_schema

    conn = make_mock_conn()
    err = oracledb.DatabaseError()
    err.args = (type("X", (), {"code": 955, "message": "ORA-00955"}),)
    conn.cursor().__enter__().execute.side_effect = err

    # Should not raise.
    ensure_schema(conn)


def test_ensure_schema_propagates_unexpected_error() -> None:
    import oracledb

    from oracle_schema_refresh.state.schema import ensure_schema

    conn = make_mock_conn()
    err = oracledb.DatabaseError()
    err.args = (type("X", (), {"code": 942, "message": "ORA-00942"}),)
    conn.cursor().__enter__().execute.side_effect = err

    with pytest.raises(oracledb.DatabaseError):
        ensure_schema(conn)


# ---------------------------------------------------------------------------
# Job / TableRecord round-trip
# ---------------------------------------------------------------------------


def test_insert_and_get_job() -> None:
    from oracle_schema_refresh.state.job import Job, get_job, insert_job

    conn = make_mock_conn()
    job = Job(
        job_id="j_20260522_120000_abcd",
        source_endpoint="src",
        target_endpoint="tgt",
        source_schema="SRC",
        target_schema="TGT",
        scn=12345,
        config_json='{"tables": ["T1"]}',
        status="PLANNED",
    )
    insert_job(conn, job)
    # mock fetchone returns whatever we set on cur.fetchone.return_value.
    # Mirror an inserted row.
    conn.cursor().__enter__().fetchone.return_value = (
        "j_20260522_120000_abcd",
        "src",
        "tgt",
        "SRC",
        "TGT",
        12345,
        '{"tables": ["T1"]}',
        "PLANNED",
        None,
        None,
        None,
    )
    fetched = get_job(conn, "j_20260522_120000_abcd")
    assert fetched is not None
    assert fetched.job_id == job.job_id
    assert fetched.status == "PLANNED"
    assert fetched.scn == 12345


def test_get_job_returns_none_when_missing() -> None:
    from oracle_schema_refresh.state.job import get_job

    conn = make_mock_conn()
    conn.cursor().__enter__().fetchone.return_value = None

    assert get_job(conn, "j_doesnt_exist") is None


def test_update_job_status_transitions() -> None:
    from oracle_schema_refresh.state.job import update_job_status

    conn = make_mock_conn()
    seen: list[tuple[str, dict]] = []
    conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen.append((sql, kw))
    )

    update_job_status(conn, "j_x", "RUNNING")
    update_job_status(conn, "j_x", "DONE")
    update_job_status(conn, "j_x", "FAILED", error="connection reset")

    statuses = [kw.get("status") for _, kw in seen]
    assert statuses == ["RUNNING", "DONE", "FAILED"]
    errors = [kw.get("error") for _, kw in seen]
    assert errors[-1] == "connection reset"


def test_insert_table_records_writes_one_per_table() -> None:
    from oracle_schema_refresh.state.table_record import (
        TableRecord,
        insert_table_records,
    )

    conn = make_mock_conn()
    cur = conn.cursor().__enter__()
    records = [
        TableRecord(job_id="j_x", table_name="T1", status="PLANNED"),
        TableRecord(job_id="j_x", table_name="T2", status="PLANNED"),
    ]
    insert_table_records(conn, records)

    # executemany OR per-row execute — accept either pattern but require
    # all tables ended up on the wire.
    all_calls = cur.execute.call_args_list + cur.executemany.call_args_list
    flat = " ".join(str(c) for c in all_calls)
    assert "T1" in flat
    assert "T2" in flat


def test_update_table_record_uses_primary_key() -> None:
    from oracle_schema_refresh.state.table_record import update_table_record

    conn = make_mock_conn()
    seen: list[tuple[str, dict]] = []
    conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen.append((sql, kw))
    )

    update_table_record(
        conn,
        job_id="j_x",
        table_name="T1",
        status="DONE",
        rows_source=42,
        rows_target=42,
    )

    assert len(seen) == 1
    sql, kw = seen[0]
    assert "UPDATE" in sql.upper() and "ORACDB$TABLES" in sql.upper()
    assert kw["job_id"] == "j_x"
    assert kw["table_name"] == "T1"
    assert kw["status"] == "DONE"
    assert kw["rows_source"] == 42


def test_get_table_records_returns_ordered_list() -> None:
    from oracle_schema_refresh.state.table_record import get_table_records

    conn = make_mock_conn()
    conn.cursor().__enter__().fetchall.return_value = [
        ("j_x", "T1", "DONE", 5, 5, None, None, None),
        ("j_x", "T2", "FAILED", 10, 9, "row count mismatch", None, None),
    ]
    records = get_table_records(conn, "j_x")
    assert [r.table_name for r in records] == ["T1", "T2"]
    assert records[0].status == "DONE"
    assert records[1].status == "FAILED"
    assert records[1].error == "row count mismatch"


def test_drop_job_cascades_to_table_records() -> None:
    """``drop_job`` deletes from oracdb$jobs; FK CASCADE handles the
    children. The unit test confirms only the parent DELETE — actual
    cascade is an Oracle integration concern."""
    from oracle_schema_refresh.state.job import drop_job

    conn = make_mock_conn()
    seen: list[str] = []
    conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen.append(sql)
    )

    drop_job(conn, "j_x")

    assert any(
        "DELETE FROM" in s.upper() and "ORACDB$JOBS" in s.upper() for s in seen
    )
