#!/usr/bin/env bash
# Fleet rollout helper for Untether — single-stage parallel upgrade across all
# five production-ish hosts (lba-1 staging, nsd, channelo, sl, mac).
#
# Companion scripts:
#   scripts/fleet-rollback.sh             — same parallel pattern, install previous version
#   scripts/run-integration-tests.sh      — writes the per-version attestation marker
#
# Strategic plan: docs/plans/2026-05-13-fleet-monitoring-and-upgrades.md
#
# Usage:
#   scripts/fleet-rollout.sh 0.35.3rc14                # rc -> TestPyPI rollout, 5 hosts parallel
#   scripts/fleet-rollout.sh 0.35.3                    # stable -> PyPI rollout, 5 hosts parallel
#   scripts/fleet-rollout.sh 0.35.3rc14 --dry-run      # print commands without executing
#   scripts/fleet-rollout.sh 0.35.3rc14 --only mac     # only roll one host
#   scripts/fleet-rollout.sh 0.35.3rc14 --skip-test-gate
#                                                      # bypass attestation (NOT recommended)
#   scripts/fleet-rollout.sh 0.35.3rc14 --force-downgrade
#                                                      # allow installing an older version
#
# Preconditions (enforced unless --skip-test-gate):
#   ~/.untether-dev/integration-test-pass-${VERSION}.json must exist.
#   The marker is written by scripts/run-integration-tests.sh on successful
#   Telegram MCP-driven integration test runs against @untether_dev_bot.
#
# What this script does NOT do:
#   - It does NOT push or merge anything (release-guard hooks would block that).
#   - It does NOT roll back automatically on partial failure. Successful hosts
#     stay upgraded; failing hosts are reported. Operator decides next move.
#   - It does NOT run integration tests itself. The attestation marker must
#     already exist when the script starts.

set -euo pipefail

PACKAGE="untether"
STATE_FILE="${HOME}/.untether-dev/fleet-rollout-state.json"
ATTESTATION_DIR="${HOME}/.untether-dev"

# ────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ────────────────────────────────────────────────────────────────────────────

VERSION=""
DRY_RUN=0
SKIP_TEST_GATE=0
FORCE_DOWNGRADE=0
ONLY_HOST=""
CLEAN_MARKERS=0

usage() {
    cat <<EOF
Usage: fleet-rollout.sh VERSION [--dry-run] [--only HOST] [--skip-test-gate] [--force-downgrade]

Single-stage parallel rollout of Untether to lba-1, nsd, channelo, sl, mac.

Required:
  VERSION              Target version (e.g. 0.35.3rc14 or 0.35.3)

Options:
  --dry-run            Print install/restart commands per host without executing
  --only HOST          Roll only one host (lba-1 | nsd | channelo | sl | mac)
  --skip-test-gate     Bypass the integration-test attestation precondition
  --force-downgrade    Allow installing an older version than the current state
  --clean-markers      Delete attestation markers older than 30 days, then exit

The integration-test attestation marker must exist at:
  ${ATTESTATION_DIR}/integration-test-pass-VERSION.json

Use scripts/run-integration-tests.sh to write the marker after a successful
Telegram MCP integration test run against @untether_dev_bot.
EOF
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage 0
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --skip-test-gate)
            SKIP_TEST_GATE=1
            shift
            ;;
        --force-downgrade)
            FORCE_DOWNGRADE=1
            shift
            ;;
        --clean-markers)
            CLEAN_MARKERS=1
            shift
            ;;
        --only)
            ONLY_HOST="${2:?--only requires a host name}"
            shift 2
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage 1
            ;;
        *)
            if [[ -z "$VERSION" ]]; then
                VERSION="$1"
            else
                echo "Unexpected argument: $1" >&2
                usage 1
            fi
            shift
            ;;
    esac
done

# Housekeeping action — remove stale markers and exit (no VERSION needed).
# Markers are never auto-cleaned otherwise (finding F); this is opt-in.
if (( CLEAN_MARKERS == 1 )); then
    echo "Cleaning attestation markers older than 30 days in ${ATTESTATION_DIR}..."
    find "$ATTESTATION_DIR" -maxdepth 1 -name 'integration-test-pass-*.json' -mtime +30 -print -delete 2>/dev/null || true
    echo "Done."
    exit 0
