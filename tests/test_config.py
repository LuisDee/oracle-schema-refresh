"""Tests for config.py — OracleConnection and RefreshConfig models."""
import pytest


def test_oracle_connection_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_USERNAME", "testuser")
    monkeypatch.setenv("ORACLE_PASSWORD", "testpass")
    monkeypatch.setenv("ORACLE_DSN", "host:1521/svc")
    from oracle_schema_refresh.config import OracleConnection

    conn = OracleConnection()
    assert conn.username == "testuser"
    assert conn.password.get_secret_value() == "testpass"
    assert conn.dsn == "host:1521/svc"


def test_refresh_config_defaults() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="SRC", target_schema="TGT", tables=["T1"])
    assert cfg.auto_include_fk_parents is True
    assert cfg.recreate_tables is False
    assert cfg.commit_mode == "per_table"
    assert cfg.insert_hint == "/*+ APPEND */"


def test_refresh_config_requires_at_least_one_table() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(source_schema="SRC", target_schema="TGT", tables=[])


def test_refresh_config_uppercases_schemas_and_tables() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="backoffice", target_schema="ldeburna", tables=["sun_ledger"])
    assert cfg.source_schema == "BACKOFFICE"
    assert cfg.target_schema == "LDEBURNA"
    assert cfg.tables == ["SUN_LEDGER"]


def test_refresh_config_invalid_commit_mode() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="SRC",
            target_schema="TGT",
            tables=["T1"],
            commit_mode="invalid",  # type: ignore[arg-type]
        )


def test_oracle_connection_password_is_secret_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_USERNAME", "testuser")
    monkeypatch.setenv("ORACLE_PASSWORD", "secret123")
    monkeypatch.setenv("ORACLE_DSN", "host:1521/svc")
    from pydantic import SecretStr

    from oracle_schema_refresh.config import OracleConnection

    conn = OracleConnection()
    assert isinstance(conn.password, SecretStr)
    assert "secret123" not in repr(conn)


def test_oracle_connection_missing_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove all ORACLE_* vars to force a missing-field error
    for var in ("ORACLE_USERNAME", "ORACLE_PASSWORD", "ORACLE_DSN"):
        monkeypatch.delenv(var, raising=False)

    from oracle_schema_refresh.config import OracleConnection

    with pytest.raises(Exception):  # ValidationError from pydantic-settings
        OracleConnection()
