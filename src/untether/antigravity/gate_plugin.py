"""Untether's agy gate plugin: its files, install, uninstall and status (#980).

The plugin is a hook manifest that sends ``PreToolUse``, ``PreInvocation``
and ``Stop`` to :mod:`untether.antigravity.gate_hook` — but only for agy runs
Untether started (the shell guard checks the marker env first). It lets an
agy run ask its questions in Telegram (``ask_question`` → the question card)
and take a mid-turn message (steer through ``PreInvocation.injectSteps``).

Native-first (R3-1, decided 2026-10-12): the plugin goes in and out through
agy's own plugin manager. ``install`` stages the three files in a temp folder
and runs ``agy plugin install <folder>`` (agy copies the whole folder to
``~/.gemini/config/plugins/untether-gate/`` and lists it in its
``import_manifest.json``); ``uninstall`` runs ``agy plugin uninstall``.
Untether writes nothing else under ``~/.gemini``. ``.untether-managed``
records what Untether installed (version, wait, file hashes): install and
uninstall refuse to touch a folder without it, or one whose files were
edited since, unless ``--force``. It is informational only — the run's
precheck compares the files with this version's own templates, never with
the manifest (a Full-access run could rewrite both).

The plugin is host-wide (one per ``HOME``): every Untether instance on the
host and the user's own agy share it. CLI only, never a Telegram button
(REVIEW-2 m3): a tap would let any allowed group member write into
``~/.gemini``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..utils import antigravity_scan
from . import gate_hook

logger = get_logger(__name__)

PLUGIN_NAME = "untether-gate"
PLUGIN_FILE = "plugin.json"
HOOKS_FILE = "hooks.json"
MANIFEST_FILE = ".untether-managed"
PLUGIN_FILES = (PLUGIN_FILE, HOOKS_FILE)

# How long a held tool call (a question in Telegram) waits for an answer.
# agy's own hook timeout is this plus ``HOOK_GRACE_S``, so Untether always
# answers first (D22.4); a different wait means re-running install.
DEFAULT_WAIT_S = 900
MIN_WAIT_S = 60
MAX_WAIT_S = 3000
HOOK_GRACE_S = 60
# PreInvocation / Stop answer at once; agy's default hook timeout is 30 s.
SHORT_HOOK_TIMEOUT_S = 15

AGY_TIMEOUT_S = 60.0
_MAX_FILE_BYTES = 64 * 1024

DESCRIPTION = (
    "Untether: Telegram questions and mid-turn messages for agy runs that "
    "Untether starts. Does nothing in your own agy sessions."
)


# Security review 3 M3: when Untether's interpreter can't run (an upgrade
# swapped the venv, the path is wrong), the shell guard answers PreToolUse
# with a deny itself instead of leaving agy a failed hook. agy 1.3.4 already
# blocks the tool when a hook exits non-zero, is killed or prints something
# that isn't JSON (probed 2026-10-13, ``gate_canary``); this doesn't rely on
# it. Plain ASCII, no quotes: it sits inside the shell's single quotes.
GUARD_DENY_REASON = (
    "The Untether gate interpreter (UNTETHER_AGY_GATE_PY) is missing or not "
    "executable, so the tool was blocked. Run untether agy-gate install on "
    "the host."
)
# Bumped whenever the hook commands change: an install of another version
# reads as "not current" (byte compare) and must be redone.
PLUGIN_VERSION = "2"


def hook_command(event: str, version: str = PLUGIN_VERSION) -> str:
    """The exact command line agy runs for *event* (``sh -c``); *version*
    "1" is what 0.36.1rc1 to rc3 installed (no interpreter check)."""
    if version == "1":
        return (
            f'[ -n "${gate_hook.ENV_MARKER}" ] || exit 0; '
            f'exec "${gate_hook.ENV_PY}" -I -m untether.antigravity.gate_hook {event}'
        )
    if event == "PreToolUse":
        deny = json.dumps({"decision": "deny", "reason": GUARD_DENY_REASON})
        missing = f"{{ printf '%s' '{deny}'; exit 0; }}"
    else:
        missing = "exit 0"
    return (
        f'[ -n "${gate_hook.ENV_MARKER}" ] || exit 0; '
        f'[ -x "${gate_hook.ENV_PY}" ] || {missing}; '
        f'exec "${gate_hook.ENV_PY}" -I -m untether.antigravity.gate_hook {event}'
    )


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()


def plugin_json_bytes(version: str = PLUGIN_VERSION) -> bytes:
    return _dump({"name": PLUGIN_NAME, "version": version, "description": DESCRIPTION})


def hooks_json_bytes(
    wait_s: int = DEFAULT_WAIT_S, version: str = PLUGIN_VERSION
) -> bytes:
    """agy needs a named top-level group (a flat manifest loads 0 hooks)."""
    return _dump(
        {
            PLUGIN_NAME: {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_command("PreToolUse", version),
                                "timeout": int(wait_s) + HOOK_GRACE_S,
                            }
                        ],
                    }
                ],
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": hook_command("PreInvocation", version),
                        "timeout": SHORT_HOOK_TIMEOUT_S,
                    }
                ],
                "Stop": [
                    {
                        "type": "command",
                        "command": hook_command("Stop", version),
                        "timeout": SHORT_HOOK_TIMEOUT_S,
                    }
                ],
            }
        }
    )


def template_files(
    wait_s: int = DEFAULT_WAIT_S, version: str = PLUGIN_VERSION
) -> dict[str, bytes]:
    return {
        PLUGIN_FILE: plugin_json_bytes(version),
        HOOKS_FILE: hooks_json_bytes(wait_s, version),
    }


# Plugin versions an earlier Untether installed. Files byte-identical to one
# of them are a genuine older install (re-run ``untether agy-gate install``);
# anything else that isn't current was changed.
PREVIOUS_VERSIONS = ("1",)


def plugin_dir() -> Path:
    return antigravity_scan.user_config_dir() / "plugins" / PLUGIN_NAME


def agy_plugin_state_path() -> Path:
    """agy's own plugin on/off switches (``agy plugin enable|disable``)."""
    return antigravity_scan.user_config_dir() / "config.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_wait_s(value: int) -> int:
    if not MIN_WAIT_S <= value <= MAX_WAIT_S:
        raise GateInstallError(
            f"--wait-s must be between {MIN_WAIT_S} and {MAX_WAIT_S} seconds."
        )
    return value


