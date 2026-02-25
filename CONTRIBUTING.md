# Contributing to Untether

Thanks for your interest in contributing to Untether! This guide covers everything you need to get started.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to uphold a welcoming, inclusive environment.

## Getting started

### Prerequisites

- **Python 3.12+** — `uv python install 3.14`
- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- At least one agent CLI on PATH for integration testing: `codex`, `claude`, `opencode`, or `pi`

### Development setup

```sh
git clone https://github.com/littlebearapps/untether.git
cd untether
uv sync --dev                    # install with dev dependencies
uv run pytest                    # verify tests pass
uv run ruff check src tests      # verify lint passes
```

For an editable install (changes take effect immediately):

```sh
pipx install -e .
```

### Running Untether locally

```sh
untether                         # starts with your ~/.untether/untether.toml
untether --debug                 # verbose logging to debug.log
untether doctor                  # validate config and connectivity
```

## Making changes

### Branch naming

Use conventional branch names:

- `feature/<description>` — new features
- `fix/<description>` — bug fixes
- `docs/<description>` — documentation changes
- `refactor/<description>` — code improvements

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add voice note transcription for Pi runner
fix: correct PTY cleanup on session timeout
docs: add OpenCode provider guide
refactor: extract event factory from claude runner
test: add cost tracker budget alert tests
```

### Code style

- **Python 3.12+** features are encouraged (match/case, type unions with `|`, etc.)
- **Ruff** for linting and formatting — run `uv run ruff check src tests` before committing
- **Australian English** in user-facing text (realise, colour, behaviour, licence)
- **Type hints** on all public functions
- **structlog** for logging
- **msgspec** for JSONL parsing
- **anyio** for async code (not raw asyncio)

### Testing

```sh
uv run pytest                           # run all tests
uv run pytest tests/test_claude_control.py -x   # run specific test file
uv run pytest -k "test_cost"            # run tests matching pattern
uv run pytest --cov                     # run with coverage report
```

**Coverage threshold**: 80% (enforced in `pyproject.toml`). New features should include tests.

Key test patterns:

- Use **stub subprocess runners** with fake CLI scripts for engine tests
- Use **`FakeTransport`** protocol doubles instead of real Telegram clients
- Verify the **3-event contract**: `StartedEvent` → `ActionEvent(s)` → `CompletedEvent`
- Use **pytest + anyio** for async tests

### Linting

```sh
uv run ruff check src tests             # lint
uv run ruff format src tests            # auto-format
```

## Architecture overview

```
Telegram ←→ TelegramPresenter ←→ RunnerBridge ←→ Runner (claude/codex/opencode/pi)
                                      |
                                 ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine-specific subprocess managers
- **RunnerBridge** (`src/untether/runner_bridge.py`) — connects runners to the transport
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — renders progress and inline keyboards
- **Commands** (`src/untether/telegram/commands/`) — in-chat command handlers

See [Architecture](docs/explanation/architecture.md) for the full breakdown.

## Adding a new engine

1. Create `src/untether/runners/myengine.py` extending `JsonlSubprocessRunner`
2. Create `src/untether/schemas/myengine.py` with msgspec structs
3. Override: `command()`, `build_args()`, `translate()`, `new_state()`
4. Export `BACKEND = EngineBackend(id="myengine", build_runner=..., cli_cmd="myengine")`
5. Register in `pyproject.toml` entry points
6. Add reference docs in `docs/reference/runners/myengine/`
7. Add tests mirroring existing runner test patterns

## Submitting changes

1. Fork the repository
2. Create a feature branch from `master`
3. Make your changes with tests
4. Verify: `uv run pytest && uv run ruff check src tests`
5. Push and open a pull request

### Pull request guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Update docs if you change user-facing behaviour
- Reference any related issues in the PR description

## Reporting issues

Use [GitHub Issues](https://github.com/littlebearapps/untether/issues) to report bugs or request features. Include:

- Untether version (`untether --version`)
- Engine and version (e.g., `claude --version`)
- Relevant config (redact your bot token!)
- Steps to reproduce
- Expected vs actual behaviour

## Questions?

- Open a [discussion](https://github.com/littlebearapps/untether/issues) on GitHub
- Join the [Telegram group](https://t.me/+qBtYAMZLW_JkYWEy)
