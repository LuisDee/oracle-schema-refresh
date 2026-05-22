"""``oracdb`` phased subcommands: plan / run / status / verify / cancel /
cleanup / wait. Plus ``run --background`` and ``--resume``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def _runner_env(tmp_path: Path) -> dict[str, str]:
    return {"ORACDB_REGISTRY": str(tmp_path / "endpoints.yaml")}


def _add_endpoints(runner: CliRunner, env: dict[str, str]) -> None:
    """Register a 'local' endpoint we'll use as both source and target."""
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_oracdb_plan_writes_state_and_returns_job_id(tmp_path: Path) -> None:
    from oracle_schema_refresh.engine import Plan
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    fake_plan = Plan(
        scn=12345,
        server_version=19,
        tables_requested=["T1"],
        tables_resolved=["T1"],
        table_order=["T1"],
        cross_host=False,
    )

    state_calls: list[tuple[str, tuple, dict]] = []

    def record(name: str):
        def _fn(*a, **kw):
            state_calls.append((name, a, kw))
            return None
        return _fn

    with patch(
        "oracle_schema_refresh.oracdb_cli.RefreshEngine.plan",
        return_value=fake_plan,
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.ensure_schema",
            side_effect=record("ensure_schema"),
        ):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.insert_job",
                side_effect=record("insert_job"),
            ):
                with patch(
                    "oracle_schema_refresh.oracdb_cli.state.insert_table_records",
                    side_effect=record("insert_table_records"),
                ):
                    with patch(
                        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
                        return_value=MagicMock(),
                    ):
                        result = runner.invoke(
                            oracdb,
                            [
                                "plan",
                                "--from", "local",
                                "--to", "local",
                                "--source-schema", "SRC",
                                "--target-schema", "TGT",
                                "--tables", "T1",
                                "--json",
                            ],
                            env=env,
                        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "plan"
    assert payload["job_id"].startswith("j_")
    assert payload["data"]["scn"] == 12345
    assert payload["data"]["table_order"] == ["T1"]

    # state writes must have happened in this order
    names = [name for name, _, _ in state_calls]
    assert names == ["ensure_schema", "insert_job", "insert_table_records"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_oracdb_status_returns_job_and_table_records(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job
    from oracle_schema_refresh.state.table_record import TableRecord

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=99,
        config_json="{}",
        status="DONE",
    )
    records = [
        TableRecord(
            job_id="j_x", table_name="T1", status="DONE",
            rows_source=5, rows_target=5,
        ),
    ]

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.get_job",
            return_value=job,
        ):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.get_table_records",
                return_value=records,
            ):
                result = runner.invoke(
                    oracdb,
                    ["status", "j_x", "--target", "local", "--json"],
                    env=env,
                )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "DONE"
    assert payload["data"]["tables"][0]["table"] == "T1"


def test_oracdb_status_unknown_job_errors(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.get_job",
            return_value=None,
        ):
            result = runner.invoke(
                oracdb,
                ["status", "j_missing", "--target", "local", "--json"],
                env=env,
            )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error_category"] == "CONFIG"


# ---------------------------------------------------------------------------
# run (foreground)
# ---------------------------------------------------------------------------


def test_oracdb_run_foreground_executes_and_updates_state(tmp_path: Path) -> None:
    from oracle_schema_refresh.engine import RefreshResult, TableResult
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=99,
        config_json=json.dumps({
            "source_schema": "SRC",
            "target_schema": "TGT",
            "tables": ["T1"],
            "auto_include_fk_parents": False,
            "dblink": None,
        }),
        status="PLANNED",
    )

    mock_result = RefreshResult(
        success=True,
        tables_requested=["T1"],
        tables_resolved=["T1"],
        table_order=["T1"],
        table_results=[
            TableResult(
                table_name="T1",
                status="ok",
                rows_loaded=5,
                rows_source=5,
                match=True,
            )
        ],
        scn=99,
    )

    status_updates: list[str] = []
    table_updates: list[tuple[str, str]] = []

    def update_status(conn, jid, status, error=None):
        status_updates.append(status)

    def update_table(conn, *, job_id, table_name, **kw):
        if "status" in kw:
            table_updates.append((table_name, kw["status"]))

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.update_job_status",
                side_effect=update_status,
            ):
                with patch(
                    "oracle_schema_refresh.oracdb_cli.state.update_table_record",
                    side_effect=update_table,
                ):
                    with patch(
                        "oracle_schema_refresh.oracdb_cli.RefreshEngine.run",
                        return_value=mock_result,
                    ):
                        result = runner.invoke(
                            oracdb,
                            ["run", "j_x", "--target", "local", "--json"],
                            env=env,
                        )

    assert result.exit_code == 0, result.output
    # RUNNING then DONE
    assert status_updates == ["RUNNING", "DONE"]
    # T1 ended DONE
    assert ("T1", "DONE") in table_updates


def test_oracdb_run_refuses_non_planned_job_without_resume(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json="{}",
        status="DONE",
    )

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            result = runner.invoke(
                oracdb, ["run", "j_x", "--target", "local", "--json"], env=env
            )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert "resume" in (payload.get("error") or "").lower() or "status" in (
        payload.get("error") or ""
    ).lower()


