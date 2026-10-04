#!/bin/bash
# test-release-guard.sh
# Unit tests for the release guard hooks (#915):
#   release-guard.sh (Bash), release-guard-protect.sh (Edit/Write),
#   release-guard-mcp.sh (GitHub MCP), and their registration in
#   .claude/settings.json.
# Run: bash .claude/hooks/tests/test-release-guard.sh
# Requires: jq. No network — `gh` is replaced by a stub on PATH.

set -uo pipefail

HOOKS="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$(cd "$HOOKS/.." && pwd)/settings.json"
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq required"; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Stub gh: `gh pr view N` reports base `dev` for PR 1, `master` otherwise.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/bin/bash
for a in "$@"; do [ "$a" = "1" ] && { echo dev; exit 0; }; done
echo master
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"

# A non-master git checkout so the "current branch" fallback never fires.
git -C "$TMP" init -q -b feature/test repo 2>/dev/null || git -C "$TMP" init -q repo
cd "$TMP/repo" || exit 2

PASS=0
FAIL=0

check() { # desc expected(deny|ask|allow) hook input-json
  local got
  got=$(printf '%s' "$4" | bash "$HOOKS/$3" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null || echo "ERROR")
  if [ "$got" = "$2" ]; then
    echo "  PASS: $1"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $1"; echo "        expected=[$2] actual=[$got]"; FAIL=$((FAIL + 1))
  fi
}

bash_in() { jq -nc --arg c "$1" '{tool_name:"Bash", tool_input:{command:$c}}'; }
bash_check() { check "$1" "$2" release-guard.sh "$(bash_in "$1")"; }
file_in() { jq -nc --arg p "$1" --arg s "${2:-x}" '{tool_name:"Edit", tool_input:{file_path:$p, new_string:$s}}'; }

echo "== release-guard.sh =="
bash_check "git push origin master"                       deny
bash_check "git push -u origin main"                      deny
bash_check "git push origin HEAD:master"                  deny
bash_check "git push origin feature/x:refs/heads/main"    deny
bash_check "git push --tags"                              deny
bash_check "git push origin v0.35.5"                      deny
bash_check "git push origin refs/tags/v0.35.5"            deny
bash_check "git push -u origin feature/x"                 allow
bash_check "git push -u origin fix/main-menu"             allow
bash_check "git push -u origin feature/v0.35.5rc19"       allow
bash_check "git push origin dev"                          allow
bash_check "git tag v0.35.5"                              deny
bash_check "git tag -l"                                   allow
bash_check "gh release create v0.35.5"                    deny
bash_check "gh pr merge 1 --squash"                       allow
bash_check "gh pr merge 2 --squash"                       deny
bash_check "gh pr merge --squash"                         deny
bash_check "gh api repos/o/r/pulls/2/merge -X PUT"        deny
bash_check "gh api repos/o/r/releases -f tag_name=v1"     deny
bash_check "gh api repos/o/r/pulls/2"                     allow
bash_check "rm .claude/hooks/release-guard.sh"            deny
bash_check "sed -i 's/a/b/' .claude/settings.json"        deny
bash_check "git checkout HEAD~1 -- .claude/settings.json" deny
bash_check "cat .claude/settings.json"                    allow
bash_check "python3 -c 1 ~/.claude/settings.json"         allow
bash_check "jq . ~/.claude/settings.json > /tmp/x && echo '{\"disableAllHooks\":true}' > /tmp/x" deny
bash_check "grep -rn disableAllHooks docs/"               allow
bash_check "systemctl --user restart untether"            ask
bash_check "systemctl --user restart untether.service"    ask
bash_check "ssh nsd 'systemctl --user restart untether'"  ask
bash_check "launchctl kickstart -k gui/501/com.littlebearapps.untether" ask
bash_check "systemctl --user restart untether-dev"        allow
bash_check "systemctl --user status untether"             allow
bash_check "scripts/staging.sh install 0.35.5rc19 && systemctl --user restart untether" allow
bash_check "uv run pytest"                                allow

