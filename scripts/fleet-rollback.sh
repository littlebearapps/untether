#!/usr/bin/env bash
# Fleet rollback helper for Untether — parallel revert to a known-good version.
#
# Used when a fleet-rollout went wrong and you need to step back to the
# previous version on all four hosts (or one host with --only).
#
# Mirrors scripts/fleet-rollout.sh's parallel SSH pattern but skips the
# attestation gate (we're going BACK to a known-good version, not forward
# to an untested one).
#
# Usage:
#   scripts/fleet-rollback.sh 0.35.2                # revert all 4 hosts to 0.35.2
#   scripts/fleet-rollback.sh 0.35.3rc13 --only mac # revert only mac
#   scripts/fleet-rollback.sh 0.35.2 --dry-run      # preview commands

set -euo pipefail

PACKAGE="untether"
STATE_FILE="${HOME}/.untether-dev/fleet-rollout-state.json"

VERSION=""
DRY_RUN=0
ONLY_HOST=""

usage() {
    cat <<EOF
Usage: fleet-rollback.sh VERSION [--dry-run] [--only HOST]

Revert Untether to VERSION on all 4 hosts in parallel (or one host with --only).

The attestation gate is intentionally SKIPPED — rollbacks go to a known-good
version, not a new one. If VERSION is itself a prerelease (rcN/aN/bN), use
TestPyPI as the index; otherwise use PyPI.

Options:
  --dry-run            Print install/restart commands per host without executing
  --only HOST          Roll only one host (lba-1 | nsd | channelo | mac)
EOF
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage 0 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --only) ONLY_HOST="${2:?--only requires a host name}"; shift 2 ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *)
            if [[ -z "$VERSION" ]]; then VERSION="$1"; else echo "Unexpected: $1" >&2; usage 1; fi
            shift
            ;;
    esac
done

[[ -n "$VERSION" ]] || { echo "VERSION argument is required." >&2; usage 1; }

if [[ "$VERSION" =~ (rc|a|b|dev) ]]; then
    IS_PRERELEASE=1
    PIP_ARGS='--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/'
    UV_INDEX_ARGS='--default-index https://test.pypi.org/simple/ --index https://pypi.org/simple/ --prerelease=allow --index-strategy unsafe-best-match'
    INDEX_SOURCE="TestPyPI"
else
    IS_PRERELEASE=0
    PIP_ARGS='--index-url https://pypi.org/simple/'
    UV_INDEX_ARGS='--default-index https://pypi.org/simple/'
    INDEX_SOURCE="PyPI"
fi

echo "Rollback to version: $VERSION ($INDEX_SOURCE)"

declare -A INSTALL_CMD
declare -A RESTART_CMD
declare -A POSTCHECK_CMD
declare -A MANAGER

# Standard remote PATH so non-interactive ssh sees brew (Apple Silicon),
# user-installed binaries, and system tools.
REMOTE_PATH='$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'

# Same detect/build helpers as fleet-rollout.sh — kept in sync intentionally.
detect_install_manager() {
    local host="$1"
    ssh "$host" "PATH=${REMOTE_PATH} bash -c '
        if uv tool list 2>/dev/null | grep -q \"^untether \"; then echo uv
        elif pipx list 2>/dev/null | grep -q \"package untether \"; then echo pipx
        elif command -v uv >/dev/null; then echo uv
        elif command -v pipx >/dev/null; then echo pipx
        else echo none; fi'" 2>/dev/null
}

build_install_cmd() {
    local host="$1" mgr="$2"
    case "$mgr" in
        uv)
            INSTALL_CMD[$host]="ssh ${host} 'PATH=${REMOTE_PATH} uv tool install --force ${UV_INDEX_ARGS} ${PACKAGE}==${VERSION}'"
            ;;
        pipx)
            INSTALL_CMD[$host]="ssh ${host} 'PATH=${REMOTE_PATH} pipx install --force --pip-args=\"${PIP_ARGS}\" ${PACKAGE}==${VERSION}'"
            ;;
        none)
            INSTALL_CMD[$host]="echo 'ERROR: ${host} has neither uv nor pipx installed' >&2; exit 1"
            ;;
        *)
            INSTALL_CMD[$host]="echo 'ERROR: unknown install manager \"${mgr}\" for ${host}' >&2; exit 1"
            ;;
    esac
}

# lba-1 always uses staging.sh (local).
INSTALL_CMD[lba-1]="cd ${HOME}/untether && scripts/staging.sh install ${VERSION}"
RESTART_CMD[lba-1]='systemctl --user restart untether'
POSTCHECK_CMD[lba-1]='systemctl --user is-active untether'
MANAGER[lba-1]='pipx (via staging.sh)'

# Restart + postcheck are manager-independent.
RESTART_CMD[nsd]="ssh nsd 'systemctl --user restart untether'"
POSTCHECK_CMD[nsd]="ssh nsd 'systemctl --user is-active untether'"

RESTART_CMD[channelo]="ssh channelo 'systemctl --user restart untether'"
POSTCHECK_CMD[channelo]="ssh channelo 'systemctl --user is-active untether'"