fi

[[ -n "$VERSION" ]] || { echo "VERSION argument is required." >&2; usage 1; }

# ────────────────────────────────────────────────────────────────────────────
# Version classification
# ────────────────────────────────────────────────────────────────────────────

if [[ "$VERSION" =~ (rc|a|b|dev) ]]; then
    IS_PRERELEASE=1
    INDEX_ARG="--pip-args=--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/"
    INDEX_SOURCE="TestPyPI"
else
    IS_PRERELEASE=0
    INDEX_ARG="--pip-args=--index-url https://pypi.org/simple/"
    INDEX_SOURCE="PyPI"
fi

echo "Target version: $VERSION ($INDEX_SOURCE; prerelease=$IS_PRERELEASE)"

# ────────────────────────────────────────────────────────────────────────────
# Attestation gate
# ────────────────────────────────────────────────────────────────────────────

ATTESTATION_MARKER="${ATTESTATION_DIR}/integration-test-pass-${VERSION}.json"

if (( SKIP_TEST_GATE == 0 )); then
    if [[ ! -f "$ATTESTATION_MARKER" ]]; then
        cat <<EOF >&2
ERROR: no integration-test attestation marker for ${VERSION}.

Expected: ${ATTESTATION_MARKER}

Run integration tests against @untether_dev_bot first, then write the marker
with scripts/run-integration-tests.sh ${VERSION} --manual. Or rerun this
script with --skip-test-gate to bypass (NOT recommended for any release
that will affect production hosts).
EOF
        exit 2
    fi
    echo "Attestation marker found: $ATTESTATION_MARKER"

    # Guardrail 1 — version-match. The marker's JSON "version" must equal VERSION.
    # Catches rolling with a copied/renamed marker, or a stale marker whose
    # filename matches but whose content attests a different version.
    MARKER_VER=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$ATTESTATION_MARKER" 2>/dev/null || echo "")
    if [[ -z "$MARKER_VER" ]]; then
        echo "WARN: could not read a \"version\" field from $ATTESTATION_MARKER — proceeding (hand-written marker?)." >&2
    elif [[ "$MARKER_VER" != "$VERSION" ]]; then
        echo "ERROR: attestation marker version ($MARKER_VER) != rollout version ($VERSION) — wrong marker?" >&2
        echo "Write the right one with scripts/run-integration-tests.sh ${VERSION} --manual, or pass --skip-test-gate to override." >&2
        exit 6
    fi

    # Guardrail 2 — staleness (warn, never block). A genuinely-unchanged rc is
    # fine to re-roll weeks later; a changed one should be re-tested first.
    if [[ -n "$(find "$ATTESTATION_MARKER" -mtime +14 2>/dev/null)" ]]; then
        echo "WARN: attestation marker for $VERSION is >14 days old — re-test if code changed since." >&2
    fi
else
    echo "WARN: --skip-test-gate set; attestation gate bypassed."
fi

# ────────────────────────────────────────────────────────────────────────────
# Supersede check (rc fast-replace)
# ────────────────────────────────────────────────────────────────────────────

