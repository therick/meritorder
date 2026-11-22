"""Request screening, in shadow mode (decision 71, docs/06-auth-and-spend.md#request-screening).

What an agent's conversation shows that a security team would want to know, from traffic the gateway already sees: an
agent's next request carries the tool calls its last answer made and their results. Like outcomes.py, only what's new
since the session's last turn is read, so each call and result is looked at once.

Checked, with fixed rules and no model (the research behind the choice is in decision 71):

* **Hidden text**: Unicode tag characters (invisible, but models read them: "ASCII smuggling"), bidirectional overrides,
  and zero-width characters, anywhere in the new messages.
* **Keys**: provider-prefixed secrets (``sk-ant-``, ``ghp_``, ``AKIA``... and private key blocks) in a tool call the
  model made (it may be sending one somewhere) or in text going to the provider (a prompt or a tool result).
* **Risky tool calls** in shell commands and file edits: piping a download into a shell, uploading data with curl or
  wget, DNS lookups with command substitution (a known way to leak data), reading credential files, and writing agent,
  editor, git-hook or shell start-up configuration.
* **Untrusted content**: a result from a web fetch or search, or from a tool of an MCP server registered as carrying
  other people's words (``mcp.servers.<name>.untrusted_content``). From then on the session is marked, and risky tool calls
  are also counted as ``after_untrusted``: the pattern of prompt injection, where text an agent reads tells it what to do.
* **Image links with data** in the model's own text: Markdown images whose URL carries a query, a common exfiltration
  channel when a client renders them.

In shadow mode nothing is blocked or changed. A request with findings gets ledger tags (``screen``: the kinds found,
joined with ``+``; ``screen_untrusted``: ``1`` once the session has read untrusted content), and
``meritorder_screening_findings_total`` counts each kind by team. Only kinds and counts are kept, never the text, the
command, the path or the key.
"""

from __future__ import annotations

import re
from typing import Any

from .outcomes import EDIT_TOOLS, SHELL_TOOLS

READ = 20000  # characters of each new message's text looked at

# ------------------------------------------------------------------ hidden text

TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
BIDI = re.compile("[‪-‮⁦-⁩]")
# Zero-width space, non-joiner, word joiner and invisible operators; not the zero-width joiner (emoji sequences use it)
# nor a byte-order mark (files start with one).
INVISIBLE = re.compile("[​‌⁠-⁤]")

# ------------------------------------------------------------------ keys

