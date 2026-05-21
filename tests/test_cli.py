"""Tests for cli.py — Click argument parsing and integration with engine."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner


def test_cli_help_exits_zero() -> None:
    from oracle_schema_refresh.cli import cli

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "refresh" in result.output.lower() or "schema" in result.output.lower()


def test_cli_no_args_exits_nonzero() -> None:
    from oracle_schema_refresh.cli import cli

    result = CliRunner().invoke(cli, [])
    assert result.exit_code != 0


def test_cli_config_file_parsed(tmp_path: Path) -> None:
    cfg = {
        "source_schema": "SRC",
        "target_schema": "TGT",
        "tables": ["T1"],
    }
    cfg_file = tmp_path / "refresh.yaml"
    cfg_file.write_text(yaml.dump(cfg))

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.total_duration_seconds = 0.1
    mock_result.table_results = []
    mock_result.summary.return_value = {"success": True, "results": [], "total_seconds": 0.1}
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch("oracle_schema_refresh.cli.RefreshEngine", return_value=mock_engine):
        with patch.dict(
            "os.environ",
            {"ORACLE_USERNAME": "u", "ORACLE_PASSWORD": "p", "ORACLE_DSN": "h:1/s"},
        ):
            from oracle_schema_refresh.cli import cli

            result = CliRunner().invoke(cli, ["--config", str(cfg_file)])

    assert result.exit_code == 0, result.output
    mock_engine.run.assert_called_once_with(dry_run=False)


def test_cli_dry_run_flag_forwarded(tmp_path: Path) -> None:
    cfg = {"source_schema": "SRC", "target_schema": "TGT", "tables": ["T1"]}
    cfg_file = tmp_path / "refresh.yaml"
    cfg_file.write_text(yaml.dump(cfg))

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.summary.return_value = {"success": True, "results": [], "total_seconds": 0.0}
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch("oracle_schema_refresh.cli.RefreshEngine", return_value=mock_engine):
        with patch.dict(
            "os.environ",
            {"ORACLE_USERNAME": "u", "ORACLE_PASSWORD": "p", "ORACLE_DSN": "h:1/s"},
        ):
            from oracle_schema_refresh.cli import cli

            CliRunner().invoke(cli, ["--config", str(cfg_file), "--dry-run"])

    mock_engine.run.assert_called_once_with(dry_run=True)


def test_cli_json_output_prints_summary(tmp_path: Path) -> None:
    import json

    cfg = {"source_schema": "SRC", "target_schema": "TGT", "tables": ["T1"]}
    cfg_file = tmp_path / "refresh.yaml"
    cfg_file.write_text(yaml.dump(cfg))

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.summary.return_value = {
        "success": True,
        "results": [{"table": "T1", "status": "ok", "rows": 5, "error": None}],
        "total_seconds": 1.2,
    }
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch("oracle_schema_refresh.cli.RefreshEngine", return_value=mock_engine):
        with patch.dict(
            "os.environ",
            {"ORACLE_USERNAME": "u", "ORACLE_PASSWORD": "p", "ORACLE_DSN": "h:1/s"},
        ):
            from oracle_schema_refresh.cli import cli

            result = CliRunner().invoke(
                cli, ["--config", str(cfg_file), "--json-output"]
            )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["success"] is True


def test_cli_inline_args_no_config(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.total_duration_seconds = 0.0
    mock_result.table_results = []
    mock_result.summary.return_value = {"success": True, "results": [], "total_seconds": 0.0}
    mock_engine = MagicMock()
    mock_engine.run.return_value = mock_result

    with patch("oracle_schema_refresh.cli.RefreshEngine", return_value=mock_engine):
        with patch.dict(
            "os.environ",
            {"ORACLE_USERNAME": "u", "ORACLE_PASSWORD": "p", "ORACLE_DSN": "h:1/s"},
        ):
            from oracle_schema_refresh.cli import cli

            result = CliRunner().invoke(
                cli,
                [
                    "--source-schema", "SRC",
                    "--target-schema", "TGT",
                    "--tables", "T1",
                ],
            )

    assert result.exit_code == 0, result.output
    mock_engine.run.assert_called_once()
