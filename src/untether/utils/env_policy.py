"""Allowlist-based env filter for engine subprocesses (#198, #409).

Background
----------

Until #198 the Claude and Pi runners spawned their subprocesses with
``env = dict(os.environ)`` — effectively handing the engine every
environment variable available to Untether, including arbitrary third-
party tokens the user happened to have set (AWS, Digital Ocean, Stripe,
internal company tools, etc.). That's fine when the user controls the
engine end-to-end, but it enlarges the blast radius of any tool-call
that exfiltrates process env (``Bash`` with ``env``, a crafted MCP,
etc.).

This module replaces the broad copy with an allowlist: only vars that a
Claude-style CLI / MCP / locale-sensitive tool actually needs are
forwarded. Everything else (including tokens from unrelated apps) is
dropped.

Scope of this change
--------------------

Only the Claude and Pi runners opt in via :func:`filtered_env` in
v0.35.2. Other engines (Codex, OpenCode, Gemini, AMP) continue to
return ``None`` from their ``env()`` hook and inherit the parent
environment unchanged. Extending to those engines needs per-engine
integration validation — see #332 for the follow-up milestone.

Extending the allowlist
-----------------------

There are two ways to extend the allowlist:

1. **Built-in defaults** (this module). Add the variable to
   ``_EXACT_ALLOW`` or ``_PREFIX_ALLOW``, ship a test in
   ``tests/test_env_policy.py``, and run the integration suite. Use
   this for vars that *every* user is likely to need.

2. **Per-deployment config** (#409). Set
   ``[security] env_extra_allow = [...]`` and
   ``env_extra_prefix_allow = [...]`` in ``untether.toml``. The
   runners pass these through to :func:`filtered_env` so the user
   doesn't need to fork or vendor-patch this module to thread a
   credential-manager token (``OP_SERVICE_ACCOUNT_TOKEN``,
   ``DOPPLER_TOKEN``, ``VAULT_*``, etc.) to engine subprocesses.

The set of NAMESPACE prefixes is deliberately narrow — add another
default prefix only when there's a clear family of vars (e.g. all
``XDG_*``). User-defined extras are filtered to the same name shape.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from ..logging import get_logger

logger = get_logger(__name__)

# Exact-match allowlist. One entry per variable.
_EXACT_ALLOW: frozenset[str] = frozenset(
    {
        # OS essentials — spawning a subprocess without these breaks
        # basic shelling, path resolution, and tooling output.
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        # CLI UX — tools that respect these render nicely for both
        # humans and our JSONL parser (which expects no ANSI).
        "NO_COLOR",
        "CI",
        "FORCE_COLOR",
        "COLORTERM",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        # XDG — config/state/cache roots. Engines and MCPs use these
        # to locate credentials files, session state, caches.
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        # Language runtimes — Python/Node need these for module
        # resolution and dynamic linker lookup on Linux/macOS.
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "NODE_PATH",
        "NODE_OPTIONS",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        # Git / SSH — engines call git + ssh for commits and auth.
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GIT_CONFIG_GLOBAL",
        "GIT_SSH_COMMAND",
        # Cloud / AI provider keys. Claude / Codex / Gemini / OpenCode
        # / AMP each need their own; list all of them here rather than
        # per-engine so ``filtered_env`` is a single source of truth.
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "CEREBRAS_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "FIREWORKS_API_KEY",
        # GitHub — used by CLI tooling inside agents (gh, git push).
        "GITHUB_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GH_TOKEN",
        # Cloudflare — for MCP servers accessing CF APIs.
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        # Bitwarden Secrets Manager — used by MCP bash wrappers that
        # call `kc_get` / `bws secret` to materialise per-project
        # credentials (Trello, Jina, Pal, etc.). See issue #409.
        "BWS_ACCESS_TOKEN",
        # Untether-set markers — Claude hooks look for UNTETHER_SESSION.
        "UNTETHER_SESSION",
        # direnv-provided workspace context.
        "PROJECT_ROOT",
        "DIRENV_DIR",
    }
)

# Prefix allowlist. A variable passes the filter when its name starts
# with one of these prefixes. Use for families of related keys that
# would otherwise require dozens of individual entries (MCP tool
# timeouts, Claude CLI knobs, etc.).
_PREFIX_ALLOW: tuple[str, ...] = (
    "CLAUDE_",  # CLAUDE_ENABLE_STREAM_WATCHDOG, CLAUDE_STREAM_IDLE_TIMEOUT_MS, ...
    "CLAUDE_CODE_",  # upstream flags like CLAUDE_CODE_ENABLE_TELEMETRY
    "MCP_",  # MCP_TOOL_TIMEOUT, MCP_SERVER_*, ...
    "MAX_MCP_",  # MAX_MCP_OUTPUT_TOKENS (upstream env name)
    "LC_",  # LC_NUMERIC, LC_TIME, ... locale variants
    "UV_",  # uv-managed Python env
    "NPM_",  # NPM_CONFIG_*, NPM_TOKEN for private registries
    "PNPM_",  # pnpm-managed Node env
    "NODE_",  # NODE_TLS_REJECT_UNAUTHORIZED (for corp CAs), etc.
    "PIP_",  # PIP_INDEX_URL, PIP_EXTRA_INDEX_URL for private PyPI
    "UNTETHER_",  # Untether's own env markers (forward-compat)
)


def is_allowed(name: str) -> bool:
    """Return True if ``name`` is in the engine-subprocess env allowlist.

    Public predicate shared by :func:`filtered_env` and
    :mod:`untether.utils.env_audit` so the allowlist remains a single
    source of truth. Promoted from the previous private ``_is_allowed``.
    """
    if name in _EXACT_ALLOW:
        return True
    return any(name.startswith(prefix) for prefix in _PREFIX_ALLOW)


def is_allowed_with_extras(
    name: str,
    *,
    extra_exact: Iterable[str] = (),
    extra_prefix: Iterable[str] = (),
) -> bool:
    """Like :func:`is_allowed` but also honours per-deployment user extras (#409).

    ``extra_exact`` and ``extra_prefix`` come from
    ``[security] env_extra_allow`` / ``env_extra_prefix_allow`` in
    ``untether.toml``. The audit module passes them through so live-
    process audits don't false-flag user-allowed names as leaks.
    """
    if is_allowed(name):
        return True
    if name in frozenset(extra_exact):
        return True
    return any(name.startswith(prefix) for prefix in extra_prefix)


# Back-compat alias for any external importers that depended on the
# previously-private name. Safe to remove once we've audited all consumers.
_is_allowed = is_allowed


def filtered_env(
    source: Mapping[str, str] | None = None,
    *,
    extra_allow: Iterable[str] = (),
    extra_prefix: Iterable[str] = (),
) -> dict[str, str]:
    """Return a filtered copy of `source` containing only allowlisted keys.

    Parameters
    ----------
    source : Mapping[str, str] | None
        Environment to filter. Defaults to ``os.environ`` when *None*.
    extra_allow : Iterable[str]
        Additional exact variable names to allow for this call (e.g.
        per-engine / per-site keys that don't belong in the global set).
    extra_prefix : Iterable[str]
        Additional name prefixes to allow (#409 — surfaces
        ``[security] env_extra_prefix_allow`` so users can pass through
        credential-manager families like ``VAULT_*``).

    Returns
    -------
    dict[str, str]
        New dict containing only names that satisfy the allowlist.
    """
    if source is None:
        source = os.environ
    extras_exact = frozenset(extra_allow)
    extras_prefix = tuple(extra_prefix)
    return {
        k: v
        for k, v in source.items()
        if is_allowed_with_extras(
            k, extra_exact=extras_exact, extra_prefix=extras_prefix
        )
    }


# Module-level latch so we emit `env_policy.user_extension` at most once
# per process even if multiple runners (Claude + Pi) call it. Reset is
# only useful in tests; expose the underlying flag via _RESET_LOG_LATCH.
_extension_logged = False


def log_user_extensions_once(
    extra_exact: Iterable[str] = (),
    extra_prefix: Iterable[str] = (),
) -> None:
    """Emit a single INFO log naming user-supplied env-policy extras (#409).

    Idempotent — re-invocations after the first non-empty call are
    no-ops so journalctl shows one record per process per restart, not
    one per spawned subprocess.
    """
    global _extension_logged
    if _extension_logged:
        return
    exact = sorted(set(extra_exact))
    prefix = sorted(set(extra_prefix))
    if not exact and not prefix:
        return
    logger.info(
        "env_policy.user_extension",
        extra_exact=exact,
        extra_prefix=prefix,
        hint=(
            "user-extended subprocess env allowlist via "
            "[security] env_extra_allow / env_extra_prefix_allow"
        ),
    )
    _extension_logged = True


def _reset_log_latch_for_tests() -> None:
    """Clear the once-per-process log latch. Tests only."""
    global _extension_logged
    _extension_logged = False


__all__ = [
    "filtered_env",
    "is_allowed",
    "is_allowed_with_extras",
    "log_user_extensions_once",
]
