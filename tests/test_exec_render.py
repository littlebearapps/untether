from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.factories import (
    action_completed,
    action_started,
    session_started,
)
from untether.markdown import (
    HARD_BREAK,
    STATUS,
    MarkdownFormatter,
    action_status,
    assemble_markdown_parts,
    format_action_line,
    format_elapsed,
    format_file_change_title,
    format_verbose_detail,
    inline_code,
    render_event_cli,
    shorten,
    starts_with_pictograph,
)
from untether.model import Action, ActionEvent, ResumeToken, StartedEvent, UntetherEvent
from untether.progress import ProgressTracker
from untether.telegram.render import render_markdown
from untether.utils.paths import reset_run_base_dir, set_run_base_dir


def _format_resume(token) -> str:
    return f"`codex resume {token.value}`"


SAMPLE_EVENTS: list[UntetherEvent] = [
    session_started("codex", "0199a213-81c0-7800-8aa1-bbab2a035a53", title="Codex"),
    action_started("a-1", "command", "bash -lc ls"),
    action_completed(
        "a-1",
        "command",
        "bash -lc ls",
        ok=True,
        detail={"exit_code": 0},
    ),
    action_completed("a-2", "note", "Checking repository root for README", ok=True),
]


def test_render_event_cli_sample_events() -> None:
    out: list[str] = []
    for evt in SAMPLE_EVENTS:
        out.extend(render_event_cli(evt))

    assert out == [
        "codex",
        "▸ `bash -lc ls`",
        "✓ `bash -lc ls`",
        "✓ Checking repository root for README",
    ]


def test_render_event_cli_handles_action_kinds() -> None:
    events: list[UntetherEvent] = [
        action_completed(
            "c-1", "command", "pytest -q", ok=False, detail={"exit_code": 1}
        ),
        action_completed(
            "s-1",
            "web_search",
            "python jsonlines parser handle unknown fields",
            ok=True,
        ),
        action_completed("t-1", "tool", "github.search_issues", ok=True),
        action_completed(
            "f-1",
            "file_change",
            "2 files",
            ok=True,
            detail={
                "changes": [
                    {"path": "README.md", "kind": "add"},
                    {"path": "src/compute_answer.py", "kind": "update"},
                ]
            },
        ),
        action_completed("n-1", "note", "stream error", ok=False),
    ]

    out: list[str] = []
    for evt in events:
        out.extend(render_event_cli(evt))

    assert any(line.startswith("✗ `pytest -q` (exit 1)") for line in out)
    assert any(
        "searched: python jsonlines parser handle unknown fields" in line
        for line in out
    )
    assert any("tool: github.search_issues" in line for line in out)
    assert any(
        "files: add `README.md`, update `src/compute_answer.py`" in line for line in out
    )
    assert any(line.startswith("✗ stream error") for line in out)


def test_file_change_renders_relative_paths_inside_cwd() -> None:
    readme_abs = str(Path.cwd() / "README.md")
    weird_abs = "~" + readme_abs
    out = render_event_cli(
        action_completed(
            "f-abs",
            "file_change",
            "README.md",
            ok=True,
            detail={
                "changes": [
                    {"path": readme_abs, "kind": "update"},
                    {"path": weird_abs, "kind": "update"},
                ]
            },
        )
    )
    assert any(
        f"files: update `README.md`, update `{weird_abs}`" in line for line in out
    )


