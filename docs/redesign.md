# Redesign: cross-host parallel Oracle copy CLI

Status: **draft / proposed contract**.
Supersedes the parts of `plan.md` and `review.md` it conflicts with. Where this
doc and those disagree, this doc wins.

This is the contract subsequent cuts execute against. Implementation lands in
small, reviewable cuts (see `Cuts roadmap` at the bottom). Do not collapse
multiple cuts into one PR.

---

## 1. Goal

Turn `oracle-schema-refresh` from a same-instance, single-threaded schema-copy
tool into a **cross-host, cross-schema, parallel multi-table Oracle copy CLI
designed for agents to drive**.

Constraints:

- **The existing same-instance refresh must keep working** through every cut.
  It becomes one special case (`--from X --to X`) of the new model, not a
  deprecated path.
- **Lightweight**: thin Python orchestrator, thick Oracle server-side
  execution. No row-by-row data flow through Python. `python-oracledb` thin
  mode only.
- **Stateless CLI**: all durable job state lives in the target database.
  Killing the CLI, moving machines, or network blips must not lose a job.
- **Agent-friendly**: JSON I/O on every command, stable exit codes, planned
  side effects shown before execution, polling-friendly status.

Out of scope:

- Heterogeneous source DBs (Postgres, MySQL, etc). Oracle ↔ Oracle only.
- Anything requiring shell access to the DB host (Data Pump file mode, etc.)
  unless explicitly opted in by a later cut.
- Backups, snapshots, point-in-time recovery, replication. This is a copy
  tool, not a DR tool.

---

## 2. Architectural decisions

### 2.1 Thin orchestrator, server-side execution

Python opens a small number of control connections, submits work, polls
status. The data plane is server-side SQL:

- Intra-instance copy: `INSERT ... SELECT ... FROM source.tbl`.
- Cross-instance copy: `INSERT ... SELECT ... FROM source.tbl@dblink`.
- Within-table parallelism: `DBMS_PARALLEL_EXECUTE` driving N chunks via the
  Oracle scheduler.

Python never calls `fetchmany()` on data rows. If we're tempted to, we've
picked the wrong strategy.

### 2.2 Endpoint abstraction

`OracleConnection` is replaced by `Endpoint`. An Endpoint represents one
named, reusable target for connections.

```python
class Endpoint:
    name: str               # "dev_uk01", "prod_us02", ...
    dsn: str                # host:port/service
    auth: AuthMethod        # password | wallet | external
    default_schema: str | None
```

`RefreshEngine.__init__` (or its successor) takes `(source: Endpoint,
target: Endpoint, config)`. For today's same-instance flow, both endpoints
resolve to the same DSN — no behaviour change.

Endpoints live in `~/.oracdb/endpoints.yaml`, registered via
`oracdb endpoints add NAME --dsn ...`. The legacy `ORACLE_*` env-var path
keeps working as the implicit default endpoint named `default` so existing
users see no break.

### 2.3 Strategy interface

One copy mechanism does not fit all table sizes. A `Strategy` is responsible
for moving one table's data from source to target. The chooser picks per
table at plan time.

```python
class Strategy(Protocol):
    name: str
    def applicable(self, profile: TableProfile) -> bool: ...
    def plan(self, source: Endpoint, target: Endpoint, table: TableProfile,
             budget: ParallelBudget) -> list[WorkUnit]: ...
    def execute(self, target_conn, units: list[WorkUnit]) -> StrategyResult: ...
    def merge(self, target_conn, units: list[WorkUnit]) -> None: ...  # may be no-op
    def cleanup(self, target_conn, units: list[WorkUnit]) -> None: ...
```

Strategies in scope (in order of cuts they land in):

| Strategy             | When picked                                  | Cut |
|----------------------|----------------------------------------------|-----|
| `direct_copy`        | Small tables (<~1M rows), no chunking needed | 1   |
| `parallel_dml`       | Medium tables; single session + PARALLEL hint| 2   |
| `chunked_staging`    | Large non-partitioned; N stg + final merge   | 2   |
| `partition_exchange` | Partitioned targets; opt-in only             | 3+  |

