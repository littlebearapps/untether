#!/bin/bash
# release-guard.sh — PreToolUse hook for Bash tool
# Blocks direct pushes to master/main, tag creation and pushes, and
# `gh release create`. Merging the dev→master release PR is allowed only via
# `gh pr merge <n>` with green CI, and always asks Nathan to confirm (#917).
# Asks before restarting a non-dev Untether service.
# Also closes the sibling paths to a master merge (#917): gh aliases, retargeting
# a PR onto master, curl/wget writes to the GitHub API, and reading the gh token.
# Feature branch pushes are ALLOWED.
# Registered in .claude/settings.json (#915).
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail
# Fail closed: an internal error exits 2, which blocks the call (exit 1 would let it through).
trap 'echo "release-guard.sh: internal error at line $LINENO — blocked to be safe" >&2; exit 2' ERR

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$COMMAND" ] && echo '{}' && exit 0
# gh's global repo flag can sit before the subcommand (`gh -R o/r pr merge 2`).
# Drop it so every gh check below sees `gh <subcommand>`. The value must be a
# plain [HOST/]OWNER/REPO followed by whitespace, so the strip can never
# swallow shell syntax (`gh -R x;git push origin master`).
# It can also sit between the command group and the subcommand
# (`gh pr -R o/r merge 2`), so strip it in both places (two passes).
ORIG_COMMAND="$COMMAND"
GH_REPO_RE="(\s+(-R|--repo)(=|\s+)[\"']?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){0,2}[\"']?)+"
for _ in 1 2; do
  COMMAND=$(printf '%s' "$COMMAND" | sed -E "s#\bgh((\s+[a-z][a-z-]*)?)${GH_REPO_RE}(\s|\$)#gh\1\7#g")
done

BLOCKED=false
REASON=""

# Any other `gh [group] -R/--repo <value>` before the subcommand ($VAR, odd
# quoting, shell metacharacters) can't be checked reliably — fail closed.
if printf '%s' "$COMMAND" | grep -qP '\bgh(\s+[a-z][a-z-]*)?\s+(-R|--repo)\b'; then
  BLOCKED=true
  REASON="gh -R/--repo with a value that isn't a plain owner/repo is blocked. Use a literal owner/repo, or put -R after the subcommand."
fi
ASK=false
ASK_REASON=""

# A branch name is master/main only as a whole token: `origin main`,
# `HEAD:main`, `refs/heads/main` — never `fix/main-menu` or `maintenance`.
BRANCH_RE='(?<![\w/.-])(refs/heads/)?(master|main)(?![\w/.-])'

# ── git push — block only if targeting master/main ────────────────

if echo "$COMMAND" | grep -qPi '\bgit\b.*\bpush\b' && \
   ! echo "$COMMAND" | grep -qPi '\bgit\s+stash\b'; then

  # Broad operations that could affect master or trigger releases
  if echo "$COMMAND" | grep -qPi '\bpush\b.*--(all|mirror|tags|follow-tags)'; then
    BLOCKED=true
    REASON="git push with --all/--mirror/--tags/--follow-tags is blocked."
  fi

  # Explicitly mentions master/main as push target
  if echo "$COMMAND" | grep -qPi "\\bpush\\b.*${BRANCH_RE}"; then
    BLOCKED=true
    REASON="git push to master/main is blocked."
  fi

  # Refspec targeting master/main (e.g. HEAD:master, feature:refs/heads/main)
  if echo "$COMMAND" | grep -qP ':(refs/heads/)?(master|main)(?![\w/.-])'; then
    BLOCKED=true
    REASON="git push with refspec targeting master/main is blocked."
  fi

  # Pushing a version tag by name (git push origin v1.2.3 / refs/tags/...)
  if echo "$COMMAND" | grep -qP '\bpush\b.*((?<![\w/.-])v\d+\.\d+|refs/tags/)'; then
    BLOCKED=true
    REASON="git push of a version tag is blocked. Tags are created by auto-tag-on-master.yml."
  fi

fi

# ── git tag with version arg ─────────────────────────────────────

if echo "$COMMAND" | grep -qPi '\bgit\s+tag\b' && \
   echo "$COMMAND" | grep -qP 'v\d' && \
   ! echo "$COMMAND" | grep -qPi '\bgit\s+tag\s+(-[ldv]\b|--list|--delete|--verify)'; then
  BLOCKED=true
  REASON="git tag creation is blocked. Tags are created by auto-tag-on-master.yml when the release PR merges."
