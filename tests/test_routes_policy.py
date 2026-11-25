"""Named routes (tiers) and a caller's data policy (decision 72): what Blackstart's facade relies on."""

from types import SimpleNamespace

import pytest
from conftest import auth, last_row

from meritorder import datapolicy
from meritorder.config import TierConfig

CHAT = {"max_tokens": 10, "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("tier, served", [("frontier", "big"), ("standard", "small"), ("cheap", "small")])
async def test_a_tier_routes_auto_at_its_level(client, gateway, tier, served):
    r = await client.post("/v1/messages", headers=auth(), json={**CHAT, "model": tier})
    assert r.status_code == 200, r.text
    row = await last_row(gateway)
    assert row["requested_model"] == tier and row["served_model"] == served
    assert r.headers["x-meritorder-floor"].endswith(":" + {"frontier": "5", "standard": "3", "cheap": "1"}[tier])


async def test_the_embed_tier_and_a_fixed_tier(client, gateway):
    r = await client.post("/v1/embeddings", headers=auth(), json={"model": "embed", "input": "hi"})
    assert r.status_code == 200, r.text
    assert (await last_row(gateway))["served_model"] == "embed-small"
    gateway.cfg.tiers["pinned"] = TierConfig(model="big")
    r = await client.post("/v1/messages", headers=auth(), json={**CHAT, "model": "pinned"})
    assert r.status_code == 200 and (await last_row(gateway))["served_model"] == "big"


async def test_a_models_own_name_wins_over_a_tier(client, gateway):
    gateway.cfg.tiers["small"] = TierConfig(level=5)
    r = await client.post("/v1/messages", headers=auth(), json={**CHAT, "model": "small"})
    assert (await last_row(gateway))["served_model"] == "small"


async def test_a_callers_data_policy(client, gateway):
    # The test's backends are mocks: provider "mock", nothing kept.
    r = await client.post("/v1/messages", headers={**auth(), "x-meritorder-allowed-providers": "anthropic, bedrock"},
                          json={**CHAT, "model": "standard"})
    assert r.status_code == 403 and "data_policy" in r.text and "isn't in x-meritorder-allowed-providers" in r.text, r.text
    r = await client.post("/v1/embeddings", headers={**auth(), "x-meritorder-allowed-providers": "anthropic"},
                          json={"model": "embed", "input": "hi"})
    assert r.status_code == 403 and "data_policy" in r.text
    ok = {**auth(), "x-meritorder-allowed-providers": "mock", "x-meritorder-zero-retention": "required"}
    assert (await client.post("/v1/messages", headers=ok, json={**CHAT, "model": "standard"})).status_code == 200


def backend(**kw):
    base = {"name": "b", "kind": "api", "base_url": "", "provider": None, "zero_retention": None, "trains_on_data": None,
            "flavor": "standard", "api": "chat", "project": None}
    return SimpleNamespace(**{**base, **kw})


def test_providers_and_facts():
    assert datapolicy.provider_of(backend(base_url="https://api.anthropic.com")) == "anthropic"
    assert datapolicy.provider_of(backend(base_url="https://bedrock-mantle.us-east-1.api.aws/anthropic")) == "bedrock"
    assert datapolicy.provider_of(backend(base_url="https://x.openai.azure.com/openai/v1")) == "azure_openai"
    assert datapolicy.provider_of(backend(base_url="https://x.services.ai.azure.com/anthropic")) == "foundry"
    assert datapolicy.provider_of(backend(flavor="vertex")) == "vertex"
    assert datapolicy.provider_of(backend(base_url="https://llm.internal", name="corp")) == "corp"
    assert datapolicy.provider_of(backend(provider="Anthropic", base_url="https://proxy.example")) == "anthropic"
    assert datapolicy.facts(backend(kind="self_hosted"))["zero_retention"] is True
    assert datapolicy.facts(backend(base_url="https://api.openai.com"))["source"] == "unknown"
    assert datapolicy.facts(backend(base_url="https://api.openai.com", zero_retention=True))["source"] == "config"


def test_policy_rules():
    assert datapolicy.from_headers({}) is None
    p = datapolicy.from_headers({"x-meritorder-zero-retention": "required"})
    assert p.refuses(backend(base_url="https://api.openai.com")) == ("provider openai isn't stated as zero-retention and "
                                                                       "x-meritorder-zero-retention is required")
    assert "keeps data" in p.refuses(backend(base_url="https://api.openai.com", zero_retention=False))
    assert p.refuses(backend(base_url="https://api.openai.com", zero_retention=True)) is None
    p = datapolicy.from_headers({"x-meritorder-allowed-providers": ""})  # an empty list allows none
    assert p.refuses(backend(base_url="https://api.anthropic.com"))


async def test_route_listing(client, gateway):
    assert (await client.get("/v1/routes")).status_code == 401
    out = (await client.get("/v1/routes", headers=auth())).json()
    routes = out["routes"]
    assert set(routes) == {"frontier", "standard", "cheap", "embed", "ocr"}
    assert [m["model"] for m in routes["frontier"]["candidates"]["coding"]] == ["big"]
    assert {m["model"] for m in routes["cheap"]["candidates"]["general"]} == {"big", "small"}
    [embed] = routes["embed"]["candidates"]["any"]
    route = embed["routes"][0]
    assert embed["model"] == "embed-small" and route["provider"] == "mock" and route["zero_retention"] is True
    assert route["price_per_mtok"]["input"] == 0.02 and route["available"] is True
    assert out["policy_headers"]["allowed_providers"] == "x-meritorder-allowed-providers"


async def test_a_tier_needs_a_level_or_a_model():
    with pytest.raises(ValueError):
        TierConfig()
    with pytest.raises(ValueError):
        TierConfig(level=3, model="big")
