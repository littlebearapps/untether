"""Tests for trigger configuration models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from untether.triggers.settings import (
    CronConfig,
    TriggerServerSettings,
    TriggersSettings,
    WebhookConfig,
    parse_trigger_config,
)


class TestTriggerServerSettings:
    def test_defaults(self):
        s = TriggerServerSettings()
        assert s.host == "127.0.0.1"
        assert s.port == 9876
        assert s.rate_limit == 60
        assert s.max_body_bytes == 1_048_576

    def test_custom_values(self):
        s = TriggerServerSettings(host="0.0.0.0", port=8080, rate_limit=120)
        assert s.host == "0.0.0.0"
        assert s.port == 8080
        assert s.rate_limit == 120

    def test_port_range_validation(self):
        with pytest.raises(ValidationError):
            TriggerServerSettings(port=0)
        with pytest.raises(ValidationError):
            TriggerServerSettings(port=70000)


class TestWebhookConfig:
    def test_basic_bearer(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            secret="tok_123",
            prompt_template="Hello {{name}}",
        )
        assert w.auth == "bearer"
        assert w.project is None
        assert w.engine is None
        assert w.chat_id is None

    def test_hmac_auth(self):
        w = WebhookConfig(
            id="gh",
            path="/hooks/gh",
            auth="hmac-sha256",
            secret="whsec_abc",
            prompt_template="Deploy: {{ref}}",
        )
        assert w.auth == "hmac-sha256"

    def test_no_auth(self):
        w = WebhookConfig(
            id="open",
            path="/hooks/open",
            auth="none",
            prompt_template="Ping",
        )
        assert w.auth == "none"
        assert w.secret is None

    def test_path_must_start_with_slash(self):
        with pytest.raises(ValidationError, match="must start with"):
            WebhookConfig(
                id="bad",
                path="hooks/noslash",
                secret="tok_1",
                prompt_template="Hello",
            )

    def test_path_health_rejected(self):
        with pytest.raises(ValidationError, match="reserved"):
            WebhookConfig(
                id="bad",
                path="/health",
                secret="tok_1",
                prompt_template="Hello",
            )

    def test_path_special_chars_rejected(self):
        with pytest.raises(ValidationError, match="alphanumeric"):
            WebhookConfig(
                id="bad",
                path="/hooks/<script>",
                secret="tok_1",
                prompt_template="Hello",
            )

    def test_path_valid_chars_accepted(self):
        w = WebhookConfig(
            id="ok",
            path="/hooks/my-webhook_v2.0",
            secret="tok_1",
            prompt_template="Hello",
        )
        assert w.path == "/hooks/my-webhook_v2.0"

    def test_auth_requires_secret(self):
        with pytest.raises(ValidationError, match="secret is required"):
            WebhookConfig(
                id="bad",
                path="/hooks/bad",
                auth="bearer",
                prompt_template="Fail",
            )

    def test_hmac_requires_secret(self):
        with pytest.raises(ValidationError, match="secret is required"):
            WebhookConfig(
                id="bad",
                path="/hooks/bad",
                auth="hmac-sha256",
                prompt_template="Fail",
            )

    def test_full_config(self):
        w = WebhookConfig(
            id="full",
            path="/hooks/full",
            project="myapp",
            engine="claude",
            chat_id=-100123,
            auth="bearer",
            secret="tok_xyz",
            prompt_template="Alert: {{text}}",
            event_filter="push",
        )
        assert w.project == "myapp"
        assert w.engine == "claude"
        assert w.chat_id == -100123
        assert w.event_filter == "push"


class TestCronConfig:
    def test_basic(self):
        c = CronConfig(
            id="daily",
            schedule="0 9 * * 1-5",
            prompt="Review PRs",
        )
        assert c.id == "daily"
        assert c.project is None

    def test_with_project(self):
        c = CronConfig(
            id="weekly",
            schedule="0 10 * * 1",
            project="infra",
            engine="codex",
            prompt="Check deps",
        )
        assert c.project == "infra"
        assert c.engine == "codex"

    def test_with_timezone(self):
        c = CronConfig(
            id="melb",
            schedule="0 8 * * *",
            timezone="Australia/Melbourne",
            prompt="Good morning",
        )
        assert c.timezone == "Australia/Melbourne"

    def test_timezone_none_by_default(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi")
        assert c.timezone is None

    def test_run_once_default_false(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi")
        assert c.run_once is False

    def test_run_once_true_accepted(self):
        c = CronConfig(
            id="deploy-check", schedule="0 15 * * *", prompt="Hi", run_once=True
        )
        assert c.run_once is True

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError, match="unknown timezone"):
            CronConfig(
                id="bad",
                schedule="* * * * *",
                timezone="Australia/Melborne",
                prompt="Nope",
            )

    # #330: per-cron permission_mode override
    def test_permission_mode_default_none(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi")
        assert c.permission_mode is None

    @pytest.mark.parametrize(
        "mode", ["default", "plan", "auto", "acceptEdits", "bypassPermissions"]
    )
    def test_permission_mode_accepts_valid_claude_values(self, mode: str):
        c = CronConfig(
            id="cr",
            schedule="* * * * *",
            engine="claude",
            prompt="Hi",
            permission_mode=mode,
        )
        assert c.permission_mode == mode

    def test_permission_mode_rejects_unknown_value_for_claude(self):
        with pytest.raises(ValidationError, match="unknown permission_mode"):
            CronConfig(
                id="cr",
                schedule="* * * * *",
                engine="claude",
                prompt="Hi",
                permission_mode="yoloMode",
            )

    @pytest.mark.parametrize("mode", ["workspace", "ask", "plan", "full"])
    def test_cron_antigravity_permission_mode_validated(self, mode):
        c = CronConfig(
            id="cr",
            schedule="* * * * *",
            engine="antigravity",
            prompt="Hi",
            permission_mode=mode,
        )
        assert c.permission_mode == mode

    @pytest.mark.parametrize("mode", ["yolo", "accept-edits", "bypassPermissions"])
    def test_cron_antigravity_permission_mode_rejects_unknown(self, mode):
        with pytest.raises(ValidationError, match="unknown permission_mode"):
            CronConfig(
                id="cr",
                schedule="* * * * *",
                engine="antigravity",
                prompt="Hi",
                permission_mode=mode,
            )

    def test_permission_mode_accepts_anything_for_unknown_engine(self):
        # Forward-compatible behaviour: engines not in
        # VALID_PERMISSION_MODES_BY_ENGINE accept any non-empty string; the
        # runner silently no-ops. See #331 / #332 for the path to strict
        # validation on Codex/Gemini/OpenCode/Pi/AMP.
        c = CronConfig(
            id="cr",
            schedule="* * * * *",
            engine="opencode",
            prompt="Hi",
            permission_mode="anything-goes",
        )
        assert c.permission_mode == "anything-goes"

    def test_permission_mode_accepts_anything_when_engine_unset(self):
        c = CronConfig(
            id="cr",
            schedule="* * * * *",
            prompt="Hi",
            permission_mode="anything",
        )
        assert c.permission_mode == "anything"


class TestCronModelReasoning:
    """#743: per-cron model / reasoning overrides."""

    def test_743_model_default_none(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi")
        assert c.model is None

    def test_743_reasoning_default_none(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi")
        assert c.reasoning is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("sonnet", "sonnet"),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("openai/gpt-5", "openai/gpt-5"),
            ("  haiku  ", "haiku"),
        ],
    )
    def test_743_model_accepted_free_form(self, raw: str, expected: str):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi", model=raw)
        assert c.model == expected

    @pytest.mark.parametrize(
        "bad", ["", "   ", "-p", "--model", "son net", "a\tb", "x\x07"]
    )
    def test_743_model_rejects_leading_dash_and_whitespace(self, bad: str):
        with pytest.raises(ValidationError):
            CronConfig(id="x", schedule="* * * * *", prompt="Hi", model=bad)

    @pytest.mark.parametrize(
        ("engine", "level"),
        [("claude", lvl) for lvl in ("low", "medium", "high", "xhigh", "max")]
        + [("codex", lvl) for lvl in ("low", "medium", "high", "xhigh")],
    )
    def test_743_reasoning_valid_for_explicit_engine(self, engine: str, level: str):
        c = CronConfig(
            id="x", schedule="* * * * *", prompt="Hi", engine=engine, reasoning=level
        )
        assert c.reasoning == level

    def test_cron_antigravity_reasoning_high_accepted(self):
        """#558 phase 05: agy joins the reasoning engines (low/medium/high)."""
        c = CronConfig(
            id="x",
            schedule="* * * * *",
            prompt="Hi",
            engine="antigravity",
            reasoning="high",
        )
        assert c.reasoning == "high"
        with pytest.raises(ValidationError, match="allowed values"):
            CronConfig(
                id="x",
                schedule="* * * * *",
                prompt="Hi",
                engine="antigravity",
                reasoning="max",
            )

    def test_743_reasoning_normalised_to_lower_case(self):
        c = CronConfig(
            id="x", schedule="* * * * *", prompt="Hi", engine="claude", reasoning="HIGH"
        )
        assert c.reasoning == "high"

    @pytest.mark.parametrize(
        ("engine", "level"),
        [("codex", "max"), ("claude", "minimal"), ("claude", "ultracode")],
    )
    def test_743_reasoning_rejected_for_explicit_engine(self, engine: str, level: str):
        with pytest.raises(ValidationError, match="allowed values"):
            CronConfig(
                id="x",
                schedule="* * * * *",
                prompt="Hi",
                engine=engine,
                reasoning=level,
            )

    def test_743_reasoning_rejected_for_engine_without_reasoning(self):
        with pytest.raises(ValidationError, match="not supported for engine"):
            CronConfig(
                id="x",
                schedule="* * * * *",
                prompt="Hi",
                engine="opencode",
                reasoning="high",
            )

    def test_743_reasoning_engine_unset_uses_global_table(self):
        c = CronConfig(id="x", schedule="* * * * *", prompt="Hi", reasoning="high")
        assert c.reasoning == "high"
        for bad in ("minimal", "bogus"):
            with pytest.raises(ValidationError, match="any engine"):
                CronConfig(id="x", schedule="* * * * *", prompt="Hi", reasoning=bad)

    def test_743_existing_cron_without_fields_parses_unchanged(self):
        raw = {
            "id": "nightly",
            "schedule": "0 6 * * *",
            "project": "p",
            "engine": "claude",
            "chat_id": -100,
            "prompt": "go",
            "timezone": "Australia/Melbourne",
            "run_once": False,
            "permission_mode": "auto",
        }
        c = CronConfig(**raw)
        assert c.model_dump(exclude_none=True) == raw


