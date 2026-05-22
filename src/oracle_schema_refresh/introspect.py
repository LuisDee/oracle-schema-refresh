"""FK discovery, dependency graph, topological sort, and DDL extraction."""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any

import oracledb
import structlog

log = structlog.get_logger()


@dataclass
class TableProfile:
    """Strategy-choice inputs for a single table.

    ``size_mb`` is a rough estimate from ``DBA_SEGMENTS`` (or 0 if the
    user doesn't have segment access). ``rows`` is the optimiser
    statistic from ``ALL_TABLES.num_rows`` — outdated stats are OK for
    picking a strategy. ``has_long`` is a hard refusal flag because
    LONG columns can't be selected over a dblink.
    """

    schema: str
    table_name: str
    rows: int = 0
    size_mb: float = 0.0
    partitioned: bool = False
    has_lob: bool = False
    has_long: bool = False


def get_table_profile(conn: Any, schema: str, table_name: str) -> TableProfile:
    """Profile a table for strategy selection.

    Five queries — one per field. Cheap dictionary reads, fine to issue
    per-table at plan time. Failures fall back to safe defaults
    (``rows=0``, ``size_mb=0``) so an unprivileged session still gets a
    usable profile (the picker just chooses ``direct_copy``).
    """
    rows = 0
    size_mb = 0.0
    partitioned = False
    has_lob = False
    has_long = False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT num_rows FROM all_tables "
            "WHERE owner = :s AND table_name = :t",
            s=schema, t=table_name,
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            rows = int(row[0])

        cur.execute(
            "SELECT SUM(bytes) FROM dba_segments "
            "WHERE owner = :s AND segment_name = :t",
            s=schema, t=table_name,
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            size_mb = float(row[0]) / (1024 * 1024)

        cur.execute(
            "SELECT partitioned FROM all_tables "
            "WHERE owner = :s AND table_name = :t",
            s=schema, t=table_name,
        )
        row = cur.fetchone()
        if row and row[0] == "YES":
            partitioned = True

        cur.execute(
            "SELECT COUNT(*) FROM all_tab_columns "
            "WHERE owner = :s AND table_name = :t "
            "AND data_type IN ('CLOB','BLOB','NCLOB','BFILE')",
            s=schema, t=table_name,
        )
        row = cur.fetchone()
        if row and row[0]:
            has_lob = int(row[0]) > 0

        cur.execute(
            "SELECT COUNT(*) FROM all_tab_columns "
            "WHERE owner = :s AND table_name = :t "
            "AND data_type = 'LONG'",
            s=schema, t=table_name,
        )
        row = cur.fetchone()
        if row and row[0]:
            has_long = int(row[0]) > 0

    return TableProfile(
        schema=schema,
        table_name=table_name,
        rows=rows,
        size_mb=size_mb,
        partitioned=partitioned,
        has_lob=has_lob,
        has_long=has_long,
    )

# ORA-31608: object/type/attribute/named not found (no indexes found for table)
_ORA_NO_OBJECTS = 31608

# Trigger-body pattern: ":NEW.col := [schema.]seq.NEXTVAL".
# Case-insensitive, tolerates whitespace and the optional schema prefix.
_TRIGGER_SEQ_NEXTVAL = re.compile(
    r":new\.([a-z0-9_$#]+)\s*:?=\s*(?:[a-z0-9_$#]+\.)?([a-z0-9_$#]+)\.nextval",
    re.IGNORECASE,
)


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Return nodes in dependency order — parents before children.

    Args:
        graph: Adjacency list where graph[node] = list of parent nodes
               that *node* depends on.

    Returns:
        Ordered list with all parents before their dependants.

    Raises:
        ValueError: If a circular dependency is detected.
    """
    # Compute in-degree: number of parents each node has (within this graph)
    in_degree: dict[str, int] = {n: 0 for n in graph}
    for node, parents in graph.items():
        for parent in parents:
            if parent in in_degree:
                in_degree[node] += 1

    # Reverse map: parent -> children that depend on it
    dependents: dict[str, list[str]] = {n: [] for n in graph}
    for node, parents in graph.items():
        for parent in parents:
            if parent in dependents:
                dependents[parent].append(node)

    # Kahn's algorithm — start with nodes that have no dependencies
    queue: deque[str] = deque(n for n, deg in in_degree.items() if deg == 0)
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for child in dependents[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(result) != len(graph):
        cycle_nodes = [n for n in graph if n not in result]
        raise ValueError(f"FK cycle detected among tables: {cycle_nodes}")

    return result


def table_exists(conn: Any, schema: str, table_name: str) -> bool:
    """Return True if table_name exists in the given schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM all_tables "
            "WHERE owner = :schema AND table_name = :table_name",
            schema=schema,
            table_name=table_name,
        )
        return cur.fetchone() is not None


