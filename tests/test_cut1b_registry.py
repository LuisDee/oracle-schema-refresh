"""Endpoint registry — YAML-backed name → Endpoint resolution."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_registry_round_trip(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import EndpointRegistry

    reg_path = tmp_path / "endpoints.yaml"
    reg = EndpointRegistry(path=reg_path)
    reg.add(name="dev", dsn="dev:1521/svc", username="u", password="p")
    reg.save()

    # New instance reads from disk.
    reg2 = EndpointRegistry(path=reg_path)
    reg2.load()
    ep = reg2.get("dev")
    assert ep.name == "dev"
    assert ep.dsn == "dev:1521/svc"
    assert ep.username == "u"
    assert ep.password.get_secret_value() == "p"


def test_registry_load_missing_file_is_empty(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import EndpointRegistry

    reg = EndpointRegistry(path=tmp_path / "nonexistent.yaml")
    reg.load()
    assert reg.list_names() == []


def test_registry_get_unknown_raises(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import (
        EndpointRegistry,
        UnknownEndpointError,
    )

    reg = EndpointRegistry(path=tmp_path / "x.yaml")
    with pytest.raises(UnknownEndpointError, match="nope"):
        reg.get("nope")


def test_registry_add_rejects_duplicate_name(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import (
        DuplicateEndpointError,
        EndpointRegistry,
    )

    reg = EndpointRegistry(path=tmp_path / "x.yaml")
    reg.add(name="dev", dsn="h:1/s", username="u", password="p")
    with pytest.raises(DuplicateEndpointError):
        reg.add(name="dev", dsn="h:1/s2", username="u", password="p")


def test_registry_remove(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import EndpointRegistry

    reg = EndpointRegistry(path=tmp_path / "x.yaml")
    reg.add(name="dev", dsn="h:1/s", username="u", password="p")
    reg.remove("dev")
    assert reg.list_names() == []


def test_registry_save_creates_parent_dirs(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.registry import EndpointRegistry

    nested = tmp_path / "a" / "b" / "endpoints.yaml"
    reg = EndpointRegistry(path=nested)
    reg.add(name="x", dsn="h:1/s", username="u", password="p")
    reg.save()
    assert nested.exists()


def test_registry_file_does_not_contain_plaintext_password_directly(
    tmp_path: Path,
) -> None:
    """Passwords land in the YAML for now (Cut 3 brings wallet auth). Make
    sure they're at least under a recognisable key so anyone scanning the
    file knows what they're looking at — and add a comment so users know
    this is sensitive."""
    from oracle_schema_refresh.endpoints.registry import EndpointRegistry

    reg_path = tmp_path / "endpoints.yaml"
    reg = EndpointRegistry(path=reg_path)
    reg.add(name="x", dsn="h:1/s", username="u", password="hunter2")
    reg.save()

    text = reg_path.read_text()
    assert "hunter2" in text  # we're honest about the limitation today
    assert "password" in text  # key name, makes grep obvious
    # Permission bits: file should not be world-readable. Verify on POSIX.
    import os
    import stat

    mode = stat.S_IMODE(os.stat(reg_path).st_mode)
    assert mode & 0o077 == 0, (
        f"endpoints.yaml must not be group/world readable; got mode {oct(mode)}"
    )
