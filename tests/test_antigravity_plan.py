"""Plan first for Antigravity (#981, shape A).

agy's own ``--mode=plan`` under the no-shell Workspace agent writes one plan
artifact into ``brain/<conversation id>/`` and stops (P-H3b); Untether posts
the plan with Approve / Deny / Let's discuss, and Approve resumes the
conversation in Workspace (P-H3c). Stream replays use the real P-H3b capture
(``context_turn``) and its approve resume (``context_resume``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from untether.context import RunContext
from untether.model import CompletedEvent, ResumeToken
from untether.runner import (
    RETIRED_CARDS_KEY,
    REVIEW_CARD_KEY,
    retired_cards,
    review_card,
)
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner, AntigravityStreamState
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema
from untether.telegram.commands.antigravity_plan import AntigravityPlanCommand
from untether.transport import RenderedMessage
from untether.utils import antigravity_plan as plan_mod
from untether.utils.paths import (
    reset_run_base_dir,
    reset_run_channel_id,
    reset_run_context,
    reset_run_sender_id,
    reset_run_thread_id,
    set_run_base_dir,
    set_run_channel_id,
    set_run_context,
    set_run_sender_id,
    set_run_thread_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
CID = "7a2d9c4e-1b3f-4e6a-8c5d-2f1e0a9b8c7d"  # context_turn / context_resume
PLAN_NAME = "add_alpha_to_readme.md"
AGENT_FLAG = "--agent=untether-workspace"
PLAN_BODY = "# Add alpha\n\n## Proposed Changes\n\nAppend `alpha` to README.md."


def _lines(name: str) -> list[str]:
    return [ln for ln in (FIXTURES / f"{name}.jsonl").read_text().splitlines() if ln]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``HOME`` so the capture's ``~/.gemini/…`` paths land in tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        plan_mod,
        "brain_root",
        lambda: home / ".gemini" / "antigravity-cli" / "brain",
    )
    return home


def _brain(home: Path, cid: str = CID) -> Path:
    path = home / ".gemini" / "antigravity-cli" / "brain" / cid
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_plan(
    home: Path,
    name: str = PLAN_NAME,
    body: str = PLAN_BODY,
    *,
    feedback: bool | None = True,
    cid: str = CID,
) -> Path:
    folder = _brain(home, cid)
    path = folder / name
    path.write_text(body)
    if feedback is not None:
        meta = {"summary": "Plan to add alpha.", "requestFeedback": feedback}
        (folder / f"{name}.metadata.json").write_text(json.dumps(meta))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def run_vars() -> Iterator[RunContext]:
    """The contextvars ``_run_engine`` sets for a person's message."""
    context = RunContext(project="demo", branch=None)
    tokens = (
        set_run_channel_id(4242),
        set_run_thread_id(7),
        set_run_sender_id(99),
        set_run_context(context),
    )
    try:
        yield context
    finally:
        reset_run_channel_id(tokens[0])
        reset_run_thread_id(tokens[1])
        reset_run_sender_id(tokens[2])
        reset_run_context(tokens[3])


def _replay(
    lines: list[str],
    *,
    options: EngineRunOptions | None,
    resume: ResumeToken | None = None,
    prompt: str = "prompt",
) -> tuple[list[Any], AntigravityStreamState]:
    runner = AntigravityRunner(antigravity_cmd="agy")
    events: list[Any] = []
    with apply_run_options(options):
        state = runner.new_state(prompt, resume)
        runner.start_run(prompt, resume, state=state)
        for line in lines:
            events.extend(
                runner.translate(
                    schema.decode_event(line),
                    state=state,
                    resume=resume,
                    found_session=None,
                )
            )
            if events and isinstance(events[-1], CompletedEvent):
                break
    return events, state


def _completed(events: list[Any]) -> CompletedEvent:
    done = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(done) == 1, events
    return done[0]


PLAN = EngineRunOptions(permission_mode="plan")


# ── helpers in utils/antigravity_plan ───────────────────────────────────────


def test_escape_slash_only_for_slash_leading_prompts() -> None:
    assert plan_mod.escape_slash("/review the diff") == "Task: /review the diff"
    assert plan_mod.escape_slash("  /x") == "Task:   /x"
    assert plan_mod.escape_slash("add a line") == "add a line"
    assert plan_mod.escape_slash("see /etc/hosts") == "see /etc/hosts"


def test_note_plan_write_keeps_write_targets_only() -> None:
    writes: list[str] = []
    plan_mod.note_plan_write(writes, "view_file", {"TargetFile": "/a"})
    plan_mod.note_plan_write(writes, "write_to_file", {"AbsolutePath": "/b"})
    plan_mod.note_plan_write(writes, "write_to_file", None)
    plan_mod.note_plan_write(writes, "write_to_file", {"TargetFile": "/c.md"})
    plan_mod.note_plan_write(writes, "replace_file_content", {"TargetFile": "/d.md"})
    assert writes == ["/c.md", "/d.md"]
    for i in range(100):
        plan_mod.note_plan_write(writes, "write_to_file", {"TargetFile": f"/{i}"})
    assert len(writes) == 32


