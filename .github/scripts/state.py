"""Load/save the dedicated state branch without putting credentials on disk.

This helper runs only on trusted scheduled/manual workflows, never in pull-request CI.
Git errors are summarized, not echoed: remote diagnostics may contain private data.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


def git(*args, cwd: Path, env: dict, accepted=(0,)):
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode not in accepted:
        raise RuntimeError(f"State git operation {args[0]} failed (exit {result.returncode})")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["load", "save"])
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8-sig"))
    directory = (root / config.get("state_dir", ".state")).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise RuntimeError("In Actions, state_dir must be a subdirectory of the repository")
    if args.mode == "save" and not (directory / "digest.sqlite3").exists():
        print("No state database to save.")
        return
    directory.mkdir(parents=True, exist_ok=True)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if urlsplit(server).scheme != "https":
        raise RuntimeError("GitHub server URL must use HTTPS")
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = "digest-state"
    env = dict(os.environ)
    auth = base64.b64encode(f"x-access-token:{env['GH_TOKEN']}".encode()).decode()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{server}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if args.mode == "load":
        git("init", "--initial-branch=" + branch, cwd=directory, env=env)
        git("remote", "add", "origin", f"{server}/{repo}.git", cwd=directory, env=env)
        remote = git(
            "ls-remote",
            "--exit-code",
            "origin",
            f"refs/heads/{branch}",
            cwd=directory,
            env=env,
            accepted=(0, 2),
        )
        if remote.returncode == 0:
            git("fetch", "--depth=1", "origin", branch, cwd=directory, env=env)
            git("checkout", "-B", branch, "FETCH_HEAD", cwd=directory, env=env)
            print("Restored persisted state.")
        else:
            print("First run: a state branch will be created after processing.")
    else:
        git("config", "user.name", "github-actions[bot]", cwd=directory, env=env)
        git(
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
            cwd=directory,
            env=env,
        )
        git("add", "--", "digest.sqlite3", cwd=directory, env=env)
        changed = git("diff", "--cached", "--quiet", cwd=directory, env=env, accepted=(0, 1))
        if changed.returncode == 0:
            print("State unchanged.")
            return
        git("commit", "-m", "Update digest state", cwd=directory, env=env)
        git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=directory, env=env)
        print("Persisted state successfully.")


if __name__ == "__main__":
    main()
