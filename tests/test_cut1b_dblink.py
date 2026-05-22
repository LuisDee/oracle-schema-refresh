"""DBLink lifecycle — existing and session modes."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from tests._helpers import make_mock_conn

# ---------------------------------------------------------------------------
# parse_dblink_spec
# ---------------------------------------------------------------------------


def test_parse_dblink_spec_existing() -> None:
    from oracle_schema_refresh.endpoints.dblink import parse_dblink_spec

    mode, name = parse_dblink_spec("existing:SRC_LINK")
    assert mode == "existing"
    assert name == "SRC_LINK"


def test_parse_dblink_spec_session_returns_none_name() -> None:
    from oracle_schema_refresh.endpoints.dblink import parse_dblink_spec

    mode, name = parse_dblink_spec("session")
    assert mode == "session"
    assert name is None


def test_parse_dblink_spec_rejects_unknown() -> None:
    from oracle_schema_refresh.endpoints.dblink import parse_dblink_spec

    with pytest.raises(ValueError):
        parse_dblink_spec("garbage")


# ---------------------------------------------------------------------------
# session_dblink — create-and-drop context manager
# ---------------------------------------------------------------------------


def _src_endpoint() -> object:
    from oracle_schema_refresh.endpoints import Endpoint

    return Endpoint(
        name="src",
        dsn="src-host:1521/srcdb",
        username="src_user",
        password=SecretStr("src_pwd"),
    )


def test_session_dblink_creates_private_link_with_explicit_credentials() -> None:
    from oracle_schema_refresh.endpoints.dblink import session_dblink

    target_conn = make_mock_conn()
    cur = target_conn.cursor().__enter__()
    seen: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen.append(sql)

    src = _src_endpoint()
    with session_dblink(target_conn, src) as link_name:
        # The generated name should be deterministic and obvious enough that
        # operators can find it if cleanup ever fails (e.g. CLI killed mid-job).
        assert link_name.startswith("ORACDB_")
        # Body of the with must run after CREATE DATABASE LINK.
        creates = [s for s in seen if "CREATE DATABASE LINK" in s.upper()]
        assert len(creates) == 1
        create_sql = creates[0]
        assert link_name in create_sql
        assert "src_user" in create_sql
        assert "src-host:1521/srcdb" in create_sql
        # Password must appear in the DDL (Oracle needs it to authenticate the
        # link) but we should still warn callers — that's a wallet job for Cut 3.
        assert "src_pwd" in create_sql

    drops = [s for s in seen if "DROP DATABASE LINK" in s.upper()]
    assert len(drops) == 1
    assert link_name in drops[0]


def test_session_dblink_drops_link_even_when_body_raises() -> None:
    from oracle_schema_refresh.endpoints.dblink import session_dblink

    target_conn = make_mock_conn()
    cur = target_conn.cursor().__enter__()
    seen: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen.append(sql)

    src = _src_endpoint()
    with pytest.raises(RuntimeError, match="boom"):
        with session_dblink(target_conn, src):
            raise RuntimeError("boom")

    drops = [s for s in seen if "DROP DATABASE LINK" in s.upper()]
    assert len(drops) == 1


def test_session_dblink_drop_failure_is_logged_not_raised() -> None:
    """If the DROP fails (e.g. lost connection), we must not mask the original
    body exception — log and move on."""
    import oracledb

    from oracle_schema_refresh.endpoints.dblink import session_dblink

    target_conn = make_mock_conn()
    cur = target_conn.cursor().__enter__()

    drop_err = oracledb.DatabaseError()
    drop_err.args = (type("X", (), {"code": 2024, "message": "ORA-02024"}),)

    def execute_side(sql: str, *a: object, **kw: object) -> None:
        if "DROP DATABASE LINK" in sql.upper():
            raise drop_err

    cur.execute.side_effect = execute_side

    # Body runs cleanly; DROP fails on exit; we should NOT see the ORA-02024.
    with session_dblink(target_conn, _src_endpoint()):
        pass


# ---------------------------------------------------------------------------
# dblink_for — dispatches on RefreshConfig.dblink
# ---------------------------------------------------------------------------


def test_dblink_for_existing_yields_name_without_create_or_drop() -> None:
    from oracle_schema_refresh.endpoints.dblink import dblink_for

    target_conn = make_mock_conn()
    cur = target_conn.cursor().__enter__()
    seen: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen.append(sql)

    src = _src_endpoint()
    with dblink_for(target_conn, src, "existing:SRC_LINK") as name:
        assert name == "SRC_LINK"

    assert not any("CREATE DATABASE LINK" in s.upper() for s in seen)
    assert not any("DROP DATABASE LINK" in s.upper() for s in seen)


def test_dblink_for_session_dispatches_to_session_dblink() -> None:
    from oracle_schema_refresh.endpoints.dblink import dblink_for

    target_conn = make_mock_conn()
    with patch(
        "oracle_schema_refresh.endpoints.dblink.session_dblink"
    ) as mock_session:
        # mock_session is a regular MagicMock, so __enter__/__exit__ work.
        mock_session.return_value.__enter__.return_value = "STUB_LINK"
        mock_session.return_value.__exit__.return_value = False
        src = _src_endpoint()
        with dblink_for(target_conn, src, "session") as name:
            assert name == "STUB_LINK"
        mock_session.assert_called_once_with(target_conn, src)


def test_dblink_for_none_raises_when_called() -> None:
    """The caller must check first — dblink_for(None) is a programming error."""
    from oracle_schema_refresh.endpoints.dblink import dblink_for

    src = _src_endpoint()
    with pytest.raises(ValueError, match="dblink spec is required"):
        with dblink_for(make_mock_conn(), src, None):
            pass
