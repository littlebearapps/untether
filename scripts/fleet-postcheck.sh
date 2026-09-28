#!/usr/bin/env bash
# fleet-postcheck.sh — post-restart health check for fleet-rollout.sh and
# fleet-rollback.sh (#745).
#
# A single `systemctl is-active` right after a restart is not proof of health:
# a service that crashes a few seconds into startup reads `active` first and
# then enters a restart loop (sl on the 0.35.5rc8 and rc11 rollouts, httpx
# 1.0.devN). This check requires the service to stay up for a stability window
# with its restart counter unchanged.
#
# Runs on the target host. Invoked locally (`bash fleet-postcheck.sh systemd`)
# or over ssh with the script on stdin (`ssh host 'bash -s -- systemd' < …`).
#
# Usage: fleet-postcheck.sh systemd|launchd [stable_seconds] [timeout_seconds]
# Exit 0 = healthy; non-zero = not healthy (reason on stdout).
set -u

mode="${1:?usage: fleet-postcheck.sh systemd|launchd [stable_s] [timeout_s]}"
stable_s="${2:-16}"
timeout_s="${3:-90}"
poll_s=2

case "$mode" in
systemd)
    unit="${FLEET_POSTCHECK_UNIT:-untether}"  # override for testing only
    restarts() { systemctl --user show "$unit" -p NRestarts --value 2>/dev/null; }
    state() { systemctl --user is-active "$unit" 2>/dev/null; }
    up_state=active
    ;;
launchd)
    label="gui/$(id -u)/com.littlebearapps.untether"
    # Top-level fields only (one leading tab); nested dicts repeat "state".
    field() { launchctl print "$label" 2>/dev/null | awk -v k="$1" '$0 ~ "^\t" k " = " {print $3; exit}'; }
    restarts() { field runs; }
    state() { field state; }
    up_state=running
    ;;
*)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac

baseline="$(restarts)"
stable=0
waited=0
s="$(state)"
while (( waited < timeout_s )); do
    s="$(state)"
    now="$(restarts)"
    if [[ "$now" != "$baseline" ]]; then
        # A restart during the window resets the clock, and the new count
        # becomes the baseline; a crash loop never accumulates a full window.
        echo "restart counter moved ${baseline} -> ${now} (state=${s})"
        baseline="$now"
        stable=0
    elif [[ "$s" == "$up_state" ]]; then
        stable=$((stable + poll_s))
        if (( stable >= stable_s )); then
            echo "${s} (stable ${stable}s, restarts=${now})"
            exit 0
        fi
    else
        stable=0
    fi
    sleep "$poll_s"
    waited=$((waited + poll_s))
done
echo "NOT HEALTHY after ${timeout_s}s: state=${s} restarts=$(restarts) (baseline ${baseline})"
exit 1
