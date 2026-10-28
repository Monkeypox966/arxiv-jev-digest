#!/bin/sh
set -eu

systemctl --no-pager list-timers arxiv-digest-scheduler.timer || true
echo
systemctl --no-pager status arxiv-digest-scheduler.timer || true
echo
if [ -f /var/lib/arxiv-digest-scheduler/health.json ]; then
  cat /var/lib/arxiv-digest-scheduler/health.json
else
  echo 'No scheduler health file yet.'
fi
