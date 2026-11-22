"""Budget step-down (decision 74): cheaper models instead of a refusal as a budget runs out."""

import pytest
from conftest import auth, last_row

from meritorder.config import BudgetStepDown
from meritorder.proxy import _month_key

HARD = {"max_tokens": 10, "messages": [{"role": "user", "content": "the hardest work"}]}
L5 = {"x-route-floor": "general:5"}  # the client asks for level 5: the step-down caps even that


async def spend(gateway, user="alice", team="default", amount=0.0):
    m = _month_key()
    await gateway.state.add_spend([f"{m}:user:{user}", f"{m}:team:{team}", f"{m}:org:all"], amount)


@pytest.fixture
def stepping(gateway):
    team = gateway.cfg.team("default")  # team budget 100, per person 10 (conftest)
    team.budget_step_down = BudgetStepDown(at=0.8, level=1)
    yield gateway
    team.budget_step_down = None


async def test_new_tasks_go_cheaper_once_a_budget_is_nearly_spent(client, stepping):
    r = await client.post("/v1/messages", headers={**auth(), **L5, "x-session-id": "s1"}, json={**HARD, "model": "auto"})
    assert (await last_row(stepping))["served_model"] == "big"  # hard work, budget untouched
    await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})  # seed the counters
    await spend(stepping, amount=8.5)  # the person's $10: 85% spent
    r = await client.post("/v1/messages", headers={**auth(), **L5, "x-session-id": "s2"}, json={**HARD, "model": "auto"})
    assert r.status_code == 200
    row = await last_row(stepping)
    assert row["served_model"] == "small" and row["tags"]["budget_step_down"] == "1"
    assert "budget_step_down" in r.headers["x-meritorder-floor"] or r.headers["x-meritorder-floor"].endswith(":1")
    # A tier is stepped down too; a named model is served as asked.
    await client.post("/v1/messages", headers={**auth(), **L5, "x-session-id": "s3"}, json={**HARD, "model": "frontier"})
    assert (await last_row(stepping))["served_model"] == "small"
    await client.post("/v1/messages", headers={**auth(), **L5, "x-session-id": "s4"}, json={**HARD, "model": "big"})
    row = await last_row(stepping)
    assert row["served_model"] == "big" and "budget_step_down" not in row["tags"]


async def test_at_the_budget_refused_unless_grace(client, stepping):
    await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})
    await spend(stepping, amount=10.5)  # the person's $10 is used up
    r = await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})
    assert r.status_code == 429 and "monthly budget" in r.text
    stepping.cfg.team("default").budget_step_down = BudgetStepDown(at=0.8, level=1, grace_share=0.1)  # up to $11
    r = await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})
    assert r.status_code == 200 and (await last_row(stepping))["served_model"] == "small"
    # The grace is for stepped-down requests only: a named model is refused.
    assert (await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "big"})).status_code == 429
    await spend(stepping, amount=1.0)  # past 110%
    assert (await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})).status_code == 429


async def test_off_by_default(client, gateway):
    await client.post("/v1/messages", headers={**auth(), **L5}, json={**HARD, "model": "auto"})
    await spend(gateway, amount=9.5)
    await client.post("/v1/messages", headers={**auth(), **L5, "x-session-id": "s5"}, json={**HARD, "model": "auto"})
    row = await last_row(gateway)
    assert row["served_model"] == "big" and "budget_step_down" not in row["tags"]


def test_settings_are_bounded():
    with pytest.raises(ValueError):
        BudgetStepDown(at=1.5)
    with pytest.raises(ValueError):
        BudgetStepDown(level=6)
