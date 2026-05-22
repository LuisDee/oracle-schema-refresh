"""``oracdb`` CLI — new agent-friendly entry point.

Coexists with the legacy ``schema-refresh`` CLI (``cli.py``). Cut 1b ships
two subcommand groups:

* ``endpoints`` — manage the named connection registry
  (``~/.oracdb/endpoints.yaml`` by default; override with
  ``ORACDB_REGISTRY`` or ``--registry``).
* ``copy`` — one-shot copy from ``--from NAME`` to ``--to NAME``.

``plan`` / ``run`` / ``status`` / ``verify`` land in Cut 1c. Wallet auth
lands in Cut 3.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from oracle_schema_refresh.config import RefreshConfig
from oracle_schema_refresh.endpoints.registry import (
    DuplicateEndpointError,
    EndpointRegistry,
    UnknownEndpointError,
)
from oracle_schema_refresh.engine import RefreshEngine


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