echo "== release-guard-protect.sh =="
check "Edit release-guard.sh"            deny  release-guard-protect.sh "$(file_in "$PWD/.claude/hooks/release-guard.sh")"
check "Edit help-faq-protect.sh"         deny  release-guard-protect.sh "$(file_in "$PWD/.claude/hooks/help-faq-protect.sh")"
check "Edit project .claude/settings.json" deny release-guard-protect.sh "$(file_in "$PWD/.claude/settings.json")"
check "Edit worktree .claude/settings.json" deny release-guard-protect.sh "$(file_in "$PWD/.claude/worktrees/a/.claude/settings.json")"
check "Edit .claude/settings.local.json" allow release-guard-protect.sh "$(file_in "$PWD/.claude/settings.local.json" '"allow": []')"
check "disableAllHooks in settings.local.json" deny release-guard-protect.sh "$(file_in "$PWD/.claude/settings.local.json" '"disableAllHooks": true')"
check "Edit ~/.claude/settings.json"     allow release-guard-protect.sh "$(file_in "$HOME/.claude/settings.json" '"effortLevel": "high"')"
check "disableAllHooks in ~/.claude/settings.json" deny release-guard-protect.sh "$(jq -nc --arg p "$HOME/.claude/settings.json" '{tool_name:"Write", tool_input:{file_path:$p, content:"{\"disableAllHooks\": true}"}}')"
check "disableAllHooks mentioned in a doc" allow release-guard-protect.sh "$(file_in "$PWD/docs/x.md" 'disableAllHooks')"
check "Edit src file"                    allow release-guard-protect.sh "$(file_in "$PWD/src/untether/x.py")"

echo "== release-guard-mcp.sh =="
check "MCP merge PR into dev"     allow release-guard-mcp.sh '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"pullNumber":1}}'
check "MCP merge PR into master"  deny  release-guard-mcp.sh '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"pullNumber":2}}'
check "MCP push_files to master"  deny  release-guard-mcp.sh '{"tool_name":"mcp__github__push_files","tool_input":{"branch":"master"}}'
check "MCP push_files no branch"  deny  release-guard-mcp.sh '{"tool_name":"mcp__github__push_files","tool_input":{}}'
check "MCP push_files to feature" allow release-guard-mcp.sh '{"tool_name":"mcp__github__push_files","tool_input":{"branch":"fix/x"}}'

echo "== .claude/settings.json registration =="
reg() { # desc jq-filter
  if jq -e "$2" "$SETTINGS" >/dev/null 2>&1; then
    echo "  PASS: $1"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $1"; FAIL=$((FAIL + 1))
  fi
}
reg "release-guard.sh on Bash" '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | select(.command | test("release-guard\\.sh"))'
reg "help-faq-protect.sh on Bash" '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | select(.command | test("help-faq-protect\\.sh"))'
reg "release-guard-protect.sh on Edit|Write" '.hooks.PreToolUse[] | select(.matcher | test("Edit")) | select(.matcher | test("Write")) | .hooks[] | select(.command | test("release-guard-protect\\.sh"))'
reg "release-guard-mcp.sh on GitHub MCP writes" '.hooks.PreToolUse[] | select(.matcher | test("merge_pull_request")) | .hooks[] | select(.command | test("release-guard-mcp\\.sh"))'
reg "commands use \$CLAUDE_PROJECT_DIR" '[.hooks[][] | .hooks[] | .command | test("CLAUDE_PROJECT_DIR")] | all'
for cmd in $(jq -r '.hooks[][] | .hooks[] | .command' "$SETTINGS" | grep -oE '\.claude/hooks/[a-z-]+\.sh'); do
  if [ -x "$HOOKS/../../$cmd" ]; then
    echo "  PASS: $cmd exists and is executable"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $cmd missing or not executable"; FAIL=$((FAIL + 1))
  fi
done

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
