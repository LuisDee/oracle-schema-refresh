# OracleSchemaRefresh — Code Review

**Reviewer**: post-implementation audit
**Date**: 2026-04-02
**Changeset**: new tool, ~450 lines of source, ~370 lines of tests

---

## Scope Assessment

A new standalone Python CLI tool was built that idempotently refreshes a configurable set of Oracle tables from a source schema to a personal dev schema on the same Oracle 19c SE2 instance. Risk profile is **low**: this is a developer convenience tool operating entirely in a dev environment, not user-facing, no production schema writes, no shared state beyond the Oracle dev DB. Changeset is ~820 lines total across 8 files — full depth review is appropriate.

---

## Phase 0: Preparation

### Build results

```
pytest tests/     40 passed, 0 failed
```

### Coverage

```
oracle_schema_refresh/cli.py        79%   (miss: 39,41,43,45,47, 140-146, 152-154, 163-164, 167)
oracle_schema_refresh/engine.py     85%   (miss: 124, 150-153, 207-211, 221, 233-246, 274, 289, 292-296)
oracle_schema_refresh/introspect.py 98%   (miss: 258, 284)
oracle_schema_refresh/config.py    100%
TOTAL                               89%
```

### Linter

```
ruff check src/ tests/
34 errors:
  - ANN401 × 15  (Any used for conn/cur parameters — pervasive)
  - F401  × 12   (unused imports in tests: call, patch, RefreshEngine imported but unused)
  - E501  × 2    (lines too long: engine.py:196, test_engine.py:361)
  - I001  × 1    (import sort in test_introspect.py)
  - ANN201 × 4   (missing return type on test helpers)
```

None are blocking. All fixable. The ANN401 warnings are arguably false positives — `oracledb.Connection` is the correct type but the thin-mode public API doesn't export a clean `Connection` type for annotations.

---

## Phase 1: Implementation History

No git history available on this branch yet. Implementation followed the plan in `plan.md` stage-by-stage. One deviation noted in the implementation:

| Deviation | Planned | Actual | Justified |
|-----------|---------|--------|-----------|
| FK constraint DDL on fresh schema | Emit FK ALTER TABLE during CREATE TABLE | Skipped (comment left in `_create_table`) | Yes — FKs disabled/re-enabled around data load anyway; avoids chicken-and-egg ordering |
| CLI framework | `argparse` | `click` | Yes — matches existing tooling patterns |

---

## Phase 2: Intent Alignment

**Original intent**: Developer can run `schema-refresh` to mirror selected Oracle tables into their personal schema, so they can run Oracle-path tests without touching the shared BACKOFFICE schema.

**What was built**: A fully functional 5-phase CLI tool matching the spec. Dry-run mode, FK graph resolution, DBMS_METADATA DDL extraction, direct-path INSERT, idempotent error handling, structured logging, JSON output.

**Gap**: FK constraints are not created in the target schema on first run (see Phase 3 finding below). This is documented but is a material difference between the source and target schemas.

### Plan fidelity

| Stage | Goal | Met | Evidence |
|-------|------|-----|---------|
| 1 (scaffold) | Package installs, 40 tests RED | Yes | `uv pip install -e ".[dev]"` succeeds, 40 failures confirmed |
| 2 (config) | Pydantic models, 6 tests GREEN | Yes | 6/6 pass |
| 3 (introspect) | FK graph, topo sort, DDL, 19 tests GREEN | Yes | 19/19 pass |
| 4 (engine) | 5-phase orchestrator, 9 tests GREEN | Yes | 9/9 pass |
| 5 (cli + README) | CLI, README, 6 tests GREEN | Yes | 6/6 pass |

### Verification checklist

- [x] Tests match existing style (pytest, unittest.mock) — `ruff` reports unused imports, not style violations
- [x] All 40 tests pass — `pytest tests/` confirmed
- [ ] Linter passes cleanly — **34 ruff violations** (see above)
- [ ] Type checker: mypy not configured, not run
- [x] No regressions to existing tooling
- [x] Error handling covers failure paths (ORA codes, missing tables, connection failure)
- [x] Logging at each phase transition
- [x] README documents prerequisites, limitations, and usage

---

## Phase 3: Technical Audit

### 3.1 Code Quality

**Rating: Acceptable**

Code is readable, well-named, and follows existing tooling patterns (hatchling, click, pydantic-settings). No dead code, no commented-out blocks, no TODO markers.

