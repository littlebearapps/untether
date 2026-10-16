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


# ── #1117: trigger config problem notices ──


def test_1117_invalid_triggers_notice_names_each_skipped_trigger() -> None:
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [
            InvalidTrigger("cron", "nightly", "prompt: required"),
            InvalidTrigger("webhook", "gh", "auth: Input should be 'bearer'"),
        ],
        phase="startup",
    )
    assert text.startswith("⚠️ Trigger config: 2 triggers skipped")
    assert "• cron nightly: prompt: required" in text
    assert "• webhook gh:" in text
    assert "started normally" in text
    assert "no earlier secret for it stays active" in text


def test_1117_invalid_triggers_notice_clips_long_errors() -> None:
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [InvalidTrigger("cron", "a", "x" * 1000)], phase="reload"
    )
    assert "x" * 300 not in text
    assert "…" in text
    assert "reloaded normally" in text
    assert "webhook" not in text  # no secret line for crons only


def test_1117_block_failure_notice_variants() -> None:
    from untether.config_reload_notification import (
        format_trigger_block_failure_notice,
    )

    startup = format_trigger_block_failure_notice(
        "server.port: bad", phase="startup", running=False, restart_hint="restart"
    )
    assert "Triggers failed to start" in startup
    assert "then restart." in startup

    idle = format_trigger_block_failure_notice(
        "server.port: bad", phase="reload", running=False
    )
    assert "no crons or webhooks are running" in idle

    live = format_trigger_block_failure_notice(
        "server.port: bad",
        phase="reload",
        running=True,
        active_secret_webhook_ids=["stripe", "gh"],
    )
    assert "previous crons and webhooks are still running" in live
    assert "old webhook secret is still active for: gh, stripe" in live

    no_secret = format_trigger_block_failure_notice(
        "server.port: bad", phase="reload", running=True
    )
    assert "secret" not in no_secret


# ── #1146: notices tell the truth about what is actually running ──

_HINT = "run `systemctl --user restart untether-dev` (or send `/restart`)"


def test_1146_server_refused_notice_exposed() -> None:
    """Start-up refusal: name the refused webhooks, say NO webhook is served
    (bearer ones too), and how to fix it."""
    from untether.config_reload_notification import (
        format_webhook_server_refused_notice,
    )

    text = format_webhook_server_refused_notice(
        ["open-b", "open-a"], exposed=True, restart_hint=_HINT
    )
    assert text.startswith("⚠️ Webhook server not started")
    assert "no webhooks are being served" in text
    assert "open-a, open-b" in text
    assert "exposed server" in text
    assert "add auth to those webhooks or remove exposed = true" in text
    assert f"then {_HINT}." in text
    assert "Crons started normally." in text
    assert "other triggers started normally" not in text
    assert "without a restart" not in text


def test_1146_server_refused_notice_non_loopback_and_invalid() -> None:
    from untether.config_reload_notification import (
        format_webhook_server_refused_notice,
    )
    from untether.triggers.settings import InvalidTrigger

    text = format_webhook_server_refused_notice(
        ["open"],
        exposed=False,
        invalid=[InvalidTrigger("cron", "nightly", "prompt: required")],
        restart_hint=_HINT,
    )
    assert "non-loopback host" in text
    assert "allow_unauthenticated_webhooks" in text
    assert "Also skipped (invalid):\n• cron nightly: prompt: required" in text
    assert "The other crons started normally." in text
    assert "Fix the skipped entries too." in text


def test_1146_invalid_notice_when_triggers_not_running() -> None:
    """After a start-up block failure nothing runs: never 'reloaded normally'
    or 'applies without a restart'."""
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [InvalidTrigger("cron", "a", "prompt: required")],
        phase="reload",
        running=False,
        restart_hint=_HINT,
    )
    assert "normally" not in text
    assert "without a restart" not in text
    assert "No triggers are running" in text
    assert f"then {_HINT}." in text


