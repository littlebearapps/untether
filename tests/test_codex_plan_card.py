"""The Codex plan card on the app-server transport (#965, phase 965-2).

After a plan turn with a ``plan`` item an attended chat gets the plan card
(✅ Approve Plan / ❌ Deny / 💬 Keep planning). The card reuses Antigravity's
store and callback (``utils/antigravity_plan``, ``agy_plan:``) with
``engine="codex"`` (D4). Approve resumes the thread with "Implement the
plan." under Ask me (D2); the thread is plan-touched, so that run opens an
experimental connection and sends ``collaborationMode default`` (D1).
``plan-auto`` resolves the card itself and the bridge enqueues the same run.
Cron and webhook runs post the plan with a 🔒 footer and no card.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_antigravity_plan import _ctx, _Executor
from tests.test_codex_app_server_runner import THREAD, Fake, _completed, _run, _runner
from tests.test_codex_plan_mode import _fake, _plan_item_frame
from untether.model import ResumeToken
from untether.runner import RETIRED_CARDS_KEY, REVIEW_CARD_KEY, review_card
from untether.runners import codex_app_server as gate
from untether.runners.run_options import EngineRunOptions
from untether.telegram.commands.antigravity_plan import AntigravityPlanCommand
from untether.telegram.loop import _apply_trigger_overrides
from untether.utils import antigravity_plan as plan_mod

project = _rt.project
gate_calls = _rt.gate_calls

_RESUME = ResumeToken(engine="codex", value=THREAD)
_PLAN = EngineRunOptions(permission_mode="plan")
_TRUSTED = {"trust_level": "trusted"}


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    gate._PLAN_THREADS.clear()
    gate._RUNS_BY_THREAD.clear()
    plan_mod._reset_for_tests()
    yield
    gate._PLAN_THREADS.clear()
    gate._RUNS_BY_THREAD.clear()
    plan_mod._reset_for_tests()
    plan_mod.install_enqueue(None)


async def _plan_run(
    tmp_path: Path, name: str = "plan", options: EngineRunOptions = _PLAN
) -> Any:
    fake = _fake(tmp_path, name, messages=[], notifications=[_plan_item_frame()])
    return _completed(await _run(_runner(fake), options=options))


def _card_id(done: Any) -> str:
    card = review_card(done.usage)
    assert card is not None
    return card["id"]


@pytest.mark.anyio
async def test_plan_turn_posts_card(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    with capture_logs() as logs:
        done = await _plan_run(tmp_path)
    card = review_card(done.usage)
    assert card is not None
    assert card["text"].startswith("📋 Codex's plan is ready — Count only")
    assert "Ask me" in card["text"]
    labels = [b["text"] for row in card["buttons"] for b in row]
    assert labels == ["✅ Approve Plan", "❌ Deny", "💬 Keep planning"]
    datas = [b["callback_data"] for row in card["buttons"] for b in row]
    assert all(d.startswith("agy_plan:") and len(d.encode()) <= 64 for d in datas)
    pending = plan_mod.peek(card["id"], None)
    assert pending is not None
    assert pending.engine == "codex" and pending.conversation_id == THREAD
    assert pending.cwd == str(project)
    assert [e["request_id"] for e in logs if e["event"] == "codex.plan.card"] == [
        card["id"]
    ]


@pytest.mark.anyio
async def test_no_plan_item_no_card(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, messages=[["a plain answer", "final_answer"]])
    done = _completed(await _run(_runner(fake), options=_PLAN))
    assert done.answer == "a plain answer"  # no note either
    assert review_card(done.usage) is None


@pytest.mark.anyio
async def test_non_plan_run_with_plan_item_has_no_card(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    done = await _plan_run(tmp_path, options=EngineRunOptions(permission_mode="ask"))
    assert review_card(done.usage) is None


async def _approve(project: Path, rid: str, **kw: Any) -> tuple[Any, _Executor]:
    executor = _Executor()
    result = await AntigravityPlanCommand().handle(
        _ctx("approve", rid, executor=executor, channel=None, **kw)  # type: ignore[arg-type]
    )
    return result, executor


@pytest.mark.anyio
async def test_approve_enqueues_implement_the_plan_default_mode(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    rid = _card_id(await _plan_run(tmp_path))
    result, executor = await _approve(project, rid, cwd=str(project))
    assert result is None
    (job,) = executor.enqueued
    assert job["prompt"] == "Implement the plan."
    assert job["resume"] == _RESUME
    (_ref, card) = executor.edits[0]
    assert "✅ Plan approved — Codex is carrying it out (Ask me)." in card.text
    assert card.extra["reply_markup"] == {"inline_keyboard": []}
    # The resumed run: the chat is still in plan mode, the context says ask.
    options = _apply_trigger_overrides(_PLAN, job["context"], engine="codex", log=False)
    assert options is not None and options.permission_mode == "ask"
    fake = _fake(
        tmp_path,
        "implement",
        messages=[["done it", "final_answer"]],
        config={"projects": {str(project): _TRUSTED}},
    )
    done = _completed(
        await _run(
            _runner(fake), resume=job["resume"], options=options, prompt=job["prompt"]
        )
    )
    init = fake.requests("initialize")[0]
    assert init["capabilities"]["experimentalApi"] is True
    assert fake.requests("turn/start")[0]["collaborationMode"]["mode"] == "default"
    assert done.answer.startswith("📋 Plan (approved)")
    assert done.answer.rstrip().endswith("done it")
    assert plan_mod.take_approved(THREAD) is None  # one-shot


@pytest.mark.anyio
async def test_approve_uses_d2_tier(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    rid = _card_id(await _plan_run(tmp_path))
    _result, executor = await _approve(project, rid, cwd=str(project))
    context = executor.enqueued[0]["context"]
    assert context.permission_mode == "ask"
    assert context.trigger_source is None
    options = _apply_trigger_overrides(_PLAN, context, engine="codex", log=False)
    fake = _fake(tmp_path, "implement", config={"projects": {str(project): _TRUSTED}})
    await _run(
        _runner(fake), resume=_RESUME, options=options, prompt="Implement the plan."
    )
    resume = fake.requests("thread/resume")[0]
    assert resume["approvalPolicy"] == "on-request"
    assert resume["sandbox"] == "workspace-write"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "words"),
    [
        ("deny", "❌ Plan denied — nothing was changed"),
        ("discuss", "💬 Keep planning — reply with what to change and Codex"),
    ],
)
async def test_deny_and_keep_planning(
    tmp_path: Path, project: Path, gate_calls: list[str], action: str, words: str
) -> None:
    rid = _card_id(await _plan_run(tmp_path))
    executor = _Executor()
    result = await AntigravityPlanCommand().handle(
        _ctx(action, rid, executor=executor, channel=None)  # type: ignore[arg-type]
    )
    assert result is None
    assert executor.enqueued == []
    assert words in executor.edits[0][1].text
    assert plan_mod.take_approved(THREAD) is None
    toast = AntigravityPlanCommand.early_answer_toast(f"{action}:{rid}")
    if action == "discuss":
        # Keep planning leaves the card answerable.
        assert toast == "Keep planning…"


@pytest.mark.anyio
async def test_cwd_moved_refuses_approve(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    rid = _card_id(await _plan_run(tmp_path))
    result, executor = await _approve(project, rid, cwd="/elsewhere")
    assert result is not None and "project directory changed" in result.text
    assert executor.enqueued == []
    assert plan_mod.take_approved(THREAD) is None


@pytest.mark.anyio
async def test_newer_plan_supersedes_card(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    first = _card_id(await _plan_run(tmp_path, "one"))
    # Pretend the bridge posted the first card, so it can be retired.
    fake = _fake(tmp_path, "two", messages=[], notifications=[_plan_item_frame()])
    done = _completed(await _run(_runner(fake), resume=_RESUME, options=_PLAN))
    second = _card_id(done)
    assert second != first
    old = plan_mod.peek(first, None)
    assert old is not None and old.status == "superseded"
    retired = done.usage[RETIRED_CARDS_KEY]
    assert [r["id"] for r in retired] == [first]
    assert "Replaced by a newer plan" in retired[0]["text"]


@pytest.mark.anyio
async def test_plan_auto_approves_without_tap(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    with capture_logs() as logs:
        done = await _plan_run(
            tmp_path, options=EngineRunOptions(permission_mode="plan-auto")
        )
    card = done.usage[REVIEW_CARD_KEY]
    assert card["auto_approve"] is True
    assert card["buttons"] == []
    assert "✅ Plan approved (auto)" in card["text"]
    assert review_card(done.usage) is not None  # the bridge still posts it
    assert [e["request_id"] for e in logs if e["event"] == "codex.plan.auto_approved"]
    approved = plan_mod.take_approved(THREAD)
    assert approved is not None and approved.request_id == card["id"]


@pytest.mark.anyio
async def test_plan_auto_bridge_enqueues_the_implement_run() -> None:
    from tests.test_exec_bridge import FakeTransport
    from untether.context import RunContext
    from untether.markdown import MarkdownPresenter
    from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
    from untether.runners.mock import Return, ScriptRunner

    plan = plan_mod.register(
        engine="codex",
        conversation_id=THREAD,
        name="p",
        channel_id=123,
        thread_id=7,
        context=RunContext(project="demo"),
        cwd=None,
        originator=None,
    )
    plan_mod.resolve(plan.request_id, "approve")
    calls: list[tuple[Any, ...]] = []

    async def enqueue(*args: Any) -> None:
        calls.append(args)

    plan_mod.install_enqueue(enqueue)
    card = {
        "id": plan.request_id,
        "text": plan_mod.card_text("p", "approve_auto", engine="codex"),
        "buttons": [],
        "auto_approve": True,
    }
    runner = ScriptRunner(
        [Return(answer="the plan", usage={REVIEW_CARD_KEY: card})],
        engine="codex",
        resume_value=THREAD,
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with capture_logs() as logs:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="plan it"),
            resume_token=None,
        )
    (final,) = [
        c["message"] for c in transport.send_calls if "followups" in c["message"].extra
    ]
    posted = final.extra["followups"][-1]
    assert "Plan approved (auto)" in posted.text
    assert posted.extra["reply_markup"] == {"inline_keyboard": []}
    ((chat, reply_to, prompt, token, context, thread),) = calls
    assert (chat, reply_to, thread) == (123, 10, 7)
    assert prompt == "Implement the plan."
    assert token == _RESUME
    assert context.permission_mode == "ask" and context.project == "demo"
    assert [e for e in logs if e["event"] == "codex.plan.auto_enqueued"]


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["cron:nightly", "webhook:w"])
async def test_unattended_plan_no_card_footer(
    tmp_path: Path, project: Path, gate_calls: list[str], trigger: str
) -> None:
    done = await _plan_run(
        tmp_path,
        options=EngineRunOptions(permission_mode="plan", unattended_trigger=trigger),
    )
    assert done.answer.startswith("1. Count only")
    assert review_card(done.usage) is None
    unattended = done.usage["unattended"]
    assert unattended["trigger"] == trigger
    assert unattended["denied"] == {"plan approval": 1}
    hint = unattended["hint"]
    assert '"auto"' in hint or "Full auto" in hint
    assert "Workspace" not in hint and "antigravity" not in hint


def test_live_approve_is_an_options_change() -> None:
    """With a live plan session, Approve's run (Ask me) differs from the
    spawn options, so the follow-up closes and resumes (#964)."""
    import anyio

    plan = plan_mod.register(
        engine="codex",
        conversation_id=THREAD,
        name="p",
        channel_id=None,
        thread_id=None,
        context=None,
        cwd=None,
        originator=None,
    )
    options = _apply_trigger_overrides(
        _PLAN, plan_mod.approve_context(plan), engine="codex", log=False
    )
    state = gate.AppServerRunState(
        factory=gate.EventFactory("codex"),
        thread_id=THREAD,
        live_mode=True,
        completed_turns=1,
        experimental=True,
        spawn_options=_PLAN,
    )
    gate._RUNS_BY_THREAD[THREAD] = state

    async def steer() -> str:
        return await gate.steer_into_session(
            THREAD, "Implement the plan.", run_options=options
        )

    assert anyio.run(steer) == "options_changed"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        (
            "- Create executable `bye.sh` with `#!/bin/sh`",
            "Create executable bye.sh with #!/bin/sh",
        ),
        ("## Count text files\n- step", "Count text files"),
        ("1. First step", "First step"),
        ("`", "plan"),
    ],
)
def test_plan_name_drops_markdown(text: str, name: str) -> None:
    # Live CX-34: a bullet with a cut code span garbled the card title.
    assert gate._plan_name(text) == name


def test_question_push_names_codex() -> None:
    from untether.runner_bridge import question_notify_text

    assert question_notify_text("codex") == "Question from Codex"
