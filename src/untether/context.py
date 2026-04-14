from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunContext:
    project: str | None = None
    branch: str | None = None
    # rc4 (#271): trigger_source is set when a run is initiated by a cron
    # or webhook (e.g. "cron:daily-review", "webhook:github-push") so the
    # Telegram meta footer can show the provenance.
    trigger_source: str | None = None