**Violations**:
- ANN401 on `conn: Any` and `cur: Any` throughout `introspect.py` and `engine.py`. The correct type is `oracledb.Connection` / `oracledb.Cursor`, but thin-mode doesn't export these cleanly in the public API. The `Any` usage is intentional and the comments explain why. Should add a module-level `# type: ignore` comment or define a local type alias `OracleConn = Any  # oracledb.Connection`.
- Unused imports in tests: `call` and `patch` imported in `test_introspect.py` and `test_engine.py` but never used. `RefreshEngine` imported inside each test function body but each function uses `_make_engine()` which already constructs it — the inner imports are dead code.
- `pyproject.toml` references removed ruff rules (`ANN101`, `ANN102`) generating a warning on every lint run.

**Duplication**: `_configure_ddl_transforms` is called once from `get_table_ddl` and once from `get_index_ddl`. The logic is identical (5 SET_TRANSFORM_PARAM calls). The extraction into a shared helper is correct. No duplication.

The SQL for FK parent discovery is identical in `discover_fk_parents` and `build_dependency_graph`. This is the one piece of SQL duplication — both query `all_constraints` with the same JOIN. A `_query_fk_parents(cur, schema, table)` private helper would eliminate it.

### 3.2 Correctness

**Rating: Acceptable — one structural issue worth understanding**

**Finding 1 — Phase 4 truncate guard checks source, not target (engine.py:177)**

```python
# Truncate in reverse order (children first)
for table in reverse_order:
    if not introspect.table_exists(conn, cfg.source_schema, table):  # checks SOURCE
        continue
    self._truncate(conn, cfg.target_schema, table)  # truncates TARGET
```

The guard checks whether the table exists in the SOURCE schema, but the operation is on the TARGET. The implicit reasoning is: if source doesn't exist, Phase 2 won't have created the target, so there's nothing to truncate. This is correct under normal execution (single run, no concurrent modifications), but it's checking the wrong thing semantically. If the source table was dropped between Phase 2 and Phase 4, this guard would correctly skip — but it's skipping because "source gone" not "target gone". On a second run where source exists but the target was manually dropped, this guard passes and `_truncate` would hit a missing table.

This is low-risk in practice (dev tool, single user, sequential phases) but semantically wrong. The guard should check `cfg.target_schema`, not `cfg.source_schema`.

**Finding 2 — FK constraints not present in fresh target schema**

`_create_table` (engine.py:276-289) calls `get_table_ddl` with `REF_CONSTRAINTS=FALSE` and has a comment explaining FK constraint DDL is skipped. This means a fresh target schema will have tables with no FK constraints. Phases 3 and 5 (disable/re-enable) query `all_constraints` on the target and find nothing — no error, but also no constraint enforcement. On the second run with `recreate_tables=False`, the existing tables from the first run (still no FK constraints) are kept. FK constraints in the target schema only appear if the user creates them manually or if `--recreate` recreates from source DDL — but `_create_table` also skips them.

In practice, for a read-only dev testing scenario this is acceptable. But it means the target schema is structurally different from the source, and FK violations in test data won't be caught. This is documented in the README as a known limitation ("FK constraints not created on fresh schema") — actually it is NOT currently documented. It should be.

**Finding 3 — `exc.args[0].code` access is unguarded (engine.py:270, 235)**

```python
code = exc.args[0].code if exc.args else None
```

This guards for empty `args` but not for the case where `exc.args[0]` is a string (non-oracledb internal error) or doesn't have `.code`. In pure oracledb errors this never happens, but it would raise `AttributeError` if a non-standard exception leaks into the `oracledb.DatabaseError` handler. Should be:

```python
code = getattr(exc.args[0], "code", None) if exc.args else None
```

**Finding 4 — Topo sort: parents not in graph are silently ignored**

```python
in_degree: dict[str, int] = {n: 0 for n in graph}
for node, parents in graph.items():
    for parent in parents:
        if parent in in_degree:    # ← skips parents not in graph
            in_degree[node] += 1
```

If `graph["A"] = ["C"]` but `"C"` is not in the graph, `in_degree["A"]` stays at 0 and A is processed as if it has no dependencies. `build_dependency_graph` only ever adds parents that are in the table set, so this can't happen in normal operation. But the function's contract says `graph[node] = list of parent nodes` with no stated precondition that all parents are graph keys. A cycle-detection cycle could also be broken if a parent is missing. This is a correctness latency, not a current bug.

