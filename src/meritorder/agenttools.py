"""The tools, MCP servers and skills an agent brings, read from its model requests and limited per team (decision 76).

Agents load MCP servers and skills on the person's machine; the gateway never sees those servers' traffic, but every
model request carries what the agent offers the model. The three harnesses Merit Order supports name them so:

* MCP tools. Claude Code: ``mcp__<server>__<tool>``. Codex: the same, sent either flat or as a Responses ``namespace``
  named ``mcp__<server>__`` holding the server's tools (formats/responses.py flattens it to the same name; a backend
  speaking the Responses API is sent the namespaces themselves, filtered here too). pi has no MCP of its own.
* Skills. Claude Code lists them in a system reminder ("The following skills are available for use with the Skill
  tool:" then ``- name: description`` lines) and runs one with its ``Skill`` tool. Codex lists them in
  ``<skills_instructions>`` (``### Available skills``, ``- name: description (file: path)``) and reads executor and cloud
  skills with ``skills.read``. pi lists them as ``<skill><name>…</name>…</skill>`` in ``<available_skills>`` and reads the
  file with its read tool. Each is matched by its own pattern; any other text is left alone.
* Every other tool goes by its name (Bash, WebFetch, shell, apply_patch, read...).

In enforce mode what isn't allowed is left out of the request: tools from ``tools`` (and a ``tool_choice`` naming one goes
back to auto), skills from their list (and the tool that runs skills when none is left). The rules are the same every
turn, so the request's prefix, and the prompt cache, stay the same turn to turn. A skill the person names themselves
(Claude Code's /skill, Codex's $skill) reaches the model as the person's own message and isn't touched, and an agent can
still read a skill's file from disk: this limits what an agent is offered, not what a person can do on their machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .formats.responses_tools import TOOLS_KEY, chat_name

MCP_NAME = re.compile(r"^mcp__(.+?)__(.+)$")
SKILL_TOOLS = ("skill", "skills__read", "skills.read")  # the tools that run skills (lowercased), Claude Code and Codex

# Claude Code's and Codex's skill lists: a header, then one "- name: description" entry per line.
_LIST_HEADERS = ("The following skills are available for use with the Skill tool:", "### Available skills")
_LIST_END = re.compile(r"^(#{1,3} |</|<system-reminder>)")
_ENTRY = re.compile(r"^- (.{1,200}?):(?: |$)")  # plugin skills' names hold a colon: "- plugin:skill: description"
# pi's (and any <available_skills> list's) entries.
_XML_SKILL = re.compile(r"[ \t]*<skill>\s*<name>([^<]{1,200})</name>.*?</skill>[ \t]*\n?", re.S)


@dataclass
class Finding:
    """What a request carried and the team's policy left out (or, in shadow mode, would have)."""

    removed: list[tuple[str, str]] = field(default_factory=list)  # (kind, name): kind mcp, tool or skill
    seen: set[tuple[str, str]] = field(default_factory=set)  # every (kind, name) offered: MCP servers, tools, skills

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for kind, _ in self.removed:
            out[kind] = out.get(kind, 0) + 1
        return out


def identify(name: str) -> tuple[str, str]:
    """('mcp', '<server>') for an MCP tool, else ('tool', name)."""
    m = MCP_NAME.match(name or "")
    return ("mcp", m.group(1)) if m else ("tool", name or "")


class Policy:
    """A team's agent_tools settings, with the MCP servers it may reach through the gateway when only those may be used."""

    def __init__(self, cfg, gateway_servers: set[str] | None = None):
        self.cfg = cfg
        self.gateway_servers = {s.lower() for s in gateway_servers} if gateway_servers is not None else None
        self.no_skills = not any(not p.startswith("!") for p in cfg.skills)

    def allows_server(self, server: str) -> bool:
        if self.cfg.gateway_mcp_only and (self.gateway_servers is None or server.lower() not in self.gateway_servers):
            return False
        return self.cfg.allows_server(server)

    def allows_name(self, name: str) -> bool:
        kind, what = identify(name)
        if kind == "mcp":
            return self.allows_server(what)
        if name.lower() in SKILL_TOOLS and self.no_skills:
            return False
        return self.cfg.allows_tool(name)

    def allows_skill(self, name: str) -> bool:
        return not self.no_skills and self.cfg.allows_skill(name.strip())


