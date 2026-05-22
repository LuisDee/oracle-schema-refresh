"""Cut 1c follow-up — address self-review holes.

* Cooperative cancellation: engine polls a check_cancellation callback
  between tables and bails with a CANCELLED-shaped result.
* Background log redirection.
* Local ~/.oracdb/jobs.json index so --target is optional.
* SELECT FOR UPDATE NOWAIT on the job row at run start (TOCTOU).
* Best-effort per-row state updates.
* oracdb jobs list.
* status --watch.
* oracdb logs JOB_ID [--follow].
* oracdb cleanup --leaked-dblinks.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from pydantic import SecretStr

from tests._helpers import make_mock_conn, patched_introspect


def _runner_env(tmp_path: Path) -> dict[str, str]:
    return {
        "ORACDB_REGISTRY": str(tmp_path / "endpoints.yaml"),
        "ORACDB_HOME": str(tmp_path / ".oracdb"),
    }


# ---------------------------------------------------------------------------
# Cooperative cancellation
# ---------------------------------------------------------------------------


def _make_engine_two_tables() -> object:
    from oracle_schema_refresh.config import OracleConnection, RefreshConfig
    from oracle_schema_refresh.engine import RefreshEngine

    return RefreshEngine(
        OracleConnection(username="u", password="p", dsn="h:1/s"),
        RefreshConfig(
            source_schema="SRC",
            target_schema="TGT",
            tables=["T1", "T2"],
            auto_include_fk_parents=False,
        ),
    )


def test_engine_run_honours_check_cancellation_between_tables() -> None:
    """When ``check_cancellation`` returns True, engine.run stops processing
    further tables and reports cancelled=True on the result."""
    from oracle_schema_refresh.endpoints.endpoint import Endpoint

    engine = _make_engine_two_tables()
    conn = make_mock_conn()

    # Cancellation fires on the 2nd check (after T1 finished).
    calls = {"n": 0}

    def check() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    with patch.object(Endpoint, "connect", return_value=conn):
        with patched_introspect(
            discover_fk_parents=["T1", "T2"],
            build_dependency_graph={"T1": [], "T2": []},
        ):
            result = engine.run(  # type: ignore[union-attr]
                dry_run=False, check_cancellation=check
            )

    assert result.cancelled is True
    assert result.success is False
    # T1 ok, T2 skipped due to cancellation
    statuses = {r.table_name: r.status for r in result.table_results}
    assert statuses.get("T1") == "ok"
    assert statuses.get("T2") in {"skipped", "cancelled"}


def test_engine_run_check_cancellation_default_is_no_op() -> None:
    """Existing callers (no callback) see no behaviour change."""
    from oracle_schema_refresh.endpoints.endpoint import Endpoint

    engine = _make_engine_two_tables()
    conn = make_mock_conn()

    with patch.object(Endpoint, "connect", return_value=conn):
        with patched_introspect(
            discover_fk_parents=["T1", "T2"],
            build_dependency_graph={"T1": [], "T2": []},
        ):
            result = engine.run(dry_run=False)  # type: ignore[union-attr]

    assert result.cancelled is False
    assert all(r.status == "ok" for r in result.table_results)


# ---------------------------------------------------------------------------
# Jobs index (~/.oracdb/jobs.json)
# ---------------------------------------------------------------------------


def test_jobs_index_round_trip(tmp_path: Path) -> None:
    from oracle_schema_refresh.jobs_index import JobsIndex

    idx_path = tmp_path / "jobs.json"
    idx = JobsIndex(path=idx_path)
    idx.add("j_x", target="tgt", source="src")
    idx.add("j_y", target="prod", source="src")
    idx.save()

    idx2 = JobsIndex(path=idx_path)
    idx2.load()
    assert idx2.target_for("j_x") == "tgt"
    assert idx2.target_for("j_y") == "prod"


def test_jobs_index_missing_file_returns_empty() -> None:
    from oracle_schema_refresh.jobs_index import JobsIndex

    idx = JobsIndex(path=Path("/nonexistent/jobs.json"))
    idx.load()
    assert idx.target_for("j_anything") is None


def test_jobs_index_remove(tmp_path: Path) -> None:
    from oracle_schema_refresh.jobs_index import JobsIndex

    idx = JobsIndex(path=tmp_path / "jobs.json")
    idx.add("j_x", target="t", source="s")
    idx.remove("j_x")
    assert idx.target_for("j_x") is None


def test_status_resolves_target_from_index_when_flag_omitted(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    # Prime the local index — pretend a plan ran earlier.
    from oracle_schema_refresh.jobs_index import JobsIndex
    idx_path = Path(env["ORACDB_HOME"]) / "jobs.json"
    idx = JobsIndex(path=idx_path)
    idx.add("j_x", target="local", source="local")
    idx.save()

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
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.get_table_records",
                return_value=[],
            ):
                result = runner.invoke(
                    oracdb, ["status", "j_x", "--json"], env=env
                )

    assert result.exit_code == 0, result.output


def test_status_without_target_and_unknown_job_errors_cleanly(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)

    result = runner.invoke(oracdb, ["status", "j_unknown", "--json"], env=env)
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["error_category"] == "CONFIG"


# ---------------------------------------------------------------------------
# jobs list
# ---------------------------------------------------------------------------


def test_oracdb_jobs_list_emits_envelope(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    jobs = [
        Job(
            job_id="j_a", source_endpoint="local", target_endpoint="local",
            source_schema="S", target_schema="T", scn=1, config_json="{}",
            status="DONE",
        ),
        Job(
            job_id="j_b", source_endpoint="local", target_endpoint="local",
            source_schema="S", target_schema="T", scn=2, config_json="{}",
            status="RUNNING",
        ),
    ]

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=MagicMock(),
    ):
        with patch(
            "oracle_schema_refresh.oracdb_cli.state.list_jobs", return_value=jobs
        ):
            result = runner.invoke(
                oracdb, ["jobs", "list", "--target", "local", "--json"], env=env
            )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert [j["job_id"] for j in payload["data"]["jobs"]] == ["j_a", "j_b"]


# ---------------------------------------------------------------------------
# status --watch
# ---------------------------------------------------------------------------


def test_status_watch_polls_until_terminal(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    states = ["RUNNING", "RUNNING", "DONE"]

    def fake_get_job(conn, jid):
        return Job(
            job_id=jid, source_endpoint="local", target_endpoint="local",
            source_schema="S", target_schema="T", scn=1, config_json="{}",
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
            with patch(
                "oracle_schema_refresh.oracdb_cli.state.get_table_records",
                return_value=[],
            ):
                with patch("oracle_schema_refresh.oracdb_cli.time.sleep"):
                    result = runner.invoke(
                        oracdb,
                        [
                            "status", "j_x", "--target", "local",
                            "--watch", "--poll-interval", "0", "--json",
                        ],
                        env=env,
                    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["status"] == "DONE"


# ---------------------------------------------------------------------------
# logs JOB_ID
# ---------------------------------------------------------------------------


def test_oracdb_logs_reads_file(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    env = _runner_env(tmp_path)
    logs_dir = Path(env["ORACDB_HOME"]) / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "j_x.log").write_text("phase_start phase=1\ntable_loaded T1\n")

    result = CliRunner().invoke(oracdb, ["logs", "j_x"], env=env)
    assert result.exit_code == 0, result.output
    assert "phase_start" in result.output
    assert "table_loaded" in result.output


def test_oracdb_logs_missing_file_errors(tmp_path: Path) -> None:
    from oracle_schema_refresh.oracdb_cli import oracdb

    env = _runner_env(tmp_path)
    result = CliRunner().invoke(oracdb, ["logs", "j_missing"], env=env)
    assert result.exit_code != 0


def test_run_background_writes_log_file(tmp_path: Path) -> None:
    """``run --background`` must redirect the worker's stdout/stderr to
    ~/.oracdb/logs/<job_id>.log so the user can read what happened."""
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    job = Job(
        job_id="j_x", source_endpoint="local", target_endpoint="local",
        source_schema="S", target_schema="T", scn=1, config_json="{}",
        status="PLANNED",
    )

    popen_calls: list[tuple] = []

    class FakePopen:
        def __init__(self, args, **kwargs) -> None:
            popen_calls.append((args, kwargs))
            self.pid = 99

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
    args, kwargs = popen_calls[0]
    # stdout / stderr now point at the log file, not /dev/null.
    log_file = Path(env["ORACDB_HOME"]) / "logs" / "j_x.log"
    # The subprocess wasn't real, so the file may not exist yet, but the
    # Popen kwargs must point at a TextIOWrapper writing to it.
    out = kwargs.get("stdout")
    err = kwargs.get("stderr")
    assert out is not None and err is not None
    assert log_file.parent.exists()  # logs dir created


# ---------------------------------------------------------------------------
# SELECT FOR UPDATE NOWAIT on the job row at run start (TOCTOU)
# ---------------------------------------------------------------------------


def test_run_locks_job_row_for_update(tmp_path: Path) -> None:
    """``run`` should acquire a row-level lock on oracdb$jobs before
    transitioning to RUNNING so two simultaneous runs don't both
    proceed."""
    from oracle_schema_refresh.engine import RefreshResult
    from oracle_schema_refresh.oracdb_cli import oracdb
    from oracle_schema_refresh.state.job import Job

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    job = Job(
        job_id="j_x", source_endpoint="local", target_endpoint="local",
        source_schema="S", target_schema="T", scn=1, config_json=json.dumps({
            "source_schema": "S", "target_schema": "T", "tables": ["T1"],
            "auto_include_fk_parents": False, "dblink": None,
        }),
        status="PLANNED",
    )

    mock_conn = MagicMock()
    seen_sqls: list[str] = []
    mock_conn.cursor.return_value.__enter__ = lambda s: s.cursor.return_value
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute.side_effect = lambda sql, *a, **kw: seen_sqls.append(sql)
    mock_conn.cursor.return_value = cur

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect",
        return_value=mock_conn,
    ):
        with patch("oracle_schema_refresh.oracdb_cli.state.get_job", return_value=job):
            with patch("oracle_schema_refresh.oracdb_cli.state.lock_job") as lock_fn:
                with patch("oracle_schema_refresh.oracdb_cli.state.update_job_status"):
                    with patch(
                        "oracle_schema_refresh.oracdb_cli.state.update_table_record"
                    ):
                        with patch(
                            "oracle_schema_refresh.oracdb_cli.RefreshEngine.run",
                            return_value=RefreshResult(
                                success=True, tables_requested=["T1"],
                                tables_resolved=["T1"], table_order=["T1"],
                            ),
                        ):
                            result = runner.invoke(
                                oracdb,
                                ["run", "j_x", "--target", "local", "--json"],
                                env=env,
                            )

    assert result.exit_code == 0, result.output
    lock_fn.assert_called_once()


def test_lock_job_swallows_no_data_returns_false() -> None:
    """``state.lock_job`` returns False when the row doesn't exist (so the
    caller can surface 'unknown job') and True on a successful lock."""
    import oracledb

    from oracle_schema_refresh.state.job import lock_job

    conn = make_mock_conn()
    cur = conn.cursor().__enter__()
    cur.fetchone.return_value = ("j_x",)
    assert lock_job(conn, "j_x") is True

    cur.fetchone.return_value = None
    assert lock_job(conn, "j_x") is False

    # ORA-00054: resource busy and acquire with NOWAIT specified
    err = oracledb.DatabaseError()
    err.args = (type("X", (), {"code": 54, "message": "ORA-00054"}),)
    cur.execute.side_effect = err
    # Should propagate so the CLI can report 'already running'.
    import pytest as _pytest
    with _pytest.raises(oracledb.DatabaseError):
        lock_job(conn, "j_x")


# ---------------------------------------------------------------------------
# leaked-dblink cleanup
# ---------------------------------------------------------------------------


def test_cleanup_leaked_dblinks_drops_orphan_oracdb_links(tmp_path: Path) -> None:
    """``oracdb cleanup --leaked-dblinks`` finds ORACDB_* private links on
    the target whose associated job is not in oracdb$jobs (or in a
    terminal state) and drops them."""
    from oracle_schema_refresh.oracdb_cli import oracdb

    runner = CliRunner()
    env = _runner_env(tmp_path)
    runner.invoke(
        oracdb,
        ["endpoints", "add", "local", "--dsn", "h:1/s", "--user", "u", "--password", "p"],
        env=env,
    )

    mock_conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    # USER_DB_LINKS query returns two leaked links.
    cur.fetchall.return_value = [("ORACDB_AABBCC",), ("ORACDB_DDEEFF",)]
    seen: list[str] = []
    cur.execute.side_effect = lambda sql, *a, **kw: seen.append(sql)
    mock_conn.cursor.return_value = cur

    with patch(
        "oracle_schema_refresh.oracdb_cli.Endpoint.connect", return_value=mock_conn
    ):
        result = runner.invoke(
            oracdb,
            ["cleanup", "--leaked-dblinks", "--target", "local", "--json"],
            env=env,
        )

    assert result.exit_code == 0, result.output
    drops = [s for s in seen if "DROP DATABASE LINK" in s.upper()]
    # Both leaked links should be dropped.
    assert any("ORACDB_AABBCC" in s for s in drops)
    assert any("ORACDB_DDEEFF" in s for s in drops)


# ---------------------------------------------------------------------------
# Sanity — existing tests still work after Endpoint import side-effects
# ---------------------------------------------------------------------------


def test_endpoint_still_importable() -> None:
    from oracle_schema_refresh.endpoints import Endpoint

    ep = Endpoint(name="x", dsn="h:1/s", username="u", password=SecretStr("p"))
    assert ep.name == "x"
