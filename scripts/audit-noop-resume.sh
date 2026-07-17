#!/usr/bin/env bash
# Fleet-correlation audit for the no-op empty-resume recovery path (#634).
#
# Companion to docs/plans/2026-07-16-noop-resume-remediation/04-test-strategy.md
# (Layer 4 — "Fleet-correlation verification"). Correlates five structlog
# event names emitted around the dangling-tool_use -> empty-resume shape:
#
#   runner.empty_result            - a resume returned 0 turns / no work
#                                     (the symptom)
#   session.auto_resend_fresh      - W1: quarantined the poisoned session id
#                                     and retried the ORIGINAL prompt fresh
#                                     (the fix — this is what should follow
#                                     every runner.empty_result)
#   session.auto_resend_empty      - legacy same-session resend path, only
#                                     taken when the `empty_resume_fresh`
#                                     flag is off
#   session.quarantined            - W2: forced-teardown recorded a session
#                                     as unsafe to resume
#   session.resume_diverted_fresh  - a later message on an already-
#                                     quarantined session was proactively
#                                     diverted to a fresh session
#
# For each host, every `runner.empty_result` SHOULD be followed
# (chronologically, matched by session id where present) by a
# `session.auto_resend_fresh`. Any that isn't is a regression signal — the
# recovery path failed to fire. rc7 success per the plan doc: zero such
# unrecovered empty_results across the fleet.
#
# Read-only — this script never touches any host's state, it only reads
# journalctl over the tailnet SSH mesh (same alias convention as
# fleet-status.sh / fleet-rollout.sh).
#
# Usage:
#   scripts/audit-noop-resume.sh <host...>
#   scripts/audit-noop-resume.sh lba-1 nsd channelo sl mac
#   scripts/audit-noop-resume.sh lba-1 --unit untether-dev --since "2 hours ago"
#
# lba-1 is queried locally (no ssh hop); every other host name is reached via
# `ssh <host>`. Unreachable hosts are warned about on stderr and skipped —
# one bad host never aborts the rest of the sweep.
#
# Exit codes:
#   0 = no regression signal on any reachable host
#   1 = at least one runner.empty_result had no subsequent recovery
#   3 = no host in the argument list was reachable at all

set -euo pipefail

SINCE="24 hours ago"
UNIT="untether"
HOSTS=()

usage() {
    cat <<EOF
Usage: audit-noop-resume.sh <host...> [--since "24 hours ago"] [--unit untether]

Fleet-correlation audit for the no-op empty-resume recovery path (#634).
Reads journalctl --user logs on each host and reports whether every
runner.empty_result was followed by a session.auto_resend_fresh recovery.

Arguments:
  host...       One or more hosts to audit. lba-1 runs locally; any other
                name is reached via 'ssh <host>' (tailnet alias).

Options:
  --since STR   journalctl --since value (default: "24 hours ago")
  --unit NAME   systemd --user unit to query (default: untether;
                pass --unit untether-dev to audit the dev instance)
  --help, -h    Show this help

Companion doc: docs/plans/2026-07-16-noop-resume-remediation/04-test-strategy.md (Layer 4)
EOF
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage 0 ;;
        --since) SINCE="${2:?--since requires a value}"; shift 2 ;;
        --unit) UNIT="${2:?--unit requires a unit name}"; shift 2 ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *) HOSTS+=("$1"); shift ;;
    esac
done

