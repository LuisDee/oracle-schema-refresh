"""``oracdb`` CLI surface — endpoints and copy subcommands."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner_env(tmp_path: Path) -> dict[str, str]:
    """Point the CLI at an isolated registry under tmp_path."""
    return {"ORACDB_REGISTRY": str(tmp_path / "endpoints.yaml")}


# ---------------------------------------------------------------------------
# endpoints add / list / remove / test
# ---------------------------------------------------------------------------


def test_oracdb_endpoints_add_persists_to_registry(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    result = CliRunner().invoke(
        oracdb,
        [
            "endpoints", "add", "dev",
            "--dsn", "h:1521/svc",
            "--user", "u",
            "--password", "p",
        ],
        env=_runner_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    reg_file = tmp_path / "endpoints.yaml"
    assert reg_file.exists()
    assert "dev" in reg_file.read_text()


def test_oracdb_endpoints_list_json(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "dev", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )
    runner.invoke(
        oracdb,
        ["endpoints", "add", "prod", "--dsn", "h:1/s2", "--user", "u", "--password", "p"],
        env=env,
    )

    result = runner.invoke(oracdb, ["endpoints", "list", "--json"], env=env)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert sorted(parsed["endpoints"]) == ["dev", "prod"]


def test_oracdb_endpoints_list_does_not_leak_passwords(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "dev", "--dsn", "h:1/s", "--user", "u",
         "--password", "ultrasecret"],
        env=env,
    )

    result = runner.invoke(oracdb, ["endpoints", "list"], env=env)
    assert "ultrasecret" not in result.output


def test_oracdb_endpoints_remove(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "dev", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )
    result = runner.invoke(oracdb, ["endpoints", "remove", "dev"], env=env)
    assert result.exit_code == 0
    list_result = runner.invoke(oracdb, ["endpoints", "list", "--json"], env=env)
    assert json.loads(list_result.output)["endpoints"] == []


def test_oracdb_endpoints_test_calls_connect(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "dev", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    mock_conn = MagicMock()
    with patch.object(Endpoint, "connect", return_value=mock_conn) as mock_connect:
        result = runner.invoke(oracdb, ["endpoints", "test", "dev"], env=env)

    assert result.exit_code == 0, result.output
    mock_connect.assert_called_once()
    mock_conn.close.assert_called_once()


def test_oracdb_endpoints_test_failure_exits_nonzero(tmp_path: Path) -> None:
    from oracle_schema_refresh.endpoints.endpoint import Endpoint
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "dev", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    with patch.object(Endpoint, "connect", side_effect=RuntimeError("no route")):
        result = runner.invoke(oracdb, ["endpoints", "test", "dev"], env=env)

    assert result.exit_code != 0
    assert "no route" in result.output


# ---------------------------------------------------------------------------
# copy — resolves endpoints, calls engine
# ---------------------------------------------------------------------------


def test_oracdb_copy_invokes_engine_with_resolved_endpoints(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "src", "--dsn", "src:1/s", "--user", "u", "--password", "p"],
        env=env,
    )
    runner.invoke(
        oracdb,
        ["endpoints", "add", "tgt", "--dsn", "tgt:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.summary.return_value = {"success": True, "results": [], "total_seconds": 0.1}
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch(
        "oracle_schema_refresh.oracdb_cli.RefreshEngine.from_endpoints",
        return_value=mock_engine,
    ) as mock_from:
        result = runner.invoke(
            oracdb,
            [
                "copy",
                "--from", "src",
                "--to", "tgt",
                "--source-schema", "SRC",
                "--target-schema", "TGT",
                "--tables", "T1,T2",
                "--dblink", "session",
                "--json",
            ],
            env=env,
        )

    assert result.exit_code == 0, result.output
    mock_from.assert_called_once()
    args, kwargs = mock_from.call_args
    src_ep, tgt_ep, cfg = args
    assert src_ep.name == "src"
    assert tgt_ep.name == "tgt"
    assert cfg.tables == ["T1", "T2"]
    assert cfg.dblink == "session"


def test_oracdb_copy_unknown_endpoint_errors_cleanly(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    result = runner.invoke(
        oracdb,
        [
            "copy",
            "--from", "nope",
            "--to", "nope2",
            "--source-schema", "S",
            "--target-schema", "T",
            "--tables", "T1",
        ],
        env=env,
    )
    assert result.exit_code != 0
    assert "nope" in result.output.lower()


def test_oracdb_copy_cross_host_without_dblink_errors(tmp_path: Path) -> None:
    """Refuse cross-host calls that didn't specify --dblink."""
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "src", "--dsn", "src:1/s", "--user", "u", "--password", "p"],
        env=env,
    )
    runner.invoke(
        oracdb,
        ["endpoints", "add", "tgt", "--dsn", "tgt:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    result = runner.invoke(
        oracdb,
        [
            "copy",
            "--from", "src",
            "--to", "tgt",
            "--source-schema", "S",
            "--target-schema", "T",
            "--tables", "T1",
            # no --dblink
        ],
        env=env,
    )
    assert result.exit_code != 0
    assert "dblink" in result.output.lower()


def test_oracdb_copy_same_endpoint_without_dblink_ok(tmp_path: Path) -> None:
    """Same source and target endpoint → no dblink needed."""
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.summary.return_value = {"success": True, "results": [], "total_seconds": 0.0}
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch(
        "oracle_schema_refresh.oracdb_cli.RefreshEngine.from_endpoints",
        return_value=mock_engine,
    ):
        result = runner.invoke(
            oracdb,
            [
                "copy",
                "--from", "local",
                "--to", "local",
                "--source-schema", "S",
                "--target-schema", "T",
                "--tables", "T1",
                "--json",
            ],
            env=env,
        )

    assert result.exit_code == 0, result.output
