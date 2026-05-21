"""Click CLI entry point for schema-refresh."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
import structlog
import yaml

from oracle_schema_refresh.config import OracleConnection, RefreshConfig
from oracle_schema_refresh.engine import RefreshEngine


def _configure_logging(verbose: bool) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            10 if verbose else 20  # DEBUG=10, INFO=20
        ),
    )


def _load_refresh_config(
    config_file: str | None,
    source_schema: str | None,
    target_schema: str | None,
    tables: tuple[str, ...],
    no_auto_fk: bool,
    recreate: bool,
    commit_mode: str = "per_table",
) -> RefreshConfig:
    """Build a RefreshConfig from either a YAML file or inline CLI args."""
    if config_file:
        raw: dict[str, Any] = yaml.safe_load(Path(config_file).read_text())
        # CLI flags override YAML values when explicitly provided
        if source_schema:
            raw["source_schema"] = source_schema
        if target_schema:
            raw["target_schema"] = target_schema
        if tables:
            raw["tables"] = list(tables)
        if no_auto_fk:
            raw["auto_include_fk_parents"] = False
        if recreate:
            raw["recreate_tables"] = True
        return RefreshConfig(**raw)

    # Inline mode — all required fields must be supplied
    if not source_schema or not target_schema or not tables:
        raise click.UsageError(
            "Provide --config FILE or all of --source-schema, --target-schema, --tables"
        )
    return RefreshConfig(
        source_schema=source_schema,
        target_schema=target_schema,
        tables=list(tables),
        auto_include_fk_parents=not no_auto_fk,
        recreate_tables=recreate,
        commit_mode=commit_mode,  # type: ignore[arg-type]
    )


@click.command()
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True),
    default=None,
    help="Path to YAML refresh config (see config.example.yaml).",
)
@click.option("--source-schema", default=None, help="Source schema name (e.g. BACKOFFICE).")
@click.option("--target-schema", default=None, help="Target schema name (e.g. LDEBURNA).")
@click.option(
    "--tables",
    multiple=True,
    help="Table(s) to refresh (repeatable: --tables T1 --tables T2).",
)
@click.option(
    "--no-auto-fk",
    is_flag=True,
    default=False,
    help="Disable automatic FK parent inclusion.",
)
@click.option(
    "--recreate",
    is_flag=True,
    default=False,
    help="DROP + re-CREATE target tables instead of TRUNCATE.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log planned actions without executing any DDL or DML.",
)
@click.option(
    "--json-output",
    is_flag=True,
    default=False,
    help="Print RefreshResult summary as JSON to stdout.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable DEBUG logging.")
def cli(
    config_file: str | None,
    source_schema: str | None,
    target_schema: str | None,
    tables: tuple[str, ...],
    no_auto_fk: bool,
    recreate: bool,
    dry_run: bool,
    json_output: bool,
    verbose: bool,
) -> None:
    """Idempotent Oracle schema refresh — copy tables from SOURCE to TARGET schema.

    Handles FK dependency ordering, index creation, and constraint disable/re-enable
    automatically. Safe to run daily; TRUNCATE + INSERT on each run by default.

    \b
    Examples:
      schema-refresh --config refresh.yaml
      schema-refresh --config refresh.yaml --dry-run -v
      schema-refresh --source-schema BACKOFFICE --target-schema LDEBURNA --tables SUN_LEDGER
      schema-refresh --config refresh.yaml --recreate --json-output
    """
    _configure_logging(verbose)

    try:
        refresh_cfg = _load_refresh_config(
            config_file, source_schema, target_schema, tables, no_auto_fk, recreate
        )
    except Exception as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        oracle_conn = OracleConnection()
    except Exception as exc:
        click.echo(
            f"Oracle credentials not found. Set ORACLE_USERNAME, ORACLE_PASSWORD, "
            f"ORACLE_DSN or create a .env file.\nError: {exc}",
            err=True,
        )
        sys.exit(1)

    engine = RefreshEngine(oracle_conn, refresh_cfg)

    try:
        result = engine.run(dry_run=dry_run)
    except Exception as exc:
        click.echo(f"Refresh failed: {exc}", err=True)
        sys.exit(1)

    if json_output:
        click.echo(json.dumps(result.summary(), indent=2))
    else:
        status = "DRY RUN" if dry_run else ("OK" if result.success else "FAILED")
        click.echo(f"[{status}] {len(result.table_results)} table(s) processed in "
                   f"{result.total_duration_seconds:.1f}s")
        for tr in result.table_results:
            icon = "+" if tr.status == "ok" else ("~" if tr.status == "skipped" else "!")
            click.echo(f"  {icon} {tr.table_name}: {tr.status} ({tr.rows_loaded} rows)")

    if not result.success:
        sys.exit(1)
