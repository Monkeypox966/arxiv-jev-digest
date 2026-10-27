from __future__ import annotations

import importlib.util
import json
import subprocess

import pytest
from conftest import ROOT

from arxiv_digest.models import InstitutionAssessment
from arxiv_digest.store import Store


@pytest.fixture
def state_module():
    spec = importlib.util.spec_from_file_location("state_helper", ROOT / ".github/scripts/state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_git_state_roundtrip_without_credentials_on_disk(tmp_path, monkeypatch, state_module):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    original_git = state_module.git

    def local_git(*args, **kwargs):
        if args[:3] == ("remote", "add", "origin"):
            args = (*args[:3], str(remote))
        return original_git(*args, **kwargs)

    monkeypatch.setattr(state_module, "git", local_git)
    monkeypatch.setenv("GH_TOKEN", "fake-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/repository")
    for name in ["first", "second"]:
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "config.toml").write_text('state_dir = ".state"', encoding="utf-8")
        monkeypatch.chdir(workspace)
        state_module.main(["load"])
        with Store(workspace / ".state") as store:
            if name == "first":
                store.cache("decision", "key", {"score": 0.95})
                store.prepare("hashed-recipient", "profile", ["paper-1"], ["paper-1"])
                store.remember_institution(
                    "research-scope",
                    InstitutionAssessment(
                        institution="ERI",
                        canonical_name="Example Research Institute",
                        score=0.8,
                        status="assessed",
                        confidence=0.9,
                        sources=["https://example.edu/"],
                    ),
                    ["eri", "example research institute"],
                )
            else:
                assert store.cached("decision", "key") == {"score": 0.95}
                assert len(store.pending()) == 1
                assert store.learned_institution("research-scope", "eri")[0].score == 0.8
                assert (workspace / ".state/institutions.json").is_file()
        state_module.main(["save"])
        snapshot = json.loads((workspace / ".state/delivery-status.json").read_text("utf-8"))
        assert not snapshot["delivered"]
        state_module.main(["save"])  # An unchanged database is a successful no-op.
        disk_config = (workspace / ".state/.git/config").read_text(encoding="utf-8")
        assert "fake-test-token" not in disk_config
        assert "extraheader" not in disk_config


def test_remote_failure_is_not_treated_as_empty_history(tmp_path, monkeypatch, state_module):
    original_git = state_module.git

    def failing_git(*args, **kwargs):
        if args[:3] == ("remote", "add", "origin"):
            args = (*args[:3], str(tmp_path / "missing.git"))
        return original_git(*args, **kwargs)

    monkeypatch.setattr(state_module, "git", failing_git)
    monkeypatch.setenv("GH_TOKEN", "fake-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/repository")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('state_dir = ".state"', encoding="utf-8")
    with pytest.raises(RuntimeError, match="ls-remote failed"):
        state_module.main(["load"])
    assert not (tmp_path / ".state/digest.sqlite3").exists()
