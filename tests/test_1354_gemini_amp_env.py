"""#1354: the deprecated Gemini and AMP runners get the allowlisted env, not Untether's whole
service environment (the #198 leak #1241 closed for Codex and OpenCode)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from untether.runners.amp import AmpRunner
from untether.runners.gemini import GeminiRunner


def _gemini_env() -> dict[str, str] | None:
    runner = GeminiRunner()
    return runner.env(state=runner.new_state("hi", None))


def _amp_env() -> dict[str, str] | None:
    runner = AmpRunner()
    return runner.env(state=runner.new_state("hi", None))


ENGINES: dict[str, Callable[[], dict[str, str] | None]] = {
    "gemini": _gemini_env,
    "amp": _amp_env,
}

# Names each engine's CLI documents for sign-in / API access (plus proxies/CA bundles), checked against
# geminicli.com/docs (auth + configuration) and ampcode.com/docs/cli (+ /settings) on 2026-11-19.
ENGINE_KEEPS: dict[str, dict[str, str]] = {
    "gemini": {
        "HOME": "/home/u",  # ~/.gemini holds the OAuth login and settings
        "GEMINI_API_KEY": "k",
        "GEMINI_MODEL": "gemini-2.5-pro",
        "GOOGLE_API_KEY": "k",
        "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
        "GOOGLE_CLOUD_PROJECT": "p",
        "GOOGLE_CLOUD_PROJECT_ID": "p",  # fallback when GOOGLE_CLOUD_PROJECT is unset
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "GOOGLE_GEMINI_BASE_URL": "https://gemini.example",
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/etc/gemini/settings.json",
        "HTTPS_PROXY": "http://p:3128",
        "SSL_CERT_FILE": "/ca.pem",
    },
    "amp": {
        "HOME": "/home/u",  # ~/.config/amp / ~/.local/share/amp hold the login
        "AMP_API_KEY": "k",
        "AMP_SKIP_UPDATE_CHECK": "1",
        "NODE_EXTRA_CA_CERTS": "/ca.pem",
        "HTTPS_PROXY": "http://p:3128",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/ca.pem",
    },
}


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_is_filtered_not_inherited(engine: str) -> None:
    """Returning None means 'inherit the parent env' — the leak. It must be a dict now."""
    assert ENGINES[engine]() is not None


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_drops_unrelated_secrets(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ZZ_CANARY_SECRET",
        "STRIPE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "MY_WEBHOOK_SECRET",
    ):
        monkeypatch.setenv(name, "leak")
    env = ENGINES[engine]()
    assert env is not None
    leaked = [
        n
        for n in (
            "ZZ_CANARY_SECRET",
            "STRIPE_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "MY_WEBHOOK_SECRET",
        )
        if n in env
    ]
    assert leaked == []
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_keeps_engine_names(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = ENGINE_KEEPS[engine]
    for name, value in kept.items():
        monkeypatch.setenv(name, value)
    env = ENGINES[engine]()
    assert env is not None
    assert {k: env.get(k) for k in kept} == kept


def test_1354_engine_prefixes_do_not_cross(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each engine's own prefix is not granted to the other engine."""
    monkeypatch.setenv("AMP_API_KEY", "amp-k")
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", "/s.json")
    gemini, amp = _gemini_env(), _amp_env()
    assert gemini is not None and amp is not None
    assert "AMP_API_KEY" not in gemini
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in amp


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_honours_user_extras(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "untether.toml"
    config.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n\n[security]\n"
        'env_extra_allow = ["MY_PROVIDER_KEY"]\n'
        'env_extra_prefix_allow = ["VAULT_"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config))
    monkeypatch.setenv("MY_PROVIDER_KEY", "k")
    monkeypatch.setenv("VAULT_TOKEN", "v")
    env = ENGINES[engine]()
    assert env is not None
    assert env["MY_PROVIDER_KEY"] == "k"
    assert env["VAULT_TOKEN"] == "v"


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_then_apply_run_env(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.context import RunContext
    from untether.utils.env_policy import apply_run_env

    monkeypatch.setenv("UNTETHER__TRANSPORTS__TELEGRAM__BOT_TOKEN", "t")
    monkeypatch.setenv("UNTETHER_RUN_ID", "stale")
    ctx = RunContext(trigger_source="cron:c", run_id="r")
    env = apply_run_env(ENGINES[engine](), ctx)
    assert env is not None
    assert env["UNTETHER_RUN_ID"] == "r"
    assert not any(k.startswith("UNTETHER__") for k in env)


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_1354_env_logs_dropped_names_once(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structlog.testing import capture_logs

    from untether.utils.env_policy import _reset_log_latch_for_tests

    _reset_log_latch_for_tests()
    monkeypatch.setenv("ZZ_CANARY_SECRET", "leak-value")
    try:
        with capture_logs() as logs:
            ENGINES[engine]()
            ENGINES[engine]()
    finally:
        _reset_log_latch_for_tests()
    dropped = [
        e
        for e in logs
        if e.get("event") == "env_policy.dropped" and e.get("engine") == engine
    ]
    assert len(dropped) == 1
    assert "leak-value" not in str(dropped)
