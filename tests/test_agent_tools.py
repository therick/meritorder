"""Agent tools per team (decision 76): the MCP servers, tools and skills Claude Code, Codex and pi offer the model, read
from their requests and, in enforce mode, left out when the team doesn't allow them."""

from conftest import ADMIN_KEY, auth, last_row

from meritorder import agenttools
from meritorder.config import AgentToolsConfig
from meritorder.formats.responses import responses_to_chat
from meritorder.formats.responses_tools import TOOLS_KEY


def policy(**kw) -> agenttools.Policy:
    gateway = kw.pop("gateway", None)
    return agenttools.Policy(AgentToolsConfig(mode="enforce", **kw), gateway)


def names(body: dict, fmt: str = "anthropic") -> list[str]:
    return [t["function"]["name"] if fmt == "openai" else t["name"] for t in body.get("tools") or []]


CLAUDE_REMINDER = """<system-reminder>
The following skills are available for use with the Skill tool:

- deploy: Ship the current branch to production.
- anthropic-skills:docx: Create and edit Word documents.
- review: Review the current diff.
</system-reminder>"""


def claude_code_body() -> dict:
    tool = lambda n: {"name": n, "description": f"{n} tool", "input_schema": {"type": "object"}}  # noqa: E731
    return {"model": "big", "max_tokens": 10, "system": [{"type": "text", "text": "You are Claude Code."}],
            "tools": [tool("Bash"), tool("WebFetch"), tool("mcp__github__create_issue"), tool("mcp__docs__search"), tool("Skill")],
            "messages": [{"role": "user", "content": [{"type": "text", "text": CLAUDE_REMINDER}, {"type": "text", "text": "fix the tests"}]}]}


def test_claude_code_tools_servers_and_skills():
    body = claude_code_body()
    out, found = agenttools.apply(body, "anthropic", policy(mcp_servers=["*", "!github"], tools=["*", "!webfetch"], skills=["*", "!deploy"]), True)
    assert names(out) == ["Bash", "mcp__docs__search", "Skill"]
    text = out["messages"][0]["content"][0]["text"]
    assert "- deploy:" not in text and "- anthropic-skills:docx: Create" in text and "- review:" in text
    assert sorted(found.removed) == [("mcp", "github"), ("skill", "deploy"), ("tool", "WebFetch")]
    assert ("skill", "anthropic-skills:docx") in found.seen and ("mcp", "docs") in found.seen
    assert body["tools"][1]["name"] == "WebFetch" and "deploy" in body["messages"][0]["content"][0]["text"]  # the original is untouched
    assert out["messages"][0]["content"][1] is body["messages"][0]["content"][1]


def test_the_same_request_every_turn():
    p = policy(mcp_servers=["docs"], skills=["review"])
    first, _ = agenttools.apply(claude_code_body(), "anthropic", p, True)
    again, _ = agenttools.apply(claude_code_body(), "anthropic", p, True)
    assert first == again  # so the prompt cache's prefix doesn't change turn to turn


def test_no_skills_takes_the_skill_tool_too():
    out, found = agenttools.apply(claude_code_body(), "anthropic", policy(skills=[]), True)
    assert "Skill" not in names(out)
    text = out["messages"][0]["content"][0]["text"]
    assert "The following skills are available" in text and "- review" not in text and "- deploy" not in text
    assert ("tool", "Skill") in found.removed


def test_shadow_changes_nothing():
    body = claude_code_body()
    out, found = agenttools.apply(body, "anthropic", policy(mcp_servers=["!*"]), False)
    assert out is body
    assert found.removed == [("mcp", "github"), ("mcp", "docs")]


def test_only_servers_through_the_gateway():
    out, found = agenttools.apply(claude_code_body(), "anthropic", policy(gateway_mcp_only=True, gateway={"docs"}), True)
    assert names(out) == ["Bash", "WebFetch", "mcp__docs__search", "Skill"]
    out, _ = agenttools.apply(claude_code_body(), "anthropic", policy(gateway_mcp_only=True, gateway=set()), True)
    assert not [n for n in names(out) if n.startswith("mcp__")]


