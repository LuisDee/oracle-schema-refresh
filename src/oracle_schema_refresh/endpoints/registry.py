"""YAML-backed registry of named ``Endpoint`` records.

Agents and humans address Oracle targets by short name (``dev_uk01``,
``prod_us02``) rather than by full DSN+credentials. The registry lives
at ``~/.oracdb/endpoints.yaml`` by default; subcommand handlers can
override the path for testing.

**Cut 1b limitation**: passwords are stored in plaintext under the
``password`` key, with file permissions tightened to ``0o600``. Wallet
authentication lands in Cut 3 and is what we'll use in production.

File format:

.. code-block:: yaml

    # ~/.oracdb/endpoints.yaml — sensitive: contains passwords.
    endpoints:
      dev_uk01:
        dsn: uk01vdb007:1521/dev
        username: ldeburna
        password: hunter2
      prod_us02:
        dsn: us02vdb003:1521/prod
        username: backoffice
        password: ...
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import SecretStr

from oracle_schema_refresh.endpoints.endpoint import Endpoint

DEFAULT_REGISTRY_PATH = Path.home() / ".oracdb" / "endpoints.yaml"

_FILE_HEADER = (
    "# ~/.oracdb/endpoints.yaml — sensitive: contains passwords.\n"
    "# Cut 1b: plaintext only. Wallet auth lands in Cut 3.\n"
    "# File permissions: 0600.\n"
)


class UnknownEndpointError(KeyError):
    """Raised when looking up a name that isn't in the registry."""


class DuplicateEndpointError(ValueError):
    """Raised when adding a name that's already in the registry."""


class EndpointRegistry:
    """In-memory cache of named endpoints, persisted to YAML on demand."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else DEFAULT_REGISTRY_PATH
        self._endpoints: dict[str, Endpoint] = {}

    def load(self) -> None:
        """Read from disk. Missing file → empty registry, no error."""
        if not self.path.exists():
            self._endpoints = {}
            return
        raw = yaml.safe_load(self.path.read_text()) or {}
        endpoints = raw.get("endpoints") or {}
        self._endpoints = {
            name: Endpoint(
                name=name,
                dsn=data["dsn"],
                username=data["username"],
                password=SecretStr(data["password"]),
                default_schema=data.get("default_schema"),
            )
            for name, data in endpoints.items()
        }

    def save(self) -> None:
        """Write to disk. Creates parent dirs; sets file mode to ``0o600``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "endpoints": {
                name: {
                    "dsn": ep.dsn,
                    "username": ep.username,
                    "password": ep.password.get_secret_value(),
                    **(
                        {"default_schema": ep.default_schema}
                        if ep.default_schema
                        else {}
                    ),
                }
                for name, ep in self._endpoints.items()
            }
        }
        self.path.write_text(_FILE_HEADER + yaml.safe_dump(payload, sort_keys=True))
        os.chmod(self.path, 0o600)

    def add(
        self,
        name: str,
        dsn: str,
        username: str,
        password: str,
        default_schema: str | None = None,
    ) -> None:
        if name in self._endpoints:
            raise DuplicateEndpointError(
                f"endpoint {name!r} already exists; use ``remove`` first"
            )
        self._endpoints[name] = Endpoint(
            name=name,
            dsn=dsn,
            username=username,
            password=SecretStr(password),
            default_schema=default_schema,
        )

    def remove(self, name: str) -> None:
        if name not in self._endpoints:
            raise UnknownEndpointError(name)
        del self._endpoints[name]

    def get(self, name: str) -> Endpoint:
        if name not in self._endpoints:
            raise UnknownEndpointError(name)
        return self._endpoints[name]

    def list_names(self) -> list[str]:
        return sorted(self._endpoints)
