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
# With -q it prints the base branch (release-guard-mcp.sh); otherwise JSON,
# with a `url` in the repo gh "resolved" (STUB_GH_REPO, default untether; #917).
# `gh alias list` prints a fixed set of aliases for the #917 alias checks.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/bin/bash
if [ "${1:-} ${2:-}" = "alias list" ]; then
  cat <<'ALIASES'
co: pr checkout
issue mine: issue list --mention @me
land: pr merge --squash --admin
multi: |-
    pr merge
    --admin
sh1: '!gh pr merge "$1" --admin'
ALIASES
  exit 0
fi
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
if $q; then
  echo "$base"
else
  echo "$json" | jq -c --arg u "https://github.com/${STUB_GH_REPO:-littlebearapps/untether}/pull/$n" '. + {url: $u}'
fi
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
bash_check "git push origin v0.36.0"                      deny
bash_check "git push origin refs/tags/v0.36.0"            deny
bash_check "git push -u origin feature/x"                 allow
bash_check "git push -u origin fix/main-menu"             allow
bash_check "git push -u origin feature/v0.36.0rc1"       allow
bash_check "git push origin dev"                          allow
# #1236: prose mentioning push with an unbalanced quote must not trip xargs
bash_check "sed -i 's/old/dev recreated via git push it'\"'\"'s fine/' notes.md"  allow
bash_check "echo \"we push it's done\""                   allow
bash_check "git push origin \"feature/x\""                allow
# #1236 hardening: on master every push without a feature target is denied
git -C "$TMP" init -q -b master onmaster 2>/dev/null
cd "$TMP/onmaster" || exit 2
bash_check "git push origin feature/x; git push"          deny
bash_check "git push origin @"                            deny
bash_check "git push origin \"HEAD\""                     deny
bash_check "git push -u origin feature/x"                 allow
cd "$TMP/repo" || exit 2
# After cd / git -C the hook can't see the branch: a push needs a target
bash_check "cd ../onmaster && git push"                   deny
bash_check "git -C ../onmaster push"                      deny
bash_check "cd ../onmaster && git push origin HEAD"       deny
bash_check "cd ../repo && git push -u origin feature/x"   allow
bash_check "git push"                                     deny
# #1390: every push names a literal branch, wherever it runs
bash_check "git push -u origin HEAD"                     deny
bash_check "git push origin \$BRANCH"                    deny
bash_check "git push --force-with-lease"                 deny
bash_check "direnv exec . git push -q origin fix/x"      allow
bash_check "git push origin HEAD:feature/x"              allow
# text that only mentions push is not a push
bash_check "git commit -m 'fix the git push args'"      allow
bash_check "grep -rn 'git push' docs/"                  allow
# #1390 review: hidden pushes fail closed, git aliases, every ref
bash_check "if git push; then :; fi"                     deny
bash_check "git push origin fix/x; if git push; then :; fi"  deny
bash_check "\\git push"                                 deny
bash_check "git -c alias.p=push p"                       deny
bash_check "git config alias.p push"                     deny
bash_check "git push origin fix/x HEAD"                  deny
bash_check "git push -o ci.skip origin fix/x"            allow
bash_check "git stash push -m wip"                       allow
bash_check "if git push origin fix/stash; then :; fi"   deny
# an existing git alias for push is denied outright (it would skip the master checks)
git config alias.pp push
bash_check "git pp origin master"                        deny
bash_check "git pp origin fix/x"                         deny
git config --unset alias.pp
bash_check "direnv exec . git push -q origin fix/x 2>&1 | tail -1"  allow
bash_check "git push origin fix/x >/dev/null"            allow
bash_check "GIT_DIR=../onmaster/.git git push"            deny
bash_check "env -C ../onmaster git push"                  deny
bash_check "bash -c 'git push'"                           deny
bash_check "eval git push"                                deny
bash_check "git -c core.worktree=../onmaster push"        deny
bash_check "GIT_DIR=../onmaster/.git git push origin fix/y"  allow
bash_check "git tag v0.36.0"                              deny
bash_check "git tag -l"                                   allow
bash_check "gh release create v0.36.0"                    deny
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
bash_check "scripts/staging.sh install 0.36.0rc1 && systemctl --user restart untether" allow
bash_check "uv run pytest"                                allow