class TestTriggersSettings:
    def test_disabled_by_default(self):
        s = TriggersSettings()
        assert s.enabled is False
        assert s.webhooks == []
        assert s.crons == []

    def test_enabled_with_webhooks(self):
        s = TriggersSettings(
            enabled=True,
            webhooks=[
                WebhookConfig(
                    id="test",
                    path="/hooks/test",
                    secret="tok_123",
                    prompt_template="Hello",
                )
            ],
        )
        assert s.enabled is True
        assert len(s.webhooks) == 1

    def test_duplicate_webhook_ids_rejected(self):
        with pytest.raises(ValidationError, match="webhook ids must be unique"):
            TriggersSettings(
                enabled=True,
                webhooks=[
                    WebhookConfig(
                        id="dup",
                        path="/hooks/a",
                        secret="tok_1",
                        prompt_template="A",
                    ),
                    WebhookConfig(
                        id="dup",
                        path="/hooks/b",
                        secret="tok_2",
                        prompt_template="B",
                    ),
                ],
            )

    def test_duplicate_webhook_paths_rejected(self):
        with pytest.raises(ValidationError, match="webhook paths must be unique"):
            TriggersSettings(
                enabled=True,
                webhooks=[
                    WebhookConfig(
                        id="a",
                        path="/hooks/shared",
                        secret="tok_1",
                        prompt_template="A",
                    ),
                    WebhookConfig(
                        id="b",
                        path="/hooks/shared",
                        secret="tok_2",
                        prompt_template="B",
                    ),
                ],
            )

    def test_default_timezone(self):
        s = TriggersSettings(default_timezone="Australia/Melbourne")
        assert s.default_timezone == "Australia/Melbourne"

    def test_default_timezone_none_by_default(self):
        s = TriggersSettings()
        assert s.default_timezone is None

    def test_invalid_default_timezone_rejected(self):
        with pytest.raises(ValidationError, match="unknown timezone"):
            TriggersSettings(default_timezone="Not/A/Timezone")

    def test_duplicate_cron_ids_rejected(self):
        with pytest.raises(ValidationError, match="cron ids must be unique"):
            TriggersSettings(
                enabled=True,
                crons=[
                    CronConfig(id="dup", schedule="* * * * *", prompt="A"),
                    CronConfig(id="dup", schedule="0 * * * *", prompt="B"),
                ],
            )


