"""``oracdb`` CLI — new agent-friendly entry point.

Coexists with the legacy ``schema-refresh`` CLI (``cli.py``). Cut 1c adds
the phased subcommands (``plan``, ``run``, ``status``, ``verify``,
``cancel``, ``cleanup``, ``wait``) on top of Cut 1b's ``endpoints`` and
``copy``. Wallet auth lands in Cut 3.

Job state lives on the target endpoint (``oracdb$jobs`` /
``oracdb$tables``) so the CLI is stateless: kill it, move machines,
resume. See ``docs/redesign.md`` §2.5.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

from oracle_schema_refresh import introspect, state
from oracle_schema_refresh.config import RefreshConfig
from oracle_schema_refresh.endpoints.endpoint import Endpoint
from oracle_schema_refresh.endpoints.registry import (
    DuplicateEndpointError,
    EndpointRegistry,
    UnknownEndpointError,
)
from oracle_schema_refresh.engine import RefreshEngine
from oracle_schema_refresh.oracdb_io import envelope, finish


def _registry(ctx_obj: dict[str, Any]) -> EndpointRegistry:
    """Resolve registry path: ``--registry`` flag > ``ORACDB_REGISTRY`` env
    > ``~/.oracdb/endpoints.yaml``."""
    explicit = ctx_obj.get("registry_path")
    env_path = os.environ.get("ORACDB_REGISTRY")
    path = Path(explicit) if explicit else (Path(env_path) if env_path else None)
    reg = EndpointRegistry(path=path)
    reg.load()
    return reg


@click.group()
@click.option(
    "--registry",
    "registry_path",
    type=click.Path(),
    default=None,
    help="Path to endpoints.yaml. Defaults to $ORACDB_REGISTRY or "
    "~/.oracdb/endpoints.yaml.",
)
@click.pass_context
def oracdb(ctx: click.Context, registry_path: str | None) -> None:
    """Oracle cross-host copy CLI for agents."""
    ctx.ensure_object(dict)
    ctx.obj["registry_path"] = registry_path


# ---------------------------------------------------------------------------
# endpoints subgroup
# ---------------------------------------------------------------------------


@oracdb.group()
def endpoints() -> None:
    """Manage the named-endpoint registry."""


@endpoints.command("add")
@click.argument("name")
@click.option("--dsn", required=True, help="Easy Connect string, e.g. host:1521/svc.")
@click.option("--user", "username", required=True)
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=False,
    help="DB password. Prompted (hidden) if omitted.",
)
@click.option("--default-schema", default=None)
@click.pass_context
def endpoints_add(
    ctx: click.Context,
    name: str,
    dsn: str,
    username: str,
    password: str,
    default_schema: str | None,
) -> None:
    """Register a new endpoint by name."""
    reg = _registry(ctx.obj)
    try:
        reg.add(
            name=name,
            dsn=dsn,
            username=username,
            password=password,
            default_schema=default_schema,
        )
    except DuplicateEndpointError as exc:
        raise click.ClickException(str(exc)) from exc
    reg.save()
    click.echo(f"Added endpoint {name!r}.")


@endpoints.command("list")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def endpoints_list(ctx: click.Context, as_json: bool) -> None:
    """List registered endpoints. Never prints passwords."""
    reg = _registry(ctx.obj)
    names = reg.list_names()
    if as_json:
        payload = {
            "endpoints": names,
            "details": [
                {
                    "name": n,
                    "dsn": reg.get(n).dsn,
                    "username": reg.get(n).username,
                }
                for n in names
            ],
        }
        click.echo(json.dumps(payload))
        return
    if not names:
        click.echo("(no endpoints registered)")
        return
    for n in names:
        ep = reg.get(n)
        click.echo(f"  {n:20}  {ep.username}@{ep.dsn}")


@endpoints.command("remove")
@click.argument("name")
@click.pass_context
def endpoints_remove(ctx: click.Context, name: str) -> None:
    """Remove an endpoint from the registry."""
    reg = _registry(ctx.obj)
    try:
        reg.remove(name)
    except UnknownEndpointError as exc:
        raise click.ClickException(f"endpoint {name!r} not found") from exc
    reg.save()
    click.echo(f"Removed endpoint {name!r}.")


@endpoints.command("test")
@click.argument("name")
@click.pass_context
def endpoints_test(ctx: click.Context, name: str) -> None:
    """Open a connection to the endpoint and immediately close it."""
    reg = _registry(ctx.obj)
    try:
        ep = reg.get(name)
    except UnknownEndpointError as exc:
        raise click.ClickException(f"endpoint {name!r} not found") from exc
    try:
        conn = ep.connect()
    except Exception as exc:  # noqa: BLE001 — surface whatever oracledb says
        raise click.ClickException(f"connect failed: {exc}") from exc
    try:
        click.echo(f"OK — connected to {ep.dsn} as {ep.username}.")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# copy — one-shot phased-into-one convenience
# ---------------------------------------------------------------------------


@oracdb.command("copy")
@click.option("--from", "src_name", required=True, help="Source endpoint name.")
@click.option("--to", "tgt_name", required=True, help="Target endpoint name.")
@click.option("--source-schema", required=True)
@click.option("--target-schema", required=True)
@click.option(
    "--tables",
    required=True,
    help="Comma-separated list of tables (e.g. T1,T2,T3).",
)
@click.option(
    "--dblink",
    default=None,
    help="Required for cross-host copies. 'session' or 'existing:NAME'.",
)
@click.option(
    "--no-auto-fk",
    is_flag=True,
    default=False,
    help="Disable automatic FK parent inclusion.",
)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_copy(
    ctx: click.Context,
    src_name: str,
    tgt_name: str,
    source_schema: str,
    target_schema: str,
    tables: str,
    dblink: str | None,
    no_auto_fk: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Copy tables from one endpoint to another."""
    reg = _registry(ctx.obj)
    try:
        src = reg.get(src_name)
        tgt = reg.get(tgt_name)
    except UnknownEndpointError as exc:
        raise click.ClickException(f"unknown endpoint: {exc}") from exc

    try:
        cfg = RefreshConfig(
            source_schema=source_schema,
            target_schema=target_schema,
            tables=[t.strip() for t in tables.split(",") if t.strip()],
            auto_include_fk_parents=not no_auto_fk,
            dblink=dblink,
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, etc.
        raise click.ClickException(f"config error: {exc}") from exc

    try:
        engine = RefreshEngine.from_endpoints(src, tgt, cfg)
    except ValueError as exc:
        # Cross-host without --dblink etc.
        raise click.ClickException(str(exc)) from exc

    try:
        result = engine.run(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — bubble up cleanly
        raise click.ClickException(f"copy failed: {exc}") from exc

    if as_json:
        click.echo(json.dumps(result.summary()))
    else:
        status = "DRY RUN" if dry_run else ("OK" if result.success else "FAILED")
        click.echo(f"[{status}] copied {len(result.table_results)} table(s).")
        for r in result.table_results:
            click.echo(f"  - {r.table_name}: {r.status} ({r.rows_loaded} rows)")
    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Cut 1c: phased subcommands (plan / run / status / verify / cancel /
#                            cleanup / wait + hidden _worker)
# ---------------------------------------------------------------------------


def _resolve_endpoint(reg: EndpointRegistry, name: str) -> Endpoint:
    """Wrap ``UnknownEndpointError`` in click.ClickException for clean exits."""
    try:
        return reg.get(name)
    except UnknownEndpointError as exc:
        raise click.ClickException(f"unknown endpoint {name!r}") from exc


@oracdb.command("plan")
@click.option("--from", "src_name", required=True)
@click.option("--to", "tgt_name", required=True)
@click.option("--source-schema", required=True)
@click.option("--target-schema", required=True)
@click.option("--tables", required=True, help="Comma-separated list.")
@click.option("--dblink", default=None, help="'session' or 'existing:NAME'.")
@click.option("--no-auto-fk", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_plan(
    ctx: click.Context,
    src_name: str,
    tgt_name: str,
    source_schema: str,
    target_schema: str,
    tables: str,
    dblink: str | None,
    no_auto_fk: bool,
    as_json: bool,
) -> None:
    """Introspect and persist a job in PLANNED state. Prints the job id."""
    reg = _registry(ctx.obj)
    src = _resolve_endpoint(reg, src_name)
    tgt = _resolve_endpoint(reg, tgt_name)

    table_list = [t.strip() for t in tables.split(",") if t.strip()]
    try:
        cfg = RefreshConfig(
            source_schema=source_schema,
            target_schema=target_schema,
            tables=table_list,
            auto_include_fk_parents=not no_auto_fk,
            dblink=dblink,
        )
        engine = RefreshEngine.from_endpoints(src, tgt, cfg)
    except ValueError as exc:
        finish(
            envelope(
                ok=False,
                command="plan",
                error=str(exc),
                error_category="CONFIG",
            ),
            as_json,
        )
        return  # pragma: no cover

    plan_obj = engine.plan()

    # Persist the plan to target-side state.
    target_conn = tgt.connect()
    try:
        state.ensure_schema(target_conn)
        job_id = state.job_id_new()
        job = state.Job(
            job_id=job_id,
            source_endpoint=src.name,
            target_endpoint=tgt.name,
            source_schema=cfg.source_schema,
            target_schema=cfg.target_schema,
            scn=plan_obj.scn,
            config_json=json.dumps(
                {
                    "source_schema": cfg.source_schema,
                    "target_schema": cfg.target_schema,
                    "tables": cfg.tables,
                    "auto_include_fk_parents": cfg.auto_include_fk_parents,
                    "dblink": cfg.dblink,
                    "commit_mode": cfg.commit_mode,
                    "insert_hint": cfg.insert_hint,
                    "call_timeout_seconds": cfg.call_timeout_seconds,
                }
            ),
            status="PLANNED",
        )
        state.insert_job(target_conn, job)
        records = [
            state.TableRecord(job_id=job_id, table_name=t, status="PLANNED")
            for t in plan_obj.table_order
        ]
        state.insert_table_records(target_conn, records)
        target_conn.commit()
    finally:
        target_conn.close()

    finish(
        envelope(
            ok=True,
            command="plan",
            job_id=job_id,
            data=plan_obj.summary(),
        ),
        as_json,
    )


def _config_from_job(job: state.Job) -> RefreshConfig:
    raw = json.loads(job.config_json)
    return RefreshConfig(**raw)


def _execute_job(
    target_conn: Any,
    reg: EndpointRegistry,
    job: state.Job,
) -> tuple[bool, str | None]:
    """Run a planned/resumed job. Updates state as it goes.

    Returns ``(ok, error_message_or_none)``.
    """
    src = reg.get(job.source_endpoint)
    tgt = reg.get(job.target_endpoint)
    cfg = _config_from_job(job)
    engine = RefreshEngine.from_endpoints(src, tgt, cfg)

    state.update_job_status(target_conn, job.job_id, "RUNNING")
    target_conn.commit()

    try:
        result = engine.run(dry_run=False)
    except Exception as exc:  # noqa: BLE001 — surface to state and re-raise
        state.update_job_status(
            target_conn, job.job_id, "FAILED", error=str(exc)[:4000]
        )
        target_conn.commit()
        return False, str(exc)

    # Mirror per-table results into oracdb$tables.
    for r in result.table_results:
        state.update_table_record(
            target_conn,
            job_id=job.job_id,
            table_name=r.table_name,
            status=(
                "DONE" if r.status == "ok"
                else "SKIPPED" if r.status == "skipped"
                else "FAILED"
            ),
            rows_source=r.rows_source if r.rows_source else None,
            rows_target=r.rows_loaded if r.rows_loaded else None,
            error=r.error,
            sequences_reset=(
                json.dumps(r.sequences_reset) if r.sequences_reset else None
            ),
            mark_started=True,
            mark_ended=True,
        )
    state.update_job_status(
        target_conn,
        job.job_id,
        "DONE" if result.success else "FAILED",
        error=None if result.success else "one or more tables failed",
    )
    target_conn.commit()
    return result.success, None if result.success else "one or more tables failed"


@oracdb.command("run")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True, help="Target endpoint name.")
@click.option("--resume", is_flag=True, default=False,
              help="Re-run a non-PLANNED job (FAILED, CANCELLED).")
@click.option("--background", is_flag=True, default=False,
              help="Spawn a detached worker, return immediately with the job id.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_run(
    ctx: click.Context,
    job_id: str,
    tgt_name: str,
    resume: bool,
    background: bool,
    as_json: bool,
) -> None:
    """Execute a previously-planned job."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    try:
        job = state.get_job(target_conn, job_id)
    finally:
        if not background:
            pass  # we'll close after the run; reopen below for background
    if job is None:
        target_conn.close()
        finish(
            envelope(
                ok=False, command="run", job_id=job_id,
                error=f"unknown job {job_id!r}", error_category="CONFIG",
            ),
            as_json,
        )
        return

    if job.status == "RUNNING":
        target_conn.close()
        finish(
            envelope(
                ok=False, command="run", job_id=job_id,
                error="job is already RUNNING — use 'cancel' or wait",
                error_category="CONFIG",
            ),
            as_json,
        )
        return

    if job.status != "PLANNED" and not resume:
        target_conn.close()
        finish(
            envelope(
                ok=False, command="run", job_id=job_id,
                error=(
                    f"job status is {job.status}; pass --resume to re-run."
                ),
                error_category="CONFIG",
            ),
            as_json,
        )
        return

    if background:
        target_conn.close()
        # Hand off to a detached worker process. The worker is invoked via
        # the same module to keep the dependency surface identical.
        args = [
            sys.executable, "-m", "oracle_schema_refresh.oracdb_cli",
            "_worker", job_id, "--target", tgt_name,
        ]
        if ctx.obj.get("registry_path"):
            args = args[:3] + ["--registry", ctx.obj["registry_path"]] + args[3:]
        subprocess.Popen(  # noqa: S603 — args are constructed, not user-input
            args,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        finish(
            envelope(
                ok=True, command="run", job_id=job_id,
                data={"started": True, "background": True},
            ),
            as_json,
        )
        return

    try:
        ok, err = _execute_job(target_conn, reg, job)
    finally:
        target_conn.close()

    finish(
        envelope(
            ok=ok, command="run", job_id=job_id,
            error=err if not ok else None,
            error_category="DATA" if not ok else None,
        ),
        as_json,
    )


@oracdb.command("_worker", hidden=True)
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.pass_context
def cmd_worker(ctx: click.Context, job_id: str, tgt_name: str) -> None:
    """Internal: execute a job (no human output)."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    try:
        job = state.get_job(target_conn, job_id)
        if job is None:
            sys.exit(1)
        _execute_job(target_conn, reg, job)
    finally:
        target_conn.close()


@oracdb.command("status")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_status(
    ctx: click.Context, job_id: str, tgt_name: str, as_json: bool
) -> None:
    """Read job + per-table status from oracdb$jobs / oracdb$tables."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    try:
        job = state.get_job(target_conn, job_id)
        if job is None:
            finish(
                envelope(
                    ok=False, command="status", job_id=job_id,
                    error=f"unknown job {job_id!r}", error_category="CONFIG",
                ),
                as_json,
            )
            return
        records = state.get_table_records(target_conn, job_id)
    finally:
        target_conn.close()

    finish(
        envelope(
            ok=True, command="status", job_id=job_id,
            data={
                "status": job.status,
                "scn": job.scn,
                "source_endpoint": job.source_endpoint,
                "target_endpoint": job.target_endpoint,
                "source_schema": job.source_schema,
                "target_schema": job.target_schema,
                "error": job.error,
                "tables": [
                    {
                        "table": r.table_name,
                        "status": r.status,
                        "rows_source": r.rows_source,
                        "rows_target": r.rows_target,
                        "error": r.error,
                    }
                    for r in records
                ],
            },
        ),
        as_json,
    )


@oracdb.command("cancel")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_cancel(
    ctx: click.Context, job_id: str, tgt_name: str, as_json: bool
) -> None:
    """Mark a job CANCELLED. Does not interrupt a foreground worker."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    try:
        job = state.get_job(target_conn, job_id)
        if job is None:
            finish(
                envelope(
                    ok=False, command="cancel", job_id=job_id,
                    error=f"unknown job {job_id!r}", error_category="CONFIG",
                ),
                as_json,
            )
            return
        state.update_job_status(target_conn, job_id, "CANCELLED")
        target_conn.commit()
    finally:
        target_conn.close()

    finish(
        envelope(ok=True, command="cancel", job_id=job_id), as_json
    )


@oracdb.command("cleanup")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_cleanup(
    ctx: click.Context, job_id: str, tgt_name: str, as_json: bool
) -> None:
    """Drop job state. Refuses to wipe an active (RUNNING) job."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    try:
        job = state.get_job(target_conn, job_id)
        if job is None:
            finish(
                envelope(
                    ok=False, command="cleanup", job_id=job_id,
                    error=f"unknown job {job_id!r}", error_category="CONFIG",
                ),
                as_json,
            )
            return
        if job.status == "RUNNING":
            finish(
                envelope(
                    ok=False, command="cleanup", job_id=job_id,
                    error="refusing to cleanup a RUNNING job; cancel it first",
                    error_category="CONFIG",
                ),
                as_json,
            )
            return
        state.drop_job(target_conn, job_id)
        target_conn.commit()
    finally:
        target_conn.close()

    finish(envelope(ok=True, command="cleanup", job_id=job_id), as_json)


@oracdb.command("verify")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_verify(
    ctx: click.Context, job_id: str, tgt_name: str, as_json: bool
) -> None:
    """Re-compare source vs target row counts at the job's SCN."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    src_conn = None
    try:
        job = state.get_job(target_conn, job_id)
        if job is None:
            finish(
                envelope(
                    ok=False, command="verify", job_id=job_id,
                    error=f"unknown job {job_id!r}", error_category="CONFIG",
                ),
                as_json,
            )
            return
        src = reg.get(job.source_endpoint)
        src_conn = src.connect() if src.dsn != tgt.dsn else target_conn
        records = state.get_table_records(target_conn, job_id)
        report: list[dict[str, Any]] = []
        all_match = True
        for r in records:
            if r.status == "SKIPPED":
                report.append({"table": r.table_name, "skipped": True})
                continue
            rows_source = introspect.get_table_row_count(
                src_conn, job.source_schema, r.table_name, as_of_scn=job.scn
            )
            rows_target = introspect.get_table_row_count(
                target_conn, job.target_schema, r.table_name
            )
            match = rows_source == rows_target
            if not match:
                all_match = False
            report.append({
                "table": r.table_name,
                "rows_source": rows_source,
                "rows_target": rows_target,
                "match": match,
            })
    finally:
        if src_conn is not None and src_conn is not target_conn:
            src_conn.close()
        target_conn.close()

    finish(
        envelope(
            ok=all_match,
            command="verify",
            job_id=job_id,
            data={"all_match": all_match, "tables": report},
            error=None if all_match else "one or more tables mismatched",
            error_category=None if all_match else "DATA",
        ),
        as_json,
    )


@oracdb.command("wait")
@click.argument("job_id")
@click.option("--target", "tgt_name", required=True)
@click.option("--timeout", type=int, default=300, show_default=True,
              help="Seconds before giving up.")
@click.option("--poll-interval", type=float, default=2.0, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def cmd_wait(
    ctx: click.Context,
    job_id: str,
    tgt_name: str,
    timeout: int,
    poll_interval: float,
    as_json: bool,
) -> None:
    """Poll the job until it reaches a terminal status or timeout expires."""
    reg = _registry(ctx.obj)
    tgt = _resolve_endpoint(reg, tgt_name)
    target_conn = tgt.connect()
    deadline = time.monotonic() + timeout
    try:
        while True:
            job = state.get_job(target_conn, job_id)
            if job is None:
                finish(
                    envelope(
                        ok=False, command="wait", job_id=job_id,
                        error=f"unknown job {job_id!r}", error_category="CONFIG",
                    ),
                    as_json,
                )
                return
            if job.status in state.TERMINAL_STATUSES:
                finish(
                    envelope(
                        ok=(job.status == "DONE"),
                        command="wait",
                        job_id=job_id,
                        data={"status": job.status},
                        error=job.error if job.status != "DONE" else None,
                        error_category="DATA" if job.status != "DONE" else None,
                    ),
                    as_json,
                )
                return
            if time.monotonic() >= deadline:
                finish(
                    envelope(
                        ok=False, command="wait", job_id=job_id,
                        data={"status": job.status},
                        error=f"timed out after {timeout}s waiting for terminal",
                        error_category="TRANSIENT",
                    ),
                    as_json,
                )
                return
            time.sleep(poll_interval)
    finally:
        target_conn.close()


if __name__ == "__main__":
    # Allows ``python -m oracle_schema_refresh.oracdb_cli ...`` —
    # used by ``run --background`` to invoke the hidden ``_worker``.
    oracdb()