**Correctness summary**: No data-corrupting bugs. Finding 1 is semantically wrong but harmless in practice. Finding 2 is the most user-impacting — the target schema won't have FK constraints. Findings 3 and 4 are defensive hardening gaps.

### 3.3 Test Coverage

**Rating: Acceptable**

89% overall. 40 tests. Happy paths and major error paths are covered. Key missing cases:

| Missing test | File | Lines | Risk |
|-------------|------|-------|------|
| `recreate_tables=True` (drop + re-create) | engine.py | 150-153 | Low — well-contained path |
| `all_or_nothing` commit mode | engine.py | 221 | Medium — different commit semantics not verified |
| Table load failure (Phase 4 except block) | engine.py | 207-211 | Medium — overall_success=False and TableResult("failed") not tested |
| ORA-02298 on constraint re-enable | engine.py | 233-244 | Medium — the most likely failure path in production |
| `_drop_table` | engine.py | 292-296 | Low |
| YAML override flags (source_schema, target_schema, tables from CLI) | cli.py | 39-47 | Low |
| Missing credentials path | cli.py | 140-146 | Low |

The ORA-02298 path is the highest-risk gap — it's exactly what would happen if a parent table row was deleted from source but child rows still exist, a realistic scenario. The code handles it correctly but no test verifies the handling.

Assertion quality is good. Tests check specific behaviour (INSERT called, commit count, constraint names in results) not just "it doesn't crash".

### 3.4 Error Handling

**Rating: Acceptable — one broad catch**

**Finding 5 — Broad `except Exception` in Phase 4 insert (engine.py:207)**

```python
try:
    if not dry_run:
        self._insert_table(conn, table)
        ...
except Exception as exc:
    ...
    overall_success = False
    table_results.append(TableResult(..., status="failed", error=str(exc)))
```

This catches `oracledb.DatabaseError`, `oracledb.InterfaceError`, but also `AttributeError`, `KeyError`, programming errors, and (in principle) anything else. A programming error in `_insert_table` would be silently swallowed as a "failed" table status rather than propagating as a bug. Should narrow to `(oracledb.DatabaseError, oracledb.InterfaceError)` at minimum.

**ORA-31608 handling in `get_index_ddl`**: correct. The try/except wraps the execute, checks the code, returns [] for no-indexes. ✓

**Connection closure in `run()`**: the `finally: conn.close()` guarantees the connection is closed even if `_execute` raises. ✓

**`topological_sort` raises `ValueError` on cycle**: propagates through `_execute` → `run()` → `cli()` where `except Exception` catches it and prints a useful error. ✓

**Timeout**: no connection timeout is configured on `oracledb.connect()`. If the Oracle host is unreachable, the connect call will hang indefinitely (or until the OS TCP timeout, which is minutes). Should add `tcp_connect_timeout=10`.

### 3.5 Security

**Rating: Acceptable — one credential exposure risk**

**Finding 6 — `OracleConnection.password` is plain `str`, not `SecretStr` (config.py:20)**

```python
class OracleConnection(BaseSettings):
    username: str
    password: str   # ← exposed in repr, serialization
```

Pydantic's `repr()` for a `BaseSettings` instance will include the password in plain text. `str(oracle_conn)` or any logging of the settings object would expose it. Should be `SecretStr` from `pydantic`:

```python
from pydantic import SecretStr
password: SecretStr
```

Then access with `oracle_conn.password.get_secret_value()` at connect time. The CLI error message at `cli.py:139-146` currently doesn't print credentials, so no immediate leak — but this is a latent risk if anyone adds debug logging.

**SQL construction**: All SQL that uses external values (schema names, table names, constraint names) uses quoted identifiers (`"SCHEMA"."TABLE"`) or bind parameters. The `get_fk_constraints_on_target` query uses positional bind parameters for the table names. ✓ No injection vectors.

**`yaml.safe_load`** used in CLI — correct. ✓

**`insert_hint`** is inserted directly into SQL without validation. A crafted YAML could set `insert_hint: "; DROP TABLE"` — but `oracledb.execute` does not support multiple statements in a single call (raises `DatabaseError: ORA-00933: SQL command not properly ended`). Not exploitable. ✓

**No hardcoded credentials** in source. ✓

### 3.6 Performance

**Rating: Acceptable — one batching opportunity**

**Finding 7 — `table_exists` called up to 4x per table**

For each table, `table_exists` is called:
1. Phase 2: source check
2. Phase 2: target check
3. Phase 4 truncate: source check
4. Phase 4 insert: source check