if [[ -f "$STATE_FILE" ]]; then
    CURRENT_INFLIGHT=$(python3 -c "import json,sys; \
data=json.load(open('${STATE_FILE}')); \
print(data.get('current_version',''))" 2>/dev/null || echo "")
    if [[ -n "$CURRENT_INFLIGHT" && "$CURRENT_INFLIGHT" != "$VERSION" ]]; then
        # Naive comparison: if VERSION sorts >= CURRENT, supersede; else require flag.
        # pip's version sorting is the canonical truth; this is a simple
        # approximation that works for sequential rcs (rc13 -> rc14).
        NEWER=$(python3 -c "
from packaging.version import Version
print('newer' if Version('${VERSION}') > Version('${CURRENT_INFLIGHT}') else 'older-or-same')
" 2>/dev/null || echo "unknown")
        if [[ "$NEWER" == "older-or-same" && $FORCE_DOWNGRADE -ne 1 ]]; then
            echo "ERROR: in-flight rollout is $CURRENT_INFLIGHT; refusing to install older $VERSION." >&2
            echo "Pass --force-downgrade if you really want to do this." >&2
            exit 3
        fi
        if [[ "$NEWER" == "newer" ]]; then
            echo "Superseding in-flight rollout: $CURRENT_INFLIGHT -> $VERSION"
        fi
    fi
fi

# ────────────────────────────────────────────────────────────────────────────
# Per-host commands
# ────────────────────────────────────────────────────────────────────────────
#
# lba-1: local install via existing scripts/staging.sh helper (always pipx-
#        based, parity with the single-host workflow)
# nsd / channelo / sl / mac: install manager auto-detected at runtime via
#        detect_install_manager() — supports both pipx and uv tool. The
#        detection probes the EXISTING install (uv tool list / pipx list)
#        first; if untether isn't installed at all, falls back to whichever
#        manager is on PATH (preferring uv if both exist).
#
# Quoting note: the install commands are passed to `eval` with the remote
# portion wrapped in *single quotes* so that the local shell hands the ENTIRE
# remote command to ssh as one string. ssh then forwards it to the remote
# shell, which re-parses the inner double-quoted --pip-args/--index argument.
# Without this layering, the local shell eats the inner quotes and
# `--extra-index-url` ends up as a top-level pipx flag, which fails.
#
# Restart cmds:
#   Linux: `systemctl --user restart untether`
#   Mac:   `launchctl kickstart -k gui/$(id -u)/com.littlebearapps.untether`

declare -A INSTALL_CMD
declare -A RESTART_CMD
declare -A POSTCHECK_CMD
declare -A MANAGER

# Index arg strings — used inside the remote-shell single-quoted command.
if (( IS_PRERELEASE == 1 )); then
    PIP_ARGS='--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/'
    # --index-strategy unsafe-best-match is required: by default uv only
    # considers versions from the FIRST index that has the package, to
    # avoid dependency-confusion attacks. Since Untether's rc lives on
    # TestPyPI but its dependencies live on PyPI, we have to tell uv to
    # look across both. Without this, the install fails with
    # "No solution found when resolving dependencies".
    UV_INDEX_ARGS='--default-index https://test.pypi.org/simple/ --index https://pypi.org/simple/ --prerelease=allow --index-strategy unsafe-best-match'
else
    PIP_ARGS='--index-url https://pypi.org/simple/'
    UV_INDEX_ARGS='--default-index https://pypi.org/simple/'
fi

# Standard remote PATH so non-interactive ssh sees brew (Apple Silicon),
# user-installed binaries, and system tools.
REMOTE_PATH='$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'

# detect_install_manager <host> → echoes "uv", "pipx", or "none".
# Probes existing install first (canonical for current state), then falls
# back to whichever manager is available on PATH (uv preferred — faster +
# Mac-default).
detect_install_manager() {
    local host="$1"
    ssh "$host" "PATH=${REMOTE_PATH} bash -c '
        if uv tool list 2>/dev/null | grep -q \"^untether \"; then echo uv
        elif pipx list 2>/dev/null | grep -q \"package untether \"; then echo pipx
        elif command -v uv >/dev/null; then echo uv
        elif command -v pipx >/dev/null; then echo pipx
        else echo none; fi'" 2>/dev/null
}

# build_install_cmd <host> <manager> → fills INSTALL_CMD[$host] with the
# right shell command for that host+manager combo.
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
            INSTALL_CMD[$host]="echo 'ERROR: ${host} has neither uv nor pipx installed; cannot install untether' >&2; exit 1"
            ;;
        *)
            INSTALL_CMD[$host]="echo 'ERROR: unknown install manager \"${mgr}\" for ${host}' >&2; exit 1"
            ;;
    esac
}

