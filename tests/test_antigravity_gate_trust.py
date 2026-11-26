"""#1032 "Trust current config" for Ask me: the fingerprint, the one-shot
offer registry and the fail-closed trust store. Every check that can't give
a complete answer must leave nothing trusted."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.antigravity import gate_precheck, gate_trust
from untether.antigravity.gate_precheck import GatePrecheck, HookGroup
from untether.antigravity.gate_trust import Fingerprint, Untrustable
from untether.utils import antigravity_scan
from untether.utils.antigravity_state import TRUST_FILENAME, TrustRecord, TrustStore

STOP = HookGroup(
    name="lint",
    enabled=True,
    source="/p/.agents/hooks.json",
    actions=({"event": "Stop", "type": "command", "command": "echo hi"},),
)


def _scan(root: Path, agy: dict[str, Any] | None = None, **kw: Any) -> Any:
    return antigravity_scan.ScanResult(
        root=root,
        agy={".agents/hooks.json": (12, "a" * 64)} if agy is None else agy,
        **kw,
    )


def _fp(tmp_path: Path, **kw: Any) -> Fingerprint | Untrustable:
    args: dict[str, Any] = {
        "cwd": tmp_path,
        "scan": _scan(tmp_path),
        "precheck": GatePrecheck(ok=True, foreign_groups=(STOP,)),
        "foreign": (".agents/hooks.json",),
        "agy_version": "1.5.18",
        "mcp": gate_precheck.MCP_NONE,
        "agent_problem": None,
    }
    args.update(kw)
    return gate_trust.fingerprint(**args)


def _digest(tmp_path: Path, **kw: Any) -> str:
    fp = _fp(tmp_path, **kw)
    assert isinstance(fp, Fingerprint), fp
    return fp.digest


@pytest.fixture(autouse=True)
def _fresh() -> None:
    gate_trust._reset_for_tests()


# ── fingerprint: untrustable ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "scan_kw",
    [
        {"truncated": True},
        {"outside_root": True},
        {"problems": (antigravity_scan.FRONT_MATTER,)},
        {"problems": (antigravity_scan.UNREADABLE,)},
    ],
)
def test_fingerprint_untrustable_when_scan_unchecked(
    tmp_path: Path, scan_kw: dict[str, Any]
) -> None:
    fp = _fp(tmp_path, scan=_scan(tmp_path, **scan_kw))
    assert isinstance(fp, Untrustable) and fp.reason == gate_trust.SCAN_INCOMPLETE
    assert isinstance(_fp(tmp_path, scan=None), Untrustable)


def test_fingerprint_untrustable_when_mcp_list_failed(tmp_path: Path) -> None:
    # None covers a failed listing, its timeout and the RAM guard's skip
    # (``_run_mcp_list`` returns None for all three).
    fp = _fp(tmp_path, mcp=None)
    assert isinstance(fp, Untrustable) and fp.reason == gate_trust.MCP_UNKNOWN


@pytest.mark.parametrize("version", [None, ""])
def test_fingerprint_untrustable_when_version_unknown(
    tmp_path: Path, version: str | None
) -> None:
    fp = _fp(tmp_path, agy_version=version)
    assert isinstance(fp, Untrustable) and fp.reason == gate_trust.VERSION_UNKNOWN


@pytest.mark.parametrize("enabled", [True, False])
def test_fingerprint_refuses_foreign_pretooluse_enabled_and_disabled(
    tmp_path: Path, enabled: bool
) -> None:
    from tests.antigravity_gate_support import hooks_listing
    from untether.antigravity.gate_precheck import evaluate_hooks

    listing = hooks_listing(
        extra_groups=[
            {
                "name": "sneaky",
                "enabled": enabled,
                "source": "/p/.agents/hooks.json",
                "actions": [{"event": "PreToolUse", "matcher": "*", "command": "x"}],
            }
        ]
    )
    pre = evaluate_hooks(gate_precheck.parse_hooks(listing), wait_s=900)
    fp = _fp(tmp_path, precheck=pre)
    assert isinstance(fp, Untrustable)
    assert fp.reason == gate_trust.FOREIGN_PRETOOLUSE
    assert "/p/.agents/hooks.json" in fp.detail


@pytest.mark.parametrize(
    "reason", [gate_trust.AGENT_HOOKS, gate_trust.AGENT_UNREADABLE]
)
def test_fingerprint_passes_agent_problem_through(tmp_path: Path, reason: str) -> None:
    fp = _fp(tmp_path, agent_problem=(reason, ".agents/agents/x.md"))
    assert isinstance(fp, Untrustable) and fp.reason == reason


# ── agent_problem: custom agents (P-T1: agy's listing never shows their hooks)


def _agent(root: Path, rel: str, text: str) -> tuple[str, Any]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    data = path.read_bytes()
    return rel, (len(data), hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize(
    "front",
    [
        "hooks: ./h.json\n",
        "hooks: []\n",  # agy loads it; conservative all the same
        "hooks:\n  - ./h.json\n",
        "Hooks: ./h.json\n",
    ],
)
def test_fingerprint_refuses_project_agent_with_hooks_key(
    tmp_path: Path, front: str
) -> None:
    rel, fp = _agent(
        tmp_path, ".agents/agents/a.md", f"---\nname: a\n{front}---\nbody\n"
    )
    scan = _scan(tmp_path, {rel: fp})
    assert gate_trust.agent_problem(scan) == (gate_trust.AGENT_HOOKS, rel)


@pytest.mark.parametrize(
    "text",
    [
        "﻿---\nname: a\n---\n",  # BOM: agy may read it differently
        "---\nname: a\nextra: {hooks: x}\n---\n",
        "---\nname: a\noptions:\n  hooks: ./h.json\n---\n",
    ],
)
def test_fingerprint_refuses_ambiguous_agent_front_matter(
    tmp_path: Path, text: str
) -> None:
    rel, fp = _agent(tmp_path, ".agents/agents/nested/agent.md", text)
    scan = _scan(tmp_path, {rel: fp})
    assert gate_trust.agent_problem(scan) == (gate_trust.AGENT_HOOKS, rel)


def test_agent_without_hooks_is_fine(tmp_path: Path) -> None:
    rel, fp = _agent(
        tmp_path, ".agents/agents/a.md", "---\nname: a\ndescription: d\n---\nhi\n"
    )
    hooks = {".agents/hooks.json": (3, "b" * 64)}
    assert gate_trust.agent_problem(_scan(tmp_path, {rel: fp, **hooks})) is None


def test_agent_changed_since_scan_is_unreadable(tmp_path: Path) -> None:
    """The bytes read must be the bytes the scan hashed (TOCTOU)."""
    rel, fp = _agent(tmp_path, ".agents/agents/a.md", "---\nname: a\n---\n")
    (tmp_path / rel).write_text("---\nname: a\nhooks: ./x\n---\n")
    assert gate_trust.agent_problem(_scan(tmp_path, {rel: fp})) == (
        gate_trust.AGENT_UNREADABLE,
        rel,
    )
    (tmp_path / rel).unlink()
    assert gate_trust.agent_problem(_scan(tmp_path, {rel: fp}))[0] == (
        gate_trust.AGENT_UNREADABLE
    )


def test_fingerprint_refuses_user_level_agent(tmp_path: Path) -> None:
    """D7 = A: user-level agents are stat only (D33), so unreadable."""
    rel = f"{antigravity_scan.USER_DISPLAY_PREFIX}agents/mine.md"
    scan = _scan(tmp_path, {rel: (10, "1:2:3")})
    assert gate_trust.agent_problem(scan) == (gate_trust.AGENT_UNREADABLE, rel)


# ── fingerprint: what changes it ───────────────────────────────────────────


@pytest.mark.parametrize(
    "agy",
    [
        {".agents/hooks.json": (12, "c" * 64)},  # content
        {".agents/hooks.json": (12, "1:2:4")},  # stat stamp
        {".agents/hooks.json": (12, "a" * 64 + ":link:x")},  # link target
        {".agents/hooks.json": (12, "a" * 64), ".agents/mcp_config.json": (1, "d")},
        {},  # removed
    ],
)
def test_fingerprint_changes_when_any_scan_entry_changes(
    tmp_path: Path, agy: dict[str, Any]
) -> None:
    assert _digest(tmp_path) != _digest(tmp_path, scan=_scan(tmp_path, agy))


def test_fingerprint_changes_when_only_settings_json_hook_changes(
    tmp_path: Path,
) -> None:
    """A group defined in agy's settings.json is only in the listing."""
    edited = replace(
        STOP, actions=({"event": "Stop", "type": "command", "command": "rm -rf ~"},)
    )
    other = HookGroup("x", True, "~/.gemini/antigravity-cli/settings.json", ())
    base = _digest(tmp_path)
    for groups in ((edited,), (STOP, other), (replace(STOP, enabled=False),)):
        assert base != _digest(
            tmp_path, precheck=GatePrecheck(ok=True, foreign_groups=groups)
        )


