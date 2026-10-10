"""``nisar-db`` commands as background jobs."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from nisar_db.api.jobs import Jobs


def _jobs(tmp_path, launcher, **kw):
    return Jobs(tmp_path / "jobs", launcher=launcher, **kw)


def test_search_job_runs_the_cli_and_lists_its_output(tmp_path, make_launcher, waiter):
    launcher = make_launcher(outputs=("results.csv",))
    jobs = _jobs(tmp_path, launcher)
    job = jobs.submit(
        "search",
        {"product_type": "GUNW", "track": 34, "bbox": [-121, 34, -119, 36]},
        "me",
    )
    assert waiter(lambda: jobs.get(job.id).state == "done")
    assert launcher.calls[0] == [
        "search",
        "--output-csv",
        "results.csv",
        "--product-type",
        "GUNW",
        "--bbox",
        "-121.0,34.0,-119.0,36.0",
        "--track",
        "34",
        "--max-results",
        "1000",
    ]
    assert jobs.get(job.id).outputs == ["results.csv"]
    record = jobs.folder(job.id) / "job.json"
    assert waiter(lambda: json.loads(record.read_text())["state"] == "done")


def test_failed_job_reports_its_last_log_line(tmp_path, make_launcher, waiter):
    jobs = _jobs(tmp_path, make_launcher(rc=2, log="starting\nError: no catalog\n"))
    job = jobs.submit("create-blackout-dates", {}, "me")
    assert waiter(lambda: jobs.get(job.id).state == "failed")
    assert (
        jobs.get(job.id).error == "Error: no catalog"
        and jobs.get(job.id).returncode == 2
    )


def test_unknown_parameters_are_refused(tmp_path, make_launcher):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _jobs(tmp_path, make_launcher()).submit("search", {"tracks": 3}, "me")


def test_unknown_kind_raises_keyerror(tmp_path, make_launcher):
    with pytest.raises(KeyError):
        _jobs(tmp_path, make_launcher()).submit("rm-rf", {}, "me")


def test_refused_heavy_kind(tmp_path, make_launcher):
    jobs = _jobs(tmp_path, make_launcher(), refuse=frozenset({"download"}))
    with pytest.raises(PermissionError, match="does not run 'download' jobs"):
        jobs.submit("download", {"granule_ids": ["G1-ASF"]}, "me")


def test_blackout_from_snow_needs_an_input_file(tmp_path, make_launcher):
    with pytest.raises(ValueError, match="source 'snow' needs input_file"):
        _jobs(tmp_path, make_launcher()).submit(
            "create-blackout-dates", {"source": "snow"}, "me"
        )


def test_shared_service_reads_only_inside_its_roots(tmp_path, make_launcher):
    allowed = tmp_path / "catalog"
    allowed.mkdir()
    (allowed / "consistent.json").write_text("{}")
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    jobs = _jobs(tmp_path, make_launcher(), allowed_roots=(allowed,))
    assert jobs.resolve(str(allowed / "consistent.json")) == str(
        (allowed / "consistent.json").resolve()
    )
    with pytest.raises(ValueError, match="outside the folders this service reads from"):
        jobs.resolve(str(secret))
    with pytest.raises(ValueError, match="no such file"):
        jobs.resolve(str(allowed / "missing.json"))


def test_one_jobs_output_feeds_the_next(tmp_path, make_launcher, waiter):
    catalog = tmp_path / "cat.csv"
    catalog.write_text("x")
    jobs = _jobs(tmp_path, make_launcher(outputs=("consistent.json",)))
    first = jobs.submit(
        "create-consistent", {"catalog": str(catalog), "nisar_gpkg": str(catalog)}, "me"
    )
    assert waiter(lambda: jobs.get(first.id).state == "done")
    second = jobs.submit(
        "create-reference-dates",
        {"consistent_json": f"job:{first.id}/consistent.json"},
        "me",
    )
    assert second.argv[2] == str((jobs.folder(first.id) / "consistent.json").resolve())


def test_download_writes_its_granule_list(tmp_path, make_launcher):
    jobs = _jobs(tmp_path, make_launcher())
    job = jobs.submit("download", {"granule_ids": ["G1-ASF", "G2-ASF"]}, "me")
    assert (jobs.folder(job.id) / "granules.txt").read_text() == "G1-ASF\nG2-ASF\n"
    assert "--granule-list" in job.argv


def test_jobs_wait_their_turn_and_can_be_cancelled(tmp_path, make_launcher, waiter):
    launcher = make_launcher(block=True)
    jobs = _jobs(tmp_path, launcher, max_jobs=1)
    a = jobs.submit("create-blackout-dates", {}, "me")
    b = jobs.submit("create-blackout-dates", {}, "me")
    assert launcher.started.acquire(timeout=5)
    assert jobs.get(a.id).state == "running" and jobs.get(b.id).state == "queued"
    jobs.cancel(a.id)
    assert waiter(lambda: jobs.get(b.id).state == "running")
    assert launcher.procs[0].terminated and jobs.get(a.id).state == "cancelled"
    assert launcher.started.acquire(timeout=5)
    launcher.procs[1].released.set()
    assert waiter(lambda: jobs.get(b.id).state == "done")


def test_a_job_left_running_by_a_stopped_server_reads_failed(tmp_path, make_launcher):
    launcher = make_launcher(block=True)
    job = _jobs(tmp_path, launcher).submit("create-blackout-dates", {}, "me")
    assert launcher.started.acquire(timeout=5)
    reloaded = _jobs(tmp_path, make_launcher())
    assert reloaded.get(job.id).state == "failed"
    assert reloaded.get(job.id).error == "the server stopped while it ran"
    launcher.procs[0].released.set()


def test_list_filters_by_owner(tmp_path, make_launcher):
    jobs = _jobs(tmp_path, make_launcher())
    jobs.submit("create-blackout-dates", {}, "alice")
    jobs.submit("create-blackout-dates", {}, "bob")
    assert [j.owner for j in jobs.listed("alice")] == ["alice"]
    assert len(jobs.listed()) == 2


def test_output_path_only_serves_listed_outputs(tmp_path, make_launcher, waiter):
    jobs = _jobs(tmp_path, make_launcher(outputs=("results.csv",)))
    job = jobs.submit("search", {}, "me")
    assert waiter(lambda: jobs.get(job.id).state == "done")
    assert jobs.output_path(job.id, "results.csv").read_text() == "out\n"
    with pytest.raises(KeyError):
        jobs.output_path(job.id, "job.json")


def test_a_cancel_during_launch_still_stops_the_process(
    tmp_path, make_launcher, waiter
):
    """Regression: cancel() between a process starting and its registration
    marked the job cancelled but left the process running."""
    launcher = make_launcher(block=True)
    jobs = _jobs(tmp_path, launcher)

    def launch_then_cancel(argv, cwd, log):
        proc = launcher(argv, cwd, log)
        jobs.cancel(job_id[0])
        return proc

    jobs.launcher = launch_then_cancel
    job_id = [None]
    job_id[0] = jobs.submit("create-blackout-dates", {}, "me").id
    assert waiter(lambda: bool(launcher.procs) and launcher.procs[0].terminated)
    assert waiter(lambda: jobs.get(job_id[0]).finished is not None)
    assert jobs.get(job_id[0]).state == "cancelled"