def test_oracdb_run_resume_allowed_for_failed_job(tmp_path: Path) -> None:
    from oracle_schema_refresh.engine import RefreshResult
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json=json.dumps({
            "source_schema": "SRC",
            "target_schema": "TGT",
            "tables": ["T1"],
            "auto_include_fk_parents": False,
            "dblink": None,
        }),
        status="FAILED",
    )

    mock_result = RefreshResult(
        success=True,
        tables_requested=["T1"],
        tables_resolved=["T1"],
        table_order=["T1"],
    )

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.update_job_status",
            ):
                with patch(
                    "oracle_schema_refresh.oracdb_cli.state.update_table_record",
                ):
                    with patch(
                        "oracle_schema_refresh.oracdb_cli.RefreshEngine.run",
                        return_value=mock_result,
                    ):
                        result = runner.invoke(
                            oracdb,
                            ["run", "j_x", "--target", "local", "--resume", "--json"],
                            env=env,
                        )

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# cancel / cleanup
# ---------------------------------------------------------------------------


def test_oracdb_cancel_marks_job_cancelled(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json="{}",
        status="PLANNED",
    )

    seen: list[tuple[str, str]] = []

    def upd(conn, jid, status, error=None):
        seen.append((jid, status))

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.update_job_status",
                side_effect=upd,
            ):
                result = runner.invoke(
                    oracdb, ["cancel", "j_x", "--target", "local", "--json"], env=env
                )

    assert result.exit_code == 0, result.output
    assert seen == [("j_x", "CANCELLED")]


def test_oracdb_cleanup_drops_state(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json="{}",
        status="DONE",
    )

    drop_calls: list[str] = []

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.drop_job",
                side_effect=lambda conn, jid: drop_calls.append(jid),
            ):
                result = runner.invoke(
                    oracdb, ["cleanup", "j_x", "--target", "local", "--json"], env=env
                )

    assert result.exit_code == 0, result.output
    assert drop_calls == ["j_x"]


def test_oracdb_cleanup_refuses_active_job(tmp_path: Path) -> None:
    """Don't wipe state for a RUNNING job — could leak staging tables in Cut 2."""
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json="{}",
        status="RUNNING",
    )

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            result = runner.invoke(
                oracdb, ["cleanup", "j_x", "--target", "local", "--json"], env=env
            )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_oracdb_verify_recompares_row_counts(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=42,
        config_json="{}",
        status="DONE",
    )

    def fake_row_count(conn, schema, table, **kw):
        # Source 5, target 5 — matches.
        return 5

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.get_table_records",
                return_value=[],
            ):
                with patch(
                    "oracle_schema_refresh.oracdb_cli.introspect.get_table_row_count",
                    side_effect=fake_row_count,
                ):
                    with patch(
                        "oracle_schema_refresh.oracdb_cli.state.get_table_records"
                    ) as get_records:
                        from oracle_schema_refresh.state.table_record import TableRecord
                        get_records.return_value = [
                            TableRecord(
                                job_id="j_x", table_name="T1", status="DONE",
                                rows_source=5, rows_target=5,
                            ),
                        ]
                        result = runner.invoke(
                            oracdb,
                            ["verify", "j_x", "--target", "local", "--json"],
                            env=env,
                        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["all_match"] is True


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


def test_oracdb_wait_returns_when_terminal(tmp_path: Path) -> None:
    """``wait`` polls oracdb$jobs until status is terminal, then returns."""
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    # First two reads: RUNNING; third: DONE.
    states = ["RUNNING", "RUNNING", "DONE"]

    def fake_get_job(conn, jid):
        return Job(
            job_id=jid,
            source_endpoint="local",
            target_endpoint="local",
            source_schema="SRC",
            target_schema="TGT",
            scn=1,
            config_json="{}",
            status=states.pop(0) if states else "DONE",
        )

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.get_job",
            side_effect=fake_get_job,
        ):
            with patch("oracle_schema_refresh.oracdb_cli.time.sleep"):
                result = runner.invoke(
                    oracdb,
                    [
                        "wait", "j_x", "--target", "local",
                        "--timeout", "10", "--poll-interval", "0",
                        "--json",
                    ],
                    env=env,
                )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["status"] == "DONE"


def test_oracdb_wait_times_out(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    def stuck(conn, jid):
        return Job(
            job_id=jid,
            source_endpoint="local",
            target_endpoint="local",
            source_schema="SRC",
            target_schema="TGT",
            scn=1,
            config_json="{}",
            status="RUNNING",
        )

    # monotonic returns far in the future on the 2nd call to force a timeout.
    times = iter([0.0, 0.0, 1000.0])

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.get_job",
            side_effect=stuck,
        ):
            with patch("oracle_schema_refresh.oracdb_cli.time.sleep"):
                with patch(
                    "oracle_schema_refresh.oracdb_cli.time.monotonic",
                    side_effect=lambda: next(times),
                ):
                    result = runner.invoke(
                        oracdb,
                        [
                            "wait", "j_x", "--target", "local",
                            "--timeout", "1", "--poll-interval", "0",
                            "--json",
                        ],
                        env=env,
                    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["error_category"] == "TRANSIENT"


# ---------------------------------------------------------------------------
# run --background spawns a detached subprocess
# ---------------------------------------------------------------------------


def test_oracdb_run_background_returns_immediately(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    _add_endpoints(runner, env)

    job = Job(
        job_id="j_x",
        source_endpoint="local",
        target_endpoint="local",
        source_schema="SRC",
        target_schema="TGT",
        scn=1,
        config_json="{}",
        status="PLANNED",
    )

    popen_calls: list[tuple[list[str], dict]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))
            self.pid = 99999

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch("oracle_schema_refresh.oracdb_cli.subprocess.Popen", FakePopen):
                result = runner.invoke(
                    oracdb,
                    ["run", "j_x", "--target", "local", "--background", "--json"],
                    env=env,
                )

    assert result.exit_code == 0, result.output
    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    # Subprocess command should include the hidden _worker subcommand.
    assert "_worker" in args
    assert "j_x" in args
    # Detached: new session, no inherited stdout
    assert kwargs.get("start_new_session") is True
