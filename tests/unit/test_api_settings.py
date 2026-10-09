"""Settings and API keys for the two server modes."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from nisar_db.api.settings import (
    HEAVY_JOBS,
    Settings,
    hash_key,
    load_keys,
)


def test_keys_from_file_hash_plain_keys(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            [
                {"name": "alice", "key": "s3cret", "scopes": ["read", "jobs"]},
                {"name": "bob", "sha256": hash_key("other")},
            ]
        )
    )
    alice, bob = load_keys(path)
    assert (
        alice.sha256 == hash_key("s3cret")
        and alice.allows("jobs")
        and not alice.allows("admin")
    )
    assert bob.scopes == frozenset({"read"})


def test_keys_from_env_string():
    (k,) = load_keys(None, "ci:abc:read+jobs")
    assert k.name == "ci" and k.allows("jobs") and k.sha256 == hash_key("abc")


def test_admin_key_allows_every_scope():
    (k,) = load_keys(None, "root:x:admin")
    assert all(k.allows(s) for s in ("read", "jobs", "admin"))


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "a", "scopes": ["read"]}, "neither 'key' nor 'sha256'"),
        ({"name": "a", "key": "k", "scopes": ["write"]}, "unknown scopes"),
    ],
)
def test_bad_keys_are_refused(tmp_path, entry, message):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps([entry]))
    with pytest.raises(ValueError, match=message):
        load_keys(path)


def test_mode_defaults():
    local, shared = Settings.for_mode("local"), Settings.for_mode("shared")
    assert (
        local.host == "127.0.0.1"
        and local.page_login
        and local.allowed_heavy_jobs == HEAVY_JOBS
    )
    assert shared.host == "0.0.0.0" and not shared.page_login
    assert (
        shared.rate_limit == 60
        and shared.cors_origins == ()
        and shared.allowed_heavy_jobs == frozenset()
    )


def test_overrides_keep_the_other_mode_defaults():
    s = Settings.for_mode("shared", max_jobs=5)
    assert s.rate_limit == 60 and s.max_jobs == 5


def test_none_override_runs_without_a_checkout():
    assert Settings.for_mode("local", repo_dir=None).repo_dir is None


def test_local_mode_refuses_a_network_address():
    with pytest.raises(ValueError, match=r"local mode listens on 127\.0\.0\.1 only"):
        Settings.for_mode("local", host="0.0.0.0").validate()


def test_shared_mode_needs_a_key():
    with pytest.raises(ValueError, match="shared mode needs at least one API key"):
        Settings.for_mode("shared").validate()
    Settings.for_mode("shared", keys=load_keys(None, "a:b:read")).validate()
