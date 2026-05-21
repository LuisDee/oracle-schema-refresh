# OracleSchemaRefresh — Remediations + Oracle Schema Refresh Skill

**Version**: 2.0 (supersedes initial implementation plan)
**Scope**: Code review remediation (10 action items) + new Claude skill

---

## Phase 0: Research Findings

### Stack
- Language: Python 3.10+
- Package manager: uv with hatchling build backend
- Linter: ruff (select: E, F, I, UP, ANN)
- Test framework: pytest with unittest.mock
- Config: pydantic-settings v2 + pydantic v2
- CLI: click 8.1
- Oracle driver: oracledb (thin mode)
- Logging: structlog

### Existing skill patterns
Two reference patterns studied:
1. **jira skill** (`~/.claude/skills/jira/SKILL.md`) — front-matter + mandatory protocol + Python scripts in `scripts/` that wrap an API. Scripts return JSON to stdout, `{"error": "..."}` to stderr, exit 1 on failure.
2. **database-access skill** (`~/.claude/skills/database-access/SKILL.md`) — front-matter + bash command templates + `databases.yaml` for named DB configs. No wrapper scripts; the CLIs (psql/sqlcl) are powerful enough on their own.

**Decision**: oracle-schema-refresh skill follows the **database-access pattern** (bash templates + YAML preset config), not the jira pattern. The `schema-refresh` CLI is already feature-complete; no wrapper scripts needed.

### Credential infrastructure already in place
`/home/coder/secrets/oracle-backoffice-dev.env` exists (created by database-access skill setup). Contains `DB_PASSWORD`. The oracle-schema-refresh skill reuses this file — no new secrets setup required.

### Tests needing coverage (from review.md)
| Missing test | Engine path | Currently covered |
|--------------|-------------|-------------------|
| ORA-02298 on re-enable | `engine.py:233–244` | No |
| `all_or_nothing` commit | `engine.py:221` | No |
| `SecretStr` repr safety | `config.py:20` | No (opposite — tests plain string equality) |
| Non-Oracle exception propagates from Phase 4 | `engine.py:207` | No |

---

## Phase 1: Business Outcome

**Business outcome**: Close the 10 open review action items to make OracleSchemaRefresh safe to run in automation (credential exposure, silent errors, hangs all addressed), and package it as a Claude skill so developers can refresh their Oracle dev tables by conversational request without knowing CLI syntax.

**How we'll know it worked**:
1. `ruff check src/ tests/` exits 0; `pytest tests/ --tb=short` shows ≥44 tests passing; `engine.py` coverage ≥93%
2. The oracle-schema-refresh skill is loaded by Claude and `--dry-run` against the real Oracle instance completes in <15 s when invoked via the skill
3. `repr(OracleConnection(...))` does not contain the password value

### Open questions
None — all 10 action items are precisely specified in `review.md`. The skill design follows an established pattern with sufficient prior art.

### Assumptions

| # | Assumption | Confidence | Fallback |
|---|-----------|------------|---------|
| 1 | `oracledb.connect` accepts `tcp_connect_timeout` kwarg in thin mode | High | Remove param, document manual timeout workaround |
| 2 | pydantic-settings v2 accepts a plain string for a `SecretStr` field (wraps automatically) | High | Pass `SecretStr("p")` explicitly in `_make_engine` |
| 3 | `/home/coder/secrets/oracle-backoffice-dev.env` contains `DB_PASSWORD` | High (verified — file exists) | Create the file |
| 4 | `.venv/bin/schema-refresh` path works after `uv pip install -e ".[dev]"` | High | Use `uv run schema-refresh` as fallback |

### Pre-mortem
- **SecretStr cascade**: changing `password: str → SecretStr` requires `get_secret_value()` at every call site. Missed call sites cause a `TypeError`. Mitigation: grep for `\.password` after the change.
- **Exception test brittleness**: the Phase 4 exception-propagation test patches `_insert_table` directly. If the method is renamed, the patch silently stops working. Mitigation: use `patch.object` (fails loudly if attribute not found).
- **Skill not loaded**: if the SKILL.md front-matter is malformed, Claude won't load it. Mitigation: validate YAML front-matter parses cleanly before finalising.

---

## Phase 2: Plan

### Scope

**In scope**:
- All 10 action items from `review.md` (6 Important + 4 Observations)
- New file: `~/.claude/skills/oracle-schema-refresh/SKILL.md`
- New file: `~/.config/oracle-schema-refresh/vatpesm.yaml` (VAT-PESM preset)
- Update `test_config.py` to test SecretStr behaviour (intentional API change)