def _note(found: Finding, name: str, allowed: bool) -> None:
    kind, what = identify(name)
    if name.lower() in SKILL_TOOLS:
        kind = "tool"
    found.seen.add((kind, what))
    if not allowed and (kind, what) not in found.removed:
        found.removed.append((kind, what))


def _tool_name(fmt: str, tool: dict) -> str:
    if fmt == "openai" and isinstance(tool.get("function"), dict):
        return str(tool["function"].get("name") or "")
    return str(tool.get("name") or tool.get("type") or "")


_DEFERRED_HEADER = "The following deferred tools are now available via ToolSearch"
_BARE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FUNCTION_LINE = re.compile(r'^<function>\{.*?"name": ?"([^"]{1,128})".*</function>$')  # escaped quotes in a description don't match


def _filter_text(text: str, policy: Policy, found: Finding) -> str:
    """The text with what isn't allowed taken out of any list of skills or tools in it (unchanged when it holds none):
    skill lists (pi's XML, Claude Code's and Codex's bullets), Claude Code's list of deferred tools, and the tool
    definitions its ToolSearch returns (``<function>{...}</function>`` lines)."""
    if "<skill>" in text:
        def xml(m: re.Match) -> str:
            name = m.group(1).strip()
            found.seen.add(("skill", name))
            if policy.allows_skill(name):
                return m.group(0)
            found.removed.append(("skill", name))
            return ""
        text = _XML_SKILL.sub(xml, text)
    lists = any(h in text for h in _LIST_HEADERS)
    deferred = _DEFERRED_HEADER in text
    functions = "<function>{" in text
    if not (lists or deferred or functions):
        return text
    out: list[str] = []
    inside = None  # "skills" or "deferred" while in such a list
    dropping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if lists and any(stripped == h for h in _LIST_HEADERS):
            inside, dropping = "skills", False
            out.append(line)
            continue
        if deferred and stripped.startswith(_DEFERRED_HEADER):
            inside, dropping = "deferred", False
            out.append(line)
            continue
        if functions and stripped.startswith("<function>{"):
            m = _FUNCTION_LINE.match(stripped)
            if m:
                ok = policy.allows_name(m.group(1))
                _note(found, m.group(1), ok)
                if not ok:
                    continue
        if inside == "skills":
            m = _ENTRY.match(stripped)
            if m:
                name = m.group(1).strip()
                found.seen.add(("skill", name))
                dropping = not policy.allows_skill(name)
                if dropping:
                    found.removed.append(("skill", name))
                    continue
            elif _LIST_END.match(stripped):
                inside, dropping = None, False
            elif dropping and stripped:
                continue  # a dropped entry's continuation line
            elif not stripped:
                dropping = False
        elif inside == "deferred":
            if _BARE_NAME.match(stripped):
                ok = policy.allows_name(stripped)
                _note(found, stripped, ok)
                if not ok:
                    continue
            else:
                inside = None
        out.append(line)
    return "\n".join(out)


_MARKERS = ("skill", _DEFERRED_HEADER, "<function>{")


def _filter_content(content, policy: Policy, found: Finding):
    """Message content (a string or blocks, tool results' included) with what isn't allowed taken out of the lists of
    skills and tools in it, and ToolSearch's references to tools that aren't allowed dropped; the same object when
    nothing changed."""
    if isinstance(content, str):
        if not any(k in content for k in _MARKERS):
            return content
        new = _filter_text(content, policy, found)
        return new if new != content else content
    if not isinstance(content, list):
        return content
    changed = False
    out = []
    for block in content:
        if isinstance(block, dict):
            t = block.get("type")
            if t == "tool_reference":  # a tool ToolSearch loaded (Anthropic's tool search): it must be in the tools
                name = str(block.get("tool_name") or "")
                ok = policy.allows_name(name)
                _note(found, name, ok)
                if not ok:
                    changed = True
                    continue
            elif t == "tool_result" and block.get("content") is not None:
                new = _filter_content(block["content"], policy, found)
                if new is not block["content"]:
                    block = {**block, "content": new if new != [] else "No tools matched."}
                    changed = True
            elif isinstance(block.get("text"), str) and any(k in block["text"] for k in _MARKERS):
                new = _filter_text(block["text"], policy, found)
                if new != block["text"]:
                    block = {**block, "text": new}
                    changed = True
        out.append(block)
    return out if changed else content