def discover_fk_parents(
    conn: Any,
    source_schema: str,
    seed_tables: list[str],
) -> list[str]:
    """Walk the FK graph from seed_tables and return the full table set.

    Only includes parent tables that belong to source_schema. Cross-schema
    FK parents are ignored (the FK DDL will reference them as-is).

    Args:
        conn: Active oracledb connection.
        source_schema: Schema owning the seed tables.
        seed_tables: Initial table list to expand.

    Returns:
        Deduplicated list of all tables (seeds + discovered parents).
    """
    visited: set[str] = set(seed_tables)
    queue: deque[str] = deque(seed_tables)

    while queue:
        table = queue.popleft()
        log.debug("discover_fk_parents", table=table, schema=source_schema)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT rc.table_name AS parent_table,
                       rc.owner AS parent_schema
                  FROM all_constraints c
                  JOIN all_constraints rc
                    ON c.r_constraint_name = rc.constraint_name
                   AND c.r_owner = rc.owner
                 WHERE c.owner = :source_schema
                   AND c.table_name = :table_name
                   AND c.constraint_type = 'R'
                """,
                source_schema=source_schema,
                table_name=table,
            )
            for parent_table, parent_schema in cur.fetchall():
                if parent_schema == source_schema and parent_table not in visited:
                    visited.add(parent_table)
                    queue.append(parent_table)

    # Preserve seed order, then append discovered parents
    result: list[str] = list(seed_tables)
    for t in visited:
        if t not in seed_tables:
            result.append(t)
    return result


def build_dependency_graph(
    conn: Any,
    source_schema: str,
    tables: list[str],
) -> dict[str, list[str]]:
    """Build an adjacency list: table -> [parent tables it depends on].

    Only edges within the provided tables set are included.

    Args:
        conn: Active oracledb connection.
        source_schema: Schema owning the tables.
        tables: Full table set to build the graph for.

    Returns:
        Dict mapping each table to its list of FK parent tables.
    """
    table_set = set(tables)
    graph: dict[str, list[str]] = {t: [] for t in tables}

    for table in tables:
        log.debug("build_dependency_graph", table=table, schema=source_schema)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT rc.table_name AS parent_table,
                       rc.owner AS parent_schema
                  FROM all_constraints c
                  JOIN all_constraints rc
                    ON c.r_constraint_name = rc.constraint_name
                   AND c.r_owner = rc.owner
                 WHERE c.owner = :source_schema
                   AND c.table_name = :table_name
                   AND c.constraint_type = 'R'
                """,
                source_schema=source_schema,
                table_name=table,
            )
            for parent_table, parent_schema in cur.fetchall():
                if parent_table in table_set:
                    graph[table].append(parent_table)

    return graph


def _configure_ddl_transforms(cur: Any) -> None:
    """Set session-level DBMS_METADATA transform parameters."""
    for param, value in [
        ("STORAGE", "FALSE"),
        ("TABLESPACE", "FALSE"),
        ("SEGMENT_ATTRIBUTES", "FALSE"),
        ("SQLTERMINATOR", "TRUE"),
        ("REF_CONSTRAINTS", "FALSE"),
    ]:
        cur.execute(
            f"BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
            f"DBMS_METADATA.SESSION_TRANSFORM,'{param}',{value}); END;"
        )


