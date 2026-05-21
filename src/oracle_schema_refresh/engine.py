"""RefreshEngine — orchestrates the 5-phase schema refresh."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import oracledb
import structlog

from oracle_schema_refresh import introspect
from oracle_schema_refresh.config import OracleConnection, RefreshConfig
from oracle_schema_refresh.endpoints import Endpoint

log = structlog.get_logger()

# ORA error codes that are safe to ignore (idempotent operations)
_IDEMPOTENT_ORA_CODES: frozenset[int] = frozenset({
    955,   # ORA-00955: name already used by an existing object
    2275,  # ORA-02275: such a referential constraint already exists
    1408,  # ORA-01408: such column list already indexed
})

# ORA-02298: cannot validate — parent keys not found
_ORA_FK_VALIDATE_FAIL = 2298


@dataclass
class TableResult:
    """Result for a single table refresh."""

    table_name: str
    status: str  # "ok" | "skipped" | "failed"
    rows_loaded: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class RefreshResult:
    """Aggregate result for a full refresh run."""

    success: bool
    tables_requested: list[str]
    tables_resolved: list[str]
    table_order: list[str]
    table_results: list[TableResult] = field(default_factory=list)
    constraints_disabled: list[str] = field(default_factory=list)
    constraints_reenabled: list[str] = field(default_factory=list)
    total_duration_seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary dict."""
        return {
            "success": self.success,
            "tables_requested": self.tables_requested,
            "table_order": self.table_order,
            "results": [
                {
                    "table": r.table_name,
                    "status": r.status,
                    "rows": r.rows_loaded,
                    "error": r.error,
                }
                for r in self.table_results
            ],
            "constraints_disabled": self.constraints_disabled,
            "constraints_reenabled": self.constraints_reenabled,
            "total_seconds": round(self.total_duration_seconds, 2),
        }


