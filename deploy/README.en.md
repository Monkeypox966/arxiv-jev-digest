# Independent server scheduler

GitHub Actions schedules can be delayed or missed. This companion starts the repository's `daily` workflow from an always-on Linux server and checks the persisted delivery receipt. Paper screening, summaries and SMTP still run in GitHub Actions; the server never needs model or SMTP credentials.

## Requirements

- Linux with systemd
- Python 3.11 or newer
- Network access to `api.github.com`
- A repository token limited to the target repository with **Actions: Read and write** and **Contents: Read**

The scheduler uses Python's standard library and calls the GitHub API directly, so GitHub CLI is optional. If you use GitHub CLI to create or inspect a token, run `gh auth login`, then `gh auth refresh -s actions:write,contents:read` and put the output of `gh auth token` in the environment file. Keep model and SMTP secrets in GitHub Actions Secrets.

## Install

Copy this directory to the server and run as root:

```sh
umask 077
cp scheduler.env.example /etc/arxiv-digest-scheduler.env
# Edit GITHUB_REPOSITORY, GH_TOKEN and any time-zone settings.
sh install.sh
```

The default schedule is 11:00 Beijing time, followed by ten-minute checks through 23:50. The timer uses UTC (`03:00–15:50 UTC`) and the program skips checks before `DAILY_AFTER`. `Persistent=yes` catches a missed timer after a server restart.

## Delivery safety

The scheduler reads `digest-state/delivery-status.json`. It stops dispatching only when every normal recipient has an SMTP-acknowledged delivery for the current local date, including an empty digest. Test emails do not count. Missing or stale receipts dispatch `daily`; an active run, a recent dispatch intent, or an uncertain pending SMTP attempt blocks duplicates.

```sh
/opt/arxiv-digest-scheduler/status.sh
journalctl -u arxiv-digest-scheduler.service --since today
```

`delivered` means the receipt confirms completion; `dispatched`, `running`, `awaiting_run_index` and `awaiting_receipt` are intermediate states. A pending delivery requires manual inspection before retrying.

## Uninstall

```sh
sh uninstall.sh
```

This removes the installed code and systemd units but preserves `/etc/arxiv-digest-scheduler.env` and `/var/lib/arxiv-digest-scheduler`. Use `sh uninstall.sh --purge` only when you explicitly want to remove the token configuration and local state as well.