`partition_exchange` is **never picked by `auto`** until proven on real
schemas. Opt-in via explicit `--strategy partition_exchange`.

Tables containing `LONG` columns: strategy chooser refuses with a clear
error. `LONG` cannot cross a dblink. Tables with LOBs get `parallel_dml`
with smaller chunk sizes, not `chunked_staging`.

### 2.4 Consistent read

Every job captures one SCN from the **source** at job start
(`SELECT current_scn FROM v$database`). All reads use `AS OF SCN :scn`,
including the row-count and hash verifications. This gives transactionally
consistent multi-table snapshots and makes FK re-enable reliable.

Applies intra-instance too — it's a free correctness win for the existing
same-host flow.

Constraint: source `UNDO_RETENTION` ≥ job duration. The `plan` command
reads the source's current `UNDO_RETENTION` and estimated job duration,
and warns if they don't match. Opt out with `--consistent-read none` for
short dev refreshes where it doesn't matter.

### 2.5 Server-side job state

All durable state lives in target-side tables. The CLI is stateless.

```sql
CREATE TABLE oracdb$jobs (
  job_id        VARCHAR2(32)  PRIMARY KEY,
  created_at    TIMESTAMP     DEFAULT SYSTIMESTAMP,
  source_dsn    VARCHAR2(256),
  source_schema VARCHAR2(128),
  target_schema VARCHAR2(128),
  scn           NUMBER,
  config_json   CLOB,
  status        VARCHAR2(16),  -- PLANNED | RUNNING | DONE | FAILED | CANCELLED
  error         VARCHAR2(4000)
);

CREATE TABLE oracdb$tables (
  job_id        VARCHAR2(32),
  table_name    VARCHAR2(128),
  strategy      VARCHAR2(32),
  rows_source   NUMBER,
  rows_target   NUMBER,
  hash_source   VARCHAR2(64),
  hash_target   VARCHAR2(64),
  task_name     VARCHAR2(128),  -- DBMS_PARALLEL_EXECUTE task name
  status        VARCHAR2(16),
  started_at    TIMESTAMP,
  ended_at      TIMESTAMP,
  error         VARCHAR2(4000),
  PRIMARY KEY (job_id, table_name)
);
```

Chunk-level state is *not* duplicated — `DBA_PARALLEL_EXECUTE_CHUNKS`
already has it, joined via `task_name`. Status queries `UNION` ours with
Oracle's.

Identifier note: `oracdb$jobs` (dollar-separator, no leading underscore)
to keep identifiers unquoted-legal in Oracle.

### 2.6 dblink lifecycle

Three modes, in order of preference for safety:

1. `--dblink existing:NAME` — DBA-managed, pre-created. Production default.
2. `--dblink session:USER@SRC` — private DB link, created at job start.
   **Note**: private DB links persist until `DROP DATABASE LINK`; the tool
   owns explicit teardown in `cleanup`. Requires `CREATE DATABASE LINK`.
3. `--dblink global:NAME` — persistent public DB link. Requires
   `CREATE PUBLIC DATABASE LINK`. Rare.

Authentication uses **Oracle wallet + `CONNECT_STRING`** wherever possible
to avoid passwords in DDL. The endpoint registry maps endpoint names to
wallet entries.

### 2.7 Concurrency model

Python uses one control connection to target + one read-only connection to
source for metadata. **No Python thread pool, no asyncio.** Parallelism
lives entirely in `DBMS_PARALLEL_EXECUTE` + `DBMS_SCHEDULER` on the target.

Budget surfaces:

- `--max-parallel N` — global cap on concurrent scheduler jobs (default 8).
- `--max-chunks-per-table N` — prevents one giant table starving others
  (default 4).
