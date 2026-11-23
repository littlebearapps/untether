"""Zero-token Codex app-server reads (#966, #846, #423): ``runners/codex_reads``.

A fake ``codex app-server`` replays the sanitised 0.171.0 probe transcripts
(``tests/fixtures/codex_app_server/reads-*.jsonl``): it answers each request
with the response Codex gave to the same method, logs every message it got,
its argv and cwd, and one line per spawn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from untether.runners import codex_app_server as gate
from untether.runners import codex_reads as reads
from untether.runners.codex import CodexRunner

FIXTURES = Path(__file__).parent / "fixtures" / "codex_app_server"
READS = FIXTURES / "reads-0.171.0.jsonl"
LOGGED_OUT = FIXTURES / "reads-loggedout-0.171.0.jsonl"

_FAKE = r"""
import json, os, sys, time
SC = json.load(open(SC_PATH))
reqs, resp = {}, {}
for line in open(SC["transcript"]):
    rec = json.loads(line)
    m = rec["msg"]
    if rec["dir"] == "c2s" and "id" in m:
        reqs[m["id"]] = (m["method"], m.get("params") or {})
    elif rec["dir"] == "s2c" and "id" in m and "method" not in m:
        resp[m["id"]] = m
answers = {}
for rid, (method, params) in reqs.items():
    if rid not in resp:
        continue
    if method == "model/list" and not params.get("includeHidden") and "model/list" in answers:
        continue
    if method == "model/list" and params.get("includeHidden"):
        answers.pop("model/list", None)
    answers.setdefault(method, {k: v for k, v in resp[rid].items() if k != "id"})
answers.update(SC.get("override", {}))
with open(SPAWNS, "a") as fh:
    fh.write("spawn\n")
with open(ARGV_PATH, "w") as fh:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd()}, fh)

def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

with open(LOG_PATH, "a") as log:
    for line in sys.stdin:
        msg = json.loads(line)
        log.write(line)
        log.flush()
        if SC.get("delay"):
            time.sleep(SC["delay"])
        if SC.get("hang"):
            continue
        if "id" not in msg:
            continue
        method = msg.get("method")
        if SC.get("exit_on") == method:
            sys.exit(0)
        if method == "initialize":
            out({"id": msg["id"], "result": {"userAgent": "fake/0.171.0"}})
            out({"method": "account/updated", "params": {"authMode": "chatgpt"}})
            out({"id": "srv-1", "method": "item/tool/call", "params": {}})
            continue
        answer = answers.get(method, {"error": {"code": -32601, "message": "nope"}})
        out({"id": msg["id"], **answer})