RESTART_CMD[mac]='ssh mac "launchctl kickstart -k gui/\$(id -u)/com.littlebearapps.untether"'
POSTCHECK_CMD[mac]='ssh mac "launchctl print gui/\$(id -u)/com.littlebearapps.untether | grep -E \"^\\s*(state|last exit code)\""'

ALL_HOSTS=(lba-1 nsd channelo mac)

if [[ -n "$ONLY_HOST" ]]; then
    valid=0
    for h in "${ALL_HOSTS[@]}"; do
        if [[ "$h" == "$ONLY_HOST" ]]; then valid=1; break; fi
    done
    if (( valid == 0 )); then
        echo "ERROR: --only $ONLY_HOST: unknown host. Choose: ${ALL_HOSTS[*]}" >&2
        exit 4
    fi
    HOSTS=("$ONLY_HOST")
else
    HOSTS=("${ALL_HOSTS[@]}")
fi

# Probe install manager for each remote host.
echo
echo "Probing install managers per host..."
for host in "${HOSTS[@]}"; do
    if [[ "$host" == "lba-1" ]]; then
        printf "  %-10s → %s\n" "$host" "${MANAGER[lba-1]}"
        continue
    fi
    mgr=$(detect_install_manager "$host")
    MANAGER[$host]="$mgr"
    build_install_cmd "$host" "$mgr"
    printf "  %-10s → %s\n" "$host" "$mgr"
    if [[ "$mgr" == "none" ]]; then
        echo "WARN: $host has neither uv nor pipx; rollback install will fail." >&2
    fi
done

if (( DRY_RUN == 1 )); then
    echo
    echo "=== DRY RUN (rollback) ==="
    for host in "${HOSTS[@]}"; do
        echo
        echo "── ${host} (manager: ${MANAGER[$host]}) ──"
        echo "install:  ${INSTALL_CMD[$host]}"
        echo "restart:  ${RESTART_CMD[$host]}"
        echo "postcheck: ${POSTCHECK_CMD[$host]}"
    done
    exit 0
fi

LOG_DIR=$(mktemp -d -t untether-fleet-rollback-XXXXXX)
echo "Per-host logs: $LOG_DIR"

declare -A PIDS
declare -A LOG_FILES

# Per-host state file updater (read-modify-write, same shape as fleet-rollout.sh).
update_state() {
    local host="$1" status="$2"
    python3 - "$STATE_FILE" "$host" "$status" <<'PY' || true
import json, sys, time, os
state_path, host, status = sys.argv[1:4]
try:
    with open(state_path) as f:
        state = json.load(f)
except Exception:
    state = {}
state.setdefault("last_rollback", {}).setdefault("hosts", {})[host] = status
state["last_rollback"]["updated_at"] = int(time.time())
os.makedirs(os.path.dirname(state_path), exist_ok=True)
with open(state_path, "w") as f:
    json.dump(state, f, indent=2)
PY
}

for host in "${HOSTS[@]}"; do
    LOG_FILES[$host]="$LOG_DIR/${host}.log"
    (
        rc=0
        {
            echo "=== install (rollback to $VERSION) ==="
            eval "${INSTALL_CMD[$host]}" || rc=$?
            if (( rc == 0 )); then
                echo "=== restart ==="
                eval "${RESTART_CMD[$host]}" || rc=$?
            fi
            if (( rc == 0 )); then
                echo "=== postcheck ==="
                eval "${POSTCHECK_CMD[$host]}" || rc=$?
            fi
        } >>"${LOG_FILES[$host]}" 2>&1
        exit "$rc"
    ) &
    PIDS[$host]=$!
done

declare -A EXITS
for host in "${HOSTS[@]}"; do
    if wait "${PIDS[$host]}"; then
        EXITS[$host]=0
        update_state "$host" "ok"
    else
        EXITS[$host]=$?
        update_state "$host" "failed (exit ${EXITS[$host]})"
    fi
done

OK_COUNT=0
FAIL_COUNT=0
echo
echo "=== fleet rollback results — version ${VERSION} ==="
for host in "${HOSTS[@]}"; do
    if (( EXITS[$host] == 0 )); then
        echo "  ${host}: OK"
        OK_COUNT=$((OK_COUNT + 1))
    else
        echo "  ${host}: FAILED (exit ${EXITS[$host]}; log ${LOG_FILES[$host]})"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

# Mark the rollback in state file (best-effort).
python3 - <<EOF || true
import json, os, time
state_path = "${STATE_FILE}"
state = {}
if os.path.exists(state_path):
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        state = {}
state["last_rollback"] = {
    "version": "${VERSION}",
    "hosts": "${HOSTS[*]}".split(),
    "at": int(time.time()),
}
os.makedirs(os.path.dirname(state_path), exist_ok=True)
with open(state_path, "w") as f:
    json.dump(state, f, indent=2)
EOF

if (( FAIL_COUNT > 0 )); then
    exit 5
fi
