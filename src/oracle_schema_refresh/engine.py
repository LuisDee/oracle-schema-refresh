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
class Plan:
    """Output of ``RefreshEngine.plan()`` — what a run would do.

    Pure introspection: no DDL, no DML, no commits. The CLI's ``plan``
    subcommand persists this into ``oracdb$jobs``/``oracdb$tables`` and
    returns the job id; ``run JOB_ID`` re-introspects to keep the
    engine path simple (Cut 2 may cache more aggressively).
    """

    scn: int
    server_version: int
    tables_requested: list[str]
    tables_resolved: list[str]
    table_order: list[str]
    cross_host: bool

    def summary(self) -> dict[str, Any]:
        return {
            "scn": self.scn,
            "server_version": self.server_version,
            "tables_requested": self.tables_requested,
            "tables_resolved": self.tables_resolved,
            "table_order": self.table_order,
            "cross_host": self.cross_host,
        }


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
        """Construct an engine with explicit source / target endpoints.

        Distinct DSNs require ``config.dblink`` to be set (either
        ``"existing:NAME"`` or ``"session"``). Same DSNs are always fine.
        """
        if source.dsn != target.dsn and config.dblink is None:
            raise ValueError(
                "Cross-host endpoints require config.dblink to be set "
                "('session' or 'existing:NAME'). "
                f"source.dsn={source.dsn!r} target.dsn={target.dsn!r}"
            )
        instance = cls.__new__(cls)
        instance._source = source
        instance._target = target
        instance._config = config
        return instance

    @property
    def _is_cross_host(self) -> bool:
        """True iff the run uses a dblink to reach a separate source endpoint."""
        return self._config.dblink is not None

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
        from oracle_schema_refresh.endpoints.dblink import dblink_for

        t0 = time.monotonic()

        target_conn = self._target.connect()
        self._apply_call_timeout(target_conn)
        # Cross-host: open a second source connection for metadata reads
        # (SCN, column lists, source row count). The data plane still goes
        # entirely through ``target_conn`` via the dblink — no row data
        # flows through Python.
        # Same-instance: reuse the single connection (one session sees its
        # own uncommitted writes — needed for post-INSERT row counts in
        # per_table mode).
        source_conn: Any
        if self._is_cross_host:
            source_conn = self._source.connect()
            self._apply_call_timeout(source_conn)
        else:
            source_conn = target_conn

        try:
            if self._is_cross_host and not dry_run:
                with dblink_for(
                    target_conn, self._source, self._config.dblink
                ) as link_name:
                    result = self._execute(
                        source_conn, target_conn, link_name, dry_run=dry_run
                    )
            else:
                result = self._execute(
                    source_conn, target_conn, None, dry_run=dry_run
                )
        finally:
            if source_conn is not target_conn:
                source_conn.close()
            target_conn.close()

        result.total_duration_seconds = time.monotonic() - t0
        log.info(
            "refresh_complete",
            success=result.success,
            total_seconds=round(result.total_duration_seconds, 2),
            dry_run=dry_run,
            cross_host=self._is_cross_host,
        )
        return result

    def _apply_call_timeout(self, conn: Any) -> None:
        if self._config.call_timeout_seconds > 0:
            # python-oracledb expects milliseconds.
            conn.call_timeout = self._config.call_timeout_seconds * 1000

    # ------------------------------------------------------------------
    # plan() — introspect-only path used by ``oracdb plan``
    # ------------------------------------------------------------------

    def plan(self) -> Plan:
        """Run introspection only. Returns a :class:`Plan`; no DDL/DML.

        Used by ``oracdb plan`` to capture the SCN and table order
        before persisting state. Same connection model as ``run`` —
        cross-host opens source + target sessions; same-instance uses
        one. Connections are released before returning.
        """
        cfg = self._config
        target_conn = self._target.connect()
        self._apply_call_timeout(target_conn)
        source_conn: Any
        if self._is_cross_host:
            source_conn = self._source.connect()
            self._apply_call_timeout(source_conn)
        else:
            source_conn = target_conn

        try:
            scn = self._capture_scn(source_conn)
            server_version = introspect.get_server_version(target_conn)

            seed_tables = list(cfg.tables)
            if cfg.auto_include_fk_parents:
                resolved = introspect.discover_fk_parents(
                    source_conn, cfg.source_schema, seed_tables
                )
            else:
                resolved = list(seed_tables)
            graph = introspect.build_dependency_graph(
                source_conn, cfg.source_schema, resolved
            )
            table_order = introspect.topological_sort(graph)

            return Plan(
                scn=scn,
                server_version=server_version,
                tables_requested=seed_tables,
                tables_resolved=resolved,
                table_order=table_order,
                cross_host=self._is_cross_host,
            )
        finally:
            if source_conn is not target_conn:
                source_conn.close()
            target_conn.close()

    # ------------------------------------------------------------------
    # Internal orchestration
    # ------------------------------------------------------------------

    def _execute(
        self,
        source_conn: Any,
        target_conn: Any,
        dblink_name: str | None,
        dry_run: bool,
    ) -> RefreshResult:
        """Run the 5-phase refresh.

        ``source_conn`` and ``target_conn`` are the same object for an
        intra-instance run; distinct sessions for cross-host. ``dblink_name``
        is the resolved DB-link name (e.g. ``"ORACDB_AB12CD"``) when
        cross-host, ``None`` otherwise.

        Routing rules:
            * Source-schema reads (SCN, columns, FK parents, source row
              count, DDL extraction) → ``source_conn``.
            * Target-schema reads and all writes (table_exists target, FK
              constraints, DDL, TRUNCATE, INSERT, target row count, sequence
              reset, commit/rollback) → ``target_conn``.
            * INSERT … SELECT runs on ``target_conn``; its SELECT references
              the source via ``... FROM "SRC"."tbl"@dblink_name`` when
              ``dblink_name`` is set.
        """
        cfg = self._config
        overall_success = True

        # Capture one SCN at job start so every source read in this run sees a
        # transactionally consistent snapshot. Read from the source session.
        scn: int | None = None
        server_version: int | None = None
        if not dry_run:
            scn = self._capture_scn(source_conn)
            log.info("scn_captured", scn=scn)
            # Target version drives ALTER SEQUENCE RESTART feasibility.
            server_version = introspect.get_server_version(target_conn)
            log.info("server_version", major=server_version)

        # ── Phase 1: INTROSPECT ────────────────────────────────────────
        log.info("phase_start", phase=1, description="introspect")
        seed_tables = list(cfg.tables)

        if cfg.auto_include_fk_parents:
            resolved = introspect.discover_fk_parents(
                source_conn, cfg.source_schema, seed_tables
            )
        else:
            resolved = list(seed_tables)

        graph = introspect.build_dependency_graph(
            source_conn, cfg.source_schema, resolved
        )
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
            if not introspect.table_exists(source_conn, cfg.source_schema, table):
                log.warning("source_table_missing", table=table, schema=cfg.source_schema)
                continue  # handled gracefully in Phase 4

            if not introspect.table_exists(target_conn, cfg.target_schema, table):
                log.info("creating_table", table=table, target=cfg.target_schema)
                if not dry_run:
                    self._create_table(source_conn, target_conn, table)
            elif cfg.recreate_tables:
                log.info("recreating_table", table=table, target=cfg.target_schema)
                if not dry_run:
                    self._drop_table(target_conn, table)
                    self._create_table(source_conn, target_conn, table)
            else:
                log.info("table_exists_keep_structure", table=table)

        # ── Phase 3: DISABLE FK CONSTRAINTS ───────────────────────────
        log.info("phase_start", phase=3, description="disable FK constraints")
        constraints = introspect.get_fk_constraints_on_target(
            target_conn, cfg.target_schema, table_order
        )
        disabled_constraints: list[str] = []
        for c in constraints:
            if c["status"] == "ENABLED":
                log.info("disabling_constraint", name=c["name"], table=c["table"])
                if not dry_run:
                    self._disable_constraint(
                        target_conn, cfg.target_schema, c["table"], c["name"]
                    )
                disabled_constraints.append(c["name"])

        # ── Phase 4: TRUNCATE AND LOAD ─────────────────────────────────
        log.info("phase_start", phase=4, description="truncate and load")
        table_results: list[TableResult] = []
        reverse_order = list(reversed(table_order))

        # Truncate in reverse order (children first)
        for table in reverse_order:
            if not introspect.table_exists(target_conn, cfg.target_schema, table):
                continue
            log.info("truncating", table=table, target=cfg.target_schema)
            if not dry_run:
                self._truncate(target_conn, cfg.target_schema, table)

        # Insert in forward order (parents first)
        for table in table_order:
            if not introspect.table_exists(source_conn, cfg.source_schema, table):
                table_results.append(TableResult(table_name=table, status="skipped"))
                log.warning("table_skipped_missing_source", table=table)
                continue

            t_start = time.monotonic()
            try:
                if not dry_run:
                    self._insert_table(
                        source_conn, target_conn, table, scn=scn, dblink=dblink_name
                    )
                if dry_run:
                    rows_source = 0
                    rows_target = 0
                else:
                    rows_source = introspect.get_table_row_count(
                        source_conn, cfg.source_schema, table, as_of_scn=scn
                    )
                    rows_target = introspect.get_table_row_count(
                        target_conn, cfg.target_schema, table
                    )
                match = rows_source == rows_target if not dry_run else None
                status = "ok" if dry_run or match else "failed"
                if not dry_run and not match:
                    overall_success = False
                if not dry_run and cfg.commit_mode == "per_table":
                    if match:
                        target_conn.commit()
                    else:
                        log.error(
                            "per_table_rollback_due_to_mismatch",
                            table=table,
                            rows_source=rows_source,
                            rows_target=rows_target,
                        )
                        target_conn.rollback()
                sequences_reset = (
                    self._reset_identity_sequences(target_conn, table, server_version)
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
            any_failed = any(r.status == "failed" for r in table_results)
            if any_failed:
                log.error(
                    "defer_mode_rollback_due_to_mismatch",
                    failed_tables=[r.table_name for r in table_results if r.status == "failed"],
                )
                target_conn.rollback()
            else:
                target_conn.commit()

        # ── Phase 5: RE-ENABLE FK CONSTRAINTS ─────────────────────────
        log.info("phase_start", phase=5, description="re-enable FK constraints")
        reenabled: list[str] = []
        for c in constraints:
            if c["name"] in disabled_constraints:
                log.info("enabling_constraint", name=c["name"], table=c["table"])
                if not dry_run:
                    try:
                        self._enable_constraint(
                            target_conn, cfg.target_schema, c["table"], c["name"]
                        )
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

    def _reset_identity_sequences(
        self, conn: Any, table: str, server_version: int | None
    ) -> list[str]:
        """Restart sequences feeding the target table to ``MAX(col)+1``.

        Covers both Oracle-12c+ IDENTITY columns and classic sequence-backed
        columns whose sequence is referenced via ``:NEW.col := seq.NEXTVAL``
        in an enabled BEFORE-INSERT trigger.

        ``ALTER SEQUENCE ... RESTART`` is Oracle-18c+. On older servers the
        whole step is skipped with a single log line — preferable to a
        spam-of-failures and a swallowed exception per sequence.

        Failures on individual sequences are logged and swallowed —
        sequence reset is best-effort and must not abort an otherwise-
        successful run.
        """
        cfg = self._config

        if server_version is not None and server_version < 18:
            log.info(
                "sequence_reset_skipped_pre_18c",
                table=table,
                server_version=server_version,
            )
            return []

        identity = introspect.get_identity_columns(conn, cfg.target_schema, table)
        trigger_seqs = introspect.get_sequence_columns_via_triggers(
            conn, cfg.target_schema, table
        )
        # Deduplicate while preserving order: identity first, then triggers.
        seen: set[tuple[str, str]] = set()
        ordered: list[tuple[str, str]] = []
        for item in [*identity, *trigger_seqs]:
            if item not in seen:
                seen.add(item)
                ordered.append(item)

        reset: list[str] = []
        for col_name, seq_name in ordered:
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

    def _create_table(
        self, source_conn: Any, target_conn: Any, table: str
    ) -> None:
        cfg = self._config
        ddl = introspect.get_table_ddl(
            source_conn, cfg.source_schema, table, cfg.target_schema
        )
        # ORA-00955: name already used by an existing object — fine on
        # idempotent re-runs when the table is already there.
        self._safe_execute(target_conn, ddl, ignore_codes=frozenset({955}))

        # FK constraints as ALTER TABLE (emitted separately via REF_CONSTRAINTS=FALSE
        # in get_table_ddl; would need a separate GET_DDL call for 'REF_CONSTRAINT'
        # object type — skipped for v0.1 since FK DDL is optional at create time and
        # constraints are disabled/re-enabled around the data load anyway)

        for index_ddl in introspect.get_index_ddl(
            source_conn, cfg.source_schema, table, cfg.target_schema
        ):
            # ORA-00955: index name already exists.
            # ORA-01408: such column list already indexed (a different name
            # but the same column set, idempotent).
            self._safe_execute(
                target_conn, index_ddl, ignore_codes=frozenset({955, 1408})
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

    def _insert_table(
        self,
        source_conn: Any,
        target_conn: Any,
        table: str,
        scn: int | None = None,
        dblink: str | None = None,
    ) -> None:
        """Insert source rows into the target with an explicit column list.

        The column list is the intersection of source and target columns
        in target column order. Columns present on the source but not on
        the target are silently dropped; columns present on the target but
        not the source get their declared default / NULL.

        When ``scn`` is set, the SELECT uses ``AS OF SCN`` so the read is
        consistent with the rest of the job. When ``dblink`` is set, the
        SELECT references ``"SRC"."tbl"@<dblink>`` and the read crosses
        the DB link from the target session.
        """
        cfg = self._config
        source_cols = introspect.get_table_columns(
            source_conn, cfg.source_schema, table
        )
        target_cols = introspect.get_table_columns(
            target_conn, cfg.target_schema, table
        )
        source_set = set(source_cols)
        common = [c for c in target_cols if c in source_set]
        if not common:
            raise ValueError(
                f"No overlapping columns between source and target for table {table!r}"
            )
        col_list = ", ".join(f'"{c}"' for c in common)
        hint = f" {cfg.insert_hint}" if cfg.insert_hint else ""
        source_ref = f'"{cfg.source_schema}"."{table}"'
        if dblink is not None:
            source_ref = f"{source_ref}@{dblink}"
        scn_clause = " AS OF SCN :scn" if scn is not None else ""
        sql = (
            f'INSERT{hint} INTO "{cfg.target_schema}"."{table}" ({col_list}) '
            f"SELECT {col_list} FROM {source_ref}{scn_clause}"
        )
        with target_conn.cursor() as cur:
            if scn is not None:
                cur.execute(sql, scn=scn)
            else:
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
