"""Tests for ``RefreshEngine.plan()`` — introspect-only, no DDL/DML.

Plan() runs phases 1 + most of 2 (introspect, FK resolution, table
order, SCN capture, server version) and returns a ``Plan`` dataclass.
The CLI's ``plan`` subcommand persists this to state and prints the
job id without executing anything.
"""
from __future__ import annotations

from unittest.mock import patch

from pydantic import SecretStr

from tests._helpers import make_mock_conn, patched_introspect


def _engine_same_instance() -> object:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    return RefreshEngine(
        OracleConnection(username="u", password="p", dsn="h:1/s"),
        RefreshConfig(
            source_schema="SRC",
            target_schema="TGT",
            tables=["T1"],
            auto_include_fk_parents=False,
        ),
    )


def test_plan_returns_scn_and_table_order() -> None:
    engine = _engine_same_instance()
    conn = make_mock_conn()
    conn.cursor().__enter__().fetchone.return_value = (98765,)

    from oracle_schema_refresh.endpoints.endpoint import Endpoint

    with patch.object(Endpoint, "connect", return_value=conn):
        with patched_introspect(
            discover_fk_parents=["T1"],
            build_dependency_graph={"T1": []},
        ):
            plan = engine.plan()  # type: ignore[union-attr]

    assert plan.scn == 98765
    assert plan.table_order == ["T1"]
    assert plan.server_version == 19  # default from helper


def test_plan_does_not_execute_ddl_or_dml() -> None:
    """plan() must touch only metadata. Any CREATE / TRUNCATE / INSERT
    in the executed-SQL list is a bug."""
    engine = _engine_same_instance()
    conn = make_mock_conn()
    seen: list[str] = []
    conn.cursor().__enter__().execute.side_effect = (
        lambda sql, *a, **kw: seen.append(sql)
    )

    from oracle_schema_refresh.endpoints.endpoint import Endpoint

    with patch.object(Endpoint, "connect", return_value=conn):
        with patched_introspect():
            engine.plan()  # type: ignore[union-attr]

    for s in seen:
        u = s.upper()
        assert "CREATE TABLE" not in u, s
        assert "TRUNCATE" not in u, s
        assert "INSERT" not in u, s
        assert "DROP TABLE" not in u, s
        assert "ALTER " not in u, s


def test_plan_resolves_fk_parents_when_enabled() -> None:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    engine = RefreshEngine(
        OracleConnection(username="u", password="p", dsn="h:1/s"),
        RefreshConfig(
            source_schema="SRC",
            target_schema="TGT",
            tables=["CHILD"],
            auto_include_fk_parents=True,
        ),
    )
    conn = make_mock_conn()

    from oracle_schema_refresh.endpoints.endpoint import Endpoint

    with patch.object(Endpoint, "connect", return_value=conn):
        with patched_introspect(
            discover_fk_parents=["CHILD", "PARENT"],
            build_dependency_graph={"CHILD": ["PARENT"], "PARENT": []},
        ):
            plan = engine.plan()  # type: ignore[union-attr]

    # parents first
    assert plan.table_order == ["PARENT", "CHILD"]
    assert plan.tables_resolved == ["CHILD", "PARENT"]


def test_plan_cross_host_uses_source_endpoint_for_scn() -> None:
    """When cross-host, SCN must come from the SOURCE endpoint, not target."""
    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src = Endpoint(name="src", dsn="src:1/s", username="u", password=SecretStr("p"))
    tgt = Endpoint(name="tgt", dsn="tgt:1/s", username="u", password=SecretStr("p"))
    cfg = RefreshConfig(
        source_schema="SRC",
        target_schema="TGT",
        tables=["T1"],
        auto_include_fk_parents=False,
        dblink="existing:L",
    )
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
    src_conn.cursor().__enter__().fetchone.return_value = (12345,)

    def fake_connect(self: Endpoint, **kw: object) -> object:
        return src_conn if self.dsn == "src:1/s" else tgt_conn

    with patch.object(Endpoint, "connect", autospec=True, side_effect=fake_connect):
        with patched_introspect():
            plan = engine.plan()  # type: ignore[union-attr]

    assert plan.scn == 12345
    assert any("current_scn" in s.lower() for s in src_sqls)
    assert not any("current_scn" in s.lower() for s in tgt_sqls)
