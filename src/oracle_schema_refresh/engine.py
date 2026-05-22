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

# ORA-02298: cannot validate — parent keys not found
_ORA_FK_VALIDATE_FAIL = 2298


@dataclass
class TableResult:
    """Result for a single table refresh.

    ``rows_loaded`` is the row count on the target after insert.
    ``rows_source`` is the row count on the source ``AS OF SCN`` — i.e.
    the snapshot the load read from. ``match`` is ``True`` iff they're
    equal; mismatches force ``status='failed'`` even if no exception was
    raised by Oracle.
    """

    table_name: str
    status: str  # "ok" | "skipped" | "failed"
    rows_loaded: int = 0
    rows_source: int = 0
    match: bool | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    sequences_reset: list[str] = field(default_factory=list)


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
    scn: int | None = None

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary dict."""
        return {
            "success": self.success,
            "tables_requested": self.tables_requested,
            "table_order": self.table_order,
            "scn": self.scn,
            "results": [
                {
                    "table": r.table_name,
                    "status": r.status,
                    "rows": r.rows_loaded,
                    "rows_source": r.rows_source,
                    "match": r.match,
                    "sequences_reset": r.sequences_reset,
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
        if self._config.call_timeout_seconds > 0:
            # python-oracledb expects milliseconds.
            conn.call_timeout = self._config.call_timeout_seconds * 1000
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

        # Capture one SCN at job start so every source read in this run sees a
        # transactionally consistent snapshot. Cheap intra-instance; in Cut 1b
        # it comes from the source endpoint.
        scn: int | None = None
        if not dry_run:
            scn = self._capture_scn(conn)
            log.info("scn_captured", scn=scn)

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
                    self._insert_table(conn, table, scn=scn)
                    if cfg.commit_mode == "per_table":
                        conn.commit()
                if dry_run:
                    rows_source = 0
                    rows_target = 0
                else:
                    rows_source = introspect.get_table_row_count(
                        conn, cfg.source_schema, table, as_of_scn=scn
                    )
                    rows_target = introspect.get_table_row_count(
                        conn, cfg.target_schema, table
                    )
                match = rows_source == rows_target if not dry_run else None
                status = "ok" if dry_run or match else "failed"
                if not dry_run and not match:
                    overall_success = False
                sequences_reset = (
                    self._reset_identity_sequences(conn, table)
                    if not dry_run and status == "ok"
                    else []
                )
                dur = time.monotonic() - t_start
                log.info(
                    "table_loaded",
                    table=table,
                    rows_source=rows_source,
                    rows_target=rows_target,
                    match=match,
                    duration=round(dur, 2),
                )
                table_results.append(
                    TableResult(
                        table_name=table,
                        status=status,
                        rows_loaded=rows_target,
                        rows_source=rows_source,
                        match=match,
                        duration_seconds=dur,
                        sequences_reset=sequences_reset,
                        error=(
                            None
                            if status == "ok"
                            else f"row count mismatch: source={rows_source} target={rows_target}"
                        ),
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

        if not dry_run and cfg.commit_mode == "defer_insert_commits":
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
            scn=scn,
        )

    # ------------------------------------------------------------------
    # SCN capture & identity-sequence reset
    # ------------------------------------------------------------------

    def _capture_scn(self, conn: Any) -> int:
        """Read the source's current SCN; used as the Flashback Query anchor."""
        with conn.cursor() as cur:
            cur.execute("SELECT current_scn FROM v$database")
            row = cur.fetchone()
            return int(row[0])

    def _reset_identity_sequences(self, conn: Any, table: str) -> list[str]:
        """Restart identity-column sequences on the target so future inserts
        don't collide with the loaded data.

        Requires Oracle 18c+ (``ALTER SEQUENCE ... RESTART``). Failures are
        logged and swallowed — sequence reset is best-effort and must not
        abort an otherwise-successful run.
        """
        cfg = self._config
        reset: list[str] = []
        identity_cols = introspect.get_identity_columns(conn, cfg.target_schema, table)
        for col_name, seq_name in identity_cols:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT NVL(MAX("{col_name}"), 0) + 1 '  # noqa: S608
                        f'FROM "{cfg.target_schema}"."{table}"'
                    )
                    start_with = int(cur.fetchone()[0])
                    cur.execute(
                        f'ALTER SEQUENCE "{cfg.target_schema}"."{seq_name}" '
                        f"RESTART START WITH {start_with}"
                    )
                reset.append(seq_name)
                log.info(
                    "sequence_reset",
                    table=table,
                    column=col_name,
                    sequence=seq_name,
                    start_with=start_with,
                )
            except oracledb.DatabaseError as exc:
                log.warning(
                    "sequence_reset_failed",
                    table=table,
                    sequence=seq_name,
                    error=str(exc),
                )
        return reset

    # ------------------------------------------------------------------
    # DDL / DML helpers
    # ------------------------------------------------------------------

    def _safe_execute(
        self,
        conn: Any,
        sql: str,
        ignore_codes: frozenset[int] = frozenset(),
    ) -> None:
        """Execute SQL, swallowing only the ORA codes the caller opted into.

        Default is the empty set — silent swallowing masks real bugs.
        Callers pass the precise codes they tolerate (e.g. ``{955}`` for
        "table already exists" on CREATE TABLE).
        """
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
        # ORA-00955: name already used by an existing object — fine on
        # idempotent re-runs when the table is already there.
        self._safe_execute(conn, ddl, ignore_codes=frozenset({955}))

        # FK constraints as ALTER TABLE (emitted separately via REF_CONSTRAINTS=FALSE
        # in get_table_ddl; would need a separate GET_DDL call for 'REF_CONSTRAINT'
        # object type — skipped for v0.1 since FK DDL is optional at create time and
        # constraints are disabled/re-enabled around the data load anyway)

        for index_ddl in introspect.get_index_ddl(
            conn, cfg.source_schema, table, cfg.target_schema
        ):
            # ORA-00955: index name already exists.
            # ORA-01408: such column list already indexed (a different name
            # but the same column set, idempotent).
            self._safe_execute(
                conn, index_ddl, ignore_codes=frozenset({955, 1408})
            )

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

    def _insert_table(self, conn: Any, table: str, scn: int | None = None) -> None:
        """Insert source rows into the target with an explicit column list.

        The column list is the intersection of source and target columns
        (in target column order). Columns present on the source but not on
        the target are silently dropped; columns present on the target but
        not the source get their declared default / NULL. This is how we
        survive a schema-drift on the source.

        When ``scn`` is set, the SELECT uses ``AS OF SCN`` so the read is
        consistent with the rest of the job.
        """
        cfg = self._config
        source_cols = introspect.get_table_columns(conn, cfg.source_schema, table)
        target_cols = introspect.get_table_columns(conn, cfg.target_schema, table)
        source_set = set(source_cols)
        # Preserve target column order so the projection lines up with the
        # INSERT column list.
        common = [c for c in target_cols if c in source_set]
        if not common:
            raise ValueError(
                f"No overlapping columns between source and target for table {table!r}"
            )
        col_list = ", ".join(f'"{c}"' for c in common)
        hint = f" {cfg.insert_hint}" if cfg.insert_hint else ""
        if scn is None:
            sql = (
                f'INSERT{hint} INTO "{cfg.target_schema}"."{table}" ({col_list}) '
                f'SELECT {col_list} FROM "{cfg.source_schema}"."{table}"'
            )
            with conn.cursor() as cur:
                cur.execute(sql)
        else:
            sql = (
                f'INSERT{hint} INTO "{cfg.target_schema}"."{table}" ({col_list}) '
                f'SELECT {col_list} FROM "{cfg.source_schema}"."{table}" '
                f"AS OF SCN :scn"
            )
            with conn.cursor() as cur:
                cur.execute(sql, scn=scn)

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
