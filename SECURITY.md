# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| Latest release on PyPI (currently 0.35.4) | Yes |
| Older releases | No |

Only the latest published release receives security fixes. Please upgrade before reporting. When v0.36.0 is published, the 0.36.x line becomes the supported one and 0.35.x stops receiving fixes.

## Reporting a vulnerability

**Do not open a public issue.** Instead, use one of these channels:

- **Email:** [security@littlebearapps.com](mailto:security@littlebearapps.com)
- **GitHub Security Advisories:** [Report a vulnerability](https://github.com/littlebearapps/untether/security/advisories/new)

Include:
- Description of the vulnerability
- Steps to reproduce
- Untether version and Python version
- Any relevant logs (redact credentials)

## Response timeline

| Step | Target |
|------|--------|
| Acknowledgement | Within 48 hours |
| Initial assessment | Within 5 business days |
| Fix for critical issues | Within 7 days |
| Public disclosure | After fix is released |

## Scope

**In scope:**
- Untether application code
- Configuration parsing and validation
- Telegram transport security
- Subprocess management and session handling

**Out of scope:**
- Upstream agent CLIs (Claude Code, Codex, OpenCode, Pi) — report to their respective maintainers
- Telegram Bot API — report to [Telegram](https://telegram.org/blog/bug-bounty)
- Bot token management — token security is the operator's responsibility
- Issues requiring physical access to the host machine

## Security improvements in v0.36.0 (upcoming)

v0.36.0 (published to TestPyPI as release candidates 0.35.5rc1–rc20, then 0.36.0rcN) closes several approval, sandbox and file-access gaps. Upgrade notes:

- **BREAKING — `extra_args` refuses approval and sandbox bypass flags** ([#209](https://github.com/littlebearapps/untether/issues/209)). Claude's `--dangerously-skip-permissions`, `--allowedTools`, `--permission-prompts` and similar, and Codex's `--yolo`, `--dangerously-bypass-approvals-and-sandbox`, `--sandbox danger-full-access`, `-C`/`--cd` and similar, now fail config load: the default engine won't start and any other engine is disabled until the flag is removed. Errors and logs name the flag, never its value. `[engines.claude] dangerously_skip_permissions = true` now logs a warning, since it overrides every `/planmode` choice. See [Security how-to → Engine CLI flags](docs/how-to/security.md#engine-cli-flags-extra_args) for what `extra_args` blocking can't stop.
- **Codex safe mode now restricts Codex** ([#830](https://github.com/littlebearapps/untether/issues/830)). Before v0.36.0, `codex exec` ignored the approval flag Untether passed, so a chat set to **safe** ran exactly like full auto (and on codex-cli 0.149.0+ every safe run failed to start). Safe now uses Codex's read-only sandbox: edits, writes and shell network access are blocked.
- **Claude prompting modes prompt** ([#749](https://github.com/littlebearapps/untether/issues/749)). `default`, `manual` and `acceptEdits` (`/planmode off`) used to approve every tool silently; anything the mode doesn't cover now waits for an Approve / Deny tap. Unattended crons and webhooks in these modes are flagged in the log (`trigger.unattended_approval_risk`), and their requests are denied rather than left waiting (next bullet).
- **BREAKING — unattended trigger runs fail closed** ([#835](https://github.com/littlebearapps/untether/issues/835)). A cron or webhook Claude run never waits on an approval nobody can give, and never approves what an attended run would have asked about: tool approvals, plan approvals and questions are denied at once (`permission.unattended_deny`, listed in the run's final), and in `auto`, `dontAsk` and `bypassPermissions` every request that still reaches Untether is denied. `plan` and prompting-mode crons (including crons with no `permission_mode`, which inherit `plan` by default) now end with a plan or a report instead of acting; give each Claude cron that should act on its own an explicit `permission_mode`. A reply to an unattended run continues in an attended session with normal buttons.
- **Approval buttons are bound to their chat** ([#388](https://github.com/littlebearapps/untether/issues/388)). A forged `claude_control:` callback sent from another chat is refused, so a pending approval can only be answered where it was posted.
- **Opt-in: only the run's originator can answer** ([#388](https://github.com/littlebearapps/untether/issues/388)). In a chat with several allowed users, any of them can answer any approval by default. Set `[transports.telegram] approval_originator_only = true` (hot-reloads) and Claude Code approval buttons, the background-agent approval message and AskUserQuestion answers are accepted only from the person whose message started the run; anyone else gets "Only the person who started this run can answer this." and a WARNING (`callback.not_originator` / `ask_user_question.not_originator`). Cron, webhook, `/at` and loop runs have no originator, so any allowed user can still answer them. See [Group chat → Only the person who started the run can approve](docs/how-to/group-chat.md#only-the-person-who-started-the-run-can-approve-opt-in).
- **OpenCode 2.x is refused before it starts** ([#970](https://github.com/littlebearapps/untether/issues/970)). OpenCode 2.x (`@opencode/cli`) runs prompts on a shared per-user background service, outside Untether's environment allowlist, working directory, cancel and stall watchdog. Untether now checks `opencode --version` before each run and refuses 2.x with an install hint for the supported 1.x CLI (`opencode-ai`), keeping the chat's saved session.
- **No silent approvals when Claude Code downgrades `auto`** ([#751](https://github.com/littlebearapps/untether/issues/751)). On a model without auto mode (such as Haiku), Claude Code quietly runs `auto` as `default`; Untether used to approve those permission requests automatically. It now shows `⚠️ Asked for auto mode — Claude Code is running default`, logs `claude.permission_mode.mismatch` and sends the requests to Telegram.
- **File deny globs match at the project root and at any depth** ([#831](https://github.com/littlebearapps/untether/issues/831)). With the default `deny_globs`, a project-root `key.pem`, `id_rsa`, `.env.local`, `.npmrc`, `.netrc` or `.ssh/config` was not denied, and nothing deeper than one level under `.ssh/` was. Matching is now strictly more denying for `/file get`, `/file put`, outbox delivery, `/browse`, webhook `file_write` and cron `file_read`; the `.git` rule is case-insensitive. A project-root `.env.example` is now denied too.
- **Outbox delivery stays inside the project and sends only fresh files** ([#924](https://github.com/littlebearapps/untether/issues/924)). Only files written or copied into `.untether-outbox/` during the run are sent; leftovers from earlier sessions are archived to `.untether-outbox/.skipped/` instead of being attached to an unrelated answer. An `outbox_dir` containing `..` now fails config load, and an outbox that resolves outside the project (for example a symlinked `.untether-outbox`) is never scanned or archived.
- **Deny globs follow symlinks** ([#390](https://github.com/littlebearapps/untether/issues/390)). `/file put` and `/file get` check the path a request resolves to as well as the path requested, so an in-root symlink (e.g. `docs/x` → `.git/hooks`) can no longer route around `.git/**` or `.env`.
- **`/browse` needs a project root** ([#389](https://github.com/littlebearapps/untether/issues/389), [#210](https://github.com/littlebearapps/untether/issues/210)). It no longer falls back to the process working directory (`$HOME` under systemd), applies the deny globs and hidden-path rules to listings and previews, resolves symlinks before its containment check, and scopes its button ids per chat.
- **Log redaction widened** ([#800](https://github.com/littlebearapps/untether/issues/800), [#679](https://github.com/littlebearapps/untether/issues/679)). Process titles are scanned in full, and bearer credentials, JWTs and `api_key=` / `token=` / `secret=` / `password=` values are redacted; `ssrf.*` log lines redact URL userinfo.
- **Dependency advisories** — `anyio` 4.15.1 (CVE-2026-63374, CVE-2026-64847; [#773](https://github.com/littlebearapps/untether/issues/773)) and `aiohttp` 3.14.3 (`PYSEC-2026-3545`/`3546`/`3547`) in the lockfile.

See [CHANGELOG v0.36.0](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0360-unreleased) for the full entry list.

## Security improvements in v0.35.3

v0.35.3 ships a follow-on hardening bundle on top of v0.35.2. Upgrade notes:

- **BREAKING — empty `allowed_user_ids` rejected at startup** ([#377](https://github.com/littlebearapps/untether/issues/377)). Previously the empty default meant any Telegram user who knew the bot username could send commands. Untether now refuses to start with `ConfigError: [transports.telegram] allowed_user_ids is empty …`. Operators who genuinely need an open bot (demos, hackathons, dev) must opt in explicitly with `allow_any_user = true`, which is logged INFO every boot (`security.allow_any_user`). See [Security how-to](docs/how-to/security.md).
- **AMP `dangerously_allow_all` default flipped to `false`** ([#206](https://github.com/littlebearapps/untether/issues/206)). AMP runs no longer skip its built-in permission system unless the operator opts in.
- **Pi session directory locked to `0o700`** ([#207](https://github.com/littlebearapps/untether/issues/207)). Other users on shared hosts can no longer read Pi session JSONL.
- **`voice_transcription_api_key` is now `SecretStr`** ([#378](https://github.com/littlebearapps/untether/issues/378)) — parity with `bot_token`. Masked in repr/str/tracebacks and structlog serialisation.
- **Prompt content removed from INFO logs** ([#205](https://github.com/littlebearapps/untether/issues/205), [#478](https://github.com/littlebearapps/untether/issues/478)) — `runner.start` no longer carries `prompt[:100]`. A debug-only `runner.start_prompt` event is available when explicitly enabled.
- **`/file get` TOCTOU window closed** ([#211](https://github.com/littlebearapps/untether/issues/211)) — single-open + bounded read in a worker thread.
- **stderr sanitisation regex extended** ([#208](https://github.com/littlebearapps/untether/issues/208)) — covers macOS (`/Users/…`, `/private/var/…`), container roots (`/app/`, `/workspace/`), and other absolute paths beyond `/home/<user>/`.
- **OpenAI project-key redaction** ([#213](https://github.com/littlebearapps/untether/issues/213)) — structlog redaction now covers `sk-proj-…` keys (the generic `sk-…` regex didn't match the project-key char set).
- **Daily cost tracker race fixed** ([#379](https://github.com/littlebearapps/untether/issues/379)) — the unguarded read-modify-write that could lose a run's cost (and bypass the per-day budget cap) is now wrapped in a lock.
- **Pygments bumped 2.19.2 → 2.20.0** ([#402](https://github.com/littlebearapps/untether/issues/402)) — clears CVE-2026-4539 (ReDoS in `AdlLexer`).
- **Auto-approve scope re-audit** ([#380](https://github.com/littlebearapps/untether/issues/380)) — `ControlRewindFilesRequest` and `ControlMcpMessageRequest` re-verified safe under the upstream Claude Code 2.1.x trust model. Regression-lock tests fail loudly if the auto-approve path starts inspecting payloads. Audit memo at `docs/audits/2026-04-27-380-auto-approve-scope-review.md`.
- **User-extensible env allowlist** ([#409](https://github.com/littlebearapps/untether/issues/409)) — `[security] env_extra_allow` and `env_extra_prefix_allow` let operators thread credential-manager tokens (1Password, Doppler, Vault, Infisical) into engine subprocesses without forking. `BWS_ACCESS_TOKEN` is now in the built-in defaults.

See [CHANGELOG v0.35.3](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0353-2026-05-20) for the full entry list.

## Security improvements in v0.35.2

v0.35.2 ships a security hardening bundle. Upgrade notes:

- **Env allowlist for Claude/Pi subprocesses** — only approved variables pass through; unrelated process env no longer leaks to agent CLIs. ([#198](https://github.com/littlebearapps/untether/issues/198))
- **Runtime env hardening (always on)** — Claude exec is wrapped with `env -i KEY=VAL …` so the resolved environment is exactly the allowlist from `utils/env_policy.filtered_env()`, even if an upstream rc-file source or wrapper script would otherwise re-introduce host vars after `subprocess.spawn(env=…)` is honoured. This hardening is **not** controlled by any config setting.
- **Runtime env audit** — gated by `[security] env_audit = true` (default). Samples `/proc/<claude_pid>/environ` once per session and emits a `claude.env_audit.leaked_var` WARNING for every non-allowlisted variable observed. Disabling the audit only silences the warning sampler — it does **not** disable the `env -i` hardening above. ([#361](https://github.com/littlebearapps/untether/issues/361))
- **`bot_token` stored as `SecretStr`** — masked in repr/str/tracebacks; unwrapped only at the transport boundary. ([#196](https://github.com/littlebearapps/untether/issues/196))
- **User-safe error messages** — voice transcription and command-dispatch failures route through `user_safe_error()` (strips URLs/paths, caps length, fallback on empty). ([#200](https://github.com/littlebearapps/untether/issues/200), [#201](https://github.com/littlebearapps/untether/issues/201))
- **Codex auth output HTML-escaped** — prevents entity injection before `<pre>` wrapping. ([#199](https://github.com/littlebearapps/untether/issues/199))
- **Download URL path validation** — blocks `://`, `..`, and leading `/` before URL construction. ([#204](https://github.com/littlebearapps/untether/issues/204))
- **Duplicate-request dedup via LRU** — bounded `OrderedDict` (max 200) closes a small race that the previous wholesale-clear approach left open. ([#197](https://github.com/littlebearapps/untether/issues/197))
- **Registry ephemeral sweep** — `_EPHEMERAL_MSGS` / `_OUTLINE_REGISTRY` entries older than 1 hour are pruned on a 60 s tick. ([#203](https://github.com/littlebearapps/untether/issues/203))
- **CI matrix interpolation moved to `env:`** — eliminates a shell-injection vector in the release pipeline. ([#195](https://github.com/littlebearapps/untether/issues/195))
- **Subprocess sites annotated inline** — global `B603/B607` bandit skips removed; each call site carries its own `# nosec` justification. ([#202](https://github.com/littlebearapps/untether/issues/202))

See [CHANGELOG v0.35.2](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0352-2026-04-20) for the full entry list.

## Disclosure policy

We follow coordinated disclosure. We ask that you:
1. Allow us reasonable time to investigate and fix the issue
2. Do not exploit the vulnerability beyond what is needed for the report
3. Do not disclose publicly until a fix is available

We credit reporters in the release notes (unless you prefer to remain anonymous).
