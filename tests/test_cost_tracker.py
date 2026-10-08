"""Tests for cost tracking and budget enforcement."""

from __future__ import annotations

from untether.cost_tracker import (
    CostAlert,
    CostBudget,
    check_run_budget,
    format_cost_alert,
    get_daily_cost,
    record_run_cost,
)


def _reset_daily():
    """Reset the global daily cost tracker."""
    import untether.cost_tracker as mod

    mod._daily_cost = ("", 0.0)
    mod._daily_cost_path = None


class TestRecordRunCost:
    def setup_method(self):
        _reset_daily()

    def test_records_cost(self):
        record_run_cost(0.50)
        assert get_daily_cost() == 0.50

    def test_accumulates_cost(self):
        record_run_cost(0.50)
        record_run_cost(0.30)
        assert get_daily_cost() == 0.80

    def test_resets_on_new_day(self):
        import untether.cost_tracker as mod

        mod._daily_cost = ("1999-01-01", 99.0)
        record_run_cost(0.10)
        assert get_daily_cost() == 0.10


class TestCheckRunBudget:
    def setup_method(self):
        _reset_daily()

    def test_no_budget_returns_none(self):
        budget = CostBudget()
        assert check_run_budget(1.0, budget) is None

    def test_under_per_run_budget(self):
        budget = CostBudget(max_cost_per_run=5.0, warn_at_pct=70)
        assert check_run_budget(1.0, budget) is None

    def test_warn_per_run_budget(self):
        budget = CostBudget(max_cost_per_run=5.0, warn_at_pct=70)
        alert = check_run_budget(4.0, budget)
        assert alert is not None
        assert alert.level == "warning"
        assert "$4.00" in alert.message
        assert not alert.should_cancel

    def test_exceed_per_run_budget(self):
        budget = CostBudget(max_cost_per_run=5.0)
        alert = check_run_budget(6.0, budget)
        assert alert is not None
        assert alert.level == "exceeded"
        assert "$6.00" in alert.message

    def test_exceed_per_run_with_auto_cancel(self):
        budget = CostBudget(max_cost_per_run=5.0, auto_cancel=True)
        alert = check_run_budget(6.0, budget)
        assert alert is not None
        assert alert.should_cancel

    def test_daily_budget_warning(self):
        record_run_cost(7.0)
        budget = CostBudget(max_cost_per_day=10.0, warn_at_pct=70)
        alert = check_run_budget(0.01, budget)
        assert alert is not None
        assert alert.level == "warning"

    def test_daily_budget_exceeded(self):
        record_run_cost(11.0)
        budget = CostBudget(max_cost_per_day=10.0)
        alert = check_run_budget(0.01, budget)
        assert alert is not None
        assert alert.level == "exceeded"

    def test_zero_cost_no_alert(self):
        budget = CostBudget(max_cost_per_run=5.0)
        assert check_run_budget(0.0, budget) is None


class TestFormatCostAlert:
    def test_formats_message(self):
        alert = CostAlert(level="warning", message="test message")
        assert format_cost_alert(alert) == "test message"


class TestConcurrentRecord:
    """#379: read-modify-write under concurrent callers must not lose updates."""

    def setup_method(self):
        _reset_daily()

    def test_concurrent_record_run_cost_atomic(self):
        from concurrent.futures import ThreadPoolExecutor

        n_calls = 200
        unit_cost = 0.01

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(record_run_cost, unit_cost) for _ in range(n_calls)]
            for future in futures:
                future.result()

        # If the read-modify-write were unguarded, concurrent threads racing
        # the (today, total + cost) assignment would lose updates and the
        # observed total would be < n * unit. The lock makes this impossible.
        expected = round(n_calls * unit_cost, 2)
        observed = round(get_daily_cost(), 2)
        assert observed == expected, (
            f"lost cost updates under concurrency: "
            f"expected ${expected:.2f}, got ${observed:.2f}"
        )


# ---------------------------------------------------------------------------
# #898: daily total persists across restarts (daily_cost.json)
# ---------------------------------------------------------------------------


