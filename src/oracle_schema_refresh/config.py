"""Configuration models for OracleSchemaRefresh."""
from __future__ import annotations

import warnings
from typing import Any, Literal

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OracleConnection(BaseSettings):
    """Oracle connection loaded from ORACLE_* environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_prefix="ORACLE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    username: str
    password: SecretStr
    dsn: str  # host:port/service_name  e.g. uk01vdb007:1521/dev


class RefreshConfig(BaseModel):
    """Configuration for a single schema refresh run.

    ``commit_mode``:
        - ``per_table`` (default): commit after each table loads. Safe for
          long runs — a mid-run failure leaves earlier tables visible.
        - ``defer_insert_commits``: hold all INSERTs in one transaction
          and commit once at the end. **Note**: TRUNCATE auto-commits as
          DDL regardless of this setting, so the truncation of every
          requested table happens up front in either case.
        - ``all_or_nothing``: deprecated alias for ``defer_insert_commits``,
          kept for backward compatibility. Will be removed in a future cut.

    ``call_timeout_seconds``:
        If non-zero, sets ``conn.call_timeout`` (in milliseconds under the
        hood) so a runaway INSERT can't hang the CLI indefinitely. Default
        ``0`` means no timeout — current behaviour.
    """

    source_schema: str
    target_schema: str
    tables: list[str]
    auto_include_fk_parents: bool = True
    recreate_tables: bool = False
    commit_mode: Literal["per_table", "defer_insert_commits", "all_or_nothing"] = (
        "per_table"
    )
    insert_hint: str = "/*+ APPEND */"
    call_timeout_seconds: int = 0
    dblink: str | None = None
    """Cross-host source link. ``"existing:NAME"`` to reuse a DBA-provisioned
    DB link; ``"session"`` to create a private link for the job's lifetime.
    ``None`` is intra-instance — no dblink, source reads go through the same
    connection as the target."""

    @field_validator("tables")
    @classmethod
    def at_least_one_table(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tables must contain at least one entry")
        return [t.upper() for t in v]

    @field_validator("source_schema", "target_schema")
    @classmethod
    def uppercase_schema(cls, v: str) -> str:
        return v.upper()

    @field_validator("commit_mode", mode="after")
    @classmethod
    def normalize_commit_mode(cls, v: Any) -> str:
        if v == "all_or_nothing":
            warnings.warn(
                "commit_mode='all_or_nothing' is misleading because TRUNCATE "
                "auto-commits as DDL. Use 'defer_insert_commits' instead — "
                "kept as a deprecated alias for now.",
                DeprecationWarning,
                stacklevel=2,
            )
            return "defer_insert_commits"
        return v

    @field_validator("call_timeout_seconds")
    @classmethod
    def call_timeout_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("call_timeout_seconds must be >= 0")
        return v

    @field_validator("dblink", mode="after")
    @classmethod
    def validate_dblink(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v == "session":
            return v
        if v.startswith("existing:"):
            name = v[len("existing:") :]
            if not name:
                raise ValueError(
                    "dblink='existing:' requires a DB link name after the colon"
                )
            return v
        raise ValueError(
            "dblink must be 'session' or 'existing:NAME' "
            f"(got {v!r})"
        )
