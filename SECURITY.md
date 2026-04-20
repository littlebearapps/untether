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
