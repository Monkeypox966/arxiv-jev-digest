#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this uninstaller as root.' >&2
  exit 1
fi

systemctl disable --now arxiv-digest-scheduler.timer 2>/dev/null || true
systemctl stop arxiv-digest-scheduler.service 2>/dev/null || true
rm -f /etc/systemd/system/arxiv-digest-scheduler.timer
rm -f /etc/systemd/system/arxiv-digest-scheduler.service
rm -rf /opt/arxiv-digest-scheduler
systemctl daemon-reload

if [ "${1:-}" = "--purge" ]; then
  rm -f /etc/arxiv-digest-scheduler.env
  rm -rf /var/lib/arxiv-digest-scheduler
  echo 'Removed scheduler code, configuration, and local state.'
else
  echo 'Removed scheduler code and units.'
  echo 'Preserved /etc/arxiv-digest-scheduler.env and /var/lib/arxiv-digest-scheduler.'
  echo 'Use --purge only if you intentionally want to delete the local token and state.'
fi
