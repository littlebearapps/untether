"""#547 axis 2 / #548 — hot-reload Telegram notification formatting.

Headline framing is the load-bearing UX choice: agents read these
messages in next-turn context and the literal "No restart needed" /
"Restart required" wording flips the trained-in "after editing config,
restart the service" reflex.
"""

from __future__ import annotations

from pathlib import Path

from untether.config_reload_notification import (
    format_hot_reload_only_notice,
    format_partial_reload_notice,
    format_reload_notification,
    format_restart_required_notice,
)


def test_hot_reload_only_headline_says_no_restart_needed() -> None:
    text = format_hot_reload_only_notice(
        path="/home/nathan/.untether/untether.toml",
        changed_keys=["triggers.crons.bc-daily-triage.schedule"],
    )
    assert "Hot-reloaded" in text
    assert "**No restart needed.**" in text
    assert "automatically" in text
    assert "triggers.crons.bc-daily-triage.schedule" in text


_DEV_HINT = "run `systemctl --user restart untether-dev` (or send `/restart`)"


def test_restart_required_headline_says_restart_required() -> None:
    text = format_restart_required_notice(
        path="/home/nathan/.untether/untether.toml",
        restart_keys=["session_mode"],
        restart_hint=_DEV_HINT,
    )
    assert "**Restart required" in text
    assert "session_mode" in text
    assert _DEV_HINT in text
    assert "To apply, run `systemctl --user restart untether-dev`" in text


def test_restart_notice_generic_hint(monkeypatch) -> None:
    """#927: an undetected service gets generic wording, never a
    confidently wrong default-unit command."""
    from untether import service_manager

    monkeypatch.setattr(service_manager, "restart_command", lambda: None)
    text = format_restart_required_notice(
        path="/home/nathan/.untether/untether.toml",
        restart_keys=["session_mode"],
    )
    assert "restart Untether's service" in text
    assert "systemctl" not in text


def test_partial_notice_uses_hint() -> None:
    text = format_partial_reload_notice(
        path="/home/nathan/.untether/untether.toml",
        hot_keys=["progress.verbose"],
        restart_keys=["session_mode"],
        restart_hint=_DEV_HINT,
    )
    assert f"To apply those, {_DEV_HINT} when ready" in text


def test_dispatcher_threads_hint() -> None:
    for hot in ([], ["progress.verbose"]):
        text = format_reload_notification(
            path=Path("~/.untether/untether.toml").expanduser(),
            hot_keys=hot,
            restart_keys=["session_mode"],
            restart_hint=_DEV_HINT,
        )
        assert _DEV_HINT in text


def test_no_hardcoded_default_unit(monkeypatch) -> None:
    """#927 regression guard: with no detected service, no formatter emits
    the default-unit command literal (built from parts so the test file
    itself doesn't trip the #929 guard hook when grepped)."""
    from untether import service_manager

    monkeypatch.setattr(service_manager, "restart_command", lambda: None)
    probe = "restart " + "untether`"
    texts = [
        format_restart_required_notice(path="/tmp/x.toml", restart_keys=["topics"]),
        format_partial_reload_notice(
            path="/tmp/x.toml", hot_keys=["a"], restart_keys=["topics"]
        ),
        format_reload_notification(
            path="/tmp/x.toml", hot_keys=[], restart_keys=["topics"]
        ),
        format_reload_notification(
            path="/tmp/x.toml", hot_keys=["a"], restart_keys=["topics"]
        ),
    ]
    for text in texts:
        assert probe not in text
        assert "systemctl" not in text


def test_partial_reload_separates_hot_and_restart_keys() -> None:
    text = format_partial_reload_notice(
        path="/home/nathan/.untether/untether.toml",
        hot_keys=["progress.verbose", "progress.max_actions"],
        restart_keys=["session_mode"],
    )
    assert "Partial reload" in text
    assert "**No restart needed for those.**" in text
    assert "Applied now:" in text
    assert "progress.verbose" in text
    assert "progress.max_actions" in text
    assert "Need restart to apply:" in text
    assert "session_mode" in text
    assert "2 of 3 keys" in text


def test_dispatcher_picks_hot_only_when_no_restart_keys() -> None:
    text = format_reload_notification(
        path=Path("~/.untether/untether.toml").expanduser(),
        hot_keys=["progress.verbose"],
        restart_keys=[],
    )
    assert "**No restart needed.**" in text
    assert "Restart required" not in text
    assert "Partial reload" not in text


def test_dispatcher_picks_restart_only_when_no_hot_keys() -> None:
    text = format_reload_notification(
        path=Path("~/.untether/untether.toml").expanduser(),
        hot_keys=[],
        restart_keys=["session_mode", "topics"],
    )
    assert "**Restart required" in text
    assert "session_mode" in text
    assert "topics" in text
    assert "**No restart needed.**" not in text


def test_dispatcher_picks_partial_when_both_present() -> None:
    text = format_reload_notification(
        path=Path("~/.untether/untether.toml").expanduser(),
        hot_keys=["progress.verbose"],
        restart_keys=["session_mode"],
    )
    assert "Partial reload" in text


def test_path_shortened_to_tilde_when_under_home() -> None:
    home = Path.home()
    text = format_hot_reload_only_notice(
        path=home / ".untether" / "untether.toml",
        changed_keys=["triggers"],
    )
    assert "~/.untether/untether.toml" in text


def test_path_used_as_is_when_outside_home() -> None:
    text = format_hot_reload_only_notice(
        path="/etc/untether/untether.toml",
        changed_keys=["triggers"],
    )
    assert "/etc/untether/untether.toml" in text


def test_keys_deduplicated_and_sorted() -> None:
    text = format_hot_reload_only_notice(
        path="/tmp/x.toml",
        changed_keys=["zeta", "alpha", "alpha", "mu"],
    )
    # Order: alpha, mu, zeta — duplicates removed
    pos_alpha = text.find("`alpha`")
    pos_mu = text.find("`mu`")
    pos_zeta = text.find("`zeta`")
    assert 0 < pos_alpha < pos_mu < pos_zeta


def test_empty_changed_keys_render_gracefully() -> None:
    text = format_hot_reload_only_notice(path="/tmp/x.toml", changed_keys=[])
    assert "Hot-reloaded" in text
    assert "(no keys)" in text