def test_find_plan_reads_the_plan_and_its_summary(home: Path) -> None:
    _write_plan(home)
    found = plan_mod.find_plan(
        CID, [f"~/.gemini/antigravity-cli/brain/{CID}/{PLAN_NAME}"]
    )
    assert found is not None
    assert found.name == PLAN_NAME
    assert found.body == PLAN_BODY
    assert found.summary == "Plan to add alpha."
    assert found.truncated is False


def test_find_plan_skips_walkthrough_task_and_feedback_false(home: Path) -> None:
    folder = _brain(home)
    walk = _write_plan(home, "walkthrough.md", "done")
    task = _write_plan(home, "task.md", "- [ ] x")
    nofb = _write_plan(home, "notes.md", "n", feedback=False)
    nometa_walk = folder / "other.md"
    nometa_walk.write_text("x")
    (folder / "other.md.metadata.json").write_text(json.dumps({"userFacing": True}))
    assert plan_mod.find_plan(CID, [str(walk), str(task), str(nofb)]) is None
    # A sidecar without requestFeedback (walkthrough-style) is not a plan.
    assert plan_mod.find_plan(CID, [str(nometa_walk)]) is None


def test_find_plan_accepts_a_missing_sidecar(home: Path) -> None:
    path = _write_plan(home, "implementation_plan.md", feedback=None)
    with structlog.testing.capture_logs() as logs:
        found = plan_mod.find_plan(CID, [str(path)])
    assert found is not None and found.name == "implementation_plan.md"
    assert any(r["event"] == "antigravity.plan.sidecar_missing" for r in logs)


def test_find_plan_newest_write_wins(home: Path) -> None:
    first = _write_plan(home, "plan_v1.md", "one")
    second = _write_plan(home, "plan_v2.md", "two")
    found = plan_mod.find_plan(CID, [str(first), str(second)])
    assert found is not None and found.body == "two"


def test_find_plan_never_reads_outside_the_conversation_folder(
    home: Path, tmp_path: Path
) -> None:
    other = _write_plan(home, cid="0d0d0d0d-1111-4222-8333-444444444444")
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    folder = _brain(home)
    link = folder / "link.md"
    link.symlink_to(outside)
    (folder / "link.md.metadata.json").write_text('{"requestFeedback": true}')
    nested = folder / "sub"
    nested.mkdir()
    (nested / "deep.md").write_text("deep")
    dotdot = f"{folder}/../{other.parent.name}/{other.name}"
    targets = [str(other), str(link), str(nested / "deep.md"), dotdot, str(outside)]
    assert plan_mod.find_plan(CID, targets) is None
    # A conversation id that could escape the brain folder is refused outright.
    assert plan_mod.find_plan("../x", [str(outside)]) is None
    assert plan_mod.find_plan(None, [str(other)]) is None


def test_find_plan_truncates_a_long_plan(home: Path) -> None:
    path = _write_plan(home, body="x" * (plan_mod.PLAN_BODY_CHARS + 50))
    found = plan_mod.find_plan(CID, [str(path)])
    assert found is not None and found.truncated
    assert len(found.body) == plan_mod.PLAN_BODY_CHARS
    assert "plan truncated" in plan_mod.plan_answer(found, "")


# ── the pending-card store ──────────────────────────────────────────────────


def _register(cid: str = CID, channel: int | None = 4242, **kw: Any) -> Any:
    defaults: dict[str, Any] = {
        "name": PLAN_NAME,
        "thread_id": None,
        "context": RunContext(project="demo"),
        "cwd": "/work/demo",
        "originator": 99,
    }
    defaults.update(kw)
    return plan_mod.register(conversation_id=cid, channel_id=channel, **defaults)


def test_claim_is_exclusive_and_release_restores() -> None:
    plan = _register()
    rid = plan.request_id
    assert plan_mod.claim(rid, 4242, "tap-1") == ("ok", plan)
    assert plan_mod.claim(rid, 4242, "tap-1")[0] == "ok"  # same tap, again
    assert plan_mod.claim(rid, 4242, "tap-2")[0] == "claimed"
    plan_mod.release("tap-1")
    assert plan_mod.claim(rid, 4242, "tap-2")[0] == "ok"
    plan_mod.resolve(rid, "deny")
    assert plan_mod.claim(rid, 4242, "tap-3")[0] == "answered"


