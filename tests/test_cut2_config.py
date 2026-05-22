"""Cut 2 RefreshConfig additions — strategy / max_parallel / chunks."""
from __future__ import annotations

import pytest


def test_strategy_default_is_auto() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    assert cfg.strategy == "auto"


def test_strategy_accepts_known_names() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    for name in (
        "auto", "direct_copy", "parallel_dml", "chunked_staging",
        "partition_exchange",
    ):
        cfg = RefreshConfig(
            source_schema="S", target_schema="T", tables=["T1"], strategy=name
        )
        assert cfg.strategy == name


def test_strategy_rejects_unknown() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="S", target_schema="T", tables=["T1"],
            strategy="warp_drive",
        )


def test_max_parallel_defaults_to_four() -> None:
    from oracle_schema_refresh.config import RefreshConfig

    cfg = RefreshConfig(source_schema="S", target_schema="T", tables=["T1"])
    assert cfg.max_parallel == 4
    assert cfg.max_chunks_per_table == 4


def test_max_parallel_must_be_positive() -> None:
    from pydantic import ValidationError

    from oracle_schema_refresh.config import RefreshConfig

    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="S", target_schema="T", tables=["T1"], max_parallel=0
        )
    with pytest.raises(ValidationError):
        RefreshConfig(
            source_schema="S", target_schema="T", tables=["T1"],
            max_chunks_per_table=0,
        )