- `plan` reports projected peak source session count so user/agent can see
  if they'll exceed `SESSIONS_PER_USER` before running.

If `EXECUTE ON DBMS_PARALLEL_EXECUTE` or `CREATE JOB` are missing,
**detected at `plan` time, not at runtime** — the chooser downgrades all
tables to `parallel_dml` and warns.

### 2.8 Validation

Three modes, very different costs:

| Mode     | What it does                                           | Cost                         |
|----------|--------------------------------------------------------|------------------------------|
| `rows`   | `SELECT COUNT(*)` source vs target (default)           | Cheap                        |
| `hash`   | `SUM(ORA_HASH(col1||col2||...))` source vs target      | Full scan over dblink — slow |
| `sample` | Hash over every Nth row                                | Light                        |

Both sides read `AS OF SCN :scn` so counts/hashes are reproducible after
the fact. Mismatches mark the table `FAILED` regardless of whether the
load itself raised.

### 2.9 Sequences

`ALL_SEQUENCES` introspected at restore time. For each sequence referenced
as an identity default in a copied table:

```sql
ALTER SEQUENCE seq RESTART START WITH (SELECT NVL(MAX(id),0)+1 FROM tbl);
```

Resolves the known limitation in current `README.md`.

---

## 3. CLI contract

Two surfaces coexist:

- `schema-refresh` — existing entry point, unchanged signature. Wraps the
  new engine in same-instance mode. Stays for at least Cuts 0–3.
- `oracdb` — new entry point. Phased commands for power use, plus one
  `copy` convenience.

### 3.1 New `oracdb` surface

```text
oracdb endpoints add NAME --dsn DSN [--wallet PATH | --user USER]
oracdb endpoints list   [--json]
oracdb endpoints test   NAME

oracdb plan --from SRC --to DST
            [--tables T1,T2 | --tables-file F | --schema S]
            [--include-fk-parents]            # default true
            [--strategy auto|direct|parallel|staging|exchange]
            [--max-parallel N]
            [--consistent-read scn|none]      # default scn
            [--scn N]                         # override SCN, default = now()
            --json

oracdb run    --plan FILE | --from SRC --to DST --tables ...
              [--background] [--resume JOB_ID]
              --json

oracdb status JOB_ID [--watch] [--json]
oracdb logs   JOB_ID [--follow] [--json]
oracdb verify JOB_ID [--mode rows|hash|sample] [--json]
oracdb cancel JOB_ID
oracdb cleanup JOB_ID                         # drops staging tables, control rows
oracdb wait   JOB_ID [--timeout T]            # server-side long poll

oracdb copy   --from SRC --to DST --tables ...  # plan → run → wait → verify
              [--wait] [--json]
```

`copy` is the one-shot agent path. `plan`/`run`/`status`/`verify` are the
phased power-user path.

### 3.2 JSON envelope (every command with `--json`)

```json
{
  "ok": true,
  "command": "plan",
  "job_id": "j_2026_05_21_b7c3",
  "data": { /* command-specific */ },
  "error": null,
  "error_category": null
}
```

`error_category` is one of:

- `CONFIG`   — bad user input
- `AUTH`     — credentials / privileges
- `TRANSIENT`— network, timeout, lock wait — retry might work
- `DATA`     — FK violation, type mismatch, schema drift
- `INTERNAL` — bug in this tool

### 3.3 Exit codes

| Code | Meaning            |
|------|--------------------|
| 0    | success            |
| 1    | user / config error|
| 2    | transient          |
| 3    | data error         |
| 4    | internal bug       |

---

## 4. Module layout (target shape)

