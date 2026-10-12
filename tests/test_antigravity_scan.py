"""Antigravity planted-config scan: agent and manifest layouts it follows
(#1043).

Each coverage test has the same shape: build the layout, scan, change the
script (or watched file) the layout points at, scan again — the digest must
differ. The scanner's other pinned behaviours (JSON oddities, links, FIFOs,
budgets) live in ``test_antigravity_permissions.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from untether.utils import antigravity_agent as agent
from untether.utils import antigravity_scan as scan


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``$HOME`` is the test folder, so nothing above it is ever looked at."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def gemini_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    monkeypatch.setattr(scan, "user_config_dir", lambda: config)
    return config


@pytest.fixture
def proj(tmp_path: Path, gemini_home: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    return project


def _write(path: Path, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _changes_digest(cwd: Path, target: Path, text: str = "echo 2") -> bool:
    """Scan, rewrite *target*, scan again: did the agy digest move?"""
    first = scan.scan_workspace_config(cwd)
    assert first.unchecked_reason is None, first.unchecked_reason
    target.write_text(text)
    return scan.scan_workspace_config(cwd).agy_digest != first.agy_digest


# -- 1. nested agent layout ---------------------------------------------------


@pytest.mark.parametrize(
    "agent_file",
    ["agents/helper/agent.md", "agents/team/helper/agent.md", "agents/helper.md"],
)
def test_nested_agent_front_matter_is_followed(agent_file: str, proj: Path) -> None:
    """agy's ``agents/<name>/agent.md`` layout is read like the flat one."""
    _write(
        proj / ".agents" / agent_file, "---\nname: helper\nplugins: vendor/plug\n---\n"
    )
    _write(
        proj / "vendor" / "plug" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./run.sh"}]}),
    )
    run = _write(proj / "vendor" / "plug" / "run.sh", "echo 1")
    assert _changes_digest(proj, run)


def test_nested_agent_in_a_followed_plugin_folder_is_parsed(proj: Path) -> None:
    _write(
        proj / ".agents" / "plugins" / "p" / "agents" / "helper" / "agent.md",
        "---\nhooks: ../../../../../tools/hooks.json\n---\n",
    )
    _write(
        proj / "tools" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./lib/run.sh"}]}),
    )
    run = _write(proj / "tools" / "lib" / "run.sh", "echo 1")
    assert _changes_digest(proj, run)


def test_markdown_outside_an_agents_folder_is_not_a_manifest(proj: Path) -> None:
    """A plugin README isn't an agent: its front matter is never parsed, so
    it can't make the project "not checked"."""
    _write(
        proj / ".agents" / "plugins" / "p" / "README.md",
        "---\nhooks: &a ./x\n---\n",
    )
    assert scan.scan_workspace_config(proj).unchecked_reason is None


def test_user_level_nested_agent_is_tracked_stat_only(
    proj: Path, gemini_home: Path
) -> None:
    _write(proj / ".agents" / "hooks.json", '{"Stop": []}')
    nested = _write(
        gemini_home / "agents" / "mine" / "agent.md", "---\nname: mine\n---\n"
    )
    first = scan.scan_workspace_config(proj)
    assert "~/.gemini/config/agents/mine/agent.md" in first.agy
    nested.write_text("---\nname: mine\nhooks: ./h.json\n---\n")
    assert scan.scan_workspace_config(proj).agy_digest != first.agy_digest


def test_untethers_own_agent_file_is_never_in_the_digest(
    proj: Path, gemini_home: Path
) -> None:
    """Installing or rewriting the Workspace agent must not hold a cron or
    raise a ⚠️ row: Untether puts that exact text there before every use."""
    managed = agent.agent_path().relative_to(scan.user_config_dir()).as_posix()
    assert managed == scan._MANAGED_AGENT
    _write(proj / ".agents" / "hooks.json", '{"Stop": []}')
    before = scan.scan_workspace_config(proj)
    assert agent.ensure_workspace_agent() is True
    after = scan.scan_workspace_config(proj)
    assert after.agy_digest == before.agy_digest
    assert not any("untether-workspace" in key for key in after.agy)
    # A stale copy from an older Untether is rewritten on the next Workspace
    # run; it must not hold the cron that would never get that far.
    agent.agent_path().write_text("---\nname: untether-workspace\n---\nold\n")
    assert scan.scan_workspace_config(proj).agy_digest == before.agy_digest


