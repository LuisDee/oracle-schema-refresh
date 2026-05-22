# CLAUDE.md

Project conventions for any Claude / agent session in this repo. Read
`docs/redesign.md` first — that's the architectural contract. This file
is just the working rules.

## What this project is

A lightweight Oracle copy CLI. Today: same-instance schema refresh **and**
cross-host single-statement copy via DB link. Target: parallel multi-table
copy, super easy for agents to drive. Thin Python orchestrator + thick
Oracle server-side execution.

See `docs/redesign.md` for the full design and cuts roadmap. Current
status (May 2026): **Cuts 0, 1a, 1a-followup, 1b done**. Cut 1c next.

## Two CLIs, both live

- `schema-refresh` — legacy same-instance entry point.
  Loads credentials from `ORACLE_*` env / `.env`. Never deprecated.
- `oracdb` — newer agent-facing entry point.
  Subcommands: `endpoints add/list/remove/test`, `copy`. Reads from
  `~/.oracdb/endpoints.yaml` (override with `$ORACDB_REGISTRY` or
  `--registry`). `plan` / `run` / `status` / `verify` / `cancel` /
  `cleanup` / `wait` land in Cut 1c.

Both must keep working through every cut.

## Working rules

1. **Land work as cuts.** Each cut is one commit, lands green tests,
   ships to the roadmap in `docs/redesign.md` §5. Don't collapse cuts.
2. **Test-first.** New behaviour starts with a failing test, then code,
   then green. No untested behaviour change.
3. **The legacy `schema-refresh` CLI must keep working** through every
   cut. It's a stable, simple interface for the same-instance case and
   not deprecated.
4. **Never silently swallow ORA codes.** `_safe_execute` ignore codes
   must be passed per call, scoped to the operation that actually
   tolerates them. Defaulting to the union of all idempotent codes is
   how real bugs get masked.
5. **No row data through Python.** Data movement is server-side SQL.
   If you reach for `cursor.fetchmany()` on table data, stop and pick a
   different strategy.
6. **One SCN per job.** All source reads (`SELECT current_scn`, source
   row count, INSERT SELECT) anchor to one SCN captured at job start.
   Across-table consistency depends on it.
7. **Confirm before destructive or shared-state actions** (git push,
   force-push, `DROP`, `TRUNCATE` outside the engine's own DDL/DML).
   Local file edits and `pytest` runs are fine without asking.
8. **Match scope to the request.** Don't add abstractions, error
   handlers, or fallback paths for cases that can't happen. Internal
   code can trust internal code.

## Repo layout

- `src/oracle_schema_refresh/`
  - `cli.py` — legacy `schema-refresh` entry point.
  - `oracdb_cli.py` — new `oracdb` entry point + subcommands.
  - `engine.py` — `RefreshEngine`, the 5-phase orchestrator. Splits
    into `job.py` + `strategy/*` in Cut 2.
  - `introspect.py` — FK discovery, dependency graph, DDL extraction,
    column lists, identity/sequence detection, server version. Splits
    into `introspect/` package in Cut 2.
  - `config.py` — `OracleConnection` (env-loaded) and `RefreshConfig`.
  - `endpoints/` — `Endpoint`, `EndpointRegistry`, dblink lifecycle.
- `tests/` — unit tests, mock `oracledb` heavily. Fast (<1s total).
  - `_helpers.py` — `make_mock_conn()`, `patched_introspect(...)`.
    Use these in new tests rather than rebuilding the nested-`with patch`
    stack.
  - `tests/integration/` — not yet created. Cut 1b's exit criterion
    needs this against `gvenzl/oracle-free:23-slim` via testcontainers.
- `docs/redesign.md` — the design contract.
- `plan.md`, `review.md` — historical v0 plan and the review that led
  to the redesign. Kept for context. Where they disagree with
  `docs/redesign.md`, the redesign wins.

## Testing

```
pytest -q              # all 124 unit tests, <1s
ruff check src/ tests/ # zero warnings expected
```

When adding Oracle SQL, prefer tests that assert on the SQL string
constructed rather than on the side effects of running it. Real-DB
behaviour is the integration suite's job, not the unit suite's.

For tests that drive `RefreshEngine.run`, use `patched_introspect(...)`
from `tests/_helpers.py` instead of writing nested `with patch(...)`
blocks — every existing test in the repo follows this pattern.

## Commits

- One cut = one commit (rebase locally to squash if needed before
  push, but don't squash merged history).
- Subject line: imperative, ≤72 chars. Examples:
  - `Cut 0: introduce Endpoint abstraction (no behaviour change)`
  - `Cut 1a: correctness fixes — SCN, explicit columns, validation, sequences`
  - `Cut 1b: cross-host direct_copy strategy`
- Body: explain *why*, list what changed at module level, note any
  back-compat decisions, reference §7 open questions resolved.
- Don't include model identifiers in commit messages.

## Open questions

Live in `docs/redesign.md` §7. Resolved so far: **q1** (commit_mode
naming — kept the field, added `defer_insert_commits` honest value,
`all_or_nothing` is a deprecated alias), **q2** (cross-host without
CREATE DATABASE LINK — refuse with clear error, no Python row
shipping), **q5** (oracdb / schema-refresh coexistence — permanent).
Still open: q3 (job_id format — Cut 1c), q4 (chunk-size unit — Cut 2).

When you resolve one, update §7 in `docs/redesign.md` and reference
the decision in the relevant cut's commit body.

## What not to do

- Don't import vendored copies of other projects (e.g. ECC) into this
  repo. If a pattern is worth borrowing, reimplement it minimally.
- Don't add `CLAUDE.md` content here that should live in
  `docs/redesign.md`. This file is for working rules; that file is for
  the design contract.
- Don't add features beyond the current cut's scope, even if they're
  small. The cuts exist to keep reviews tractable.
- Don't store passwords anywhere new. Today they live in
  `~/.oracdb/endpoints.yaml` (Cut 1b) and in `.env` (legacy). Wallet
  auth is the Cut 3 fix; resist adding "just for now" secret stores.
