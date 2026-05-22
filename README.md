# OracleSchemaRefresh / oracdb

Lightweight Oracle table-copy CLI. Two surfaces:

- `schema-refresh` — same-instance refresh of one or more tables from a
  source schema to a target schema on the same DB. Daily-driver tool.
- `oracdb` — agent-facing CLI with a named-endpoint registry. Supports
  same-instance refresh **and** cross-host copy via DB link. `plan` /
  `run` / `status` / `verify` subcommands land in Cut 1c.

Thin Python orchestrator, thick Oracle server-side execution: row data
never flows through Python. All source reads in a job anchor to one
SCN (`SELECT current_scn FROM v$database` at start), so the copy is
transactionally consistent across tables.

See `docs/redesign.md` for the design contract and `CLAUDE.md` for the
working rules. Status (May 2026): **Cuts 0, 1a, 1a-followup, 1b done**.

## How it works (5 phases)

1. **INTROSPECT** — resolve FK parent tables, build dependency graph,
   topological sort (parents first). Capture one SCN. Detect server
   version (for `ALTER SEQUENCE … RESTART` feasibility).
2. **PREPARE** — create tables in target schema if missing (or drop +
   re-create with `--recreate`).
3. **DISABLE FK CONSTRAINTS** — disable FK constraints on target tables
   before truncation.
4. **TRUNCATE AND LOAD** — truncate in reverse order, then per table:
   `INSERT /*+ APPEND */ INTO target (cols) SELECT cols FROM source AS
   OF SCN :scn` (or `... FROM source@dblink AS OF SCN :scn` for
   cross-host). Compare source vs target row counts; mismatches roll
   back the per-table INSERT and mark the table failed. Reset identity
   and trigger-driven sequences via `ALTER SEQUENCE … RESTART`
   (Oracle 18c+).
5. **RE-ENABLE FK CONSTRAINTS** — re-enable with `ENABLE VALIDATE`;
   `ORA-02298` from constraint validation marks the run failed but
   doesn't abort.

## Prerequisites

1. **Target schema must exist** (DBA creates once):
   ```sql
   CREATE USER LDEBURNA IDENTIFIED BY <password>
     DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS;
   GRANT CONNECT, RESOURCE TO LDEBURNA;
   ```

2. **Minimum privileges** on the connecting user:
   - `SELECT ANY TABLE` — to read source schema data.
   - `CREATE ANY TABLE`, `DROP ANY TABLE`, `ALTER ANY TABLE` — to
     manage target tables.
   - `SELECT_CATALOG_ROLE` — for `ALL_CONSTRAINTS` / `ALL_TABLES` /
     `ALL_TRIGGERS` / `ALL_TAB_IDENTITY_COLS` access.
   - `EXECUTE ON SYS.DBMS_METADATA` — for DDL extraction.
   - For cross-host `oracdb copy --dblink session`:
     `CREATE DATABASE LINK`. For `--dblink existing:NAME`: nothing
     extra — the DBA pre-provisions the link.

3. **Python 3.10+** (no Oracle Instant Client required — uses
   `oracledb` in thin mode).

## Installation

```bash
uv venv
uv pip install -e ".[dev]"
```

## `schema-refresh` (same-instance, legacy)

### Configuration

`.env` or shell:

```bash
ORACLE_USERNAME=ldeburna
ORACLE_PASSWORD=<your-password>
ORACLE_DSN=uk01vdb007:1521/dev
```

YAML (`refresh.yaml`) — see `config.example.yaml`:

```yaml
source_schema: BACKOFFICE
target_schema: LDEBURNA
tables:
  - SUN_LEDGER
auto_include_fk_parents: true
recreate_tables: false
commit_mode: per_table   # or defer_insert_commits
insert_hint: "/*+ APPEND */"
call_timeout_seconds: 0  # 0 = no per-call timeout
```

### Usage

