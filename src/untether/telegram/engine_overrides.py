from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import msgspec

OverrideSource = Literal["topic_override", "chat_default", "default"]

REASONING_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")
REASONING_SUPPORTED_ENGINES = frozenset({"codex"})


ASK_QUESTIONS_SUPPORTED_ENGINES = frozenset({"claude"})

DIFF_PREVIEW_SUPPORTED_ENGINES = frozenset({"claude"})

SUBSCRIPTION_USAGE_SUPPORTED_ENGINES = frozenset({"claude"})


class EngineOverrides(msgspec.Struct, forbid_unknown_fields=False):
    model: str | None = None
    reasoning: str | None = None
    permission_mode: str | None = None
    ask_questions: bool | None = None
    diff_preview: bool | None = None
    show_api_cost: bool | None = None
    show_subscription_usage: bool | None = None


@dataclass(frozen=True, slots=True)
class OverrideValueResolution:
    value: str | None
    source: OverrideSource
    topic_value: str | None
    chat_value: str | None


def normalize_override_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_overrides(overrides: EngineOverrides | None) -> EngineOverrides | None:
    if overrides is None:
        return None
    model = normalize_override_value(overrides.model)
    reasoning = normalize_override_value(overrides.reasoning)
    permission_mode = normalize_override_value(overrides.permission_mode)
    ask_questions = overrides.ask_questions
    diff_preview = overrides.diff_preview
    show_api_cost = overrides.show_api_cost
    show_subscription_usage = overrides.show_subscription_usage
    if (
        model is None
        and reasoning is None
        and permission_mode is None
        and ask_questions is None
        and diff_preview is None
        and show_api_cost is None
        and show_subscription_usage is None
    ):
        return None
    return EngineOverrides(
        model=model,
        reasoning=reasoning,
        permission_mode=permission_mode,
        ask_questions=ask_questions,
        diff_preview=diff_preview,
        show_api_cost=show_api_cost,
        show_subscription_usage=show_subscription_usage,
    )


def merge_overrides(
    topic_override: EngineOverrides | None,
    chat_override: EngineOverrides | None,
) -> EngineOverrides | None:
    topic = normalize_overrides(topic_override)
    chat = normalize_overrides(chat_override)
    if topic is None and chat is None:
        return None
    model = None
    reasoning = None
    permission_mode = None
    if topic is not None and topic.model is not None:
        model = topic.model
    elif chat is not None:
        model = chat.model
    if topic is not None and topic.reasoning is not None:
        reasoning = topic.reasoning
    elif chat is not None:
        reasoning = chat.reasoning
    if topic is not None and topic.permission_mode is not None:
        permission_mode = topic.permission_mode
    elif chat is not None:
        permission_mode = chat.permission_mode
    ask_questions = None
    if topic is not None and topic.ask_questions is not None:
        ask_questions = topic.ask_questions
    elif chat is not None:
        ask_questions = chat.ask_questions
    diff_preview = None
    if topic is not None and topic.diff_preview is not None:
        diff_preview = topic.diff_preview
    elif chat is not None:
        diff_preview = chat.diff_preview
    show_api_cost = None
    if topic is not None and topic.show_api_cost is not None:
        show_api_cost = topic.show_api_cost
    elif chat is not None:
        show_api_cost = chat.show_api_cost
    show_subscription_usage = None
    if topic is not None and topic.show_subscription_usage is not None:
        show_subscription_usage = topic.show_subscription_usage
    elif chat is not None:
        show_subscription_usage = chat.show_subscription_usage
    return normalize_overrides(
        EngineOverrides(
            model=model,
            reasoning=reasoning,
            permission_mode=permission_mode,
            ask_questions=ask_questions,
            diff_preview=diff_preview,
            show_api_cost=show_api_cost,
            show_subscription_usage=show_subscription_usage,
        )
    )


def resolve_override_value(
    *,
    topic_override: EngineOverrides | None,
    chat_override: EngineOverrides | None,
    field: Literal["model", "reasoning"],
) -> OverrideValueResolution:
    topic_value = normalize_override_value(
        getattr(topic_override, field, None) if topic_override is not None else None
    )
    chat_value = normalize_override_value(
        getattr(chat_override, field, None) if chat_override is not None else None
    )
    if topic_value is not None:
        return OverrideValueResolution(
            value=topic_value,
            source="topic_override",
            topic_value=topic_value,
            chat_value=chat_value,
        )
    if chat_value is not None:
        return OverrideValueResolution(
            value=chat_value,
            source="chat_default",
            topic_value=topic_value,
            chat_value=chat_value,
        )
    return OverrideValueResolution(
        value=None,
        source="default",
        topic_value=topic_value,
        chat_value=chat_value,
    )


def allowed_reasoning_levels(engine: str) -> tuple[str, ...]:
    _ = engine
    return REASONING_LEVELS


def supports_reasoning(engine: str) -> bool:
    return engine in REASONING_SUPPORTED_ENGINES