For 10 tables this is 40 ALL_TABLES queries. Each is a lightweight indexed lookup, so the actual cost is negligible for a dev tool. But caching results (a `dict[tuple[str,str], bool]`) would reduce to 2 unique lookups per table and make the code's intent clearer.

**`build_dependency_graph` runs N queries for N tables** — could be batched into a single query with `table_name IN (...)`. For 10 tables this is 10 round trips vs 1. Same for `discover_fk_parents`.

**Direct-path INSERT** — correctly used. The `/*+ APPEND */` hint is embedded in the SQL. ✓ The known caveat (exclusive lock) is documented in README. ✓

### 3.7 Observability

**Rating: Strong**

`structlog` logging at every phase transition (`phase_start`), every table action (`creating_table`, `truncating`, `table_loaded`, `table_skipped_*`), every constraint operation (`disabling_constraint`, `enabling_constraint`), and every error (`table_load_failed`, `constraint_reenable_failed`).

`RefreshResult.summary()` gives a complete machine-readable audit trail.

`--dry-run -v` gives full DEBUG output of planned actions.

Gap: no timing logged for Phase 2 (prepare) or Phase 3/5 (constraint operations). Only per-table insert timing and total run timing are captured. Minor.

### 3.8 Edge Cases

**Rating: Acceptable**

| Case | Handled | How |
|------|---------|-----|
| Table missing from source | Yes | Phase 2 warning + continue; Phase 4 skipped status |
| Table has no indexes | Yes | ORA-31608 caught, returns [] |
| Circular FK dependency | Yes | `topological_sort` raises ValueError with cycle tables |
| FK parent in different schema | Yes | Documented — cross-schema refs ignored in auto-include |
| ORA-00955 (table already exists) | Yes | `_safe_execute` swallows |
| ORA-02298 (FK validate fails) | Yes | Logged, `overall_success=False`, continues |
| Zero tables configured | Yes | `RefreshConfig` validator raises |
| Empty target schema | Partial | Tables created without FK constraints (Finding 2) |
| `all_or_nothing` mode + TRUNCATE | Partial | Documented in README that TRUNCATE auto-commits |
| Connection loss mid-refresh | Partial | `conn.close()` in `finally` but mid-phase state is unclear |
| No connection timeout | Missing | `oracledb.connect()` has no timeout — hangs on unreachable host |

### 3.9 Technical Debt

**Rating: Acceptable**

Documented known limitations in README: no sequence reset, no incremental refresh, no parallel loading, TRUNCATE not rollback-safe.

One undocumented limitation: **FK constraints not created in target schema** (Finding 2). Should be added to README known limitations.

The `# type: ignore[arg-type]` at `cli.py:61` and `test_engine.py:84` are acceptable workarounds for the mypy/click type inference gap on `commit_mode`.

The `# noqa: S608` on `get_table_row_count` is correct — the SQL uses quoted identifiers from internal config, not user input.

### 3.10 Rollback

**Rating: Strong**

The tool creates no persistent state of its own (no lock files, no config files written). Rolling back the tool means deleting the directory. Rolling back a mistaken `schema-refresh` run means running it again (idempotent) or manually inspecting the target schema — which is the user's own schema.

### 3.11 Documentation

**Rating: Strong**

README covers: prerequisites, installation, env vars, YAML config, CLI flags, exit codes, known limitations. The VAT-PESM integration note is useful.

Gap: FK constraints not present in fresh target is a known limitation not yet documented in README.

Gap: `config.example.yaml` doesn't document the `commit_mode` and `insert_hint` fields inline (though README covers them).

### 3.12 Dependencies

**Rating: Strong**

| Dependency | Justification | Risk |
|------------|--------------|------|
| `oracledb>=2.0` | Oracle's official Python driver, thin mode | Low — stable, Oracle-maintained |
| `pydantic>=2.0` | Config validation, already used project-wide | Low |
| `pydantic-settings>=2.0` | Env/dotenv loading | Low |
| `structlog>=24.0` | Structured logging | Low |
| `pyyaml>=6.0` | YAML config parsing | Low — `safe_load` used |
| `click>=8.1` | CLI framework, matches existing tooling | Low |

No new risk introduced. All are well-maintained libraries already present in the broader tooling ecosystem.

---

## Phase 4: Failure Modes

