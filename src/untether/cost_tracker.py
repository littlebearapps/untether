"""Cost tracking and budget enforcement for Untether runs."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .logging import get_logger

logger = get_logger(__name__)

# Daily cost accumulator: (date_str, total_cost)
_daily_cost: tuple[str, float] = ("", 0.0)


@dataclass(slots=True)
class CostBudget:
    max_cost_per_run: float | None = None
    max_cost_per_day: float | None = None
    warn_at_pct: int = 70
    auto_cancel: bool = False


@dataclass(frozen=True, slots=True)
class CostAlert:
    level: str  # "info", "warning", "critical", "exceeded"
    message: str
    should_cancel: bool = False
    ratio: float = 0.0  # percentage of budget used (e.g. 34.0)
    scope: str = ""  # "per_run" or "per_day"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def record_run_cost(cost: float) -> None:
    """Record the cost of a completed run for daily tracking."""
    global _daily_cost
    today = _today()
    date, total = _daily_cost
    _daily_cost = (today, cost) if date != today else (today, total + cost)
    logger.debug(
        "cost_tracker.recorded",
        cost=cost,
        daily_total=_daily_cost[1],
    )


def get_daily_cost() -> float:
    """Get today's accumulated cost."""
    date, total = _daily_cost
    if date != _today():
        return 0.0
    return total


def check_run_budget(
    run_cost: float,
    budget: CostBudget,
) -> CostAlert | None:
    """Check if a completed run's cost exceeds budget thresholds.

    Returns a CostAlert if a threshold is crossed, or None.
    """
    logger.debug(
        "cost_budget.check",
        run_cost=run_cost,
        has_per_run=budget.max_cost_per_run is not None,
        has_per_day=budget.max_cost_per_day is not None,
    )
    if budget.max_cost_per_run is not None and run_cost > 0:
        if run_cost >= budget.max_cost_per_run:
            logger.error(
                "cost_budget.exceeded",
                scope="per_run",
                run_cost=run_cost,
                budget=budget.max_cost_per_run,
                auto_cancel=budget.auto_cancel,
            )
            return CostAlert(
                level="exceeded",
                message=(
                    f"🛑 Run cost ${run_cost:.2f} exceeded "
                    f"per-run budget ${budget.max_cost_per_run:.2f}"
                ),
                should_cancel=budget.auto_cancel,
                ratio=run_cost / budget.max_cost_per_run * 100,
                scope="per_run",
            )
        ratio = run_cost / budget.max_cost_per_run * 100
        if ratio >= budget.warn_at_pct:
            logger.warning(
                "cost_budget.alert",
                scope="per_run",
                run_cost=run_cost,
                budget=budget.max_cost_per_run,
                ratio=round(ratio, 1),
            )
            return CostAlert(
                level="warning",
                message=(
                    f"⚠️ Run cost ${run_cost:.2f} is {ratio:.0f}% of "
                    f"per-run budget ${budget.max_cost_per_run:.2f}"
                ),
                ratio=ratio,
                scope="per_run",
            )

    if budget.max_cost_per_day is not None:
        daily = get_daily_cost()
        if daily >= budget.max_cost_per_day:
            logger.error(
                "cost_budget.exceeded",
                scope="per_day",
                daily_cost=daily,
                budget=budget.max_cost_per_day,
                auto_cancel=budget.auto_cancel,
            )
            return CostAlert(
                level="exceeded",
                message=(
                    f"🛑 Daily cost ${daily:.2f} exceeded "
                    f"budget ${budget.max_cost_per_day:.2f}"
                ),
                should_cancel=budget.auto_cancel,
                ratio=daily / budget.max_cost_per_day * 100,
                scope="per_day",
            )
        ratio = daily / budget.max_cost_per_day * 100
        if ratio >= budget.warn_at_pct:
            logger.warning(
                "cost_budget.alert",
                scope="per_day",
                daily_cost=daily,
                budget=budget.max_cost_per_day,
                ratio=round(ratio, 1),
            )
            return CostAlert(
                level="warning",
                message=(
                    f"⚠️ Daily cost ${daily:.2f} is {ratio:.0f}% of "
                    f"budget ${budget.max_cost_per_day:.2f}"
                ),
                ratio=ratio,
                scope="per_day",
            )

    return None


def format_cost_alert(alert: CostAlert) -> str:
    """Format a cost alert for display."""
    return alert.message