def test_only_the_exact_managed_file_is_exempt(proj: Path, gemini_home: Path) -> None:
    assert agent.ensure_workspace_agent() is True
    folder = agent.agent_path().parent
    _write(folder / "other.md", "---\nname: x\n---\n")
    _write(folder / "sub" / "agent.md", "---\nname: y\n---\n")
    _write(folder.parent / "Untether-Workspace2" / "agent.md", "---\nname: z\n---\n")
    keys = set(scan.scan_workspace_config(proj).agy)
    prefix = "~/.gemini/config/agents/"
    assert keys == {
        f"{prefix}untether-workspace/other.md",
        f"{prefix}untether-workspace/sub/agent.md",
        f"{prefix}Untether-Workspace2/agent.md",
    }
    # A link where the managed file should be is still recorded.
    agent.agent_path().unlink()
    os.symlink(folder / "other.md", agent.agent_path())
    assert (
        f"{prefix}untether-workspace/agent.md" in scan.scan_workspace_config(proj).agy
    )


# -- 2. bare folder names in declarative manifests ---------------------------


@pytest.mark.parametrize("value", ["vendor-plugins", "./vendor-plugins"])
@pytest.mark.parametrize(
    "manifest",
    [
        ".agents/plugins.json",  # a listing: every string names a folder
        ".agents/plugins/p/plugin.json",  # directly under a path key
    ],
)
def test_bare_folder_name_under_a_path_key_is_walked(
    manifest: str, value: str, proj: Path
) -> None:
    """``"tools"`` is followed wherever ``"./tools"`` is: beside the
    manifest, at the run's cwd and at the project root."""
    _write(proj / manifest, json.dumps({"plugins": [value]}))
    _write(
        proj / "vendor-plugins" / "p" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./run.sh"}]}),
    )
    run = _write(proj / "vendor-plugins" / "p" / "run.sh", "echo 1")
    assert _changes_digest(proj, run)


def test_bare_folder_name_beside_a_referenced_manifest_is_walked(proj: Path) -> None:
    """Any bare string is tried beside its own manifest (free when that
    folder is already walked; it isn't for a manifest named by path)."""
    _write(proj / ".agents" / "plugins.json", json.dumps(["../tools/plugin.json"]))
    _write(proj / "tools" / "plugin.json", json.dumps({"entry": "lib"}))
    _write(
        proj / "tools" / "lib" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./run.sh"}]}),
    )
    run = _write(proj / "tools" / "lib" / "run.sh", "echo 1")
    assert _changes_digest(proj, run)


def test_bare_name_elsewhere_does_not_pull_in_a_project_folder(proj: Path) -> None:
    """A plugin called ``docs`` must not put the project's ``docs/`` in the
    digest (every docs edit would hold the crons); nor does a hook manifest,
    whose strings are commands."""
    _write(
        proj / ".agents" / "plugins" / "p" / "plugin.json",
        json.dumps(
            {"name": "docs", "keywords": ["docs"], "meta": {"hooks": {"x": "docs"}}}
        ),
    )
    _write(proj / ".agents" / "hooks.json", json.dumps({"hooks": "docs"}))
    page = _write(proj / "docs" / "index.md", "# 1")
    assert not _changes_digest(proj, page, "# 2")
    assert not any(k.startswith("docs/") for k in scan.scan_workspace_config(proj).agy)


def test_bare_names_do_not_spend_the_entry_budget(proj: Path) -> None:
    """A manifest full of plain words (names, keywords) costs nothing: a
    bare name is charged only when something by that name exists."""
    words = [f"word{i}" for i in range(scan.MAX_FILES + 500)]
    _write(proj / ".agents" / "plugins.json", json.dumps({"plugins": words}))
    _write(proj / ".agents" / "plugins" / "p" / "plugin.json", json.dumps(words))
    result = scan.scan_workspace_config(proj)
    assert result.truncated is False
    assert result.unchecked_reason is None


