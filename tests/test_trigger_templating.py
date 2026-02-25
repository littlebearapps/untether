"""Tests for webhook prompt templating."""

from __future__ import annotations

from untether.triggers.templating import render_prompt, _UNTRUSTED_PREFIX


class TestRenderPrompt:
    def test_simple_substitution(self):
        result = render_prompt("Hello {{name}}", {"name": "Alice"})
        assert "Hello Alice" in result

    def test_nested_path(self):
        payload = {"event": {"data": {"title": "Incident #42"}}}
        result = render_prompt("Alert: {{event.data.title}}", payload)
        assert "Alert: Incident #42" in result

    def test_missing_field_renders_empty(self):
        result = render_prompt("Value: {{missing}}", {"other": "x"})
        assert "Value: " in result

    def test_deeply_nested_missing(self):
        result = render_prompt("{{a.b.c.d}}", {"a": {"b": {}}})
        assert _UNTRUSTED_PREFIX in result

    def test_no_template_vars(self):
        result = render_prompt("Plain text prompt", {})
        assert "Plain text prompt" in result

    def test_untrusted_prefix_present(self):
        result = render_prompt("Hello", {})
        assert result.startswith(_UNTRUSTED_PREFIX)

    def test_multiple_substitutions(self):
        template = "{{repo}} branch {{branch}} by {{user}}"
        payload = {"repo": "untether", "branch": "main", "user": "nathan"}
        result = render_prompt(template, payload)
        assert "untether branch main by nathan" in result

    def test_special_characters_in_values(self):
        result = render_prompt("{{msg}}", {"msg": "hello <b>world</b> & co"})
        assert "hello <b>world</b> & co" in result

    def test_numeric_value(self):
        result = render_prompt("Count: {{count}}", {"count": 42})
        assert "Count: 42" in result

    def test_null_value_renders_empty(self):
        result = render_prompt("Val: {{val}}", {"val": None})
        assert "Val: " in result

    def test_list_index_access(self):
        payload = {"items": ["first", "second"]}
        result = render_prompt("{{items.0}}", payload)
        assert "first" in result

    def test_dict_value_renders_as_string(self):
        payload = {"nested": {"key": "val"}}
        result = render_prompt("{{nested}}", payload)
        assert "key" in result
