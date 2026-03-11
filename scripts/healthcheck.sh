#!/usr/bin/env bash
# Post-deploy health check for Untether.
#
# Validates:
#   1. systemd service is active and not restart-looping
#   2. Installed version matches expected (optional --version arg)
#   3. No ERROR-level logs in the first 60 seconds after start
#   4. Bot is alive (Telegram Bot API getMe)
#
# Usage:
#   healthcheck.sh                    # check production
#   healthcheck.sh --dev              # check dev instance
#   healthcheck.sh --version 0.35.0   # verify specific version
#
# Exit codes:
#   0 = all checks pass
#   1 = one or more checks failed

set -euo pipefail

SERVICE="untether.service"
EXPECTED_VERSION=""
CHECKS_PASSED=0
CHECKS_FAILED=0

pass() { echo "OK: $1"; ((CHECKS_PASSED++)); }
fail() { echo "FAIL: $1"; ((CHECKS_FAILED++)); }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev)
            SERVICE="untether-dev.service"
            shift
            ;;
        --version)
            EXPECTED_VERSION="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--dev] [--version X.Y.Z]"
            exit 1
            ;;
    esac
done

echo "Checking service: $SERVICE"
echo "---"

# 1. Service is active
if systemctl --user is-active --quiet "$SERVICE"; then
    pass "service is active"
else
    fail "service is not active ($(systemctl --user is-active "$SERVICE" 2>/dev/null || echo 'unknown'))"
fi

# 2. Not in a restart loop (check NRestarts)
RESTARTS=$(systemctl --user show "$SERVICE" --property=NRestarts --value 2>/dev/null || echo "?")
if [[ "$RESTARTS" == "0" || "$RESTARTS" == "?" ]]; then
    pass "no restarts detected (NRestarts=$RESTARTS)"
else
    fail "service has restarted $RESTARTS time(s) — possible crash loop"
fi

# 3. Version check
if [[ -n "$EXPECTED_VERSION" ]]; then
    INSTALLED_VERSION=$(untether --version 2>/dev/null | grep -oP '[\d.]+' | head -1 || echo "unknown")
    if [[ "$INSTALLED_VERSION" == "$EXPECTED_VERSION" ]]; then
        pass "version matches: $INSTALLED_VERSION"
    else
        fail "version mismatch: expected $EXPECTED_VERSION, got $INSTALLED_VERSION"
    fi
fi

# 4. Recent errors (last 60 seconds)
ERROR_COUNT=$(journalctl --user -u "$SERVICE" -S "-60s" --no-pager -p err 2>/dev/null | grep -c . || true)
if [[ "$ERROR_COUNT" -eq 0 ]]; then
    pass "no ERROR-level log entries in last 60s"
else
    fail "$ERROR_COUNT ERROR-level log entries in last 60s"
    journalctl --user -u "$SERVICE" -S "-60s" --no-pager -p err 2>/dev/null | head -5
fi

# 5. Bot API liveness (getMe)
CONFIG_DIR="$HOME/.untether"
if [[ "$SERVICE" == "untether-dev.service" ]]; then
    CONFIG_DIR="$HOME/.untether-dev"
fi

BOT_TOKEN=""
if [[ -f "$CONFIG_DIR/untether.toml" ]]; then
    BOT_TOKEN=$(grep -oP 'bot_token\s*=\s*"\K[^"]+' "$CONFIG_DIR/untether.toml" 2>/dev/null || true)
fi

if [[ -n "$BOT_TOKEN" ]]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 5 --max-time 10 \
        "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "200" ]]; then
        pass "Telegram Bot API getMe returned 200"
    else
        fail "Telegram Bot API getMe returned HTTP $HTTP_CODE"
    fi
else
    echo "SKIP: no bot_token found in $CONFIG_DIR/untether.toml — skipping API liveness check"
fi

# Summary
echo "---"
echo "$CHECKS_PASSED passed, $CHECKS_FAILED failed"
[[ "$CHECKS_FAILED" -eq 0 ]]
