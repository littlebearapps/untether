"""End-to-end reproduction harness for the no-op empty-resume bug (#634, W6a).

Spawns a REAL ``untether.runners.claude.ClaudeRunner`` subprocess against the
deterministic fake CLI at ``tests/fake_clis/fake_claude_noop_resume.py``,
driven through the real ``untether.runner_bridge.handle_message`` bridge —
proving the whole pipeline (spawn -> stream-json parse -> anomaly detection
-> fresh recovery) against production-shaped JSONL, deterministically and
without a real Anthropic API call.

Reused/adapted from the sketch in
``docs/plans/2026-07-16-noop-resume-remediation/04-test-strategy.md``,
"Layer 1 -- Fake-claude reproduction harness".

In-process unit coverage for the W1 quarantine-and-fresh recovery itself
(using ``MockRunner``/``ScriptRunner``) already lives in
``tests/test_exec_bridge.py`` (the ``test_631_*`` / ``test_596_*`` tests).
This file's job is narrower and different: prove the REAL ``ClaudeRunner``
(argv construction, subprocess spawn, PTY stdin, msgspec stream-json
decoding, event translation) round-trips correctly into that same recovery
logic when driven against a scripted CLI double, not a Python-level fake
runner.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport
from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.claude import ENGINE as CLAUDE_ENGINE
from untether.runners.claude import ClaudeRunner
from untether.schemas.claude import (
    StreamResultMessage,
    StreamSystemMessage,
    decode_stream_json_line,
)
from untether.session_quarantine import QuarantineStore, set_quarantine_store

FAKE_CLI_PATH = Path(__file__).parent / "fake_clis" / "fake_claude_noop_resume.py"

# Harness-only env vars carrying scenario selection past ClaudeRunner.env()'s
# production security allowlist -- see _HarnessClaudeRunner docstring below.
_HARNESS_ENV_VARS = ("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_LINGER_S")


class _HarnessClaudeRunner(ClaudeRunner):
    """ClaudeRunner subclass used ONLY by this harness.

    Production hardening in ``ClaudeRunner.env()`` (#198/#409) filters the
    child process environment down to an allowlist (``untether.utils.
    env_policy``) so an engine subprocess can't read arbitrary host env
    vars/secrets. ``FAKE_CLAUDE_SCENARIO`` / ``FAKE_CLAUDE_LINGER_S``
    intentionally are NOT on that allowlist -- they only exist for this
    test double and have no reason to ever reach a real `claude` subprocess
    in production. This override re-adds them on top of the real filtered
    env after delegating to ``super().env()``.

    Every other hook (``command``, ``build_args``, ``stdin_payload``,
    ``run_impl``, translation) is completely untouched -- the
    spawn/parse/translate pipeline under test is 100% production code.
    """

    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in _HARNESS_ENV_VARS:
            if key in os.environ:
                base[key] = os.environ[key]
        return base


def _harness_runner() -> _HarnessClaudeRunner:
    assert FAKE_CLI_PATH.exists(), f"missing fake CLI: {FAKE_CLI_PATH}"
    assert os.access(FAKE_CLI_PATH, os.X_OK), (
        f"fake CLI is not executable (chmod +x): {FAKE_CLI_PATH}"
    )
    return _HarnessClaudeRunner(claude_cmd=str(FAKE_CLI_PATH))


@pytest.fixture
def quarantine_store(tmp_path):
    """Inject a fresh, isolated QuarantineStore for the duration of a test
    (mirrors the identically-named fixture in tests/test_exec_bridge.py) so
    the module-level singleton never lazily loads the real config-adjacent
    quarantine file."""
    store = QuarantineStore(path=tmp_path / "session_quarantine.json")
    set_quarantine_store(store)
    try:
        yield store
    finally:
        set_quarantine_store(None)


async def _run_bounded(coro, *, timeout: float = 10.0) -> None:
    """Bound handle_message so a pipeline regression fails fast with a clear
    message instead of hanging the suite."""
    with anyio.move_on_after(timeout) as scope:
        await coro
    assert not scope.cancelled_caught, (
        f"handle_message did not complete within {timeout}s — "
        "harness pipeline likely stalled"
    )


@pytest.mark.anyio
async def test_harness_dangling_then_empty_resume_recovers_fresh(
    monkeypatch, quarantine_store
) -> None:
    """#634 W6a end-to-end: a resume of a poisoned session returns an empty
    0-turn result from the REAL ClaudeRunner subprocess pipeline; the W1
    quarantine-and-fresh recovery clears + quarantines it and re-runs the
    original prompt as a fresh session, whose real answer reaches the
    transport -- all driven through real argv construction, PTY spawn,
    stream-json parsing, and translation (no MockRunner)."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "dangling_then_empty_resume")
    monkeypatch.setenv("FAKE_CLAUDE_LINGER_S", "0")

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
    )
    poisoned_sid = "S-poisoned-634"
    cleared: list[str] = []

    async def on_resume_failed(tok: ResumeToken) -> None:
        cleared.append(tok.value)

    with capture_logs() as logs:
        await _run_bounded(
            handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(
                    channel_id=123, message_id=10, text="please continue"
                ),
                resume_token=ResumeToken(engine=CLAUDE_ENGINE, value=poisoned_sid),
                on_resume_failed=on_resume_failed,
            )
        )

    # The real subprocess pipeline classified the poisoned resume as the
    # no-op empty-resume anomaly...
    assert any(r.get("event") == "runner.empty_result" for r in logs)
    # ...and W1's quarantine-and-fresh recovery fired exactly once.
    assert sum(1 for r in logs if r.get("event") == "session.auto_resend_fresh") == 1
    # The poisoned token was cleared and quarantined so it is never resumed
    # again.
    assert poisoned_sid in cleared
    assert quarantine_store.is_quarantined(CLAUDE_ENGINE, poisoned_sid) is True
    # Exactly two real subprocess spawns: the poisoned resume, then the
    # fresh recovery leg (proves the fresh leg really invoked the script
    # WITHOUT --resume -- see the fake CLI's branch on argv presence).
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") == 2
    # The fresh leg's real answer reached the user in the FINAL delivered
    # message. (An earlier edit legitimately shows the transient "retrying
    # automatically..." notice -- that's expected mid-flight UX, not what
    # this assertion is about.)
    final_text = transport.edit_calls[-1]["message"].text
    assert "started" in final_text
    assert "engine returned an empty result" not in final_text


