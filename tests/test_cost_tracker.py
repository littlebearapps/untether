"""Tests for cost tracking and budget enforcement."""

from __future__ import annotations


from untether.cost_tracker import (
    CostBudget,
    CostAlert,
    check_run_budget,
    format_cost_alert,
    get_daily_cost,
    record_run_cost,
)


def _reset_daily():
    """Reset the global daily cost tracker."""
    import untether.cost_tracker as mod

    mod._daily_cost = ("", 0.0)


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
