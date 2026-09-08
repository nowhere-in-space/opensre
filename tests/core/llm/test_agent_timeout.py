"""The per-request ceiling for an agent call has to be settable.

Ninety seconds fits a hosted vendor API. Against a regional or self-hosted
endpoint serving a large model the first call - the one carrying every tool
schema - runs longer than that, and the retry that follows redoes the same work
and meets the same ceiling. The turn then costs three timeouts and answers
nothing, which is strictly worse than waiting once.
"""

from __future__ import annotations

import pytest

from config.constants.llm import AGENT_TIMEOUT_ENV
from core.llm.shared.openai_chat_completions import (
    DEFAULT_AGENT_CLIENT_TIMEOUT_SEC,
    agent_client_timeout_sec,
)


def test_the_default_is_unchanged_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENT_TIMEOUT_ENV, raising=False)

    assert agent_client_timeout_sec() == DEFAULT_AGENT_CLIENT_TIMEOUT_SEC


def test_a_slow_endpoint_can_be_given_more_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, "300")

    assert agent_client_timeout_sec() == 300.0


def test_the_value_is_read_per_call_not_bound_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env file loads after this module is imported, so import time is too early."""
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, "120")
    first = agent_client_timeout_sec()
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, "240")

    assert (first, agent_client_timeout_sec()) == (120.0, 240.0)


@pytest.mark.parametrize("value", ["", "   ", "abc", "0", "-30"])
def test_an_unusable_value_falls_back_instead_of_failing(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in an env file must not stop the agent from answering."""
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, value)

    assert agent_client_timeout_sec() == DEFAULT_AGENT_CLIENT_TIMEOUT_SEC
