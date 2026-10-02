"""Image arguments preserve upstream safe-mode and diagnostic options."""

import pytest

from untether.model import ResumeToken
from untether.runners.codex import CODEX_SAFE_PERMISSION_MODE, CodexRunner
from untether.runners.run_options import (
    EngineRunOptions,
    apply_run_options,
)


@pytest.mark.parametrize(
    "resume",
    [
        None,
        ResumeToken(engine="codex", value="session-123"),
        ResumeToken(engine="codex", value="", is_continue=True),
    ],
)
def test_safe_mode_with_images_preserves_sandbox_and_argv(resume) -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("inspect", resume)
    options = EngineRunOptions(
        permission_mode=CODEX_SAFE_PERMISSION_MODE,
        image_paths=("incoming/image.png",),
        ignored_reasoning="unsupported-level",
    )
    with apply_run_options(options):
        args = runner.build_args("inspect", resume, state=state)
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--image") + 1] == "incoming/image.png"
    assert state.argv == args
    if resume is not None:
        assert args.index("--sandbox") < args.index("resume") < args.index("--image")
        assert args[-2:] == ["--last" if resume.is_continue else resume.value, "-"]
    else:
        assert "resume" not in args
        assert args[-1] == "-"
