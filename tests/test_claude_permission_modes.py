"""Claude permission-mode semantics (#741) and allowlist validation (#742).

Reference: Claude Code CLI 2.1.228, verified on lba-1 2026-08-12 by
``claude --help`` plus a per-value ``--permission-mode`` spawn probe.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from untether.runners.run_options import (
    CLAUDE_CLI_PERMISSION_MODES,
    CLAUDE_PLAN_AUTO_MODE,
    LEGACY_CLAUDE_PLAN_AUTO_MODE,
    VALID_PERMISSION_MODES_BY_ENGINE,
    claude_cli_permission_mode,
    is_claude_plan_auto,
)

# ---------------------------------------------------------------------------
# #742 — the canonical allowlist
# ---------------------------------------------------------------------------


def test_cli_mode_set_matches_cli_2_1_228() -> None:
    """The set derived from CLI 2.1.228, including the `manual` alias.

    ``claude --help`` lists ``manual`` in place of ``default``; both are
    accepted by the binary (probed 2026-08-12).
    """
    expected = frozenset(
        {
            "default",
            "manual",
            "plan",
            "auto",
            "acceptEdits",
            "dontAsk",
            "bypassPermissions",
        }
    )
    assert expected == CLAUDE_CLI_PERMISSION_MODES


def test_default_is_still_accepted() -> None:
    """#742 claimed `default` was CLI-rejected; the probe disproved it."""
    assert "default" in CLAUDE_CLI_PERMISSION_MODES


def test_manual_and_dont_ask_are_reachable() -> None:
    """Both were legal upstream but rejected by our parse-time validator."""
    allowed = VALID_PERMISSION_MODES_BY_ENGINE["claude"]
    assert "manual" in allowed
    assert "dontAsk" in allowed


def test_allowlist_is_cli_set_plus_untether_sugar() -> None:
    allowed = VALID_PERMISSION_MODES_BY_ENGINE["claude"]
    assert allowed == CLAUDE_CLI_PERMISSION_MODES | {CLAUDE_PLAN_AUTO_MODE}


# ---------------------------------------------------------------------------
# #741 — `auto` reaches the CLI unmodified; the sugar is renamed
# ---------------------------------------------------------------------------


def test_auto_passes_through_verbatim() -> None:
    """The load-bearing fix: `auto` must no longer be remapped to `plan`."""
    assert claude_cli_permission_mode("auto") == "auto"


def test_plan_auto_sugar_maps_to_cli_plan() -> None:
    assert claude_cli_permission_mode(CLAUDE_PLAN_AUTO_MODE) == "plan"


@pytest.mark.parametrize(
    "mode",
    ["default", "manual", "plan", "acceptEdits", "dontAsk", "bypassPermissions"],
)
def test_genuine_modes_pass_through(mode: str) -> None:
    assert claude_cli_permission_mode(mode) == mode


def test_none_stays_none() -> None:
    assert claude_cli_permission_mode(None) is None


def test_is_claude_plan_auto_only_matches_the_sugar() -> None:
    assert is_claude_plan_auto(CLAUDE_PLAN_AUTO_MODE) is True
    # The whole point of #741: upstream `auto` must NOT arm the plan-gate
    # rubber stamp.
    assert is_claude_plan_auto("auto") is False
    assert is_claude_plan_auto("plan") is False
    assert is_claude_plan_auto(None) is False


def test_legacy_spelling_constant_is_auto() -> None:
    """Documents what pre-0.35.5rc8 chat prefs hold."""
    assert LEGACY_CLAUDE_PLAN_AUTO_MODE == "auto"
    assert CLAUDE_PLAN_AUTO_MODE != LEGACY_CLAUDE_PLAN_AUTO_MODE


# ---------------------------------------------------------------------------
# Drift detection — fails when the installed CLI diverges from the constant
# ---------------------------------------------------------------------------