def test_tool_choice_naming_a_removed_tool():
    body = {**claude_code_body(), "tool_choice": {"type": "tool", "name": "WebFetch"}}
    out, _ = agenttools.apply(body, "anthropic", policy(tools=["*", "!WebFetch"]), True)
    assert out["tool_choice"] == {"type": "auto"}


CODEX_SKILLS = """<skills_instructions>
## Skills
A skill is a set of local instructions to follow that is stored in a `SKILL.md` file.
### Available skills
- deploy: Ship it. (file: r0/deploy/SKILL.md)
- lint-fix: Fix lint errors. (file: r0/lint-fix/SKILL.md)
### How to use skills
- Discovery: The list above is the skills available in this session.
</skills_instructions>"""


def codex_body() -> dict:
    """A Codex turn as it sends it: its MCP servers' tools in namespaces, and its skills in the instructions."""
    fn = lambda n: {"type": "function", "name": n, "description": n, "parameters": {"type": "object", "properties": {}}}  # noqa: E731
    return {"model": "big", "instructions": "You are Codex.", "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": CODEX_SKILLS}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the lint"}]}],
        "tools": [fn("shell"), {"type": "custom", "name": "apply_patch", "description": "patch", "format": {"type": "text"}},
                  {"type": "namespace", "name": "mcp__linear__", "description": "Linear", "tools": [fn("get_issue"), fn("create_issue")]},
                  {"type": "namespace", "name": "mcp__docs__", "description": "Docs", "tools": [fn("search")]},
                  {"type": "web_search", "external_web_access": False}]}


def test_codex_namespaces_and_skills():
    chat = responses_to_chat(codex_body())
    assert "mcp__linear__get_issue" in names(chat, "openai")
    out, found = agenttools.apply(chat, "openai", policy(mcp_servers=["*", "!linear"], tools=["*", "!web_search"], skills=["*", "!deploy"]), True)
    assert names(out, "openai") == ["shell", "apply_patch", "mcp__docs__search"]
    native = out[TOOLS_KEY]["native"]
    assert [t.get("name") or t["type"] for t in native] == ["shell", "apply_patch", "mcp__docs__"]  # what a Responses backend is sent
    assert not out[TOOLS_KEY]["hosted"]
    dev = next(m for m in out["messages"] if m["role"] in ("developer", "system") and "skills_instructions" in str(m["content"]))
    text = dev["content"] if isinstance(dev["content"], str) else dev["content"][0]["text"]
    assert "- deploy:" not in text and "- lint-fix: Fix lint errors." in text and "### How to use skills" in text
    assert ("skill", "deploy") in found.removed and ("mcp", "linear") in found.removed and ("tool", "web_search") in found.removed


def test_codex_namespace_partly_allowed():
    """A namespace keeps the tools still allowed (MCP servers are allowed or not as a whole, so this is a tool rule)."""
    chat = responses_to_chat(codex_body())
    out, _ = agenttools.apply(chat, "openai", policy(tools=["*", "!mcp__linear__create_issue"]), True)
    # An MCP tool's name is its server's: a tool rule doesn't reach it, so both Linear tools stay.
    assert "mcp__linear__create_issue" in names(out, "openai")


PI_SYSTEM = """You are pi.
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.

<available_skills>
  <skill>
    <name>deploy</name>
    <description>Ship it.</description>
    <location>/home/me/.pi/skills/deploy/SKILL.md</location>
  </skill>
  <skill>
    <name>pdf</name>
    <description>Work with PDFs.</description>
    <location>/home/me/.pi/skills/pdf/SKILL.md</location>
  </skill>
</available_skills>"""


