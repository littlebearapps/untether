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

# Stub gh for `gh pr view N`:
#   1 → base dev            2 → dev→master, CI green    3 → dev→master, a check pending
#   4 → feature→master      5 → dev→master, no checks   other → lookup fails
# With -q it prints the base branch (release-guard-mcp.sh); otherwise JSON.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/bin/bash
n=""; q=false
for a in "$@"; do
  case "$a" in -q) q=true ;; *) [[ -z "$n" && "$a" =~ ^[0-9]+$ ]] && n="$a" ;; esac
done
green='[{"conclusion":"SUCCESS"},{"conclusion":"SKIPPED"},{"state":"SUCCESS"}]'
case "$n" in
  1) base=dev;    json='{"baseRefName":"dev","headRefName":"fix/x","title":"fix","statusCheckRollup":[]}' ;;
  2) base=master; json='{"baseRefName":"master","headRefName":"dev","title":"release: v9.9.9","statusCheckRollup":'"$green"'}' ;;
  3) base=master; json='{"baseRefName":"master","headRefName":"dev","title":"release: v9.9.9","statusCheckRollup":[{"conclusion":"SUCCESS"},{"conclusion":null,"status":"IN_PROGRESS"}]}' ;;
  4) base=master; json='{"baseRefName":"master","headRefName":"feature/x","title":"feat","statusCheckRollup":'"$green"'}' ;;
  5) base=master; json='{"baseRefName":"master","headRefName":"dev","title":"release: v9.9.9","statusCheckRollup":[]}' ;;
  *) exit 1 ;;
esac
if $q; then echo "$base"; else echo "$json"; fi
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"

# A non-master git checkout so the "current branch" fallback never fires, with
# an untether origin so PR merges pass the checkout check. A second checkout
# with another origin stands in for "a different repo".
git -C "$TMP" init -q -b feature/test repo 2>/dev/null || git -C "$TMP" init -q repo
git -C "$TMP/repo" remote add origin https://github.com/littlebearapps/untether.git
git -C "$TMP" init -q other
git -C "$TMP/other" remote add origin https://github.com/littlebearapps/other.git
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

bash_in() { jq -nc --arg c "$1" --arg d "${2:-$PWD}" '{tool_name:"Bash", cwd:$d, tool_input:{command:$c}}'; }
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
bash_check "gh pr merge 2 --squash --admin"               ask
bash_check "gh pr merge 2 --squash --admin --delete-branch" deny
bash_check "gh pr merge 3 --squash --admin"               deny
bash_check "gh pr merge 4 --squash --admin"               deny
bash_check "gh pr merge 5 --squash --admin"               deny
bash_check "gh pr merge 99 --squash"                      deny
bash_check "gh pr merge --squash"                         deny
bash_check "gh workflow run release.yml --ref v9.9.9"     ask
bash_check "gh workflow run ci.yml"                       ask
bash_check "gh pr merge 1 --squash && gh pr merge 2 --squash --admin" deny
bash_check "gh -R littlebearapps/untether pr merge 2 --squash --admin" deny
bash_check "gh --repo=littlebearapps/untether pr merge 3 --squash --admin" deny
bash_check "gh -R littlebearapps/untether pr merge 1 --squash" deny
bash_check "gh -R littlebearapps/untether workflow run release.yml" ask
bash_check "gh -R littlebearapps/untether release create v9.9.9" deny
bash_check "gh api graphql -f query='mutation { mergePullRequest(input:{}) { clientMutationId } }'" deny
bash_check "gh api graphql -f query='mutation { createRef(input:{}) { clientMutationId } }'" deny
bash_check "gh api graphql -f query='{ viewer { login } }'" allow
bash_check "gh -R x;git push origin master"               deny
bash_check "gh -R a/b&&git push origin master"            deny
bash_check "gh -R x;gh release create v9.9.9"             deny
bash_check "gh -R \$REPO pr merge 2 --squash --admin"     deny
bash_check "gh -R \"littlebearapps/untether\" pr merge 2 --squash --admin" deny
bash_check "gh -R github.com/littlebearapps/untether pr merge 2 --squash --admin" deny
bash_check "gh pr view 2 -R littlebearapps/untether"      allow
bash_check "gh pr -R littlebearapps/untether merge 2 --squash --admin" deny
bash_check "gh pr --repo=littlebearapps/untether merge 3 --squash --admin" deny
bash_check "gh workflow -R littlebearapps/untether run release.yml" ask
bash_check "gh release -R littlebearapps/untether create v9.9.9" deny
bash_check "gh pr merge 2 --squash --admin -R littlebearapps/untether" deny
bash_check "GH_REPO=littlebearapps/other gh pr merge 1 --squash" deny
bash_check "gh pr merge 1 --squash --repo littlebearapps/other" deny
bash_check "cd ../other && gh pr merge 1 --squash"       deny
bash_check "gh pr merge 1 -s"                             allow
bash_check "gh pr merge 1 --squash -Rlittlebearapps/other" deny
bash_check "env GH_REPO=littlebearapps/other gh pr merge 1 --squash" deny
bash_check "gh pr merge 2 --squash --admin --body x"      deny
bash_check "gh pr checkout 5 && git merge dev"            deny
bash_check "gh workflow run 123456 --ref v9.9.9"          ask
bash_check "gh run rerun 123456"                          ask
check "gh pr merge from another repo's checkout" deny release-guard.sh "$(bash_in "gh pr merge 1 --squash" "$TMP/other")"
check "gh pr merge from a non-repo dir"          deny release-guard.sh "$(bash_in "gh pr merge 1 --squash" "$TMP")"
GH_REPO=littlebearapps/other check "GH_REPO set in the session env" deny release-guard.sh "$(bash_in "gh pr merge 1 --squash")"
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
check "Edit .claude/./settings.json"     deny  release-guard-protect.sh "$(file_in "$PWD/.claude/./settings.json")"
check "Edit .claude//settings.json"      deny  release-guard-protect.sh "$(file_in "$PWD/.claude//settings.json")"
check "Edit .claude/hooks/../settings.json" deny release-guard-protect.sh "$(file_in "$PWD/.claude/hooks/../settings.json")"
check "Edit .claude/settings.local.json" allow release-guard-protect.sh "$(file_in "$PWD/.claude/settings.local.json" '"allow": []')"
check "disableAllHooks in settings.local.json" deny release-guard-protect.sh "$(file_in "$PWD/.claude/settings.local.json" '"disableAllHooks": true')"
check "Edit ~/.claude/settings.json"     allow release-guard-protect.sh "$(file_in "$HOME/.claude/settings.json" '"effortLevel": "high"')"
check "disableAllHooks in ~/.claude/settings.json" deny release-guard-protect.sh "$(jq -nc --arg p "$HOME/.claude/settings.json" '{tool_name:"Write", tool_input:{file_path:$p, content:"{\"disableAllHooks\": true}"}}')"
check "disableAllHooks mentioned in a doc" allow release-guard-protect.sh "$(file_in "$PWD/docs/x.md" 'disableAllHooks')"
check "Edit src file"                    allow release-guard-protect.sh "$(file_in "$PWD/src/untether/x.py")"

echo "== release-guard-mcp.sh =="
check "MCP merge PR into dev"     allow release-guard-mcp.sh '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"pullNumber":1}}'
check "MCP merge release PR (use gh pr merge)" deny release-guard-mcp.sh '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"pullNumber":2}}'
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
