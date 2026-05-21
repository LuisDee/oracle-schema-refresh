"""Tests for endpoints.Endpoint — Cut 0 abstraction with no behaviour change."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_endpoint_wraps_oracle_connection_fields() -> None:
    from oracle_schema_refresh.config import OracleConnection
    from oracle_schema_refresh.endpoints import Endpoint

    oc = OracleConnection(username="u", password="secret", dsn="h:1521/s")
    ep = Endpoint.from_oracle_connection(oc)

    assert ep.name == "default"
    assert ep.dsn == "h:1521/s"
    assert ep.username == "u"
    assert ep.password.get_secret_value() == "secret"
    assert ep.default_schema is None


def test_endpoint_password_is_not_in_repr() -> None:
    from pydantic import SecretStr

    from oracle_schema_refresh.endpoints import Endpoint

    ep = Endpoint(
        name="default",
        dsn="h:1521/s",
        username="u",
        password=SecretStr("hunter2"),
    )
    assert "hunter2" not in repr(ep)


def test_endpoint_from_oracle_connection_accepts_custom_name() -> None:
    from oracle_schema_refresh.config import OracleConnection
    from oracle_schema_refresh.endpoints import Endpoint

    oc = OracleConnection(username="u", password="p", dsn="h:1521/s")
    ep = Endpoint.from_oracle_connection(oc, name="dev_uk01")
    assert ep.name == "dev_uk01"


def test_endpoint_connect_calls_oracledb_with_resolved_credentials() -> None:
    from pydantic import SecretStr

    from oracle_schema_refresh.endpoints import Endpoint

    ep = Endpoint(
        name="default",
        dsn="h:1521/s",
        username="u",
        password=SecretStr("hunter2"),
    )

    fake_conn = MagicMock()
    with patch("oracledb.connect", return_value=fake_conn) as mock_connect:
        result = ep.connect()

    assert result is fake_conn
    mock_connect.assert_called_once()
    kwargs = mock_connect.call_args.kwargs
    assert kwargs["user"] == "u"
    assert kwargs["password"] == "hunter2"  # resolved from SecretStr
    assert kwargs["dsn"] == "h:1521/s"
    # Legacy 10s TCP connect timeout preserved as default.
    assert kwargs["tcp_connect_timeout"] == 10


def test_endpoint_connect_forwards_extra_kwargs() -> None:
    from pydantic import SecretStr

    from oracle_schema_refresh.endpoints import Endpoint

    ep = Endpoint(name="x", dsn="h:1/s", username="u", password=SecretStr("p"))

    with patch("oracledb.connect", return_value=MagicMock()) as mock_connect:
        ep.connect(mode=42, tcp_connect_timeout=5)

    kwargs = mock_connect.call_args.kwargs
    assert kwargs["mode"] == 42
    # Caller's explicit value beats the default.
    assert kwargs["tcp_connect_timeout"] == 5


# ---------------------------------------------------------------------------
# RefreshEngine — accepts Endpoint and OracleConnection equivalently
# ---------------------------------------------------------------------------


def _make_mock_conn() -> MagicMock:
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_refresh_engine_accepts_endpoint_directly() -> None:
    from pydantic import SecretStr

    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    ep = Endpoint(name="x", dsn="h:1/s", username="u", password=SecretStr("p"))
    cfg = RefreshConfig(source_schema="SRC", target_schema="TGT", tables=["T1"])

    engine = RefreshEngine(ep, cfg)
    assert engine._source is ep
    assert engine._target is ep


def test_refresh_engine_legacy_oracle_connection_wraps_into_endpoint() -> None:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    oc = OracleConnection(username="u", password="p", dsn="h:1/s")
    cfg = RefreshConfig(source_schema="SRC", target_schema="TGT", tables=["T1"])

    engine = RefreshEngine(oc, cfg)

    assert isinstance(engine._source, Endpoint)
    assert engine._source is engine._target  # same-instance refresh
    assert engine._source.dsn == "h:1/s"


def test_refresh_engine_from_endpoints_same_dsn_ok() -> None:
    from pydantic import SecretStr

    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src = Endpoint(name="s", dsn="h:1/s", username="u", password=SecretStr("p"))
    tgt = Endpoint(name="t", dsn="h:1/s", username="u2", password=SecretStr("p"))
    cfg = RefreshConfig(source_schema="SRC", target_schema="TGT", tables=["T1"])

    engine = RefreshEngine.from_endpoints(src, tgt, cfg)
    assert engine._source is src
    assert engine._target is tgt


def test_refresh_engine_from_endpoints_rejects_distinct_dsn() -> None:
    """Cross-host execution lands in Cut 1b; distinct DSNs must error early."""
    from pydantic import SecretStr

    from oracle_schema_refresh.config import RefreshConfig
    from oracle_schema_refresh.endpoints import Endpoint
    from oracle_schema_refresh.engine import RefreshEngine

    src = Endpoint(name="s", dsn="hostA:1521/svc", username="u", password=SecretStr("p"))
    tgt = Endpoint(name="t", dsn="hostB:1521/svc", username="u", password=SecretStr("p"))
    cfg = RefreshConfig(source_schema="SRC", target_schema="TGT", tables=["T1"])

    with pytest.raises(ValueError, match="Cross-host"):
        RefreshEngine.from_endpoints(src, tgt, cfg)


def test_refresh_engine_run_opens_connection_via_endpoint() -> None:
    """The engine must route through Endpoint.connect rather than calling
    oracledb.connect directly, so future endpoint behaviour (wallets, dblinks)
    has one place to land."""
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    oc = OracleConnection(username="u", password="p", dsn="h:1/s")
    cfg = RefreshConfig(
        source_schema="SRC",
        target_schema="TGT",
        tables=["T1"],
        auto_include_fk_parents=False,
    )
    engine = RefreshEngine(oc, cfg)
    mock_conn = _make_mock_conn()

    from oracle_schema_refresh.endpoints import Endpoint

    with patch.object(Endpoint, "connect", return_value=mock_conn) as ep_connect:
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
                        engine.run(dry_run=True)

    ep_connect.assert_called_once()
    mock_conn.close.assert_called_once()
