"""JSON envelope and exit-code helpers shared across ``oracdb`` subcommands.

See ``docs/redesign.md`` §3.2 (envelope) and §3.3 (exit codes).
"""
from __future__ import annotations

import json
import sys
from enum import IntEnum
from typing import Any

import click


class ExitCode(IntEnum):
    OK = 0
    USER_ERROR = 1
    TRANSIENT = 2
    DATA_ERROR = 3
    INTERNAL = 4


# Error categories emitted in the JSON envelope. Map cleanly onto exit codes.
ERROR_CATEGORIES = frozenset(
    {"CONFIG", "AUTH", "TRANSIENT", "DATA", "INTERNAL"}
)


def envelope(
    *,
    ok: bool,
    command: str,
    data: dict[str, Any] | None = None,
    error: str | None = None,
    error_category: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical JSON envelope. Pass to ``click.echo(json.dumps(env))``."""
    if error_category is not None:
        assert error_category in ERROR_CATEGORIES, (
            f"unknown error_category {error_category!r}; "
            f"expected one of {sorted(ERROR_CATEGORIES)}"
        )
    return {
        "ok": ok,
        "command": command,
        "job_id": job_id,
        "data": data,
        "error": error,
        "error_category": error_category,
    }


def emit(env: dict[str, Any], as_json: bool) -> None:
    """Write the envelope (JSON) or a human-readable summary."""
    if as_json:
        click.echo(json.dumps(env))
        return
    cmd = env["command"]
    if env["ok"]:
        click.echo(f"[OK] {cmd}")
        if env.get("job_id"):
            click.echo(f"  job_id: {env['job_id']}")
        data = env.get("data") or {}
        for k, v in data.items():
            click.echo(f"  {k}: {v}")
    else:
        click.echo(f"[FAILED] {cmd}: {env.get('error') or 'unknown error'}", err=True)


def exit_for_category(error_category: str | None) -> int:
    """Map an envelope error_category to a CLI exit code."""
    if error_category is None:
        return ExitCode.OK
    return {
        "CONFIG": ExitCode.USER_ERROR,
        "AUTH": ExitCode.USER_ERROR,
        "TRANSIENT": ExitCode.TRANSIENT,
        "DATA": ExitCode.DATA_ERROR,
        "INTERNAL": ExitCode.INTERNAL,
    }.get(error_category, ExitCode.INTERNAL)


def finish(env: dict[str, Any], as_json: bool) -> None:
    """Emit the envelope, then ``sys.exit`` with the matching code."""
    emit(env, as_json)
    if env["ok"]:
        sys.exit(ExitCode.OK)
    sys.exit(exit_for_category(env.get("error_category")))