class GateInstallError(Exception):
    """Install / uninstall refused or failed; the message is for the user."""


@dataclass(slots=True)
class InstallState:
    """What is on disk at :func:`plugin_dir` (read-only)."""

    path: Path
    present: bool = False
    # ``.untether-managed`` parsed; None when absent or unreadable.
    manifest: dict[str, Any] | None = None
    # file name → ``ok`` / ``modified`` / ``missing`` (against the manifest).
    files: dict[str, str] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)
    wait_s: int | None = None
    # The files are byte-identical to what *this* Untether writes.
    current: bool = False
    # ... or to what an earlier Untether wrote (``PREVIOUS_VERSIONS``).
    previous_version: str | None = None
    # ``agy plugin disable`` was run (agy's config.json); None: unknown.
    disabled: bool | None = None
    problem: str | None = None

    @property
    def managed(self) -> bool:
        return self.manifest is not None

    @property
    def edited(self) -> list[str]:
        return [name for name, status in self.files.items() if status != "ok"]

    @property
    def ready(self) -> bool:
        """Installed by Untether, unedited and current — the cheap check."""
        return (
            self.present
            and self.managed
            and not self.edited
            and self.current
            and self.disabled is not True
        )

    def summary(self) -> str:
        if not self.present:
            return "not installed"
        if not self.managed:
            return "a plugin folder Untether didn't install"
        if self.edited:
            return "edited since install: " + ", ".join(sorted(self.edited))
        if self.disabled:
            return "installed but disabled in agy (agy plugin enable untether-gate)"
        if not self.current and self.previous_version is not None:
            return (
                f"plugin version {self.previous_version} from an earlier Untether"
                " (run `untether agy-gate install` to update it)"
            )
        if not self.current:
            return (
                "not the files this Untether version installs (another version,"
                " or changed along with its record)"
            )
        return "installed"


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_bytes()[:65536])
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def _plugin_disabled() -> bool | None:
    try:
        data = json.loads(agy_plugin_state_path().read_bytes()[:262144])
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entry = plugins.get(PLUGIN_NAME) if isinstance(plugins, dict) else None
    return isinstance(entry, dict) and entry.get("enabled") is False


