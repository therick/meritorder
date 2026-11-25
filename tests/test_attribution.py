"""Attribution (decision 70): Claude Code's agent labels and a service's own fields (X-MeritOrder-Tags, up to 32) reach
the ledger, and GET /v1/capabilities tells a service built on the gateway how to use them."""

from conftest import ADMIN_KEY, auth, last_row

from meritorder.proxy import MAX_TAGS

BODY = {"model": "big", "max_tokens": 10, "messages": [{"role": "user", "content": "hello"}]}


async def test_claude_code_labels_become_tags(client, gateway):
    headers = {**auth(), "x-claude-code-session-id": "s-1", "x-claude-code-agent-id": "agent-7",
               "x-claude-code-parent-agent-id": "agent-1", "x-claude-code-agent-type": "Explore",
               "x-claude-code-request-class": "main", "x-meritorder-tags": "project=checkout"}
    r = await client.post("/v1/messages", headers=headers, json=BODY)
    assert r.status_code == 200, r.text
    row = await last_row(gateway)
    assert row["tags"] == {"cc_agent": "agent-7", "cc_parent_agent": "agent-1", "cc_agent_type": "Explore",
                           "cc_request_class": "main", "project": "checkout"}
    # Reports split cost by them like any tag.
    rows = (await client.get("/v1/reports/requests?tag=cc_agent_type=Explore", headers=auth(ADMIN_KEY))).json()["rows"]
    assert [x["id"] for x in rows] == [r.headers["x-meritorder-request-id"]]


async def test_a_client_tag_wins_over_a_claude_code_label_and_blank_labels_are_skipped(client, gateway):
    headers = {**auth(), "x-claude-code-agent-type": "  ", "x-claude-code-agent-id": "a",
               "x-meritorder-tags": "cc_agent=renamed"}
    await client.post("/v1/messages", headers=headers, json=BODY)
    assert (await last_row(gateway))["tags"] == {"cc_agent": "renamed"}


async def test_a_service_can_attribute_with_many_fields(client, gateway):
    fields = {f"f{i}": f"v{i}" for i in range(MAX_TAGS + 5)}
    header = ",".join(f"{k}={v}" for k, v in fields.items())
    await client.post("/v1/messages", headers={**auth(), "x-meritorder-tags": header}, json=BODY)
    tags = (await last_row(gateway))["tags"]
    assert len(tags) == MAX_TAGS == 32 and tags["f31"] == "v31" and "f32" not in tags


async def test_capabilities(client, gateway):
    assert (await client.get("/v1/capabilities")).status_code == 401
    caps = (await client.get("/v1/capabilities", headers=auth())).json()["capabilities"]
    assert caps["usage_in_response"] is True and caps["attribution_passthrough"]["max_tags"] == MAX_TAGS
    assert caps["cost_in_response"]["header"] == "x-meritorder-cost-usd" and caps["cost_in_response"]["streaming"] is False
    assert caps["embedding_route"] is True and caps["embedding_models"] == ["embed-small"]
    assert caps["route_listing"] == {"path": "/v1/routes", "tiers": ["cheap", "embed", "frontier", "ocr", "standard"]}
    assert caps["ocr_route"] is False  # no OCR model configured
    assert caps["data_policy_enforcement"]["refusal"] == "403 data_policy" and caps["health"] == "/readyz"