```text
src/oracle_schema_refresh/
  endpoints/         # named connections, wallet, credential resolution
    __init__.py
    registry.py      # endpoints.yaml load/save
    auth.py          # password | wallet | external
    dblink.py        # dblink lifecycle (existing | session | global)
  introspect/        # source metadata: deps, types, sizes, hazards
    __init__.py
    fk.py            # discover_fk_parents, dependency graph, topo sort
    ddl.py           # DBMS_METADATA extraction
    profile.py       # TableProfile: rows, size, partitioned?, LOB?, LONG?
  strategy/          # picker + per-strategy implementations
    __init__.py
    picker.py        # auto selection logic
    direct_copy.py
    parallel_dml.py
    chunked_staging.py
    partition_exchange.py
  executor/          # DBMS_PARALLEL_EXECUTE wrapper, task submission, polling
    __init__.py
    dpe.py           # wrapper around DBMS_PARALLEL_EXECUTE
    scheduler.py     # poll DBA_SCHEDULER_JOBS for completion
  state/             # control-table schema + accessors
    __init__.py
    schema.py        # CREATE TABLE oracdb$... DDL, migrations
    job.py           # Job model + read/write
    table_record.py  # per-table state row
  verify/            # row count + ORA_HASH compare
    __init__.py
    rows.py
    hash.py
  job.py             # top-level orchestration: phases over many tables
  cli/               # click subcommands, JSON I/O
    __init__.py
    schema_refresh.py  # legacy entry point
    oracdb.py          # new entry point + subcommands
  config.py          # RefreshConfig and friends — kept compatible
```

Today's `engine.py` becomes:

- `strategy/direct_copy.py` (the actual data movement)
- `job.py` (the 5-phase orchestration, now over many tables / endpoints)
- the FK/topo/DDL helpers fan out into `introspect/`

Today's `introspect.py` splits across `introspect/fk.py`, `introspect/ddl.py`,
`introspect/profile.py`.

---

## 5. Cuts roadmap

Each cut is a single PR. Each lands green tests. No cut breaks the legacy
`schema-refresh` CLI.

### Cut 0 — `Endpoint` abstraction (no behaviour change)

- Introduce `Endpoint` dataclass and `endpoints/auth.py`.
- `RefreshEngine.__init__(source: Endpoint, target: Endpoint, config)` —
  for the legacy path, both endpoints resolve to the same DSN built from
  `OracleConnection`.
- Existing tests pass unchanged.
- `schema-refresh` CLI unchanged.

**Exit criterion**: `pytest` passes, no diff in observable CLI behaviour.

### Cut 1a — correctness fixes (no new architecture)

These fix bugs the redesign discussion identified, but don't need any of
the new machinery:

- `AS OF SCN :scn` reads in `_insert_table` (intra-instance — no dblink yet).
- Explicit column list from `ALL_TAB_COLUMNS` instead of `SELECT *`.
- Rename or remove `commit_mode="all_or_nothing"` (it's misleading because
  TRUNCATE auto-commits — pick honest semantics).
- Add `call_timeout` on the connection.
- Source-vs-target row-count validation in `TableResult`
  (`rows_source`, `rows_target`, `match`).
- Tighten `_safe_execute` ignore sets — pass per-call rather than defaulting
  to the union of all idempotent codes.
- `ALTER SEQUENCE` restart for identity columns.

**Exit criterion**: all the above visible in `RefreshResult.summary()`,
tests updated, behaviour gated so existing callers still get the old
shape unless they ask for the new fields.

### Cut 1b — cross-host `direct_copy` strategy

- `endpoints/dblink.py` — `existing` and `session` modes.
- `strategy/direct_copy.py` with `INSERT ... SELECT * FROM tbl@link
  AS OF SCN :scn`.
- New `oracdb` CLI surface with `endpoints add/list/test` + `copy`.
- `schema-refresh` still works unchanged.

**Exit criterion**: cross-host single-statement copy of a small table works
end-to-end against testcontainers-oracle. SCN consistency verified by a
race test.

### Cut 1c — server-side state + phased CLI

- `state/` module: `oracdb$jobs`, `oracdb$tables` tables, accessors.
- `oracdb plan` / `run` / `status` / `verify` / `cancel` / `cleanup` /
  `wait` subcommands.
