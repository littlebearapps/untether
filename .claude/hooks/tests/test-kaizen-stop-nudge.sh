#!/bin/bash
# test-kaizen-stop-nudge.sh
# Unit tests for .claude/hooks/kaizen-stop-nudge.sh
# Run: bash .claude/hooks/tests/test-kaizen-stop-nudge.sh
# Requires: jq. No git, no network. Self-contained (uses temp fixtures).

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/kaizen-stop-nudge.sh"
[ -r "$HOOK" ] || { echo "FATAL: hook not found at $HOOK"; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq required"; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

assert_eq() { # desc expected actual
  if [ "$2" = "$3" ]; then
    echo "  PASS: $1"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $1"; echo "        expected=[$2] actual=[$3]"; FAIL=$((FAIL + 1))
  fi
}

decision() { echo "$1" | jq -r '.decision // "none"' 2>/dev/null || echo "PARSE_ERROR"; }

mkinput() { # transcript_path stop_active(true|false)
  jq -nc --arg tp "$1" --argjson sa "${2:-false}" \
    '{transcript_path:$tp, stop_hook_active:$sa}'
}

# Transcript-line builders (JSONL)
EDIT_LINE='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{}}]}}'
TEXT_LINE='{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
FRICTION_LINE='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"boom"}]}}'
KAIZEN_LINE='{"type":"assistant","message":{"content":[{"type":"text","text":"## /kaizen run report: 0 captures"}]}}'

# Fixtures
T_SUB="$TMP/sub.jsonl"          # 3 edits, no friction, kaizen not run
printf '%s\n%s\n%s\n%s\n' "$TEXT_LINE" "$EDIT_LINE" "$EDIT_LINE" "$EDIT_LINE" > "$T_SUB"

T_EMPTY="$TMP/empty.jsonl"      # 0 edits, no friction
printf '%s\n%s\n' "$TEXT_LINE" "$TEXT_LINE" > "$T_EMPTY"

T_TWO="$TMP/two.jsonl"          # 2 edits (below threshold), no friction
printf '%s\n%s\n' "$EDIT_LINE" "$EDIT_LINE" > "$T_TWO"

T_FRICTION="$TMP/friction.jsonl" # 1 edit + friction
printf '%s\n%s\n' "$EDIT_LINE" "$FRICTION_LINE" > "$T_FRICTION"

T_RAN="$TMP/ran.jsonl"         # 3 edits BUT kaizen already ran
printf '%s\n%s\n%s\n%s\n' "$EDIT_LINE" "$EDIT_LINE" "$EDIT_LINE" "$KAIZEN_LINE" > "$T_RAN"

run() { # stdin_json  [env assignment...]  -> echoes hook stdout
  local input="$1"; shift
  printf '%s' "$input" | env -u UNTETHER_SESSION "$@" bash "$HOOK"
}

echo "== kaizen-stop-nudge.sh tests =="

# 1. stop_hook_active short-circuits even on a substantive session
OUT=$(run "$(mkinput "$T_SUB" true)")
assert_eq "stop_hook_active -> allow stop" "none" "$(decision "$OUT")"

# 2. Untether session is skipped
OUT=$(printf '%s' "$(mkinput "$T_SUB" false)" | env UNTETHER_SESSION=1 bash "$HOOK")
assert_eq "UNTETHER_SESSION -> allow stop" "none" "$(decision "$OUT")"

# 3. Missing / unreadable transcript fails open
OUT=$(run "$(mkinput "$TMP/does-not-exist.jsonl" false)")
assert_eq "missing transcript -> allow stop (fail open)" "none" "$(decision "$OUT")"

# 4. Non-substantive, no friction -> no nudge
OUT=$(run "$(mkinput "$T_EMPTY" false)")
assert_eq "0 edits, no friction -> allow stop" "none" "$(decision "$OUT")"

# 5. Below edit threshold -> no nudge
OUT=$(run "$(mkinput "$T_TWO" false)")
assert_eq "2 edits (< 3) -> allow stop" "none" "$(decision "$OUT")"

# 6. Substantive (3 edits), kaizen not run -> nudge
OUT=$(run "$(mkinput "$T_SUB" false)")
assert_eq "3 edits -> block (nudge)" "block" "$(decision "$OUT")"
REASON=$(echo "$OUT" | jq -r '.reason // ""')
case "$REASON" in
  *"/kaizen"*) echo "  PASS: nudge reason mentions /kaizen"; PASS=$((PASS + 1)) ;;
  *) echo "  FAIL: nudge reason mentions /kaizen"; echo "        reason=[$REASON]"; FAIL=$((FAIL + 1)) ;;
esac

# 7. Friction (1 edit + tool error) -> nudge
OUT=$(run "$(mkinput "$T_FRICTION" false)")
assert_eq "friction (< 3 edits) -> block (nudge)" "block" "$(decision "$OUT")"

# 8. Substantive but /kaizen already ran -> no nudge
OUT=$(run "$(mkinput "$T_RAN" false)")
assert_eq "kaizen already ran -> allow stop" "none" "$(decision "$OUT")"

# 9. Custom threshold via env respected (KAIZEN_MIN_EDITS=2 makes T_TWO nudge)
OUT=$(printf '%s' "$(mkinput "$T_TWO" false)" | env -u UNTETHER_SESSION KAIZEN_MIN_EDITS=2 bash "$HOOK")
assert_eq "KAIZEN_MIN_EDITS=2 -> 2 edits blocks" "block" "$(decision "$OUT")"

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
