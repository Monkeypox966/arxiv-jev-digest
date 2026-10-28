"""Independent Linux timer: dispatch daily mode and verify persisted SMTP receipts.

Python standard library only. No model/SMTP credentials or paper database are needed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


class SchedulerError(RuntimeError):
    pass


@dataclass
class Settings:
    repository: str
    token: str = field(repr=False)
    ref: str = "main"
    workflow: str = "daily.yml"
    timezone: str = "Asia/Shanghai"
    after: str = "11:00"
    directory: Path = Path("/var/lib/arxiv-digest-scheduler")

    def __post_init__(self):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repository):
            raise SchedulerError("GITHUB_REPOSITORY must be OWNER/REPO")
        if not self.token.strip():
            raise SchedulerError("GH_TOKEN is required")
        if not re.fullmatch(r"[\w.-]+\.ya?ml", self.workflow):
            raise SchedulerError("Invalid workflow filename")
        if not re.fullmatch(r"\d{2}:\d{2}", self.after):
            raise SchedulerError("DAILY_AFTER must use HH:MM")
        time.fromisoformat(self.after)
        ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls):
        return cls(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GH_TOKEN", ""),
            ref=os.environ.get("GITHUB_REF_NAME", "main"),
            workflow=os.environ.get("GITHUB_WORKFLOW_FILE", "daily.yml"),
            timezone=os.environ.get("DIGEST_TIMEZONE", "Asia/Shanghai"),
            after=os.environ.get("DAILY_AFTER", "11:00"),
            directory=Path(os.environ.get("STATE_DIRECTORY", "/var/lib/arxiv-digest-scheduler")),
        )


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise SchedulerError("Refusing to forward GitHub credentials to another origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(SameOriginRedirect())

    def call(self, path, *, payload=None, missing_ok=False):
        request = Request(
            "https://api.github.com/repos/" + self.config.repository + "/" + path,
            headers={
                "Authorization": "Bearer " + self.config.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "arxiv-jev-digest-independent-scheduler",
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise SchedulerError("GitHub response exceeds size limit")
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            # Do not log request objects, headers, tokens or response bodies.
            raise SchedulerError(f"GitHub API HTTP {exc.code}") from None

    def receipt(self):
        result = self.call("contents/delivery-status.json?ref=digest-state", missing_ok=True)
        if result is None:
            return None
        if result.get("encoding") != "base64" or result.get("size", 0) > 65536:
            raise SchedulerError("Unexpected delivery receipt format")
        return json.loads(base64.b64decode(result["content"]))

    def runs(self):
        return self.call(
            "actions/workflows/"
            + quote(self.config.workflow, safe="")
            + "/runs?"
            + urlencode({"branch": self.config.ref, "per_page": 100})
        )["workflow_runs"]

    def dispatch(self, marker):
        return self.call(
            "actions/workflows/" + quote(self.config.workflow, safe="") + "/dispatches",
            payload={
                "ref": self.config.ref,
                "inputs": {"mode": "daily", "scheduler_id": marker},
            },
        )


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_receipt(value, config):
    if value is None:
        return
    if value.get("schema_version") != 1 or value.get("timezone") != config.timezone:
        raise SchedulerError("Receipt schema/timezone does not match scheduler configuration")
    for name in ("recipient_count", "acknowledged_count", "pending_count"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise SchedulerError("Invalid receipt counts")
    count, acknowledged = value["recipient_count"], value["acknowledged_count"]
    if acknowledged > count or type(value.get("delivered")) is not bool:
        raise SchedulerError("Invalid delivery receipt")
    if value["delivered"] != (count > 0 and acknowledged == count and value["pending_count"] == 0):
        raise SchedulerError("Inconsistent delivery receipt")
    datetime.fromisoformat(value["date"])


def tick(config, api, state, *, now=None, force_dispatch=False):
    now = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone))

    def finish(status, **details):
        health = {"status": status, "checked_at": now.isoformat(), **details}
        atomic_json(config.directory / "state.json", state)
        atomic_json(config.directory / "health.json", health)
        return health

    if now.time() < time.fromisoformat(config.after) and not force_dispatch:
        return finish("not_due")
    receipt = api.receipt()
    validate_receipt(receipt, config)
    if receipt and receipt["date"] == now.date().isoformat() and receipt["delivered"]:
        if not force_dispatch:
            return finish("delivered", receipt=receipt)
    runs = api.runs()
    active = next((run for run in runs if run["status"] != "completed"), None)
    if active:
        started = datetime.fromisoformat(active["created_at"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > 110 * 60:
            raise SchedulerError(
                "Workflow is stuck for over 110 minutes; operator attention needed"
            )
        return finish("running", run_id=active["id"], run_url=active["html_url"])
    if receipt and receipt["pending_count"]:
        raise SchedulerError("Uncertain SMTP acceptance: inspect pending delivery before retrying")
    previous = state.get("last_dispatch")
    if previous:
        elapsed = (now - datetime.fromisoformat(previous["at"])).total_seconds()
        matched = next(
            (run for run in runs if previous["marker"] in run.get("display_title", "")), None
        )
        # A timed-out POST may still have created a run. Allow indexing to catch up.
        if matched is None and elapsed < 15 * 60:
            return finish("awaiting_run_index", marker=previous["marker"])
        if matched is not None and elapsed < 5 * 60:
            return finish("awaiting_receipt", run_id=matched["id"])
    marker = "vps-" + now.strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
    state["last_dispatch"] = {"marker": marker, "at": now.isoformat()}
    # Record intent before POST so loss of the response never causes an immediate duplicate.
    atomic_json(config.directory / "state.json", state)
    api.dispatch(marker)
    return finish("dispatched", marker=marker, receipt=receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force-dispatch",
        action="store_true",
        help="Test the external trigger even after today's receipt; still uses daily deduplication",
    )
    args = parser.parse_args(argv)
    config = Settings.from_env()
    config.directory.mkdir(parents=True, exist_ok=True)
    # Linux advisory lock also protects manual starts against the systemd timer.
    import fcntl

    with (config.directory / "scheduler.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"already_running"}')
            return 0
        try:
            path = config.directory / "state.json"
            state = json.loads(path.read_text("utf-8")) if path.is_file() else {}
            result = tick(config, GitHub(config), state, force_dispatch=args.force_dispatch)
        except Exception as exc:
            result = {
                "status": "error",
                "checked_at": datetime.now(UTC).isoformat(),
                "error": str(exc) if isinstance(exc, SchedulerError) else type(exc).__name__,
            }
            atomic_json(config.directory / "health.json", result)
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