def _filter_native(registry: dict, policy: Policy, found: Finding) -> dict:
    """A Responses request's own tools (sent as they came to a backend speaking the Responses API), filtered the same way."""
    native = []
    for spec in registry.get("native") or []:
        if not isinstance(spec, dict):
            native.append(spec)
            continue
        if spec.get("type") == "namespace":
            inner = [t for t in spec.get("tools") or []
                     if not isinstance(t, dict) or policy.allows_name(chat_name(spec.get("name"), str(t.get("name") or "")))]
            if inner:
                native.append({**spec, "tools": inner} if len(inner) != len(spec.get("tools") or []) else spec)
            continue
        if policy.allows_name(str(spec.get("name") or spec.get("type") or "")):
            native.append(spec)
    hosted = []
    for h in registry.get("hosted") or []:  # tools that run at OpenAI (web search...): not in the Chat Completions tools
        name = str(h.get("name") or h.get("type") or "")
        ok = policy.allows_name(name)
        _note(found, name, ok)
        if ok:
            hosted.append(h)
    entries = registry.get("entries") or {}
    return {**registry, "native": native, "hosted": hosted, "entries": {k: v for k, v in entries.items() if policy.allows_name(k)}}


def apply(body: dict, fmt: str, policy: Policy, enforce: bool) -> tuple[dict, Finding]:
    """The request with what the policy doesn't allow left out (when ``enforce``; else the same body), and what was found.
    ``fmt``: the client's format (anthropic, or openai for Chat Completions and Responses turns made into it)."""
    found = Finding()
    tools = body.get("tools")
    kept_tools = None
    if isinstance(tools, list):
        kept_tools = []
        for t in tools:
            if not isinstance(t, dict):
                kept_tools.append(t)
                continue
            name = _tool_name(fmt, t)
            ok = policy.allows_name(name)
            _note(found, name, ok)
            if ok:
                kept_tools.append(t)
        # A tool that runs skills carries the list in its description in some versions: filter it there too.
        for i, t in enumerate(kept_tools):
            if not isinstance(t, dict):
                continue
            nested = fmt == "openai" and isinstance(t.get("function"), dict)
            holder = t["function"] if nested else t
            desc = holder.get("description")
            if isinstance(desc, str) and "<skill>" in desc:
                new = _filter_text(desc, policy, found)
                if new != desc:
                    kept_tools[i] = {**t, "function": {**holder, "description": new}} if nested else {**t, "description": new}
    new_body = dict(body)
    if kept_tools is not None and len(kept_tools) != len(tools):
        new_body["tools"] = kept_tools
        tc = body.get("tool_choice")
        chosen = (tc.get("name") or (tc.get("function") or {}).get("name")) if isinstance(tc, dict) else None
        if chosen and not policy.allows_name(chosen):
            new_body["tool_choice"] = {"type": "auto"} if fmt == "anthropic" else "auto"
        if not kept_tools:
            new_body.pop("tools", None)
            new_body.pop("tool_choice", None)
    elif kept_tools is not None and kept_tools != tools:
        new_body["tools"] = kept_tools
    if isinstance(body.get(TOOLS_KEY), dict):
        new_body[TOOLS_KEY] = _filter_native(body[TOOLS_KEY], policy, found)

    system = body.get("system")
    if system is not None:
        new = _filter_content(system, policy, found)
        if new is not system:
            new_body["system"] = new
    if isinstance(body.get("instructions"), str):
        new = _filter_content(body["instructions"], policy, found)
        if new is not body["instructions"]:
            new_body["instructions"] = new
    msgs = body.get("messages")
    if isinstance(msgs, list):
        out = msgs
        for i, m in enumerate(msgs):
            if not isinstance(m, dict) or m.get("role") == "assistant":
                continue
            new = _filter_content(m.get("content"), policy, found)
            if new is not m.get("content"):
                if out is msgs:
                    out = list(msgs)
                out[i] = {**m, "content": new}
        if out is not msgs:
            new_body["messages"] = out
    return (new_body if enforce else body), found
