"""``nisar-db serve``: options become settings; unsafe setups are refused."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import uvicorn
from click.testing import CliRunner

from nisar_db.cli import cli_app


@pytest.fixture
def served(monkeypatch):
    """Record what ``uvicorn.run`` was asked to serve instead of serving it."""
    for var in ("NISAR_DB_API_MODE", "NISAR_DB_API_KEYS", "NISAR_DB_API_KEYS_FILE"):
        monkeypatch.delenv(var, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw})
    )
    return calls


def test_serve_help():
    result = CliRunner().invoke(cli_app, ["serve", "--help"])
    assert result.exit_code == 0 and "--mode [local|shared]" in result.output


def test_serve_local_defaults(served, tmp_path, viewer_page):
    result = CliRunner().invoke(
        cli_app,
        ["serve", "--cache-dir", str(tmp_path), "--viewer-html", str(viewer_page)],
    )
    assert result.exit_code == 0, result.output
    (call,) = served
    settings = call["app"].state.settings
    assert call["host"] == "127.0.0.1" and call["port"] == 8797
    assert settings.mode == "local" and settings.keys == () and settings.rate_limit == 0


def test_serve_shared_needs_keys(served, tmp_path):
    result = CliRunner().invoke(
        cli_app, ["serve", "--mode", "shared", "--cache-dir", str(tmp_path)]
    )
    assert (
        result.exit_code == 1
        and "shared mode needs at least one API key" in result.output
    )
    assert served == []


def test_serve_shared_with_env_keys(served, monkeypatch, tmp_path, viewer_page):
    monkeypatch.setenv("NISAR_DB_API_KEYS", "team:k:read+jobs")
    result = CliRunner().invoke(
        cli_app,
        [
            "serve",
            "--mode",
            "shared",
            "--cache-dir",
            str(tmp_path),
            "--viewer-html",
            str(viewer_page),
            "--cors-origin",
            "https://portal.example",
            "--rate-limit",
            "10",
            "--allow-job",
            "download",
            "--private-read",
            "--public-url",
            "https://nisar-db.example.org",
        ],
    )
    assert result.exit_code == 0, result.output
    settings = served[0]["app"].state.settings
    assert served[0]["host"] == "0.0.0.0"
    assert [k.name for k in settings.keys] == ["team"] and settings.private_read
    assert (
        settings.cors_origins == ("https://portal.example",)
        and settings.rate_limit == 10
    )
    assert settings.allowed_heavy_jobs == frozenset({"download"})
    assert settings.public_url == "https://nisar-db.example.org"


@pytest.mark.usefixtures("served")
def test_serve_local_refuses_a_network_host(tmp_path):
    result = CliRunner().invoke(
        cli_app, ["serve", "--host", "0.0.0.0", "--cache-dir", str(tmp_path)]
    )
    assert (
        result.exit_code == 1
        and "local mode listens on 127.0.0.1 only" in result.output
    )
