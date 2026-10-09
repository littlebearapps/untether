"""Tests for prompt preamble injection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from untether.runner_bridge import (
    _DEFAULT_PREAMBLE,
    _apply_preamble,
)
from untether.runners.claude import (
    _PREPEND_BODY_CAP,
    _PREPEND_LENGTH_GATE,
    _prepend_exitplanmode_plan,
)
from untether.settings import PreambleSettings


def test_default_preamble_prepended() -> None:
    """Default preamble is prepended when settings use defaults."""
    result = _apply_preamble("fix the bug", "claude")
    assert result.startswith("[Untether]")
    assert result.endswith("fix the bug")
    assert "\n\n---\n\n" in result
    assert _DEFAULT_PREAMBLE in result


def test_preamble_disabled() -> None:
    """Prompt is returned unchanged when preamble is disabled."""
    cfg = PreambleSettings(enabled=False)
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug", "claude")
    assert result == "fix the bug"


def test_custom_preamble_text() -> None:
    """Custom preamble text overrides the default."""
    cfg = PreambleSettings(text="Custom context for this agent.")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug", "claude")
    assert result.startswith("Custom context for this agent.")
    assert result.endswith("fix the bug")
    assert _DEFAULT_PREAMBLE not in result


def test_preamble_empty_text() -> None:
    """Empty text string effectively disables the preamble."""
    cfg = PreambleSettings(text="")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug", "claude")
    assert result == "fix the bug"


def test_preamble_settings_defaults() -> None:
    """PreambleSettings defaults to enabled with no custom text."""
    cfg = PreambleSettings()
    assert cfg.enabled is True
    assert cfg.text is None


def test_default_preamble_includes_outbox_instructions() -> None:
    """Default preamble tells agents about the .untether-outbox/ delivery mechanism."""
    assert ".untether-outbox/" in _DEFAULT_PREAMBLE
    assert "/file get" in _DEFAULT_PREAMBLE


def test_preamble_mentions_outbox_freshness() -> None:
    """#924: only files written during the run are sent; agents must copy an
    older file in again to resend it."""
    assert "during this run are sent" in _DEFAULT_PREAMBLE
    assert "copy it in again" in _DEFAULT_PREAMBLE


def test_default_preamble_warns_against_systemctl_restart() -> None:
    """#547 axis 1: agents routinely follow ``edit untether.toml`` with
    ``systemctl --user restart untether`` because their training data is
    full of "restart the service after config changes". Untether already
    hot-reloads the file; the restart drops the agent's own final answer
    (drain timeout + outbox.fail_pending). The preamble must tell agents
    explicitly NOT to restart after editing config."""
    # Headline: hot-reload mentioned
    assert "hot-reload" in _DEFAULT_PREAMBLE.lower()
    # Explicit "do NOT" framing
    assert "Do NOT" in _DEFAULT_PREAMBLE
    assert "systemctl" in _DEFAULT_PREAMBLE
    assert "restart untether" in _DEFAULT_PREAMBLE
    # Consequence spelled out so agents understand why
    assert "drop" in _DEFAULT_PREAMBLE.lower() or "lost" in _DEFAULT_PREAMBLE.lower()
    # Restart-only keys mentioned so agents know the exception
    assert "bot_token" in _DEFAULT_PREAMBLE
    assert "chat_id" in _DEFAULT_PREAMBLE


def test_preamble_config_path_and_triggers_restart() -> None:
    """#927: the preamble must not assume the default unit/config path is the
    running instance, and lists `[triggers] enabled` as restart-only (#894)."""
    assert "default `~/.untether/untether.toml`" in _DEFAULT_PREAMBLE
    assert "[triggers] enabled" in _DEFAULT_PREAMBLE
    assert "launchctl kickstart" in _DEFAULT_PREAMBLE
    assert "untether-*" in _DEFAULT_PREAMBLE


# ───── #508 / #515 — plan-mode preamble clauses ────────────────────────


def test_default_preamble_has_exitplanmode_plan_body_clause() -> None:
    """A1 (#515 tuning): ExitPlanMode plan body must be a concise 3-5
    bullet summary - never just a file path, but also not an expanded
    substantive summary (rc11 over-fire). The plan is shown for
    approval, not as the final deliverable."""
    assert "ExitPlanMode" in _DEFAULT_PREAMBLE
    assert "concise 3–5 bullet" in _DEFAULT_PREAMBLE
    assert "never just a file path" in _DEFAULT_PREAMBLE
    assert "shown to the user for approval, not as the final deliverable" in (
        _DEFAULT_PREAMBLE
    )


def test_default_preamble_has_post_approval_brief_summary_clause() -> None:
    """A2 (#515 tuning): After ExitPlanMode is approved, the final
    Telegram message should be a brief CLI-style summary (3-7 bullets
    or 1-2 short paragraphs, ~500-1500 chars). Do NOT re-paste the full
    plan content - rc11 told Claude to "repeat substantive findings"
    which produced 30k-char finals."""
    assert "After `ExitPlanMode` is approved" in _DEFAULT_PREAMBLE
    assert "brief CLI-style summary" in _DEFAULT_PREAMBLE
    assert "3–7 bullets" in _DEFAULT_PREAMBLE
    assert "Do NOT re-paste the full plan content" in _DEFAULT_PREAMBLE
    assert "~500–1500 characters" in _DEFAULT_PREAMBLE


def test_default_preamble_summary_block_asks_for_headline_summary() -> None:
    """A3 (#515 tuning): the ## Summary block's Plan/Document Created
    bullet asks for a pointer + 3-5 bullet headline summary, not a
    re-paste of the full plan content. The user already saw the plan
    during approval."""
    assert "3–5 bullet headline summary" in _DEFAULT_PREAMBLE
    assert "not a re-paste of the full content" in _DEFAULT_PREAMBLE


def test_default_preamble_does_not_drive_verbose_post_approval_text() -> None:
    """Regression for #515: ensure the rc11 verbosity-driving phrases
    that produced 42k-char Telegram finals are no longer present."""
    # rc11 A2 phrase that told Claude to repeat the full content
    assert "MUST repeat the substantive findings or decisions" not in (
        _DEFAULT_PREAMBLE
    )
    # rc11 A1 phrase that told Claude to expand bullets into a
    # substantive summary for research/audit tasks
    assert "expand the bullets into a substantive summary" not in _DEFAULT_PREAMBLE
    # rc11 A3 phrase that told Claude to put full findings inline
    assert "do not require the user to open the file" not in _DEFAULT_PREAMBLE


# ───── #508 / #515 Layer E — _prepend_exitplanmode_plan helper ─────────


def test_prepend_exitplanmode_plan_when_final_answer_short() -> None:
    """The original #508 repro: post-approval result is brief (584
    chars in the live capture). Plan body must be prepended so the user
    sees the substantive findings in chat."""
    plan = "- Finding 1\n- Finding 2\n- Recommend X"
    short_final = "Plan approved — research is complete. See file."
    assert len(short_final) < _PREPEND_LENGTH_GATE

    result = _prepend_exitplanmode_plan(short_final, plan)

    assert "📋 Plan (approved):" in result
    assert plan in result
    assert short_final in result
    # Plan body comes before the brief acknowledgement (separator)
    assert result.index(plan) < result.index(short_final)


def test_prepend_exitplanmode_plan_skipped_when_answer_substantive() -> None:
    """#515: when the post-approval text is ≥ ``_PREPEND_LENGTH_GATE``
    chars (Claude wrote a real CLI-style summary), do NOT prepend the
    plan body — the post-approval text is doing the job. This is the
    load-bearing change vs rc11/rc12 where the substring check failed
    on paraphrased summaries and double-shipped content."""
    plan = "- Finding 1\n- Finding 2\n- Recommend X"
    substantive_final = (
        "I investigated the issue and here is what I found:\n\n"
        "- Headline 1: module X had a regression introduced in commit abc123\n"
        "- Headline 2: the root cause was a missing null guard in the parser\n"
        "- Headline 3: rolled back commit abc123 and added a regression test\n"
        "- Headline 4: next step is to backfill the affected rows Monday\n\n"
        "Decisions made: kept the legacy code path for one more release cycle to\n"
        "give downstream consumers time to migrate; full removal scheduled for the\n"
        "next minor version once telemetry confirms zero active callers.\n\n"
        "Next steps: open a follow-up issue for the backfill, send a heads-up in\n"
        "the team channel, and re-run the daily-audit cron tomorrow morning to\n"
        "confirm the regression is gone from the verification window.\n"
    )
    assert len(substantive_final) >= _PREPEND_LENGTH_GATE

    result = _prepend_exitplanmode_plan(substantive_final, plan)

    assert result == substantive_final
    assert "📋 Plan (approved):" not in result


def test_prepend_exitplanmode_plan_caps_long_plan_body() -> None:
    """#515: when Layer E does fire and the captured plan body is
    longer than ``_PREPEND_BODY_CAP``, truncate it to avoid runaway
    finals. Live staging captures had 5,000-char plan bodies that got
    prepended in full."""
    plan = "x" * (_PREPEND_BODY_CAP + 1000)
    short_final = "ok"

    result = _prepend_exitplanmode_plan(short_final, plan)

    assert "📋 Plan (approved):" in result
    assert "plan truncated" in result
    # Plan body in the result should be ~_PREPEND_BODY_CAP chars (plus
    # the truncation suffix), not the full 2500-char original.
    assert "x" * (_PREPEND_BODY_CAP + 100) not in result


def test_prepend_exitplanmode_plan_skipped_when_already_substring() -> None:
    """Secondary skip rule: when the plan body is a literal substring
    of the final answer (rare with rc13 wording, but a cheap belt-and-
    braces check), do not prepend."""
    plan = "- Finding 1\n- Finding 2"
    final = "Here is what I found:\n- Finding 1\n- Finding 2\n\nNext steps: ..."
    assert len(final) < _PREPEND_LENGTH_GATE

    result = _prepend_exitplanmode_plan(final, plan)

    assert result == final
    assert "📋 Plan (approved):" not in result


def test_prepend_exitplanmode_plan_skipped_when_no_plan_body() -> None:
    """No plan body captured → return the final answer unchanged."""
    final = "ok"
    assert _prepend_exitplanmode_plan(final, None) == final
    assert _prepend_exitplanmode_plan(final, "") == final
    assert _prepend_exitplanmode_plan(final, "   \n\t") == final


def test_prepend_exitplanmode_plan_handles_empty_final_answer() -> None:
    """If the post-approval result yields an entirely empty final answer
    (no fallback text either), the plan body becomes the full answer."""
    plan = "- Finding 1"
    result = _prepend_exitplanmode_plan("", plan)
    assert result.startswith("📋 Plan (approved):")
    assert plan in result


def test_prepend_exitplanmode_plan_handles_none_final_answer() -> None:
    """None ``final_answer`` is handled the same as empty string."""
    plan = "- Finding 1"
    result = _prepend_exitplanmode_plan(None, plan)
    assert "📋 Plan (approved):" in result
    assert plan in result


# ── capability-driven preamble (#558, 08 §11, D23) ──────────────────────────

# sha256 of ``_apply_preamble("fix the bug")`` at 4d6b5c7e (before the
# split), per ask-questions toggle. Claude's text must never move.
_CLAUDE_SNAPSHOT = {
    None: (3356, "e4a9c6527ea152844888e8bbf73f33487602f167936b3e5c81490249e0658a48"),
    True: (3356, "e4a9c6527ea152844888e8bbf73f33487602f167936b3e5c81490249e0658a48"),
    False: (3346, "ac5f9eec3c255a2113b536aec88df19a571fdc201e151641ae82a162fc2a208b"),
}
_REPLY_CLARIFICATION = (
    "If you need clarification, say so in your final reply — the user will"
    " answer in their next message."
)


def _preamble_for(engine: str, ask: bool | None = None, cfg=None) -> str:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    options = None if ask is None else EngineRunOptions(ask_questions=ask)
    with (
        patch(
            "untether.runner_bridge._load_preamble_settings",
            return_value=cfg or PreambleSettings(),
        ),
        apply_run_options(options),
    ):
        return _apply_preamble("fix the bug", engine)


@pytest.mark.parametrize("ask", [None, True, False])
def test_preamble_claude_byte_identical(ask: bool | None) -> None:
    import hashlib

    result = _preamble_for("claude", ask)
    length, digest = _CLAUDE_SNAPSHOT[ask]
    assert len(result) == length
    assert hashlib.sha256(result.encode()).hexdigest() == digest


def test_default_preamble_constant_byte_identical() -> None:
    """``_DEFAULT_PREAMBLE`` (what ``[preamble] text`` is compared against)
    is still the full Claude text, assembled from the split parts."""
    import hashlib

    from untether.runner_bridge import _CLAUDE_PLAN_MODE_BLOCK

    assert len(_DEFAULT_PREAMBLE) == 3200
    assert hashlib.sha256(_DEFAULT_PREAMBLE.encode()).hexdigest() == (
        "4b0a5c2560b7328635534b3d6bfe77522ea92d3770d037e1d096a7d07c907fd5"
    )
    assert _CLAUDE_PLAN_MODE_BLOCK in _DEFAULT_PREAMBLE
    assert _CLAUDE_PLAN_MODE_BLOCK.startswith("Plan-mode requirements")
    assert _CLAUDE_PLAN_MODE_BLOCK.endswith("\n\n")


@pytest.mark.parametrize("engine", ["antigravity", "codex", "opencode", "pi"])
@pytest.mark.parametrize("ask", [None, True, False])
def test_preamble_non_claude_gets_reply_clarification(
    engine: str, ask: bool | None
) -> None:
    result = _preamble_for(engine, ask)
    assert "AskUserQuestion" not in result
    assert "ExitPlanMode" not in result
    assert "Plan-mode requirements" not in result
    preamble, _, prompt = result.partition("\n\n---\n\n")
    assert prompt == "fix the bug"
    assert preamble.endswith("\n\n" + _REPLY_CLARIFICATION)
    assert preamble.count(_REPLY_CLARIFICATION) == 1


def test_preamble_non_claude_is_claude_minus_the_claude_only_parts() -> None:
    """Nothing else moved: the other engines get exactly Claude's text
    without the plan-mode block, with the reply sentence swapped in."""
    from untether.runner_bridge import _CLAUDE_PLAN_MODE_BLOCK

    claude = _preamble_for("claude")
    ask_paragraph = (
        "When you need clarification from the user, use AskUserQuestion "
        "with clear options. The user will see interactive buttons to choose from."
    )
    assert ask_paragraph in claude
    expected = claude.replace(_CLAUDE_PLAN_MODE_BLOCK, "").replace(
        ask_paragraph, _REPLY_CLARIFICATION
    )
    for engine in ("antigravity", "codex", "opencode", "pi"):
        assert _preamble_for(engine) == expected
    # Everything every engine needs is still there.
    for needle in (
        "The user can ONLY see your final assistant text messages",
        "Do NOT restart the Untether service",
        ".untether-outbox/",
        "## Summary",
        "### Decisions Needed (if any)",
    ):
        assert needle in expected


@pytest.mark.parametrize("engine", ["claude", "antigravity", "codex"])
def test_custom_preamble_text_still_verbatim(engine: str) -> None:
    """A ``[preamble] text`` override is never edited — plan-mode wording
    included; only the appended sentence follows the engine."""
    custom = (
        "Custom context.\n\nPlan-mode requirements (when you call `ExitPlanMode`): x"
    )
    result = _preamble_for(engine, cfg=PreambleSettings(text=custom))
    assert result.startswith(custom + "\n\n")
    if engine == "claude":
        assert "use AskUserQuestion" in result
    else:
        assert result.startswith(custom + "\n\n" + _REPLY_CLARIFICATION)


def test_custom_text_equal_to_default_is_left_whole_for_every_engine() -> None:
    result = _preamble_for("codex", cfg=PreambleSettings(text=_DEFAULT_PREAMBLE))
    assert result.startswith(_DEFAULT_PREAMBLE)


def test_preamble_capability_sets() -> None:
    """rc3 adds antigravity to the ask set when its buttons exist."""
    from untether.telegram.engine_overrides import (
        ASK_QUESTIONS_SUPPORTED_ENGINES,
        PLAN_EXIT_TOOL_ENGINES,
    )

    assert frozenset({"claude"}) == ASK_QUESTIONS_SUPPORTED_ENGINES
    assert frozenset({"claude"}) == PLAN_EXIT_TOOL_ENGINES


def test_preamble_applied_log_len_per_engine() -> None:
    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        _preamble_for("claude")
        _preamble_for("antigravity")
    lens = [e["preamble_len"] for e in logs if e["event"] == "preamble.applied"]
    assert lens[0] == 3338  # 08 exit gate: Claude's is the same as before
    assert lens[1] < lens[0]


def test_apply_preamble_call_site_passes_engine() -> None:
    """The one production call passes the run's engine (never a default)."""
    import ast
    import inspect

    import untether.runner_bridge as bridge

    tree = ast.parse(inspect.getsource(bridge))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_apply_preamble"
    ]
    assert len(calls) == 1
    (call,) = calls
    assert len(call.args) == 2
    assert ast.unparse(call.args[1]) == "runner.engine"
    params = inspect.signature(bridge._apply_preamble).parameters
    assert params["engine"].default is inspect.Parameter.empty
