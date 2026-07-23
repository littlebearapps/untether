#!/usr/bin/env bash
# Fleet status for Untether — a one-shot, READ-ONLY view of what version and
# service state every host is on right now.
#
# Companion to:
#   scripts/fleet-rollout.sh    — parallel upgrade (writes state)
#   scripts/fleet-rollback.sh   — parallel revert
#   scripts/healthcheck.sh      — single-host post-deploy health check
#
# Unlike rollout/rollback this NEVER installs or restarts anything. It only reads,
# over the tailnet SSH mesh, per host:
#   - installed version   (`untether --version`, uv/pipx-agnostic)
#   - service state       (systemctl --user is-active | launchctl state on Mac)
#   - last restart / since (systemctl ActiveEnterTimestamp | ps lstart on Mac)
#
# Every remote ssh carries `-o ConnectTimeout=8 -o BatchMode=yes` so a down or
# asleep host is reported as "unreachable" in ~8s instead of hanging the sweep.
#
# The host list, REMOTE_PATH, and Mac launchctl label mirror fleet-rollout.sh so
# the three scripts agree on what "the fleet" is. Kept in sync intentionally,
# exactly as fleet-rollout.sh <-> fleet-rollback.sh already are (three small
# independent scripts, no shared sourcing).
#
# Usage:
#   scripts/fleet-status.sh                # table for all 5 hosts
#   scripts/fleet-status.sh --only sl      # one host
#   scripts/fleet-status.sh --json         # machine-readable (for /monitor, CI)
#
# Strategic plan: docs/plans/fleet/03-phase3-fleet-ops-hardening.md (3a)

set -euo pipefail

JSON=0
ONLY_HOST=""

usage() {
    cat <<EOF
Usage: fleet-status.sh [--only HOST] [--json]

Read-only version + service-state view of the Untether fleet
(lba-1, nsd, channelo, sl, mac).

Options:
  --only HOST   Show only one host (lba-1 | nsd | channelo | sl | mac)
  --json        Emit machine-readable JSON instead of a table
  --help, -h    Show this help
EOF
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage 0 ;;
        --json) JSON=1; shift ;;
        --only) ONLY_HOST="${2:?--only requires a host name}"; shift 2 ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *) echo "Unexpected argument: $1" >&2; usage 1 ;;
    esac
done

# Host list + remote PATH + Mac launchctl label mirror fleet-rollout.sh.
ALL_HOSTS=(lba-1 nsd channelo sl mac)
MAC_LABEL='com.littlebearapps.untether'
SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes)

if [[ -n "$ONLY_HOST" ]]; then
    valid=0
    for h in "${ALL_HOSTS[@]}"; do [[ "$h" == "$ONLY_HOST" ]] && valid=1; done
    if (( valid == 0 )); then
        echo "ERROR: --only $ONLY_HOST: unknown host. Choose: ${ALL_HOSTS[*]}" >&2
        exit 4
    fi
    HOSTS=("$ONLY_HOST")
else
    HOSTS=("${ALL_HOSTS[@]}")
fi

# ── Probe scripts (single-quoted so $/" stay literal until they run on the host;
#    $HOME expands wherever the snippet executes — local for lba-1, remote via ssh).

read -r -d '' LINUX_PROBE <<'PROBE' || true
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
v=$(untether --version 2>/dev/null | grep -oE "[0-9]+[.][0-9]+[.][0-9]+[a-z0-9]*" | head -1)
s=$(systemctl --user is-active untether 2>/dev/null || true)
t=$(systemctl --user show untether -p ActiveEnterTimestamp --value 2>/dev/null || true)
printf "%s|%s|%s" "${v:-?}" "${s:-inactive}" "${t:-?}"
PROBE

read -r -d '' MAC_PROBE <<'PROBE' || true
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
v=$(untether --version 2>/dev/null | grep -oE "[0-9]+[.][0-9]+[.][0-9]+[a-z0-9]*" | head -1)
label=gui/$(id -u)/com.littlebearapps.untether
info=$(launchctl print "$label" 2>/dev/null || true)
s=$(printf "%s\n" "$info" | sed -n "s/^[[:space:]]*state = //p" | head -1)
pid=$(printf "%s\n" "$info" | sed -n "s/^[[:space:]]*pid = //p" | head -1)
t="?"
if [ -n "$pid" ]; then t=$(ps -o lstart= -p "$pid" 2>/dev/null | sed "s/^[[:space:]]*//;s/[[:space:]]*$//"); fi
printf "%s|%s|%s" "${v:-?}" "${s:-not-loaded}" "${t:-?}"
PROBE

# probe_host <host> → echoes "VERSION|SERVICE|SINCE"; SERVICE=unreachable if ssh fails.
probe_host() {
    local host="$1" out rc=0
    if [[ "$host" == "lba-1" ]]; then
        out=$(bash -c "$LINUX_PROBE" 2>/dev/null) || rc=$?
    elif [[ "$host" == "mac" ]]; then
        out=$(ssh "${SSH_OPTS[@]}" "$host" "$MAC_PROBE" 2>/dev/null) || rc=$?
    else
        out=$(ssh "${SSH_OPTS[@]}" "$host" "$LINUX_PROBE" 2>/dev/null) || rc=$?
    fi
    if (( rc != 0 )) || [[ -z "$out" ]]; then
        echo "?|unreachable|?"
    else
        echo "$out"
    fi
}

# ── Fire all probes in parallel, collect into temp files (read-only, fast).
TMP=$(mktemp -d -t untether-fleet-status-XXXXXX)
trap 'rm -rf "$TMP"' EXIT

declare -A PIDS
for host in "${HOSTS[@]}"; do
    ( probe_host "$host" >"$TMP/$host" ) &
    PIDS[$host]=$!
done
for host in "${HOSTS[@]}"; do wait "${PIDS[$host]}" 2>/dev/null || true; done

# ── Render.
if (( JSON == 1 )); then
    # Build JSON with python3 for correct escaping.
    python3 - "$TMP" "${HOSTS[@]}" <<'PY'
import json, sys, os
tmp = sys.argv[1]; hosts = sys.argv[2:]
rows = []
for h in hosts:
    try:
        v, s, t = (x.strip() for x in open(os.path.join(tmp, h)).read().split("|", 2))
    except Exception:
        v, s, t = "?", "unreachable", "?"
    rows.append({"host": h, "version": v, "service": s, "since": t})
print(json.dumps({"hosts": rows}, indent=2))
PY
else
    printf "%-10s %-14s %-13s %s\n" "HOST" "VERSION" "SERVICE" "SINCE"
    for host in "${HOSTS[@]}"; do
        IFS='|' read -r v s t <"$TMP/$host" || { v="?"; s="unreachable"; t="?"; }
        printf "%-10s %-14s %-13s %s\n" "$host" "${v:-?}" "${s:-?}" "${t:-?}"
    done
fi
