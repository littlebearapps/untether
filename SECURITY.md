# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

Only the latest published release receives security fixes. Please upgrade before reporting.

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

See [CHANGELOG v0.35.3](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0353) for the full entry list.

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

See [CHANGELOG v0.35.2](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0352) for the full entry list.

## Disclosure policy

We follow coordinated disclosure. We ask that you:
1. Allow us reasonable time to investigate and fix the issue
2. Do not exploit the vulnerability beyond what is needed for the report
3. Do not disclose publicly until a fix is available

We credit reporters in the release notes (unless you prefer to remain anonymous).
