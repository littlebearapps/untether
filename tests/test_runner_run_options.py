from untether.model import ResumeToken
from untether.runners.claude import ClaudeRunner
from untether.runners.codex import CodexRunner
from untether.runners.opencode import OpenCodeRunner, OpenCodeStreamState
from untether.runners.pi import ENGINE as PI_ENGINE
from untether.runners.pi import PiRunner, PiStreamState
from untether.runners.run_options import (
    CLAUDE_PLAN_AUTO_MODE,
    EngineRunOptions,
    apply_run_options,
)


def test_codex_run_options_override_model_and_reasoning() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=["-c", "notify=[]"])
    state = runner.new_state("hi", None)
    with apply_run_options(EngineRunOptions(model="gpt-4.1-mini", reasoning="low")):
        args = runner.build_args("hi", None, state=state)

    assert args == [
        "-c",
        "notify=[]",
        "--model",
        "gpt-4.1-mini",
        "-c",
        "model_reasoning_effort=low",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "-",
    ]


def test_codex_run_options_place_images_for_new_and_resumed_sessions() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    options = EngineRunOptions(image_paths=("incoming/one.jpg", "incoming/two.png"))

    with apply_run_options(options):
        new_args = runner.build_args("inspect", None, state=None)
        resumed_args = runner.build_args(
            "inspect",
            ResumeToken(engine="codex", value="session-123"),
            state=None,
        )

    assert new_args == [
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "--image",
        "incoming/one.jpg",
        "--image",
        "incoming/two.png",
        "-",
    ]
    assert resumed_args == [
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "resume",
        "--image",
        "incoming/one.jpg",
        "--image",
        "incoming/two.png",
        "session-123",
        "-",
    ]


def test_codex_run_options_place_images_for_continue_session() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    options = EngineRunOptions(image_paths=("incoming/continued.png",))

    with apply_run_options(options):
        args = runner.build_args(
            "inspect",
            ResumeToken(engine="codex", value="", is_continue=True),
            state=None,
        )

    assert args == [
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "resume",
        "--image",
        "incoming/continued.png",
        "--last",
        "-",
    ]


def test_claude_run_options_override_model() -> None:
    runner = ClaudeRunner(claude_cmd="claude", model="claude-sonnet")
    with apply_run_options(EngineRunOptions(model="claude-opus")):
        args = runner.build_args("hi", None, state=None)

    assert "--model" in args
    model_idx = args.index("--model") + 1
    assert args[model_idx] == "claude-opus"


def test_opencode_run_options_override_model() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode", model="claude-sonnet")
    state = OpenCodeStreamState()
    with apply_run_options(EngineRunOptions(model="gpt-4o-mini")):
        args = runner.build_args("hi", None, state=state)

    assert "--model" in args
    model_idx = args.index("--model") + 1
    assert args[model_idx] == "gpt-4o-mini"


def test_pi_run_options_override_model() -> None:
    runner = PiRunner(extra_args=[], model="pi-default", provider=None)
    state = PiStreamState(resume=ResumeToken(engine=PI_ENGINE, value="sess.jsonl"))
    with apply_run_options(EngineRunOptions(model="pi-override")):
        args = runner.build_args("hi", None, state=state)

    assert "--model" in args
    model_idx = args.index("--model") + 1
    assert args[model_idx] == "pi-override"


def test_claude_auto_mode_passes_auto_to_cli() -> None:
    """#741 permission_mode 'auto' now reaches the CLI verbatim.

    Until 0.35.5rc8 it was rewritten to 'plan', which shadowed Claude Code's
    own classifier-gated auto mode and made it unreachable.
    """
    runner = ClaudeRunner(claude_cmd="claude", permission_mode="auto")
    args = runner.build_args("hi", None, state=None)

    assert "--permission-mode" in args
    mode_idx = args.index("--permission-mode") + 1
    assert args[mode_idx] == "auto"


def test_claude_plan_auto_mode_passes_plan_to_cli() -> None:
    """Untether's renamed sugar still starts the CLI in plan mode (#741)."""
    runner = ClaudeRunner(claude_cmd="claude", permission_mode=CLAUDE_PLAN_AUTO_MODE)
    args = runner.build_args("hi", None, state=None)

    assert "--permission-mode" in args
    mode_idx = args.index("--permission-mode") + 1
    assert args[mode_idx] == "plan"