def test_file_change_renders_change_objects(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    base.mkdir()
    abs_path = str(base / "changelog.md")
    token = set_run_base_dir(base)
    try:
        out = render_event_cli(
            action_completed(
                "f-obj",
                "file_change",
                "ignored",
                ok=True,
                detail={"changes": [SimpleNamespace(path=abs_path, kind="update")]},
            )
        )
    finally:
        reset_run_base_dir(token)
    assert any("files: update `changelog.md`" in line for line in out)


def test_file_change_title_relativizes_absolute_title(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    base.mkdir()
    abs_path = str(base / "changelog.md")
    token = set_run_base_dir(base)
    try:
        out = render_event_cli(
            action_completed("f-abs", "file_change", abs_path, ok=True)
        )
    finally:
        reset_run_base_dir(token)
    assert any("files: `changelog.md`" in line for line in out)


def test_progress_renderer_renders_progress_and_final() -> None:
    tracker = ProgressTracker(engine="codex")
    for evt in SAMPLE_EVENTS:
        tracker.note_event(evt)

    state = tracker.snapshot(resume_formatter=_format_resume)
    formatter = MarkdownFormatter(max_actions=5)
    progress_parts = formatter.render_progress_parts(state, elapsed_s=3.0)
    progress = assemble_markdown_parts(progress_parts)
    assert progress.startswith("working · codex · 3s · step 2")
    assert "✓ `bash -lc ls`" in progress
    assert "`codex resume 0199a213-81c0-7800-8aa1-bbab2a035a53`" in progress

    final_parts = formatter.render_final_parts(
        state, elapsed_s=3.0, status="done", answer="answer"
    )
    final = assemble_markdown_parts(final_parts)
    assert final.startswith("done · codex · 3s · step 2")
    assert "✓ `bash -lc ls`" not in final
    assert "Checking repository root for README" not in final
    assert "answer" in final
    assert final.rstrip().endswith(
        "`codex resume 0199a213-81c0-7800-8aa1-bbab2a035a53`"
    )


def test_progress_renderer_footer_includes_ctx_before_resume() -> None:
    tracker = ProgressTracker(engine="codex")
    for evt in SAMPLE_EVENTS:
        tracker.note_event(evt)

    state = tracker.snapshot(
        resume_formatter=_format_resume,
        context_line="dir: z80 @feat/name",
    )
    formatter = MarkdownFormatter(max_actions=5)
    parts = formatter.render_progress_parts(state, elapsed_s=0.0)
    assert parts.footer == (
        "\N{LABEL} dir: z80 @feat/name"
        f"{HARD_BREAK}"
        f"{HARD_BREAK}\u21a9\ufe0f `codex resume 0199a213-81c0-7800-8aa1-bbab2a035a53`"
    )


def test_progress_renderer_clamps_actions_and_ignores_unknown() -> None:
    tracker = ProgressTracker(engine="codex")
    events = [
        action_completed(
            f"item_{i}",
            "command",
            f"echo {i}",
            ok=True,
            detail={"exit_code": 0},
        )
        for i in range(6)
    ]

    for evt in events:
        assert tracker.note_event(evt) is True

    state = tracker.snapshot()
    formatter = MarkdownFormatter(max_actions=3, command_width=20)
    parts = formatter.render_progress_parts(state, elapsed_s=0.0)
    lines = parts.body.split(HARD_BREAK) if parts.body else []
    assert len(lines) == 3
    assert "echo 3" in lines[0]
    assert "echo 5" in lines[-1]
    mystery = SimpleNamespace(type="mystery")
    assert tracker.note_event(cast(UntetherEvent, mystery)) is False


def test_progress_renderer_renders_commands_in_markdown() -> None:
    tracker = ProgressTracker(engine="codex")
    for i in (30, 31, 32):
        tracker.note_event(
            action_completed(
                f"item_{i}",
                "command",
                f"echo {i}",
                ok=True,
                detail={"exit_code": 0},
            )
        )

    state = tracker.snapshot()
    formatter = MarkdownFormatter(max_actions=5, command_width=None)
    md = assemble_markdown_parts(formatter.render_progress_parts(state, elapsed_s=0.0))
    text, _ = render_markdown(md)
    assert "✓ echo 30" in text
    assert "✓ echo 31" in text
    assert "✓ echo 32" in text


def test_progress_renderer_handles_duplicate_action_ids() -> None:
    tracker = ProgressTracker(engine="codex")
    events = [
        action_started("dup", "command", "echo first"),
        action_completed(
            "dup",
            "command",
            "echo first",
            ok=True,
            detail={"exit_code": 0},
        ),
        action_started("dup", "command", "echo second"),
        action_completed(
            "dup",
            "command",
            "echo second",
            ok=True,
            detail={"exit_code": 0},
        ),
    ]

    for evt in events:
        assert tracker.note_event(evt) is True

    state = tracker.snapshot()
    formatter = MarkdownFormatter(max_actions=5)
    parts = formatter.render_progress_parts(state, elapsed_s=0.0)
    lines = parts.body.split(HARD_BREAK) if parts.body else []
    assert len(lines) == 1
    assert lines[0].startswith("✓ ")
    assert "echo second" in lines[0]


def test_progress_renderer_collapses_action_updates() -> None:
    tracker = ProgressTracker(engine="codex")
    events = [
        action_started("a-1", "command", "echo one"),
        action_started("a-1", "command", "echo two"),
        action_completed(
            "a-1",
            "command",
            "echo two",
            ok=True,
            detail={"exit_code": 0},
        ),
    ]

    for evt in events:
        assert tracker.note_event(evt) is True

    assert tracker.action_count == 1
    state = tracker.snapshot()
    formatter = MarkdownFormatter(max_actions=5)
    parts = formatter.render_progress_parts(state, elapsed_s=0.0)
    lines = parts.body.split(HARD_BREAK) if parts.body else []
    assert len(lines) == 1
    assert lines[0].startswith("✓ ")
    assert "echo two" in lines[0]


def test_progress_renderer_deterministic_output() -> None:
    events = [
        action_started("a-1", "command", "echo ok"),
        action_completed(
            "a-1",
            "command",
            "echo ok",
            ok=True,
            detail={"exit_code": 0},
        ),
    ]
    t1 = ProgressTracker(engine="codex")
    t2 = ProgressTracker(engine="codex")

    for evt in events:
        t1.note_event(evt)
        t2.note_event(evt)

    f1 = MarkdownFormatter(max_actions=5)
    f2 = MarkdownFormatter(max_actions=5)
    assert assemble_markdown_parts(
        f1.render_progress_parts(t1.snapshot(), elapsed_s=1.0)
    ) == assemble_markdown_parts(f2.render_progress_parts(t2.snapshot(), elapsed_s=1.0))


def test_format_elapsed_branches() -> None:
    assert format_elapsed(3661) == "1h 01m"
    assert format_elapsed(61) == "1m 01s"
    assert format_elapsed(1.4) == "1s"


def test_shorten_and_action_status_branches() -> None:
    assert shorten("hello", None) == "hello"
    assert shorten("hello", 0) == ""
    shortened = shorten("hello world", 6)
    assert shortened.endswith("…")
    assert len(shortened) <= 6

    action_ok = Action(id="ok", kind="command", title="x", detail={"exit_code": 0})
    action_fail = Action(id="fail", kind="command", title="x", detail={"exit_code": 2})

    assert action_status(action_ok, completed=False, ok=None) == STATUS["running"]
    assert action_status(action_ok, completed=True, ok=None) == STATUS["done"]
    assert action_status(action_fail, completed=True, ok=None) == STATUS["fail"]


def test_format_file_change_title_handles_overflow_and_invalid() -> None:
    action = Action(
        id="f",
        kind="file_change",
        title="files",
        detail={
            "changes": [
                "bad",
                {"path": ""},
                {"path": "a", "kind": "add"},
                {"path": "b"},
                {"path": "c"},
                {"path": "d"},
            ]
        },
    )
    title = format_file_change_title(action, command_width=200)
    assert title.startswith("files: ")
    assert "…(" in title

    fallback = format_file_change_title(
        Action(id="empty", kind="file_change", title="all files"), command_width=50
    )
    assert fallback == "files: all files"


def test_render_event_cli_ignores_turn_actions() -> None:
    event = ActionEvent(
        engine="codex",
        action=Action(id="turn", kind="turn", title="turn"),
        phase="started",
        ok=None,
    )
    assert render_event_cli(event) == []


def test_progress_renderer_ignores_missing_action_id() -> None:
    tracker = ProgressTracker(engine="codex")
    resume = ResumeToken(engine="codex", value="abc")
    tracker.note_event(StartedEvent(engine="codex", resume=resume, title="Session"))

    event = ActionEvent(
        engine="codex",
        action=Action(id="", kind="command", title="echo"),
        phase="started",
        ok=None,
    )
    assert tracker.note_event(event) is False

    formatter = MarkdownFormatter()
    header = assemble_markdown_parts(
        formatter.render_progress_parts(tracker.snapshot(), elapsed_s=0.0)
    )
    assert header.startswith("working · codex · 0s")


def _ws_title(detail: dict) -> str:
    from untether.markdown import format_action_title

    action = Action(id="w", kind="web_search", title="t", detail=detail)
    return format_action_title(action, command_width=None)


def test_format_action_title_web_search_prefixes() -> None:
    """#419 D3: verb prefix per Codex web-search action type."""
    assert _ws_title({"action_type": "search"}) == "searched: t"
    assert _ws_title({"action_type": "open_page"}) == "opened: t"
    assert _ws_title({"action_type": "find_in_page"}) == "find in page: t"
    assert _ws_title({"action_type": "other"}) == "t"


def test_format_action_title_web_search_claude_unchanged() -> None:
    """Regression: Claude's WebSearch has no action_type → ``searched:``."""
    assert _ws_title({}) == "searched: t"
    assert _ws_title({"query": "t"}) == "searched: t"


# --- #868: emoji-led notes use their own emoji as the status -----------------


@pytest.mark.parametrize(
    "title",
    [
        "⚠️ 7-day limit 79% used — resets Sat 09:00 AEST",
        "⏳ Rate limited — retrying in 30s",
        "🛡️ Safeguards stopped a response",
        "ℹ️ x",
        "↪️ Switched model a → b",
        "🗜️ Context compacted",
    ],
)
def test_868_completed_emoji_note_has_no_done_glyph(title: str) -> None:
    for kind in ("note", "warning"):
        line = format_action_line(
            Action(id="n", kind=kind, title=title),  # type: ignore[arg-type]
            "completed",
            True,
            command_width=300,
        )
        assert line == title


def test_868_completed_plain_note_keeps_done_glyph() -> None:
    title = "reasoning override is not supported for this engine"
    line = format_action_line(
        Action(id="n", kind="note", title=title), "completed", True, command_width=300
    )
    assert line == f"✓ {title}"


def test_868_failed_emoji_note_keeps_fail_glyph() -> None:
    line = format_action_line(
        Action(id="n", kind="note", title="🗜️ Compaction failed"),
        "completed",
        False,
        command_width=300,
    )
    assert line == "✗ 🗜️ Compaction failed"


def test_868_running_emoji_note_keeps_running_glyph() -> None:
    line = format_action_line(
        Action(id="n", kind="note", title="🗜️ Compacting context…"),
        "started",
        None,
        command_width=300,
    )
    assert line == "▸ 🗜️ Compacting context…"


def test_868_completed_command_starting_with_emoji_keeps_glyph() -> None:
    line = format_action_line(
        Action(id="c", kind="command", title="⚠ echo hi"),
        "completed",
        True,
        command_width=300,
    )
    assert line.startswith("✓ ")


@pytest.mark.parametrize(
    "text",
    [
        "⚠️ warn",
        "⚠ warn",
        "⏳ wait",
        "🔁 retry",
        "🛡️ guard",
        "🛡 guard",
        "ℹ️ info",
        "↪️ fallback",
        "↪ fallback",
        "🗜️ compact",
        "✅ ok",
        "🔔 bell",
        "  ⚠️ leading spaces",
        # ✓ U+2713 is category So: a note title starting with ✓ drops the
        # extra glyph rather than doubling it (documented, desirable).
        "✓ already ticked",
        # box drawing is So too — plan 06 relies on this for tree lines
        "├── src",
    ],
)
def test_868_starts_with_pictograph_true(text: str) -> None:
    assert starts_with_pictograph(text) is True


@pytest.mark.parametrize(
    "text",
    ["a", "#1", "`code`", "", "   ", "- item", "1. one", "ℹ bare info (no FE0F)"],
)
def test_868_starts_with_pictograph_false(text: str) -> None:
    assert starts_with_pictograph(text) is False


# --- #871: backticks in agent text never break the action list ---------------


def _code_texts(text: str, entities: list[dict]) -> list[str]:
    raw = text.encode("utf-16-le")
    return [
        raw[e["offset"] * 2 : (e["offset"] + e["length"]) * 2].decode("utf-16-le")
        for e in entities
        if e["type"] == "code"
    ]


def _render_lines(lines: list[str]) -> tuple[str, list[dict]]:
    return render_markdown(HARD_BREAK.join(lines))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("git status", "`git status`"),
        ("echo `date`", "`` echo `date` ``"),
        ("a ``` b", "````a ``` b````"),
        ("`", "`` ` ``"),
        ("x`", "`` x` ``"),
        ("", ""),
        ("a\nb", "`a b`"),
        ("a\r\nb", "`a b`"),
    ],
)
def test_871_inline_code_fence_outruns_inner_runs(text: str, expected: str) -> None:
    assert inline_code(text) == expected