@pytest.mark.anyio
async def test_harness_healthy_resume_no_recovery(
    monkeypatch, quarantine_store
) -> None:
    """#634 W6a negative control: a healthy resume through the real
    ClaudeRunner pipeline returns a real answer on the first try -- no
    anomaly, no quarantine, no recovery run, exactly one subprocess spawn."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "healthy_resume")
    monkeypatch.delenv("FAKE_CLAUDE_LINGER_S", raising=False)

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
    )
    healthy_sid = "S-healthy-634"

    with capture_logs() as logs:
        await _run_bounded(
            handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(
                    channel_id=123, message_id=11, text="please continue"
                ),
                resume_token=ResumeToken(engine=CLAUDE_ENGINE, value=healthy_sid),
            )
        )

    assert not any(r.get("event") == "runner.empty_result" for r in logs)
    assert not any(r.get("event") == "session.auto_resend_fresh" for r in logs)
    assert quarantine_store.is_quarantined(CLAUDE_ENGINE, healthy_sid) is False
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") == 1
    all_text = " ".join(
        c["message"].text for c in transport.edit_calls + transport.send_calls
    )
    assert "Here is the continued answer." in all_text


def _read_lines_bounded(fd: int, n: int, *, timeout: float) -> list[bytes]:
    """Read exactly ``n`` newline-terminated lines from raw fd ``fd``
    within ``timeout`` seconds total, or raise.

    Deliberately uses ``os.read`` on the raw fd rather than
    ``BufferedReader.readline()`` gated by ``select()``: a buffered
    reader's first ``read()`` syscall can pull BOTH JSONL lines out of the
    kernel pipe in one shot (the fake CLI writes them back-to-back before
    its linger sleep), leaving the second line sitting in the buffered
    reader's *userspace* buffer. A subsequent ``select()`` call only
    inspects the raw fd, sees no new kernel-level data, and blocks until
    more arrives — which in the linger scenario means blocking until the
    process wakes up and exits, silently defeating the very "still alive"
    assertion this test exists to make. Reading the raw fd directly keeps
    every byte visible to ``select()`` exactly once.
    """
    deadline = time.monotonic() + timeout
    buf = b""
    lines: list[bytes] = []
    while len(lines) < n:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for {n} line(s); got {len(lines)}"
        ready, _, _ = select.select([fd], [], [], remaining)
        assert ready, f"timed out waiting for {n} line(s); got {len(lines)}"
        chunk = os.read(fd, 65536)
        if not chunk:
            break  # EOF
        buf += chunk
        while b"\n" in buf and len(lines) < n:
            line, buf = buf.split(b"\n", 1)
            lines.append(line)
    assert len(lines) == n, f"expected {n} line(s), only got {len(lines)}"
    return lines


def test_harness_linger_scenario_emits_valid_result_and_outlives_it() -> None:
    """#634 W6a smoke test for `linger_then_sigterm_after_result` ONLY:
    proves the scenario's own JSONL emission shape decodes with the REAL
    msgspec schema and that the process stays alive past the result line by
    ~FAKE_CLAUDE_LINGER_S -- i.e. it models the forced-teardown limbo case
    (W2) faithfully. Deliberately does NOT drive the full SIGTERM/watchdog
    path through ClaudeRunner end-to-end -- Task 5 covers the
    forced-teardown quarantine record with the in-process watchdog pattern;
    this test's only job is the scenario script's emission shape.

    ``linger_s`` is deliberately generous (not the minimum that passes
    locally): the test never waits for the linger to expire before its
    liveness assertion -- it reads the two JSONL lines, asserts the process
    is still alive, then kills it in the ``finally`` block regardless of
    where in the sleep it still is. Widening the linger costs nothing in
    wall-clock, so the margin between "both lines flushed" and "sleep
    expires" is kept comfortably larger than any plausible CI cold-start /
    scheduler-latency jitter (a tight 0.35s margin was observed to be
    survivable locally but not comfortably clear of that jitter budget).
    """
    linger_s = 2.0
    env = dict(os.environ)
    env["FAKE_CLAUDE_SCENARIO"] = "linger_then_sigterm_after_result"
    env["FAKE_CLAUDE_LINGER_S"] = str(linger_s)

    proc = subprocess.Popen(
        [
            str(FAKE_CLI_PATH),
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--resume",
            "S-linger-634",
            "--",
            "keep going",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        started_at = time.monotonic()
        assert proc.stdout is not None
        init_line, result_line = _read_lines_bounded(
            proc.stdout.fileno(), 2, timeout=5.0
        )
        elapsed_to_result = time.monotonic() - started_at

        init_event = decode_stream_json_line(init_line)
        result_event = decode_stream_json_line(result_line)

        assert isinstance(init_event, StreamSystemMessage)
        assert init_event.session_id == "S-linger-634"
        assert isinstance(result_event, StreamResultMessage)
        assert result_event.is_error is False
        assert result_event.num_turns >= 1
        assert result_event.session_id == "S-linger-634"

        # Right after the result line, the process must still be alive --
        # it is lingering (sleeping), not exiting immediately. This is the
        # shape W2's forced-teardown SIGTERM path relies on. Deliberately
        # does NOT wait out the rest of the `linger_s` sleep before this
        # assertion or afterward -- the process is killed in `finally`
        # regardless of how far into its sleep it still is -- so a larger
        # `linger_s` buys CI safety margin without costing wall-clock here.
        assert proc.poll() is None, (
            "fake CLI exited immediately after the result line instead of "
            "lingering — scenario does not model the forced-teardown case"
        )
        # Sanity check on the assertion above: if reading both lines somehow
        # took longer than the linger itself, "still alive" would be a
        # foregone conclusion rather than a meaningful check of the
        # lingering behaviour.
        assert elapsed_to_result < linger_s, (
            f"reading both JSONL lines took {elapsed_to_result:.2f}s, longer "
            f"than linger_s={linger_s}s — the liveness assertion above "
            "wasn't a meaningful check"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


@pytest.mark.anyio
async def test_harness_w4_diverts_fresh_when_prior_owner_will_not_hand_off(
    monkeypatch, quarantine_store
) -> None:
    """#634 W6b / #633 (W4) end-to-end through the real spawn pipeline.

    Converts the manual `B-RESUME` integration procedure into deterministic
    coverage: a live subprocess still owns the session, so the follow-up must
    NOT resume it. Instead the bounded handoff wait times out and the run
    diverts to a fresh session — the whole point of W4, since resuming a
    session with a live owner is what leaves the upstream turn dangling and
    produces the 0-turn empty result rc7 could only recover from afterwards.

    Uses a short handoff timeout so the test is fast; the production default
    (30s) is exercised by the unit tests in test_exec_bridge.py.
    """
    import untether.runner_bridge as rb
    from untether.runners import claude as claude_mod
    from untether.settings import AutoContinueSettings

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "resume_survives_sigterm")
    monkeypatch.setattr(
        rb,
        "_load_auto_continue_settings",
        lambda: AutoContinueSettings(
            serialize_session_owner=True, session_handoff_timeout_s=0.2
        ),
    )

    sid = "sess-w4-live-owner"
    # Simulate the prior subprocess still owning the session, exactly as
    # run_impl would have registered it on its first StartedEvent.
    claude_mod._SESSION_STDIN[sid] = object()

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
    )

    try:
        with capture_logs() as logs:
            await _run_bounded(
                handle_message(
                    cfg,
                    runner=runner,
                    incoming=IncomingMessage(
                        channel_id=99, message_id=1, text="follow up"
                    ),
                    resume_token=ResumeToken(engine=CLAUDE_ENGINE, value=sid),
                )
            )
    finally:
        claude_mod._SESSION_STDIN.pop(sid, None)

    # (a) the handoff was attempted and timed out rather than deadlocking
    assert any(
        r.get("event") == "session.handoff" and r.get("outcome") == "timed_out"
        for r in logs
    ), "W4 gate did not run or did not time out against a live owner"
    # (b) diverted fresh with the structured reason
    assert any(
        r.get("event") == "session.resume_diverted_fresh"
        and r.get("reason") == "handoff_timeout"
        for r in logs
    )
    # (c) exactly ONE spawn, and it was the fresh leg — never a --resume of a
    # session that still had a live owner. This is the core W4 invariant.
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") == 1
    # (d) the user still got a real answer rather than silence
    all_text = " ".join(
        c["message"].text for c in transport.edit_calls + transport.send_calls
    )
    assert "Fresh answer." in all_text


# ---------------------------------------------------------------------------
# #640 — auto-continue signal-death suppression
#
# The two tests below are a PAIR and only mean something together. They drive
# the IDENTICAL frame sequence (init -> assistant tool_use -> user tool_result,
# and deliberately no `result` frame) through the real ClaudeRunner spawn
# pipeline, differing ONLY in the exit path: signal death vs rc=0.
#
# Pre-fix, `ClaudeRunner.run_impl` never wrote `stream.proc_returncode` back
# (it was assigned only in the base runner), so the bridge always read `None`,
# `_is_signal_death(None)` returned False, and BOTH of these would have
# auto-continued. Post-fix only the clean-exit twin does.
#
# Prior coverage for #640 was a source-text grep for the literal string
# "stream.proc_returncode = rc" — it never executed the code. These do.
# ---------------------------------------------------------------------------


def _joined_text(transport: FakeTransport) -> str:
    return " ".join(
        c["message"].text for c in transport.edit_calls + transport.send_calls
    )


@pytest.mark.anyio
async def test_harness_640_signal_death_suppresses_auto_continue(
    monkeypatch, quarantine_store
) -> None:
    """#640: a Claude subprocess that dies by SIGNAL after emitting a
    tool_result must NOT be auto-continued.

    This is the death-spiral guard #589 relied on. Fleet evidence on nsd (14
    days) showed it never fired: correlating each `session.auto_continue` with
    the preceding `subprocess.exit` gave {rc=0: 47, rc=143: 2} — two
    auto-continues straight after a SIGTERM.
    """
    import untether.runner_bridge as rb
    from untether.runner_bridge import _is_signal_death, _should_auto_continue
    from untether.settings import AutoContinueSettings

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tool_result_then_sigterm")
    monkeypatch.delenv("FAKE_CLAUDE_LINGER_S", raising=False)
    monkeypatch.setattr(
        rb,
        "_load_auto_continue_settings",
        lambda: AutoContinueSettings(enabled=True, max_retries=1),
    )

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )

    with capture_logs() as logs:
        await _run_bounded(
            handle_message(
                cfg,
                runner=runner,
                # No resume token: the fake CLI branches on `--resume`, so the
                # first leg gets the tool_result sequence and any auto-continue
                # re-entry would arrive WITH --resume and return the
                # UNEXPECTED-AUTO-CONTINUE marker asserted against below.
                incoming=IncomingMessage(channel_id=99, message_id=1, text="go"),
                resume_token=None,
            )
        )

    # (a) The fix actually landed at runtime: a real signal-death return code
    # reached the stream state. Pre-fix this field was None for every Claude
    # run, which is the whole defect.
    exits = [r for r in logs if r.get("event") == "subprocess.exit"]
    assert exits, "no subprocess.exit was recorded"
    rc = exits[-1].get("rc")
    assert rc is not None, "proc_returncode is None — #640 regression"
    assert _is_signal_death(rc), f"expected a signal death, observed rc={rc!r}"

    # (b) No auto-continue fired.
    assert not [r for r in logs if r.get("event") == "session.auto_continue"], (
        "auto-continue fired after a signal death — #640 guard is inert"
    )

    # (c) Exactly one spawn: the run was never re-entered.
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") == 1

    # (d) The marker the fake CLI emits ONLY on a wrongly-fired auto-continue
    # never reached the user — a loud check that (b) isn't just a miscount.
    assert "UNEXPECTED-AUTO-CONTINUE" not in _joined_text(transport)

    # (e) COUNTERFACTUAL — the discriminating assertion. Feed the gate the
    # values this run actually produced and flip ONLY proc_returncode back to
    # the pre-fix `None`. If the gate would have auto-continued then but does
    # not now, the return-code capture is provably what suppresses it — this
    # rules out a false pass where some earlier arm (cancelled / non-"user"
    # last_event_type / falsy resume) did the suppressing instead.
    summary = [r for r in logs if r.get("event") == "session.summary"]
    assert summary, "no session.summary recorded"
    observed_last_event = summary[-1].get("last_event_type")
    assert observed_last_event == "user", (
        f"scenario did not end on a user/tool_result frame (got "
        f"{observed_last_event!r}) — the gate would short-circuit before the "
        "signal-death arm and this test would pass for the wrong reason"
    )
    gate_kwargs = {
        "last_event_type": observed_last_event,
        "engine": "claude",
        "cancelled": False,
        "resume_value": "sid-observed",
        "auto_continued_count": 0,
        "max_retries": 1,
    }
    assert _should_auto_continue(**gate_kwargs, proc_returncode=None) is True, (
        "pre-fix baseline is wrong: with proc_returncode=None the gate should "
        "have allowed auto-continue"
    )
    assert _should_auto_continue(**gate_kwargs, proc_returncode=rc) is False


@pytest.mark.anyio
async def test_harness_640_clean_exit_auto_continues_with_integer_returncode(
    monkeypatch, quarantine_store
) -> None:
    """#640 positive control: identical frames to the sigterm twin, rc=0.

    Proves the frame sequence alone DOES drive auto-continue, so the twin's
    silence is attributable to the exit path and nothing else. Also asserts
    the logged `proc_returncode` is the integer 0 rather than `None` — that
    field being None was the entire defect, and it is now visible in
    journalctl instead of requiring log-line correlation (issue item 3).
    """
    import untether.runner_bridge as rb
    from untether.settings import AutoContinueSettings

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tool_result_then_clean_exit")
    monkeypatch.delenv("FAKE_CLAUDE_LINGER_S", raising=False)
    monkeypatch.setattr(
        rb,
        "_load_auto_continue_settings",
        lambda: AutoContinueSettings(enabled=True, max_retries=1),
    )

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )

    with capture_logs() as logs:
        await _run_bounded(
            handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=99, message_id=2, text="go"),
                resume_token=None,
            )
        )

    ac = [r for r in logs if r.get("event") == "session.auto_continue"]
    assert len(ac) == 1, f"expected exactly one auto-continue, got {len(ac)}"
    assert ac[0].get("last_event_type") == "user"
    # The heart of #640: an integer, never None.
    assert ac[0].get("proc_returncode") == 0
    assert isinstance(ac[0].get("proc_returncode"), int)

    # Two spawns: the original leg plus the auto-continue re-entry.
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") == 2
    assert "Continued after tool result." in _joined_text(transport)


@pytest.mark.anyio
async def test_harness_633_handoff_waits_on_owner_with_real_background_handles(
    monkeypatch, quarantine_store
) -> None:
    """#633/#647: the handoff wait must SEE real background work.

    Closes a specific coverage gap: every existing bridge test for #633
    monkeypatches ``wait_for_session_handoff``, and the unit tests in
    test_exec_runner.py hand-insert into ``_SESSION_STDIN`` without ever
    touching ``_SESSION_BG_STATE``. So nothing exercised the real
    ``_register_background_handle`` -> ``session_live_bg_count`` ->
    ``session.handoff_wait(live_bg_count=N)`` chain, and a regression in that
    bookkeeping would have gone unnoticed.

    Everything here is production code except the two registry entries, which
    stand in for a prior subprocess that ``run_impl`` would have registered on
    its first StartedEvent and which is still alive in post-result limbo.

    This is the deterministic counterpart to the live repro: chasing the
    live window proved unreliable because the lingering owner exits within
    ~20ms of the follow-up being received (Telegram long-poll latency is
    5-30s and swamps it), so criteria 1-2 are pinned down here instead.
    """
    import untether.runner_bridge as rb
    from untether.runners import claude as claude_mod
    from untether.schemas.claude import StreamToolUseBlock
    from untether.settings import AutoContinueSettings

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "healthy_resume")
    monkeypatch.delenv("FAKE_CLAUDE_LINGER_S", raising=False)
    monkeypatch.setattr(
        rb,
        "_load_auto_continue_settings",
        lambda: AutoContinueSettings(
            serialize_session_owner=True,
            session_handoff_timeout_s=1.0,
            session_handoff_bg_timeout_s=2.0,
        ),
    )

    sid = "sess-633-real-bg"
    state = claude_mod.ClaudeStreamState()
    # Register TWO genuine background handles through the production
    # registrar, exactly as a tool_use frame would.
    for tool_id, name, raw in (
        ("bg-agent-1", "Agent", {"run_in_background": True, "prompt": "survey"}),
        ("bg-bash-1", "Bash", {"run_in_background": True, "command": "sleep 400"}),
    ):
        claude_mod._register_background_handle(
            state, StreamToolUseBlock(id=tool_id, name=name, input=raw)
        )
    assert claude_mod.has_live_background_work(state) is True
    assert claude_mod.session_live_bg_count(sid) == 0, "not registered yet"

    claude_mod._SESSION_STDIN[sid] = object()
    claude_mod._SESSION_BG_STATE[sid] = state
    # The real counter, over the real registry.
    assert claude_mod.session_live_bg_count(sid) == 2

    transport = FakeTransport()
    runner = _harness_runner()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )

    try:
        with capture_logs() as logs:
            await _run_bounded(
                handle_message(
                    cfg,
                    runner=runner,
                    incoming=IncomingMessage(
                        channel_id=99, message_id=3, text="follow up"
                    ),
                    resume_token=ResumeToken(engine=CLAUDE_ENGINE, value=sid),
                ),
                timeout=20.0,
            )
    finally:
        claude_mod._SESSION_STDIN.pop(sid, None)
        claude_mod._SESSION_BG_STATE.pop(sid, None)

    # Criterion 1: the wait ran AND saw the real background work.
    waits = [r for r in logs if r.get("event") == "session.handoff_wait"]
    assert waits, "handoff wait never ran against a live owner"
    assert waits[0].get("live_bg_count") == 2, (
        f"live_bg_count did not reflect the real registered handles: "
        f"{waits[0].get('live_bg_count')!r}"
    )

    # Criterion 2: it terminated with a structured outcome and an elapsed time.
    done = [r for r in logs if r.get("event") == "session.handoff_wait_done"]
    assert done, "handoff wait never logged completion — possible deadlock"
    assert done[0].get("outcome") in {"free", "exited", "timed_out"}
    assert isinstance(done[0].get("elapsed_s"), (int, float))

    # #647: the owner still holds live background work at base timeout, so the
    # wait is extended rather than silently abandoning the session's context.
    assert any(
        r.get("event") == "session.handoff_bg_extended" and r.get("live_bg_count") == 2
        for r in logs
    ), "background-aware extension did not engage despite live background work"

    # Bounded, not a deadlock: never two live owners for one sid.
    assert sum(1 for r in logs if r.get("event") == "subprocess.spawn") <= 1


def test_640_should_auto_continue_rc_table() -> None:
    """#640 acceptance item 4 — the return-code table, as the filer specified.

    Pure-predicate coverage of the gate arm itself, independent of the spawn
    pipeline. `None` remains eligible by design: it is the documented
    fail-open for engines that do not thread a return code. For Claude it is
    now always populated on the normal exit path (see the harness pair above).
    """
    from untether.runner_bridge import _should_auto_continue

    base = {
        "last_event_type": "user",
        "engine": "claude",
        "cancelled": False,
        "resume_value": "sid",
        "auto_continued_count": 0,
        "max_retries": 1,
    }
    eligible = {0, None}
    for rc in (0, 1, 128, 137, 143, -9, -15, None):
        expected = rc in eligible
        assert _should_auto_continue(**base, proc_returncode=rc) is expected, (
            f"rc={rc!r} should {'' if expected else 'NOT '}be eligible"
        )