class TestParseTriggerConfig:
    def test_parse_valid(self):
        raw = {
            "enabled": True,
            "server": {"port": 8080},
            "webhooks": [
                {
                    "id": "test",
                    "path": "/hooks/test",
                    "secret": "abc",
                    "prompt_template": "Hello",
                }
            ],
        }
        s = parse_trigger_config(raw)
        assert s.enabled is True
        assert s.server.port == 8080
        assert len(s.webhooks) == 1

    def test_parse_empty(self):
        s = parse_trigger_config({})
        assert s.enabled is False

    def test_parse_invalid_raises(self):
        with pytest.raises(ValidationError):
            parse_trigger_config({"server": {"port": "not_a_number"}})


class TestWebhookActionValidation:
    """Validate action-specific required fields."""

    def test_default_action_is_agent_run(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            prompt_template="Hello",
        )
        assert w.action == "agent_run"

    def test_agent_run_requires_prompt_template(self):
        with pytest.raises(ValidationError, match="prompt_template is required"):
            WebhookConfig(
                id="test",
                path="/hooks/test",
                auth="none",
                action="agent_run",
            )

    def test_file_write_requires_file_path(self):
        with pytest.raises(ValidationError, match="file_path is required"):
            WebhookConfig(
                id="test",
                path="/hooks/test",
                auth="none",
                action="file_write",
            )

    def test_file_write_valid(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="file_write",
            file_path="/tmp/output.json",
        )
        assert w.action == "file_write"
        assert w.file_path == "/tmp/output.json"

    def test_http_forward_requires_forward_url(self):
        with pytest.raises(ValidationError, match="forward_url is required"):
            WebhookConfig(
                id="test",
                path="/hooks/test",
                auth="none",
                action="http_forward",
            )

    def test_http_forward_valid(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="http_forward",
            forward_url="https://example.com/events",
        )
        assert w.action == "http_forward"
        assert w.forward_url == "https://example.com/events"

    def test_notify_only_requires_message_template(self):
        with pytest.raises(ValidationError, match="message_template is required"):
            WebhookConfig(
                id="test",
                path="/hooks/test",
                auth="none",
                action="notify_only",
            )

    def test_notify_only_valid(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="notify_only",
            message_template="Alert: {{event}}",
        )
        assert w.action == "notify_only"
        assert w.message_template == "Alert: {{event}}"

    def test_backward_compat_existing_config(self):
        """Existing configs without action field still work."""
        w = WebhookConfig(
            id="legacy",
            path="/hooks/legacy",
            auth="bearer",
            secret="tok_123",
            prompt_template="Hello {{name}}",
        )
        assert w.action == "agent_run"
        assert w.prompt_template == "Hello {{name}}"

    def test_forward_headers_accepted(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="http_forward",
            forward_url="https://example.com",
            forward_headers={"Authorization": "Bearer tok_123"},
        )
        assert w.forward_headers == {"Authorization": "Bearer tok_123"}

    def test_on_conflict_values(self):
        for conflict in ("overwrite", "append_timestamp", "error"):
            w = WebhookConfig(
                id="test",
                path="/hooks/test",
                auth="none",
                action="file_write",
                file_path="/tmp/out.json",
                on_conflict=conflict,
            )
            assert w.on_conflict == conflict

    def test_notify_flags(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="file_write",
            file_path="/tmp/out.json",
            notify_on_success=True,
            notify_on_failure=True,
        )
        assert w.notify_on_success is True
        assert w.notify_on_failure is True

    def test_multipart_defaults(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            action="file_write",
            file_path="/tmp/out.json",
        )
        assert w.accept_multipart is False
        assert w.file_destination is None
        assert w.max_file_size_bytes == 52_428_800

    def test_multipart_enabled(self):
        w = WebhookConfig(
            id="test",
            path="/hooks/test",
            auth="none",
            prompt_template="Process {{form.batch_id}}",
            accept_multipart=True,
            file_destination="~/uploads/{{form.date}}/{{file.filename}}",
            max_file_size_bytes=10_000_000,
        )
        assert w.accept_multipart is True
        assert w.file_destination is not None
        assert w.max_file_size_bytes == 10_000_000