class TestDailyCostPersistence:
    def setup_method(self):
        _reset_daily()

    def teardown_method(self):
        _reset_daily()

    @staticmethod
    def _restart(config_path):
        """Simulate a process restart: wipe module state, then reload."""
        import untether.cost_tracker as mod

        _reset_daily()
        mod.init_daily_cost(config_path)

    def test_path_sits_beside_config(self, tmp_path):
        from untether.cost_tracker import resolve_daily_cost_path

        cfg = tmp_path / "untether.toml"
        assert resolve_daily_cost_path(cfg) == tmp_path / "daily_cost.json"

    def test_round_trip_survives_restart(self, tmp_path):
        import json
        import time

        from untether.cost_tracker import init_daily_cost

        cfg = tmp_path / "untether.toml"
        init_daily_cost(cfg)
        record_run_cost(0.40)
        record_run_cost(0.35)
        data = json.loads((tmp_path / "daily_cost.json").read_text())
        assert data["date"] == time.strftime("%Y-%m-%d")
        assert data["total_usd"] == 0.75

        self._restart(cfg)
        assert get_daily_cost() == 0.75
        record_run_cost(0.25)
        assert get_daily_cost() == 1.0

    def test_stale_date_ignored(self, tmp_path):
        import json

        from untether.cost_tracker import init_daily_cost

        cfg = tmp_path / "untether.toml"
        (tmp_path / "daily_cost.json").write_text(
            json.dumps({"date": "1999-01-01", "total_usd": 42.0})
        )
        init_daily_cost(cfg)
        assert get_daily_cost() == 0.0
        record_run_cost(0.10)
        assert get_daily_cost() == 0.10

    def test_missing_file_is_zero(self, tmp_path):
        from untether.cost_tracker import init_daily_cost

        init_daily_cost(tmp_path / "untether.toml")
        assert get_daily_cost() == 0.0

    def test_corrupt_file_is_zero(self, tmp_path):
        from untether.cost_tracker import init_daily_cost

        (tmp_path / "daily_cost.json").write_text("{not json")
        init_daily_cost(tmp_path / "untether.toml")
        assert get_daily_cost() == 0.0
        record_run_cost(0.20)
        assert get_daily_cost() == 0.20

    def test_wrong_shape_is_zero(self, tmp_path):
        import json
        import time

        from untether.cost_tracker import init_daily_cost

        (tmp_path / "daily_cost.json").write_text(
            json.dumps({"date": time.strftime("%Y-%m-%d"), "total_usd": "lots"})
        )
        init_daily_cost(tmp_path / "untether.toml")
        assert get_daily_cost() == 0.0

    def test_write_failure_never_breaks_a_run(self, tmp_path, monkeypatch):
        import untether.cost_tracker as mod
        from untether.cost_tracker import init_daily_cost

        init_daily_cost(tmp_path / "untether.toml")

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mod, "atomic_write_json", _boom)
        record_run_cost(0.30)  # must not raise
        assert get_daily_cost() == 0.30

    def test_concurrent_records_persist_final_total(self, tmp_path):
        import json
        from concurrent.futures import ThreadPoolExecutor

        from untether.cost_tracker import init_daily_cost

        cfg = tmp_path / "untether.toml"
        init_daily_cost(cfg)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for f in [pool.submit(record_run_cost, 0.01) for _ in range(100)]:
                f.result()
        assert round(get_daily_cost(), 2) == 1.0
        data = json.loads((tmp_path / "daily_cost.json").read_text())
        assert round(data["total_usd"], 2) == 1.0
        self._restart(cfg)
        assert round(get_daily_cost(), 2) == 1.0

    def test_uninitialised_stays_memory_only(self, tmp_path):
        record_run_cost(0.50)
        assert get_daily_cost() == 0.50
        assert not (tmp_path / "daily_cost.json").exists()


# ---------------------------------------------------------------------------
# #658: config.cost_visibility_gap one-shot warning (runner_bridge)
# ---------------------------------------------------------------------------


def _gap_settings(
    *,
    enabled: bool = False,
    per_run: float | None = None,
    per_day: float | None = None,
    show_api_cost: bool = False,
    show_subscription_usage: bool = True,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        cost_budget=SimpleNamespace(
            enabled=enabled,
            max_cost_per_run=per_run,
            max_cost_per_day=per_day,
        ),
        footer=SimpleNamespace(
            show_api_cost=show_api_cost,
            show_subscription_usage=show_subscription_usage,
        ),
    )


