"""#1186: reading the Codex plugin's job state (read-only, plugin 1.0.6 layout)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from untether.utils.codex_plugin_jobs import (
    default_state_roots,
    session_plugin_job_labels,
    session_plugin_jobs,
)

SID = "sess-1186"


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def write_state(root: Path, jobs: list[dict[str, Any]], *, workspace: str) -> Path:
    """A ``state.json`` as plugin 1.0.6 writes it
    (``scripts/lib/state.mjs``: ``<slug>-<sha256[:16]>/state.json``)."""
    path = root / f"{workspace}-0123456789abcdef" / "state.json"
    path.parent.mkdir(parents=True)
    (path.parent / "jobs").mkdir()
    payload = {"version": 1, "config": {"stopReviewGate": False}, "jobs": jobs}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def job(job_id: str, *, session: str = SID, status: str = "running", pid: Any = None):
    return {
        "id": job_id,
        "kind": "task",
        "jobClass": "task",
        "title": "Codex task",
        "workspaceRoot": "/work/project",
        "sessionId": session,
        "status": status,
        "phase": status,
        "pid": os.getpid() if pid is None else pid,
        "logFile": f"/state/jobs/{job_id}.log",
        "createdAt": "2026-11-18T00:00:00.000Z",
        "updatedAt": "2026-11-18T00:00:00.000Z",
    }


def test_lists_own_live_queued_and_running_jobs(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        [job("task-a"), job("task-b", status="queued")],
        workspace="project",
    )
    assert session_plugin_jobs(SID, [tmp_path]) == ["task-a", "task-b"]


def test_matches_across_workspace_dirs_and_roots(tmp_path: Path) -> None:
    other_root = tmp_path / "tmp-fallback"
    write_state(tmp_path / "data", [job("task-a")], workspace="one")
    write_state(other_root, [job("task-b")], workspace="two")
    assert session_plugin_jobs(SID, [tmp_path / "data", other_root]) == [
        "task-a",
        "task-b",
    ]


def test_ignores_other_sessions_finished_and_dead_jobs(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        [
            job("other", session="sess-other"),
            job("done", status="completed"),
            job("failed", status="failed"),
            job("dead", pid=_dead_pid()),
            job("no-pid", pid=None) | {"pid": None},
            job("bad-pid", pid=0),
            job("bool-pid", pid=True),
        ],
        workspace="project",
    )
    assert session_plugin_jobs(SID, [tmp_path]) == []


def test_missing_or_malformed_state_fails_open(tmp_path: Path) -> None:
    assert session_plugin_jobs(SID, [tmp_path / "absent"]) == []
    bad = tmp_path / "ws-0123456789abcdef" / "state.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json", encoding="utf-8")
    odd = tmp_path / "ws2-0123456789abcdef" / "state.json"
    odd.parent.mkdir(parents=True)
    odd.write_text(json.dumps({"jobs": {"x": 1}}), encoding="utf-8")
    listy = tmp_path / "ws3-0123456789abcdef" / "state.json"
    listy.parent.mkdir(parents=True)
    listy.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert session_plugin_jobs(SID, [tmp_path]) == []


def test_never_writes_the_plugin_state(tmp_path: Path) -> None:
    path = write_state(tmp_path, [job("task-a")], workspace="project")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    session_plugin_jobs(SID, [tmp_path])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["jobs", "state.json"]


def test_job_labels_title_and_id(tmp_path: Path) -> None:
    """#1186 step 3: the close notice names each job as ``<title> <id>`` (what
    ``/codex:status`` prints); an unusable title falls back to "Codex job".
    The prompt ``summary`` is user content and never appears."""
    write_state(
        tmp_path,
        [
            job("task-a") | {"summary": "SECRET PROMPT"},
            job("task-b") | {"title": "Codex Review"},
            {k: v for k, v in job("task-c").items() if k != "title"},
            job("task-d") | {"title": "   "},
            job("task-e") | {"title": 7},
            job("task-f") | {"title": "x" * 41},
            job("task-x", session="sess-other"),
        ],
        workspace="project",
    )
    labels = session_plugin_job_labels(SID, [tmp_path])
    assert labels == [
        "Codex task task-a",
        "Codex Review task-b",
        "Codex job task-c",
        "Codex job task-d",
        "Codex job task-e",
        "Codex job task-f",
    ]
    assert not any("SECRET" in label for label in labels)
    # The hold's id list is unchanged.
    assert session_plugin_jobs(SID, [tmp_path]) == [
        "task-a",
        "task-b",
        "task-c",
        "task-d",
        "task-e",
        "task-f",
    ]


def test_job_labels_fail_open(tmp_path: Path) -> None:
    assert session_plugin_job_labels(SID, [tmp_path / "absent"]) == []
    assert session_plugin_job_labels(SID, ()) == []


def test_default_root_is_the_plugin_data_dir_only() -> None:
    """The plugin's world-writable tmp fallback is never read (security)."""
    (data,) = default_state_roots()
    assert data.parts[-5:] == (
        ".claude",
        "plugins",
        "data",
        "codex-openai-codex",
        "state",
    )


# ── untrusted state: refused, never followed or blocked on ─────────────────


def test_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    write_state(real, [job("task-a")], workspace="project")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert session_plugin_jobs(SID, [link]) == []


def test_symlinked_state_file_is_not_followed(tmp_path: Path) -> None:
    elsewhere = write_state(tmp_path / "elsewhere", [job("task-a")], workspace="x")
    root = tmp_path / "root"
    ws = root / "project-0123456789abcdef"
    ws.mkdir(parents=True)
    (ws / "state.json").symlink_to(elsewhere)
    assert session_plugin_jobs(SID, [root]) == []


def test_fifo_state_file_neither_hangs_nor_counts(tmp_path: Path) -> None:
    ws = tmp_path / "project-0123456789abcdef"
    ws.mkdir()
    os.mkfifo(ws / "state.json")
    assert session_plugin_jobs(SID, [tmp_path]) == []


def test_oversized_state_file_is_skipped(tmp_path: Path) -> None:
    path = write_state(tmp_path, [job("task-a")], workspace="project")
    path.write_text(path.read_text() + " " * (1024 * 1024), encoding="utf-8")
    assert session_plugin_jobs(SID, [tmp_path]) == []


def test_foreign_owned_state_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_state(tmp_path, [job("task-a")], workspace="project")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert session_plugin_jobs(SID, [tmp_path]) == []
