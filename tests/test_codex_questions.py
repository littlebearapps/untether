"""Codex question buttons on the app-server transport (#965, phase 965-3).

In Plan mode Codex asks with the server request ``item/tool/requestUserInput``
(1-3 questions, up to 4 options each, ``options`` may be null). An attended
run with Ask mode on defers it to the shared ask keyboard (options + "Other
(type reply)"); the answer goes back as ``{answers: {<id>: {answers: [..]}}}``.
Cron/webhook runs and Ask mode off answer ``{answers: {}}`` (Codex's own
"no answers: continue with best judgement" fallback). ``isSecret`` answers
are never echoed, logged or shown on the progress row (D5).

Frame: ``tests/fixtures/codex_app_server/p7-question-frames-0.171.0.jsonl``
(P7 on codex-cli 0.171.0: ``thread/status/changed`` waiting, the request, and
``serverRequest/resolved``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests.test_codex_app_server_runner import THREAD, Fake, _completed, _runner
from untether.interactive import registry
from untether.interactive.questions import (
    _ASK_QUESTION_FLOWS,
    _PENDING_ASK_REQUESTS,
    answer_ask_question,
    answer_ask_question_with_options,
    ask_flow_is_secret,
    get_ask_question_flow_by_id,
)
from untether.model import ActionEvent
from untether.runners import codex_app_server as gate
from untether.runners.codex_rpc import CodexRpcPeer
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.engine_overrides import (
    ask_questions_supported,
    diff_preview_supported,
)
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

USER_INPUT = "item/tool/requestUserInput"
PLAN = EngineRunOptions(permission_mode="plan")
_FRAMES = [
    json.loads(line)
    for line in (
        Path(__file__).parent
        / "fixtures"
        / "codex_app_server"
        / "p7-question-frames-0.171.0.jsonl"
    )
    .read_text()
    .splitlines()
]


@pytest.fixture
def project(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture(autouse=True)
def _supported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(cmd: str, resume: Any) -> None:
        return None

    monkeypatch.setattr(gate, "app_server_unsupported_event", supported)
    gate._PLAN_THREADS.clear()
    yield
    gate._PLAN_THREADS.clear()


def _p7_request(rid: int = 0) -> dict[str, Any]:
    frame = copy.deepcopy(next(f for f in _FRAMES if f.get("method") == USER_INPUT))
    frame["id"] = rid
    frame["params"]["threadId"] = THREAD
    frame["params"]["turnId"] = "turn-1"
    return frame


def _request(questions: list[dict[str, Any]], rid: int = 0) -> dict[str, Any]:
    frame = _p7_request(rid)
    frame["params"]["questions"] = questions
    return frame


def _question(qid: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": qid,
        "header": extra.pop("header", ""),
        "question": text,
        "isOther": True,
        "isSecret": extra.pop("isSecret", False),
        "options": extra.pop("options", [{"label": "Yes", "description": "y"}]),
    }


def _cards(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "started"
        and e.action.detail.get("ask_flow")
    ]


def _closed(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.id.startswith("codex.question.")
    ]


def _labels(card: ActionEvent) -> list[str]:
    return [
        b["text"]
        for row in card.action.detail["inline_keyboard"]["buttons"]
        for b in row
    ]


def _replies(fake: Fake, rid: int) -> list[dict]:
    return [m for m in fake.received() if m.get("id") == rid and "method" not in m]


async def _tap(request_id: str, *indexes: int) -> bool:
    """What the ``aq:opt`` handler does: one option per question, in order."""
    flow = get_ask_question_flow_by_id(request_id)
    assert flow is not None
    for index in indexes:
        q = flow.questions[flow.current_index]
        flow.answers[q["question"]] = q["options"][index]["label"]
        flow.current_index += 1
    return await answer_ask_question_with_options(request_id)


async def _drive(
    fake: Fake,
    on_card: Callable[[str], Awaitable[Any]] | None,
    options: EngineRunOptions | None = PLAN,
) -> list[Any]:
    events: list[Any] = []
    with apply_run_options(options), anyio.fail_after(20):
        async for evt in _runner(fake).run("plan it", None):
            events.append(evt)
            if on_card is not None and evt in _cards([evt]):
                await on_card(evt.action.detail["request_id"])
    return events


# --- the card ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_p7_question_renders_buttons(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _p7_request(0)}])
    seen: list[str] = []

    async def on_card(rid: str) -> None:
        seen.append(rid)
        flow = get_ask_question_flow_by_id(rid)
        assert flow is not None and flow.channel_id == 0
        assert _PENDING_ASK_REQUESTS[rid][1] == flow.questions[0]["question"]
        assert registry._REQUEST_META[rid].kind == "ask"
        await _tap(rid, 0)

    with capture_logs() as logs:
        events = await _drive(fake, on_card)
    (card,) = _cards(events)
    assert card.action.title == (
        "❓ Files: Should I count all files in this directory, or only the .txt files?"
    )
    assert _labels(card) == [
        "All files (Recommended)",
        "Only .txt files",
        "Other (type reply)",
    ]
    data = card.action.detail["inline_keyboard"]["buttons"][0][0]["callback_data"]
    assert data == f"aq:opt:0:{seen[0]}"
    shown = [e for e in logs if e["event"] == "codex.app_server.question"]
    assert shown[0]["questions"] == 1 and shown[0]["secret"] is False
    assert _completed(events).ok


@pytest.mark.anyio
async def test_option_tap_answers_by_id(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _p7_request(4)}])
    events = await _drive(fake, lambda rid: _tap(rid, 1))
    assert _replies(fake, 4) == [
        {
            "id": 4,
            "result": {"answers": {"file_scope": {"answers": ["Only .txt files"]}}},
        }
    ]
    (closed,) = _closed(events)
    assert closed.action.title == "❓ answered"
    assert closed.ok
    assert closed.action.id == _cards(events)[0].action.id


@pytest.mark.anyio
async def test_other_free_text_answer(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _p7_request(2)}])

    async def on_card(rid: str) -> None:
        assert await answer_ask_question(rid, "Count only the .txt files")

    with capture_logs() as logs:
        await _drive(fake, on_card)
    assert _replies(fake, 2) == [
        {
            "id": 2,
            "result": {
                "answers": {"file_scope": {"answers": ["Count only the .txt files"]}}
            },
        }
    ]
    answered = [e for e in logs if e["event"] == "codex.app_server.question.answered"]
    assert answered[0]["answer_len"] == len("Count only the .txt files")
    assert "Count only" not in str(logs)


@pytest.mark.anyio
async def test_three_questions_in_turn(tmp_path: Path, project: Path) -> None:
    questions = [
        _question("a", "Which file?", options=[{"label": "x.txt"}, {"label": "y.txt"}]),
        _question("b", "Which file?", options=[{"label": "z.txt"}]),
        _question(
            "c",
            "How many?",
            options=[{"label": str(n), "description": None} for n in range(6)],
        ),
        _question("d", "Never shown?"),
    ]
    fake = Fake(tmp_path, approvals=[{"request": _request(questions, 7)}])
    shapes: list[Any] = []

    async def on_card(rid: str) -> None:
        flow = get_ask_question_flow_by_id(rid)
        assert flow is not None
        shapes.append(
            [(q["question"], q["id"], len(q["options"])) for q in flow.questions]
        )
        await _tap(rid, 1, 0, 3)

    events = await _drive(fake, on_card)
    # Capped at 3 questions and 4 options; repeated texts made unique.
    assert shapes == [
        [("Which file?", "a", 2), ("Which file? (2)", "b", 1), ("How many?", "c", 4)]
    ]
    assert _cards(events)[0].action.title == "❓ Question 1 of 3: Which file?"
    assert _replies(fake, 7)[0]["result"] == {
        "answers": {
            "a": {"answers": ["y.txt"]},
            "b": {"answers": ["z.txt"]},
            "c": {"answers": ["3"]},
        }
    }


@pytest.mark.anyio
async def test_null_options_text_only(tmp_path: Path, project: Path) -> None:
    fake = Fake(
        tmp_path,
        approvals=[{"request": _request([_question("n", "Name?", options=None)], 3)}],
    )
    events = await _drive(fake, lambda rid: answer_ask_question(rid, "Bluey"))
    assert _labels(_cards(events)[0]) == ["Other (type reply)"]
    assert _replies(fake, 3)[0]["result"] == {"answers": {"n": {"answers": ["Bluey"]}}}


@pytest.mark.anyio
async def test_typed_answer_keeps_earlier_option_answers(
    tmp_path: Path, project: Path
) -> None:
    questions = [_question("a", "First?"), _question("b", "Second?")]
    fake = Fake(tmp_path, approvals=[{"request": _request(questions, 1)}])

    async def on_card(rid: str) -> None:
        flow = get_ask_question_flow_by_id(rid)
        assert flow is not None
        flow.answers["First?"] = "Yes"
        flow.current_index = 1
        assert await answer_ask_question(rid, "typed")

    await _drive(fake, on_card)
    assert _replies(fake, 1)[0]["result"] == {
        "answers": {"a": {"answers": ["Yes"]}, "b": {"answers": ["typed"]}}
    }


# --- isSecret (D5) ------------------------------------------------------------------


@pytest.mark.anyio
async def test_secret_answer_not_echoed_or_logged(
    tmp_path: Path, project: Path
) -> None:
    secret = "hunter2-do-not-log"
    fake = Fake(
        tmp_path,
        approvals=[
            {"request": _request([_question("pw", "Password?", isSecret=True)], 5)}
        ],
    )
    rids: list[str] = []

    async def on_card(rid: str) -> None:
        rids.append(rid)
        assert ask_flow_is_secret(rid)
        assert await answer_ask_question(rid, secret)
        # Dropped right after the reply: nothing holds the answer.
        assert rid not in _ASK_QUESTION_FLOWS
        assert rid not in registry._REQUEST_TO_INPUT

    with capture_logs() as logs:
        events = await _drive(fake, on_card)
    assert _replies(fake, 5)[0]["result"] == {"answers": {"pw": {"answers": [secret]}}}
    assert secret not in str(logs)
    assert not any(
        secret in (e.action.title or "") for e in events if isinstance(e, ActionEvent)
    )
    (closed,) = _closed(events)
    assert closed.action.title == "❓ answered (hidden)"
    assert not ask_flow_is_secret(rids[0])


@pytest.mark.anyio
@pytest.mark.parametrize("other", [False, True], ids=["typed", "other-button"])
async def test_secret_typed_reply_is_not_echoed_in_chat(
    monkeypatch: pytest.MonkeyPatch, other: bool
) -> None:
    from tests.test_telegram_bridge import (
        CODEX_ENGINE,
        Return,
        ScriptRunner,
        TelegramBridgeConfig,
        _388_loop_cfg,
        _388_msg,
        run_main_loop,
    )
    from untether.interactive.questions import AskQuestionState

    secret = "hunter2-chat"
    rid = "cx_secretloop"
    sent: list[str] = []

    async def _answer(request_id: str, text: str) -> bool:
        sent.append(text)
        return True

    async def _answer_flow(request_id: str) -> bool:
        flow = _ASK_QUESTION_FLOWS.pop(request_id)
        sent.extend(flow.answers.values())
        return True

    monkeypatch.setattr("untether.interactive.questions.answer_ask_question", _answer)
    monkeypatch.setattr(
        "untether.interactive.questions.answer_ask_question_with_options",
        _answer_flow,
    )
    question = {"question": "Token?", "options": [], "id": "t", "secret": True}
    _ASK_QUESTION_FLOWS[rid] = AskQuestionState(
        request_id=rid, channel_id=123, questions=[question], awaiting_text=other
    )
    _PENDING_ASK_REQUESTS[rid] = (123, "Token?")
    registry._REQUEST_TO_SESSION[rid] = "codex:x"
    runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
    cfg, transport = _388_loop_cfg(runner, originator_only=False)

    async def poller(_cfg: TelegramBridgeConfig):
        yield _388_msg(secret, 2)

    try:
        with capture_logs() as logs:
            await run_main_loop(cfg, poller)
    finally:
        _ASK_QUESTION_FLOWS.pop(rid, None)
        _PENDING_ASK_REQUESTS.pop(rid, None)
        registry._REQUEST_TO_SESSION.pop(rid, None)

    assert sent == [secret]
    texts = [c["message"].text for c in transport.send_calls]
    assert texts == ["Answer sent to Codex."]
    assert secret not in str(logs)
    assert runner.calls == []


@pytest.mark.anyio
async def test_secret_typed_answer_hidden_in_the_tap_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret typed earlier in a flow whose last question is tapped."""
    from tests.test_ask_user_question import CHAT_A, _make_command_ctx
    from untether.interactive.questions import AskQuestionState
    from untether.telegram.commands import ask_question as cmd_mod

    rid = "cx_secretsum"
    _ASK_QUESTION_FLOWS[rid] = AskQuestionState(
        request_id=rid,
        channel_id=CHAT_A,
        questions=[
            {"question": "Token?", "options": [], "secret": True},
            {"question": "Region?", "options": [{"label": "syd"}]},
        ],
        current_index=1,
        answers={"Token?": "hunter2-summary"},
    )

    async def _answer(request_id: str) -> bool:
        _ASK_QUESTION_FLOWS.pop(request_id, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options", _answer
    )
    try:
        result = await cmd_mod.AskQuestionCommand().handle(
            _make_command_ctx(f"opt:0:{rid}")
        )
    finally:
        _ASK_QUESTION_FLOWS.pop(rid, None)
    assert result is not None
    assert "hunter2-summary" not in result.text
    assert "A: (hidden)" in result.text and "A: syd" in result.text


