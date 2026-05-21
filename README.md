# OracleSchemaRefresh

Idempotent CLI tool that refreshes a configurable set of Oracle tables from a source schema (e.g. `BACKOFFICE`) to a personal dev schema (e.g. `LDEBURNA`) on the **same Oracle 19c SE2 instance**. Designed to run daily or on-demand so developers work against their own data without affecting the shared schema.

## How it works (5 phases)

1. **INTROSPECT** — resolve FK parent tables, build dependency graph, topological sort (parents first)
2. **PREPARE** — create tables in target schema if missing (or drop + re-create with `--recreate`)
3. **DISABLE FK CONSTRAINTS** — disable FK constraints on target tables before truncation
4. **TRUNCATE AND LOAD** — truncate in reverse order, `INSERT /*+ APPEND */` in forward order
5. **RE-ENABLE FK CONSTRAINTS** — re-enable and validate constraints

Entire data transfer happens in Oracle's SGA (no dump files). Direct-path INSERT is ~3x faster than conventional INSERT for bulk loads.

## Prerequisites

1. **Target schema must exist** (DBA creates once):
   ```sql
   CREATE USER LDEBURNA IDENTIFIED BY <password>
     DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS;
   GRANT CONNECT, RESOURCE TO LDEBURNA;
   ```

2. **Minimum privileges** on the connecting user:
   - `SELECT ANY TABLE` — to read source schema data
   - `CREATE ANY TABLE`, `DROP ANY TABLE`, `ALTER ANY TABLE` — to manage target tables
   - `SELECT_CATALOG_ROLE` — for `ALL_CONSTRAINTS` / `ALL_TABLES` access
   - `EXECUTE ON SYS.DBMS_METADATA` — for DDL extraction

3. **Python 3.10+** (no Oracle Instant Client required — uses `oracledb` thin mode)

## Installation

```bash
cd /home/coder/scripts/tooling/OracleSchemaRefresh
uv venv
uv pip install -e ".[dev]"
```

## Configuration

### Environment variables (`.env` or shell)

```bash
ORACLE_USERNAME=ldeburna
ORACLE_PASSWORD=<your-password>
ORACLE_DSN=uk01vdb007.uk.makoglobal.com:1521/dev
```

The `ORACLE_*` variables in `VAT-PESM/.env` use the same DSN format and are directly compatible.

### YAML config file (recommended)

```yaml
# refresh.yaml
source_schema: BACKOFFICE
target_schema: LDEBURNA
tables:
  - SUN_LEDGER
auto_include_fk_parents: true   # auto-discover FK parents (default: true)
recreate_tables: false           # TRUNCATE+INSERT (default) vs DROP+CREATE
commit_mode: per_table           # per_table (default) or all_or_nothing
insert_hint: "/*+ APPEND */"     # set to "" for conventional insert
```

See `config.example.yaml` for full documentation.

## Usage

```bash
# Run from YAML config
schema-refresh --config refresh.yaml

# Dry run (logs actions, executes nothing)
schema-refresh --config refresh.yaml --dry-run -v

# Inline — no config file needed
schema-refresh --source-schema BACKOFFICE --target-schema LDEBURNA --tables SUN_LEDGER

# JSON output (for scripts/CI)
schema-refresh --config refresh.yaml --json-output

# Drop and re-create table structure, then reload
schema-refresh --config refresh.yaml --recreate
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All tables loaded and constraints re-enabled successfully |
| 1 | One or more tables failed, or a constraint could not be re-validated |

## Known limitations

- **TRUNCATE is not rollback-safe.** TRUNCATE is Oracle DDL and auto-commits. In `all_or_nothing` mode only the INSERT commits are deferred — the truncation cannot be undone.
- **No sequence reset.** Identity column sequences are not copied from source. Values in the target schema will restart from where they were before the truncation.
- **No incremental refresh.** Each run is a full TRUNCATE + INSERT. SCN/timestamp-based delta sync is a future extension.
- **Exclusive lock during load.** `/*+ APPEND */` takes an exclusive table lock. No concurrent DML on target tables during refresh.
- **Tables with triggers.** If a target table has triggers, Oracle silently ignores the `/*+ APPEND */` hint and falls back to conventional INSERT. This is correct behaviour — performance is slightly lower but data is loaded correctly.
- **FK constraints not created in target schema.** DDL extraction uses `REF_CONSTRAINTS=FALSE` — FK constraints are not emitted during `CREATE TABLE`. A fresh target schema has tables with no FK constraint enforcement. This is acceptable for dev testing but means the target schema is structurally different from the source.

## VAT-PESM integration

After running a refresh, set `VAT_ORACLE_USER=ldeburna` (already in `VAT-PESM/.env`) and run the Oracle test suite:

```bash
cd /path/to/VAT-PESM/backend
python -m pytest tests/test_data_provider.py -m oracle -v
```