fi

# ── gh release create ────────────────────────────────────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+release\s+create\b'; then
  BLOCKED=true
  REASON="gh release create is blocked. release.yml creates the GitHub release when the release PR merges."
fi

# ── gh api writes that merge, release or move refs ───────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+api\b' && \
   echo "$COMMAND" | grep -qPi '/(pulls/\d+/merge|merges|releases|git/refs|git/tags)\b' && \
   echo "$COMMAND" | grep -qP '(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)\b'; then
  BLOCKED=true
  REASON="gh api writes to merge, release, tag or ref endpoints are blocked. Use gh pr merge <number>."
fi

# The GraphQL API reaches the same operations without a REST path.
if echo "$COMMAND" | grep -qPi '\bgh\s+api\s+graphql\b' && \
   echo "$COMMAND" | grep -qPi '\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|createRef|updateRefs?|deleteRef|createRelease|updateRelease)\b'; then
  BLOCKED=true
  REASON="gh api graphql merge, release or ref mutations are blocked. Use gh pr merge <number>."
fi

# Retargeting a PR onto master through GraphQL (the query is quoted text, so
# this one matches keywords like the check above).
if echo "$COMMAND" | grep -qPi '\bgh\s+api\s+graphql\b' && \
   echo "$COMMAND" | grep -qP '\bupdatePullRequest\b' && \
   echo "$COMMAND" | grep -qP '\bbaseRefName\b'; then
  BLOCKED=true
  REASON="Changing a PR's base branch through gh api graphql is blocked. Open the dev→master release PR with /pr-main instead."
fi

# ── Sibling paths to a master merge (#917, round 6) ──────────────
#
# The checks above match keywords anywhere in the command, so they also fire
# on text that only mentions them. The checks below match structure instead:
# STRUCT_COMMAND is the command with quoted free text blanked, and CMD_POS
# anchors a match to the start of a simple command (after `;`, `&&`, `|`,
# `$(`, env assignments or a wrapper such as `direnv exec .`).