def test_card_answers_only_in_its_own_chat() -> None:
    plan = _register()
    assert plan_mod.claim(plan.request_id, 1, "tap")[0] == "expired"
    assert plan_mod.peek(plan.request_id, 1) is None
    assert plan_mod.claim("nope", 4242, "tap")[0] == "expired"


def test_new_card_supersedes_older_ones_of_the_conversation() -> None:
    old = _register()
    other = _register(cid="0d0d0d0d-1111-4222-8333-444444444444")
    new = _register()
    assert plan_mod.claim(old.request_id, 4242, "t")[0] == "superseded"
    assert plan_mod.claim(other.request_id, 4242, "t")[0] == "ok"
    assert plan_mod.claim(new.request_id, 4242, "u")[0] == "ok"


def test_cards_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _register()
    now = plan.created_at + plan_mod.PENDING_TTL_S + 1
    monkeypatch.setattr(plan_mod, "_now", lambda: now)
    assert plan_mod.claim(plan.request_id, 4242, "t")[0] == "expired"


def test_approve_arms_a_one_shot_marker() -> None:
    plan = _register()
    plan_mod.resolve(plan.request_id, "approve")
    assert plan_mod.take_approved(CID) is plan
    assert plan_mod.take_approved(CID) is None


def test_approve_context_is_workspace_in_the_plan_runs_project() -> None:
    plan = _register(
        context=RunContext(project="demo", branch="feat", trigger_source="at:1")
    )
    ctx = plan_mod.approve_context(plan)
    assert (ctx.project, ctx.branch) == ("demo", "feat")
    assert ctx.permission_mode == "workspace"
    assert ctx.trigger_source is None
    bare = plan_mod.approve_context(_register(context=None))
    assert bare.project is None and bare.permission_mode == "workspace"


def test_keyboard_fits_telegram_callback_limit() -> None:
    plan = _register()
    rows = plan_mod.keyboard(plan.request_id)
    labels = [b["text"] for row in rows for b in row]
    assert labels == ["✅ Approve Plan", "❌ Deny", "💬 Let's discuss"]
    for row in rows:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64
            assert button["callback_data"].startswith("agy_plan:")


# ── the runner ──────────────────────────────────────────────────────────────


def _args(options: EngineRunOptions | None) -> list[str]:
    runner = AntigravityRunner(antigravity_cmd="agy")
    with apply_run_options(options):
        state = runner.new_state("p", None)
        return runner.build_args("p", None, state=state)


def test_plan_first_argv_is_agy_plan_mode_under_the_workspace_agent(
    project: Path,
) -> None:
    args = _args(PLAN)
    assert args[-2:] == ["--mode=plan", AGENT_FLAG]
    assert agy.BYPASS_FLAG not in args
    # C7: the flag would turn --mode=plan into a no-op.
    assert "--disable-slash-commands" not in args
    assert "--disable-slash-commands" in _args(None)


def test_plan_first_stdin_escapes_a_slash_prompt() -> None:
    runner = AntigravityRunner(antigravity_cmd="agy")
    with apply_run_options(PLAN):
        state = runner.new_state("/review", None)
    payload = json.loads(runner.stdin_payload("/review it", None, state=state))
    assert payload["message"]["content"] == "Task: /review it"
    with apply_run_options(None):
        state = runner.new_state("/review", None)
    payload = json.loads(runner.stdin_payload("/review it", None, state=state))
    assert payload["message"]["content"] == "/review it"


def test_plan_turn_posts_the_plan_and_a_card(home: Path, run_vars: RunContext) -> None:
    _write_plan(home)
    with structlog.testing.capture_logs() as logs:
        events, state = _replay(_lines("context_turn"), options=PLAN)
    done = _completed(events)
    assert done.ok is True
    assert done.answer.startswith(f"📋 **Plan** — `{PLAN_NAME}`\n\n{PLAN_BODY}")
    assert "(condensed)" in done.answer  # agy's own reply follows the plan
    card = review_card(done.usage)
    assert card is not None
    assert card["text"].startswith(f"📋 Antigravity's plan is ready — {PLAN_NAME}")
    data = [b["callback_data"] for row in card["buttons"] for b in row]
    rid = data[0].rsplit(":", 1)[1]
    assert data == [f"agy_plan:{a}:{rid}" for a in ("approve", "deny", "discuss")]
    pending = plan_mod.peek(rid, 4242)
    assert pending is not None
    assert (pending.conversation_id, pending.channel_id, pending.thread_id) == (
        CID,
        4242,
        7,
    )
    assert pending.originator == 99 and pending.context == run_vars
    assert state.mode_label == "plan first"
    events_seen = [r["event"] for r in logs]
    assert "antigravity.plan.detected" in events_seen
    assert "antigravity.plan.card" in events_seen


