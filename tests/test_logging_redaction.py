"""Token redaction processor coverage (#213, prior bot-token work).

The structlog `_redact_event_dict` processor must strip:
- Telegram bot tokens (`123456789:ABCdef...` and `bot123:...`)
- OpenAI API keys (`sk-...`)
- OpenAI project keys (`sk-proj-...`) — distinct char set from generic sk- (#213)
- GitHub tokens (`ghp_`, `ghs_`, `gho_`, `github_pat_`)
- #800: `Authorization:`/`Bearer` credentials, JWTs, `api_key=`/`token=`/
  `secret=`/`password=` values — without touching token counts
"""

from __future__ import annotations

from untether.logging import _redact_event_dict, _redact_text


class TestRedactText:
    def test_redacts_telegram_bot_token(self) -> None:
        out = _redact_text("token=123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
        assert "ABCdef" not in out
        assert "[REDACTED_TOKEN]" in out

    def test_redacts_telegram_with_bot_prefix(self) -> None:
        out = _redact_text(
            "https://api.telegram.org/bot123456789:abcXYZ_token-value/getMe"
        )
        assert "abcXYZ_token" not in out
        assert "bot[REDACTED]" in out

    def test_redacts_openai_classic_key(self) -> None:
        out = _redact_text("OPENAI_API_KEY=sk-abcdefghij1234567890ABCDEF")
        assert "sk-abcdefghij" not in out
        assert "[REDACTED_KEY]" in out

    def test_redacts_openai_project_key(self) -> None:
        # #213: sk-proj- variant uses underscore/hyphen, missed by the
        # generic [A-Za-z0-9] sk- pattern.
        out = _redact_text("key=sk-proj-AbC_dEf-GhI_jKl-MnO_pQr-StU_vWx-YzAbCdEfGh")
        assert "sk-proj-AbC_dEf" not in out
        assert "[REDACTED_KEY]" in out

    def test_redacts_github_pat(self) -> None:
        out = _redact_text("token github_pat_11ABCDE0_supersecretvalue123")
        assert "supersecret" not in out
        assert "[REDACTED_TOKEN]" in out

    def test_preserves_unmatched_text(self) -> None:
        text = "Just a normal log line without any secrets at all."
        assert _redact_text(text) == text


class TestRedactEventDict:
    def test_redacts_string_values(self) -> None:
        out = _redact_event_dict(
            None, "info", {"event": "ok", "key": "sk-abc1234567890ABCDEFGH"}
        )
        assert "sk-abc" not in out["key"]
        assert "[REDACTED_KEY]" in out["key"]

    def test_redacts_nested_dict(self) -> None:
        ed = {
            "event": "error",
            "details": {"api_key": "sk-proj-aaa_bbb-ccc_ddd-eee_fff-ggg_hhh"},
        }
        out = _redact_event_dict(None, "info", ed)
        assert "sk-proj-aaa" not in out["details"]["api_key"]
        assert "[REDACTED_KEY]" in out["details"]["api_key"]

    def test_redacts_list_items(self) -> None:
        ed = {
            "event": "headers",
            "items": ["X-Foo: bar", "Authorization: sk-abc1234567890ABCDEFGH"],
        }
        out = _redact_event_dict(None, "info", ed)
        assert all("sk-abc" not in item for item in out["items"])

    def test_redacts_bytes_value(self) -> None:
        ed = {"event": "raw", "blob": b"telegram_token=987654321:UnSafe_value-xyz"}
        out = _redact_event_dict(None, "info", ed)
        assert (
            b"UnSafe_value" not in out["blob"].encode()
            if isinstance(out["blob"], str)
            else True
        )
        assert "[REDACTED_TOKEN]" in out["blob"]


# ── #800: generic bearer / JWT / key=value shapes ───────────────────────────

_FAKE_HEX = "FAKEFAKEFAKE0123456789abcdef0123"
_FAKE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"


class TestRedactGenericSecrets:
    def test_redacts_authorization_bearer_header_no_space(self) -> None:
        out = _redact_text(f"mcp --header Authorization:Bearer {_FAKE_HEX}")
        assert "FAKEFAKE" not in out
        assert out == "mcp --header Authorization:Bearer [REDACTED]"

    def test_redacts_authorization_bearer_header_with_spaces(self) -> None:
        out = _redact_text(f"authorization: bearer {_FAKE_HEX[:11]}")
        assert "FAKEFAKE" not in out
        assert out == "authorization: bearer [REDACTED]"

    def test_redacts_authorization_basic_header(self) -> None:
        out = _redact_text("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in out
        assert out == "Authorization: Basic [REDACTED]"

    def test_redacts_standalone_bearer_token(self) -> None:
        out = _redact_text(f"sending Bearer {_FAKE_HEX} upstream")
        assert "FAKEFAKE" not in out
        assert out == "sending Bearer [REDACTED] upstream"

    def test_redacts_truncated_bearer_prefix(self) -> None:
        # The nsd evidence: an 80-char cut left ~11 chars of a hex token.
        out = _redact_text("--header Authorization:Bearer 3f9a0c1b2d4")
        assert "3f9a0c1b2d4" not in out

    def test_redacts_jwt(self) -> None:
        out = _redact_text(f"got {_FAKE_JWT} back")
        assert "eyJhbGci" not in out
        assert out == "got [REDACTED_JWT] back"

    def test_redacts_truncated_jwt(self) -> None:
        # Header + partial payload, no second dot (cut by a max_len).
        out = _redact_text("id eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiO")
        assert "eyJhbGci" not in out
        assert out == "id [REDACTED_JWT]"

    def test_redacts_key_value_assignments(self) -> None:
        for text, secret in [
            ("api_key=abcd1234efgh", "abcd1234efgh"),
            ("GROQ_API_KEY=gsk_live_abc123", "gsk_live_abc123"),
            ("apikey: zyx987wvu", "zyx987wvu"),
            ("api-key=zyx987wvu", "zyx987wvu"),
            ("access_token=ya29.a0AfH6SMB", "ya29.a0AfH6SMB"),
            ("client_secret='s3cr3t-value'", "s3cr3t-value"),
            ('"password": "hunter2"', "hunter2"),
            ("password=hunter2", "hunter2"),
            ("password=correcthorse", "correcthorse"),
            ("https://x.example/sse?token=abcdef123456&x=1", "abcdef123456"),
        ]:
            out = _redact_text(text)
            assert secret not in out, (text, out)
            assert "[REDACTED]" in out, (text, out)

    def test_key_value_keeps_the_key_and_trailing_text(self) -> None:
        out = _redact_text("https://x.example/sse?token=abcdef123456&x=1")
        assert out == "https://x.example/sse?token=[REDACTED]&x=1"

    def test_does_not_double_redact_specific_patterns(self) -> None:
        out = _redact_text("token=123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
        assert out == "token=[REDACTED_TOKEN]"

    def test_preserves_token_counts_and_usage_fields(self) -> None:
        for text in [
            "usage.total_tokens=52000",
            "total_tokens=52000",
            "token_count=123",
            "input_tokens: 4096 output_tokens: 812",
            "max_tokens=8192",
            "max_token=4096",
            "cache_read_input_tokens=100",
            "tokens=12",
            "keyboard=inline",
            "monkey=banana",
            "secret=None",
            "password: null",
            "token=true",
        ]:
            assert _redact_text(text) == text, text

    def test_preserves_prose_mentioning_tokens(self) -> None:
        for text in [
            "Invalid bearer token",
            "bearer token expired",
            "Authorization failed for user",
            "token: expired",
            "context window token budget exceeded",
            "catalog.refresh_sent request_id=ut_catalog_refresh_abc_1",
            "session.summary peak_idle_seconds=12.5",
        ]:
            assert _redact_text(text) == text, text

    def test_redacts_close_grace_children_structure(self) -> None:
        # Through the structlog processor, shaped like the
        # claude.live_session.close_grace_expired event.
        ed = {
            "event": "claude.live_session.close_grace_expired",
            "child_count": 2,
            "children": [
                {"pid": 1, "wchan": "ep_poll", "cmd": "npm exec firecrawl-mcp"},
                {
                    "pid": 2,
                    "wchan": None,
                    "cmd": f"mcp --header Authorization:Bearer {_FAKE_JWT[:40]}",
                },
            ],
        }
        out = _redact_event_dict(None, "warning", ed)
        assert out["children"][0]["cmd"] == "npm exec firecrawl-mcp"
        assert "eyJhbGci" not in out["children"][1]["cmd"]
        assert out["child_count"] == 2


def test_no_log_call_passes_a_level_field() -> None:
    """structlog's ``add_log_level`` writes the log level into ``level``, so a
    ``level=`` field on a log call is silently overwritten (rc15 integration
    finding: ``run.reasoning.unsupported_level_ignored`` lost its value)."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "untether"
    methods = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
    offenders = [
        f"{path.relative_to(src)}:{node.lineno}"
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in methods
        and isinstance(node.func.value, ast.Name)
        and "log" in node.func.value.id.lower()
        and any(kw.arg == "level" for kw in node.keywords)
    ]
    assert offenders == []
