"""Tests for the integration-test attestation marker writer.

Covers the #674 SHA-binding of ``scripts/run-integration-tests.sh``: the marker
records ``head_sha`` + ``dev_bot_id`` so the fleet-rollout gate binds an exact
commit + the real dev bot, not a reusable boolean.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run-integration-tests.sh"
FAKE_VERSION = "0.0.0test"


def _run(
    *args: str, home: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), FAKE_VERSION, "--manual", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _marker(home: Path) -> dict:
    path = home / ".untether-dev" / f"integration-test-pass-{FAKE_VERSION}.json"
    assert path.exists(), f"marker not written: {path}"
    return json.loads(path.read_text())


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_marker_records_head_sha_and_dev_bot_id(tmp_path: Path) -> None:
    _run(home=tmp_path)
    m = _marker(tmp_path)
    assert m["version"] == FAKE_VERSION
    assert m["head_sha"] == _git_head()  # auto-derived from the script's repo
    assert m["dev_bot_id"] == "8678330610"  # documented default
    assert m["dev_bot"] == "@untether_dev_bot"


def test_head_sha_flag_overrides(tmp_path: Path) -> None:
    _run("--head-sha", "deadbeef", home=tmp_path)
    assert _marker(tmp_path)["head_sha"] == "deadbeef"


def test_head_sha_env_overrides(tmp_path: Path) -> None:
    _run(home=tmp_path, env_extra={"UT_INTEGRATION_HEAD_SHA": "cafef00d"})
    assert _marker(tmp_path)["head_sha"] == "cafef00d"


def test_dev_bot_id_env_overrides(tmp_path: Path) -> None:
    _run(home=tmp_path, env_extra={"UT_DEV_BOT_ID": "12345"})
    assert _marker(tmp_path)["dev_bot_id"] == "12345"


def test_tiers_and_notes_preserved(tmp_path: Path) -> None:
    _run("--tiers", "tier7,tier1-claude", "--notes", "all green", home=tmp_path)
    m = _marker(tmp_path)
    assert m["tiers"] == ["tier7", "tier1-claude"]
    assert m["notes"] == "all green"