def test_871_inline_code_shortens_before_fencing() -> None:
    text = ("word `tick` " * 40).strip()
    out = inline_code(text, 80)
    fence = out[: len(out) - len(out.lstrip("`"))]
    assert len(fence) == 2  # outruns the single backticks inside
    assert out.endswith(fence)
    inner = out[len(fence) : -len(fence)].strip()
    assert len(inner) <= 80


def test_871_command_titles_with_backticks_render_one_per_line() -> None:
    commands = ['grep "today `pg_migration_head`|X" .', "git status", "git log -1"]
    lines = [
        format_action_line(
            Action(id=str(i), kind="command", title=c),
            "completed",
            True,
            command_width=300,
        )
        for i, c in enumerate(commands)
    ]
    text, entities = _render_lines(lines)
    assert text.splitlines() == [f"✓ {c}" for c in commands]
    assert _code_texts(text, entities) == commands


def test_871_heredoc_pr_body_progress_line() -> None:
    heredoc = (
        "cat > /tmp/pr-body.md <<'EOF'\n"
        "`setup-python-env` moves to `actions/setup-python@v6` (Node 24)\nEOF"
    )
    running = "timeout 590 gh pr checks --watch"
    lines = [
        format_action_line(
            Action(id="1", kind="command", title=heredoc),
            "completed",
            True,
            command_width=300,
        ),
        format_action_line(
            Action(id="2", kind="command", title="sed -i 's/a/b/' x.md"),
            "completed",
            True,
            command_width=300,
        ),
        format_action_line(
            Action(
                id="3",
                kind="command",
                title=running,
                detail={"name": "Bash", "input": {"command": running}},
            ),
            "started",
            None,
            command_width=300,
            elapsed_seconds=210,
        ),
    ]
    text, entities = _render_lines(lines)
    out_lines = text.splitlines()
    assert len(out_lines) == 3
    assert out_lines[2].startswith("▸ timeout 590")
    # #986: the title already shows the whole command, so no repeat.
    assert out_lines[2].endswith(" · 3m 30s")
    codes = _code_texts(text, entities)
    assert all("·" not in c and "▸" not in c and "✓" not in c for c in codes)
    assert "`" not in text.replace("`setup-python-env`", "").replace(
        "`actions/setup-python@v6`", ""
    )