# lba-1 always uses staging.sh — that's the canonical local install path.
# No detection needed because lba-1 is the host running this script.
INSTALL_CMD[lba-1]="cd ${HOME}/untether && scripts/staging.sh install ${VERSION}"
RESTART_CMD[lba-1]='systemctl --user restart untether'
POSTCHECK_CMD[lba-1]='systemctl --user is-active untether'
MANAGER[lba-1]='pipx (via staging.sh)'

# Restart + postcheck per host. These are NOT manager-dependent — only the
# install path varies.
RESTART_CMD[nsd]="ssh nsd 'systemctl --user restart untether'"
POSTCHECK_CMD[nsd]="ssh nsd 'systemctl --user is-active untether'"

RESTART_CMD[channelo]="ssh channelo 'systemctl --user restart untether'"
POSTCHECK_CMD[channelo]="ssh channelo 'systemctl --user is-active untether'"

RESTART_CMD[sl]="ssh sl 'systemctl --user restart untether'"
POSTCHECK_CMD[sl]="ssh sl 'systemctl --user is-active untether'"

RESTART_CMD[mac]='ssh mac "launchctl kickstart -k gui/\$(id -u)/com.littlebearapps.untether"'
POSTCHECK_CMD[mac]='ssh mac "launchctl print gui/\$(id -u)/com.littlebearapps.untether | grep -E \"^\\s*(state|last exit code)\""'

# ────────────────────────────────────────────────────────────────────────────
# Host selection
# ────────────────────────────────────────────────────────────────────────────

ALL_HOSTS=(lba-1 nsd channelo sl mac)

if [[ -n "$ONLY_HOST" ]]; then
    # Validate the host name against ALL_HOSTS (lba-1 is always known;
    # nsd/channelo/mac need to be in the static list).
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

# ────────────────────────────────────────────────────────────────────────────
# Probe install manager for each remote host (sequential — fast on tailnet)
# ────────────────────────────────────────────────────────────────────────────

echo
echo "Probing install managers per host..."
for host in "${HOSTS[@]}"; do
    if [[ "$host" == "lba-1" ]]; then
        # Already set above — local host always uses staging.sh
        printf "  %-10s → %s\n" "$host" "${MANAGER[lba-1]}"
        continue
    fi
    mgr=$(detect_install_manager "$host")
    MANAGER[$host]="$mgr"
    build_install_cmd "$host" "$mgr"
    printf "  %-10s → %s\n" "$host" "$mgr"
    if [[ "$mgr" == "none" ]]; then
        echo "WARN: $host has neither uv nor pipx; install will fail." >&2
    fi
done

# ────────────────────────────────────────────────────────────────────────────
# Dry-run preview
# ────────────────────────────────────────────────────────────────────────────

if (( DRY_RUN == 1 )); then
    echo
    echo "=== DRY RUN (no commands executed) ==="
    for host in "${HOSTS[@]}"; do
        echo
        echo "── ${host} (manager: ${MANAGER[$host]}) ──"
        echo "install:  ${INSTALL_CMD[$host]}"
        echo "restart:  ${RESTART_CMD[$host]}"
        echo "postcheck: ${POSTCHECK_CMD[$host]}"
    done
    echo
    echo "After rollout, manually verify each bot via Telegram /ping:"
    echo "  @hetz_lba1_bot    (lba-1)"
    echo "  @hetz_nsd_bot     (nsd)"
    echo "  @hetz_channelo_bot (channelo)"
    echo "  @hetz_sl_bot      (sl)"
    echo "  @local_mb_bot     (mac)"
    exit 0
fi

# ────────────────────────────────────────────────────────────────────────────
# Live rollout — fire installs+restarts in parallel
# ────────────────────────────────────────────────────────────────────────────

mkdir -p "$ATTESTATION_DIR"

# Note start in state file (best-effort, never blocks the rollout).
python3 - <<EOF || true
import json, os, time
state_path = "${STATE_FILE}"
hosts = "${HOSTS[*]}".split()
state = {
    "current_version": "${VERSION}",
    "started_at": int(time.time()),
    "hosts": {h: "in_flight" for h in hosts},
    "is_prerelease": ${IS_PRERELEASE},
    "skip_test_gate": ${SKIP_TEST_GATE},
}
os.makedirs(os.path.dirname(state_path), exist_ok=True)
with open(state_path, "w") as f:
    json.dump(state, f, indent=2)