# --- withdrawal and run end ---------------------------------------------------------


@pytest.mark.anyio
async def test_withdrawn_by_server_request_resolved(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _p7_request(6), "withdraw": True}])
    with capture_logs() as logs:
        events = await _drive(fake, None)
    rid = _cards(events)[0].action.detail["request_id"]
    (closed,) = _closed(events)
    assert closed.action.title == "❓ question withdrawn — Codex stopped waiting"
    assert not closed.ok
    assert rid not in _ASK_QUESTION_FLOWS
    assert rid not in _PENDING_ASK_REQUESTS
    assert any(e["event"] == "codex.app_server.question.withdrawn" for e in logs)
    assert not await answer_ask_question(rid, "late")
    assert _replies(fake, 6) == []


@pytest.mark.anyio
async def test_question_card_retired_on_turn_end(tmp_path: Path, project: Path) -> None:
    # The request arrives and the turn completes without an answer.
    fake = Fake(tmp_path, notifications=[_p7_request(8)])
    events = await _drive(fake, None)
    rid = _cards(events)[0].action.detail["request_id"]
    (closed,) = _closed(events)
    assert closed.action.title == "❓ question withdrawn — Codex stopped waiting"
    assert rid not in _ASK_QUESTION_FLOWS
    assert _completed(events).ok