def test_871_long_running_tail_fences_command_detail() -> None:
    cmd = "echo `date` && sleep 300"
    line = format_action_line(
        Action(
            id="1",
            kind="command",
            title=cmd,
            detail={"name": "Bash", "input": {"command": cmd}},
        ),
        "started",
        None,
        # A title cut narrower than the command keeps the tail detail (#986).
        command_width=12,
        elapsed_seconds=210,
    )
    assert line.endswith(" · 3m 30s · ``echo `date` && sleep 300``")
    text, entities = render_markdown(line)
    assert text == f"▸ echo `date`… · 3m 30s · {cmd}"
    assert _code_texts(text, entities) == ["echo `date`…", cmd]


def test_871_long_running_tail_edit_detail_keeps_span_closed() -> None:
    edit = Action(
        id="1",
        kind="file_change",
        title="src/some/deeply/nested/package/directory/module_name.py",
        detail={
            "name": "Edit",
            "input": {
                "file_path": "src/some/deeply/nested/package/directory/module_name.py",
                "old_string": "def foo(bar):\n    return `x` + bar  # long tail text",
            },
        },
    )
    lines = [
        format_action_line(
            edit, "started", None, command_width=300, elapsed_seconds=120
        ),
        format_action_line(
            Action(id="2", kind="command", title="git status"),
            "started",
            None,
            command_width=300,
        ),
        format_action_line(
            Action(id="3", kind="command", title="git diff"),
            "started",
            None,
            command_width=300,
        ),
    ]
    text, entities = _render_lines(lines)
    assert len(text.splitlines()) == 3
    assert all("▸" not in c for c in _code_texts(text, entities))


