"""Codex ``app-server`` support gate (#961). The transport itself is #962.

Detect, don't assume: before the first app-server spawn per installed binary,
Untether asks the CLI for its own protocol schema (``codex app-server
generate-json-schema``, stable and ``--experimental``; zero tokens, ~0.2 s
each) and checks every method it depends on is there at the expected tier
(``schemas/codex_app_server.py``). A CLI that fails the check is refused
before spawn; the version is only quoted in the message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from ..events import EventFactory
from ..logging import get_logger
from ..model import CompletedEvent, ResumeToken
from ..runner import PRESPAWN_BLOCKED_KEY
from ..schemas.codex_app_server import (
    CODEX_APP_SERVER_SENDS,
    CODEX_APP_SERVER_TYPES,
    CODEX_APP_SERVER_USED,
    MIN_TESTED_APP_SERVER_VERSION,
)

logger = get_logger(__name__)

APP_SERVER_UNSUPPORTED_BLOCK = "codex_app_server_unsupported"
SCHEMA_BUNDLE_FILE = "codex_app_server_protocol.schemas.json"
_PROBE_TIMEOUT_S = 30.0
_UNIONS = {
    "client_request": "ClientRequest",
    "client_notification": "ClientNotification",
    "server_notification": "ServerNotification",
    "server_request": "ServerRequest",
}

type Bundle = dict[str, Any]
# key → (tier, {name: tier}); see ``schema_subset``.
type Subset = dict[str, dict[str, Any]]


# --- schema subset ----------------------------------------------------------


def _resolve(bundle: Bundle, ref: str) -> dict[str, Any] | None:
    node: Any = bundle
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _definition(bundle: Bundle, name: str) -> dict[str, Any] | None:
    defs = bundle.get("definitions", {})
    found = defs.get(name)
    if found is None:
        found = defs.get("v2", {}).get(name)
    return found if isinstance(found, dict) else None


def _props(defn: dict[str, Any] | None) -> set[str]:
    return set((defn or {}).get("properties", {}))


def _string_values(defn: dict[str, Any] | None) -> set[str]:
    if defn is None:
        return set()
    values = {v for v in defn.get("enum", ()) if isinstance(v, str)}
    for variant in defn.get("oneOf", ()):
        values |= {v for v in variant.get("enum", ()) if isinstance(v, str)}
    return values


def _flat_view(bundle: Bundle) -> dict[str, set[str]]:
    """One bundle as ``key → names`` for everything Untether depends on."""
    view: dict[str, set[str]] = {}
    for kind, union_name in _UNIONS.items():
        union = _definition(bundle, union_name) or {}
        for variant in union.get("oneOf", ()):
            props = variant.get("properties", {})
            methods = props.get("method", {}).get("enum", ())
            if len(methods) != 1 or (kind, methods[0]) not in CODEX_APP_SERVER_USED:
                continue
            names: set[str] = set()
            params_ref = props.get("params", {}).get("$ref")
            params = _resolve(bundle, params_ref) if params_ref else None
            names |= {f"params.{p}" for p in _props(params)}
            if kind.endswith("request") and params is not None:
                title = str(params.get("title", ""))
                response = _definition(
                    bundle, title.removesuffix("Params") + "Response"
                )
                names |= {f"result.{p}" for p in _props(response)}
            view[f"{kind} {methods[0]}"] = names
    for type_name, variants in CODEX_APP_SERVER_TYPES.items():
        defn = _definition(bundle, type_name)
        if defn is None:
            continue
        if not variants:
            view[f"type {type_name}"] = _props(defn)
            continue
        for variant in defn.get("oneOf", ()):
            tags = variant.get("properties", {}).get("type", {}).get("enum", ())
            if len(tags) == 1 and tags[0] in variants:
                view[f"type {type_name}.{tags[0]}"] = _props(variant)
    for enum_name in CODEX_APP_SERVER_SENDS:
        values = _string_values(_definition(bundle, enum_name))
        if values:
            view[f"enum {enum_name}"] = values
    return view


def schema_subset(stable: Bundle, experimental: Bundle) -> Subset:
    """The tiered subset of both bundles: ``{key: {"tier", "names"}}``.

    A key or name in the stable bundle is ``S``; one only in the experimental
    bundle is ``E``. This is what the committed fixture holds and what the
    drift test and the gate compare.
    """
    s_view, e_view = _flat_view(stable), _flat_view(experimental)
    subset: Subset = {}
    for key in sorted(s_view.keys() | e_view.keys()):
        s_names = s_view.get(key, set())
        names = s_names | e_view.get(key, set())
        subset[key] = {
            "tier": "S" if key in s_view else "E",
            "names": {n: ("S" if n in s_names else "E") for n in sorted(names)},
        }
    return subset


def unsupported_methods(subset: Subset) -> list[str]:
    """What blocks the transport: a used method missing, or one Untether
    uses as stable that is now experimental-only."""
    problems: list[str] = []
    for (kind, method), tier in CODEX_APP_SERVER_USED.items():
        found = subset.get(f"{kind} {method}")
        if found is None:
            problems.append(f"`{method}` missing")
        elif tier == "S" and found["tier"] != "S":
            problems.append(f"`{method}` is experimental-only")
    return problems


# --- probing the installed CLI ----------------------------------------------


def _run(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 — fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        check=False,
    )


def generate_schema_bundle(cmd: str, out_dir: Path, *, experimental: bool) -> Bundle:
    """Run ``codex app-server generate-json-schema`` and load the bundle.

    Raises ``RuntimeError`` (rc != 0, no bundle) or ``OSError`` /
    ``subprocess.TimeoutExpired`` (could not run).
    """
    argv = [cmd, "app-server", "generate-json-schema", "--out", str(out_dir)]
    if experimental:
        argv.insert(3, "--experimental")
    proc = _run(argv, str(out_dir.parent))
    bundle_path = out_dir / SCHEMA_BUNDLE_FILE
    if proc.returncode != 0 or not bundle_path.is_file():
        raise RuntimeError(f"generate-json-schema exited rc={proc.returncode}")
    return json.loads(bundle_path.read_text(encoding="utf-8"))


def probe_schema_subset(cmd: str) -> Subset:
    """Both bundles from the installed CLI → ``schema_subset`` (temp dir cleaned)."""
    with tempfile.TemporaryDirectory(prefix="untether-codex-schema-") as tmp:
        root = Path(tmp)
        stable = generate_schema_bundle(cmd, root / "stable", experimental=False)
        experimental = generate_schema_bundle(cmd, root / "exp", experimental=True)
    return schema_subset(stable, experimental)


@dataclass(frozen=True, slots=True)
class AppServerSupport:
    version: str | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


_SUPPORT_CACHE: dict[tuple[str, float], AppServerSupport] = {}


def _cli_version(path: str) -> str | None:
    try:
        proc = _run([path, "--version"], tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out.removeprefix("codex-cli ").strip() or None


def codex_app_server_support(cmd: str) -> AppServerSupport:
    """#961: can this Codex CLI run Untether's app-server transport?

    Cached per (binary path, mtime), so a Codex auto-update re-checks. A
    probe that could not run (timeout, OS error) is not cached. Blocking:
    call it off the event loop.
    """
    path = shutil.which(cmd) or (cmd if os.path.isabs(cmd) else None)
    if path is None:
        return AppServerSupport(None, (f"`{cmd}` not found on PATH",))
    try:
        real = os.path.realpath(path)
        key = (real, os.stat(real).st_mtime)
    except OSError as exc:
        return AppServerSupport(None, (f"cannot stat `{cmd}`: {exc.strerror}",))
    cached = _SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    version = _cli_version(path)
    try:
        problems = tuple(unsupported_methods(probe_schema_subset(path)))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "codex.app_server.probe_failed", version=version, error=type(exc).__name__
        )
        return AppServerSupport(version, ("schema probe could not run",))
    except (RuntimeError, ValueError) as exc:
        # Old CLI without the subcommand, or an unreadable bundle.
        problems = (f"schema unavailable ({exc})",)
    support = AppServerSupport(version, problems)
    _SUPPORT_CACHE[key] = support
    if support.ok:
        logger.info("codex.app_server.ready", version=version)
    else:
        logger.error(
            "codex.app_server.unsupported", version=version, problems=list(problems)
        )
    return support


def unsupported_message(support: AppServerSupport) -> str:
    version = support.version or "unknown version"
    detail = "; ".join(support.problems[:3])
    return (
        f"🛑 This Codex CLI ({version}) doesn't provide the app-server protocol "
        f"Untether needs ({detail}), so this run wasn't started. The app-server "
        f"transport is tested with codex-cli {MIN_TESTED_APP_SERVER_VERSION} or "
        'later: upgrade Codex, or switch to `transport = "exec"` under `[engines.codex]`.'
    )


async def app_server_unsupported_event(
    cmd: str, resume: ResumeToken | None
) -> CompletedEvent | None:
    """The pre-spawn refusal for ``run_impl``, or None when supported."""
    support = await anyio.to_thread.run_sync(codex_app_server_support, cmd)
    if support.ok:
        return None
    return EventFactory("codex").completed_error(
        error=unsupported_message(support),
        resume=resume,
        # Never ran the engine: keep the chat's saved session (#838).
        usage={PRESPAWN_BLOCKED_KEY: APP_SERVER_UNSUPPORTED_BLOCK},
    )