**Out of scope**:
- New CLI features (incremental refresh, parallelism, sequence reset)
- Live integration tests against Oracle (manual smoke test only, post-implementation)
- Skill Python wrapper scripts (bash templates are sufficient)

**Grey areas**:
- `tcp_connect_timeout` kwarg name: verify against installed oracledb version during Stage 2. If not accepted, use `connection_timeout`.

### TDD Skill Invocation Table

| Skill / Agent | Stage | When to invoke |
|--------------|-------|---------------|
| `/tdd-workflow` | Stage 1, 2, 3 | Before each stage begins |
| `code-reviewer` | Stage 1, 2, 3, 4 | After each stage completes |
| `security-reviewer` | Stage 2, 4 | After SecretStr changes and after credential skill lands |
| `python-reviewer` | Stage 2, 3 | After Python source changes |
| `/verification-before-completion` | End | Before marking entire plan done |
| `/systematic-debugging` | Any | On any test failure before attempting fix |

---

## Stage 1: New test coverage (RED phase — tests fail before Stage 2 code changes)

**Goal**: Write four tests that define the acceptance criteria for Stage 2 code changes — three will fail now, one (all_or_nothing) may already pass.

**Why first**: TDD mandate. Tests define the contract; code must satisfy them.

**Depends on**: None.

**Files affected**:
- `tests/test_config.py` — add `test_oracle_connection_password_is_secret_str`
- `tests/test_engine.py` — add three new tests

**Approach**:

Add to `tests/test_config.py` (after the existing `test_oracle_connection_loads_from_env`):

```python
def test_oracle_connection_password_is_secret_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_USERNAME", "testuser")
    monkeypatch.setenv("ORACLE_PASSWORD", "secret123")
    monkeypatch.setenv("ORACLE_DSN", "host:1521/svc")
    from pydantic import SecretStr
    from oracle_schema_refresh.config import OracleConnection

    conn = OracleConnection()
    assert isinstance(conn.password, SecretStr)
    assert "secret123" not in repr(conn)
```

Add to `tests/test_engine.py`:

```python
def test_engine_phase5_ora02298_marks_run_failed() -> None:
    """ORA-02298 on FK re-enable must set success=False but not abort."""
    import oracledb
    from oracle_schema_refresh.engine import RefreshEngine

    engine = _make_engine()
    mock_conn = _make_mock_conn()
    fake_constraints = [{"name": "FK_ONE", "table": "T1", "status": "ENABLED"}]

    ora_err = oracledb.DatabaseError()
    ora_err.args = (MagicMock(code=2298, message="ORA-02298: cannot validate"),)

    def raise_on_enable(sql: str, *a: object, **kw: object) -> None:
        if isinstance(sql, str) and "ENABLE" in sql.upper():
            raise ora_err

    mock_conn.cursor().__enter__().execute.side_effect = raise_on_enable

    with patch("oracledb.connect", return_value=mock_conn):
        with patch("oracle_schema_refresh.engine.introspect.table_exists", return_value=True):
            with patch("oracle_schema_refresh.engine.introspect.discover_fk_parents", return_value=["T1"]):
                with patch("oracle_schema_refresh.engine.introspect.build_dependency_graph", return_value={"T1": []}):
                    with patch("oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target", return_value=fake_constraints):
                        with patch("oracle_schema_refresh.engine.introspect.get_table_row_count", return_value=0):
                            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.success is False
    assert result.constraints_reenabled == []


def test_engine_all_or_nothing_commits_once_at_end() -> None:
    """all_or_nothing mode must commit exactly once after all tables are inserted."""
    from oracle_schema_refresh.engine import RefreshEngine

    engine = _make_engine(tables=["T1", "T2"], commit_mode="all_or_nothing")
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch("oracle_schema_refresh.engine.introspect.table_exists", return_value=True):
            with patch("oracle_schema_refresh.engine.introspect.discover_fk_parents", return_value=["T1", "T2"]):
                with patch("oracle_schema_refresh.engine.introspect.build_dependency_graph", return_value={"T1": [], "T2": []}):
                    with patch("oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target", return_value=[]):
                        with patch("oracle_schema_refresh.engine.introspect.get_table_row_count", return_value=0):
                            engine.run(dry_run=False)  # type: ignore[union-attr]

    assert mock_conn.commit.call_count == 1


def test_engine_phase4_non_oracle_exception_propagates() -> None:
    """ValueError inside _insert_table must propagate, not be silently caught as 'failed' status."""
    from oracle_schema_refresh.engine import RefreshEngine

    engine = _make_engine()
    mock_conn = _make_mock_conn()

    with patch("oracledb.connect", return_value=mock_conn):
        with patch("oracle_schema_refresh.engine.introspect.table_exists", return_value=True):
            with patch("oracle_schema_refresh.engine.introspect.discover_fk_parents", return_value=["T1"]):
                with patch("oracle_schema_refresh.engine.introspect.build_dependency_graph", return_value={"T1": []}):
                    with patch("oracle_schema_refresh.engine.introspect.get_fk_constraints_on_target", return_value=[]):
                        with patch.object(
                            engine,  # type: ignore[union-attr]
                            "_insert_table",
                            side_effect=ValueError("unexpected programming error"),
                        ):
                            with pytest.raises(ValueError, match="unexpected programming error"):
                                engine.run(dry_run=False)
```