def test_fingerprint_changes_when_mcp_list_changes(tmp_path: Path) -> None:
    assert _digest(tmp_path) != _digest(tmp_path, mcp="e" * 64)


def test_fingerprint_changes_when_agy_version_changes(tmp_path: Path) -> None:
    assert _digest(tmp_path) != _digest(tmp_path, agy_version="1.5.19")


def test_fingerprint_changes_when_cwd_changes(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    assert _digest(tmp_path) != _digest(tmp_path, cwd=sub)


def test_fingerprint_ignores_listing_order(tmp_path: Path) -> None:
    other = HookGroup("b", True, "/p/x.json", ({"event": "Stop", "command": "y"},))
    one = GatePrecheck(ok=True, foreign_groups=(STOP, other))
    two = GatePrecheck(ok=True, foreign_groups=(other, STOP))
    assert _digest(tmp_path, precheck=one) == _digest(tmp_path, precheck=two)


def test_fingerprint_ignores_our_own_group(tmp_path: Path) -> None:
    """Only the *other* groups are hashed: a reinstalled gate (same
    listing otherwise) asks nothing again."""
    from tests.antigravity_gate_support import hooks_listing
    from untether.antigravity.gate_precheck import evaluate_hooks

    extra = [
        {
            "name": STOP.name,
            "enabled": True,
            "source": STOP.source,
            "actions": list(STOP.actions),
        }
    ]
    a = evaluate_hooks(
        gate_precheck.parse_hooks(hooks_listing(900, extra_groups=extra)), wait_s=900
    )
    b = evaluate_hooks(
        gate_precheck.parse_hooks(hooks_listing(600, extra_groups=extra)), wait_s=600
    )
    assert _digest(tmp_path, precheck=a) == _digest(tmp_path, precheck=b)


def test_fingerprint_shape(tmp_path: Path) -> None:
    fp = _fp(tmp_path)
    assert isinstance(fp, Fingerprint)
    assert len(fp.digest) == 64 and fp.short == fp.digest[:12]
    assert fp.paths == (".agents/hooks.json",)
    assert fp.groups == ("lint (/p/.agents/hooks.json)",)
    assert fp.agy_version == "1.5.18"


# ── offers: one-shot, chat-bound, expiring ─────────────────────────────────


def _offer(tmp_path: Path, chat: int = 1) -> str:
    fp = _fp(tmp_path)
    assert isinstance(fp, Fingerprint)
    return gate_trust.register_offer(chat, str(tmp_path), fp)


def test_offer_bound_to_chat(tmp_path: Path) -> None:
    token = _offer(tmp_path, chat=1)
    assert len(token) == 8
    assert gate_trust.claim_offer(token, chat_id=2) is None
    assert gate_trust.peek_offer(token, chat_id=2) is None
    claimed = gate_trust.claim_offer(token, chat_id=1)
    assert claimed is not None and claimed.root == str(tmp_path)


def test_offer_expires_after_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(gate_trust.time, "monotonic", lambda: now[0])
    token = _offer(tmp_path)
    now[0] += gate_trust.OFFER_TTL_S + 1
    assert gate_trust.peek_offer(token, chat_id=1) is None
    assert gate_trust.claim_offer(token, chat_id=1) is None


def test_offer_claimed_once(tmp_path: Path) -> None:
    token = _offer(tmp_path)
    assert gate_trust.claim_offer(token, chat_id=1) is not None
    assert gate_trust.claim_offer(token, chat_id=1) is None


def test_offer_peek_does_not_claim(tmp_path: Path) -> None:
    token = _offer(tmp_path)
    assert gate_trust.peek_offer(token, chat_id=1) is not None
    assert gate_trust.peek_offer(token, chat_id=1) is not None
    assert gate_trust.claim_offer(token, chat_id=1) is not None


def test_offer_registry_caps_entries(tmp_path: Path) -> None:
    tokens = [_offer(tmp_path) for _ in range(gate_trust.OFFER_MAX + 5)]
    assert len(gate_trust._OFFERS) == gate_trust.OFFER_MAX
    assert gate_trust.peek_offer(tokens[0], chat_id=1) is None
    assert gate_trust.peek_offer(tokens[-1], chat_id=1) is not None


# ── TrustStore: fail closed ────────────────────────────────────────────────


def _record(fp: str = "f" * 64) -> TrustRecord:
    return TrustRecord(
        fp=fp,
        paths=(".agents/hooks.json",),
        groups=("lint (/p)",),
        agy="1.5.18",
        at=1700000000.0,
        by=7,
    )


def _store(tmp_path: Path) -> TrustStore:
    return TrustStore(tmp_path / TRUST_FILENAME)


def test_trust_store_file_is_owner_only(tmp_path: Path) -> None:
    # The record can switch on agy's bypass, so nobody else may write it.
    store = _store(tmp_path)
    assert store.set(1, "/p", _record())
    assert (tmp_path / TRUST_FILENAME).stat().st_mode & 0o777 == 0o600
    os.chmod(tmp_path / TRUST_FILENAME, 0o664)
    assert store.revoke(1, "/p")
    assert (tmp_path / TRUST_FILENAME).stat().st_mode & 0o777 == 0o600


def test_trust_store_missing_file_trusts_nothing(tmp_path: Path) -> None:
    assert _store(tmp_path).get(1, "/p") is None


def test_trust_store_corrupt_json_trusts_nothing_and_warns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.set(1, "/p", _record())
    (tmp_path / TRUST_FILENAME).write_text("{not json")
    with structlog.testing.capture_logs() as logs:
        assert store.get(1, "/p") is None
    assert [log for log in logs if log["event"] == "antigravity.trust.load_failed"]


_GOOD = {
    "1|/p": {"fp": "f" * 64, "paths": [], "groups": [], "agy": "1", "at": 1, "by": None}
}


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "records": _GOOD},
        {"version": "1", "records": _GOOD},
        {"records": _GOOD},
        {"version": 1, "records": list(_GOOD.values())},
        [_GOOD],
    ],
)
def test_trust_store_wrong_version_trusts_nothing(tmp_path: Path, payload: Any) -> None:
    (tmp_path / TRUST_FILENAME).write_text(json.dumps({"version": 1, "records": _GOOD}))
    assert _store(tmp_path).get(1, "/p") is not None
    (tmp_path / TRUST_FILENAME).write_text(json.dumps(payload))
    assert _store(tmp_path).get(1, "/p") is None


