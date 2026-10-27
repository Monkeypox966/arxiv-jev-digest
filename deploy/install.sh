#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [ ! -f /etc/arxiv-digest-scheduler.env ]; then
  echo 'Create /etc/arxiv-digest-scheduler.env from scheduler.env.example first.' >&2
  exit 1
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /opt/arxiv-digest-scheduler
install -m 0644 "$source_dir/scheduler.py" /opt/arxiv-digest-scheduler/scheduler.py
install -m 0644 "$source_dir/arxiv-digest-scheduler.service" /etc/systemd/system/
install -m 0644 "$source_dir/arxiv-digest-scheduler.timer" /etc/systemd/system/
chmod 0600 /etc/arxiv-digest-scheduler.env
systemd-analyze verify /etc/systemd/system/arxiv-digest-scheduler.service /etc/systemd/system/arxiv-digest-scheduler.timer
systemctl daemon-reload
systemctl start arxiv-digest-scheduler.service
systemctl enable --now arxiv-digest-scheduler.timer
systemctl list-timers --no-pager arxiv-digest-scheduler.timer