**Expected RED results**:
- `test_oracle_connection_password_is_secret_str` → FAIL (`conn.password` is `str`, not `SecretStr`)
- `test_engine_phase5_ora02298_marks_run_failed` → PASS (code path already exists)
- `test_engine_all_or_nothing_commits_once_at_end` → PASS (code path already exists)
- `test_engine_phase4_non_oracle_exception_propagates` → FAIL (`except Exception` catches `ValueError`)

**TDD — RED phase**:
```
cd /home/coder/scripts/tooling/OracleSchemaRefresh && \
  .venv/bin/pytest tests/test_config.py::test_oracle_connection_password_is_secret_str \
    tests/test_engine.py::test_engine_phase5_ora02298_marks_run_failed \
    tests/test_engine.py::test_engine_all_or_nothing_commits_once_at_end \
    tests/test_engine.py::test_engine_phase4_non_oracle_exception_propagates -v
```
Expected: 2 FAIL, 2 PASS.

**Post-stage validation**: code-reviewer agent.

**Risks**: None — adding tests only. Full regression suite must still pass.

**Rollback**: Delete the 4 added test functions.

**Status**: [ ] Not Started

---

## Stage 2: Code hardening (GREEN — makes Stage 1 failing tests pass)

**Goal**: Fix all 10 review action items in source code; all new tests pass; full suite green.

**Depends on**: Stage 1.

**Files affected**:
- `src/oracle_schema_refresh/config.py`
- `src/oracle_schema_refresh/engine.py`
- `tests/test_config.py` (update existing test for SecretStr API)

**Approach**:

### 2a — config.py: SecretStr (Important #1)

```python
# Add to imports at top of config.py:
from pydantic import SecretStr

# Change field declaration (line 20):
# BEFORE: password: str
# AFTER:  password: SecretStr
```

Downstream in `engine.py` line 95:
```python
# BEFORE: password=self._oracle_conn.password,
# AFTER:  password=self._oracle_conn.password.get_secret_value(),
```

Update `test_config.py` line 12 (intentional API change — old test asserts plain-string equality):
```python
# BEFORE: assert conn.password == "testpass"
# AFTER:  assert conn.password.get_secret_value() == "testpass"
```

After change, grep for all missed call sites:
```bash
grep -n "\.password[^_.]" src/oracle_schema_refresh/*.py tests/*.py
```

### 2b — engine.py: connect timeout (Important #2)

```python
# lines 93–97 BEFORE:
conn = oracledb.connect(
    user=self._oracle_conn.username,
    password=self._oracle_conn.password,
    dsn=self._oracle_conn.dsn,
)

# AFTER:
conn = oracledb.connect(
    user=self._oracle_conn.username,
    password=self._oracle_conn.password.get_secret_value(),
    dsn=self._oracle_conn.dsn,
    tcp_connect_timeout=10,
)
```

Pre-flight check before editing — verify kwarg is valid in installed version:
```bash
cd /home/coder/scripts/tooling/OracleSchemaRefresh && \
  .venv/bin/python3 -c "import inspect, oracledb; print(inspect.signature(oracledb.connect))"
```
If `tcp_connect_timeout` is absent, use `connection_timeout=10` instead.

### 2c — engine.py: narrow exception in Phase 4 (Important #3)

```python
# line 207 BEFORE: except Exception as exc:
# AFTER:
except (oracledb.DatabaseError, oracledb.InterfaceError) as exc:
```

