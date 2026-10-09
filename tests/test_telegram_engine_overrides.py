import pytest

from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.engine_overrides import (
    EngineOverrides,
    merge_overrides,
    resolve_override_value,
)
from untether.telegram.topic_state import TopicStateStore


def test_merge_overrides_prefers_topic_values() -> None:
    topic = EngineOverrides(model=None, reasoning="high")
    chat = EngineOverrides(model="gpt-4.1-mini", reasoning=None)
    merged = merge_overrides(topic, chat)

    assert merged is not None
    assert merged.model == "gpt-4.1-mini"
    assert merged.reasoning == "high"


def test_resolve_override_value_tracks_sources() -> None:
    topic = EngineOverrides(model="gpt-4.1", reasoning=None)
    chat = EngineOverrides(model="gpt-4.1-mini", reasoning="low")
    resolution = resolve_override_value(
        topic_override=topic,
        chat_override=chat,
        field="model",
    )

    assert resolution.value == "gpt-4.1"
    assert resolution.source == "topic_override"
    assert resolution.topic_value == "gpt-4.1"
    assert resolution.chat_value == "gpt-4.1-mini"


@pytest.mark.anyio
async def test_chat_prefs_engine_overrides_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram_chat_prefs_state.json"
    store = ChatPrefsStore(path)
    await store.set_engine_override(
        123,
        "codex",
        EngineOverrides(model="gpt-4.1-mini", reasoning="low"),
    )

    override = await store.get_engine_override(123, "codex")
    assert override is not None
    assert override.model == "gpt-4.1-mini"
    assert override.reasoning == "low"

    store2 = ChatPrefsStore(path)
    override2 = await store2.get_engine_override(123, "codex")
    assert override2 is not None
    assert override2.model == "gpt-4.1-mini"
    assert override2.reasoning == "low"

    await store2.set_engine_override(
        123,
        "codex",
        EngineOverrides(model=None, reasoning="low"),
    )
    override3 = await store2.get_engine_override(123, "codex")
    assert override3 is not None
    assert override3.model is None
    assert override3.reasoning == "low"

    await store2.set_engine_override(
        123,
        "codex",
        EngineOverrides(model=None, reasoning=None),
    )
    override4 = await store2.get_engine_override(123, "codex")
    assert override4 is None


@pytest.mark.anyio
async def test_topic_state_engine_overrides_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram_topics_state.json"
    store = TopicStateStore(path)
    await store.set_engine_override(
        1,
        10,
        "codex",
        EngineOverrides(model="gpt-4.1", reasoning="medium"),
    )

    override = await store.get_engine_override(1, 10, "codex")
    assert override is not None
    assert override.model == "gpt-4.1"
    assert override.reasoning == "medium"

    store2 = TopicStateStore(path)
    override2 = await store2.get_engine_override(1, 10, "codex")
    assert override2 is not None
    assert override2.model == "gpt-4.1"
    assert override2.reasoning == "medium"


def test_merge_overrides_diff_preview_topic_wins() -> None:
    topic = EngineOverrides(diff_preview=False)
    chat = EngineOverrides(diff_preview=True)
    merged = merge_overrides(topic, chat)
    assert merged is not None
    assert merged.diff_preview is False


def test_merge_overrides_diff_preview_chat_fallback() -> None:
    topic = EngineOverrides(diff_preview=None)
    chat = EngineOverrides(diff_preview=True)
    merged = merge_overrides(topic, chat)
    assert merged is not None
    assert merged.diff_preview is True


def test_get_engine_default_reasoning_claude(tmp_path) -> None:
    """Reads effortLevel from Claude settings.json."""
    import json
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_reasoning

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"effortLevel": "high"}))

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_reasoning("claude") == "high"


def test_get_engine_default_reasoning_claude_max(tmp_path) -> None:
    """Reads max effort level from Claude settings.json."""
    import json
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_reasoning

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"effortLevel": "max"}))

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_reasoning("claude") == "max"


def test_get_engine_default_reasoning_no_file(tmp_path) -> None:
    """Returns None when settings file doesn't exist."""
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_reasoning

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_reasoning("claude") is None


def test_get_engine_default_reasoning_unsupported_engine() -> None:
    """Returns None for engines without config file support."""
    from untether.telegram.engine_overrides import get_engine_default_reasoning

    assert get_engine_default_reasoning("codex") is None
    assert get_engine_default_reasoning("gemini") is None