def test_871_verbose_grep_pattern_with_backtick() -> None:
    action = Action(
        id="1",
        kind="tool",
        title="grep",
        detail={"name": "Grep", "input": {"pattern": "`foo`"}},
    )
    assert format_verbose_detail(action) == "→ `` `foo` ``"


def test_871_verbose_command_line_fenced() -> None:
    from untether.progress import ActionState, ProgressState

    def _state(i: int, title: str) -> ActionState:
        return ActionState(
            action=Action(
                id=str(i),
                kind="command",
                title=title,
                detail={"name": "Bash", "input": {"command": title}},
            ),
            phase="completed",
            ok=True,
            display_phase="completed",
            completed=True,
            first_seen=i,
            last_update=i,
        )

    state = ProgressState(
        engine="claude",
        action_count=2,
        actions=(_state(0, "echo `date`"), _state(1, "git status")),
        resume=None,
        resume_line=None,
        context_line=None,
    )
    formatter = MarkdownFormatter(max_actions=5, verbosity="verbose")
    parts = formatter.render_progress_parts(state, elapsed_s=1.0)
    text, entities = render_markdown(assemble_markdown_parts(parts))
    body = text.splitlines()[1:]
    assert "✓ echo `date`" in body
    assert "echo `date`" in body
    assert "✓ git status" in body
    assert _code_texts(text, entities).count("echo `date`") == 2


