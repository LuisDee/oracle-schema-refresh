"""Tests for introspect.py — FK graph, topo sort, DDL extraction."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


def test_topological_sort_linear_chain() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    # C depends on B, B depends on A → order must be A, B, C
    graph = {"C": ["B"], "B": ["A"], "A": []}
    result = topological_sort(graph)
    assert result.index("A") < result.index("B") < result.index("C")


def test_topological_sort_diamond() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    # D depends on B and C; B and C both depend on A
    graph = {"D": ["B", "C"], "B": ["A"], "C": ["A"], "A": []}
    result = topological_sort(graph)
    assert result.index("A") < result.index("B")
    assert result.index("A") < result.index("C")
    assert result.index("B") < result.index("D")
    assert result.index("C") < result.index("D")


def test_topological_sort_single_node() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    assert topological_sort({"A": []}) == ["A"]


def test_topological_sort_no_edges() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    result = topological_sort({"A": [], "B": [], "C": []})
    assert set(result) == {"A", "B", "C"}


def test_topological_sort_detects_cycle() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    with pytest.raises(ValueError, match="[Cc]ycle"):
        topological_sort({"A": ["B"], "B": ["A"]})


def test_topological_sort_detects_three_node_cycle() -> None:
    from oracle_schema_refresh.introspect import topological_sort

    with pytest.raises(ValueError, match="[Cc]ycle"):
        topological_sort({"A": ["C"], "B": ["A"], "C": ["B"]})


# ---------------------------------------------------------------------------
# table_exists
# ---------------------------------------------------------------------------


def _make_conn(fetchone_return: object = None) -> MagicMock:
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_return
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_table_exists_true() -> None:
    from oracle_schema_refresh.introspect import table_exists

    conn = _make_conn(fetchone_return=("SUN_LEDGER",))
    assert table_exists(conn, "BACKOFFICE", "SUN_LEDGER") is True


def test_table_exists_false() -> None:
    from oracle_schema_refresh.introspect import table_exists

    conn = _make_conn(fetchone_return=None)
    assert table_exists(conn, "BACKOFFICE", "MISSING_TABLE") is False


# ---------------------------------------------------------------------------
# discover_fk_parents
# ---------------------------------------------------------------------------


def test_discover_fk_parents_returns_parent_in_same_schema() -> None:
    from oracle_schema_refresh.introspect import discover_fk_parents

    cur = MagicMock()
    # First call: CHILD_TABLE has parent PARENT_TABLE in BACKOFFICE
    # Second call: PARENT_TABLE has no parents
    cur.fetchall.side_effect = [
        [("PARENT_TABLE", "BACKOFFICE")],
        [],
    ]
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    result = discover_fk_parents(conn, "BACKOFFICE", ["CHILD_TABLE"])
    assert "PARENT_TABLE" in result
    assert "CHILD_TABLE" in result


def test_discover_fk_parents_ignores_cross_schema_parent() -> None:
    from oracle_schema_refresh.introspect import discover_fk_parents

    cur = MagicMock()
    # CHILD_TABLE has a FK parent in a DIFFERENT schema — should not be auto-included
    cur.fetchall.side_effect = [
        [("PARENT_TABLE", "OTHER_SCHEMA")],
    ]
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    result = discover_fk_parents(conn, "BACKOFFICE", ["CHILD_TABLE"])
    assert "PARENT_TABLE" not in result
    assert "CHILD_TABLE" in result


def test_discover_fk_parents_no_fks_returns_seed() -> None:
    from oracle_schema_refresh.introspect import discover_fk_parents

    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    result = discover_fk_parents(conn, "BACKOFFICE", ["SUN_LEDGER"])
    assert result == ["SUN_LEDGER"]


# ---------------------------------------------------------------------------
# build_dependency_graph
# ---------------------------------------------------------------------------


def test_build_dependency_graph_two_tables() -> None:
    from oracle_schema_refresh.introspect import build_dependency_graph

    cur = MagicMock()
    # CHILD depends on PARENT
    cur.fetchall.side_effect = [
        [("PARENT", "BACKOFFICE")],  # edges for CHILD
        [],                          # edges for PARENT
    ]
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    graph = build_dependency_graph(conn, "BACKOFFICE", ["CHILD", "PARENT"])
    assert "PARENT" in graph["CHILD"]
    assert graph["PARENT"] == []


def test_build_dependency_graph_no_deps() -> None:
    from oracle_schema_refresh.introspect import build_dependency_graph

    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    graph = build_dependency_graph(conn, "BACKOFFICE", ["T1", "T2"])
    assert graph == {"T1": [], "T2": []}


# ---------------------------------------------------------------------------
# get_table_ddl
# ---------------------------------------------------------------------------


def test_get_table_ddl_remaps_schema() -> None:
    from oracle_schema_refresh.introspect import get_table_ddl

    lob = MagicMock()
    lob.read.return_value = (
        '  CREATE TABLE "BACKOFFICE"."SUN_LEDGER" (ID NUMBER)  '
    )
    cur = MagicMock()
    cur.fetchone.return_value = (lob,)
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    ddl = get_table_ddl(conn, "BACKOFFICE", "SUN_LEDGER", "LDEBURNA")
    assert '"LDEBURNA"' in ddl
    assert '"BACKOFFICE"' not in ddl


def test_get_table_ddl_returns_stripped_string() -> None:
    from oracle_schema_refresh.introspect import get_table_ddl

    lob = MagicMock()
    lob.read.return_value = '  CREATE TABLE "BACKOFFICE"."T1" (X NUMBER)  \n'
    cur = MagicMock()
    cur.fetchone.return_value = (lob,)
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    ddl = get_table_ddl(conn, "BACKOFFICE", "T1", "LDEBURNA")
    assert ddl == ddl.strip()


def test_get_table_ddl_strips_sqlterminator_semicolon() -> None:
    """SQLTERMINATOR=TRUE appends ';' to the DDL — it must be removed before execute()."""
    from oracle_schema_refresh.introspect import get_table_ddl

    lob = MagicMock()
    lob.read.return_value = '  CREATE TABLE "BACKOFFICE"."T1" (X NUMBER)\n   ) ;\n'
    cur = MagicMock()
    cur.fetchone.return_value = (lob,)
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    ddl = get_table_ddl(conn, "BACKOFFICE", "T1", "LDEBURNA")
    assert not ddl.endswith(";")
    assert ")" in ddl  # table body still present


# ---------------------------------------------------------------------------
# get_index_ddl
# ---------------------------------------------------------------------------


def test_get_index_ddl_returns_statements() -> None:
    from oracle_schema_refresh.introspect import get_index_ddl

    lob = MagicMock()
    lob.read.return_value = (
        '  CREATE INDEX "BACKOFFICE"."IDX_1" ON "BACKOFFICE"."T1" (COL1);\n'
        '  CREATE INDEX "BACKOFFICE"."IDX_2" ON "BACKOFFICE"."T1" (COL2);\n'
    )
    cur = MagicMock()
    cur.fetchone.return_value = (lob,)
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    stmts = get_index_ddl(conn, "BACKOFFICE", "T1", "LDEBURNA")
    assert len(stmts) >= 1
    for s in stmts:
        assert '"LDEBURNA"' in s


def test_get_index_ddl_handles_no_indexes() -> None:
    """ORA-31608: object not found — treat as no indexes, return empty list."""
    import oracledb

    from oracle_schema_refresh.introspect import get_index_ddl

    cur = MagicMock()
    err = oracledb.DatabaseError()
    err.args = (MagicMock(code=31608, message="ORA-31608"),)
    cur.execute.side_effect = err
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    stmts = get_index_ddl(conn, "BACKOFFICE", "T1", "LDEBURNA")
    assert stmts == []


# ---------------------------------------------------------------------------
# get_fk_constraints_on_target
# ---------------------------------------------------------------------------


def test_get_fk_constraints_on_target() -> None:
    from oracle_schema_refresh.introspect import get_fk_constraints_on_target

    cur = MagicMock()
    cur.fetchall.return_value = [
        ("FK_CHILD_PARENT", "CHILD_TABLE", "ENABLED"),
        ("FK_OTHER", "OTHER_TABLE", "ENABLED"),
    ]
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    constraints = get_fk_constraints_on_target(conn, "LDEBURNA", ["CHILD_TABLE", "OTHER_TABLE"])
    assert len(constraints) == 2
    assert constraints[0]["name"] == "FK_CHILD_PARENT"
    assert constraints[0]["table"] == "CHILD_TABLE"


# ---------------------------------------------------------------------------
# get_table_row_count
# ---------------------------------------------------------------------------


def test_get_table_row_count() -> None:
    from oracle_schema_refresh.introspect import get_table_row_count

    cur = MagicMock()
    cur.fetchone.return_value = (42,)
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    count = get_table_row_count(conn, "BACKOFFICE", "SUN_LEDGER")
    assert count == 42