class TestCronConfigFetch:
    """Tests for CronConfig with fetch block."""

    def test_cron_with_fetch(self):
        c = CronConfig(
            id="daily",
            schedule="0 9 * * 1-5",
            prompt_template="Issues: {{fetch_result}}",
            fetch={
                "type": "http_get",
                "url": "https://api.github.com/issues",
            },
        )
        assert c.fetch is not None
        assert c.fetch.type == "http_get"
        assert c.fetch.url == "https://api.github.com/issues"

    def test_cron_prompt_or_template_required(self):
        with pytest.raises(ValidationError, match="either prompt or prompt_template"):
            CronConfig(
                id="bad",
                schedule="* * * * *",
            )

    def test_cron_prompt_template_without_fetch(self):
        c = CronConfig(
            id="test",
            schedule="* * * * *",
            prompt_template="Static template",
        )
        assert c.prompt is None
        assert c.prompt_template == "Static template"

    def test_cron_backward_compat_prompt_only(self):
        c = CronConfig(
            id="legacy",
            schedule="0 9 * * *",
            prompt="Review PRs",
        )
        assert c.prompt == "Review PRs"
        assert c.fetch is None


# ── #894: project-only triggers fall back to the transport default chat ──


def _fallback_settings(**cron_overrides) -> TriggersSettings:
    cron = {"id": "nightly", "schedule": "0 2 * * *", "prompt": "hi"}
    cron.update(cron_overrides)
    return parse_trigger_config({"enabled": True, "crons": [cron]})