def _capture_gap_warnings(monkeypatch):
    from untether import runner_bridge

    warnings: list[tuple[str, dict]] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(runner_bridge, "logger", _Logger())
    monkeypatch.setattr(runner_bridge, "_cost_visibility_gap_warned", False)
    return warnings


def test_cost_visibility_gap_fires_once(monkeypatch) -> None:
    from untether.runner_bridge import _warn_cost_visibility_gap

    warnings = _capture_gap_warnings(monkeypatch)
    settings = _gap_settings()
    _warn_cost_visibility_gap(7.5, settings, False)
    _warn_cost_visibility_gap(3.0, settings, False)

    events = [w for w in warnings if w[0] == "config.cost_visibility_gap"]
    assert len(events) == 1
    fields = events[0][1]
    assert fields["total_cost_usd"] == 7.5
    assert fields["show_api_cost"] is False
    assert fields["show_subscription_usage"] is True
    assert fields["cost_budget_enabled"] is False


def test_cost_visibility_gap_silent_when_cost_displayed(monkeypatch) -> None:
    from untether.runner_bridge import _warn_cost_visibility_gap

    warnings = _capture_gap_warnings(monkeypatch)
    _warn_cost_visibility_gap(7.5, _gap_settings(show_api_cost=True), False)
    assert warnings == []


def test_cost_visibility_gap_silent_with_effective_budget(monkeypatch) -> None:
    from untether.runner_bridge import _warn_cost_visibility_gap

    warnings = _capture_gap_warnings(monkeypatch)
    _warn_cost_visibility_gap(7.5, _gap_settings(enabled=True, per_run=20.0), True)
    assert warnings == []


def test_cost_visibility_gap_fires_when_enabled_but_capless(monkeypatch) -> None:
    """[cost_budget] enabled=true with both caps None provides no protection
    — the gap warning must still fire."""
    from untether.runner_bridge import _warn_cost_visibility_gap

    warnings = _capture_gap_warnings(monkeypatch)
    _warn_cost_visibility_gap(7.5, _gap_settings(enabled=True), True)
    events = [w for w in warnings if w[0] == "config.cost_visibility_gap"]
    assert len(events) == 1
    assert events[0][1]["has_per_run_budget"] is False
    assert events[0][1]["has_per_day_budget"] is False


# ---------------------------------------------------------------------------
# #702: cost.run_outlier — a per-run spend signal that survives no [cost_budget]
# ---------------------------------------------------------------------------


def _outlier_settings(
    *,
    enabled: bool = False,
    warn_run_above_usd: float | None = None,
    notify_run_outlier: bool = True,
    show_api_cost: bool = False,
    show_subscription_usage: bool = True,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        cost_budget=SimpleNamespace(
            enabled=enabled,
            warn_run_above_usd=warn_run_above_usd,
            notify_run_outlier=notify_run_outlier,
        ),
        footer=SimpleNamespace(
            show_api_cost=show_api_cost,
            show_subscription_usage=show_subscription_usage,
        ),
    )


def _capture_outlier(monkeypatch, settings):
    """Capture warnings and pin the settings _check_run_cost_outlier loads."""
    from untether import runner_bridge
    from untether import settings as settings_mod

    warnings: list[tuple[str, dict]] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(runner_bridge, "logger", _Logger())
    monkeypatch.setattr(
        settings_mod, "load_settings_if_exists", lambda: (settings, None)
    )
    return warnings


def test_run_outlier_fires_without_any_budget(monkeypatch) -> None:
    """The #702 core claim: `enabled=False` (the fleet default) must no longer
    mean that no amount of spend can produce a signal."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier({"total_cost_usd": 22.06})

    events = [w for w in warnings if w[0] == "cost.run_outlier"]
    assert len(events) == 1
    fields = events[0][1]
    assert fields["total_cost_usd"] == 22.06
    assert fields["threshold_usd"] == 20.0
    assert fields["budget_configured"] is False
    assert fields["show_api_cost"] is False
    assert text is not None
    assert "$22.06" in text


def test_run_outlier_silent_below_threshold(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    assert _check_run_cost_outlier({"total_cost_usd": 14.44}) is None
    assert warnings == []


def test_run_outlier_honours_custom_threshold(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings(warn_run_above_usd=5.0))
    assert _check_run_cost_outlier({"total_cost_usd": 9.30}) is not None
    assert [w for w in warnings if w[0] == "cost.run_outlier"]


def test_run_outlier_threshold_zero_disables(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings(warn_run_above_usd=0.0))
    assert _check_run_cost_outlier({"total_cost_usd": 999.0}) is None
    assert warnings == []


def test_run_outlier_notice_opt_out_keeps_the_log(monkeypatch) -> None:
    """`notify_run_outlier = false` silences the chat line only — the log
    event is what the issue watcher ingests and must survive."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(
        monkeypatch, _outlier_settings(notify_run_outlier=False)
    )
    assert _check_run_cost_outlier({"total_cost_usd": 22.06}) is None
    assert len([w for w in warnings if w[0] == "cost.run_outlier"]) == 1