EOF

declare -A PIDS
declare -A LOG_FILES

LOG_DIR=$(mktemp -d -t untether-fleet-rollout-XXXXXX)
echo "Per-host logs: $LOG_DIR"

# Helper: update a single host's status in the state file (read-modify-write).
update_state() {
    local host="$1" status="$2"
    python3 - "$STATE_FILE" "$host" "$status" <<'PY' || true
import json, sys, time
state_path, host, status = sys.argv[1:4]
try:
    with open(state_path) as f:
        state = json.load(f)
except Exception:
    state = {"hosts": {}}
state.setdefault("hosts", {})[host] = status
state["last_update"] = int(time.time())
with open(state_path, "w") as f:
    json.dump(state, f, indent=2)
PY
}

for host in "${HOSTS[@]}"; do
    LOG_FILES[$host]="$LOG_DIR/${host}.log"
    (
        rc=0
        {
            echo "=== install ==="
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

# Heartbeat — print which hosts are still in-flight every 15s so a long
# run doesn't look hung to the operator (and so harnesses with idle
# time-caps don't SIGKILL the script).
heartbeat_pid=""
heartbeat() {
    while true; do
        sleep 15
        local still=()
        for h in "${HOSTS[@]}"; do
            if kill -0 "${PIDS[$h]}" 2>/dev/null; then still+=("$h"); fi
        done
        if (( ${#still[@]} == 0 )); then break; fi
        echo "[heartbeat $(date -u +%H:%M:%S)] in-flight: ${still[*]}"
    done
}
heartbeat &
heartbeat_pid=$!

# Wait for all parallel branches and collect exit codes; update state file
# per host as it finishes so an operator inspecting state.json mid-run sees
# accurate per-host status.
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

# Stop the heartbeat (still running because its `kill -0` loop hasn't
# noticed yet). Suppress any "no such process" if the loop already exited.
if [[ -n "$heartbeat_pid" ]]; then
    kill "$heartbeat_pid" 2>/dev/null || true
    wait "$heartbeat_pid" 2>/dev/null || true
fi

# ────────────────────────────────────────────────────────────────────────────
# Result summary
# ────────────────────────────────────────────────────────────────────────────

OK_COUNT=0
FAIL_COUNT=0
echo
echo "=== fleet rollout results — version ${VERSION} ==="
for host in "${HOSTS[@]}"; do
    if (( EXITS[$host] == 0 )); then
        echo "  ${host}: OK"
        OK_COUNT=$((OK_COUNT + 1))
    else
        echo "  ${host}: FAILED (exit ${EXITS[$host]}; log ${LOG_FILES[$host]})"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

# Mark the rollout as finished in the state file (per-host statuses were
# already written by update_state() in the wait loop above).
python3 - "$STATE_FILE" <<'PY' || true
import json, sys, time
state_path = sys.argv[1]
try:
    with open(state_path) as f:
        state = json.load(f)
except Exception:
    state = {}
state["finished_at"] = int(time.time())
with open(state_path, "w") as f:
    json.dump(state, f, indent=2)
PY

cat <<EOF

Next steps:
  - Confirm versions landed:  scripts/fleet-status.sh   (all 5 hosts, one shot)
  - Verify each bot answers:  run the /ping sweep in
      docs/reference/fleet-ping-verification.md  (Claude-driven via Telegram MCP)
  - Inspect per-host logs in $LOG_DIR for any unexpected output.
  - If a host failed: investigate and either roll forward (rerun this script)
    or roll that host back: scripts/fleet-rollback.sh <prev> --only <host>

NOT done automatically:
  - Telegram /ping checks (send them via the playbook above — the MCP tools live
    inside Claude Code, not this shell).
  - Rollback of successful hosts on partial failure.
EOF

# Exit non-zero if any host failed so CI / chained scripts can react.
if (( FAIL_COUNT > 0 )); then
    exit 5
fi
