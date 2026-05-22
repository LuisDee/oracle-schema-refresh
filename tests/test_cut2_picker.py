"""Strategy picker — auto-selection from ``TableProfile``."""
from __future__ import annotations

import pytest


def _profile(**kwargs: object) -> object:
    from oracle_schema_refresh.introspect import TableProfile

    defaults = dict(schema="S", table_name="T", rows=0, size_mb=0.0)
    defaults.update(kwargs)
    return TableProfile(**defaults)  # type: ignore[arg-type]


def test_picker_chooses_direct_copy_for_small_tables() -> None:
    from oracle_schema_refresh.strategy.picker import pick_strategy

    # ~ 100k rows, ~10MB — small. direct_copy fits.
    name = pick_strategy(_profile(rows=100_000, size_mb=10.0), supports_dpe=True)
    assert name == "direct_copy"


def test_picker_chooses_parallel_dml_for_medium_tables() -> None:
    from oracle_schema_refresh.strategy.picker import pick_strategy

    # Above the direct_copy threshold but below chunked_staging — uses
    # parallel_dml (single-session PARALLEL hint).
    name = pick_strategy(_profile(rows=5_000_000, size_mb=500.0), supports_dpe=True)
    assert name == "parallel_dml"


def test_picker_chooses_chunked_staging_for_large_tables() -> None:
    from oracle_schema_refresh.strategy.picker import pick_strategy

    # ~ 5 GB, ~ 200M rows — chunked staging.
    name = pick_strategy(
        _profile(rows=200_000_000, size_mb=5_000.0), supports_dpe=True
    )
    assert name == "chunked_staging"


def test_picker_downgrades_to_parallel_dml_without_dpe_privilege() -> None:
    """If the session can't run DBMS_PARALLEL_EXECUTE, chunked_staging is
    silently downgraded to parallel_dml (still useful — one PARALLEL
    statement)."""
    from oracle_schema_refresh.strategy.picker import pick_strategy

    name = pick_strategy(
        _profile(rows=200_000_000, size_mb=5_000.0), supports_dpe=False
    )
    assert name == "parallel_dml"


def test_picker_refuses_tables_with_long_columns() -> None:
    """LONG columns can't be selected over a dblink. Cross-host loads of
    LONG tables must be refused at plan time."""
    from oracle_schema_refresh.strategy.picker import pick_strategy

    with pytest.raises(ValueError, match="LONG"):
        pick_strategy(_profile(has_long=True), supports_dpe=True)


def test_picker_uses_parallel_dml_for_lob_heavy_tables() -> None:
    """Even when size warrants chunked_staging, a LOB-heavy table goes
    to parallel_dml — chunking gains little when the bottleneck is LOB
    shipping over the dblink."""
    from oracle_schema_refresh.strategy.picker import pick_strategy

    name = pick_strategy(
        _profile(rows=200_000_000, size_mb=10_000.0, has_lob=True),
        supports_dpe=True,
    )
    assert name == "parallel_dml"


def test_picker_explicit_override() -> None:
    """An explicit ``override`` argument always wins."""
    from oracle_schema_refresh.strategy.picker import pick_strategy

    name = pick_strategy(
        _profile(rows=10, size_mb=0.1),
        supports_dpe=True,
        override="chunked_staging",
    )
    assert name == "chunked_staging"


def test_picker_explicit_override_validates() -> None:
    from oracle_schema_refresh.strategy.picker import pick_strategy

    with pytest.raises(ValueError, match="unknown strategy"):
        pick_strategy(_profile(), supports_dpe=True, override="warp_drive")