- JSON envelope + exit codes wired everywhere.
- `--resume JOB_ID` works for failed/cancelled jobs in the `direct_copy`
  case.

**Exit criterion**: agent flow `plan → run --background → status --watch
→ verify` works end-to-end. Job state survives killing the CLI mid-run.

### Cut 2 — within-table parallelism

- `executor/dpe.py` — `DBMS_PARALLEL_EXECUTE` wrapper.
- `strategy/parallel_dml.py` — single-session `PARALLEL` hint.
- `strategy/chunked_staging.py` — per-chunk staging tables + final merge.
- Strategy chooser (`auto`).
- Privilege detection at `plan` time with strategy downgrade.

**Exit criterion**: 100M-row table copy in a CI integration test
demonstrates measurable speedup with `--max-parallel 4`.

### Cut 3 — opt-in advanced strategies + ergonomics

- `strategy/partition_exchange.py` — opt-in only via explicit flag.
- LOB-aware tuning.
- Wallet-only auth path proven end-to-end.
- `oracdb logs JOB_ID` streaming.

### Cut 4 — (optional) Data Pump file-mode

Only if real users have multi-TB tables where SQL-only loses. Big
operational complexity bump (needs shell access to DB host or
`DATA_PUMP_DIR` privileges). Defer until justified by usage.

---

## 6. Testing strategy

Tests split cleanly between:

- **Unit tests** — mock `oracledb` like today's tests do. Cover orchestration
  logic, planner, strategy picker, state machine transitions. Fast, no
  Oracle required. Live in `tests/unit/`.
- **Integration tests** — real Oracle via `testcontainers-oracle`
  (`gvenzl/oracle-free:23-slim`). Cover DBMS_PARALLEL_EXECUTE wiring,
  dblink behaviour, SCN consistency, FK re-enable. Slow, gated behind
  a `pytest -m integration` marker. Live in `tests/integration/`.

The contract that integration tests verify but unit tests can't:

1. `AS OF SCN :scn` produces transactionally consistent reads across
   concurrent writes.
2. `DBMS_PARALLEL_EXECUTE` task names are unique per `(job_id, table)` and
   don't collide across concurrent jobs.
3. `INSERT /*+ APPEND */` does take an exclusive lock — proven by failing
   to run two concurrently into the same target.
4. Private dblinks created by `session` mode are dropped by `cleanup`.

CI runs unit tests on every PR, integration tests on a nightly schedule
plus on-demand via a manual workflow trigger.

---

## 7. Open questions

To resolve before the corresponding cut starts:

1. **Cut 1a**: what's the honest name for `commit_mode`? Candidates:
   `defer_insert_commits` / `commit_per_table` / drop the option and
   always commit per table.
2. **Cut 1b**: do we need to support source endpoints that have **no**
   `CREATE DATABASE LINK` available at all (i.e. fall back to Python row
   shipping)? Default position: **no** — refuse with a clear error.
3. **Cut 1c**: `job_id` format. Suggested `j_<YYYYMMDD>_<HHMMSS>_<rand4>`
   so it sorts chronologically and is human-readable in `oracdb$jobs`.
4. **Cut 2**: how do we estimate chunk count for `chunked_staging`?
   By table size in MB (target ~256MB/chunk) or by row count (target
   ~10M rows/chunk)? Pick one and document.
5. **Cross-cut**: should `oracdb` eventually replace `schema-refresh`, or
   coexist permanently? Default position: **coexist permanently** —
   `schema-refresh` is a stable, simple interface for the same-instance
   case.

---

## 8. Non-goals (explicit)

So they don't creep in by accident:

- Schema diffing / DDL synchronisation. Use a real tool (Liquibase, etc).
- Bidirectional replication. One-shot copy only.
- Incremental copy (CDC, change tracking). Always a full refresh per job.
- Encrypted-at-rest credential management beyond Oracle wallet.
- A web UI, a daemon, or a long-running service. CLI only.
- Cross-DB-vendor copies.