### 2d — engine.py: fix truncate guard (Observation #7)

```python
# line 177 BEFORE:
if not introspect.table_exists(conn, cfg.source_schema, table):
# AFTER:
if not introspect.table_exists(conn, cfg.target_schema, table):
```

Impact analysis: `test_engine_skips_table_missing_from_source` uses `fake_table_exists` returning `True` for TGT, `False` for SRC. After fix, Phase 4 truncate guard checks TGT (returns True → truncate runs); insert guard still checks SRC (returns False → status="skipped"). Test assertion `status=="skipped"` still holds. Verify by running the test.

### 2e — engine.py: defensive getattr (Observation #10)

Two locations (lines 270 and 234):
```python
# BEFORE: code = exc.args[0].code if exc.args else None
# AFTER:  code = getattr(exc.args[0], "code", None) if exc.args else None
```

**TDD — GREEN phase**:
```
cd /home/coder/scripts/tooling/OracleSchemaRefresh && \
  .venv/bin/pytest tests/ -v --tb=short
```
Expected: All 44+ tests pass.

**TDD — REFACTOR phase**: No structural changes. Run same command.

**Post-stage validation**:
- `security-reviewer` agent (credential handling changed)
- `python-reviewer` agent
- `grep -n "\.password[^_.]" src/oracle_schema_refresh/*.py tests/*.py` — verify only `.get_secret_value()` references remain

**Risks**: `tcp_connect_timeout` kwarg may be wrong name — pre-flight check mitigates.

**Rollback**: `git checkout src/oracle_schema_refresh/config.py src/oracle_schema_refresh/engine.py tests/test_config.py`

**Status**: [ ] Not Started

---

## Stage 3: Linter cleanup + README update

**Goal**: `ruff check` exits 0; README documents the FK constraint known limitation.

**Depends on**: Stage 2.

**Files affected**:
- `src/` and `tests/` (auto-fixed by ruff)
- `pyproject.toml` — ruff ignore list
- `README.md` — known limitations section

**Approach**:

### 3a — Auto-fix
```bash
cd /home/coder/scripts/tooling/OracleSchemaRefresh && .venv/bin/ruff check --fix src/ tests/
```
Fixes: unused imports (`call`, `patch` in tests), import sort in `test_introspect.py`, long lines.

### 3b — pyproject.toml ruff ignore list

```toml
# BEFORE:
ignore = ["ANN101", "ANN102"]

# AFTER (ANN401 retained for oracledb thin-mode Any annotations):
ignore = ["ANN401"]  # oracledb thin-mode doesn't export a clean Connection type for annotations
```

### 3c — README known limitation

Under `## Known limitations`, add after the "Tables with triggers" bullet:

```markdown
- **FK constraints not created in target schema.** DDL extraction uses `REF_CONSTRAINTS=FALSE` — FK constraints are not emitted during `CREATE TABLE`. A fresh target schema has no FK constraint enforcement. This is acceptable for dev testing but means the target schema is structurally different from source.
```

**TDD — RED phase**:
```
cd /home/coder/scripts/tooling/OracleSchemaRefresh && .venv/bin/ruff check src/ tests/; echo "exit: $?"
```
Expected: Non-zero (violations exist).

**TDD — GREEN phase**: After `ruff --fix` and edits.
```
cd /home/coder/scripts/tooling/OracleSchemaRefresh && .venv/bin/ruff check src/ tests/ && echo "CLEAN"
```
Expected: `CLEAN`.

**TDD — REFACTOR phase**: Run full test suite to confirm ruff --fix didn't alter logic.
```
cd /home/coder/scripts/tooling/OracleSchemaRefresh && .venv/bin/pytest tests/ -v
```

**Post-stage validation**: code-reviewer agent.

**Risks**: `ruff --fix` may occasionally make an incorrect change on complex expressions. Review the diff before accepting.

**Rollback**: `git checkout pyproject.toml README.md src/ tests/`

**Status**: [ ] Not Started

---

## Stage 4: Oracle Schema Refresh Skill

**Goal**: A Claude skill at `~/.claude/skills/oracle-schema-refresh/SKILL.md` enabling conversational invocation of `schema-refresh` with dry-run gating, credential sourcing, and named presets.

**Why last**: Depends on the CLI being hardened (Stages 1–3) before documenting it as the canonical invocation method.

**Depends on**: Stages 1–3.

