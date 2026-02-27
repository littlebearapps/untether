"""Tests for prompt preamble injection."""

from __future__ import annotations

from unittest.mock import patch

from untether.runner_bridge import _DEFAULT_PREAMBLE, _apply_preamble
from untether.settings import PreambleSettings


def test_default_preamble_prepended() -> None:
    """Default preamble is prepended when settings use defaults."""
    result = _apply_preamble("fix the bug")
    assert result.startswith("[Untether]")
    assert result.endswith("fix the bug")
    assert "\n\n---\n\n" in result
    assert _DEFAULT_PREAMBLE in result


def test_preamble_disabled() -> None:
    """Prompt is returned unchanged when preamble is disabled."""
    cfg = PreambleSettings(enabled=False)
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result == "fix the bug"


def test_custom_preamble_text() -> None:
    """Custom preamble text overrides the default."""
    cfg = PreambleSettings(text="Custom context for this agent.")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result.startswith("Custom context for this agent.")
    assert result.endswith("fix the bug")
    assert _DEFAULT_PREAMBLE not in result


def test_preamble_empty_text() -> None:
    """Empty text string effectively disables the preamble."""
    cfg = PreambleSettings(text="")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result == "fix the bug"


def test_preamble_settings_defaults() -> None:
    """PreambleSettings defaults to enabled with no custom text."""
    cfg = PreambleSettings()
    assert cfg.enabled is True
    assert cfg.text is None
