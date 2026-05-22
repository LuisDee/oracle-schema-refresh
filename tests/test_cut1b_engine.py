"""Engine cross-host integration — RefreshEngine drives DBLink + two endpoints."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from tests._helpers import make_mock_conn, patched_introspect


def _make_src_tgt_config(dblink: str | None = "session") -> tuple[object, object, object]:
    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint

    src = Endpoint(
        name="src", dsn="src-host:1521/srcdb", username="su", password=SecretStr("sp")
    )
    tgt = Endpoint(
        name="tgt", dsn="tgt-host:1521/tgtdb", username="tu", password=SecretStr("tp")
    )
    cfg = RefreshConfig(
        source_schema="SRC",
        target_schema="TGT",
        tables=["T1"],
        auto_include_fk_parents=False,
        dblink=dblink,
    )
    return src, tgt, cfg


# ---------------------------------------------------------------------------
# from_endpoints: distinct DSNs now allowed iff config.dblink is set
# ---------------------------------------------------------------------------


def test_from_endpoints_distinct_dsn_with_dblink_ok() -> None:
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink="session")
    # Should not raise.
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)
    assert engine._source is src
    assert engine._target is tgt


def test_from_endpoints_distinct_dsn_without_dblink_still_refused() -> None:
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink=None)
    with pytest.raises(ValueError, match="dblink"):
        RefreshEngine.from_endpoints(src, tgt, cfg)


def test_from_endpoints_same_dsn_without_dblink_still_ok() -> None:
    """Same-instance refresh must keep working — no dblink needed, no error."""
    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    ep = Endpoint(name="x", dsn="h:1/s", username="u", password=SecretStr("p"))
    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    engine = RefreshEngine.from_endpoints(ep, ep, cfg)
    assert engine._source is ep
    assert engine._target is ep


# ---------------------------------------------------------------------------
# run(): two connections + dblink in cross-host mode
# ---------------------------------------------------------------------------


def test_cross_host_run_opens_both_endpoints() -> None:
    """When cfg.dblink is set, the engine must open both source and target
    endpoints (not just target)."""
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink="existing:SRC_LINK")
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)

    src_conn = make_mock_conn()
    tgt_conn = make_mock_conn()

    def fake_connect(self: Endpoint, **kw: object) -> object:
        return src_conn if self.dsn == "src-host:1521/srcdb" else tgt_conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect():
            engine.run(dry_run=True)

    assert src_conn.close.called
    assert tgt_conn.close.called


def test_cross_host_run_creates_session_dblink_and_drops_it() -> None:
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink="session")
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)

    src_conn = make_mock_conn()
    tgt_conn = make_mock_conn()
    seen_sqls: list[str] = []
    tgt_conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen_sqls.append(sql)
    )

    def fake_connect(self: Endpoint, **kw: object) -> object:
        return src_conn if self.dsn == "src-host:1521/srcdb" else tgt_conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect(get_table_row_count=0):
            engine.run(dry_run=False)

    creates = [s for s in seen_sqls if "CREATE DATABASE LINK" in s.upper()]
    drops = [s for s in seen_sqls if "DROP DATABASE LINK" in s.upper()]
    assert len(creates) == 1
    assert len(drops) == 1


def test_cross_host_insert_uses_dblink_in_select() -> None:
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink="existing:SRC_LINK")
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)

    src_conn = make_mock_conn()
    tgt_conn = make_mock_conn()

    target_sqls: list[str] = []
    tgt_conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: target_sqls.append(sql)
    )

    def fake_connect(self: Endpoint, **kw: object) -> object:
        return src_conn if self.dsn == "src-host:1521/srcdb" else tgt_conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect(get_table_columns=["ID", "NAME"], get_table_row_count=5):
            engine.run(dry_run=False)

    inserts = [s for s in target_sqls if "INSERT" in s.upper()]
    assert inserts
    assert any("@SRC_LINK" in s for s in inserts), (
        f"INSERT must reference @SRC_LINK; got {inserts}"
    )
    assert any("AS OF SCN" in s.upper() for s in inserts), (
        f"INSERT must still use AS OF SCN; got {inserts}"
    )


def test_cross_host_source_scn_captured_from_source_conn() -> None:
    """The SCN read must be on the source connection, not the target."""
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src, tgt, cfg = _make_src_tgt_config(dblink="existing:L")
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)

    src_conn = make_mock_conn()
    tgt_conn = make_mock_conn()
    src_sqls: list[str] = []
    tgt_sqls: list[str] = []
    src_conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: src_sqls.append(sql)
    )
    tgt_conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: tgt_sqls.append(sql)
    )

    def fake_connect(self: Endpoint, **kw: object) -> object:
        return src_conn if self.dsn == "src-host:1521/srcdb" else tgt_conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect(get_table_row_count=0):
            engine.run(dry_run=False)

    assert any("current_scn" in s.lower() for s in src_sqls), (
        "SCN must be read on source conn"
    )
    assert not any("current_scn" in s.lower() for s in tgt_sqls), (
        "SCN must NOT be read on target conn"
    )


def test_same_instance_still_uses_one_connection() -> None:
    """Back-compat: when same DSN and no dblink, only one connection opens."""
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    oc = OracleConnection(username="u", password="p", dsn="h:1/s")
    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    engine = RefreshEngine(oc, cfg)

    conn = make_mock_conn()
    connect_calls: list[str] = []

    def fake_connect(self: Endpoint, **kw: object) -> object:
        connect_calls.append(self.dsn)
        return conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect():
            engine.run(dry_run=True)

    assert len(connect_calls) == 1, (
        f"expected exactly one connect for same-instance; got {connect_calls}"
    )