def test_plan_turn_without_a_plan_is_an_ordinary_answer(home: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        events, _ = _replay(_lines("context_turn"), options=PLAN)  # no file
    done = _completed(events)
    assert done.ok is True
    assert REVIEW_CARD_KEY not in (done.usage or {})
    assert done.answer.endswith(plan_mod.NO_PLAN_NOTE)
    assert any(r["event"] == "antigravity.plan.no_artifact" for r in logs)


def test_workspace_turn_never_looks_for_a_plan(home: Path) -> None:
    _write_plan(home)
    events, state = _replay(_lines("context_turn"), options=None)
    done = _completed(events)
    assert REVIEW_CARD_KEY not in (done.usage or {})
    assert "📋" not in done.answer
    assert state.plan_writes == []


def test_unattended_plan_turn_posts_the_plan_and_changes_nothing(home: Path) -> None:
    _write_plan(home)
    options = EngineRunOptions(permission_mode="plan", unattended_trigger="cron:x")
    with structlog.testing.capture_logs() as logs:
        events, _ = _replay(_lines("context_turn"), options=options)
    done = _completed(events)
    assert done.answer.startswith(f"📋 **Plan** — `{PLAN_NAME}`")
    assert REVIEW_CARD_KEY not in (done.usage or {})
    unattended = (done.usage or {})["unattended"]
    assert unattended["trigger"] == "cron:x"
    assert unattended["denied"] == {"plan approval": 1}
    assert 'permission_mode = "workspace"' in unattended["hint"]
    deny = [r for r in logs if r["event"] == "permission.unattended_deny"]
    assert deny and deny[0]["reason"] == "plan_mode"
    assert plan_mod._PENDING == {}


def test_a_new_plan_turn_supersedes_the_conversations_cards() -> None:
    old = _register()
    runner = AntigravityRunner(antigravity_cmd="agy")
    with apply_run_options(PLAN):
        runner.new_state("revise it", ResumeToken(engine="antigravity", value=CID))
    assert plan_mod.claim(old.request_id, 4242, "t")[0] == "superseded"


def test_the_approved_run_says_so(home: Path) -> None:
    plan = _register()
    plan_mod.resolve(plan.request_id, "approve")
    resume = ResumeToken(engine="antigravity", value=CID)
    options = EngineRunOptions(permission_mode="workspace")
    events, state = _replay(
        _lines("context_resume"),
        options=options,
        resume=resume,
        prompt=plan_mod.APPROVE_PROMPT,
    )
    assert state.mode_label == "workspace · plan approved"
    done = _completed(events)
    assert done.answer.startswith(f"📋 Plan (approved) — `{PLAN_NAME}`\n\n")
    # One-shot: a later Workspace run on the conversation is plain again.
    _, again = _replay(
        _lines("context_resume"),
        options=options,
        resume=resume,
        prompt=plan_mod.APPROVE_PROMPT,
    )
    assert again.approved_plan is None and again.mode_label == "workspace"


def test_plan_denial_wording() -> None:
    text = agy.denial_paragraph(["file outside the project"], mode="plan", trigger=None)
    assert text.endswith("Plan first works only with the project's own files.")
    assert "/new" not in text
    # #1073: a write is plan mode itself, wherever the file is.
    text = agy.denial_paragraph(["file change"], mode="plan", trigger=None)
    assert text == (
        "⚠️ Antigravity was blocked from changing a file — Plan first doesn't"
        " change files until you approve the plan."
    )
    both = agy.denial_paragraph(
        ["file change", "file outside the project"], mode="plan", trigger=None
    )
    assert both.startswith(
        "⚠️ Antigravity was blocked from changing a file and from using a file"
        " outside the project — Plan first doesn't change files"
    )


@pytest.mark.anyio
async def test_plan_first_run_end_to_end_with_fake_agy(
    home: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole spawn path: no gate refusal any more, agy's plan mode on
    the argv, the escaped prompt on stdin, the card on the result."""
    _write_plan(home)
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    monkeypatch.setenv(
        "UNTETHER_FAKE_AGY_SCRIPT", str(FIXTURES / "context_turn.script")
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with apply_run_options(PLAN):
        events = [e async for e in runner.run("/plan the change", None)]
    done = _completed(events)
    assert done.ok is True, done.error
    assert review_card(done.usage) is not None
    seen = json.loads(record.read_text())
    assert "--mode=plan" in seen["argv"] and AGENT_FLAG in seen["argv"]
    assert "--disable-slash-commands" not in seen["argv"]
    line = json.loads(seen["stdin"].splitlines()[0])
    assert line["message"]["content"] == "Task: /plan the change"


# ── the bridge: card after the final, custom unattended hint ────────────────


def test_append_review_card_adds_a_trailing_message_with_buttons() -> None:
    from untether.runner_bridge import _append_review_card

    buttons = plan_mod.keyboard("abc")
    usage = {REVIEW_CARD_KEY: {"text": "card", "buttons": buttons}}
    base = RenderedMessage(text="final", extra={"entities": []})
    out = _append_review_card(base, usage)
    (chunk,) = out.extra["followups"]
    assert chunk.text == "card"
    assert chunk.extra["reply_markup"] == {"inline_keyboard": buttons}
    assert out.text == "final"
    split = RenderedMessage(
        text="one", extra={"followups": [RenderedMessage(text="two")]}
    )
    out = _append_review_card(split, usage)
    assert [m.text for m in out.extra["followups"]] == ["two", "card"]
    for bad in (None, {}, {REVIEW_CARD_KEY: {"text": "x"}}, {REVIEW_CARD_KEY: "x"}):
        assert _append_review_card(base, bad) is base
    bad_rows = {REVIEW_CARD_KEY: {"text": "x", "buttons": [[]]}}
    assert _append_review_card(base, bad_rows) is base


def test_unattended_footer_uses_the_engines_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import untether.runner_bridge as rb

    monkeypatch.setattr(rb, "_UNATTENDED_HINTED", {})
    footer = rb._unattended_footer(
        {
            "trigger": "cron:p",
            "mode": "plan",
            "denied": {"plan approval": 1},
            "hint": plan_mod.unattended_hint("cron:p"),
        }
    )
    assert "denied plan approval" in footer
    assert 'set permission_mode = "workspace" on the cron' in footer
    assert "plan-auto" not in footer


@pytest.mark.anyio
async def test_bridge_sends_the_card_after_the_final() -> None:
    from tests.test_exec_bridge import FakeTransport
    from untether.markdown import MarkdownPresenter
    from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
    from untether.runners.mock import Return, ScriptRunner

    buttons = plan_mod.keyboard("abc123")
    runner = ScriptRunner(
        [
            Return(
                answer="📋 **Plan** — `p.md`\n\nDo it.",
                usage={REVIEW_CARD_KEY: {"text": "card text", "buttons": buttons}},
            )
        ],
        engine="antigravity",
        resume_value=CID,
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="plan it"),
        resume_token=None,
    )
    final = transport.send_calls[-1]["message"]
    assert "Do it." in final.text
    card = final.extra["followups"][-1]
    assert card.text == "card text"
    assert card.extra["reply_markup"] == {"inline_keyboard": buttons}


# ── the callback backend ────────────────────────────────────────────────────


class _Executor:
    def __init__(self, *, with_enqueue: bool = True) -> None:
        self.edits: list[tuple[Any, RenderedMessage]] = []
        self.enqueued: list[dict[str, Any]] = []
        if not with_enqueue:
            self.enqueue_resume = None  # type: ignore[assignment]

    async def edit(self, ref: Any, message: RenderedMessage) -> Any:
        self.edits.append((ref, message))
        return ref

    async def send(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the plan card is edited, never answered anew")

    async def enqueue_resume(self, prompt: str, resume: ResumeToken, **kw: Any) -> None:
        self.enqueued.append({"prompt": prompt, "resume": resume, **kw})


def _ctx(
    action: str,
    rid: str,
    *,
    executor: _Executor | None = None,
    channel: int = 4242,
    cwd: str | None = "/work/demo",
    query: str | None = "q-1",
) -> Any:
    runtime = SimpleNamespace(
        resolve_run_cwd=lambda context: Path(cwd) if cwd is not None else None
    )
    return SimpleNamespace(
        args_text=f"{action}:{rid}",
        message=SimpleNamespace(channel_id=channel, message_id=555, thread_id=None),
        callback_query_id=query,
        executor=executor or _Executor(),
        runtime=runtime,
    )


@pytest.mark.anyio
async def test_approve_resumes_the_conversation_in_workspace() -> None:
    plan = _register(thread_id=7)
    executor = _Executor()
    with structlog.testing.capture_logs() as logs:
        result = await AntigravityPlanCommand().handle(
            _ctx("approve", plan.request_id, executor=executor)
        )
    assert result is None
    (job,) = executor.enqueued
    assert job["prompt"] == plan_mod.APPROVE_PROMPT
    assert job["resume"] == ResumeToken(engine="antigravity", value=CID)
    assert job["context"].permission_mode == "workspace"
    assert job["context"].project == "demo"
    assert job["reply_to_message_id"] == 555 and job["thread_id"] == 7
    (ref, card) = executor.edits[0]
    assert ref.message_id == 555
    assert "✅ Plan approved" in card.text
    assert card.extra["reply_markup"] == {"inline_keyboard": []}
    assert plan_mod.take_approved(CID) is plan
    decision = [r for r in logs if r["event"] == "antigravity.plan.decision"]
    assert decision[0]["decision"] == "approve" and decision[0]["path"] == "resume"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "words"),
    [("deny", "❌ Plan denied — nothing was changed"), ("discuss", "💬 Let's discuss")],
)
async def test_deny_and_discuss_run_nothing(action: str, words: str) -> None:
    plan = _register()
    executor = _Executor()
    result = await AntigravityPlanCommand().handle(
        _ctx(action, plan.request_id, executor=executor)
    )
    assert result is None
    assert executor.enqueued == []
    assert words in executor.edits[0][1].text
    assert plan_mod.take_approved(CID) is None


@pytest.mark.anyio
async def test_a_second_tap_is_told_it_was_answered() -> None:
    plan = _register()
    cmd = AntigravityPlanCommand()
    await cmd.handle(_ctx("deny", plan.request_id))
    executor = _Executor()
    result = await cmd.handle(
        _ctx("approve", plan.request_id, executor=executor, query="q-2")
    )
    # #1074: no second message; the card keeps its decision, without buttons.
    assert result is None
    assert executor.enqueued == []
    ((ref, card),) = executor.edits
    assert ref.message_id == 555
    assert "❌ Plan denied" in card.text
    assert card.extra["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.anyio
async def test_superseded_expired_and_foreign_taps_do_nothing() -> None:
    """#1074: a refused tap runs nothing and takes the card's buttons off, so
    it can't be tapped again; the toast already said why."""
    old = _register()
    _register()  # a newer plan for the same conversation
    cmd = AntigravityPlanCommand()
    executor = _Executor()
    result = await cmd.handle(_ctx("approve", old.request_id, executor=executor))
    assert result is None
    ((ref, card),) = executor.edits
    assert ref.message_id == 555
    assert card.text == (
        f"📋 Antigravity's plan is ready — {PLAN_NAME}\n\n"
        "ℹ️ Replaced by a newer plan — use the latest card."
    )
    assert card.extra["reply_markup"] == {"inline_keyboard": []}
    # Retired by the tap: a later run won't edit it again.
    assert plan_mod.retire([old.request_id]) == []
    executor = _Executor()
    result = await cmd.handle(_ctx("approve", "deadbeef0000", executor=executor))
    assert result is None
    ((_, card),) = executor.edits
    assert (
        "expired" in card.text and card.extra["reply_markup"]["inline_keyboard"] == []
    )
    fresh = _register(cid="0d0d0d0d-1111-4222-8333-444444444444")
    executor = _Executor()
    result = await cmd.handle(
        _ctx("approve", fresh.request_id, executor=executor, channel=1)
    )
    assert result is None and "expired" in executor.edits[0][1].text
    assert executor.enqueued == []
    # The other chat's card itself is untouched and still answerable.
    assert plan_mod.claim(fresh.request_id, 4242, "owner")[0] == "ok"


@pytest.mark.anyio
async def test_refused_tap_falls_back_to_a_message_when_the_edit_fails() -> None:
    old = _register()
    _register()

    class _Failing(_Executor):
        async def edit(self, ref: Any, message: RenderedMessage) -> Any:
            return None

    result = await AntigravityPlanCommand().handle(
        _ctx("approve", old.request_id, executor=_Failing())
    )
    assert result is not None and "newer plan" in result.text


@pytest.mark.anyio
async def test_a_tap_in_flight_elsewhere_leaves_the_card_alone() -> None:
    plan = _register()
    assert plan_mod.claim(plan.request_id, 4242, "other-tap")[0] == "ok"
    executor = _Executor()
    result = await AntigravityPlanCommand().handle(
        _ctx("deny", plan.request_id, executor=executor, query="q-9")
    )
    assert result is not None and executor.edits == []


def test_a_newer_plan_turn_retires_the_old_cards_buttons(
    home: Path, run_vars: RunContext
) -> None:
    """#1074: the plan turn that replaces a card hands its id and closing
    text to the bridge once (``usage[RETIRED_CARDS_KEY]``); the new card
    carries its own id so the bridge can find it later."""
    old = _register()
    _write_plan(home)
    resume = ResumeToken(engine="antigravity", value=CID)
    events, state = _replay(_lines("context_turn"), options=PLAN, resume=resume)
    assert state.superseded_cards == [old.request_id]
    done = _completed(events)
    assert retired_cards(done.usage) == [
        (
            old.request_id,
            f"📋 Antigravity's plan is ready — {PLAN_NAME}\n\n"
            "ℹ️ Replaced by a newer plan — use the latest card.",
        )
    ]
    card = review_card(done.usage)
    assert card is not None and card["id"] != old.request_id
    assert plan_mod.peek(card["id"], 4242) is not None
    assert plan_mod.retire([old.request_id]) == []  # handed out once


@pytest.mark.anyio
async def test_bridge_edits_a_retired_card_it_posted() -> None:
    """#1074: the bridge records where a card with an id landed and, when a
    later run retires it, edits it through the transport without buttons."""
    from tests.test_exec_bridge import FakeTransport
    from untether.markdown import MarkdownPresenter
    from untether.runner import pop_review_card_ref
    from untether.runner_bridge import (
        ExecBridgeConfig,
        IncomingMessage,
        _append_review_card,
        handle_message,
    )
    from untether.runners.mock import Return, ScriptRunner
    from untether.transport import MessageRef

    usage = {REVIEW_CARD_KEY: {"id": "old1", "text": "card", "buttons": [[{}]]}}
    chunk = _append_review_card(RenderedMessage(text="f"), usage).extra["followups"][0]
    card_ref = MessageRef(channel_id=123, message_id=77, thread_id=None)
    chunk.extra["on_sent"](card_ref)  # what the Telegram transport does

    runner = ScriptRunner(
        [
            Return(
                answer="next plan",
                usage={
                    RETIRED_CARDS_KEY: [
                        {"id": "old1", "text": "replaced"},
                        {"id": "never-posted", "text": "x"},
                        {"id": 5, "text": "bad"},
                    ]
                },
            )
        ],
        engine="antigravity",
        resume_value=CID,
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="again"),
        resume_token=None,
    )
    retired = [c for c in transport.edit_calls if c["ref"] == card_ref]
    assert len(retired) == 1
    assert retired[0]["message"].text == "replaced"
    assert retired[0]["message"].extra["reply_markup"] == {"inline_keyboard": []}
    # Each card is retired once: its ref is gone.
    assert pop_review_card_ref("old1") is None
    assert retired_cards({RETIRED_CARDS_KEY: "nope"}) == []


@pytest.mark.anyio
async def test_approve_refuses_when_the_project_moved() -> None:
    plan = _register()
    executor = _Executor()
    result = await AntigravityPlanCommand().handle(
        _ctx("approve", plan.request_id, executor=executor, cwd="/elsewhere")
    )
    assert result is not None and "project directory changed" in result.text
    assert executor.enqueued == []
    assert plan_mod.take_approved(CID) is None


@pytest.mark.anyio
async def test_approve_without_a_scheduler_keeps_the_card() -> None:
    plan = _register()
    result = await AntigravityPlanCommand().handle(
        _ctx("approve", plan.request_id, executor=_Executor(with_enqueue=False))
    )
    assert result is not None and "couldn't start" in result.text
    assert plan_mod.claim(plan.request_id, 4242, "later")[0] == "ok"


def test_early_toast_claims_for_the_tap_and_release_frees_it() -> None:
    plan = _register()
    cmd = AntigravityPlanCommand
    assert (
        cmd.early_answer_toast(
            f"approve:{plan.request_id}", channel_id=4242, claim_owner="a"
        )
        == "Approved"
    )
    assert (
        cmd.early_answer_toast(
            f"deny:{plan.request_id}", channel_id=4242, claim_owner="b"
        )
        == "Already answered"
    )
    cmd.release_early_claim("a")
    assert (
        cmd.early_answer_toast(f"deny:{plan.request_id}", channel_id=4242) == "Denied"
    )
    assert cmd.early_answer_toast("approve:nope", channel_id=4242) == (
        "This plan has expired"
    )
    assert cmd.early_answer_toast("bogus:x") is None


def test_only_the_originator_may_answer_when_configured() -> None:
    from untether.telegram.approval_originator import callback_originator_mismatch

    plan = _register(originator=99)
    args = f"approve:{plan.request_id}"
    assert callback_originator_mismatch("agy_plan", args, 4242, 99) is None
    assert callback_originator_mismatch("agy_plan", args, 4242, 5) == (
        plan.request_id,
        99,
    )
    cron_plan = _register(cid="0d0d0d0d-1111-4222-8333-444444444444", originator=None)
    assert (
        callback_originator_mismatch(
            "agy_plan", f"approve:{cron_plan.request_id}", 4242, 5
        )
        is None
    )


def test_callback_backend_is_registered() -> None:
    from untether.commands import get_command

    backend = get_command("agy_plan", allowlist=None, required=False)
    assert isinstance(backend, AntigravityPlanCommand)


# ── /config and /planmode ───────────────────────────────────────────────────


def test_plan_first_and_ask_me_are_offered() -> None:
    from untether.telegram.commands import _antigravity_mode_text as text

    assert text.button_text("agp") == "Plan first"
    # #980: Ask me is real; without Telegram approvals it reads "set up".
    assert text.button_text("aga") == "Ask me"
    assert text.button_text("aga", gate_ready=False) == "🔒 Ask me — set up"
    assert text.button_text("agp", gate_ready=False) == "Plan first"


@pytest.mark.parametrize(
    ("before", "after", "new"),
    [
        ("workspace", "plan", False),
        ("plan", "workspace", False),
        ("ask", "full", False),
        ("full", "ask", False),
        ("workspace", "ask", True),
        ("plan", "ask", True),
        ("ask", "workspace", True),
        ("workspace", "full", True),
        (None, "ask", True),
        ("yolo", "plan", False),
    ],
)
def test_needs_new_only_across_agent_groups(
    before: str | None, after: str, new: bool
) -> None:
    """#980 R3-3: Workspace / Plan first share Untether's no-shell agent, Ask
    me / Full access agy's default one; only a switch across needs /new."""
    from untether.telegram.commands import _antigravity_mode_text as text

    assert text.needs_new(before, after) is new


async def _planmode(
    tmp_path: Path, args: str, *, toml_mode: str | None = None
) -> tuple[str, str | None]:
    from tests.test_command_engine_gates import FakeCommandContext, FakeTransportRuntime
    from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
    from untether.telegram.commands.planmode import PlanModeCommand

    runner = SimpleNamespace(default_permission_mode=toml_mode)
    ctx = FakeCommandContext(
        args_text=args,
        config_path=tmp_path / "untether.toml",
        runtime=FakeTransportRuntime(default_engine="antigravity", runner=runner),
    )
    result = await PlanModeCommand().handle(ctx)  # type: ignore[arg-type]
    assert result is not None
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    override = await prefs.get_engine_override(100, "antigravity")
    return result.text, override.permission_mode if override else None


@pytest.mark.anyio
async def test_planmode_antigravity_on_off_show_clear(tmp_path: Path) -> None:
    text, stored = await _planmode(tmp_path, "on")
    assert stored == "plan" and "<b>plan first</b>" in text
    text, stored = await _planmode(tmp_path, "show")
    assert stored == "plan" and "<b>plan first</b>" in text
    text, stored = await _planmode(tmp_path, "")  # toggle → off
    assert stored is None and "<b>workspace</b> (default)" in text
    text, stored = await _planmode(tmp_path, "")  # toggle → on
    assert stored == "plan"
    text, stored = await _planmode(tmp_path, "clear")
    assert stored is None and "override cleared" in text


@pytest.mark.anyio
async def test_planmode_antigravity_off_beats_a_toml_plan_default(
    tmp_path: Path,
) -> None:
    text, stored = await _planmode(tmp_path, "show", toml_mode="plan")
    assert "<b>plan first</b> (from untether.toml)" in text
    text, stored = await _planmode(tmp_path, "off", toml_mode="plan")
    assert stored == "workspace" and "<b>workspace</b>" in text


@pytest.mark.anyio
async def test_planmode_antigravity_from_full_access_says_new(tmp_path: Path) -> None:
    """#1060: a conversation keeps the tools it started with, so leaving or
    entering Full access needs /new; Workspace ↔ Plan first doesn't."""
    text, stored = await _planmode(tmp_path, "on", toml_mode="full")
    assert stored == "plan"
    assert text.startswith("send /new to start using it. permission mode")
    text, _ = await _planmode(tmp_path, "off", toml_mode="full")
    assert text.startswith("send /new to start using it.")
    assert "<b>full access</b>" in text
    text, _ = await _planmode(tmp_path, "on")
    text, _ = await _planmode(tmp_path, "off")
    assert text.startswith("applies from your next message.")


@pytest.mark.anyio
async def test_planmode_antigravity_from_ask_me_says_new(tmp_path: Path) -> None:
    """#980 R3-3: Ask me runs on agy's default agent like Full access, so a
    switch to Plan first (the no-shell agent) needs /new too."""
    text, stored = await _planmode(tmp_path, "on", toml_mode="ask")
    assert stored == "plan"
    assert text.startswith("send /new to start using it. permission mode")
    text, _ = await _planmode(tmp_path, "off", toml_mode="ask")
    assert text.startswith("send /new to start using it.")
    assert "<b>ask me</b>" in text


@pytest.mark.anyio
@pytest.mark.parametrize("arg", ["auto", "plan-auto"])
async def test_planmode_antigravity_refuses_claude_only_modes(
    tmp_path: Path, arg: str
) -> None:
    text, stored = await _planmode(tmp_path, arg)
    assert stored is None
    assert "isn't available for Antigravity" in text


def test_conftest_isolates_the_brain_folder() -> None:
    # The autouse fixture points it away from the host's ~/.gemini.
    real = Path(os.path.expanduser("~")) / ".gemini"
    assert not str(plan_mod.brain_root()).startswith(str(real))
