#!/usr/bin/env python3
"""Re-derive the Codex app-server schema fixture from the installed CLI (#961).

Zero tokens: runs `codex app-server generate-json-schema` (stable and
`--experimental`) and writes the subset Untether depends on. After a re-derive,
bump `PROBED_APP_SERVER_VERSION` in `src/untether/schemas/codex_app_server.py`
and rename the fixture to match.

    uv run python scripts/codex_app_server_schema_subset.py \
        tests/fixtures/codex_app_server/schema-subset-<version>.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from untether.runners.codex_app_server import _cli_version, probe_schema_subset


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    data = {
        "codex_version": _cli_version("codex"),
        "subset": probe_schema_subset("codex"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} (codex-cli {data['codex_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