SECRETS: list[tuple[str, re.Pattern]] = [
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("stripe", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("merit_order", re.compile(r"\bmo-[A-Za-z0-9_-]{40,}")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
]

# ------------------------------------------------------------------ risky tool calls

SHELL_RULES: list[tuple[str, re.Pattern]] = [
    ("pipe_to_shell", re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b|\b(?:curl|wget)\b[^|;&]*\|\s*(?:python\d?|node|perl|ruby)\b")),
    ("upload", re.compile(r"\bcurl\b[^|;&]*\s(?:-d|--data(?:-binary|-raw|-urlencode)?|-F|--form|-T|--upload-file)\b|"
                          r"\bwget\b[^|;&]*--post-(?:data|file)\b|\b(?:nc|ncat|netcat)\b[^|;&]*\s\d{2,5}\b")),
    ("dns_exfil", re.compile(r"\b(?:ping|dig|nslookup|host|drill)\b[^|;&\n]*(?:\$\(|`)")),
    ("credential_read", re.compile(r"\b(?:cat|less|more|head|tail|cp|scp|base64|xxd|od|strings|grep|tar|zip)\b[^|;&\n]*"
                                   r"(?:\.ssh/|id_(?:rsa|ed25519|ecdsa)\b|\.aws/credentials|\.netrc\b|\.git-credentials|\.npmrc\b|"
                                   r"\.pypirc\b|\.kube/config|(?:^|[\s/])\.env(?:\.\w+)?\b|\.docker/config\.json)")),
    ("config_write", re.compile(r">>?\s*~?[\w./-]*(?:\.(?:bash|zsh)rc|\.profile|\.bash_profile|\.git/hooks/[\w-]+|"
                                r"\.claude/settings[\w.]*\.json|\.mcp\.json|\.cursor/mcp\.json|\.vscode/settings\.json)\b")),
]
CONFIG_PATHS = re.compile(r"(?:^|/)(?:\.claude/settings[\w.]*\.json|\.mcp\.json|\.cursor/mcp\.json|\.vscode/(?:settings|tasks)\.json|"
                          r"\.git/hooks/[\w-]+|\.(?:bash|zsh)rc|\.profile|\.bash_profile|\.github/workflows/[\w.-]+)$")
RISKY = {"pipe_to_shell", "upload", "dns_exfil", "credential_read", "config_write", "secret_in_tool_call"}

# Tools whose results are someone else's words: fetched pages and search results.
UNTRUSTED_TOOLS = re.compile(r"(?:^|[_.:])(?:web_?fetch|fetch|web_?search|search_web|browse\w*|browser\w*|read_url|get_url|"
                             r"http_get|curl_url|scrape\w*)$", re.I)
MD_IMAGE = re.compile(r"!\[[^\]]{0,200}\]\(\s*https?://[^)\s]+\?[^)\s]+\)")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content[:READ]
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") in ("text", "input_text", "output_text"):
                    parts.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_result":
                    parts.append(_text(b.get("content")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)[:READ]
    return ""


def _messages(body: dict) -> list:
    msgs = body.get("messages")
    return msgs if isinstance(msgs, list) else []


def _calls(m: dict) -> list[tuple[str, dict]]:
    """A message's tool calls as (lowercased name, arguments), in Anthropic and Chat Completions shapes."""
    from .outcomes import _args

    out = []
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        out.append((str(fn.get("name") or "").lower(), _args(fn.get("arguments"))))
    content = m.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append((str(b.get("name") or "").lower(), b.get("input") if isinstance(b.get("input"), dict) else {}))
    return out


def _results(m: dict, names: dict[str, str]) -> list[tuple[str, str]]:
    """A message's tool results as (the tool's name, where known, its text)."""
    if m.get("role") == "tool":
        return [(names.get(m.get("tool_call_id") or "", ""), _text(m.get("content")))]
    content = m.get("content")
    if not isinstance(content, list):
        return []
    return [(names.get(b.get("tool_use_id") or "", ""), _text(b.get("content")))
            for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


def _ids(m: dict) -> dict[str, str]:
    ids = {}
    for tc in m.get("tool_calls") or []:
        ids[tc.get("id") or ""] = str((tc.get("function") or {}).get("name") or "").lower()
    if isinstance(m.get("content"), list):
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                ids[b.get("id") or ""] = str(b.get("name") or "").lower()
    return ids


def _command(args: dict) -> str:
    for k in ("command", "cmd", "script", "input"):
        v = args.get(k)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        if isinstance(v, str) and v:
            return v[:READ]
    return ""


def _path(args: dict) -> str:
    for k in ("file_path", "path", "filename", "target_file", "notebook_path"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _secrets(text: str) -> set[str]:
    return {name for name, rx in SECRETS if rx.search(text)}


def _hidden(text: str, found: dict[str, int]) -> None:
    for kind, rx in (("unicode_tags", TAG_CHARS), ("unicode_bidi", BIDI), ("unicode_invisible", INVISIBLE)):
        if rx.search(text):
            found[kind] = found.get(kind, 0) + 1


def scan(body: dict, since: int, untrusted: bool = False, untrusted_prefixes: tuple[str, ...] = ()) -> tuple[dict[str, int], bool]:
    """Findings in the messages after the first ``since``: kind -> count, and whether the session has read untrusted
    content (``untrusted`` is what it had before this request). Kinds: unicode_tags, unicode_bidi, unicode_invisible,
    secret_in_prompt, secret_in_tool_call, the shell rules (pipe_to_shell, upload, dns_exfil, credential_read,
    config_write), md_image_url, untrusted_content, and after_untrusted for each risky one once the session is marked.
    ``untrusted_prefixes``: tool names (lowercased) whose results also count as untrusted content, such as the tools of an
    MCP server registered with untrusted_content (``mcp__<server>__``)."""
    msgs = _messages(body)
    new = msgs[since:] if 0 <= since <= len(msgs) else []
    found: dict[str, int] = {}

    def add(kind: str, n: int = 1) -> None:
        found[kind] = found.get(kind, 0) + n

    names: dict[str, str] = {}
    for m in msgs[:since]:  # the tool calls the new results answer may be in earlier messages
        if isinstance(m, dict):
            names.update(_ids(m))
    system = body.get("system")
    if since == 0 and system:
        _hidden(_text(system) if not isinstance(system, str) else system[:READ], found)
    risky = 0
    for m in new:
        if not isinstance(m, dict):
            continue
        names.update(_ids(m))
        role = m.get("role")
        text = _text(m.get("content"))
        if text:
            _hidden(text, found)
            if role == "assistant" and MD_IMAGE.search(text):
                add("md_image_url")
                risky += 1
        for name, args in _calls(m):
            blob = " ".join(str(v) for v in args.values() if isinstance(v, (str, int, float)))[:READ]
            _hidden(blob, found)
            if _secrets(blob):
                add("secret_in_tool_call")
                risky += 1
            if name in SHELL_TOOLS or name.endswith("bash") or name.endswith("shell"):
                cmd = _command(args)
                for kind, rx in SHELL_RULES:
                    if rx.search(cmd):
                        add(kind)
                        risky += 1
            if name in EDIT_TOOLS and CONFIG_PATHS.search(_path(args)):
                add("config_write")
                risky += 1
        if role in ("user", "tool", "system", "developer"):
            for tool, result in _results(m, names):
                if UNTRUSTED_TOOLS.search(tool) or tool.startswith("mcp__") and "fetch" in tool or \
                        untrusted_prefixes and tool.startswith(untrusted_prefixes):
                    add("untrusted_content")
                    untrusted = True
            if _secrets(text):
                add("secret_in_prompt")
    if untrusted and risky:
        add("after_untrusted", risky)
    return found, untrusted


def tags(found: dict[str, int], untrusted: bool) -> dict[str, str]:
    """The ledger tags for a request's findings (none when there are none)."""
    out = {}
    kinds = sorted(k for k in found if k not in ("untrusted_content",))
    if kinds:
        out["screen"] = "+".join(kinds)
    if untrusted:
        out["screen_untrusted"] = "1"
    return out
