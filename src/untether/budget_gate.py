"""``[cost_budget] auto_cancel`` enforcement — "Stop at limit" (#896).

Budgets are checked when a result arrives (Claude reports cost only then), so
enforcement happens at the two points where Untether can act without killing
a reply in progress:

* **Daily gate (before a run):** once today's total (persisted, #898) reaches
  ``max_cost_per_day``, new runs are refused. Attended chats get a one-shot
  **Run anyway** button; crons and webhooks are skipped with a notice.
* **Turn boundary (after a result):** when a live session's cumulative spend
  passes ``max_cost_per_run`` (or the day's total reaches
  ``max_cost_per_day``), the reply is delivered and the session is closed so
  no further wake turns or follow-ups add spend.

Both are active only when budgets are enabled AND ``auto_cancel`` is on —
per-chat ``/config`` overrides first, then ``[cost_budget]``. This module is
policy only; the Telegram side lives in ``telegram/budget_notice.py``.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from .cost_tracker import CostBudget, get_daily_cost
from .logging import get_logger

logger = get_logger(__name__)

type StopScope = Literal["per_run", "per_day"]
type RunStop = tuple[StopScope, float, float]  # (scope, spent, limit)

RUN_ANYWAY_PREFIX = "budget:run:"
RUN_ANYWAY_LABEL = "Run anyway"
BUDGET_STOP_REASON = "budget_stop"


def effective_budget(run_options: Any = None) -> CostBudget | None:
    """The chat's budget, or ``None`` when budgets are off for it.

    Same precedence as ``runner_bridge._check_cost_budget``: a per-chat
    ``budget_enabled`` / ``budget_auto_cancel`` override wins over the global
    ``[cost_budget]`` value. Fails open (``None``) on any settings error.
    """
    try:
        from .settings import load_settings_if_exists

        result = load_settings_if_exists()
    except Exception:  # noqa: BLE001 — a broken config must not block runs
        logger.warning("cost_budget.settings_unreadable", exc_info=True)
        return None
    if result is None:
        return None
    cfg = result[0].cost_budget
    enabled = getattr(run_options, "budget_enabled", None)
    if enabled is None:
        enabled = cfg.enabled
    if not enabled:
        return None
    auto_cancel = getattr(run_options, "budget_auto_cancel", None)
    if auto_cancel is None:
        auto_cancel = cfg.auto_cancel
    return CostBudget(
        max_cost_per_run=cfg.max_cost_per_run,
        max_cost_per_day=cfg.max_cost_per_day,
        warn_at_pct=cfg.warn_at_pct,
        auto_cancel=bool(auto_cancel),
    )


def daily_gate(run_options: Any = None) -> tuple[float, float] | None:
    """``(today's cost, daily limit)`` when new runs must be refused."""
    budget = effective_budget(run_options)
    if budget is None or not budget.auto_cancel or budget.max_cost_per_day is None:
        return None
    daily = get_daily_cost()
    if daily < budget.max_cost_per_day:
        return None
    return daily, budget.max_cost_per_day


def run_stop(run_cost: float, run_options: Any = None) -> RunStop | None:
    """Whether a live session must end after the reply just delivered.

    ``run_cost`` is the run's cumulative spend (every turn of a live session),
    not the last turn's delta.
    """
    budget = effective_budget(run_options)
    if budget is None or not budget.auto_cancel:
        return None
    if budget.max_cost_per_run is not None and run_cost >= budget.max_cost_per_run:
        return "per_run", run_cost, budget.max_cost_per_run
    if budget.max_cost_per_day is not None:
        daily = get_daily_cost()
        if daily >= budget.max_cost_per_day:
            return "per_day", daily, budget.max_cost_per_day
    return None


def run_stop_text(stop: RunStop) -> str:
    scope, spent, limit = stop
    if scope == "per_run":
        return (
            f"\N{OCTAGONAL SIGN} Stopped: run cost ${spent:.2f} passed the "
            f"per-run budget ${limit:.2f}"
        )
    return (
        f"\N{OCTAGONAL SIGN} Stopped: today's cost ${spent:.2f} reached the "
        f"daily budget ${limit:.2f}"
    )


def daily_block_text(daily: float, limit: float, *, skipped: str | None) -> str:
    head = f"\N{OCTAGONAL SIGN} Daily budget reached (${daily:.2f} of ${limit:.2f})."
    if skipped is not None:
        return f"{head} Skipped {skipped}; new runs are paused until midnight."
    return f"{head} New runs are paused until midnight."


# ---------------------------------------------------------------------------
# Run anyway — one-shot, chat-scoped (#388 pattern)
# ---------------------------------------------------------------------------

_PENDING_MAX = 64
_PENDING_TTL_S = 24 * 3600.0


@dataclass(frozen=True, slots=True)
class PendingRun:
    chat_id: int
    rerun: Callable[[], Awaitable[None]]
    created: float
    notice: str = ""


_PENDING_RUNS: dict[str, PendingRun] = {}


def _prune(now: float) -> None:
    for token in [
        t for t, p in _PENDING_RUNS.items() if now - p.created > _PENDING_TTL_S
    ]:
        del _PENDING_RUNS[token]
    while len(_PENDING_RUNS) >= _PENDING_MAX:
        del _PENDING_RUNS[next(iter(_PENDING_RUNS))]


def register_pending_run(
    chat_id: int, rerun: Callable[[], Awaitable[None]], *, notice: str = ""
) -> str:
    """Remember a refused run; returns the token for its button."""
    now = time.monotonic()
    _prune(now)
    token = secrets.token_hex(4)
    while token in _PENDING_RUNS:
        token = secrets.token_hex(4)
    _PENDING_RUNS[token] = PendingRun(
        chat_id=chat_id, rerun=rerun, created=now, notice=notice
    )
    return token


def claim_pending_run(token: str, *, chat_id: int) -> PendingRun | None:
    """Take a refused run once. A tap from any other chat reads as expired
    and leaves the entry in place (#388: callbacks are bound to their chat)."""
    pending = _PENDING_RUNS.get(token)
    if pending is None:
        return None
    if pending.chat_id != chat_id:
        logger.warning(
            "cost_budget.run_anyway_foreign_chat",
            bound_chat_id=pending.chat_id,
            chat_id=chat_id,
        )
        return None
    if time.monotonic() - pending.created > _PENDING_TTL_S:
        del _PENDING_RUNS[token]
        return None
    del _PENDING_RUNS[token]
    return pending


def run_anyway_callback_data(token: str) -> str:
    return f"{RUN_ANYWAY_PREFIX}{token}"