def get_table_ddl(
    conn: Any,
    source_schema: str,
    table_name: str,
    target_schema: str,
) -> str:
    """Extract CREATE TABLE DDL remapped to target_schema.

    Uses DBMS_METADATA.GET_DDL with session-level transform parameters.
    Schema name replacement is performed on the returned DDL string.

    Args:
        conn: Active oracledb connection.
        source_schema: Schema that owns the table.
        table_name: Table to extract DDL for.
        target_schema: Schema name to substitute in the DDL.

    Returns:
        Remapped DDL string (stripped of leading/trailing whitespace).
    """
    with conn.cursor() as cur:
        _configure_ddl_transforms(cur)
        cur.execute(
            "SELECT DBMS_METADATA.GET_DDL('TABLE', :name, :schema) FROM DUAL",
            name=table_name,
            schema=source_schema,
        )
        row = cur.fetchone()
        ddl: str = row[0].read()

    # Oracle always quotes schema names in DDL output
    ddl = ddl.replace(f'"{source_schema}".', f'"{target_schema}".')
    # Also handle unquoted form (defensive)
    ddl = ddl.replace(f"{source_schema}.", f"{target_schema}.")
    # SQLTERMINATOR=TRUE appends a trailing ";" — strip it because
    # oracledb.execute() does not accept SQL with a semicolon terminator.
    return ddl.strip().rstrip(";").strip()


def get_index_ddl(
    conn: Any,
    source_schema: str,
    table_name: str,
    target_schema: str,
) -> list[str]:
    """Extract CREATE INDEX DDL statements remapped to target_schema.

    Returns an empty list if the table has no indexes (ORA-31608 is handled
    gracefully rather than raised).

    Args:
        conn: Active oracledb connection.
        source_schema: Schema that owns the table.
        table_name: Table to extract index DDL for.
        target_schema: Schema name to substitute in the DDL.

    Returns:
        List of individual CREATE INDEX statements (may be empty).
    """
    with conn.cursor() as cur:
        try:
            _configure_ddl_transforms(cur)
            cur.execute(
                "SELECT DBMS_METADATA.GET_DEPENDENT_DDL('INDEX', :name, :schema) FROM DUAL",
                name=table_name,
                schema=source_schema,
            )
            row = cur.fetchone()
            raw: str = row[0].read()
        except oracledb.DatabaseError as exc:
            if exc.args and exc.args[0].code == _ORA_NO_OBJECTS:
                log.debug("get_index_ddl_no_indexes", table=table_name)
                return []
            raise

    # Remap schema, split on semicolons into individual statements
    raw = raw.replace(f'"{source_schema}".', f'"{target_schema}".')
    raw = raw.replace(f"{source_schema}.", f"{target_schema}.")

    stmts = [s.strip() for s in raw.split(";") if s.strip()]
    return stmts


