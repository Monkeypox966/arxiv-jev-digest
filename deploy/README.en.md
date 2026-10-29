# Independent server scheduler

GitHub Actions schedules can be delayed or missed. This companion dispatches the repository's `daily` workflow hourly from an always-on Linux server. Actions owns announcement freshness, daily deduplication, screening, translation and SMTP; the server never needs model or SMTP credentials.

## Requirements

- Linux with systemd
- Python 3.11 or newer
- Network access to `api.github.com`
- A fine-grained token limited to the target repository with **Actions: Read and write**

The scheduler uses Python's standard library and calls the GitHub API directly, so GitHub CLI is optional. It does not read repository contents or delivery receipts. Keep model and SMTP secrets in GitHub Actions Secrets.

## Install

Copy this directory to the server and run as root:

```sh
umask 077
cp scheduler.env.example /etc/arxiv-digest-scheduler.env
# Edit GITHUB_REPOSITORY, GH_TOKEN and any time-zone settings.
sh install.sh
```

The default schedule dispatches once per hour from **10:00 through 23:00 Beijing time**, using UTC `02:00–15:00`. Dispatches continue after a successful email; Actions skips redundant work. `Persistent=yes` catches a missed timer after a server restart.

## Delivery safety

Actions restores its own delivery history and checks for an acknowledged daily delivery covering today's announcement. If already delivered, it skips before fetching; if `/new` is still dated before today, it skips before metadata API or model calls. Fresh batches without matches also send nothing. Empty digests are never sent. The server only guards active runs and recent dispatch intents against accidental duplicate requests; SMTP pending protection remains in Actions.

```sh
/opt/arxiv-digest-scheduler/status.sh
journalctl -u arxiv-digest-scheduler.service --since today
```

`dispatched` means a trigger request was submitted, not that email was sent. `running`, `awaiting_run_index` and `awaiting_previous_run` indicate waiting. Actions skip reasons `waiting_for_arxiv_update`, `already_delivered_today` and `no_matching_papers` are normal no-email outcomes. A pending SMTP delivery requires manual inspection before retrying.

## Uninstall

```sh
sh uninstall.sh
```

This removes the installed code and systemd units but preserves `/etc/arxiv-digest-scheduler.env` and `/var/lib/arxiv-digest-scheduler`. Use `sh uninstall.sh --purge` only when you explicitly want to remove the token configuration and local state as well.