@pytest.mark.anyio
async def test_answer_after_run_end_is_refused(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, notifications=[_p7_request(9)])
    events = await _drive(fake, None)
    rid = _cards(events)[0].action.detail["request_id"]
    assert rid not in registry._REQUEST_TO_SESSION
    assert not await answer_ask_question(rid, "too late")
    assert _replies(fake, 9) == []


# --- nobody to ask ------------------------------------------------------------------


@pytest.mark.anyio
async def test_unattended_answers_empty_with_row(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, notifications=[_p7_request(1)])
    options = EngineRunOptions(
        permission_mode="plan", unattended_trigger="cron:nightly"
    )
    with capture_logs() as logs:
        events = await _drive(fake, None, options=options)
    assert not _cards(events)
    assert _replies(fake, 1) == [{"id": 1, "result": {"answers": {}}}]
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.id for r in rows] == ["codex.unattended_deny"]
    assert "question" in rows[0].action.title
    denials = [e for e in logs if e["event"] == "permission.unattended_deny"]
    assert denials[0]["tool_name"] == "question"
    assert (_completed(events).usage or {})["unattended"]["denied"] == {"question": 1}


@pytest.mark.anyio
async def test_questions_off_answers_empty(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, notifications=[_p7_request(2)])
    options = EngineRunOptions(permission_mode="plan", ask_questions=False)
    events = await _drive(fake, None, options=options)
    assert not _cards(events)
    assert _replies(fake, 2) == [{"id": 2, "result": {"answers": {}}}]
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.id for r in rows] == ["codex.question_off"]
    assert rows[0].action.title.startswith("❓ Codex asked a question")
    assert rows[0].level == "info"
    assert _completed(events).ok