def test_get_engine_default_model_opencode(tmp_path) -> None:
    """#475: reads the top-level model string from opencode.json."""
    import json
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_model

    oc_dir = tmp_path / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(
        json.dumps({"model": "deepseek/deepseek-v4-pro"})
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_model("opencode") == "deepseek/deepseek-v4-pro"


def test_get_engine_default_model_pi_joins_provider(tmp_path) -> None:
    """#475: joins defaultProvider/defaultModel from Pi settings.json."""
    import json
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_model

    pi_dir = tmp_path / ".pi" / "agent"
    pi_dir.mkdir(parents=True)
    (pi_dir / "settings.json").write_text(
        json.dumps({"defaultProvider": "kimi-coding", "defaultModel": "k2p6"})
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_model("pi") == "kimi-coding/k2p6"


def test_get_engine_default_model_pi_model_only(tmp_path) -> None:
    """#475: a Pi settings file without defaultProvider returns bare model."""
    import json
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_model

    pi_dir = tmp_path / ".pi" / "agent"
    pi_dir.mkdir(parents=True)
    (pi_dir / "settings.json").write_text(json.dumps({"defaultModel": "k2p6"}))

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_model("pi") == "k2p6"


def test_get_engine_default_model_missing_or_malformed(tmp_path) -> None:
    """#475: absent or garbage settings files resolve to None (fallback to
    the static placeholder hint)."""
    from unittest.mock import patch

    from untether.telegram.engine_overrides import get_engine_default_model

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_model("opencode") is None
        assert get_engine_default_model("pi") is None

    oc_dir = tmp_path / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text("{not json")
    with patch("pathlib.Path.home", return_value=tmp_path):
        assert get_engine_default_model("opencode") is None


def test_get_engine_default_model_auto_routed_engines() -> None:
    """#475: auto-routed engines keep the static hint (None here)."""
    from untether.telegram.engine_overrides import get_engine_default_model

    for engine in ("claude", "codex", "gemini", "amp"):
        assert get_engine_default_model(engine) is None


def test_get_reasoning_label() -> None:
    """Engine-specific reasoning labels."""
    from untether.telegram.engine_overrides import get_reasoning_label

    assert get_reasoning_label("claude") == "Effort"
    assert get_reasoning_label("codex") == "Reasoning"
    assert get_reasoning_label("pi") == "Thinking"
    assert get_reasoning_label("gemini") == "Reasoning"
    assert get_reasoning_label("amp") == "Reasoning"


# ---------------------------------------------------------------------------
# loop_enabled (#289) — per-chat /loop mode toggle


def test_loop_enabled_default_none() -> None:
    """Default state is None — meaning 'inherit global [loop] enabled'."""
    overrides = EngineOverrides()
    assert overrides.loop_enabled is None


def test_merge_overrides_loop_enabled_topic_wins() -> None:
    topic = EngineOverrides(loop_enabled=True)
    chat = EngineOverrides(loop_enabled=False)
    merged = merge_overrides(topic, chat)
    assert merged is not None
    assert merged.loop_enabled is True


def test_merge_overrides_loop_enabled_chat_fallback() -> None:
    topic = EngineOverrides(loop_enabled=None)
    chat = EngineOverrides(loop_enabled=True)
    merged = merge_overrides(topic, chat)
    assert merged is not None
    assert merged.loop_enabled is True


def test_merge_overrides_loop_enabled_both_none() -> None:
    """Both unset → merge_overrides returns None (no overrides)."""
    topic = EngineOverrides(loop_enabled=None)
    chat = EngineOverrides(loop_enabled=None)
    merged = merge_overrides(topic, chat)
    assert merged is None


@pytest.mark.anyio
async def test_chat_prefs_loop_enabled_roundtrip(tmp_path) -> None:
    """Per-chat loop_enabled survives store reload."""
    path = tmp_path / "telegram_chat_prefs_state.json"
    store = ChatPrefsStore(path)
    await store.set_engine_override(
        456,
        "claude",
        EngineOverrides(loop_enabled=True),
    )

    override = await store.get_engine_override(456, "claude")
    assert override is not None
    assert override.loop_enabled is True

    store2 = ChatPrefsStore(path)
    override2 = await store2.get_engine_override(456, "claude")
    assert override2 is not None
    assert override2.loop_enabled is True


def test_loop_supported_engines_constant_is_claude_only() -> None:
    """LOOP_SUPPORTED_ENGINES is intentionally Claude-only — other engines
    don't expose CronCreate / ScheduleWakeup."""
    from untether.telegram.engine_overrides import LOOP_SUPPORTED_ENGINES

    assert frozenset({"claude"}) == LOOP_SUPPORTED_ENGINES


# --- #416: Codex `minimal` retired; stale prefs sanitised at resolution ---


def test_codex_reasoning_levels_exclude_minimal() -> None:
    from untether.telegram.engine_overrides import allowed_reasoning_levels

    assert "minimal" not in allowed_reasoning_levels("codex")
    assert allowed_reasoning_levels("codex") == ("low", "medium", "high", "xhigh")


def test_no_engine_offers_minimal() -> None:
    from untether.telegram.engine_overrides import (
        REASONING_LEVELS,
        REASONING_SUPPORTED_ENGINES,
        allowed_reasoning_levels,
    )

    assert "minimal" not in REASONING_LEVELS
    for engine in REASONING_SUPPORTED_ENGINES:
        assert "minimal" not in allowed_reasoning_levels(engine), engine


def test_claude_reasoning_levels_unchanged() -> None:
    from untether.telegram.engine_overrides import allowed_reasoning_levels

    assert allowed_reasoning_levels("claude") == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_drop_unsupported_reasoning_codex_minimal() -> None:
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    raw = EngineRunOptions(model="gpt-5.5", reasoning="minimal", permission_mode="x")
    out = drop_unsupported_reasoning("codex", raw)
    assert out is not None
    assert out.reasoning is None
    assert out.ignored_reasoning == "minimal"
    assert out.model == "gpt-5.5"
    assert out.permission_mode == "x"


def test_drop_unsupported_reasoning_idempotent() -> None:
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    once = drop_unsupported_reasoning("codex", EngineRunOptions(reasoning="minimal"))
    twice = drop_unsupported_reasoning("codex", once)
    assert twice == once
    assert twice is once


def test_drop_unsupported_reasoning_allowed_level_same_object() -> None:
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    opts = EngineRunOptions(reasoning="high")
    assert drop_unsupported_reasoning("codex", opts) is opts


def test_drop_unsupported_reasoning_unsupported_engine_untouched() -> None:
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    opts = EngineRunOptions(reasoning="high")
    assert drop_unsupported_reasoning("opencode", opts) is opts


def test_drop_unsupported_reasoning_none() -> None:
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    assert drop_unsupported_reasoning("codex", None) is None


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["chat", "topic"])
async def test_resolve_engine_run_options_sanitises_stale_chat_and_topic_minimal(
    tmp_path, level: str
) -> None:
    from untether.telegram.loop import _resolve_engine_run_options

    chat_prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    topic_store = TopicStateStore(tmp_path / "telegram_topics_state.json")
    stale = EngineOverrides(model="gpt-5.5", reasoning="minimal")
    if level == "chat":
        await chat_prefs.set_engine_override(1, "codex", stale)
    else:
        await topic_store.set_engine_override(1, 10, "codex", stale)

    first = await _resolve_engine_run_options(1, 10, "codex", chat_prefs, topic_store)
    second = await _resolve_engine_run_options(1, 10, "codex", chat_prefs, topic_store)
    assert first is not None
    assert first.reasoning is None
    assert first.ignored_reasoning == "minimal"
    assert first.model == "gpt-5.5"
    assert first == second


# ── Antigravity effort (#558 phase 05, D29) ─────────────────────────────────


def test_antigravity_reasoning_levels_low_medium_high() -> None:
    """The static button ceiling: no agy model accepts xhigh or max
    (probes/1.3.x/z-model-effort-combos.txt); the drift suite re-checks."""
    from untether.telegram.engine_overrides import allowed_reasoning_levels

    assert allowed_reasoning_levels("antigravity") == ("low", "medium", "high")


def test_reasoning_levels_global_unchanged() -> None:
    """PR #766 carried a pre-#416 copy with ``minimal``; the fallback must
    stay as it is."""
    from untether.telegram.engine_overrides import REASONING_LEVELS

    assert REASONING_LEVELS == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("level", ["xhigh", "max"])
def test_drop_unsupported_reasoning_antigravity_xhigh_and_max_dropped(
    level: str,
) -> None:
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    out = drop_unsupported_reasoning("antigravity", EngineRunOptions(reasoning=level))
    assert out is not None
    assert out.reasoning is None
    assert out.ignored_reasoning == level


def test_antigravity_reasoning_label_effort() -> None:
    from untether.telegram.engine_overrides import get_reasoning_label

    assert get_reasoning_label("antigravity") == "Effort"


def test_antigravity_supports_reasoning_for_cron_validation() -> None:
    from untether.telegram.engine_overrides import (
        REASONING_SUPPORTED_ENGINES,
        supports_reasoning,
    )

    assert supports_reasoning("antigravity") is True
    # Nobody else joined or left.
    assert frozenset({"claude", "codex", "antigravity"}) == REASONING_SUPPORTED_ENGINES


def test_antigravity_defaults_never_read_agy_settings() -> None:
    """D33: agy's own settings file is out of bounds — no guessing."""
    from untether.telegram.engine_overrides import (
        get_engine_default_model,
        get_engine_default_reasoning,
    )

    assert get_engine_default_model("antigravity") is None
    assert get_engine_default_reasoning("antigravity") is None


def test_plan_exit_tool_engines_is_claude_only() -> None:
    """08 §11: only Claude has an ``ExitPlanMode`` tool to write a plan for."""
    from untether.telegram.engine_overrides import PLAN_EXIT_TOOL_ENGINES

    assert frozenset({"claude"}) == PLAN_EXIT_TOOL_ENGINES
