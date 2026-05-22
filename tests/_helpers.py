"""Shared test helpers — kept out of test files so they don't pollute discovery."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch


def make_mock_conn() -> MagicMock:
    """Mock ``oracledb`` connection whose cursor() supports ``with`` and
    returns ``(0,)`` from ``fetchone`` and ``[]`` from ``fetchall`` by default.
    """
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


# Sensible defaults that let RefreshEngine.run complete without hitting Oracle.
# Each entry is patched as ``oracle_schema_refresh.engine.introspect.<name>``.
_INTROSPECT_DEFAULTS: dict[str, Any] = {
    "table_exists": True,
    "discover_fk_parents": ["T1"],
    "build_dependency_graph": {"T1": []},
    "get_fk_constraints_on_target": [],
    "get_table_columns": ["C1"],
    "get_identity_columns": [],
    "get_sequence_columns_via_triggers": [],
    "get_server_version": 19,
    "get_table_row_count": 0,
}


@contextmanager
def patched_introspect(
    *, side_effects: dict[str, Any] | None = None, **return_values: Any
) -> Iterator[dict[str, Any]]:
    """Patch every ``engine.introspect.*`` call ``RefreshEngine.run`` makes.

    Keyword arguments override the default ``return_value`` per function.
    ``side_effects={"fn": callable}`` sets ``side_effect`` instead — use when
    the value depends on the call args (e.g. row count differing by schema).

    Yields a dict ``{name: MagicMock}`` so individual call assertions are
    possible.
    """
    side_effects = side_effects or {}
    merged = {**_INTROSPECT_DEFAULTS, **return_values}
    with ExitStack() as stack:
        patches: dict[str, Any] = {}
        for name, value in merged.items():
            target = f"oracle_schema_refresh.engine.introspect.{name}"
            patches[name] = stack.enter_context(patch(target, return_value=value))
        for name, fn in side_effects.items():
            target = f"oracle_schema_refresh.engine.introspect.{name}"
            if name in patches:
                patches[name].side_effect = fn
            else:
                patches[name] = stack.enter_context(patch(target, side_effect=fn))
        yield patches
