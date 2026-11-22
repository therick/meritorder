"""Other versions of a model (routing.upgrade_versions) and thinking settings fitted to the Claude model sent to."""

import pytest
from conftest import auth

from meritorder.config import ModelConfig, Route, other_version
from meritorder.formats.thinking import budget_refused, disabled_refused, effort_for_budget, fit_thinking

SONNET = ModelConfig(name="claude-sonnet", aliases=["claude-sonnet-*", "anthropic/claude-sonnet*", "sonnet"],
                     routes=[Route(backend="anthropic", model="claude-sonnet-5-5"), Route(backend="bedrock", model="anthropic.claude-sonnet-5-5"),
                             Route(backend="openrouter", model="anthropic/claude-sonnet-5.5")])


@pytest.mark.parametrize("name, other", [
    ("claude-sonnet-4-5", True), ("claude-sonnet-4-5-20250929", True), ("anthropic/claude-sonnet-4.5", True),
    ("claude-sonnet-5-5", False), ("anthropic.claude-sonnet-5-5", False), ("anthropic/claude-sonnet-5.5", False),
    ("sonnet", False), ("claude-sonnet", False), ("claude-sonnet-latest", False),
])
def test_other_version(name, other):
    assert other_version(SONNET, name) is other


async def test_upgraded_by_default_and_said_so(client, gateway):
    r = await client.post("/v1/messages", headers=auth(), json={"model": "claude-big-0-9", "max_tokens": 10,
                                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["x-meritorder-model"] == "big" and r.headers["x-meritorder-upgraded-from"] == "claude-big-0-9"
    r = await client.post("/v1/messages", headers=auth(), json={"model": "big", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
    assert "x-meritorder-upgraded-from" not in r.headers


async def test_off_refuses_other_versions(client, gateway):
    gateway.cfg.routing.upgrade_versions = False
    try:
        r = await client.post("/v1/messages", headers=auth(), json={"model": "claude-big-0-9", "max_tokens": 10,
                                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 404
        assert "another version of big, which serves big-1" in r.text
        r = await client.post("/v1/messages", headers=auth(), json={"model": "big", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
    finally:
        gateway.cfg.routing.upgrade_versions = True


@pytest.mark.parametrize("model, refused", [
    ("claude-fable-5-2", True), ("claude-opus-5-5", True), ("claude-opus-4-8", True), ("claude-opus-4-7", True),
    ("claude-opus-4-6", False), ("claude-sonnet-5-5", True), ("anthropic.claude-sonnet-5-5", True), ("claude-sonnet-4-6", False),
    ("claude-haiku-5-5", True), ("claude-haiku-4-5", False), ("claude-opus-4-20250514", False), ("gpt-6-luna", False),
])
def test_budget_refused(model, refused):
    assert budget_refused(model) is refused


def test_enabled_becomes_adaptive_with_an_effort():
    up = {"model": "claude-sonnet-5-5", "thinking": {"type": "enabled", "budget_tokens": 10000}}
    assert fit_thinking(up, "claude-sonnet-5-5", ["low", "medium", "high", "xhigh", "max"])
    assert up["thinking"] == {"type": "adaptive"} and up["output_config"] == {"effort": "medium"}
    up = {"thinking": {"type": "enabled", "budget_tokens": 64000}, "output_config": {"effort": "low"}}
    fit_thinking(up, "claude-opus-5-5", [])
    assert up["thinking"] == {"type": "adaptive"} and up["output_config"] == {"effort": "low"}  # the client's effort stays
    up = {"thinking": {"type": "enabled", "budget_tokens": 2000}}
    assert fit_thinking(up, "claude-haiku-4-5", []) is None and up["thinking"]["budget_tokens"] == 2000  # it takes budgets


def test_disabled():
    up = {"thinking": {"type": "disabled"}}
    fit_thinking(up, "claude-sonnet-5-5", ["low", "high"])
    assert up["thinking"] == {"type": "between_tools"}
    up = {"thinking": {"type": "disabled"}}
    fit_thinking(up, "claude-opus-5-5", ["low", "high"])
    assert "thinking" not in up and up["output_config"]["effort"] == "low"
    assert disabled_refused("claude-haiku-5-5", "high") is False and disabled_refused("claude-haiku-5-5", "max") is True
    up = {"thinking": {"type": "disabled"}, "output_config": {"effort": "medium"}}
    assert fit_thinking(up, "claude-haiku-5-5", []) is None


def test_effort_for_budget_nearest_level():
    assert effort_for_budget(40000, ["low", "medium", "high"]) == "high"
    assert effort_for_budget("x", ["low"]) is None
