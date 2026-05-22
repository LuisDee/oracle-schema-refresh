"""DB-link lifecycle for cross-host copies.

Cut 1b supports two modes (see ``docs/redesign.md`` §2.6):

* ``existing:NAME`` — assume the named DB link already exists on the
  target. The CLI never creates or drops it. This is the production
  default: DBA pre-provisions the link, agents just name it.
* ``session`` — create a private DB link on the target session at job
  start, drop it at job end. Requires ``CREATE DATABASE LINK`` privilege
  on the target user. The link name is unique per session so concurrent
  jobs don't collide.

Wallet-authenticated link creation is a Cut 3 task; today the session
mode embeds the source username/password in the DDL.
"""
from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import oracledb
import structlog

from oracle_schema_refresh.endpoints.endpoint import Endpoint

log = structlog.get_logger()


def parse_dblink_spec(spec: str) -> tuple[str, str | None]:
    """Split a ``RefreshConfig.dblink`` value into ``(mode, name)``.

    Returns:
        ``("existing", "<name>")`` or ``("session", None)``.

    Raises:
        ValueError: when the spec doesn't match either form.
    """
    if spec == "session":
        return ("session", None)
    if spec.startswith("existing:"):
        name = spec[len("existing:") :]
        if not name:
            raise ValueError("existing: requires a DB link name after the colon")
        return ("existing", name)
    raise ValueError(
        f"unrecognised dblink spec {spec!r} — expected 'session' or 'existing:NAME'"
    )


def _new_session_link_name() -> str:
    """Generate a unique-per-session DB link name.

    Format: ``ORACDB_<12 hex chars>``. Deterministically tagged with the
    ``ORACDB_`` prefix so an operator can identify and clean up leaked
    links if a job is killed mid-flight.
    """
    return f"ORACDB_{secrets.token_hex(6).upper()}"


@contextmanager
def session_dblink(target_conn: Any, source: Endpoint) -> Iterator[str]:
    """Create a private DB link on ``target_conn`` pointing at ``source``;
    drop it on exit.

    Yields the link name so the caller can write
    ``... FROM tbl@<name>``.

    The DROP runs in a ``finally`` and is best-effort — a drop failure is
    logged but does not mask a body exception. If the body completes
    cleanly but the drop fails, the exception is also swallowed (with a
    warning) because at that point the run succeeded and we can't undo
    the work; an operator can ``DROP DATABASE LINK ORACDB_*`` later.
    """
    name = _new_session_link_name()
    create_sql = (
        f"CREATE DATABASE LINK {name} "
        f"CONNECT TO {source.username} "
        f"IDENTIFIED BY \"{source.password.get_secret_value()}\" "
        f"USING '{source.dsn}'"
    )
    with target_conn.cursor() as cur:
        cur.execute(create_sql)
    log.info("dblink_created", name=name, mode="session", source=source.name)

    try:
        yield name
    finally:
        try:
            with target_conn.cursor() as cur:
                cur.execute(f"DROP DATABASE LINK {name}")
            log.info("dblink_dropped", name=name)
        except oracledb.DatabaseError as exc:
            log.warning("dblink_drop_failed", name=name, error=str(exc))


@contextmanager
def dblink_for(
    target_conn: Any, source: Endpoint, spec: str | None
) -> Iterator[str]:
    """Resolve a ``RefreshConfig.dblink`` spec into a usable link name.

    For ``existing:NAME`` the name is yielded as-is — no DDL runs.
    For ``session`` a private link is created and dropped on exit.

    ``spec is None`` is a programming error: callers must check before
    invoking this for the cross-host path.
    """
    if spec is None:
        raise ValueError("dblink spec is required for the cross-host path")
    mode, name = parse_dblink_spec(spec)
    if mode == "existing":
        assert name is not None  # parse_dblink_spec guarantees
        yield name
    elif mode == "session":
        with session_dblink(target_conn, source) as session_name:
            yield session_name
    else:  # pragma: no cover — parse_dblink_spec rejects others
        raise ValueError(f"unsupported dblink mode {mode!r}")