"""


class Fake:
    def __init__(self, tmp_path: Path, *, transcript: Path = READS, **sc: Any) -> None:
        self.dir = tmp_path / "fake"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "received.jsonl"
        self.argv = self.dir / "argv.json"
        self.spawns = self.dir / "spawns.log"
        sc_path = self.dir / "scenario.json"
        sc_path.write_text(json.dumps({"transcript": str(transcript), **sc}))
        self.cmd = self.dir / "codex"
        self.cmd.write_text(
            f"#!{sys.executable}\n"
            f"SC_PATH, LOG_PATH, ARGV_PATH, SPAWNS = {str(sc_path)!r}, "
            f"{str(self.log)!r}, {str(self.argv)!r}, {str(self.spawns)!r}\n" + _FAKE
        )
        self.cmd.chmod(0o755)

    def runner(self, extra_args: list[str] | None = None) -> CodexRunner:
        return CodexRunner(codex_cmd=str(self.cmd), extra_args=extra_args or [])

    def methods(self) -> list[str]:
        if not self.log.exists():
            return []
        return [json.loads(x).get("method") for x in self.log.read_text().splitlines()]

    def spawn_count(self) -> int:
        return len(self.spawns.read_text().splitlines()) if self.spawns.exists() else 0


@pytest.fixture
def supported(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def support(cmd: str) -> gate.AppServerSupport:
        calls.append(cmd)
        return gate.AppServerSupport("0.171.0", ())

    monkeypatch.setattr(gate, "codex_app_server_support", support)
    return calls


@pytest.mark.anyio
async def test_one_spawn_answers_every_catalogue_read(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path)
    runner = fake.runner(["-c", "notify=[]", "--profile", "p", "--enable", "x"])
    catalogue = await reads.get_catalogue(runner, "/proj")
    assert fake.spawn_count() == 1
    sent = json.loads(fake.argv.read_text())
    # Only the app-server allow-list of extra_args is passed on.
    assert sent["argv"] == [
        "-c",
        "notify=[]",
        "--enable",
        "x",
        "app-server",
        "--listen",
        "stdio://",
    ]
    assert not Path(sent["cwd"]).exists()  # fresh temp dir, removed after
    methods = fake.methods()
    assert methods[:2] == ["initialize", "initialized"]
    assert set(methods[2:]) == {
        "account/read",
        "model/list",
        "config/read",
        "modelProvider/capabilities/read",
        "configRequirements/read",
        None,  # the answer to the fake's server request (declined)
    }
    assert "thread/start" not in methods and "turn/start" not in methods
    assert catalogue.default_model == "gpt-6.2-sol"
    assert catalogue.config == reads.ConfigInfo("gpt-6.1-sol", "low", None)
    assert catalogue.models["gpt-6.1-sol"].levels == reads.CODEX_REASONING_VOCAB
    assert "ultra" not in catalogue.models["gpt-6-luna"].levels
    assert catalogue.models["gpt-6-luna"].default_level == "medium"
    assert catalogue.models["gpt-reserve"].hidden
    assert catalogue.models["gpt-6.2-sol"].descriptions["ultra"] == (
        "Maximum reasoning with automatic task delegation"
    )
    assert catalogue.web_search_supported is True
    assert catalogue.web_search_allowed is None
    assert (catalogue.auth_kind, catalogue.plan) == ("chatgpt", "pro")


@pytest.mark.anyio
async def test_usage_parse_from_the_pro_transcript(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path)
    usage, age = await reads.get_usage(fake.runner())
    assert age == 0.0
    assert usage.auth_kind == "chatgpt" and usage.plan == "pro"
    assert usage.allowed is True
    assert usage.buckets == (
        reads.Bucket("codex", (reads.Window("7d", 8, 1795593678),), None),
    )
    assert usage.has_credits and not usage.unlimited
    assert usage.credits_balance == "62500.0000000000"
    assert usage.reset_credits == 3
    # No email, user id or account id is kept anywhere in the cache.
    assert "redacted" not in repr(reads._USAGE.values)
    assert "redacted" not in repr(reads.usage_stats())


@pytest.mark.anyio
async def test_logged_out_is_not_signed_in_but_catalogue_reads(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path, transcript=LOGGED_OUT)
    with pytest.raises(reads.CodexNotSignedIn):
        await reads.get_usage(fake.runner())
    catalogue = await reads.get_catalogue(fake.runner(), None)
    assert catalogue.auth_kind is None
    assert catalogue.models  # bundled list
    assert catalogue.config is not None and catalogue.config.model is None


def test_rate_limit_auth_error_is_not_signed_in() -> None:
    account = {"account": {"type": "chatgpt", "planType": "pro"}}
    err = reads.RpcError(
        -32600, "codex account authentication required to read rate limits"
    )
    with pytest.raises(reads.CodexNotSignedIn):
        reads.parse_usage(account, err)
    with pytest.raises(reads.CodexReadError) as exc:
        reads.parse_usage(account, reads.RpcError(-32603, "boom"))
    assert exc.value.kind == "error" and exc.value.code == -32603


@pytest.mark.parametrize("kind", ["apiKey", "amazonBedrock", "gcp"])
def test_non_chatgpt_auth_has_no_buckets(kind: str) -> None:
    raw = reads.parse_usage({"account": {"type": kind}}, None)
    usage = raw.build()
    assert usage.auth_kind == kind and usage.buckets == ()


@pytest.mark.anyio
async def test_timeout_kills_the_read(
    tmp_path: Path, supported: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reads, "READ_TIMEOUT_S", 0.5)
    fake = Fake(tmp_path, hang=True)
    with anyio.fail_after(15), pytest.raises(reads.CodexReadError) as exc:
        await reads.get_usage(fake.runner())
    assert exc.value.kind == "timeout"


@pytest.mark.anyio
async def test_early_exit_is_exited(tmp_path: Path, supported: list[str]) -> None:
    fake = Fake(tmp_path, exit_on="account/rateLimits/read")
    with pytest.raises(reads.CodexReadError) as exc:
        await reads.get_usage(fake.runner())
    assert exc.value.kind == "exited"


@pytest.mark.anyio
async def test_unsupported_gate_never_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate,
        "codex_app_server_support",
        lambda cmd: gate.AppServerSupport("0.160.0", ("`model/list` missing",)),
    )
    fake = Fake(tmp_path)
    with pytest.raises(reads.CodexReadUnavailable) as exc:
        await reads.get_usage(fake.runner())
    assert exc.value.version == "0.160.0"
    assert fake.spawn_count() == 0


def test_read_args_keeps_only_the_app_server_allow_list() -> None:
    assert reads.read_args(
        [
            "-c",
            "a=1",
            "--config=b=2",
            "-cc=3",
            "--profile",
            "work",
            "--strict-config",
            "--disable=y",
            "--search",
            "-m",
            "gpt",
        ]
    ) == ["-c", "a=1", "--config=b=2", "-cc=3", "--strict-config", "--disable=y"]


@pytest.mark.anyio
async def test_cache_hit_ttl_and_failure_ttl(
    tmp_path: Path, supported: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Fake(tmp_path)
    runner = fake.runner()
    await reads.get_usage(runner)
    await reads.get_usage(runner)
    assert fake.spawn_count() == 1
    # Expire the entry: the next call reads again.
    for key, (at, value) in list(reads._USAGE.values.items()):
        reads._USAGE.values[key] = (at - reads.USAGE_TTL_S - 1, value)
    await reads.get_usage(runner)
    assert fake.spawn_count() == 2

    # A failure is cached for the failure TTL: no second spawn.
    broken = Fake(tmp_path / "b", exit_on="account/read")
    for _ in range(2):
        with pytest.raises(reads.CodexReadError):
            await reads.get_usage(broken.runner())
    assert broken.spawn_count() == 1
    assert reads.usage_stats().last_error_kind == "exited"


@pytest.mark.anyio
async def test_concurrent_callers_share_one_spawn(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path, delay=0.2)
    runner = fake.runner()
    results: list[Any] = []

    async def call() -> None:
        results.append(await reads.get_usage(runner))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
    assert fake.spawn_count() == 1
    assert len(results) == 2


def test_window_labels() -> None:
    assert reads.window_label(300) == "5h"
    assert reads.window_label(10080) == "7d"
    assert reads.window_label(90) == "90m"
    assert reads.window_label(120) == "2h"
    assert reads.window_label(None) == "limit"


def test_time_until() -> None:
    assert reads.time_until(1000 + 2 * 86400 + 4 * 3600, now=1000) == "2d 4h"
    assert reads.time_until(1000 + 3600 + 14 * 60, now=1000) == "1h 14m"
    assert reads.time_until(500, now=1000) == "0m"


def _limits(**by_id: Any) -> dict[str, Any]:
    return {
        "ordinaryUsageAllowed": False,
        "rateLimits": {},
        "rateLimitsByLimitId": by_id,
    }


def test_multi_bucket_and_single_bucket_fallback() -> None:
    account = {"account": {"type": "chatgpt", "planType": "team"}}
    raw = reads.parse_usage(
        account,
        _limits(
            codex={
                "limitId": "codex",
                "primary": {
                    "usedPercent": 72,
                    "windowDurationMins": 300,
                    "resetsAt": 5,
                },
                "secondary": {"usedPercent": 30, "windowDurationMins": 10080},
                "rateLimitReachedType": None,
            },
            other={
                "limitId": "other",
                "limitName": "Sol models",
                "primary": {"usedPercent": 100, "windowDurationMins": 10080},
                "rateLimitReachedType": "rate_limit_reached",
            },
        ),
    )
    usage = raw.build()
    assert usage.allowed is False
    assert [b.name for b in usage.buckets] == ["codex", "Sol models"]
    assert [w.label for w in usage.buckets[0].windows] == ["5h", "7d"]
    assert usage.reached_type == "rate_limit_reached"
    single = reads.parse_usage(
        account,
        {
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 1}},
            "rateLimitsByLimitId": None,
        },
    ).build()
    assert [b.name for b in single.buckets] == ["codex"]
    assert single.allowed is None


@pytest.mark.anyio
async def test_push_merge_null_never_clears(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path)
    runner = fake.runner()
    await reads.get_usage(runner)
    cmd = runner.command()
    reads.merge_push(
        cmd,
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 11, "windowDurationMins": 10080},
                "credits": None,
                "planType": None,
            }
        },
    )
    usage = reads.peek_usage(cmd)
    assert usage is not None
    assert usage.buckets[0].windows[0].pct == 11
    assert usage.credits_balance == "62500.0000000000"  # null didn't clear
    assert usage.plan == "pro"


def test_push_without_a_read_feeds_peek_but_not_get() -> None:
    reads.merge_push(
        "codex",
        {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 40}}},
    )
    usage = reads.peek_usage("codex")
    assert usage is not None and usage.buckets[0].windows[0].pct == 40
    # The aged entry is never served as a fresh /usage read.
    key = next(iter(reads._USAGE.values))
    assert reads._USAGE._fresh(key) is None


@pytest.mark.anyio
async def test_peeks_after_a_catalogue_read(
    tmp_path: Path, supported: list[str]
) -> None:
    fake = Fake(tmp_path)
    runner = fake.runner()
    cmd = runner.command()
    assert reads.peek_levels(cmd, None) is None
    assert reads.peek_effective_model(cmd, "/proj") is None
    await reads.get_catalogue(runner, "/proj")
    assert reads.peek_effective_model(cmd, "/proj") == "gpt-6.1-sol"
    assert reads.peek_effective_model(cmd, None) == "gpt-6.1-sol"
    assert reads.peek_effective_model(cmd, "/elsewhere") is None
    assert reads.peek_levels(cmd, None) == reads.CODEX_REASONING_VOCAB
    assert reads.peek_levels(cmd, "gpt-6-luna") == reads.CODEX_REASONING_VOCAB[:5]
    assert reads.peek_levels(cmd, "no-such-model") is None


def test_effective_model_falls_back_to_is_default() -> None:
    catalogue = reads.CodexCatalogue(
        models={},
        default_model="gpt-6.2-sol",
        config=reads.ConfigInfo(None, None, None),
        web_search_supported=None,
        web_search_allowed=None,
        auth_kind=None,
        plan=None,
    )
    assert reads.effective_model(catalogue) == "gpt-6.2-sol"


@pytest.mark.anyio
async def test_warm_up_runs_once_per_cwd_per_hour(
    tmp_path: Path, supported: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reads, "WARMUP_ENABLED", True)
    fake = Fake(tmp_path)
    runner = fake.runner()
    reads.maybe_warm_up(runner, "/proj")
    reads.maybe_warm_up(runner, "/proj")
    with anyio.fail_after(15):
        while reads._WARM_TASKS:
            await anyio.sleep(0.05)
    assert fake.spawn_count() == 2  # catalogue + usage, once
    assert reads.peek_effective_model(runner.command(), "/proj") == "gpt-6.1-sol"
    assert reads.peek_usage(runner.command()) is not None


def test_warm_up_off_does_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    reads.maybe_warm_up(fake.runner(), "/proj")  # WARMUP_ENABLED is off in tests
    assert reads._LAST_WARM == {}