| Category | Scenario | Handled | How | Tested |
|----------|----------|---------|-----|--------|
| Network | Oracle host unreachable | No | Hangs until OS TCP timeout | No |
| Network | Connection drops mid-refresh | Partial | `finally: conn.close()` | No |
| Data | FK parent row deleted, child exists in target | Yes | ORA-02298 caught, logged, run marked failed | No |
| Data | Source table dropped between Phase 2 and 4 | Partial | Phase 4 guard skips, but truncated already | No |
| State | TRUNCATE on child before parent when FK enabled | Yes | Phase 3 disables FKs first | Yes (indirectly) |
| State | Interrupted mid-run (Ctrl+C) | Partial | Truncated tables remain empty until next run | No |
| State | All-or-nothing: TRUNCATE then INSERT fails | Partial | TRUNCATE already committed (documented) | No |
| Auth | Expired session during long load | No | No keepalive, no reconnect logic | No |
| Auth | Wrong credentials | Yes | `oracledb.connect` raises, caught in CLI | No |
| Resources | Source table has 5.9M rows — memory | Yes | Direct-path INSERT streams, no Python materialization | N/A |
| Deploy | Re-run while first run is in progress | No | No locking mechanism | No |

**Most significant unhandled path**: connection timeout on `oracledb.connect()`. On a machine that can't reach the Oracle host, `schema-refresh` would hang silently with no feedback until the OS kills the TCP connection (typically 2+ minutes). A `tcp_connect_timeout` parameter to `oracledb.connect` would surface this immediately.

---

## Phase 5: Luck Inventory

1. **Single-statement execute**: SQL injection via `insert_hint` is blocked by `oracledb`'s single-statement-per-execute constraint. This was not explicitly designed as a security control.

2. **Session-level DDL transforms are sticky**: `_configure_ddl_transforms` sets `SESSION_TRANSFORM` params that persist for the connection's lifetime. This works because a fresh connection is created per run. If connection pooling were introduced, transforms from a previous run could leak to the next.

3. **DBMS_METADATA LOB size**: For very wide tables with many columns and constraints, the LOB returned by `GET_DDL` could be large. `lob.read()` materialises it fully in memory. This has never been a problem because `SUN_LEDGER` is a narrow table — but a 1000-column table could return a large LOB.

4. **No concurrent access**: The test environment has a single developer. If two developers ran `schema-refresh` targeting the same target schema simultaneously (impossible since each developer has their own schema) the exclusive lock from `/*+ APPEND */` would cause a hang. The tool design avoids this by design (personal schemas), but the assumption is never checked.

---

## Phase 6: Summary

### Dimension Ratings

| Dimension | Rating | Critical findings |
|-----------|--------|-------------------|
| Intent alignment | Strong | FK constraints absent from fresh target |
| Code quality | Acceptable | 34 ruff violations (mostly ANN401, unused imports) |
| Correctness | Acceptable | Phase 4 truncate checks wrong schema; unguarded `.code` access |
| Test coverage | Acceptable | ORA-02298 path untested; all_or_nothing untested |
| Error handling | Acceptable | Broad `except Exception` in Phase 4; no connect timeout |
| Security | Acceptable | `password` should be `SecretStr` |
| Performance | Acceptable | `table_exists` called 4x per table; FK queries not batched |
| Observability | Strong | — |
| Edge cases | Acceptable | No connect timeout |
| Technical debt | Acceptable | FK constraint limitation not in README |
| Rollback | Strong | — |
| Documentation | Strong | Missing one known limitation |
| Dependencies | Strong | — |

### Critical (must fix before using against a schema you care about)

None. This is a dev tool against your own schema. No data loss risk.

### Important (should fix before wider use or automation)

1. **`OracleConnection.password` → `SecretStr`** (`config.py:20`) — latent credential exposure in repr/logs.
2. **Add `tcp_connect_timeout` to `oracledb.connect()`** (`engine.py:93`) — currently hangs on unreachable host.
3. **Narrow `except Exception` to `oracledb.DatabaseError`** in Phase 4 insert loop (`engine.py:207`) — programming errors silently appear as failed tables.
4. **Test ORA-02298 path** — the most realistic production failure mode is untested.
5. **Document FK constraints not created in fresh target** — add to README known limitations.

### Observations (consider)

6. Phase 4 truncate guard should check target schema, not source (`engine.py:177`).
7. `exc.args[0].code` → `getattr(exc.args[0], "code", None)` for defensive access (`engine.py:270, 235`).
8. Remove unused imports from test files (`call`, `patch`, `RefreshEngine` in per-function imports) — fixable with `ruff --fix`.
9. Remove deprecated `ANN101`/`ANN102` from ruff ignore list in `pyproject.toml`.
10. SQL for FK parent discovery is duplicated across `discover_fk_parents` and `build_dependency_graph` — extract `_query_fk_parents(cur, schema, table)`.
11. `table_exists` called 4x per table — cache results in a `dict[tuple, bool]` within `_execute`.