_PROJECT_CHATS = {"myapp": -100555, "same": 123}


def _project_chat_id(project: str) -> int | None:
    return _PROJECT_CHATS.get(project.lower())


_WEBHOOK_WITH_PROJECT = {
    "enabled": True,
    "webhooks": [
        {
            "id": "gh",
            "path": "/hooks/gh",
            "auth": "none",
            "prompt_template": "x",
            "project": "myapp",
        }
    ],
}


class TestChatFallback:
    def test_project_cron_without_chat_id_is_flagged(self):
        from untether.triggers.settings import find_trigger_chat_fallbacks

        found = find_trigger_chat_fallbacks(
            _fallback_settings(project="myapp"),
            default_chat_id=123,
            project_chat_id=_project_chat_id,
        )
        assert [
            (f.kind, f.trigger_id, f.project, f.project_chat_id) for f in found
        ] == [("cron", "nightly", "myapp", -100555)]

    def test_project_alias_is_case_insensitive(self):
        from untether.triggers.settings import find_trigger_chat_fallbacks

        found = find_trigger_chat_fallbacks(
            _fallback_settings(project="MyApp"),
            default_chat_id=123,
            project_chat_id=_project_chat_id,
        )
        assert len(found) == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"project": "myapp", "chat_id": -100555},  # explicit chat_id
            {"project": "myapp", "chat_id": 123},  # explicit, even if default
            {"project": "same"},  # project chat IS the default chat
            {"project": "unbound"},  # project has no bound chat
            {},  # no project at all
        ],
    )
    def test_not_flagged(self, overrides):
        from untether.triggers.settings import find_trigger_chat_fallbacks

        assert (
            find_trigger_chat_fallbacks(
                _fallback_settings(**overrides),
                default_chat_id=123,
                project_chat_id=_project_chat_id,
            )
            == []
        )

    def test_webhook_flagged_too(self):
        from untether.triggers.settings import find_trigger_chat_fallbacks

        found = find_trigger_chat_fallbacks(
            parse_trigger_config(_WEBHOOK_WITH_PROJECT),
            default_chat_id=123,
            project_chat_id=_project_chat_id,
        )
        assert [(f.kind, f.trigger_id) for f in found] == [("webhook", "gh")]

    def test_warn_logs_once_per_trigger(self):
        from structlog.testing import capture_logs

        from untether.triggers.settings import warn_trigger_chat_fallbacks

        warned: set = set()
        settings = _fallback_settings(project="myapp")
        with capture_logs() as logs:
            for _ in range(3):  # startup + two reloads
                warn_trigger_chat_fallbacks(
                    settings,
                    default_chat_id=123,
                    project_chat_id=_project_chat_id,
                    warned=warned,
                )
        hits = [e for e in logs if e["event"] == "trigger.cron.chat_fallback"]
        assert len(hits) == 1
        assert hits[0]["log_level"] == "warning"
        assert hits[0]["cron_id"] == "nightly"
        assert hits[0]["project"] == "myapp"
        assert hits[0]["project_chat_id"] == -100555
        assert hits[0]["default_chat_id"] == 123

    def test_warn_webhook_event_name(self):
        from structlog.testing import capture_logs

        from untether.triggers.settings import warn_trigger_chat_fallbacks

        with capture_logs() as logs:
            warn_trigger_chat_fallbacks(
                parse_trigger_config(_WEBHOOK_WITH_PROJECT),
                default_chat_id=123,
                project_chat_id=_project_chat_id,
                warned=set(),
            )
        hits = [e for e in logs if e["event"] == "trigger.webhook.chat_fallback"]
        assert len(hits) == 1
        assert hits[0]["webhook_id"] == "gh"