if [[ ${#HOSTS[@]} -eq 0 ]]; then
    echo "ERROR: at least one host is required." >&2
    usage 1
fi

SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes)

TMP=$(mktemp -d -t untether-noop-resume-audit-XXXXXX)
trap 'rm -rf "$TMP"' EXIT

echo "Auditing no-op empty-resume recovery — unit=$UNIT since=\"$SINCE\""
echo "Hosts: ${HOSTS[*]}"
echo

UNREACHABLE=()
REACHABLE=()

for host in "${HOSTS[@]}"; do
    logfile="$TMP/$host.log"
    errfile="$TMP/$host.err"
    rc=0
    if [[ "$host" == "lba-1" ]]; then
        journalctl --user -u "$UNIT" --since "$SINCE" -o short-iso \
            >"$logfile" 2>"$errfile" || rc=$?
    else
        # Build the remote command as a single properly-quoted string —
        # $SINCE contains spaces, and ssh joins trailing argv with spaces
        # before handing it to the remote shell, so passing $UNIT/$SINCE as
        # separate ssh arguments would silently mis-split "24 hours ago".
        remote_cmd="journalctl --user -u ${UNIT@Q} --since ${SINCE@Q} -o short-iso"
        ssh "${SSH_OPTS[@]}" "$host" "$remote_cmd" \
            >"$logfile" 2>"$errfile" || rc=$?
    fi
    if (( rc != 0 )); then
        UNREACHABLE+=("$host")
        err_snippet=$(tail -1 "$errfile" 2>/dev/null || true)
        echo "WARNING: $host unreachable or journalctl failed (rc=$rc)${err_snippet:+: $err_snippet}" >&2
        continue
    fi
    REACHABLE+=("$host")
done

if [[ ${#REACHABLE[@]} -eq 0 ]]; then
    echo "ERROR: no hosts were reachable." >&2
    exit 3
fi

# ── Correlate + render. One python3 process, all hosts — matches
#    fleet-status.sh's pattern of doing table/JSON formatting in python for
#    correct parsing instead of ad-hoc awk over structlog's ConsoleRenderer
#    output (`TIMESTAMP [level  ] event.name  key=value key=value ...`).
py_rc=0
python3 - "$TMP" "${REACHABLE[@]}" <<'PY' || py_rc=$?
import re
import sys
from pathlib import Path

tmp = Path(sys.argv[1])
hosts = sys.argv[2:]

EVENTS = (
    "runner.empty_result",
    "session.auto_resend_fresh",
    "session.auto_resend_empty",
    "session.quarantined",
    "session.resume_diverted_fresh",
)
KV_RE = re.compile(r"(\w+)=(\S*)")
TS_RE = re.compile(r"^(\S+)")  # journalctl -o short-iso: first token is its own timestamp

# Which structlog field carries the session id, per event (see
# runner_bridge.py / session_quarantine.py — the field name isn't uniform).
SESSION_FIELDS = ("resume", "old_session_id", "session_id")


def parse_host(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(errors="replace").splitlines():
        event = next((e for e in EVENTS if e in line), None)
        if event is None:
            continue
        kv = dict(KV_RE.findall(line))
        sid = None
        for field in SESSION_FIELDS:
            val = kv.get(field)
            if val and val != "None":
                sid = val
                break
        ts_match = TS_RE.match(line)
        records.append(
            {
                "event": event,
                "ts": ts_match.group(1) if ts_match else "?",
                "session_id": sid,
                "raw": line.strip(),
            }
        )
    return records  # journalctl output is already chronological


def correlate(records: list[dict]) -> dict:
    """For every runner.empty_result, look forward (chronologically) for a
    matching recovery event. Matched by session id when both sides have
    one; falls back to "any later recovery event" when the id is missing
    on either side (still useful signal, just less precise)."""
    fresh_recovered = []
    legacy_recovered = []  # recovered via the flag-off legacy path, not a
    # regression but worth surfacing separately since it means
    # empty_resume_fresh is off on this host
    unrecovered = []

    for i, r in enumerate(records):
        if r["event"] != "runner.empty_result":
            continue
        sid = r["session_id"]
        later = records[i + 1 :]
        if any(
            e["event"] == "session.auto_resend_fresh"
            and (sid is None or e["session_id"] == sid)
            for e in later
        ):
            fresh_recovered.append(r)
        elif any(
            e["event"] == "session.auto_resend_empty"
            and (sid is None or e["session_id"] == sid)
            for e in later
        ):
            legacy_recovered.append(r)
        else:
            unrecovered.append(r)

    return {
        "empty_total": sum(1 for r in records if r["event"] == "runner.empty_result"),
        "fresh_total": sum(1 for r in records if r["event"] == "session.auto_resend_fresh"),
        "quarantined_total": sum(1 for r in records if r["event"] == "session.quarantined"),
        "diverted_total": sum(
            1 for r in records if r["event"] == "session.resume_diverted_fresh"
        ),
        "fresh_recovered": fresh_recovered,
        "legacy_recovered": legacy_recovered,
        "unrecovered": unrecovered,
    }


host_stats = {host: correlate(parse_host(tmp / f"{host}.log")) for host in hosts}

header = f"{'HOST':<12} {'EMPTY':>6} {'RECOVERED':>10} {'RATE':>6} {'QUARANTINE':>11} {'DIVERT':>7} {'REGRESSION':>10}"
print(header)
print("-" * len(header))

any_regression = False
for host in hosts:
    s = host_stats[host]
    empty = s["empty_total"]
    recovered = len(s["fresh_recovered"]) + len(s["legacy_recovered"])
    rate = f"{(recovered / empty * 100):.0f}%" if empty else "n/a"
    regression = len(s["unrecovered"])
    if regression:
        any_regression = True
    print(
        f"{host:<12} {empty:>6} {recovered:>10} {rate:>6} "
        f"{s['quarantined_total']:>11} {s['diverted_total']:>7} {regression:>10}"
    )
    if s["legacy_recovered"]:
        print(
            f"    note: {len(s['legacy_recovered'])} recovered via the legacy "
            "session.auto_resend_empty path (empty_resume_fresh flag off on this host)"
        )

print()
if any_regression:
    print("REGRESSION SIGNAL — runner.empty_result with NO subsequent recovery:")
    for host in hosts:
        for r in host_stats[host]["unrecovered"]:
            print(f"  [{host}] {r['ts']}  session={r['session_id'] or '?'}")
            print(f"      {r['raw']}")
    sys.exit(1)

print("No regressions detected across reachable hosts — every runner.empty_result recovered.")
PY

if [[ ${#UNREACHABLE[@]} -gt 0 ]]; then
    echo >&2
    echo "Unreachable/skipped hosts: ${UNREACHABLE[*]}" >&2
fi

exit "$py_rc"