def _cli_declared_modes() -> set[str] | None:
    """Parse the choices commander prints when given an invalid value."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    try:
        proc = subprocess.run(
            [claude, "--permission-mode", "__untether_drift_probe__", "-p", "x"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = f"{proc.stderr}\n{proc.stdout}"
    marker = "Allowed choices are "
    if marker not in blob:
        return None
    tail = blob.split(marker, 1)[1]
    tail = tail.split(".", 1)[0]
    return {item.strip() for item in tail.split(",") if item.strip()}


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_no_drift_against_installed_cli() -> None:
    """Catch allowlist rot automatically instead of by periodic audit (#742).

    ``default`` is deliberately excluded from the comparison: the CLI accepts
    it but omits it from the choices list, because ``manual`` is its label.
    """
    declared = _cli_declared_modes()
    if declared is None:
        pytest.skip("could not parse permission-mode choices from the installed CLI")
    ours = set(CLAUDE_CLI_PERMISSION_MODES) - {"default"}
    assert declared == ours, (
        f"installed CLI advertises {sorted(declared)} but "
        f"CLAUDE_CLI_PERMISSION_MODES holds {sorted(ours)} (excluding 'default'); "
        "re-derive the constant and update the version note in run_options.py"
    )


# ---------------------------------------------------------------------------
# #742 — cron and engine-config paths must accept/reject identically
# ---------------------------------------------------------------------------


def _engine_config_accepts(mode: str, tmp_path) -> bool:
    from untether.config import ConfigError
    from untether.runners.claude import _validate_permission_mode

    try:
        _validate_permission_mode(mode, tmp_path / "untether.toml")
    except ConfigError:
        return False
    return True


def _cron_accepts(mode: str) -> bool:
    from pydantic import ValidationError

    from untether.triggers.settings import CronConfig

    try:
        CronConfig(
            id="c1",
            schedule="0 9 * * *",
            prompt="hi",
            engine="claude",
            permission_mode=mode,
        )
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("mode", sorted(VALID_PERMISSION_MODES_BY_ENGINE["claude"]))
def test_cron_and_engine_config_accept_every_allowed_mode(mode, tmp_path) -> None:
    assert _cron_accepts(mode) is True
    assert _engine_config_accepts(mode, tmp_path) is True


@pytest.mark.parametrize("mode", ["palan", "Plan", "", "  ", "yolo"])
def test_cron_and_engine_config_reject_identically(mode, tmp_path) -> None:
    """Parity: the same value must not pass in one table and fail in the other.

    Before #742, ``[engines.claude] permission_mode = "palan"`` sailed through
    config load and died at subprocess spawn instead.
    """
    assert _cron_accepts(mode) is False
    assert _engine_config_accepts(mode, tmp_path) is False


def test_engine_config_none_is_allowed(tmp_path) -> None:
    from untether.runners.claude import _validate_permission_mode

    assert _validate_permission_mode(None, tmp_path / "untether.toml") is None


def test_engine_config_rejects_non_string(tmp_path) -> None:
    from untether.config import ConfigError
    from untether.runners.claude import _validate_permission_mode

    with pytest.raises(ConfigError):
        _validate_permission_mode(123, tmp_path / "untether.toml")


def test_engine_config_strips_whitespace(tmp_path) -> None:
    from untether.runners.claude import _validate_permission_mode

    assert _validate_permission_mode("  plan  ", tmp_path / "untether.toml") == "plan"


def test_engine_config_error_names_the_key_and_path(tmp_path) -> None:
    from untether.config import ConfigError
    from untether.runners.claude import _validate_permission_mode

    path = tmp_path / "untether.toml"
    with pytest.raises(ConfigError) as excinfo:
        _validate_permission_mode("palan", path)
    message = str(excinfo.value)
    assert "claude.permission_mode" in message
    assert "palan" in message
    assert str(path) in message


# ---------------------------------------------------------------------------
# #741 — migration of the legacy `auto` spelling
# ---------------------------------------------------------------------------


def test_751_validate_permission_mode_no_longer_warns(tmp_path) -> None:
    """Hand-authored TOML `auto` keeps its new meaning and is not rewritten.

    #751: the operator WARN moved to the config audit (startup + reload,
    crons included); validation itself must stay silent so the two can't
    double-fire and the old reload-swallowing process latch can't return.
    """
    from structlog.testing import capture_logs

    import untether.runners.claude as claude_mod

    # capture_logs, not monkeypatch.setattr(claude_mod.logger, "warning", …):
    # restoring an attribute on structlog's lazy proxy pins a bound method
    # with the default processors, so later capture_logs() calls never see
    # this module's warnings (#791 full-suite isolation bug).
    path = tmp_path / "untether.toml"
    with capture_logs() as logs:
        assert claude_mod._validate_permission_mode("auto", path) == "auto"
        assert claude_mod._validate_permission_mode("auto", path) == "auto"
    assert [e for e in logs if e.get("log_level") == "warning"] == []
    assert not hasattr(claude_mod, "_LEGACY_AUTO_WARNED")


def test_chat_prefs_auto_migrates_to_plan_auto() -> None:
    """Stored prefs were written by our own UI, so `auto` meant the sugar."""
    from untether.telegram.engine_overrides import (
        EngineOverrides,
        migrate_legacy_overrides,
        migrate_legacy_permission_mode,
    )

    assert migrate_legacy_permission_mode("claude", "auto") == CLAUDE_PLAN_AUTO_MODE

    migrated = migrate_legacy_overrides(
        "claude", EngineOverrides(permission_mode="auto")
    )
    assert migrated is not None
    assert migrated.permission_mode == CLAUDE_PLAN_AUTO_MODE


# ---------------------------------------------------------------------------
# #741 — the migration must be ONE-SHOT, not applied on every read
# ---------------------------------------------------------------------------


def _write_prefs(path, *, mode: str, migrated_flag: bool | None) -> None:
    import json

    payload = {
        "version": 1,
        "chats": {
            "-100": {
                "default_engine": None,
                "trigger_mode": None,
                "context_project": None,
                "context_branch": None,
                "engine_overrides": {"claude": {"permission_mode": mode}},
            }
        },
    }
    if migrated_flag is not None:
        payload["permission_mode_migrated"] = migrated_flag
    path.write_text(json.dumps(payload))


@pytest.mark.anyio
async def test_legacy_prefs_file_is_migrated_once(tmp_path) -> None:
    """A pre-0.35.5rc8 file (no marker) has `auto` rewritten to `plan-auto`."""
    from untether.telegram.chat_prefs import ChatPrefsStore

    path = tmp_path / "telegram_chat_prefs_state.json"
    _write_prefs(path, mode="auto", migrated_flag=None)

    store = ChatPrefsStore(path)
    override = await store.get_engine_override(-100, "claude")
    assert override is not None
    assert override.permission_mode == CLAUDE_PLAN_AUTO_MODE

    # The rewrite is persisted, and the marker stops it running again.
    import json

    saved = json.loads(path.read_text())
    assert saved["permission_mode_migrated"] is True
    assert (
        saved["chats"]["-100"]["engine_overrides"]["claude"]["permission_mode"]
        == CLAUDE_PLAN_AUTO_MODE
    )


@pytest.mark.anyio
async def test_auto_chosen_after_migration_is_preserved(tmp_path) -> None:
    """The live regression this replaced a read-time rewrite to fix (#741).

    Once the file is marked migrated, `auto` is a value the user can pick from
    `/planmode` or `/config` to mean Claude Code's own auto mode. A read-time
    rewrite sent it straight back to `plan-auto`, so the new mode was
    unreachable through the UI — caught on `@untether_dev_bot`, where pressing
    **Auto** still spawned `--permission-mode plan`.
    """
    from untether.telegram.chat_prefs import ChatPrefsStore

    path = tmp_path / "telegram_chat_prefs_state.json"
    _write_prefs(path, mode="auto", migrated_flag=True)

    store = ChatPrefsStore(path)
    override = await store.get_engine_override(-100, "claude")
    assert override is not None
    assert override.permission_mode == "auto"


@pytest.mark.anyio
async def test_auto_written_after_migration_survives_a_reload(tmp_path) -> None:
    """End-to-end: choose `auto`, reload from disk, still `auto`."""
    from untether.telegram.chat_prefs import ChatPrefsStore
    from untether.telegram.engine_overrides import EngineOverrides

    path = tmp_path / "telegram_chat_prefs_state.json"
    _write_prefs(path, mode="auto", migrated_flag=None)

    store = ChatPrefsStore(path)
    # First read migrates the legacy value...
    first = await store.get_engine_override(-100, "claude")
    assert first is not None
    assert first.permission_mode == CLAUDE_PLAN_AUTO_MODE

    # ...then the user deliberately selects upstream auto.
    await store.set_engine_override(
        -100, "claude", EngineOverrides(permission_mode="auto")
    )

    reloaded = ChatPrefsStore(path)
    override = await reloaded.get_engine_override(-100, "claude")
    assert override is not None
    assert override.permission_mode == "auto"


@pytest.mark.anyio
async def test_migration_marks_a_clean_file_without_touching_values(tmp_path) -> None:
    from untether.telegram.chat_prefs import ChatPrefsStore

    path = tmp_path / "telegram_chat_prefs_state.json"
    _write_prefs(path, mode="plan", migrated_flag=None)

    store = ChatPrefsStore(path)
    override = await store.get_engine_override(-100, "claude")
    assert override is not None
    assert override.permission_mode == "plan"

    import json

    assert json.loads(path.read_text())["permission_mode_migrated"] is True


def test_migration_preserves_other_override_fields() -> None:
    from untether.telegram.engine_overrides import (
        EngineOverrides,
        migrate_legacy_overrides,
    )

    migrated = migrate_legacy_overrides(
        "claude",
        EngineOverrides(permission_mode="auto", model="opus", diff_preview=True),
    )
    assert migrated is not None
    assert migrated.model == "opus"
    assert migrated.diff_preview is True


def test_migration_is_claude_only() -> None:
    """Codex uses `auto` as a legitimate, differently-meaning value."""
    from untether.telegram.engine_overrides import migrate_legacy_permission_mode

    assert migrate_legacy_permission_mode("codex", "auto") == "auto"
    assert migrate_legacy_permission_mode("gemini", "auto") == "auto"


@pytest.mark.parametrize("mode", [None, "plan", "acceptEdits", CLAUDE_PLAN_AUTO_MODE])
def test_migration_leaves_other_modes_alone(mode) -> None:
    from untether.telegram.engine_overrides import migrate_legacy_permission_mode

    assert migrate_legacy_permission_mode("claude", mode) == mode


def test_migration_handles_none_overrides() -> None:
    from untether.telegram.engine_overrides import migrate_legacy_overrides

    assert migrate_legacy_overrides("claude", None) is None


# ---------------------------------------------------------------------------
# #749 — prompting vs autonomous classification (rc9 phase 01)
# ---------------------------------------------------------------------------


def test_749_prompting_mode_classification_matrix() -> None:
    """All 8 accepted values, pinned explicitly rather than by rule.

    A table beats a predicate here: the whole class of bug #749 fixes is
    "a mode was silently lumped in with the wrong group", and an
    enumeration makes that visible in the diff.
    """
    from untether.runners.run_options import is_claude_prompting_mode

    expected = {
        # Prompting: the CLI intends to ask the user.
        "default": True,
        "manual": True,
        "acceptEdits": True,
        # Autonomous: the CLI resolves earlier in the pipeline, or the user
        # asked for no prompts at all.
        "plan": False,
        CLAUDE_PLAN_AUTO_MODE: False,
        "auto": False,
        "dontAsk": False,
        "bypassPermissions": False,
    }
    actual = {mode: is_claude_prompting_mode(mode) for mode in expected}
    assert actual == expected

    # Every value the config layer accepts is classified — no silent gaps.
    assert set(expected) == set(VALID_PERMISSION_MODES_BY_ENGINE["claude"])


def test_749_unset_mode_is_not_a_prompting_mode() -> None:
    """`None` means the legacy `-p` path: no control channel, no stage 6."""
    from untether.runners.run_options import is_claude_prompting_mode

    assert is_claude_prompting_mode(None) is False


def test_749_unknown_mode_is_not_a_prompting_mode() -> None:
    """Forward-compatibility: an unrecognised mode keeps today's behaviour.

    Drift is caught by `test_no_drift_against_installed_cli`, which fails
    loudly when the CLI grows a mode we haven't classified.
    """
    from untether.runners.run_options import is_claude_prompting_mode

    assert is_claude_prompting_mode("someFutureMode") is False


# ---------------------------------------------------------------------------
# #750 — release-gate probe (decisions.md D-10)
#
# rc9 does NOT migrate to claude-agent-sdk-python.  It keeps the hand-rolled
# PTY + stream-json + control-registry layer, which depends entirely on
# `--permission-prompt-tool stdio` remaining accepted.  If Anthropic ever
# drops the flag, CI must fail here rather than five fleet hosts failing in
# production.
# ---------------------------------------------------------------------------

# The CLI this probe was last green against.  Bump when re-verified.
PROBED_CLI_VERSION = "2.1.229"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_750_permission_prompt_tool_flag_still_accepted() -> None:
    """`--permission-prompt-tool` must survive as a recognised option.

    The flag is **hidden** — it does not appear in ``claude --help`` — so its
    presence can only be established by spawning the binary.  Passing it with
    its argument deliberately **missing** makes commander answer the question
    without ever reaching the model:

      * flag known   -> ``option '--permission-prompt-tool <tool>' argument
        missing``
      * flag dropped -> ``unknown option '--permission-prompt-tool'``

    Both are argument-parse errors, so this probe costs **zero tokens** and
    needs no auth, no MCP servers and no network.  An earlier draft passed a
    bogus *tool name* instead; that reached the model on a machine with no MCP
    servers configured and billed a real turn.  Don't reintroduce it.
    """
    claude = shutil.which("claude")
    assert claude is not None
    try:
        proc = subprocess.run(
            [claude, "--permission-prompt-tool"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("could not spawn the installed CLI")

    blob = f"{proc.stderr}\n{proc.stdout}".lower()

    if "unknown option" in blob:
        pytest.fail(
            "the installed Claude CLI no longer accepts "
            "--permission-prompt-tool; Untether's control channel depends on "
            f"it (last green on CLI {PROBED_CLI_VERSION}). See #750 / "
            "decisions.md D-10 — this is the trigger for the "
            "claude-agent-sdk-python migration."
        )
    if "argument missing" not in blob:
        pytest.skip(
            "the installed CLI answered neither 'argument missing' nor "
            "'unknown option'; commander's error wording has changed and the "
            f"probe needs re-deriving (last green on CLI {PROBED_CLI_VERSION})"
        )


# ---------------------------------------------------------------------------
# #751 — config-time permission audit (startup + reload, crons included)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _audit_state():
    from untether.permission_audit import reset_permission_audit_state

    reset_permission_audit_state()
    yield
    reset_permission_audit_state()


def _triggers(*crons: dict, enabled: bool = True):
    from untether.triggers.settings import parse_trigger_config

    return parse_trigger_config(
        {
            "enabled": enabled,
            "crons": [
                {"schedule": "0 0 1 1 *", "prompt": "hi", **cron} for cron in crons
            ],
        }
    )


def _resolver(default: str = "claude", **projects: str | None):
    from untether.permission_audit import make_engine_resolver

    return make_engine_resolver(default_engine=default, project_engines=projects)


def _audit(engine_mode=None, triggers=None, resolver=None, spent=()):
    from untether.permission_audit import audit_claude_permission_modes

    return audit_claude_permission_modes(
        engine_mode=engine_mode,
        triggers=triggers,
        resolve_engine=resolver or _resolver(),
        spent_cron_ids=spent,
    )


def _warnings(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event and e["log_level"] == "warning"]


def test_751_audit_engine_auto_single_event() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    audit = _audit(engine_mode="auto")
    assert audit.auto_entries == ("engines.claude",)
    with capture_logs() as logs:
        log_permission_audit(audit, config_path="/x/untether.toml", reason="startup")
    (event,) = _warnings(logs, "claude.permission_mode.auto_semantics_changed")
    assert event["entries"] == ["engines.claude"]
    assert event["count"] == 1
    assert event["reason"] == "startup"
    assert CLAUDE_PLAN_AUTO_MODE in event["note"]


def test_751_audit_lists_engine_and_crons_in_one_event() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    audit = _audit(
        engine_mode="auto",
        triggers=_triggers(
            {"id": "a1", "permission_mode": "auto"},
            {"id": "a2", "engine": "claude", "permission_mode": "auto"},
        ),
    )
    with capture_logs() as logs:
        log_permission_audit(audit, config_path=None, reason="startup")
    (event,) = _warnings(logs, "claude.permission_mode.auto_semantics_changed")
    assert event["entries"] == [
        "engines.claude",
        "triggers.crons[a1]",
        "triggers.crons[a2]",
    ]
    assert event["count"] == 3


def test_751_audit_resolves_engine_like_runtime() -> None:
    """Cron engine → project default_engine → default, as at dispatch."""
    from pathlib import Path

    from untether.config import ProjectConfig, ProjectsConfig
    from untether.context import RunContext
    from untether.router import AutoRouter, RunnerEntry
    from untether.runners.mock import ScriptRunner
    from untether.transport_runtime import TransportRuntime

    crons = _triggers(
        {"id": "p_codex", "project": "cx", "permission_mode": "auto"},
        {"id": "p_claude", "project": "cl", "permission_mode": "auto"},
        {"id": "explicit_codex", "engine": "codex", "permission_mode": "auto"},
        {"id": "no_project", "permission_mode": "auto"},
    )
    audit = _audit(triggers=crons, resolver=_resolver("codex", cx="codex", cl="claude"))
    assert audit.auto_entries == ("triggers.crons[p_claude]",)

    # Parity with the real resolver the dispatcher uses.
    entries = [
        RunnerEntry(engine=e, runner=ScriptRunner([], engine=e))
        for e in ("codex", "claude")
    ]
    projects = ProjectsConfig(
        projects={
            alias: ProjectConfig(
                alias=alias,
                path=Path("/tmp"),
                worktrees_dir=Path(".w"),
                default_engine=engine,
            )
            for alias, engine in (("cx", "codex"), ("cl", "claude"))
        }
    )
    runtime = TransportRuntime(
        router=AutoRouter(entries=entries, default_engine="codex"),
        projects=projects,
        allowlist=None,
        config_path=None,
        plugin_configs=None,
        watch_config=False,
    )
    resolve = _resolver("codex", cx="codex", cl="claude")
    for cron in crons.crons:
        assert resolve(cron.engine, cron.project) == runtime.resolve_engine(
            engine_override=cron.engine,
            context=RunContext(project=cron.project),
        )


@pytest.mark.parametrize(
    "mode", ["plan-auto", "dontAsk", "bypassPermissions", "auto", None]
)
def test_751_audit_silent_when_no_auto_and_no_risk(mode) -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    cron = {"id": "c"} if mode is None else {"id": "c", "permission_mode": mode}
    audit = _audit(engine_mode="plan", triggers=_triggers(cron))
    assert audit.unattended == ()
    assert audit.invalid == ()
    with capture_logs() as logs:
        log_permission_audit(audit, config_path=None, reason="startup")
    names = {e["event"] for e in logs if e["log_level"] == "warning"}
    if mode == "auto":
        assert names == {"claude.permission_mode.auto_semantics_changed"}
    else:
        assert names == set()


def test_751_audit_skips_crons_when_triggers_disabled() -> None:
    audit = _audit(
        triggers=_triggers(
            {"id": "a", "permission_mode": "auto"},
            {"id": "d", "permission_mode": "default"},
            enabled=False,
        )
    )
    assert audit.empty


def test_751_audit_does_not_rewrite_toml(tmp_path, monkeypatch) -> None:
    import hashlib
    import os

    import untether.runtime_loader as runtime_loader
    from untether.runtime_loader import build_runtime_spec
    from untether.settings import load_settings

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")

    path = _write_config(
        tmp_path,
        engine_mode="auto",
        crons=[{"id": "c1", "permission_mode": "auto"}],
    )
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    settings, resolved = load_settings(path)
    build_runtime_spec(settings=settings, config_path=resolved)
    after = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    assert before == after


@pytest.mark.parametrize(
    ("mode", "waits_for"),
    [
        ("default", "tool approval"),
        ("manual", "tool approval"),
        ("acceptEdits", "tool approval"),
        ("plan", "plan approval"),
        ("plan-auto", None),
        ("auto", None),
        ("dontAsk", None),
        ("bypassPermissions", None),
    ],
)
def test_751_unattended_modes_matrix(mode, waits_for) -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    audit = _audit(triggers=_triggers({"id": "c2", "permission_mode": mode}))
    with capture_logs() as logs:
        log_permission_audit(audit, config_path=None, reason="startup")
    risk = _warnings(logs, "trigger.unattended_approval_risk")
    if waits_for is None:
        assert audit.unattended == ()
        assert risk == []
    else:
        assert audit.unattended == (("cron:c2", mode),)
        (event,) = risk
        assert event["phase"] == "config"
        assert event["entries"] == [
            {"trigger": "cron:c2", "mode": mode, "waits_for": waits_for}
        ]


def test_751_tap_required_modes_superset_of_prompting() -> None:
    from untether.runners.run_options import (
        _CLAUDE_PROMPTING_MODES,
        CLAUDE_TAP_REQUIRED_MODES,
    )

    assert _CLAUDE_PROMPTING_MODES | {"plan"} == CLAUDE_TAP_REQUIRED_MODES
    assert CLAUDE_PLAN_AUTO_MODE not in CLAUDE_TAP_REQUIRED_MODES


def test_751_spent_run_once_cron_not_an_unattended_risk() -> None:
    """Decision 10: a spent one-shot no longer fires; `auto` stays listed."""
    crons = _triggers(
        {"id": "once", "run_once": True, "permission_mode": "default"},
        {"id": "once_auto", "run_once": True, "permission_mode": "auto"},
        {"id": "again", "run_once": True, "permission_mode": "plan"},
    )
    audit = _audit(triggers=crons, spent={"once", "once_auto"})
    assert audit.unattended == (("cron:again", "plan"),)
    assert audit.auto_entries == ("triggers.crons[once_auto]",)


def test_751_invalid_cron_mode_for_resolved_claude_warns() -> None:
    """The cron validator can't know the default engine — the audit can."""
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    crons = _triggers({"id": "c3", "permission_mode": "bogus-typo"})
    audit = _audit(triggers=crons)
    assert audit.invalid == (("cron:c3", "bogus-typo"),)
    with capture_logs() as logs:
        log_permission_audit(audit, config_path=None, reason="startup")
    (event,) = _warnings(logs, "trigger.cron.permission_mode_invalid")
    assert event["trigger"] == "cron:c3"
    assert event["mode"] == "bogus-typo"
    assert "plan-auto" in event["allowed"]

    # Resolves to codex → not Claude's business.
    assert _audit(triggers=crons, resolver=_resolver("codex")).empty


def test_751_reload_same_findings_no_repeat() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    audit = _audit(engine_mode="auto")
    with capture_logs() as logs:
        log_permission_audit(audit, config_path=None, reason="startup")
        log_permission_audit(audit, config_path=None, reason="reload")
    assert len(_warnings(logs, "claude.permission_mode.auto_semantics_changed")) == 1


def test_751_reload_new_entry_reemits_with_reason_reload() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    with capture_logs() as logs:
        log_permission_audit(
            _audit(engine_mode="auto"), config_path=None, reason="startup"
        )
        log_permission_audit(
            _audit(
                engine_mode="auto",
                triggers=_triggers({"id": "n", "permission_mode": "auto"}),
            ),
            config_path=None,
            reason="reload",
        )
    first, second = _warnings(logs, "claude.permission_mode.auto_semantics_changed")
    assert first["reason"] == "startup"
    assert second["reason"] == "reload"
    assert second["entries"] == ["engines.claude", "triggers.crons[n]"]


def test_751_reload_cleared_then_readded_reemits() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import log_permission_audit

    with capture_logs() as logs:
        log_permission_audit(
            _audit(engine_mode="auto"), config_path=None, reason="startup"
        )
        log_permission_audit(
            _audit(engine_mode="plan"), config_path=None, reason="reload"
        )
        log_permission_audit(
            _audit(engine_mode="auto"), config_path=None, reason="reload"
        )
    events = _warnings(logs, "claude.permission_mode.auto_semantics_changed")
    assert [e["reason"] for e in events] == ["startup", "reload"]


def test_751_audit_entries_capped() -> None:
    from structlog.testing import capture_logs

    from untether.permission_audit import MAX_LOGGED_ENTRIES, log_permission_audit

    crons = _triggers(*({"id": f"c{i}", "permission_mode": "auto"} for i in range(60)))
    with capture_logs() as logs:
        log_permission_audit(_audit(triggers=crons), config_path=None, reason="startup")
    (event,) = _warnings(logs, "claude.permission_mode.auto_semantics_changed")
    assert len(event["entries"]) == MAX_LOGGED_ENTRIES
    assert event["count"] == 60


def test_751_normalise_manual_to_default() -> None:
    from untether.runners.run_options import normalise_claude_cli_mode

    # Probe P1b (CLI 2.1.285): `--permission-mode manual` reports `default`.
    assert normalise_claude_cli_mode("manual") == "default"
    assert normalise_claude_cli_mode("default") == "default"
    assert normalise_claude_cli_mode(None) is None


def test_751_normalise_plan_auto_to_plan() -> None:
    from untether.runners.run_options import normalise_claude_cli_mode

    assert normalise_claude_cli_mode("plan-auto") == "plan"
    for mode in ("plan", "auto", "acceptEdits", "dontAsk", "bypassPermissions"):
        assert normalise_claude_cli_mode(mode) == mode


def _write_config(tmp_path, *, engine_mode=None, crons=(), extra: str = ""):
    """A minimal real untether.toml (claude default engine)."""
    import json as _json

    lines = [
        'default_engine = "claude"',
        'transport = "telegram"',
        "[transports.telegram]",
        'bot_token = "123:abc"',
        "chat_id = 1",
        "allowed_user_ids = [1]",
    ]
    if engine_mode is not None:
        lines += ["[claude]", f"permission_mode = {_json.dumps(engine_mode)}"]
    if crons:
        lines += ["[triggers]", "enabled = true"]
        for cron in crons:
            lines.append("[[triggers.crons]]")
            body = {"schedule": "0 0 1 1 *", "prompt": "hi", **cron}
            lines += [f"{k} = {_json.dumps(v)}" for k, v in body.items()]
    path = tmp_path / "untether.toml"
    path.write_text("\n".join(lines) + "\n" + extra)
    return path
