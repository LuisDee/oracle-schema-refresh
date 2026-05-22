"""Integration test harness.

Strategy: try three sources for a real Oracle, in order of preference:

1. ``$ORACDB_TEST_DSN`` (with ``$ORACDB_TEST_USER`` + ``$ORACDB_TEST_PASSWORD``).
   Point this at an existing dev/CI Oracle and integration tests skip
   container startup entirely — useful when the dev already has an
   Oracle they trust.
2. ``testcontainers`` + ``gvenzl/oracle-free:23-slim`` if Docker is
   reachable. Session-scoped — the container is started once for the
   whole suite and torn down on exit.
3. Skip all integration tests with an explanatory reason if neither is
   available.

Mark integration tests with ``@pytest.mark.integration``. Unit-only
runs (``pytest -q``) skip the whole directory by default; opt in with
``pytest -m integration``.
"""
from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest

# ----- 1. Detect what's available --------------------------------------------


def _env_dsn() -> tuple[str, str, str] | None:
    """Read ``$ORACDB_TEST_DSN`` + credentials. Expects a user that can
    CREATE USER / GRANT (e.g. SYSTEM on a PDB)."""
    dsn = os.environ.get("ORACDB_TEST_DSN")
    if not dsn:
        return None
    user = os.environ.get("ORACDB_TEST_USER", "system")
    password = os.environ.get("ORACDB_TEST_PASSWORD", "oracle")
    return user, password, dsn


def _docker_available() -> bool:
    """``True`` if the Docker daemon is reachable on the default socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect("/var/run/docker.sock")
        sock.close()
        return True
    except OSError:
        return False


def _testcontainers_available() -> bool:
    try:
        import testcontainers  # noqa: F401
        return True
    except ImportError:
        return False


def _skip_reason() -> str | None:
    """Return a reason string when integration tests can't run, else None."""
    if _env_dsn() is not None:
        return None
    if not _docker_available():
        return (
            "Integration tests need either $ORACDB_TEST_DSN pointing at an "
            "existing Oracle or a reachable Docker daemon."
        )
    if not _testcontainers_available():
        return (
            "Docker is reachable but `testcontainers` is not installed. "
            "Run `pip install 'oracle-schema-refresh[integration]'` or "
            "`pip install testcontainers`."
        )
    return None


# ----- 2. pytest plumbing ----------------------------------------------------


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every integration test if the prerequisites aren't met."""
    reason = _skip_reason()
    if reason is None:
        return
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


# ----- 3. Container fixture --------------------------------------------------


@pytest.fixture(scope="session")
def oracle_dsn() -> Iterator[tuple[str, str, str]]:
    """Yield ``(user, password, dsn)`` for the session-scoped Oracle.

    Sources, in order:
      a) ``$ORACDB_TEST_DSN`` if set.
      b) A ``gvenzl/oracle-free:23-slim`` container.
    """
    env = _env_dsn()
    if env is not None:
        yield env
        return

    # Container path — import lazily so non-integration runs don't need
    # testcontainers installed at all.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    # SYSTEM on FREEPDB1 has CREATE USER + GRANT, which the fresh-schemas
    # fixture needs. ORACLE_PASSWORD is the SYS / SYSTEM password.
    container = (
        DockerContainer("gvenzl/oracle-free:23-slim")
        .with_env("ORACLE_PASSWORD", "oracle")
        .with_exposed_ports(1521)
    )
    container.start()
    try:
        wait_for_logs(container, "DATABASE IS READY TO USE", timeout=240)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(1521)
        dsn = f"{host}:{port}/FREEPDB1"
        yield ("system", "oracle", dsn)
    finally:
        container.stop()


@pytest.fixture
def oracle_conn(oracle_dsn: tuple[str, str, str]) -> Iterator[object]:
    """Open a fresh ``oracledb`` connection per test. Auto-commit off."""
    import oracledb

    user, password, dsn = oracle_dsn
    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def oracle_endpoint(oracle_dsn: tuple[str, str, str]) -> object:
    """An :class:`Endpoint` pointing at the test Oracle."""
    from pydantic import SecretStr

    from oracle_schema_refresh.endpoints import Endpoint

    user, password, dsn = oracle_dsn
    return Endpoint(
        name="test", dsn=dsn, username=user, password=SecretStr(password)
    )


# ----- 4. Schema sandbox -----------------------------------------------------


@pytest.fixture
def fresh_schemas(oracle_conn: object) -> Iterator[tuple[str, str]]:
    """Create a unique source/target schema pair and tear them down.

    Requires the connection in ``oracle_conn`` to have ``CREATE USER``,
    ``DROP USER``, and ``GRANT`` privileges (SYSTEM on a PDB does).
    Names like ``ORACDB_SRC_<8 hex>`` keep them out of every operator's
    way.
    """
    import secrets

    suffix = secrets.token_hex(4).upper()
    src = f"ORACDB_SRC_{suffix}"
    tgt = f"ORACDB_TGT_{suffix}"
    cur = oracle_conn.cursor()  # type: ignore[attr-defined]
    try:
        for schema in (src, tgt):
            cur.execute(
                f'CREATE USER "{schema}" IDENTIFIED BY oracle '
                f"DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS"
            )
            cur.execute(f'GRANT CREATE SESSION, RESOURCE TO "{schema}"')
        yield (src, tgt)
    finally:
        for schema in (src, tgt):
            try:
                cur.execute(f'DROP USER "{schema}" CASCADE')
            except Exception:  # noqa: BLE001 — best-effort
                pass
        cur.close()