def test_871_verbose_line_with_double_backticks_stays_inline() -> None:
    text, entities = render_markdown("  " + inline_code("a `` b"))
    assert [e["type"] for e in entities] == ["code"]


def test_871_file_change_paths_shortened_before_fencing() -> None:
    long = "src/" + "very_long_directory_name/" * 20 + "file.py"
    action = Action(
        id="f",
        kind="file_change",
        title="3 files",
        detail={"changes": [{"path": long, "kind": "update"}] * 3},
    )
    title = format_file_change_title(action, command_width=120)
    text, entities = render_markdown(title)
    assert len(_code_texts(text, entities)) == 3
    assert all(c.endswith("file.py") for c in _code_texts(text, entities))


def test_871_codex_command_with_backtick_renders_one_line() -> None:
    import json as _json

    from untether.events import EventFactory
    from untether.runners.codex import translate_codex_event
    from untether.schemas import codex as codex_schema

    evt = {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": 'echo "a `b` c"',
            "aggregated_output": "",
            "exit_code": 0,
            "status": "completed",
        },
    }
    out = translate_codex_event(
        codex_schema.decode_event(_json.dumps(evt)),
        title="Codex",
        factory=EventFactory("codex"),
    )
    lines = [
        format_action_line(e.action, "completed", e.ok, command_width=300) for e in out
    ] + ["✓ `ls`"]
    text, entities = _render_lines(lines)
    assert len(text.splitlines()) == 2
    assert 'echo "a `b` c"' in _code_texts(text, entities)