def test_run_outlier_fires_with_a_budget_configured(monkeypatch) -> None:
    """A $22 run under a $100 cap trips no budget alert but is still spend
    worth reporting."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings(enabled=True))
    assert _check_run_cost_outlier({"total_cost_usd": 22.06}) is not None
    events = [w for w in warnings if w[0] == "cost.run_outlier"]
    assert events[0][1]["budget_configured"] is True


def test_run_outlier_ignores_empty_and_zero_usage(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    assert _check_run_cost_outlier(None) is None
    assert _check_run_cost_outlier({}) is None
    assert _check_run_cost_outlier({"total_cost_usd": 0.0}) is None
    assert warnings == []


def test_run_outlier_fails_open_on_settings_error(monkeypatch) -> None:
    from untether import runner_bridge
    from untether import settings as settings_mod
    from untether.runner_bridge import _check_run_cost_outlier

    warnings: list[tuple[str, dict]] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

        def __getattr__(self, name):
            return lambda *a, **k: None

    def _boom():
        raise RuntimeError("toml exploded")

    monkeypatch.setattr(runner_bridge, "logger", _Logger())
    monkeypatch.setattr(settings_mod, "load_settings_if_exists", _boom)

    assert _check_run_cost_outlier({"total_cost_usd": 99.0}) is None
    assert [w for w in warnings if w[0] == "cost.run_outlier_check_failed"]


# ---------------------------------------------------------------------------
# #717: cost.run_outlier carries the SHAPE of the spend, not just the amount
# ---------------------------------------------------------------------------


def _claude_usage(
    *,
    cost: float,
    num_turns: int,
    duration_api_ms: int = 60_000,
    input_tokens: int = 1_200,
    output_tokens: int = 900,
    cache_read: int = 40_000,
    cache_creation: int = 5_000,
) -> dict:
    """A usage payload shaped like `_usage_payload` in runners/claude.py."""
    return {
        "total_cost_usd": cost,
        "duration_ms": duration_api_ms + 1_000,
        "duration_api_ms": duration_api_ms,
        "num_turns": num_turns,
        "subtype": "success",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }


def test_717_outlier_log_distinguishes_context_bloat_from_big_task(monkeypatch) -> None:
    """The two runs from the issue: a 2-turn $20.50 and a 40-turn $19.58.

    They fired in the same 20-minute window, on the same host, in the same
    session — and called for opposite operator responses. From the #702 log
    line alone they were indistinguishable, because the only number in it was
    the dollar amount.
    """
    from untether.runner_bridge import _check_run_cost_outlier

    # $15 threshold so both of the issue's runs clear it, as they did on the
    # host that reported them ($19.58 sits just under the $20 default).
    warnings = _capture_outlier(monkeypatch, _outlier_settings(warn_run_above_usd=15.0))
    _check_run_cost_outlier(
        _claude_usage(cost=20.50, num_turns=2, cache_read=900_000, input_tokens=1_100)
    )
    _check_run_cost_outlier(_claude_usage(cost=19.58, num_turns=40))

    events = [w[1] for w in warnings if w[0] == "cost.run_outlier"]
    assert len(events) == 2
    bloat, big_task = events

    # The discriminator: near-identical cost, an order of magnitude apart in
    # turns — and now visibly so.
    assert bloat["num_turns"] == 2
    assert big_task["num_turns"] == 40
    assert bloat["usd_per_turn"] > big_task["usd_per_turn"] * 10

    # Context bloat is self-evident from the cache-read to input ratio.
    assert bloat["cache_read_input_tokens"] == 900_000
    assert bloat["input_tokens"] == 1_100

    # Duration separates "slow and expensive" from "fast and expensive".
    assert bloat["duration_api_ms"] == 60_000


def test_717_outlier_keeps_the_702_fields(monkeypatch) -> None:
    """Additive only — #702's fields and the chat notice are unchanged."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier(_claude_usage(cost=25.56, num_turns=40))

    fields = next(w[1] for w in warnings if w[0] == "cost.run_outlier")
    assert fields["total_cost_usd"] == 25.56
    assert fields["threshold_usd"] == 20.0
    assert fields["budget_configured"] is False
    assert fields["show_api_cost"] is False
    assert fields["show_subscription_usage"] is True
    assert text is not None and "$25.56" in text