@pytest.mark.anyio
async def test_peer_default_decline_is_empty_answers() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    peer = CodexRpcPeer(send)
    line = json.dumps({"id": 3, "method": USER_INPUT, "params": {}}).encode()
    assert await peer.feed(line) is None
    assert [json.loads(s) for s in sent] == [{"id": 3, "result": {"answers": {}}}]


# --- gating -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ask_supported_only_on_app_server(tmp_path: Path) -> None:
    from tests.test_config_command import (
        _app_server,
        _buttons_data,
        _last_send_msg,
        _make_ctx,
    )
    from untether.telegram.commands.config import ConfigCommand

    assert ask_questions_supported("claude")
    assert not ask_questions_supported("codex")
    assert not ask_questions_supported("codex", transport="exec")
    assert ask_questions_supported("codex", transport="app-server")
    # Diff preview follows the same transport rule (#963).
    assert diff_preview_supported("codex", transport="app-server")
    assert not diff_preview_supported("codex", transport="exec")

    ctx = _make_ctx(config_path=tmp_path / "prefs.json", default_engine="codex")
    await ConfigCommand().handle(ctx)
    msg = _last_send_msg(ctx)
    assert "config:aq" not in _buttons_data(msg)
    assert "Questions (plan mode only)" not in msg.text

    ctx = _app_server(
        _make_ctx(config_path=tmp_path / "prefs.json", default_engine="codex")
    )
    await ConfigCommand().handle(ctx)
    msg = _last_send_msg(ctx)
    assert "config:aq" in _buttons_data(msg)
    assert "Questions (plan mode only)" in msg.text

    page = _app_server(
        _make_ctx(
            args_text="aq",
            text="config:aq",
            config_path=tmp_path / "prefs.json",
            default_engine="codex",
        )
    )
    await ConfigCommand().handle(page)
    from tests.test_config_command import _last_edit_msg

    edited = _last_edit_msg(page)
    assert "config:aq:off" in _buttons_data(edited)
    assert "plan mode" in edited.text