### Patterns to repeat

- **Plan-then-implement with TDD stages** worked well. All 40 tests were written before implementation and caught real bugs during development (the cycle detection message, the mock side_effect scope issue).
- **`structlog` at every phase transition** — makes a dry run useful as a pre-flight checklist and makes failures easy to diagnose from logs alone.
- **`RefreshResult.summary()` as a structured return** — clean separation between the engine result and the CLI presentation.
- **`_safe_execute` with an explicit set of idempotent error codes** — better than ad-hoc try/except at each call site.

---

## Phase 7: Action Items

| # | Action | Priority | Owner | Done criteria |
|---|--------|----------|-------|---------------|
| 1 | Change `OracleConnection.password` to `SecretStr`; update `engine.py:95` to call `.get_secret_value()` | Important | implementer | `ruff`, tests pass; `repr(oracle_conn)` does not show password |
| 2 | Add `tcp_connect_timeout=10` to `oracledb.connect()` call in `engine.py:93` | Important | implementer | Running with unreachable DSN exits within 15s with clear error |
| 3 | Narrow `except Exception` → `except (oracledb.DatabaseError, oracledb.InterfaceError)` at `engine.py:207` | Important | implementer | `ruff`, tests pass |
| 4 | Add test for ORA-02298 path in `test_engine.py` | Important | implementer | Test fails before fix, passes after; Phase 5 error branch covered |
| 5 | Add test for `all_or_nothing` commit mode | Important | implementer | `engine.py:221` line covered; single `conn.commit()` called |
| 6 | Add "FK constraints not present in fresh target schema" to README known limitations | Important | implementer | README updated |
| 7 | Fix Phase 4 truncate guard: `cfg.source_schema` → `cfg.target_schema` at `engine.py:177` | Observation | implementer | Tests updated to reflect corrected semantics |
| 8 | `ruff --fix src/ tests/` to auto-fix unused imports, import sorting, line length | Observation | implementer | `ruff check` exits 0 |
| 9 | Remove `ANN101`, `ANN102` from `pyproject.toml` ruff ignore list | Observation | implementer | No ruff warning on `ruff check` invocation |
| 10 | Replace `exc.args[0].code` with `getattr(exc.args[0], "code", None)` at `engine.py:270, 235` | Observation | implementer | No `AttributeError` possible from DatabaseError handler |

---

## Phase 8: Retrospective

### What concrete decisions would change starting over?

`SecretStr` for the password field from the beginning — it's a one-line change at config time but requires propagating `get_secret_value()` everywhere after the fact. Type it correctly at the start.

### What's the weakest part?

The test suite for `engine.py`. The 5-phase orchestrator has the most complex behaviour but only 9 tests, several of which are thin (test that a method was called, not what it did). The ORA-02298 and `all_or_nothing` paths are entirely untested. These are exactly the paths that would fail in real use.

### Where is confidence lowest?

The `_configure_ddl_transforms` → `DBMS_METADATA.GET_DDL` path. This has never been run against a real Oracle instance. The mock returns a predetermined string. Whether the actual DBMS_METADATA output format matches the assumptions (schema always quoted, semicolon termination, no PL/SQL blocks) is unknown until a real run.

### What surprised the implementer?

The `execute.side_effect` scoping in the mock setup for `test_engine_swallows_ora_00955_on_create_table`. Setting a side_effect on a mock cursor fires for every `execute` call on that cursor — including TRUNCATE, which isn't in `_safe_execute`. Required the side_effect to be a function that inspects the SQL rather than a blanket exception.

### What non-code friction slowed implementation?

The `python-oracledb` vs `oracledb` PyPI package name mismatch required an extra fix cycle at scaffold. The package is documented as `python-oracledb` everywhere but published as `oracledb` on PyPI.

### What systemic insight emerged?

For a tool that wraps a database, 100% of the interesting behaviour is in the database interaction paths — and all of those paths are mocked. The unit tests are structurally sound but the confidence they provide about actual Oracle compatibility is low. A single smoke-test run against the real dev DB (connect, extract one table DDL, INSERT one row, check row count) would provide more real-world confidence than all 40 unit tests combined.