def test_1146_invalid_notice_when_webhook_server_down() -> None:
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [InvalidTrigger("webhook", "gh", "auth: bad")],
        phase="reload",
        webhook_server_running=False,
        restart_hint=_HINT,
    )
    assert "other triggers reloaded normally" not in text
    assert "Crons reloaded normally" in text
    assert "the webhook server isn't running" in text
    assert "no webhooks are being served" in text
    assert "without a restart" not in text
    assert f"to start the webhook server, {_HINT}." in text


def test_1146_invalid_notice_lists_refused_webhooks_on_reload() -> None:
    """A reload refusing an auth = "none" webhook gets the same short notice
    as a skipped invalid webhook."""
    from untether.config_reload_notification import format_invalid_triggers_notice

    text = format_invalid_triggers_notice(
        [], phase="reload", refused_webhook_ids=["open"], exposed=True
    )
    assert text.startswith("⚠️ Trigger config: 1 trigger skipped\n")
    assert "(invalid)" not in text
    assert '• webhook open: refused, auth = "none" isn\'t allowed on an exposed' in text
    assert "The other triggers reloaded normally." in text
    assert "remove exposed = true" in text
    assert "applies without a restart" in text


def test_1146_block_failure_not_running_says_restart() -> None:
    from untether.config_reload_notification import (
        format_trigger_block_failure_notice,
    )

    text = format_trigger_block_failure_notice(
        "crons: duplicate id", phase="reload", running=False, restart_hint=_HINT
    )
    assert "no crons or webhooks are running" in text
    assert f"then {_HINT}." in text


def test_1146_restart_notice_names_reason_not_key() -> None:
    reason = "triggers failed at start-up, so no crons or webhooks are running"
    text = format_reload_notification(
        path="/tmp/untether.toml",
        hot_keys=[],
        restart_keys=[],
        restart_reasons=[reason],
        restart_hint=_HINT,
    )
    assert "**Restart required" in text
    assert reason in text
    assert "Restart-only keys touched" not in text
    assert "triggers.enabled" not in text
    assert f"To apply, {_HINT}." in text


def test_1146_restart_reason_with_keys_and_hot_keys() -> None:
    reason = "triggers failed at start-up"
    with_keys = format_reload_notification(
        path="/tmp/untether.toml",
        hot_keys=[],
        restart_keys=["session_mode"],
        restart_reasons=[reason],
        restart_hint=_HINT,
    )
    assert "Restart-only keys touched: `session_mode`" in with_keys
    assert f"Also needs a restart: {reason}" in with_keys

    partial = format_reload_notification(
        path="/tmp/untether.toml",
        hot_keys=["progress.verbose"],
        restart_keys=[],
        restart_reasons=[reason],
        restart_hint=_HINT,
    )
    assert "Partial reload" in partial
    assert "Need restart to apply" not in partial
    assert f"Still needs a restart: {reason}" in partial
    assert _HINT in partial


# ── #1144 / #1117: trigger notices render user text literally ──
#
# The notices go to Telegram through prepare_telegram (markdown → entities),
# so a schedule like '99 99 * * *' next to '* *' must not become italics.


def _render(text: str) -> tuple[str, list[dict[str, object]]]:
    from untether.markdown import MarkdownParts
    from untether.telegram.render import prepare_telegram

    return prepare_telegram(MarkdownParts(header=text))


_TRICKY = "schedule: '99 99 * * *' or '* *', _a_ `tick` [x](y) <b> **z**"
_TRICKY_ID = "it8_bad*sched`"


def _assert_literal(rendered: str, entities: list[dict[str, object]]) -> None:
    assert _TRICKY in rendered
    assert _TRICKY_ID in rendered
    # Only the restart hint's code spans (and linkify, which Telegram clients
    # apply to 'crons.0.id' anyway) may format anything.
    assert {e["type"] for e in entities} <= {"code", "text_link"}
    for entity in entities:
        offset, length = int(entity["offset"]), int(entity["length"])  # type: ignore[call-overload]
        assert "*" not in rendered[offset : offset + length]


