"""``Endpoint`` — a named, reusable Oracle connection target.

Cut 0 introduces this abstraction without changing behaviour: the legacy
``OracleConnection`` is wrapped into a single default endpoint that serves as
both source and target. Subsequent cuts split source/target onto distinct
endpoints and add wallet authentication, dblink lifecycle, and registry I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import oracledb
from pydantic import SecretStr

from oracle_schema_refresh.config import OracleConnection


@dataclass(frozen=True)
class Endpoint:
    """A named Oracle connection target.

    Today this carries password auth only. Wallet and external auth modes
    land in Cut 1b alongside the new ``oracdb endpoints`` CLI.

    Attributes:
        name: Human-readable identifier (e.g. ``"default"``, ``"dev_uk01"``).
        dsn: Oracle Easy Connect string, e.g. ``"host:1521/service"``.
        username: DB account.
        password: DB password, held as ``SecretStr`` so ``repr`` won't leak.
        default_schema: Optional schema to default to when a config omits
            ``source_schema`` or ``target_schema``. Not consumed in Cut 0.
    """

    name: str
    dsn: str
    username: str
    password: SecretStr
    default_schema: str | None = None

    @classmethod
    def from_oracle_connection(
        cls, oc: OracleConnection, name: str = "default"
    ) -> Endpoint:
        """Wrap a legacy ``OracleConnection`` into an ``Endpoint``."""
        return cls(
            name=name,
            dsn=oc.dsn,
            username=oc.username,
            password=oc.password,
        )

    def connect(self, **kwargs: Any) -> Any:
        """Open an ``oracledb`` connection to this endpoint.

        Extra keyword arguments are forwarded to :func:`oracledb.connect`.
        Defaults to a 10-second TCP connect timeout to keep the legacy
        behaviour from ``engine.py``.
        """
        kwargs.setdefault("tcp_connect_timeout", 10)
        return oracledb.connect(
            user=self.username,
            password=self.password.get_secret_value(),
            dsn=self.dsn,
            **kwargs,
        )
