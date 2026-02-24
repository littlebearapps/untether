# Repo map

Quick pointers for navigating the Untether codebase.

## Where things start

- CLI entry point: `src/untether/cli.py`
- Telegram backend entry point: `src/untether/telegram/backend.py`
- Telegram bridge loop: `src/untether/telegram/bridge.py`
- Transport-agnostic handler: `src/untether/runner_bridge.py`

## Core concepts

- Domain types (resume tokens, events, actions): `src/untether/model.py`
- Runner protocol: `src/untether/runner.py`
- Router selection and resume polling: `src/untether/router.py`
- Per-thread scheduling: `src/untether/scheduler.py`
- Progress reduction and rendering: `src/untether/progress.py`, `src/untether/markdown.py`

## Engines and streaming

- Runner implementations: `src/untether/runners/*`
- JSONL decoding schemas: `src/untether/schemas/*`

## Plugins

- Public API boundary (`untether.api`): `src/untether/api.py`
- Entrypoint discovery + lazy loading: `src/untether/plugins.py`
- Engine/transport/command backend loading: `src/untether/engines.py`, `src/untether/transports.py`, `src/untether/commands.py`

## Configuration

- Settings model + TOML/env loading: `src/untether/settings.py`
- Config migrations: `src/untether/config_migrations.py`

## Docs and contracts

- Normative behavior: [Specification](../specification.md)
- Runner invariants: `tests/test_runner_contract.py`