def test_trust_store_symlink_path_trusts_nothing(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _store(tmp_path).set(1, "/p", _record())
    os.replace(tmp_path / TRUST_FILENAME, real)
    (tmp_path / TRUST_FILENAME).symlink_to(real)
    with structlog.testing.capture_logs() as logs:
        assert _store(tmp_path).get(1, "/p") is None
    assert [log for log in logs if log["event"] == "antigravity.trust.load_failed"]


@pytest.mark.parametrize(
    "bad",
    [
        {"fp": "short"},
        {"fp": "F" * 64},
        {"paths": "x"},
        {"paths": [1]},
        {"groups": None},
        {"agy": 1},
        {"at": "now"},
        {"at": True},
        {"by": "me"},
        {"by": True},
    ],
)
def test_trust_store_drops_records_with_wrong_types(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    rec = {"fp": "f" * 64, "paths": [], "groups": [], "agy": "1", "at": 1, "by": 3}
    payload = {
        "version": 1,
        "records": {"1|/p": {**rec, **bad}, "2|/p": rec},
    }
    (tmp_path / TRUST_FILENAME).write_text(json.dumps(payload))
    store = _store(tmp_path)
    assert store.get(1, "/p") is None
    assert store.get(2, "/p") is not None


def test_trust_store_save_failure_returns_false_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.utils import antigravity_state

    store = _store(tmp_path)
    assert store.set(1, "/p", _record("a" * 64))

    def boom(path: Path, payload: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(antigravity_state, "atomic_write_json", boom)
    assert store.set(1, "/p", _record("b" * 64)) is False
    assert store.set(2, "/p", _record()) is False
    got = store.get(1, "/p")
    assert got is not None and got.fp == "a" * 64
    assert store.get(2, "/p") is None
    # Revoke fails closed the other way: the record is gone in memory even
    # though the file still has it (reported as not saved).
    assert store.revoke(1, "/p") is False
    assert store.get(1, "/p") is None


def test_trust_store_keyed_by_chat_and_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.set(1, "/p", _record())
    assert store.get(1, "/p") is not None
    assert store.get(2, "/p") is None
    assert store.get(1, "/q") is None
    # Survives a restart (a new store reads the file).
    assert _store(tmp_path).get(1, "/p") == _record()


def test_trust_store_revoke(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set(1, "/p", _record())
    assert store.revoke(1, "/p") is True
    assert store.get(1, "/p") is None
    assert _store(tmp_path).get(1, "/p") is None
    assert store.revoke(1, "/p") is False


def test_trust_store_reloads_on_mtime_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.set(1, "/p", _record())
    other = _store(tmp_path)
    assert other.revoke(1, "/p")
    st = (tmp_path / TRUST_FILENAME).stat()
    os.utime(tmp_path / TRUST_FILENAME, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert store.get(1, "/p") is None


def test_trust_store_caps_records(tmp_path: Path) -> None:
    from untether.utils import antigravity_state

    store = _store(tmp_path)
    for chat in range(antigravity_state._MAX_TRUST + 3):
        assert store.set(chat, "/p", _record())
    assert store.get(0, "/p") is None
    assert store.get(antigravity_state._MAX_TRUST + 2, "/p") is not None
    data = json.loads((tmp_path / TRUST_FILENAME).read_text())
    assert len(data["records"]) == antigravity_state._MAX_TRUST


def test_trust_store_in_memory_without_path() -> None:
    store = TrustStore(None)
    assert store.set(1, "/p", _record())
    assert store.get(1, "/p") is not None
    assert store.revoke(1, "/p")