def get_fk_constraints_on_target(
    conn: Any,
    target_schema: str,
    tables: list[str],
) -> list[dict[str, str]]:
    """Return FK constraints on the target schema tables.

    Args:
        conn: Active oracledb connection.
        target_schema: Schema to query.
        tables: Tables to query constraints for.

    Returns:
        List of dicts with keys: name, table, status.
    """
    if not tables:
        return []

    placeholders = ", ".join(f":t{i}" for i in range(len(tables)))
    params: dict[str, Any] = {"schema": target_schema}
    params.update({f"t{i}": t for i, t in enumerate(tables)})

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT constraint_name, table_name, status
              FROM all_constraints
             WHERE owner = :schema
               AND constraint_type = 'R'
               AND table_name IN ({placeholders})
            ORDER BY table_name, constraint_name
            """,
            **params,
        )
        return [
            {"name": row[0], "table": row[1], "status": row[2]}
            for row in cur.fetchall()
        ]


def get_table_row_count(
    conn: Any,
    schema: str,
    table_name: str,
    as_of_scn: int | None = None,
) -> int:
    """Return the row count for a table.

    If ``as_of_scn`` is provided, the count uses a Flashback Query read
    (``AS OF SCN :scn``) so it matches a job's consistent snapshot.
    """
    with conn.cursor() as cur:
        if as_of_scn is None:
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')  # noqa: S608
        else:
            cur.execute(
                f'SELECT COUNT(*) FROM "{schema}"."{table_name}" AS OF SCN :scn',  # noqa: S608
                scn=as_of_scn,
            )
        row = cur.fetchone()
        return int(row[0])


def get_table_columns(conn: Any, schema: str, table_name: str) -> list[str]:
    """Return the column names of a table in declaration order.

    Used to generate explicit column lists for INSERT … SELECT so adding a
    column on the source can't break the target load via a position
    mismatch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
              FROM all_tab_columns
             WHERE owner = :schema AND table_name = :table_name
             ORDER BY column_id
            """,
            schema=schema,
            table_name=table_name,
        )
        return [row[0] for row in cur.fetchall()]


def get_identity_columns(
    conn: Any, schema: str, table_name: str
) -> list[tuple[str, str]]:
    """Return ``(column_name, sequence_name)`` for each Oracle-12c+ identity
    column on the table.

    Classic sequence-backed columns (trigger sets ``:NEW.col := seq.NEXTVAL``)
    are picked up separately by :func:`get_sequence_columns_via_triggers`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, sequence_name
              FROM all_tab_identity_cols
             WHERE owner = :schema AND table_name = :table_name
            """,
            schema=schema,
            table_name=table_name,
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def get_sequence_columns_via_triggers(
    conn: Any, schema: str, table_name: str
) -> list[tuple[str, str]]:
    """Return ``(column_name, sequence_name)`` for trigger-driven sequence
    columns on the table — best-effort.

    Reads BEFORE-INSERT trigger bodies from ``all_triggers`` and pattern-matches
    ``:NEW.col := [schema.]seq.NEXTVAL``. Won't catch every dialect (PL/SQL
    that wraps the assignment in conditionals, e.g.) but covers the
    overwhelming common case. The full-coverage answer requires the planned
    Cut 2 strategy that consults schema-supplied mapping, hence the
    best-effort label.

    Catalog or LOB-read errors are swallowed with a logged warning — the
    rest of the run must not abort because we couldn't sniff sequences.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trigger_name, trigger_body
                  FROM all_triggers
                 WHERE owner = :schema
                   AND table_name = :table_name
                   AND triggering_event LIKE '%INSERT%'
                   AND status = 'ENABLED'
                """,
                schema=schema,
                table_name=table_name,
            )
            rows = cur.fetchall()
    except oracledb.DatabaseError as exc:
        log.warning("trigger_scan_failed", table=table_name, error=str(exc))
        return []

    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for trigger_name, body in rows:
        try:
            body_text = body.read() if hasattr(body, "read") else str(body or "")
        except oracledb.DatabaseError as exc:
            log.warning(
                "trigger_body_read_failed",
                trigger=trigger_name,
                error=str(exc),
            )
            continue
        for match in _TRIGGER_SEQ_NEXTVAL.finditer(body_text):
            col = match.group(1).upper()
            seq = match.group(2).upper()
            key = (col, seq)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def supports_dbms_parallel_execute(conn: Any) -> bool:
    """True iff the session has EXECUTE on DBMS_PARALLEL_EXECUTE.

    Used by the strategy picker to downgrade ``chunked_staging`` to
    ``parallel_dml`` when the priv is missing rather than fail at
    runtime in the middle of phase 4.
    """
    sql = (
        "SELECT 1 FROM all_tab_privs "
        "WHERE table_name = 'DBMS_PARALLEL_EXECUTE' "
        "AND privilege = 'EXECUTE' "
        "AND (grantee = USER OR grantee IN "
        "  (SELECT granted_role FROM user_role_privs))"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone() is not None
    except oracledb.DatabaseError:
        return False


def get_server_version(conn: Any) -> int:
    """Return the major Oracle server version (e.g. 19, 21, 23).

    Uses ``DBMS_DB_VERSION.VERSION`` which is a PL/SQL constant accessible
    to any session — avoids the v$version / v$instance permission pitfalls.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT dbms_db_version.version FROM dual")
        row = cur.fetchone()
        return int(row[0])
