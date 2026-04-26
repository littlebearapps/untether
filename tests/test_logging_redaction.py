"""Token redaction processor coverage (#213, prior bot-token work).

The structlog `_redact_event_dict` processor must strip:
- Telegram bot tokens (`123456789:ABCdef...` and `bot123:...`)
- OpenAI API keys (`sk-...`)
- OpenAI project keys (`sk-proj-...`) — distinct char set from generic sk- (#213)
- GitHub tokens (`ghp_`, `ghs_`, `gho_`, `github_pat_`)
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
