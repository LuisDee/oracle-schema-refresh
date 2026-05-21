"""Configuration models for OracleSchemaRefresh."""
from __future__ import annotations

from typing import Literal

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
    """Configuration for a single schema refresh run."""

    source_schema: str
    target_schema: str
    tables: list[str]
    auto_include_fk_parents: bool = True
    recreate_tables: bool = False
    commit_mode: Literal["per_table", "all_or_nothing"] = "per_table"
    insert_hint: str = "/*+ APPEND */"

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
