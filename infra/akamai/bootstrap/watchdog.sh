#!/usr/bin/env bash
# Workload watchdog: powers the host off after a configured idle period with
# no active benchmark or serving traffic.
#
# SCOPE WARNING — NOT A BILLING CONTROL: Akamai bills for the instance while
# it EXISTS on the account, powered on or off. This watchdog only limits a
# runaway or forgotten WORKLOAD. Stopping charges requires the owner-approved
# teardown (deletion) workflow: blackwell-cloud teardown-plan / destroy.

set -euo pipefail

IDLE_MINUTES="${1:-45}"
STATE_FILE="/var/lib/bwlab-watchdog.last-active"

is_active() {
  # Active when a benchmark driver is running or the serving container is
  # processing (container running counts as active only with a live driver;
  # an idle serving container alone does not keep the host up).
  pgrep -f "blackwell-bench|blackwell-cloud pilot|blackwell-cloud mvl-baseline" >/dev/null 2>&1
}

now_epoch="$(date +%s)"
if is_active; then
  echo "${now_epoch}" > "${STATE_FILE}"
  exit 0
fi

if [ ! -f "${STATE_FILE}" ]; then
  echo "${now_epoch}" > "${STATE_FILE}"
  exit 0
fi

last_active="$(cat "${STATE_FILE}")"
idle_s=$(( now_epoch - last_active ))
if [ "${idle_s}" -ge $(( IDLE_MINUTES * 60 )) ]; then
  logger -t bwlab-watchdog "idle ${idle_s}s >= ${IDLE_MINUTES}m: powering off. \
NOTE: a powered-off Akamai instance still bills; run the owner-approved teardown."
  systemctl poweroff
fi
