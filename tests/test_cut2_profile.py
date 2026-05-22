"""TableProfile + ``introspect.get_table_profile`` — drives strategy choice."""
from __future__ import annotations

from tests._helpers import make_mock_conn


def test_table_profile_dataclass_defaults() -> None:
    from oracle_schema_refresh.introspect import TableProfile

    p = TableProfile(schema="S", table_name="T", rows=10, size_mb=1.5)
    assert p.partitioned is False
    assert p.has_lob is False
    assert p.has_long is False


def test_get_table_profile_reads_rows_and_size_from_dba_views() -> None:
    """Profile uses ALL_TABLES.num_rows and dba_segments sum(bytes)."""
    from oracle_schema_refresh import introspect

    conn = make_mock_conn()
    cur = conn.cursor().__enter__()

    # Sequence the responses for each .execute → .fetchone pair.
    responses = [
        (1000,),       # ALL_TABLES num_rows
        (16777216,),   # SUM(bytes) from segments (16 MB)
        ("NO",),       # partitioned
        (0,),          # LOB count
        (0,),          # LONG count
    ]
    cur.fetchone.side_effect = list(responses)

    profile = introspect.get_table_profile(conn, "S", "T")
    assert profile.rows == 1000
    assert profile.size_mb == 16.0
    assert profile.partitioned is False
    assert profile.has_lob is False
    assert profile.has_long is False


def test_get_table_profile_detects_partitioned_lob_long() -> None:
    from oracle_schema_refresh import introspect

    conn = make_mock_conn()
    cur = conn.cursor().__enter__()
    cur.fetchone.side_effect = [
        (5_000_000,),
        (2 * 1024 * 1024 * 1024,),  # 2 GB
        ("YES",),
        (3,),    # 3 LOB columns
        (1,),    # 1 LONG column
    ]

    p = introspect.get_table_profile(conn, "S", "T")
    assert p.rows == 5_000_000
    assert p.size_mb == 2048.0
    assert p.partitioned is True
    assert p.has_lob is True
    assert p.has_long is True


def test_get_table_profile_missing_size_estimates_to_zero() -> None:
    """When dba_segments returns NULL (table not yet analysed, or no
    segment privileges), size_mb should be 0 rather than None."""
    from oracle_schema_refresh import introspect

    conn = make_mock_conn()
    cur = conn.cursor().__enter__()
    cur.fetchone.side_effect = [
        (None,),       # num_rows not collected
        (None,),       # no segment access
        ("NO",),
        (0,),
        (0,),
    ]

    p = introspect.get_table_profile(conn, "S", "T")
    assert p.rows == 0
    assert p.size_mb == 0.0