def inspect_install(*, check_disabled: bool = True) -> InstallState:
    """Read what is installed. Never raises."""
    folder = plugin_dir()
    state = InstallState(path=folder)
    try:
        if not os.path.lexists(folder):
            return state
        state.present = True
        if folder.is_symlink() or not folder.is_dir():
            state.problem = "not a plain folder"
            return state
        state.manifest = _read_manifest(folder / MANIFEST_FILE)
        names = sorted(entry.name for entry in os.scandir(folder))
        state.extra = [n for n in names if n not in (*PLUGIN_FILES, MANIFEST_FILE)]
        wanted = state.manifest.get("files", {}) if state.manifest else {}
        raw_wait = state.manifest.get("wait_s") if state.manifest else None
        if isinstance(raw_wait, int) and MIN_WAIT_S <= raw_wait <= MAX_WAIT_S:
            state.wait_s = raw_wait
        on_disk: dict[str, bytes] = {}
        for name in PLUGIN_FILES:
            path = folder / name
            if path.is_symlink() or not path.is_file():
                state.files[name] = "missing"
                continue
            with open(path, "rb") as handle:
                data = handle.read(_MAX_FILE_BYTES + 1)  # ours are ~1 KB
            on_disk[name] = data
            state.files[name] = (
                "ok" if wanted.get(name) == _sha256(data) else "modified"
            )
        if state.extra:
            for name in state.extra:
                state.files[name] = "modified"
        templates = template_files(state.wait_s or DEFAULT_WAIT_S)
        state.current = state.wait_s is not None and all(
            on_disk.get(name) == data for name, data in templates.items()
        )
        state.previous_version = next(
            (
                version
                for version in PREVIOUS_VERSIONS
                if state.wait_s is not None
                and all(
                    on_disk.get(name) == data
                    for name, data in template_files(state.wait_s, version).items()
                )
            ),
            None,
        )
        if check_disabled:
            state.disabled = _plugin_disabled()
    except OSError as exc:
        state.problem = type(exc).__name__
    return state


def verified_own_files() -> frozenset[str]:
    """The plugin files the planted-config scanner may leave out (#1043):
    all three, only when the folder is exactly what *this* Untether writes —
    ``plugin.json`` and ``hooks.json`` byte-identical to its own templates,
    ``.untether-managed`` recording those hashes, nothing else in the folder.
    Otherwise none (the manifest alone proves nothing: whatever can rewrite
    ``hooks.json`` can rewrite it too), so a changed or extra file is
    tracked like any other agy plugin."""
    state = inspect_install(check_disabled=False)
    if (
        state.present
        and state.problem is None
        and state.managed
        and not state.edited
        and state.current
    ):
        return frozenset((*PLUGIN_FILES, MANIFEST_FILE))
    return frozenset()


def installed_ready() -> bool:
    """Cheap per-message check: Untether's plugin is installed, unedited,
    current and not disabled. The run's ``-p /hooks`` precheck decides."""
    return inspect_install().ready


def _manifest_bytes(files: dict[str, bytes], wait_s: int) -> bytes:
    from .. import __version__

    return _dump(
        {
            "untether_version": __version__,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "protocol": int(gate_hook.PROTOCOL),
            "wait_s": wait_s,
            "files": {name: _sha256(data) for name, data in files.items()},
        }
    )