def test_1144_invalid_notice_renders_schedules_literally() -> None:
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [
            InvalidTrigger("cron", _TRICKY_ID, _TRICKY),
            InvalidTrigger("cron", "two", "schedule: '* *' needs 5 fields"),
        ],
        phase="reload",
        refused_webhook_ids=["open_*_hook"],
        exposed=True,
        webhook_server_running=False,
        restart_hint=_HINT,
    )
    rendered, entities = _render(text)
    _assert_literal(rendered, entities)
    assert "schedule: '* *' needs 5 fields" in rendered
    assert "webhook open_*_hook: refused" in rendered
    assert "systemctl --user restart untether-dev" in rendered  # hint still code


def test_1144_server_refused_notice_renders_user_text_literally() -> None:
    from untether.config_reload_notification import (
        format_webhook_server_refused_notice,
    )
    from untether.triggers.settings import InvalidTrigger

    text = format_webhook_server_refused_notice(
        ["open_*_a", "open_*_b"],
        exposed=True,
        invalid=[InvalidTrigger("cron", _TRICKY_ID, _TRICKY)],
        restart_hint=_HINT,
    )
    rendered, entities = _render(text)
    _assert_literal(rendered, entities)
    assert "Refused: open_*_a, open_*_b" in rendered


def test_1144_block_failure_notices_render_error_literally() -> None:
    from untether.config_reload_notification import (
        format_trigger_block_failure_notice,
    )

    error = f"crons.0.id {_TRICKY_ID}: {_TRICKY}"
    for kwargs in (
        {"phase": "startup", "running": False},
        {"phase": "reload", "running": False},
        {
            "phase": "reload",
            "running": True,
            "active_secret_webhook_ids": [_TRICKY_ID, "gh_*_x"],
        },
        {"phase": "reload", "running": True, "webhook_server_running": False},
    ):
        text = format_trigger_block_failure_notice(
            error,
            restart_hint=_HINT,
            **kwargs,  # type: ignore[arg-type]
        )
        rendered, entities = _render(text)
        _assert_literal(rendered, entities)
    assert (
        "gh_*_x"
        in _render(
            format_trigger_block_failure_notice(
                "x", phase="reload", running=True, active_secret_webhook_ids=["gh_*_x"]
            )
        )[0]
    )


def test_1144_user_text_newlines_cannot_start_markdown_blocks() -> None:
    """A newline inside an id or error would let '- ' / '1. ' / '# ' start a
    list or heading; user text is kept on its own line."""
    from untether.config_reload_notification import format_invalid_triggers_notice
    from untether.triggers.settings import InvalidTrigger

    text = format_invalid_triggers_notice(
        [InvalidTrigger("cron", "a\n- b", "bad\n# heading\n1. item")],
        phase="reload",
    )
    rendered, entities = _render(text)
    assert "cron a - b: bad # heading 1. item" in rendered
    assert entities == []


# ── #1146: block-failure notice when the webhook server never started ──


def test_1146_block_failure_with_webhook_server_down() -> None:
    """The server refused at start-up stays down: the notice mustn't claim
    webhooks still run or that an old secret is still active."""
    from untether.config_reload_notification import (
        format_trigger_block_failure_notice,
    )

    text = format_trigger_block_failure_notice(
        "crons: ids must be unique",
        phase="reload",
        running=True,
        active_secret_webhook_ids=["it8-bearer"],
        webhook_server_running=False,
        restart_hint=_HINT,
    )
    assert "previous crons are still running unchanged" in text
    assert "crons and webhooks are still running" not in text
    assert "no webhooks are being served" in text
    assert "the webhook server isn't running" in text
    assert "secret" not in text
    assert "it8-bearer" not in text
    assert "Error: crons: ids must be unique" in text
    assert "a cron fix applies on save" in text
    assert f"to start the webhook server, {_HINT}." in text
