"""Token redaction processor coverage (#213, prior bot-token work).

The structlog `_redact_event_dict` processor must strip:
- Telegram bot tokens (`123456789:ABCdef...` and `bot123:...`)
- OpenAI API keys (`sk-...`)
- OpenAI project keys (`sk-proj-...`) — distinct char set from generic sk- (#213)
- GitHub tokens (`ghp_`, `ghs_`, `gho_`, `github_pat_`)
- #800: `Authorization:`/`Bearer` credentials, JWTs, `api_key=`/`token=`/
  `secret=`/`password=` values — without touching token counts
- #841: URL userinfo (`scheme://user:pass@host`) in any string field
"""

from __future__ import annotations

import time

from untether.logging import _redact_event_dict, _redact_text, _redact_value


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


class TestRedactUrlUserinfo:
    """#841: credentials embedded in a URL's userinfo are masked everywhere."""

    def test_masks_user_and_password(self) -> None:
        assert (
            _redact_text("https://alice:s3cretPass@whisper.example.com/v1")
            == "https://[REDACTED]@whisper.example.com/v1"
        )

    def test_masks_username_only_token(self) -> None:
        out = _redact_text("https://ghp_FAKEabcdefghijklmnop12@github.com/o/r.git")
        assert "ghp_FAKE" not in out
        assert out.endswith("@github.com/o/r.git")
        assert _redact_text("http://u@h/x") == "http://[REDACTED]@h/x"

    def test_masks_password_with_unescaped_at(self) -> None:
        out = _redact_text("https://u:p@ss@host/v1")
        assert out == "https://[REDACTED]@host/v1"
        assert "p@ss" not in out
        assert "ss@" not in out

    def test_masks_other_schemes_and_case(self) -> None:
        assert _redact_text("postgres://a:b@db:5432/x") == (
            "postgres://[REDACTED]@db:5432/x"
        )
        assert _redact_text("redis://:pw@h:6379") == "redis://[REDACTED]@h:6379"
        assert _redact_text("HTTPS://U:P@H/") == "HTTPS://[REDACTED]@H/"

    def test_masks_inside_longer_text_and_containers(self) -> None:
        assert _redact_text("fetch failed for url 'https://u:p@h/x'") == (
            "fetch failed for url 'https://[REDACTED]@h/x'"
        )
        nested = {
            "a": ["https://u:pw1@h/x", {"b": "ftp://v:pw2@h"}],
            "c": b"https://w:pw3@h/",
        }
        out = _redact_value(nested, memo={})
        assert "pw1" not in str(out)
        assert "pw2" not in str(out)
        assert "pw3" not in str(out)
        # processor-level: trigger fetch/action logs carry url= verbatim
        event = _redact_event_dict(
            None,
            "info",
            {"event": "triggers.fetch.ok", "url": "https://u:p@api.example/x"},
        )
        assert event["url"] == "https://[REDACTED]@api.example/x"

    def test_idempotent(self) -> None:
        once = _redact_text("see https://u:p@h/x and redis://:pw@r")
        assert _redact_text(once) == once

    def test_preserves_already_masked_userinfo(self) -> None:
        # the #679 / voice ``***@`` "credentials configured" marker survives
        assert _redact_text("https://***@h/v1") == "https://***@h/v1"
        assert _redact_text("http://***@host:8000/v1, next") == (
            "http://***@host:8000/v1, next"
        )
        # ...but a real userinfo hiding behind a fake marker does not
        out = _redact_text("https://***@evil:pw@host/")
        assert "pw" not in out

    def test_voice_endpoint_marker_survives_the_processor(self) -> None:
        from untether.telegram.voice import _log_endpoint

        endpoint = _log_endpoint("https://u:pw@stt.example.com/v1?sig=abc")
        event = _redact_event_dict(
            None,
            "error",
            {"event": "openai.transcribe.error", "endpoint": endpoint},
        )
        assert event["endpoint"] == "https://***@stt.example.com/v1"

    def test_preserves_urls_without_userinfo(self) -> None:
        for text in (
            "https://api.groq.com/openai/v1",
            "http://[::1]:8000/v1",
            "https://www.npmjs.com/package/@scope/pkg",
            "https://medium.com/@user/post",
            "user@example.com",
            "git@github.com:org/repo.git",
            "mailto:a@b.c",
            "see a@b and https://x.example/",
            '{"u":"https://h","e":"a@b"}',
            "https://x.example/?x=1@y",
            "https://x.example/#frag@x",
            "openai-default",
        ):
            assert _redact_text(text) == text, text

    def test_documented_over_redaction_url_run_into_email(self) -> None:
        # ``,`` and ``;`` are legal userinfo sub-delims, so a URL that runs
        # straight into an email with no whitespace is masked. Log-only and
        # accepted (#841 review finding 2).
        assert _redact_text("https://a.example,team@corp.com") == (
            "https://[REDACTED]@corp.com"
        )

    def test_no_quadratic_blowup(self) -> None:
        for text in (
            "a" * 200_000 + "://" + "b" * 200_000,
            "://" * 100_000,
            ("://" + "x" * 600) * 500,
        ):
            start = time.perf_counter()
            _redact_text(text)
            assert time.perf_counter() - start < 0.5