**Files affected**:
- `~/.claude/skills/oracle-schema-refresh/SKILL.md` (new)
- `~/.config/oracle-schema-refresh/vatpesm.yaml` (new)

**Approach**:

### 4a — Preset config

`~/.config/oracle-schema-refresh/vatpesm.yaml`:
```yaml
# VAT-PESM: Oracle tables required for VAT-PESM integration test suite
source_schema: BACKOFFICE
target_schema: LDEBURNA
tables:
  - SUN_LEDGER
auto_include_fk_parents: true
recreate_tables: false
commit_mode: per_table
insert_hint: "/*+ APPEND */"
```

### 4b — Skill file

Front-matter trigger keywords: "schema refresh", "refresh oracle", "refresh tables", "refresh BACKOFFICE", "refresh dev schema", "oracle schema", "sync my oracle", "refresh SUN_LEDGER", "oracle-schema-refresh".

**Credential sourcing** (reuses database-access secret file pattern):
```bash
(
  source /home/coder/secrets/oracle-backoffice-dev.env
  export ORACLE_USERNAME=ldeburna
  export ORACLE_PASSWORD=$DB_PASSWORD
  export ORACLE_DSN=uk01vdb007.uk.makoglobal.com:1521/dev
  /home/coder/scripts/tooling/OracleSchemaRefresh/.venv/bin/schema-refresh [ARGS]
)
```

