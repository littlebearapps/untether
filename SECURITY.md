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

## Disclosure policy

We follow coordinated disclosure. We ask that you:
1. Allow us reasonable time to investigate and fix the issue
2. Do not exploit the vulnerability beyond what is needed for the report
3. Do not disclose publicly until a fix is available

We credit reporters in the release notes (unless you prefer to remain anonymous).