def test_717_outlier_omits_absent_shape_fields(monkeypatch) -> None:
    """A usage dict carrying only the cost (a non-Claude engine, or a result
    with no token block) must still log — with the shape fields simply
    absent rather than logged as None, so a log consumer can filter on
    presence."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    _check_run_cost_outlier({"total_cost_usd": 30.0})

    fields = next(w[1] for w in warnings if w[0] == "cost.run_outlier")
    assert fields["total_cost_usd"] == 30.0
    for absent in (
        "num_turns",
        "usd_per_turn",
        "duration_api_ms",
        "input_tokens",
        "cache_read_input_tokens",
    ):
        assert absent not in fields


def test_717_outlier_survives_a_malformed_token_block(monkeypatch) -> None:
    """The shape helper is on the delivery path — a junk `usage` sub-dict
    must never take down the run."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier(
        {
            "total_cost_usd": 21.0,
            "num_turns": "seven",  # wrong type
            "duration_api_ms": None,
            "usage": "not-a-dict",
        }
    )

    fields = next(w[1] for w in warnings if w[0] == "cost.run_outlier")
    assert fields["total_cost_usd"] == 21.0
    assert "num_turns" not in fields
    assert "usd_per_turn" not in fields
    assert "input_tokens" not in fields
    assert text is not None


def test_717_zero_turn_run_logs_turns_without_dividing(monkeypatch) -> None:
    """A 0-turn result (the #596 empty-resume shape) must not raise on the
    derived per-turn figure."""
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    _check_run_cost_outlier(_claude_usage(cost=21.0, num_turns=0))

    fields = next(w[1] for w in warnings if w[0] == "cost.run_outlier")
    assert fields["num_turns"] == 0
    assert "usd_per_turn" not in fields


# ---------------------------------------------------------------------------
# #821: the outlier notice says when background agents' spend is included
# ---------------------------------------------------------------------------


def test_run_outlier_notice_names_background_agents(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier(
        {
            "total_cost_usd": 26.94,
            "num_turns": 3,
            "background": {
                "agents": 10,
                "agents_live": 4,
                "agents_ended": 6,
                "task_ids": ["a1", "a2"],
            },
        }
    )
    assert text == (
        "\U0001f4b8 This run cost $26.94 (over the $20.00 alert) \N{EM DASH} "
        "includes spend by 10 background agents since the previous reply"
    )
    (fields,) = [kw for event, kw in warnings if event == "cost.run_outlier"]
    assert fields["bg_agents"] == 10
    assert fields["bg_agents_live"] == 4 and fields["bg_agents_ended"] == 6
    assert fields["bg_task_ids"] == ["a1", "a2"]


def test_run_outlier_notice_singular_agent(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier(
        {"total_cost_usd": 21.0, "background": {"agents": 1}}
    )
    assert text is not None
    assert text.endswith(
        "includes spend by 1 background agent since the previous reply"
    )


def test_run_outlier_without_background_has_no_clause_or_fields(monkeypatch) -> None:
    from untether.runner_bridge import _check_run_cost_outlier

    warnings = _capture_outlier(monkeypatch, _outlier_settings())
    text = _check_run_cost_outlier({"total_cost_usd": 22.06})
    assert text == "\U0001f4b8 This run cost $22.06 (over the $20.00 alert)"
    (fields,) = [kw for event, kw in warnings if event == "cost.run_outlier"]
    assert not any(key.startswith("bg_") for key in fields)