**SKILL.md outline**:
1. Front-matter (name + description with trigger keywords)
2. **Available presets** — table of preset name → file → tables → description
3. **MANDATORY DRY-RUN PROTOCOL** — always run `--dry-run -v` first, show full output, ask for explicit confirmation before real run (mirrors jira skill's Draft-for-Review)
4. **Bash command templates**:
   - Dry run with named preset
   - Real run with JSON output
   - Inline (no config file) for ad-hoc table refresh
   - Recreate mode (`--recreate`) — with extra confirmation gate
5. **Reading results** — `+`/`~`/`!` icons, JSON fields, exit codes
6. **Adding a new preset** — create YAML in `~/.config/oracle-schema-refresh/`
7. **Fallback** — if `.venv` is stale, use `cd ... && uv run schema-refresh`
8. **Known limitations** — FK constraints, TRUNCATE not rollback-safe, no incremental

**Testing for skill** (skill files are markdown, not programs — no pytest):

Pre-condition (RED equivalent):
```bash
ls ~/.claude/skills/oracle-schema-refresh/ 2>&1 || echo "NOT_FOUND"
```
Expected: `NOT_FOUND`.

Post-creation validation (GREEN):
```bash
# 1. YAML front-matter parses cleanly
python3 -c "
import yaml, pathlib
text = pathlib.Path('~/.claude/skills/oracle-schema-refresh/SKILL.md').expanduser().read_text()
parts = text.split('---', 2)
yaml.safe_load(parts[1])
print('front-matter OK')
"

# 2. Dry-run smoke test against real Oracle
(
  source /home/coder/secrets/oracle-backoffice-dev.env
  export ORACLE_USERNAME=ldeburna
  export ORACLE_PASSWORD=$DB_PASSWORD
  export ORACLE_DSN=uk01vdb007.uk.makoglobal.com:1521/dev
  /home/coder/scripts/tooling/OracleSchemaRefresh/.venv/bin/schema-refresh \
    --config ~/.config/oracle-schema-refresh/vatpesm.yaml \
    --dry-run -v
)
```
Expected: exits 0, logs show all 5 phases, `[DRY RUN]` in summary.

**Post-stage validation**:
- `security-reviewer` agent (documents credential handling)
- `code-reviewer` agent

**Risks**:
- Stale `.venv` path breaks all skill invocations. Mitigation: fallback command documented in skill.
- `~/.config/oracle-schema-refresh/` directory must be created first. Mitigation: `mkdir -p` in setup section of skill.

**Rollback**: `rm -rf ~/.claude/skills/oracle-schema-refresh/ ~/.config/oracle-schema-refresh/`

**Status**: [ ] Not Started

---

## Holistic Rollback

All four stages are independently reversible. No database migrations. No production system interaction. No point of no return.

| Stage | Rollback action |
|-------|----------------|
| 1 | Delete 4 test functions from test_config.py and test_engine.py |
| 2 | `git checkout src/oracle_schema_refresh/config.py src/oracle_schema_refresh/engine.py tests/test_config.py` |
| 3 | `git checkout pyproject.toml README.md src/ tests/` |
| 4 | `rm -rf ~/.claude/skills/oracle-schema-refresh/ ~/.config/oracle-schema-refresh/` |

---

## Phase 3: Verification Checklist

### TDD Compliance
- [ ] Every stage had tests written BEFORE implementation (RED phase)
- [ ] `test_oracle_connection_password_is_secret_str` and `test_engine_phase4_non_oracle_exception_propagates` confirmed failing in RED phase
- [ ] `test_config.py:12` update is an intentional API change (SecretStr), not fixing a test to pass broken code — documented
- [ ] `/tdd-workflow` skill invoked before each stage
- [ ] `/verification-before-completion` invoked before marking work done

### Quality Gates
- [ ] All tests pass: `cd /home/coder/scripts/tooling/OracleSchemaRefresh && .venv/bin/pytest tests/ -v` (≥44 tests)
- [ ] Linter clean: `.venv/bin/ruff check src/ tests/` exits 0
- [ ] `repr(OracleConnection(...))` does not contain the password value
- [ ] `grep -rn "\.password[^_.]" src/` shows only `.get_secret_value()` references
- [ ] `tcp_connect_timeout` accepted by installed oracledb (verified by pre-flight check)
- [ ] Phase 4 `except` is `(oracledb.DatabaseError, oracledb.InterfaceError)` — not bare `Exception`
- [ ] Skill YAML front-matter parses cleanly
- [ ] Skill dry-run smoke test exits 0 against `uk01vdb007`
- [ ] README known limitations includes FK constraints paragraph

### Skill / Agent Gates
- [ ] `code-reviewer` invoked after each stage
- [ ] `security-reviewer` invoked after Stages 2 and 4
- [ ] `python-reviewer` invoked after Stages 2 and 3

---

## Phase 4: Todo Breakdown

### Stage 1 — New tests
- [ ] Add `test_oracle_connection_password_is_secret_str` to `tests/test_config.py`
- [ ] Add `test_engine_phase5_ora02298_marks_run_failed` to `tests/test_engine.py`
- [ ] Add `test_engine_all_or_nothing_commits_once_at_end` to `tests/test_engine.py`
- [ ] Add `test_engine_phase4_non_oracle_exception_propagates` to `tests/test_engine.py`
- [ ] Run RED: confirm 2 fail (`password_is_secret_str`, `non_oracle_exception_propagates`), 2 pass

### Stage 2 — Code hardening
- [ ] `config.py`: `from pydantic import SecretStr`; `password: SecretStr`
- [ ] `engine.py:95`: `password=self._oracle_conn.password.get_secret_value()`
- [ ] `engine.py:93-97`: add `tcp_connect_timeout=10` (verify kwarg name first)
- [ ] `engine.py:207`: `except Exception` → `except (oracledb.DatabaseError, oracledb.InterfaceError)`
- [ ] `engine.py:177`: `cfg.source_schema` → `cfg.target_schema` in truncate guard
- [ ] `engine.py:270`: `exc.args[0].code` → `getattr(exc.args[0], "code", None)`
- [ ] `engine.py:234`: same getattr fix
- [ ] `tests/test_config.py:12`: `conn.password == "testpass"` → `conn.password.get_secret_value() == "testpass"`
- [ ] Grep for missed `.password` references
- [ ] Run GREEN: `pytest tests/ -v` — all pass
- [ ] Invoke `security-reviewer` and `python-reviewer` agents

### Stage 3 — Linter + README
- [ ] `ruff check --fix src/ tests/`
- [ ] `pyproject.toml`: replace `["ANN101", "ANN102"]` with `["ANN401"]` + comment
- [ ] `README.md`: add FK constraint limitation paragraph under Known Limitations
- [ ] Verify `ruff check src/ tests/` exits 0
- [ ] Run full suite to confirm no regressions

### Stage 4 — Skill
- [ ] `mkdir -p ~/.claude/skills/oracle-schema-refresh ~/.config/oracle-schema-refresh`
- [ ] Create `~/.config/oracle-schema-refresh/vatpesm.yaml`
- [ ] Create `~/.claude/skills/oracle-schema-refresh/SKILL.md`
- [ ] Validate YAML front-matter: `python3 -c "import yaml, pathlib; ..."` (command above)
- [ ] Smoke test dry-run against Oracle (command above)
- [ ] Invoke `security-reviewer` and `code-reviewer` agents

---

*Plan status: awaiting approval. No code has been written.*
