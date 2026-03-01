# Dev setup

Set up Untether for local development, run checks, and test changes safely via the dev instance. Untether works via [Telegram](https://telegram.org), available on iPhone, iPad, Android, Mac, Windows, Linux, and [Telegram Web](https://web.telegram.org).

## Clone and install

```bash
git clone https://github.com/littlebearapps/untether
cd untether

# Run directly with uv (installs deps automatically)
uv run untether --help
```

## Install as a local tool (optional)

```bash
uv tool install .
untether --help
```

## Two-instance model

Untether runs two separate instances on the same machine:

| | Production | Dev |
|---|---|---|
| **Service** | `untether.service` | `untether-dev.service` |
| **Bot** | `@your_production_bot` | `@your_dev_bot` |
| **Source** | PyPI wheel (frozen) | Local editable (`src/`) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |
| **Binary** | `~/.local/bin/untether` (pipx) | `.venv/bin/untether` (editable) |

!!! warning "Never restart production to test local changes"
    `systemctl --user restart untether` does NOT pick up local code changes — production runs a frozen PyPI wheel. Restarting production during development is always wrong and risks disrupting live chat.

## Development cycle

The standard workflow:

```bash
# 1. Edit source code
vim src/untether/telegram/commands/my_feature.py

# 2. Run checks
uv run pytest && uv run ruff check src/

# 3. Restart the dev service to pick up changes
systemctl --user restart untether-dev

# 4. Check dev service logs
journalctl --user -u untether-dev -f

# 5. Test via @your_dev_bot in Telegram
```

<!-- SCREENSHOT: journalctl output showing untether-dev starting cleanly -->

Always test via the dev bot before merging. Never send test messages to the production bot.

## Run checks

```bash
# Individual checks
uv run pytest                        # tests (Python 3.12+, 80% coverage threshold)
uv run ruff check src tests          # linting
uv run ruff format --check src tests # formatting
uv run ty check .                    # type checking (warnings only, not blocking)

# All at once
just check
```

!!! tip "Format before committing"
    Always run `uv run ruff format src/ tests/` before committing — CI checks formatting strictly.

## CI pipeline

GitHub Actions runs these checks on every push and PR:

| Job | What it checks |
|-----|---------------|
| format | `ruff format --check --diff` |
| ruff | `ruff check` with GitHub annotations |
| ty | Type checking (warnings only — 11 pre-existing warnings) |
| pytest | Tests on Python 3.12, 3.13, 3.14 with 80% coverage |
| build | `uv build` wheel + sdist validation |
| lockfile | `uv lock --check` ensures lockfile is in sync |
| pip-audit | Dependency vulnerability scanning |
| bandit | Python security static analysis |
| docs | Documentation site build |

## Test conventions

- **Framework:** pytest + anyio for async tests
- **Coverage:** 80% threshold enforced in `pyproject.toml`
- **Patterns:** Stub subprocess runners with fake CLI scripts, mock transport with `FakeTransport` dataclass
- **Key test files:** `test_claude_control.py` (56 tests), `test_callback_dispatch.py` (28 tests), `test_cost_tracker.py` (56 tests)

Run specific test files:

```bash
uv run pytest tests/test_claude_control.py -x    # stop on first failure
uv run pytest tests/test_export_command.py -v     # verbose output
uv run pytest -k "test_approve"                   # run tests matching pattern
```

## Promoting to production

Only after code is merged and released to PyPI:

```bash
# Option 1: graceful upgrade (recommended)
# Send /restart in Telegram first, wait for drain, then:
pipx upgrade untether
systemctl --user restart untether

# Option 2: direct upgrade
pipx upgrade untether && systemctl --user restart untether
```

!!! note "Graceful restart"
    Sending `/restart` in Telegram lets active runs finish before the service exits. This avoids interrupting in-progress tasks.

## Branch naming

Follow conventional branch names:

- `feature/*` — new features
- `fix/*` — bug fixes
- `docs/*` — documentation changes

## Related

- [Troubleshooting](troubleshooting.md) — common issues and debug mode
- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Contributing guide](https://github.com/littlebearapps/untether/blob/master/CONTRIBUTING.md) — full contribution guidelines