# Single-quoted strings, and double-quoted strings with no command
# substitution, become "" — unless they are one plain word (a branch, URL or
# field=value), which is kept bare so `--base "master"` still matches. A
# double-quoted string holding `$(` or a backtick is kept, because it runs.
# Heredoc bodies are dropped the same way — including the usual
# `--body "$(cat <<'EOF' … EOF)"` — unless the delimiter is unquoted and the
# body holds a substitution. An unterminated quote or heredoc keeps the rest
# as-is (fail towards detection).
struct_command() {
  awk -v sq="'" '
    function word(b) {  # one plain word stays bare; anything else is blanked
      if (b ~ /^[A-Za-z0-9_.\/:=@%+,?&#~{}-]+$/) { gsub(/&/, "_", b); return b }
      return "\"\""
    }
    function hd_op(s, k,    q, e) {  # k is just past "<<"; returns the index after the delimiter word, 0 if none
      H_DASH = 0; H_QUOTED = 0; H_DELIM = ""
      if (substr(s, k, 1) == "-") { H_DASH = 1; k++ }
      while (substr(s, k, 1) == " " || substr(s, k, 1) == "\t") k++
      q = substr(s, k, 1)
      if (q == sq || q == "\"") {
        e = index(substr(s, k + 1), q)
        if (e < 2) return 0
        H_QUOTED = 1; H_DELIM = substr(s, k + 1, e - 1); return k + e + 1
      }
      if (q == "\\") { H_QUOTED = 1; k++ }
      if (!match(substr(s, k), /^[A-Za-z0-9_]+/)) return 0
      H_DELIM = substr(s, k, RLENGTH); return k + RLENGTH
    }
    function hd_body(s, p,    e, line, t) {  # p is the first body char; sets H_BODY, returns the index after the end line, 0 if none
      H_BODY = ""
      while (p <= length(s)) {
        e = index(substr(s, p), "\n")
        line = (e ? substr(s, p, e - 1) : substr(s, p))
        t = line; if (H_DASH) sub(/^\t+/, "", t)
        if (t == H_DELIM) return p + length(line)
        H_BODY = H_BODY line "\n"
        if (!e) return 0
        p += e
      }
      return 0
    }
    function hd_runs() { return !H_QUOTED && (index(H_BODY, "$(") || index(H_BODY, "`")) }
    function cat_heredoc(s, j,    k, a) {  # length of a `$(cat <<EOF … EOF)` unit starting at j, 0 if it is not one
      k = j + 2
      while (substr(s, k, 1) == " ") k++
      if (substr(s, k, 3) != "cat") return 0
      k += 3
      while (substr(s, k, 1) == " ") k++
      if (substr(s, k, 2) != "<<" || substr(s, k, 3) == "<<<") return 0
      k = hd_op(s, k + 2)
      if (!k) return 0
      while (substr(s, k, 1) == " ") k++
      if (substr(s, k, 1) != "\n") return 0
      a = hd_body(s, k + 1)
      if (!a) return 0
      while (a <= length(s) && index(" \t\n", substr(s, a, 1))) a++
      if (substr(s, a, 1) != ")") return 0
      return a - j + 1
    }
    function strip(s,    n, out, i, c, j, d, e, body, keep, closed, h, a, pend, pdash, pquoted, pdelim) {
      n = length(s); out = ""; i = 1; pend = 0
      while (i <= n) {
        c = substr(s, i, 1)
        if (c == "\\") { out = out substr(s, i, 2); i += 2; continue }
        if (c == "<" && !pend && substr(s, i, 2) == "<<" && substr(s, i, 3) != "<<<") {
          j = hd_op(s, i + 2)
          if (j) {
            pend = 1; pdash = H_DASH; pquoted = H_QUOTED; pdelim = H_DELIM
            out = out "<<" H_DELIM; i = j; continue
          }
        }
        if (c == "\n" && pend) {
          pend = 0; H_DASH = pdash; H_QUOTED = pquoted; H_DELIM = pdelim
          a = hd_body(s, i + 1)
          if (!a) { out = out substr(s, i); break }
          out = out "\n" (hd_runs() ? H_BODY : "")
          i = a; continue
        }
        if (c == sq) {
          e = index(substr(s, i + 1), sq)
          if (!e) { out = out substr(s, i); break }
          out = out word(substr(s, i + 1, e - 1)); i += e + 1; continue
        }
        if (c == "\"") {
          j = i + 1; body = ""; keep = 0; closed = 0
          while (j <= n) {
            d = substr(s, j, 1)
            if (d == "\"") { closed = 1; break }
            if (d == "\\") { body = body substr(s, j, 2); j += 2; continue }
            if (d == "`") keep = 1
            if (d == "$" && substr(s, j + 1, 1) == "(") {
              h = cat_heredoc(s, j)
              if (h) {
                if (hd_runs()) { keep = 1; body = body substr(s, j, h) }
                j += h; continue
              }
              keep = 1
            }
            body = body d; j++
          }
          if (!closed) { out = out substr(s, i); break }
          out = out (keep ? " " strip(body) " " : word(body))
          i = j + 1; continue
        }
        out = out c; i++
      }
      return out
    }
    { s = (NR == 1 ? "" : s "\n") $0 }
    END { printf "%s", strip(s) }'
}

# True unless the value is a literal branch name other than master/main. A
# value the hook can't read ($VAR, blanked quotes, @file) counts as master.
base_is_protected() {
  [[ ! "$1" =~ ^[A-Za-z0-9_./-]+$ ]] || [[ "$1" =~ ^(refs/heads/)?(master|main)$ ]]
}

CMD_POS='(?:^|[;&|(){`])\s*(?:(?:[A-Za-z_]\w*=\S*|env|command|exec|builtin|sudo|time|nohup|nice|then|do|else|!|timeout\s+\S+|xargs(?:\s+-\S+)*|direnv\s+exec\s+\S+)\s+)*'
GH_POS="${CMD_POS}(?:[\\w./~-]*/)?gh"
# The rest of one simple command: up to the next `;`, `&` or `|`, stepping over
# `$(…)` and backtick substitutions so a pipe inside one doesn't end it early.
SEG_TAIL='(?:\$\([^()]*(?:\([^()]*\)[^()]*)*\)|`[^`]*`|[^;&|])*'

# None of this applies to a command that never mentions gh, curl or wget, so
# those skip the extra work.
if [[ "$COMMAND" =~ (^|[^A-Za-z0-9_])(gh|curl|wget|push)([^A-Za-z0-9_]|$) ]]; then
  # A command that hands a quoted string to another shell (`bash -c '…'`, `eval`,
  # `ssh host '…'`, `xargs`, `bash <<EOF`) runs that string, so its quotes are
  # treated as command separators and nothing is blanked.
  if printf '%s' "$COMMAND" | grep -qP -- "${CMD_POS}(?:(?:ba|z|da|k)?sh\\s+(?:-\\w+\\s+)*-\\w*c\\b|(?:ba|z|da|k)?sh\\b[^;&|]*<<|eval\\b|ssh(?![\\w-])|su\\s[^;&|]*-c\\b|xargs\\b|watch\\b|parallel\\b)"; then
    STRUCT_COMMAND=$(printf '%s' "$COMMAND" | tr "\"'" ';;')
  else
    STRUCT_COMMAND=$(printf '%s' "$COMMAND" | struct_command)
  fi

  # 0. git push names its target (#1390, #1236). Every `git … push` in command
  #    position must give at least one ref, and every ref must be a literal
  #    branch or refspec other than HEAD/@; a bare push, a $VAR or a blanked
  #    quote is denied. The hook's cwd is never trusted (cd, git -C, GIT_DIR,
  #    sh -c all move the push elsewhere), and quoted text that only mentions
  #    push is blanked, so it doesn't count. master/main targets are denied by
  #    the keyword checks above.
  #    Git aliases can hide a push: setting one is denied, and an existing alias
  #    that expands to push is read as push.
  if printf '%s' "$STRUCT_COMMAND" | grep -qP -- '\bgit\b[^;&|]*\s-c\s*alias\.|\bgit\b[^;&|]*\bconfig\b[^;&|]*\balias\.'; then
    BLOCKED=true
    REASON="Setting a git alias is blocked: an alias can hide a push from this guard. Run the full git command instead."
  fi
  PUSH_VERBS=push
  while IFS= read -r ALIAS_LINE; do
    ALIAS_NAME=${ALIAS_LINE%% *}
    ALIAS_NAME=${ALIAS_NAME#alias.}
    [[ "$ALIAS_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] && PUSH_VERBS="$PUSH_VERBS|$(printf '%s' "$ALIAS_NAME" | sed 's/[.]/\\./g')"
  done <<< "$(git config --get-regexp '^alias\.' 2>/dev/null | grep -P '^\S+\s.*\bpush\b' || true)"
  PUSH_SEGS=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "${CMD_POS}(?:(?:eval|env(?:\\s+(?:-\\S+(?:\\s+[^\\s;&|-]\\S*)?|[A-Za-z_]\\w*=\\S*))*)\\s+)*(?:[\\w./~-]*/)?git(?:\\s+-\\S+(?:\\s+[^\\s;&|-]\\S*)?)*?\\s+\\K(?:${PUSH_VERBS})\\b${SEG_TAIL}" || true)
  while IFS= read -r PUSH_ARGS; do
    [ -n "$PUSH_ARGS" ] || continue
    # Drop redirections (2>&1 ends the segment at `&`), flags (and the value
    # of the ones that take one), then quotes.
    PUSH_NOFLAG=$(printf '%s\n' "$PUSH_ARGS" | sed -E 's/(^|\s)[0-9]*[<>]+\S*//g; s/(^|\s)(-o|--push-option|--repo|--receive-pack|--exec)(\s+|=)\S+//g; s/(^|\s)--?[a-zA-Z][a-zA-Z0-9_=-]*//g' | tr -d "'\"")
    read -r -a PUSH_WORDS <<< "$PUSH_NOFLAG"
    if [ "${#PUSH_WORDS[@]}" -lt 3 ]; then
      BLOCKED=true
      REASON="git push must name its branch: use git push -u origin <feature-branch> (no bare push, HEAD, @ or variable target)."
    fi
    for PUSH_REF in "${PUSH_WORDS[@]:2}"; do
      if [[ ! "$PUSH_REF" =~ ^\+?[A-Za-z0-9_./:-]+$ ]] || [[ "$PUSH_REF" =~ ^\+?(HEAD|@)$ ]]; then
        BLOCKED=true
        REASON="git push must name its branch: use git push -u origin <feature-branch> (no bare push, HEAD, @ or variable target)."
      fi
    done
  done <<< "$PUSH_SEGS"
  # Fail closed: more git+push words in the blanked command than pushes found
  # in command position means one was missed (if/while, nice -n, \git, …).
  PUSH_KW=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "\\bgit\\b(?![^;&|]*\\bstash\\b)[^;&|]*?\\b(?:${PUSH_VERBS})\\b" | wc -l || true)
  if [ "$PUSH_KW" -gt "$(printf '%s' "$PUSH_SEGS" | grep -c . || true)" ]; then
    BLOCKED=true
    REASON="git push blocked: the guard couldn't read this push. Run it on its own: git push -u origin <feature-branch>"
  fi

  # Everything below is about gh, curl and wget only.
  if [[ "$COMMAND" =~ (^|[^A-Za-z0-9_])(gh|curl|wget)([^A-Za-z0-9_]|$) ]]; then

  # 1. gh aliases. An alias set in one command and used in the next hides a merge
  #    or release from every check here. Setting one is never needed.
  if printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s+alias\\s+(set|import)\\b"; then
    BLOCKED=true
    REASON="gh alias set/import is blocked: an alias can hide a merge or release command from this guard. Run the full gh command instead."
  fi

  #    Using an alias that already exists (set by hand, or in gh's config.yml):
  #    look the name up and refuse when it expands to something the checks above
  #    would want to see. Harmless aliases (`co` → `pr checkout`) pass.
  if printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s"; then
    while IFS= read -r ALIAS_LINE; do
      case "$ALIAS_LINE" in '' | ' '* | $'\t'*) continue ;; esac  # body of a multi-line expansion
      [[ "$ALIAS_LINE" == *": "* ]] || continue
      ALIAS_NAME=${ALIAS_LINE%%: *}
      ALIAS_EXP=${ALIAS_LINE#*: }
      ALIAS_EXP=${ALIAS_EXP#[\"\']}
      [[ "$ALIAS_NAME" =~ ^[A-Za-z0-9_-]+(\ [A-Za-z0-9_-]+)*$ ]] || continue
      if printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s+${ALIAS_NAME// /\\s+}(?![\\w-])"; then
        if [[ "$ALIAS_EXP" == '|'* || "$ALIAS_EXP" == '>'* ]] || \
           printf '%s' "$ALIAS_EXP" | grep -qP '^!|\b(merge|release|api|workflow|rerun|alias|auth|set-default|extension)\b|\bpr\s+edit\b'; then
          BLOCKED=true
          REASON="gh alias '$ALIAS_NAME' expands to a shell, merge, release, API or workflow command, which this guard can't check through an alias. Run the expanded command directly."
        fi
      fi
    done < <(gh alias list 2>/dev/null || true)
  fi

  # 2. Retargeting a PR onto master. With auto-merge on, `gh pr edit --base
  #    master` turns a dev PR into a master merge. The release PR is opened
  #    against master by /pr-main, never retargeted.
  PR_EDIT_SEG=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "${GH_POS}\\s+pr\\s+edit\\b${SEG_TAIL}" || true)
  if [ -n "$PR_EDIT_SEG" ]; then
    while IFS= read -r BASE_VAL; do
      if base_is_protected "$BASE_VAL"; then
        BLOCKED=true
        REASON="gh pr edit --base master/main (or a base this guard can't read) is blocked. Open the dev→master release PR with /pr-main instead."
      fi
    done < <(printf '%s\n' "$PR_EDIT_SEG" | grep -oP -- '(?<=\s)(-B\s*|--base(=|\s+))\K\S+' || true)
  fi

  #    The same edit through the REST API: PATCH …/pulls/<n> with base=.
  API_SEG=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "${GH_POS}\\s+api\\b${SEG_TAIL}" || true)
  if printf '%s' "$API_SEG" | grep -qP '/pulls/\d+(?![\w/])'; then
    while IFS= read -r BASE_VAL; do
      if base_is_protected "${BASE_VAL#base=}"; then
        BLOCKED=true
        REASON="gh api …/pulls/<n> with base=master/main (or a base this guard can't read) is blocked. Open the dev→master release PR with /pr-main instead."
      fi
    done < <(printf '%s\n' "$API_SEG" | grep -oP -- '(?<=\s)(-f|-F|--field|--raw-field)(=|\s*)\Kbase=\S*' || true)
    # A request body from a file or stdin can carry "base" where the hook can't see it.
    if printf '%s' "$API_SEG" | grep -qP '(^|\s)--input\b'; then
      ASK=true
      ASK_REASON="⚠️ This sends a request body the release guard can't read to a pull-request endpoint. A body with \"base\": \"master\" would retarget the PR onto master. Approve only if the body doesn't change the base branch."
    fi
  fi

  # 3. curl/wget writes to the GitHub API. They carry Nathan's admin token
  #    ($(gh auth token), GH_TOKEN) straight past every gh check above. Reads are
  #    fine; every write has a gh equivalent this guard can check.
  if printf '%s' "$COMMAND" | grep -qPi '(?<![\w.-])(api|uploads)\.github\.com\b|(?<![\w.-])github\.com/api/'; then
    CURL_SEG=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "${CMD_POS}(?:[\\w./~-]*/)?curl\\b${SEG_TAIL}" || true)
    WGET_SEG=$(printf '%s' "$STRUCT_COMMAND" | grep -oP -- "${CMD_POS}(?:[\\w./~-]*/)?wget\\b${SEG_TAIL}" || true)
    if printf '%s' "$CURL_SEG" | grep -qP '(^|\s)(-[a-zA-Z]*X\s*|--request(=|\s+))(?i:POST|PUT|PATCH|DELETE)\b|(^|\s)(-[dFT]|-[a-zA-Z]+[dFT](?![\w-])|--(data(-raw|-binary|-urlencode|-ascii)?|json|form(-string)?|upload-file)(?![\w-]))' || \
       printf '%s' "$WGET_SEG" | grep -qP '(^|\s)--(method(=|\s+)(?i:POST|PUT|PATCH|DELETE)|post-data|post-file|body-data|body-file)\b'; then
      BLOCKED=true
      REASON="curl/wget writes to the GitHub API are blocked: they bypass the checks on gh. Use gh (gh pr …, gh issue …, gh api) so the guard can see the request."
    fi
  fi

  #    Reading the token out of gh is the first half of the same path (save it
  #    now, use it from a script later), so it asks.
  if printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s+auth\\s+(token\\b|status\\b${SEG_TAIL}\\s(--show-token|-t)\\b)"; then
    ASK=true
    ASK_REASON="🔑 This reads Nathan's GitHub token out of gh. The token has admin rights, and a program holding it can write to GitHub without the release guard seeing it. Approve only if a tool really needs the raw token; gh and gh api don't."
  fi

  # 4. Repo resolution. `gh repo set-default` changes which repo every later gh
  #    command acts on (the merge check below also confirms the resolved repo).
  if printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s+repo\\s+set-default\\b" && \
     ! printf '%s' "$STRUCT_COMMAND" | grep -qP -- "${GH_POS}\\s+repo\\s+set-default\\s+(--view|-v)\\s*(\$|[;&|)])"; then
    ASK=true
    ASK_REASON="⚠️ gh repo set-default changes which repository gh pr, gh release and gh workflow act on in this checkout. Approve only if Nathan asked for it."
  fi
  fi
fi

# ── Manually dispatching the release pipeline — ask first ────────
#
# The #376 fallback (`gh workflow run release.yml --ref vX.Y.Z`) publishes to
# PyPI, so it needs the same confirmation as the release merge.

# Any dispatch or re-run asks: a workflow can be named by numeric ID, and
# re-running a failed release run publishes.
if echo "$ORIG_COMMAND" | grep -qPi '\bgh\b.*\bworkflow\b.*\brun\b|\bgh\b.*\brun\b.*\brerun\b'; then
  ASK=true
  ASK_REASON="🚀 This dispatches or re-runs a GitHub Actions workflow, which may be the PyPI release pipeline. Approve only if Nathan explicitly approved it."
fi

# ── gh pr merge — dev freely; master only as a confirmed release ──
#
# dev → TestPyPI: allowed. master/main is the release gate (auto-tag →
# release.yml → PyPI → fleet). Claude may merge only the dev→master release PR,
# only with green CI, and always as "ask" so Nathan confirms in the permission
# prompt (#917). `--admin` bypasses GitHub's review and CI rules, which is why
# this hook re-checks CI itself.

# Allowlist, not denylist: any command that could merge a PR must be exactly
# `gh pr merge <N> [--squash|--merge|--rebase] [--admin] [--auto]` with nothing
# else on the line (no -R, env vars, cd, chaining), and run from an untether
# checkout — so the PR the hook looks up is the PR gh merges.
MERGE_RE='^[[:space:]]*gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[0-9]+([[:space:]]+(--squash|--merge|--rebase|--admin|--auto|-s|-m|-r))*[[:space:]]*$'
HOOK_CWD=$(echo "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || echo "")

if printf '%s' "$ORIG_COMMAND" | grep -qP '\bgh\b' && printf '%s' "$ORIG_COMMAND" | grep -qP '\bpr\b' && \
   printf '%s' "$ORIG_COMMAND" | grep -qPi '\bmerge\b'; then
  ORIGIN=$(git -C "${HOOK_CWD:-.}" remote get-url origin 2>/dev/null || echo "")
  if [[ ! "$ORIG_COMMAND" =~ $MERGE_RE ]]; then
    BLOCKED=true
    REASON="PR merges must be exactly: gh pr merge <number> --squash [--admin] — nothing else on the line (no -R/--repo, env vars, cd or chained commands). Run anything else separately."
  elif ! echo "$ORIGIN" | grep -qiP 'github\.com[:/]littlebearapps/untether(\.git)?/?$' || [ -n "${GH_REPO:-}${GH_HOST:-}" ]; then
    BLOCKED=true
    REASON="gh pr merge from this session may only run in a littlebearapps/untether checkout (cwd origin: '${ORIGIN:-none}'; GH_REPO/GH_HOST must be unset)."
  else
  PR_NUM=$(echo "$ORIG_COMMAND" | grep -oP '\bmerge\s+\K\d+')
    # Look the PR up the way `gh pr merge` will: from the session's cwd, with
    # no --repo, so gh's own resolution applies (an `upstream` remote outranks
    # `origin`, and `gh repo set-default` overrides both). The URL that comes
    # back names the repo gh would merge in; it must be untether (#917).
    PR_JSON=$(cd "${HOOK_CWD:-.}" 2>/dev/null && gh pr view "$PR_NUM" --json baseRefName,headRefName,title,statusCheckRollup,url 2>/dev/null || echo '{}')
    PR_BASE=$(echo "$PR_JSON" | jq -r '.baseRefName // "unknown"' 2>/dev/null || echo "unknown")
    PR_URL=$(echo "$PR_JSON" | jq -r '.url // ""' 2>/dev/null || echo "")
    if ! printf '%s' "$PR_URL" | grep -qiP "^https://github\\.com/littlebearapps/untether/pull/${PR_NUM}/?\$"; then
      BLOCKED=true
      REASON="gh pr merge blocked: gh resolves PR #$PR_NUM from this checkout to '${PR_URL:-nothing}', not littlebearapps/untether. An extra remote (e.g. upstream) or gh repo set-default changes which repo gh acts on; fix that, then retry."
    elif [ "$PR_BASE" = "dev" ]; then
      : # Allow merges to dev (TestPyPI/staging)
    elif [ "$PR_BASE" = "master" ] || [ "$PR_BASE" = "main" ]; then
      PR_HEAD=$(echo "$PR_JSON" | jq -r '.headRefName // ""')
      PR_TITLE=$(echo "$PR_JSON" | jq -r '.title // ""')
      CHECKS_TOTAL=$(echo "$PR_JSON" | jq -r '[.statusCheckRollup[]?] | length')
      CHECKS_NOT_GREEN=$(echo "$PR_JSON" | jq -r '[.statusCheckRollup[]? | (.conclusion // .state // "") | ascii_upcase | select(. != "SUCCESS" and . != "SKIPPED" and . != "NEUTRAL")] | length')
      if [ "$PR_HEAD" != "dev" ]; then
        BLOCKED=true
        REASON="Only the dev→master release PR may be merged to $PR_BASE (PR #$PR_NUM's head is '$PR_HEAD')."
      elif echo "$COMMAND" | grep -qP '(^|\s)(-d|--delete-branch)\b'; then
        BLOCKED=true
        REASON="Merging the release PR with --delete-branch would delete dev. Drop the flag."
      elif [ "$CHECKS_TOTAL" = "0" ] || [ "$CHECKS_NOT_GREEN" != "0" ]; then
        BLOCKED=true
        REASON="Release PR #$PR_NUM has $CHECKS_NOT_GREEN of $CHECKS_TOTAL checks not green (pending, failed or none reported). Wait for CI to pass, then retry."
      else
        ASK=true
        ASK_REASON="🚀 RELEASE: merge PR #$PR_NUM \"$PR_TITLE\" (dev → $PR_BASE). This publishes to PyPI (auto-tag → release.yml) and makes it the stable release. CI: $CHECKS_TOTAL checks green. Approve only if Nathan explicitly approved this release."
      fi
    else
      BLOCKED=true
      REASON="gh pr merge blocked: couldn't confirm PR #$PR_NUM's base branch ('$PR_BASE')."
    fi
  fi
fi

# ── Self-protection ──────────────────────────────────────────────

if echo "$COMMAND" | grep -qF 'release-guard' && \
   echo "$COMMAND" | grep -qPi '\b(rm|mv|cp|install|dd|tee|chmod|chown|unlink|truncate|shred|ln)\b|>\s'; then
  BLOCKED=true
  REASON="Cannot modify release guard files via shell."
fi

# The project .claude/settings.json registers these hooks. The user-level
# ~/.claude/settings.json is not covered here (only disableAllHooks is).
PROJECT_CMD=$(echo "$COMMAND" | sed -E "s#(~|\\\$HOME|\\\$\\{HOME\\}|${HOME})/\\.claude/settings#USER_SETTINGS#g")
if echo "$PROJECT_CMD" | grep -qP '\.claude/settings\.json|hooks\.json' && \
   echo "$PROJECT_CMD" | grep -qPi '\b(rm|mv|cp|install|dd|sed|awk|perl|python3?|ruby|node|tee|truncate|ln|checkout|restore)\b|>\s'; then
  BLOCKED=true
  REASON="Cannot modify .claude/settings.json (the hook registration) via shell commands."
fi

if echo "$COMMAND" | grep -qF 'disableAllHooks' && \
   echo "$COMMAND" | grep -qPi '\b(cp|mv|install|dd|sed|awk|perl|python3?|ruby|node|jq|tee|truncate|ln|claude)\b|>\s?'; then
  BLOCKED=true
  REASON="disableAllHooks would switch off the release guard. Only Nathan sets it."
fi

# ── Restarting a non-dev Untether service — ask first ────────────
#
# Staging (untether.service) runs a PyPI/TestPyPI wheel, so restarting it
# never tests local code; the fleet hosts are production. Restarting from
# inside an active Untether session also drops the final message. The
# legitimate paths are scripts/staging.sh install, pipx upgrade and
# scripts/fleet-rollout.sh, which restart internally.

if echo "$COMMAND" | grep -qP '\bsystemctl\b.*\b(restart|start|stop|kill|reload|try-restart|reload-or-restart)\b.*(?<![\w-])untether(\.service)?(?![\w.-])' || \
   echo "$COMMAND" | grep -qP '\blaunchctl\b.*\b(kickstart|stop|start|bootout|unload)\b.*com\.littlebearapps\.untether(?![\w.-])'; then
  if ! echo "$COMMAND" | grep -qP 'staging\.sh\s+install|pipx\s+(upgrade|install)\b.*\buntether\b'; then
    ASK=true
    ASK_REASON="⚠️ This restarts a non-dev Untether service (staging or a fleet host). Staging runs a PyPI/TestPyPI wheel, so local code changes have no effect on it — test with: systemctl --user restart untether-dev. Upgrade with scripts/staging.sh install or scripts/fleet-rollout.sh. Never restart from inside an active Untether session."
  fi
fi

# ── Output ───────────────────────────────────────────────────────
#
# Claude Code PreToolUse hook output schema (current as of 2026-04-15):
#   {
#     "hookSpecificOutput": {
#       "hookEventName": "PreToolUse",
#       "permissionDecision": "deny" | "ask" | "allow",
#       "permissionDecisionReason": "<text>"
#     }
#   }
# The legacy {"decision":"block","reason":...} shape is silently ignored, so
# blocks return as no-ops. See https://code.claude.com/docs/en/hooks for the
# spec. A hook "ask" forces the prompt even in auto mode (CLI ≥ 2.1.211); in an
# unattended -p run nobody can answer it, so the call is denied.

if [ "$BLOCKED" = true ]; then
  REASON_FULL=$(printf '🛑 RELEASE GUARD: %s\n\nAllowed: feature/dev branch pushes, PRs to dev, gh pr merge <n> for dev PRs, and — with Nathan'"'"'s explicit approval and green CI — gh pr merge <n> --squash --admin for the dev→master release PR (the hook asks him to confirm).\nBlocked: direct pushes to master/main, tags, gh release create.' "$REASON")
  jq -n --arg r "$REASON_FULL" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
elif [ "$ASK" = true ]; then
  jq -n --arg r "$ASK_REASON" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: $r
    }
  }'
else
  echo '{}'
fi
