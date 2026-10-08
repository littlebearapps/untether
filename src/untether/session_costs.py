"""Per-session cumulative cost and token ledger (#778, #776 phase 05, #419).

Claude Code's ``result.total_cost_usd`` is cumulative per *session* and is
carried across ``--resume`` (probe F12, 2026-09-27): a resumed run's result
reports the whole session's spend so far, and a live multi-turn process
reports a running total at every turn. Recording it raw over-counts every
resumed run. This ledger remembers the last cumulative value seen per
session so callers can record the delta.

Codex's ``turn.completed.usage`` has the same shape problem for tokens: it
is the thread's running total, including every earlier ``exec resume`` run
(#419). ``record_tokens`` keeps per-session token totals in the same entries
(one file, one ``engine:session`` key space) and returns the per-run delta.

Persisted to JSON beside ``untether.toml`` (like ``session_quarantine.json``)
so a restart doesn't turn the next resume into a raw-total recording.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

# Session ids stay resumable for a while; a month is a generous ceiling.
_MAX_AGE_SECONDS = 30 * 24 * 3600


def _key(engine: str, session_id: str) -> str:
    return f"{engine}:{session_id}"


# How an engine reports token usage on its CompletedEvent: a running total
# for the whole thread (Codex) or this run only (OpenCode).
type TokenScope = Literal["thread_cumulative", "per_run"]

# Token fields understood across engines. Codex (flat): input/cached_input/
# cache_write_input/output/reasoning_output. OpenCode (nested under
# ``usage["usage"]``): input/output/reasoning/cache_read/cache_write. Claude
# (nested) uses input/output plus cache_* names not listed here.
_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _int_tokens(raw: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in _TOKEN_KEYS:
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = max(0, v)
    return out


def token_counts(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    """Token counts from ``CompletedEvent.usage`` in either shape: nested
    ``{"usage": {...}}`` (Claude, OpenCode) or flat (Codex). Nested wins when
    ``usage["usage"]`` is a dict. Int values of the known token keys only
    (bool excluded, negatives clamp to 0); ``None`` when neither
    ``input_tokens`` nor ``output_tokens`` is a positive int."""
    if not isinstance(usage, Mapping):
        return None
    inner = usage.get("usage")
    source: Mapping[str, Any] = inner if isinstance(inner, Mapping) else usage
    counts = _int_tokens(source)
    if counts.get("input_tokens", 0) <= 0 and counts.get("output_tokens", 0) <= 0:
        return None
    return counts


@dataclass(frozen=True, slots=True)
class TokenDelta:
    delta: dict[str, int]  # this run
    cumulative: dict[str, int]  # session/thread total after this run
    # "ledger" | "new_session" | "baseline_unknown" | "zero_total" | "per_run"
    source: str
    runs: int


@dataclass(frozen=True, slots=True)
class SessionTokens:
    totals: dict[str, int]
    last_run: dict[str, int]
    runs: int
    last_source: str
    updated_ts: float


@dataclass(frozen=True, slots=True)
class CostDelta:
    delta: float
    cumulative: float
    # "ledger" (previous value known), "baseline" (seeded by the runner, e.g.
    # the resume-guard's absorbed result), "new_session" (no prior spend),
    # "baseline_unknown" (resumed session never seen before — raw recorded).
    source: str


@dataclass
class SessionCostLedger:
    path: Path | None
    _entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SessionCostLedger:
        entries: dict[str, dict[str, Any]] = {}
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                entries = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logger.warning("session_costs.load_failed", path=str(path), exc_info=True)
        ledger = cls(path=path, _entries=entries)
        ledger._prune()
        return ledger

    def last(self, engine: str, session_id: str) -> float | None:
        entry = self._entries.get(_key(engine, session_id))
        if entry is None or "cost" not in entry:
            # A token-only entry (#419) has no cost baseline.
            return None
        try:
            return float(entry["cost"])
        except (TypeError, ValueError):
            return None

    def record(
        self,
        engine: str,
        session_id: str,
        cumulative: float,
        *,
        resumed: bool,
        baseline: float | None = None,
    ) -> CostDelta:
        """Store ``cumulative`` for the session and return the spend since
        the previous record."""
        previous = self.last(engine, session_id)
        if previous is not None:
            source = "ledger"
        elif baseline is not None:
            previous, source = baseline, "baseline"
        elif not resumed:
            previous, source = 0.0, "new_session"
        else:
            source = "baseline_unknown"
        delta = cumulative if previous is None else max(0.0, cumulative - previous)
        # Merge (#419): an entry may also carry token totals.
        entry = self._entries.setdefault(_key(engine, session_id), {})
        entry.update(cost=float(cumulative), ts=time.time())
        self._flush()
        if source == "baseline_unknown":
            logger.info(
                "cost.baseline_unknown",
                engine=engine,
                session_id=session_id,
                recorded=cumulative,
            )
        return CostDelta(delta=delta, cumulative=cumulative, source=source)

    def record_tokens(
        self,
        engine: str,
        session_id: str,
        tokens: Mapping[str, Any],
        *,
        scope: TokenScope,
        resumed: bool,
    ) -> TokenDelta:
        """Record this run's token usage and return the per-run delta (#419).

        ``thread_cumulative``: ``tokens`` is the thread's running total. The
        delta is per field ``max(0, new - prev)`` and the stored baseline is
        the per-field max, so a transiently low total can't make a later run
        re-count tokens already recorded. An all-zero total (a turn with no
        model call) writes nothing. ``per_run``: ``tokens`` is this run's
        usage and is added to the stored session total.
        """
        key = _key(engine, session_id)
        new = _int_tokens(tokens)
        entry = self._entries.get(key)
        stored = entry.get("tokens") if entry is not None else None
        prev = _int_tokens(stored) if isinstance(stored, Mapping) else None
        runs_before = entry.get("runs") if entry is not None else None
        runs = runs_before if isinstance(runs_before, int) else 0

        if scope == "per_run":
            base = prev or {}
            delta = new
            cumulative = {
                k: base.get(k, 0) + new.get(k, 0) for k in _union_keys(base, new)
            }
            source = "per_run"
        else:
            if not any(new.values()):
                base = prev or {}
                return TokenDelta(
                    delta=dict.fromkeys(new, 0),
                    cumulative=dict(base),
                    source="zero_total",
                    runs=runs,
                )
            if prev is not None:
                source = "ledger"
            elif not resumed:
                prev, source = {}, "new_session"
            else:
                source = "baseline_unknown"
            if prev is None:
                delta = dict(new)
                cumulative = dict(new)
            else:
                delta = {k: max(0, new.get(k, 0) - prev.get(k, 0)) for k in new}
                cumulative = {
                    k: max(prev.get(k, 0), new.get(k, 0))
                    for k in _union_keys(prev, new)
                }
        runs += 1
        entry = self._entries.setdefault(key, {})
        entry.update(
            tokens=cumulative,
            last_run_tokens=delta,
            runs=runs,
            token_source=source,
            ts=time.time(),
        )
        self._flush()
        if source == "baseline_unknown":
            logger.info(
                "usage.token_baseline_unknown",
                engine=engine,
                session_id=session_id,
                recorded_input=new.get("input_tokens"),
                recorded_output=new.get("output_tokens"),
            )
        return TokenDelta(
            delta=delta, cumulative=dict(cumulative), source=source, runs=runs
        )

    def session_tokens(self, engine: str, session_id: str) -> SessionTokens | None:
        """Token totals recorded for a session, or ``None`` (#419/#417)."""
        entry = self._entries.get(_key(engine, session_id))
        if entry is None:
            return None
        totals = entry.get("tokens")
        if not isinstance(totals, Mapping):
            return None
        last_run = entry.get("last_run_tokens")
        runs = entry.get("runs")
        source = entry.get("token_source")
        ts = entry.get("ts")
        return SessionTokens(
            totals=_int_tokens(totals),
            last_run=_int_tokens(last_run) if isinstance(last_run, Mapping) else {},
            runs=runs if isinstance(runs, int) and not isinstance(runs, bool) else 0,
            last_source=source if isinstance(source, str) else "",
            updated_ts=float(ts) if isinstance(ts, (int, float)) else 0.0,
        )

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_SECONDS
        for k in [
            k
            for k, v in self._entries.items()
            if not isinstance(v.get("ts"), (int, float)) or v["ts"] < cutoff
        ]:
            del self._entries[k]

    def _flush(self) -> None:
        if self.path is None:
            return
        try:
            atomic_write_json(self.path, self._entries)
        except OSError:
            logger.warning(
                "session_costs.flush_failed", path=str(self.path), exc_info=True
            )


def _union_keys(a: Mapping[str, int], b: Mapping[str, int]) -> list[str]:
    return [k for k in _TOKEN_KEYS if k in a or k in b]


_LEDGER: SessionCostLedger | None = None


def resolve_session_costs_path(config_path: Path) -> Path:
    return config_path.with_name("session_costs.json")


def get_session_cost_ledger() -> SessionCostLedger:
    global _LEDGER
    if _LEDGER is None:
        from .settings import _resolve_config_path

        _LEDGER = SessionCostLedger.load(
            resolve_session_costs_path(_resolve_config_path(None))
        )
    return _LEDGER


def set_session_cost_ledger(ledger: SessionCostLedger | None) -> None:
    global _LEDGER
    _LEDGER = ledger