def _run_agy(agy_cmd: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603 — fixed argv, no shell
            [agy_cmd, "plugin", *args],
            capture_output=True,
            text=True,
            timeout=AGY_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GateInstallError(f"agy not found ({agy_cmd}).") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateInstallError(
            f"agy plugin {args[0]} didn't finish in {AGY_TIMEOUT_S:.0f} s."
        ) from exc


def _refuse_unless_ours(state: InstallState, *, force: bool, verb: str) -> None:
    if force or not state.present:
        return
    if state.problem is not None:
        raise GateInstallError(
            f"{state.path} is {state.problem}; not touching it without --force."
        )
    if not state.managed:
        raise GateInstallError(
            f"{state.path} wasn't installed by Untether (no {MANIFEST_FILE}); "
            f"not going to {verb} it without --force."
        )
    if state.edited:
        raise GateInstallError(
            f"{state.path} was edited since Untether installed it "
            f"({', '.join(sorted(state.edited))}); not going to {verb} it "
            "without --force."
        )


@dataclass(frozen=True, slots=True)
class InstallPlan:
    target: Path
    files: dict[str, bytes]
    wait_s: int


def plan_install(wait_s: int = DEFAULT_WAIT_S) -> InstallPlan:
    check_wait_s(wait_s)
    return InstallPlan(target=plugin_dir(), files=template_files(wait_s), wait_s=wait_s)


def install(
    *,
    agy_cmd: str,
    wait_s: int = DEFAULT_WAIT_S,
    force: bool = False,
    echo: Callable[[str], None] = lambda _msg: None,
) -> InstallState:
    """Stage the plugin and hand it to ``agy plugin install``. Raises
    :class:`GateInstallError` with a user-facing message."""
    plan = plan_install(wait_s)
    before = inspect_install()
    _refuse_unless_ours(before, force=force, verb="replace")
    if before.present:
        echo(f"Removing the previous install at {before.path} …")
        _uninstall_via_agy(agy_cmd, before)
    stage_root = Path(tempfile.mkdtemp(prefix="untether-agy-gate-"))
    try:
        staged = stage_root / PLUGIN_NAME
        staged.mkdir(mode=0o700)
        for name, data in plan.files.items():
            (staged / name).write_bytes(data)
        (staged / MANIFEST_FILE).write_bytes(_manifest_bytes(plan.files, wait_s))
        validate = _run_agy(agy_cmd, "validate", str(staged))
        if validate.returncode != 0:
            raise GateInstallError(
                "agy plugin validate refused the plugin:\n"
                + (validate.stdout + validate.stderr).strip()[:2000]
            )
        result = _run_agy(agy_cmd, "install", str(staged))
        if result.returncode != 0:
            raise GateInstallError(
                "agy plugin install failed:\n"
                + (result.stdout + result.stderr).strip()[:2000]
            )
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    after = inspect_install()
    if not after.ready:
        raise GateInstallError(
            f"agy reported success, but {after.path} isn't what Untether "
            f"staged ({after.summary()})."
        )
    logger.info(
        "antigravity.gate.installed", path=str(after.path), wait_s=wait_s, force=force
    )
    return after


def _uninstall_via_agy(agy_cmd: str, state: InstallState) -> None:
    result = _run_agy(agy_cmd, "uninstall", PLUGIN_NAME)
    if result.returncode != 0:
        raise GateInstallError(
            "agy plugin uninstall failed:\n"
            + (result.stdout + result.stderr).strip()[:2000]
        )
    if os.path.lexists(state.path):
        # A folder agy doesn't list (dropped in by hand): agy's uninstall is
        # a no-op for it. Only reached for our own folder or with --force.
        if state.path.is_symlink():
            state.path.unlink()
        else:
            shutil.rmtree(state.path)


def uninstall(*, agy_cmd: str, force: bool = False) -> bool:
    """``agy plugin uninstall untether-gate``; False when nothing was there."""
    state = inspect_install()
    if not state.present:
        return False
    _refuse_unless_ours(state, force=force, verb="remove")
    _uninstall_via_agy(agy_cmd, state)
    logger.info("antigravity.gate.uninstalled", path=str(state.path), force=force)
    return True
