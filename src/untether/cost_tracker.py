"""Cost tracking and budget enforcement for Untether runs."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

# Daily cost accumulator: (date_str, total_cost).
# #379: guarded by `_daily_cost_lock` so concurrent finalize_run calls can't
# race the read-modify-write and silently lose a run's cost. The critical
# section is a single tuple assignment (sub-microsecond), so a `threading.Lock`
# is fine — both async tasks (cooperative) and threaded callers are safe.
_daily_cost: tuple[str, float] = ("", 0.0)
_daily_cost_lock = threading.Lock()

# #898: the daily total is persisted beside ``untether.toml`` so a restart
# (rollout, 03:00 reboot, crash) doesn't reset today's spend to $0. ``None``
# until ``init_daily_cost`` runs at startup; until then the total is
# memory-only (tests, path-less embedders).
DAILY_COST_FILENAME = "daily_cost.json"
_daily_cost_path: Path | None = None

# #702: fallback threshold for the budget-independent per-run spend signal.
# Matches the outlier threshold `/monitor` already applies when auditing spend,
# so the in-product signal and the audit agree on what counts as an outlier.
DEFAULT_RUN_OUTLIER_USD = 20.0


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
    # Host local time: the daily budget resets at the host's local midnight.
    return time.strftime("%Y-%m-%d")


def resolve_daily_cost_path(config_path: Path) -> Path:
    return config_path.with_name(DAILY_COST_FILENAME)


def _read_daily_cost_file(path: Path) -> tuple[str, float] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("cost_tracker.daily_load_failed", path=str(path), exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    date = raw.get("date")
    total = raw.get("total_usd")
    if (
        not isinstance(date, str)
        or isinstance(total, bool)
        or not isinstance(total, (int, float))
        or total < 0
    ):
        logger.warning("cost_tracker.daily_load_invalid", path=str(path))
        return None
    return date, float(total)


def init_daily_cost(config_path: Path) -> None:
    """Load today's persisted total (#898) and persist future records.

    Called once at startup. A file from an earlier day, a missing file or a
    corrupt one all start today at $0. Never raises.
    """
    global _daily_cost, _daily_cost_path
    path = resolve_daily_cost_path(config_path)
    loaded = _read_daily_cost_file(path)
    today = _today()
    with _daily_cost_lock:
        _daily_cost_path = path
        if loaded is not None and loaded[0] == today:
            date, total = _daily_cost
            # Keep anything recorded in-process before init (same day).
            extra = total if date == today else 0.0
            _daily_cost = (today, loaded[1] + extra)
        daily_total = _daily_cost[1] if _daily_cost[0] == today else 0.0
    logger.info("cost_tracker.daily_loaded", path=str(path), daily_total=daily_total)


def ensure_daily_cost_loaded(config_path: Path | None) -> None:
    """Initialise from ``config_path`` if startup hasn't already."""
    if _daily_cost_path is None and config_path is not None:
        init_daily_cost(config_path)


def _persist_daily_cost_locked() -> None:
    # Caller holds ``_daily_cost_lock`` so writes land in record order.
    if _daily_cost_path is None:
        return
    date, total = _daily_cost
    try:
        atomic_write_json(_daily_cost_path, {"date": date, "total_usd": total})
    except Exception:  # noqa: BLE001 — persistence must never break a run
        logger.warning(
            "cost_tracker.daily_persist_failed",
            path=str(_daily_cost_path),
            exc_info=True,
        )


def record_run_cost(cost: float) -> None:
    """Record the cost of a completed run for daily tracking."""
    global _daily_cost
    today = _today()
    with _daily_cost_lock:
        date, total = _daily_cost
        _daily_cost = (today, cost) if date != today else (today, total + cost)
        daily_total = _daily_cost[1]
        _persist_daily_cost_locked()
    logger.debug(
        "cost_tracker.recorded",
        cost=cost,
        daily_total=daily_total,
    )


def get_daily_cost() -> float:
    """Get today's accumulated cost."""
    with _daily_cost_lock:
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
