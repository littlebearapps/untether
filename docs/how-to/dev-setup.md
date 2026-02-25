# Dev setup

Set up Untether for local development and run the checks.

## Clone and run

```bash
git clone https://github.com/littlebearapps/untether
cd untether

# Run directly with uv (installs deps automatically)
uv run untether --help
```

## Install locally (optional)

```bash
uv tool install .
untether --help
```

## Run checks

```bash
uv run pytest
uv run ruff check src tests
uv run ty check .

# Or all at once
just check
```

