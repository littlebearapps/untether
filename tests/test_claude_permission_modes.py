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


def test_toml_auto_warns_once_and_keeps_the_new_meaning(tmp_path, monkeypatch) -> None:
    """Hand-authored TOML is not rewritten — the operator gets a WARN."""
    import untether.runners.claude as claude_mod

    monkeypatch.setattr(claude_mod, "_LEGACY_AUTO_WARNED", False)
    from structlog.testing import capture_logs

    # capture_logs, not monkeypatch.setattr(claude_mod.logger, "warning", …):
    # restoring an attribute on structlog's lazy proxy pins a bound method
    # with the default processors, so later capture_logs() calls never see
    # this module's warnings (#791 full-suite isolation bug).
    path = tmp_path / "untether.toml"
    with capture_logs() as logs:
        assert claude_mod._validate_permission_mode("auto", path) == "auto"
        assert claude_mod._validate_permission_mode("auto", path) == "auto"
    warnings = [(e["event"], e) for e in logs if e.get("log_level") == "warning"]

    # One-shot per process, not per run.
    assert len(warnings) == 1
    event, kwargs = warnings[0]
    assert event == "claude.permission_mode.auto_semantics_changed"
    assert CLAUDE_PLAN_AUTO_MODE in kwargs["note"]


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