```bash
schema-refresh --config refresh.yaml
schema-refresh --config refresh.yaml --dry-run -v
schema-refresh --source-schema BACKOFFICE --target-schema LDEBURNA --tables SUN_LEDGER
schema-refresh --config refresh.yaml --json-output
schema-refresh --config refresh.yaml --recreate
```

## `oracdb` (named endpoints, cross-host capable)

### Register endpoints once

```bash
oracdb endpoints add dev_uk01 --dsn uk01vdb007:1521/dev --user ldeburna
# (prompts for password if --password not given)
oracdb endpoints add prod_us02 --dsn us02vdb003:1521/prod --user backoffice
oracdb endpoints list
oracdb endpoints test dev_uk01
```

Stored in `~/.oracdb/endpoints.yaml` (mode `0o600`). Override with
`$ORACDB_REGISTRY` or `--registry PATH`.

### Copy tables

```bash
# Same-instance (no dblink needed)
oracdb copy --from dev_uk01 --to dev_uk01 \
            --source-schema BACKOFFICE --target-schema LDEBURNA \
            --tables SUN_LEDGER --json

# Cross-host — DBA-provisioned link
oracdb copy --from prod_us02 --to dev_uk01 \
            --source-schema BACKOFFICE --target-schema LDEBURNA \
            --tables SUN_LEDGER \
            --dblink existing:PROD_US02_LINK --json

# Cross-host — private session-scoped link (CREATE DATABASE LINK priv)
oracdb copy --from prod_us02 --to dev_uk01 \
            --source-schema BACKOFFICE --target-schema LDEBURNA \
            --tables SUN_LEDGER \
            --dblink session --json
```

Cross-host without `--dblink` is refused with an explicit error.
Session-mode credentials are embedded in `CREATE DATABASE LINK` DDL
today — use `existing:NAME` (wallet-authenticated) in production until
Cut 3 lands wallet support.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All tables loaded, row counts matched, constraints re-enabled |
| 1 | One or more tables failed (row mismatch, ORA-02298, etc.) |

Cut 1c will widen this to `0` / `1` / `2` / `3` / `4` per the JSON
envelope spec in `docs/redesign.md` §3.

## Known limitations (current)

- **TRUNCATE is not rollback-safe.** TRUNCATE is Oracle DDL and
  auto-commits. A row-count mismatch on a table rolls back that
  table's INSERT (so we don't commit garbage), but the previous
  contents are already gone. Atomic per-table loads need the
  staging-table strategy from Cut 2.
- **Plain-sequence detection is best-effort.** `:NEW.col := seq.NEXTVAL`
  in enabled BEFORE-INSERT triggers is picked up; dynamic-SQL
  `EXECUTE IMMEDIATE 'SELECT seq.NEXTVAL …'` is not. Identity-column
  sequences (12c+) are picked up exhaustively.
- **`ALTER SEQUENCE … RESTART` is Oracle 18c+.** On older servers the
  reset step is skipped entirely (with one log line); existing data is
  loaded correctly but the sequence isn't reset.
- **Exclusive lock during `/*+ APPEND */`.** No concurrent DML on
  target tables during refresh. Lands properly with chunked staging in
  Cut 2.
- **Triggers silently disable `/*+ APPEND */`.** Oracle behaviour, not
  ours. Data still loads correctly; just slower.
- **FK constraints not created on first run.** DDL extraction uses
  `REF_CONSTRAINTS=FALSE`. The disable/enable around the data load
  applies to whatever constraints already exist.
- **Session-mode dblinks embed the source password in DDL.** Visible
  via `V$SQL` and audit. Use `--dblink existing:NAME` with a
  wallet-authenticated link in any environment that matters. Cut 3
  brings wallet-auth for `session` mode.
- **No incremental / CDC mode.** Each run is a full TRUNCATE + INSERT.
- **No within-table parallelism.** Cut 2 (`DBMS_PARALLEL_EXECUTE` +
  chunked staging) is the headline change for big tables.