# -- 3. front matter the line reader can't vouch for --------------------------


@pytest.mark.parametrize(
    "front",
    [
        '---\n"hooks": ../../tools\n---\n',  # quoted key
        "---\n'plugins': ../../tools\n---\n",
        "---\n{hooks: ../../tools}\n---\n",  # flow mapping
        "---\nextra: {hooks: ../../tools}\n---\n",
        "---\nhooks: &h ../../tools\n---\n",  # anchor
        "---\nbase: &b ../../tools\nhooks: *b\n---\n",  # alias
        "---\nhooks: !!str ../../tools\n---\n",  # tag
        "---\nhooks: {dir: ../../tools}\n---\n",
        "---\noptions:\n  hooks: ../../tools\n---\n",  # nested under another key
        "---\n? hooks\n: ../../tools\n---\n",  # explicit key
        '---\nhooks: "../../tools" # the "real" one\n---\n',  # comment + quotes
    ],
)
def test_front_matter_shape_the_scan_cannot_read_is_unchecked(
    front: str, proj: Path
) -> None:
    _write(proj / ".agents" / "agents" / "helper.md", front)
    _write(
        proj / "tools" / "hooks.json", json.dumps({"Stop": [{"command": "sh ./r.sh"}]})
    )
    _write(proj / "tools" / "r.sh", "echo 1")
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.AMBIGUOUS


@pytest.mark.parametrize(
    "front",
    [
        "---\nhooks: ../../tools # where the hooks live\n---\n",  # trailing comment
        "---\nhooks:\n\n  - ../../tools\n---\n",  # blank line in a block list
        "---\nhooks:\n# the hooks\n  - ../../tools\n---\n",  # comment line
        "---\nhooks:\n  # the hooks\n  - '../../tools'\n---\n",
        "---\nhooks: >-\n  ../../tools\n---\n",  # block scalars read line by line
    ],
)
def test_front_matter_plain_yaml_shapes_are_followed(front: str, proj: Path) -> None:
    _write(proj / ".agents" / "agents" / "helper.md", front)
    _write(
        proj / "tools" / "hooks.json", json.dumps({"Stop": [{"command": "sh ./r.sh"}]})
    )
    run = _write(proj / "tools" / "r.sh", "echo 1")
    assert _changes_digest(proj, run)


@pytest.mark.parametrize(
    "front",
    [
        # Inline hooks: commands may use pipes, globs-in-quotes and matchers.
        '---\nhooks:\n  Stop:\n    - matcher: "*"\n      command: cat x | wc -l > out\n---\n',
        "---\nname: helper\ndescription: Runs after hooks and plugins finish\n---\n",
        "---\nname: helper\ntools:\n  - view_file\n---\n# Body\nhooks: {x}\n",
    ],
)
def test_ordinary_agent_front_matter_stays_checked(front: str, proj: Path) -> None:
    _write(proj / ".agents" / "agents" / "helper" / "agent.md", front)
    assert scan.scan_workspace_config(proj).unchecked_reason is None


# -- 4. project-root detection -------------------------------------------------


