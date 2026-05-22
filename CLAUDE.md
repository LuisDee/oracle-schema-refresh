# CLAUDE.md

Project conventions for any Claude / agent session in this repo. Read
`docs/redesign.md` first — that's the architectural contract. This file
is just the working rules.

## What this project is

A lightweight Oracle copy CLI. Today: same-instance schema refresh.
Target: cross-host, parallel, multi-table copy, super easy for agents to
drive. Thin Python orchestrator + thick Oracle server-side execution.

See `docs/redesign.md` for the full design and the cuts roadmap.

## Working rules

1. **Land work as cuts.** Each cut is one PR, lands green tests, ships
   to the roadmap in `docs/redesign.md` §5. Don't collapse cuts.
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
6. **Confirm before destructive or shared-state actions** (git push,
   force-push, `DROP`, `TRUNCATE` outside the engine's own DDL/DML).
   Local file edits and `pytest` runs are fine without asking.
7. **Match scope to the request.** Don't add abstractions, error
   handlers, or fallback paths for cases that can't happen. Internal
   code can trust internal code.

## Repo layout

- `src/oracle_schema_refresh/` — package source.
- `tests/` — unit tests, mock `oracledb` heavily. Fast, no Oracle
  required.
- `tests/integration/` — (not yet created) real-Oracle tests via
  testcontainers. Gated behind `pytest -m integration`.
- `docs/redesign.md` — the design contract.
- `plan.md`, `review.md` — historical v0 plan and the review that led
  to the redesign. Kept for context. Where they disagree with
  `docs/redesign.md`, the redesign wins.

## Testing

Run `pytest -q` from the repo root. Tests should run in well under a
second. If they start being slow, you've probably accidentally hit a
real network or filesystem call.

When adding Oracle SQL, prefer tests that assert on the SQL string
constructed rather than on the side effects of running it. Real-DB
behaviour is the integration suite's job, not the unit suite's.

## Commits

- One cut = one commit (rebase locally to squash if needed before
  push, but don't squash merged history).
- Subject line: imperative, ≤72 chars. Examples:
  - `Cut 0: introduce Endpoint abstraction (no behaviour change)`
  - `Cut 1a: consistent-read SCN, explicit column lists, ...`
- Body: explain *why*, list what changed at module level, note any
  back-compat decisions.
- Don't include model identifiers in commit messages.

## Open questions

Live in `docs/redesign.md` §7. When you resolve one, update that
section and reference the decision in the relevant cut's commit.

## What not to do

- Don't import vendored copies of other projects (e.g. ECC) into this
  repo. If a pattern is worth borrowing, reimplement it minimally.
- Don't add `CLAUDE.md` content here that should live in
  `docs/redesign.md`. This file is for working rules; that file is for
  the design contract.
- Don't add features beyond the current cut's scope, even if they're
  small. The cuts exist to keep reviews tractable.
