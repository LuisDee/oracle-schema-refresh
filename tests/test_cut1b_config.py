"""Cut 1b config additions — dblink field on RefreshConfig."""
from __future__ import annotations

import pytest


def test_refresh_config_dblink_default_none() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    assert cfg.dblink is None


def test_refresh_config_dblink_existing_form() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(
        source_schema="S",
        target_schema="T",
        tables=["T1"],
        dblink="existing:SRC_LINK",
    )
    assert cfg.dblink == "existing:SRC_LINK"


def test_refresh_config_dblink_session_form() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(
        source_schema="S",
        target_schema="T",
        tables=["T1"],
        dblink="session",
    )
    assert cfg.dblink == "session"


def test_refresh_config_dblink_rejects_unknown_form() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="S",
            target_schema="T",
            tables=["T1"],
            dblink="garbage",
        )


def test_refresh_config_dblink_rejects_empty_name() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="S",
            target_schema="T",
            tables=["T1"],
            dblink="existing:",
        )