def test_pi_skills():
    body = {"model": "big", "messages": [{"role": "system", "content": PI_SYSTEM}, {"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("read", "bash", "edit", "write")]}
    out, found = agenttools.apply(body, "openai", policy(skills=["pdf"], tools=["*", "!write"]), True)
    text = out["messages"][0]["content"]
    assert "<name>deploy</name>" not in text and "<name>pdf</name>" in text and text.rstrip().endswith("</available_skills>")
    assert names(out, "openai") == ["read", "bash", "edit"]
    assert sorted(found.removed) == [("skill", "deploy"), ("tool", "write")]


def test_skills_seen_even_when_not_limited():
    _, found = agenttools.apply(claude_code_body(), "anthropic", policy(), True)
    assert {("skill", "deploy"), ("skill", "review"), ("mcp", "github"), ("tool", "Bash")} <= found.seen
    assert found.removed == []


async def test_enforced_through_the_gateway(client, gateway, monkeypatch):
    team = gateway.cfg.team("default")
    team.agent_tools = AgentToolsConfig(mode="enforce", mcp_servers=["*", "!github"], skills=["*", "!deploy"])
    sent: list[dict] = []
    build = gateway.build_upstream

    def capture(send_body, *a, **kw):
        sent.append(send_body)
        return build(send_body, *a, **kw)

    monkeypatch.setattr(gateway, "build_upstream", capture)
    try:
        r = await client.post("/v1/messages", headers={**auth(), "x-session-id": "tools-1"}, json=claude_code_body())
        assert r.status_code == 200, r.text
        assert names(sent[-1]) == ["Bash", "WebFetch", "mcp__docs__search", "Skill"]
        assert "- deploy:" not in sent[-1]["messages"][0]["content"][0]["text"]
        assert (await last_row(gateway))["tags"] == {"agent_tools": "removed:2"}
        assert "meritorder_agent_tools_removed_total" in gateway.metrics.render()
        await gateway.drain()
        r = await client.get("/v1/admin/agent-tools", headers=auth(ADMIN_KEY))
        assert r.status_code == 200, r.text
        default = next(t for t in r.json()["teams"] if t["team"] == "default")
        seen = {(s["kind"], s["name"]): s["left_out"] for s in default["seen"]}
        assert seen[("mcp", "github")] is True and seen[("mcp", "docs")] is False and seen[("skill", "deploy")] is True
        assert default["policy"]["mode"] == "enforce"
        # A client can't set the tag itself.
        team.agent_tools = AgentToolsConfig()
        await client.post("/v1/messages", headers={**auth(), "x-meritorder-tags": "agent_tools=none"},
                          json={"model": "big", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
        assert (await last_row(gateway))["tags"] == {}
    finally:
        team.agent_tools = AgentToolsConfig()


DEFERRED = """<system-reminder>
The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded. Use ToolSearch to load them:
CronCreate
WebFetch
mcp__blocked__ask
mcp__docs__search
</system-reminder>"""


def test_claude_code_deferred_tools_and_tool_search():
    """Claude Code lists deferred tools by name in a reminder and loads them with ToolSearch: both are filtered, so a
    tool left out can't be found and loaded again."""
    fn = lambda n: '<function>{"description": "d \\\\"name\\\\": \\\\"x\\\\"", "name": "%s", "parameters": {}}</function>' % n  # noqa: E731
    body = {**claude_code_body(), "messages": [
        {"role": "user", "content": [{"type": "text", "text": DEFERRED}, {"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "s1", "name": "ToolSearch", "input": {"query": "wiki"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s1", "content": [
            {"type": "tool_reference", "tool_name": "mcp__blocked__ask"}, {"type": "tool_reference", "tool_name": "mcp__docs__search"}]}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "s2", "name": "ToolSearch", "input": {"query": "fetch"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s2",
                                      "content": "<functions>\n" + fn("WebFetch") + "\n" + fn("CronCreate") + "\n</functions>"}]}]}
    out, found = agenttools.apply(body, "anthropic", policy(mcp_servers=["*", "!blocked"], tools=["*", "!WebFetch"]), True)
    listed = out["messages"][0]["content"][0]["text"].split("\n")
    assert "WebFetch" not in listed and "mcp__blocked__ask" not in listed and "mcp__docs__search" in listed and "CronCreate" in listed
    assert out["messages"][2]["content"][0]["content"] == [{"type": "tool_reference", "tool_name": "mcp__docs__search"}]
    loaded = out["messages"][4]["content"][0]["content"]
    assert '"name": "WebFetch"' not in loaded and '"name": "CronCreate"' in loaded
    assert ("mcp", "blocked") in found.removed and ("tool", "WebFetch") in found.removed
    assert out["messages"][1] is body["messages"][1]  # the model's own turns are never changed