class RefreshEngine:
    """Orchestrates the 5-phase idempotent schema refresh.

    Accepts either a legacy ``OracleConnection`` (env-loaded credentials) or
    an :class:`Endpoint`. In both cases the same endpoint is currently used
    for source and target; Cut 1b will split these.
    """

    def __init__(
        self,
        oracle_conn: OracleConnection | Endpoint,
        config: RefreshConfig,
    ) -> None:
        if isinstance(oracle_conn, Endpoint):
            endpoint = oracle_conn
        else:
            endpoint = Endpoint.from_oracle_connection(oracle_conn)
        # Same endpoint for source and target today — Cut 1b splits these.
        self._source: Endpoint = endpoint
        self._target: Endpoint = endpoint
        self._config = config

    @classmethod
    def from_endpoints(
        cls,
        source: Endpoint,
        target: Endpoint,
        config: RefreshConfig,
    ) -> RefreshEngine:
        """Construct an engine with distinct source / target endpoints.

        Cut 0 still requires both endpoints to share a DSN — cross-host
        execution lands in Cut 1b. Passing distinct DSNs today raises
        ``ValueError`` so callers don't silently get same-instance behaviour
        when they expect cross-host.
        """
        if source.dsn != target.dsn:
            raise ValueError(
                "Cross-host endpoints not yet supported (lands in Cut 1b). "
                f"source.dsn={source.dsn!r} target.dsn={target.dsn!r}"
            )
        instance = cls.__new__(cls)
        instance._source = source
        instance._target = target
        instance._config = config
        return instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> RefreshResult:
        """Execute a full refresh run.

        Args:
            dry_run: If True, log every action but execute no DDL/DML.

        Returns:
            RefreshResult with per-table outcomes and totals.
        """
        t0 = time.monotonic()
        # Same-instance refresh: source and target are the same endpoint,
        # one connection handles both. Cut 1b will open a connection per
        # endpoint and route reads/writes accordingly.
        conn = self._target.connect()
        try:
            result = self._execute(conn, dry_run=dry_run)
        finally:
            conn.close()
        result.total_duration_seconds = time.monotonic() - t0
        log.info(
            "refresh_complete",
            success=result.success,
            total_seconds=round(result.total_duration_seconds, 2),
            dry_run=dry_run,
        )
        return result

    # ------------------------------------------------------------------
    # Internal orchestration
    # ------------------------------------------------------------------

    def _execute(self, conn: Any, dry_run: bool) -> RefreshResult:
        cfg = self._config
        overall_success = True

        # ── Phase 1: INTROSPECT ────────────────────────────────────────
        log.info("phase_start", phase=1, description="introspect")
        seed_tables = list(cfg.tables)

        if cfg.auto_include_fk_parents:
            resolved = introspect.discover_fk_parents(conn, cfg.source_schema, seed_tables)
        else:
            resolved = list(seed_tables)

        graph = introspect.build_dependency_graph(conn, cfg.source_schema, resolved)
        table_order = introspect.topological_sort(graph)  # parents first

        log.info(
            "phase1_complete",
            requested=seed_tables,
            resolved=resolved,
            order=table_order,
        )

        # ── Phase 2: PREPARE ───────────────────────────────────────────
        log.info("phase_start", phase=2, description="prepare tables")
        for table in table_order:
            if not introspect.table_exists(conn, cfg.source_schema, table):
                log.warning("source_table_missing", table=table, schema=cfg.source_schema)
                continue  # handled gracefully in Phase 4

            if not introspect.table_exists(conn, cfg.target_schema, table):
                log.info("creating_table", table=table, target=cfg.target_schema)
                if not dry_run:
                    self._create_table(conn, table)
            elif cfg.recreate_tables:
                log.info("recreating_table", table=table, target=cfg.target_schema)
                if not dry_run:
                    self._drop_table(conn, table)
                    self._create_table(conn, table)
            else:
                log.info("table_exists_keep_structure", table=table)

        # ── Phase 3: DISABLE FK CONSTRAINTS ───────────────────────────
        log.info("phase_start", phase=3, description="disable FK constraints")
        constraints = introspect.get_fk_constraints_on_target(
            conn, cfg.target_schema, table_order
        )
        disabled_constraints: list[str] = []
        for c in constraints:
            if c["status"] == "ENABLED":
                log.info("disabling_constraint", name=c["name"], table=c["table"])
                if not dry_run:
                    self._disable_constraint(conn, cfg.target_schema, c["table"], c["name"])
                disabled_constraints.append(c["name"])

        # ── Phase 4: TRUNCATE AND LOAD ─────────────────────────────────
        log.info("phase_start", phase=4, description="truncate and load")
        table_results: list[TableResult] = []
        reverse_order = list(reversed(table_order))

        # Truncate in reverse order (children first)
        for table in reverse_order:
            if not introspect.table_exists(conn, cfg.target_schema, table):
                continue
            log.info("truncating", table=table, target=cfg.target_schema)
            if not dry_run:
                self._truncate(conn, cfg.target_schema, table)

        # Insert in forward order (parents first)
        for table in table_order:
            if not introspect.table_exists(conn, cfg.source_schema, table):
                table_results.append(TableResult(table_name=table, status="skipped"))
                log.warning("table_skipped_missing_source", table=table)
                continue

            t_start = time.monotonic()
            try:
                if not dry_run:
                    self._insert_table(conn, table)
                    if cfg.commit_mode == "per_table":
                        conn.commit()
                rows = (
                    introspect.get_table_row_count(conn, cfg.target_schema, table)
                    if not dry_run
                    else 0
                )
                dur = time.monotonic() - t_start
                log.info("table_loaded", table=table, rows=rows, duration=round(dur, 2))
                table_results.append(
                    TableResult(
                        table_name=table,
                        status="ok",
                        rows_loaded=rows,
                        duration_seconds=dur,
                    )
                )
            except (oracledb.DatabaseError, oracledb.InterfaceError) as exc:
                dur = time.monotonic() - t_start
                log.error("table_load_failed", table=table, error=str(exc))
                overall_success = False
                table_results.append(
                    TableResult(
                        table_name=table,
                        status="failed",
                        duration_seconds=dur,
                        error=str(exc),
                    )
                )

        if not dry_run and cfg.commit_mode == "all_or_nothing":
            conn.commit()

        # ── Phase 5: RE-ENABLE FK CONSTRAINTS ─────────────────────────
        log.info("phase_start", phase=5, description="re-enable FK constraints")
        reenabled: list[str] = []
        for c in constraints:
            if c["name"] in disabled_constraints:
                log.info("enabling_constraint", name=c["name"], table=c["table"])
                if not dry_run:
                    try:
                        self._enable_constraint(conn, cfg.target_schema, c["table"], c["name"])
                        reenabled.append(c["name"])
                    except oracledb.DatabaseError as exc:
                        code = getattr(exc.args[0], "code", None) if exc.args else None
                        if code == _ORA_FK_VALIDATE_FAIL:
                            log.error(
                                "constraint_reenable_failed",
                                name=c["name"],
                                table=c["table"],
                                error=str(exc),
                            )
                            overall_success = False
                        else:
                            raise
                else:
                    reenabled.append(c["name"])

        return RefreshResult(
            success=overall_success,
            tables_requested=seed_tables,
            tables_resolved=resolved,
            table_order=table_order,
            table_results=table_results,
            constraints_disabled=disabled_constraints,
            constraints_reenabled=reenabled,
        )

    # ------------------------------------------------------------------
    # DDL / DML helpers
    # ------------------------------------------------------------------

    def _safe_execute(
        self, conn: Any, sql: str, ignore_codes: frozenset[int] = _IDEMPOTENT_ORA_CODES
    ) -> None:
        """Execute SQL, swallowing known idempotent ORA error codes."""
        with conn.cursor() as cur:
            try:
                cur.execute(sql)
            except oracledb.DatabaseError as exc:
                code = getattr(exc.args[0], "code", None) if exc.args else None
                if code in ignore_codes:
                    log.debug("idempotent_error_swallowed", code=code, sql=sql[:80])
                else:
                    raise

    def _create_table(self, conn: Any, table: str) -> None:
        cfg = self._config
        ddl = introspect.get_table_ddl(conn, cfg.source_schema, table, cfg.target_schema)
        self._safe_execute(conn, ddl)

        # FK constraints as ALTER TABLE (emitted separately via REF_CONSTRAINTS=FALSE
        # in get_table_ddl; would need a separate GET_DDL call for 'REF_CONSTRAINT'
        # object type — skipped for v0.1 since FK DDL is optional at create time and
        # constraints are disabled/re-enabled around the data load anyway)

        for index_ddl in introspect.get_index_ddl(
            conn, cfg.source_schema, table, cfg.target_schema
        ):
            self._safe_execute(conn, index_ddl)

    def _drop_table(self, conn: Any, table: str) -> None:
        cfg = self._config
        sql = f'DROP TABLE "{cfg.target_schema}"."{table}" CASCADE CONSTRAINTS PURGE'
        log.info("drop_table", sql=sql)
        with conn.cursor() as cur:
            cur.execute(sql)

    def _truncate(self, conn: Any, schema: str, table: str) -> None:
        sql = f'TRUNCATE TABLE "{schema}"."{table}"'
        with conn.cursor() as cur:
            cur.execute(sql)

    def _insert_table(self, conn: Any, table: str) -> None:
        cfg = self._config
        hint = f" {cfg.insert_hint}" if cfg.insert_hint else ""
        sql = (
            f'INSERT{hint} INTO "{cfg.target_schema}"."{table}" '
            f'SELECT * FROM "{cfg.source_schema}"."{table}"'
        )
        with conn.cursor() as cur:
            cur.execute(sql)

    def _disable_constraint(
        self, conn: Any, schema: str, table: str, constraint: str
    ) -> None:
        sql = f'ALTER TABLE "{schema}"."{table}" DISABLE CONSTRAINT "{constraint}"'
        with conn.cursor() as cur:
            cur.execute(sql)

    def _enable_constraint(
        self, conn: Any, schema: str, table: str, constraint: str
    ) -> None:
        sql = (
            f'ALTER TABLE "{schema}"."{table}" '
            f'ENABLE VALIDATE CONSTRAINT "{constraint}"'
        )
        with conn.cursor() as cur:
            cur.execute(sql)