echo "== release-guard.sh: sibling paths (#917) =="
# 1. gh aliases — setting one, and using one that hides a merge/release.
bash_check "gh alias set x y"                             deny
bash_check "gh alias set land 'pr merge --squash --admin'" deny
bash_check "gh alias import aliases.yml"                  deny
bash_check "direnv exec . gh alias set x 'release create'" deny
bash_check "true && gh alias set x y"                     deny
bash_check "bash -c 'gh alias set x y'"                   deny
bash_check "echo \"\$(gh alias set x y)\""                deny
bash_check "gh land 2"                                    deny
bash_check "gh sh1 2"                                     deny
bash_check "gh multi 2"                                   deny
bash_check "gh alias list"                                allow
bash_check "gh alias delete land"                         allow
bash_check "gh co 12"                                     allow
bash_check "gh issue mine"                                allow
bash_check "gh issue comment 5 --body 'do not run gh alias set here'" allow
bash_check "gh issue comment 5 --body 'then: gh land 2'"  allow
bash_check "git commit -m \"docs: explain why gh alias set is blocked\"" allow
# Quoted text and heredoc bodies that only mention a command are not the command.
check "PR body heredoc mentioning alias/edit/curl" allow release-guard.sh "$(bash_in $'gh pr create --base dev --title x --body "$(cat <<\'EOF\'\nRun `gh alias set` never.\ngh pr edit 7 --base master\ncurl -X PUT https://api.github.com/x\nEOF\n)"')"
check "commit message heredoc mentioning the token command" allow release-guard.sh "$(bash_in $'git commit -m "$(cat <<\'EOF\'\ndocs: guard notes\n\ngh auth token asks; "gh alias set" is denied.\nEOF\n)"')"
check "heredoc written to a file" allow release-guard.sh "$(bash_in $'cat > /tmp/notes.md <<\'EOF\'\ngh alias set x y\nEOF')"
check "heredoc fed to a shell" deny release-guard.sh "$(bash_in $'bash <<\'EOF\'\ngh alias set x y\nEOF')"
check "substitution inside an unquoted heredoc" deny release-guard.sh "$(bash_in $'gh pr comment 5 --body "$(cat <<EOF\n$(gh alias set x y)\nEOF\n)"')"
bash_check "echo 'say \"hi'; gh alias set a \"b\""        deny
bash_check "gh pr comment 5 --body \"it's fine; gh alias set x\"" allow
# 2. Retargeting a PR onto master.
bash_check "gh pr edit 7 --base master"                   deny
bash_check "gh pr edit 7 --base=main"                     deny
bash_check "gh pr edit 7 -B master"                       deny
bash_check "gh pr edit 7 --base \"master\""               deny
bash_check "gh pr edit 7 --base \"\$TARGET\""             deny
bash_check "gh pr edit --base master 7 --title x"         deny
bash_check "gh pr -R littlebearapps/untether edit 7 --base master" deny
bash_check "echo 7 | xargs -I{} gh pr edit {} --base master" deny
bash_check "for n in 7 8; do gh pr edit \$n --base master; done" deny
bash_check "gh api -X PATCH repos/o/r/pulls/7 -f base=master" deny
bash_check "gh api repos/{owner}/{repo}/pulls/7 -X PATCH -f 'base=main'" deny
bash_check "gh api -X PATCH repos/o/r/pulls/7 -F base=@b.txt" deny
bash_check "gh api -X PATCH repos/o/r/pulls/7 --input body.json" ask
bash_check "gh api graphql -f query='mutation { updatePullRequest(input:{pullRequestId:\"x\", baseRefName:\"master\"}) { clientMutationId } }'" deny
bash_check "gh pr edit 7 --base dev"                      allow
bash_check "gh pr edit 7 --base feature/main-menu"        allow
bash_check "gh pr edit 7 --title 'retarget to master'"    allow
bash_check "gh pr edit 7 --body 'was: gh pr edit 7 --base master'" allow
bash_check "gh pr create --base master --head dev --title 'release: v9.9.9'" allow
bash_check "gh api -X PATCH repos/o/r/pulls/7 -f base=dev" allow
bash_check "gh api -X PATCH repos/o/r/pulls/7 -f title=x" allow
bash_check "gh api repos/o/r/pulls/7/comments -f body='set base=master'" allow
bash_check "gh api graphql -f query='{ repository(owner:\"o\",name:\"r\"){ pullRequest(number:1){ baseRefName } } }'" allow
# 3. curl/wget writes to the GitHub API, and reading the gh token.
bash_check "curl -X PUT -H \"Authorization: Bearer \$(gh auth token)\" https://api.github.com/repos/o/r/pulls/2/merge" deny
bash_check "curl -s -H \"Authorization: token \$GH_TOKEN\" -d '{\"tag_name\":\"v1\"}' https://api.github.com/repos/o/r/releases" deny
bash_check "curl -sXPOST https://api.github.com/repos/o/r/git/refs --json '{}'" deny
bash_check "curl -H \"Authorization: Bearer \$(gh auth token | tr -d x)\" https://api.github.com/repos/o/r/pulls/2/merge -X PUT" deny
bash_check "T=\$(gh auth token); curl -X PATCH -H \"Authorization: Bearer \$T\" 'https://api.github.com/repos/o/r/pulls/7' -d '{\"base\":\"master\"}'" deny
bash_check "wget --method=PUT --header=\"Authorization: Bearer \$(gh auth token)\" https://api.github.com/repos/o/r/pulls/2/merge" deny
bash_check "wget --post-data='{}' https://api.github.com/repos/o/r/releases" deny
bash_check "gh auth token"                                ask
bash_check "GH_TOKEN=\$(gh auth token) some-tool"         ask
bash_check "gh auth status --show-token"                  ask
bash_check "gh auth status"                               allow
bash_check "curl -s https://api.github.com/repos/o/r/pulls/2" allow
bash_check "curl -s https://api.github.com/repos/o/r/pulls | grep -F dev" allow
bash_check "curl -X POST https://example.com/hook -d '{\"a\":1}'" allow
bash_check "curl -fsSL https://raw.githubusercontent.com/o/r/master/install.sh" allow
bash_check "wget -T 10 https://api.github.com/repos/o/r/releases/latest" allow
bash_check "gh issue comment 5 --body 'use curl -X PUT https://api.github.com/x'" allow
bash_check "git commit -m 'docs: never run gh auth token in a script'" allow
# 4. Repo resolution — gh may act on a repo other than origin.
bash_check "gh repo set-default banteg/takopi"            ask
bash_check "gh repo set-default --unset"                  ask
bash_check "gh repo set-default --view"                   allow
bash_check "gh repo view"                                 allow
STUB_GH_REPO=banteg/takopi check "gh resolves a dev PR to another repo (upstream remote)" deny release-guard.sh "$(bash_in "gh pr merge 1 --squash")"
STUB_GH_REPO=banteg/takopi check "gh resolves the release PR to another repo" deny release-guard.sh "$(bash_in "gh pr merge 2 --squash --admin")"

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