def _repo_with_sub(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(
        repo / ".agents" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./h.sh"}]}),
    )
    return repo, sub, _write(repo / "h.sh", "echo 1")


@pytest.mark.parametrize("planted", ["empty-file", "junk-file", "directory", "gitdir"])
def test_planted_git_entry_cannot_drop_the_repo_agents_folder(
    planted: str, tmp_path: Path, gemini_home: Path
) -> None:
    """Whatever sits at ``pkg/.git``, the repo-level ``.agents/`` above it
    stays in the digest."""
    repo, sub, script = _repo_with_sub(tmp_path)
    if planted == "empty-file":
        (sub / ".git").write_text("")
    elif planted == "junk-file":
        (sub / ".git").write_text("not a git file\n")
    elif planted == "directory":
        (sub / ".git").mkdir()
    else:
        (sub / ".git").write_text("gitdir: ../.git/modules/pkg\n")
    assert _changes_digest(sub, script)


def test_git_file_without_gitdir_is_not_a_project_root(
    tmp_path: Path, gemini_home: Path
) -> None:
    repo, sub, _ = _repo_with_sub(tmp_path)
    (sub / ".git").write_text("")
    assert scan.find_project_root(sub) == repo
    (sub / ".git").write_text("gitdir: ../.git/worktrees/pkg\n")
    assert scan.find_project_root(sub) == sub  # worktrees and submodules
    os.unlink(sub / ".git")
    os.mkfifo(sub / ".git")  # never opened: would block
    assert scan.find_project_root(sub) == repo


def test_worktree_root_keeps_working(tmp_path: Path, gemini_home: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text("gitdir: /somewhere/.git/worktrees/tree\n")
    _write(
        tree / ".agents" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh ./h.sh"}]}),
    )
    script = _write(tree / "h.sh", "echo 1")
    _write(tmp_path / ".agents" / "hooks.json", '{"Stop": []}')  # above: no repo
    result = scan.scan_workspace_config(tree)
    assert result.root == tree
    assert sorted(result.agy) == [".agents/hooks.json", "h.sh"]
    assert _changes_digest(tree, script)


# -- 5. .agents/settings.json --------------------------------------------------


def test_project_settings_json_is_a_reference_manifest(proj: Path) -> None:
    _write(
        proj / ".agents" / "settings.json",
        json.dumps({"hooks": {"Stop": [{"command": "sh scripts/stop.sh"}]}}),
    )
    script = _write(proj / "scripts" / "stop.sh", "echo 1")
    assert _changes_digest(proj, script)
    _write(proj / ".agents" / "settings.json", '{"hooks": {}, // why\n}')
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.AMBIGUOUS


# -- 6. a command that is itself dynamic ---------------------------------------


@pytest.mark.parametrize(
    "command",
    ["$RUNNER", "${RUNNER} --flag", "`cat cmd`", "$(cat cmd) arg", '"$RUNNER" x'],
)
def test_dynamic_command_word_is_unchecked(command: str, proj: Path) -> None:
    _write(
        proj / ".agents" / "hooks.json", json.dumps({"Stop": [{"command": command}]})
    )
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.VARIABLE_PATH
    _write(proj / ".agents" / "hooks.json", "{}")
    _write(
        proj / ".agents" / "agents" / "a" / "agent.md",
        f"---\nhooks:\n  Stop:\n    - command: {command}\n---\n",
    )
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.VARIABLE_PATH


@pytest.mark.parametrize(
    "manifest",
    [
        {"Stop": [{"command": "echo $USER"}]},
        {"Stop": [{"command": "$HOME/bin/hook"}]},
        {"s": {"command": "npx", "args": ["-y", "pkg"], "env": {"KEY": "${KEY}"}}},
        {"description": "$5 off", "Stop": [{"command": "true"}]},
    ],
)
def test_variables_outside_the_command_word_stay_checked(
    manifest: dict[str, object], proj: Path
) -> None:
    _write(proj / ".agents" / "hooks.json", json.dumps(manifest))
    assert scan.scan_workspace_config(proj).unchecked_reason is None


# -- .git/config in the cross-engine set --------------------------------------


def test_git_config_is_in_the_cross_engine_set(proj: Path) -> None:
    config = _write(proj / ".git" / "config", "[core]\n\tbare = false\n")
    _write(proj / ".git" / "hooks" / "pre-commit", "#!/bin/sh\n")
    first = scan.scan_workspace_config(proj)
    assert {".git/config", ".git/hooks/pre-commit"} <= set(first.cross)
    config.write_text("[core]\n\thooksPath = /tmp/evil-hooks\n")
    second = scan.scan_workspace_config(proj)
    assert second.cross_digest != first.cross_digest
    assert scan.changed_paths(first.cross, second.cross) == [".git/config"]
    # The cross-engine set never refuses, and never moves the agy digest.
    assert second.agy_digest == first.agy_digest
    assert second.unchecked_reason is None
