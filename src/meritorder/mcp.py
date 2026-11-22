"""The MCP gateway (decision 75, docs/06-auth-and-spend.md#mcp-gateway): agents reach MCP tool servers through Merit Order.

An operator registers each server once (``mcp.servers``), with the gateway's credential for it. Agents connect to
``/mcp/<server>`` with their Merit Order key, as they do for models, so the same people, teams and roles apply:

* Who may use what: a team's ``mcp.servers`` and ``mcp.tools`` ("server/tool" globs). Tools a team may not use are left
  out of the tool list, and calls to them are refused.
* Tool pinning: each tool's definition (name, description, schemas) is hashed when first seen. A changed definition, the
  "rug pull" where a server rewrites a tool's description to steer the agent, is recorded; with ``pinning: enforce`` the
  tool is hidden, and calls to it refused, until an admin approves the new definition on the console.
* Keys in tool arguments and results (screening.SECRETS): recorded, or with ``secrets: block`` a call whose arguments
  carry one is refused and one in a result is replaced with a marker. Hidden Unicode and instructions in tool
  descriptions are recorded.
* A per-person limit on tool calls a minute.
* A log of every tool call: who, which tool, how it went and how long it took; never the arguments or the results.

The transport is MCP's Streamable HTTP (2025-03-26 and later): JSON-RPC POSTs answered with JSON or an event stream, a
GET stream for the server's own messages, and DELETE to end a session. Messages pass through as they are, but for the
tool lists and tool calls above. The caller's key is never sent on: the server gets the gateway's credential (auth:
api_key), the caller's own credential for that server (auth: forward), or none. A server's session ID is bound to the
person who opened it, so no one else can use it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import AuthError, Principal, bearer_token
from .screening import BIDI, INVISIBLE, SECRETS, TAG_CHARS

log = logging.getLogger("meritorder.mcp")

REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "retry-after", "cache-control")
MAX_REQUEST = 4 * 1024 * 1024  # a JSON-RPC message from a client
MAX_RESPONSE = 32 * 1024 * 1024  # a JSON answer, or one event of a stream
SESSION_TTL = 24 * 3600  # a server session's owner is kept this long after its last use
PIN_CACHE_SECONDS = 30  # how soon another gateway sees an admin's approval
DEFINITION_FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")

# Words in a tool's description that address the model rather than describe the tool: the "tool poisoning" pattern.
STEERING = re.compile(r"<\s*(?:important|system|instructions?)\s*>|ignore (?:all |any )?(?:previous|prior|other) instructions|"
                      r"do not (?:tell|mention|inform|show) (?:this to )?the user|before using (?:this|any) tool|"
                      r"(?:~|\$HOME)/\.(?:ssh|aws|config)|\bid_(?:rsa|ed25519)\b|\.env\b|mcp\.json", re.I)


def definition(tool: dict) -> dict:
    return {k: tool[k] for k in DEFINITION_FIELDS if k in tool}


def tool_hash(tool: dict) -> str:
    return hashlib.sha256(json.dumps(definition(tool), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hidden_kinds(text: str) -> set[str]:
    return {kind for kind, rx in (("unicode_tags", TAG_CHARS), ("unicode_bidi", BIDI), ("unicode_invisible", INVISIBLE))
            if rx.search(text)}


def secret_kinds(text: str) -> set[str]:
    return {name for name, rx in SECRETS if rx.search(text)}


def _strings(value: Any, limit: int = 1 << 20) -> str:
    """The strings in a JSON value, joined (up to ``limit`` characters), for scanning."""
    out: list[str] = []
    size = 0

    def walk(v: Any) -> None:
        nonlocal size
        if size > limit:
            return
        if isinstance(v, str):
            out.append(v)
            size += len(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return "\n".join(out)[:limit]


def redact(value: Any) -> tuple[Any, set[str]]:
    """The value with every key in its strings replaced by a marker, and the kinds replaced."""
    found: set[str] = set()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            for name, rx in SECRETS:
                if rx.search(v):
                    found.add(name)
                    v = rx.sub(f"[key removed by Merit Order: {name}]", v)
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value), found


def description_findings(tool: dict) -> set[str]:
    text = _strings({k: tool.get(k) for k in ("description", "title", "inputSchema", "annotations")})
    found = hidden_kinds(text)
    if STEERING.search(text):
        found.add("steering_description")
    return found


def rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def refused(id_: Any, message: str) -> dict:
    """A refused call, as a tool result with isError: the agent reads why (a protocol error would be retried or hidden)."""
    return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": f"Merit Order refused this tool call: {message}"}],
                                                    "isError": True}}


def _error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001 if status in (401, 403) else -32000, "message": message}},
                        status_code=status, headers=headers)


@dataclass
class Call:
    """A tool call on its way: logged once its answer comes back (or doesn't)."""

    tool: str
    started: float
    request_bytes: int
    findings: dict[str, int] = field(default_factory=dict)


class Pins:
    """Each server's pinned tool definitions, from the store, cached briefly per gateway."""

    def __init__(self, store):
        self.store = store
        self._cache: dict[str, tuple[float, dict[str, dict]]] = {}

    async def rows(self, server: str) -> dict[str, dict]:
        hit = self._cache.get(server)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        rows = {r["name"]: r for r in await self.store.mcp_pins(server)}
        self._cache[server] = (time.monotonic() + PIN_CACHE_SECONDS, rows)
        return rows

    def forget(self, server: str | None = None) -> None:
        if server is None:
            self._cache.clear()
        else:
            self._cache.pop(server, None)

    async def see(self, srv, tool: dict) -> dict:
        """The tool's pin, pinning it (or its new definition) if this is the first time it's seen as it is."""
        rows = await self.rows(srv.name)
        h = tool_hash(tool)
        row = rows.get(tool["name"])
        if row is None or row["hash"] != h:
            row = await self.store.mcp_pin(srv.name, tool["name"], h, definition(tool), approve=srv.new_tools == "allow")
            rows[tool["name"]] = row
        return row


def pin_status(row: dict | None) -> str:
    """approved, new (waiting for an admin), or changed (since an admin, or first sight, approved it)."""
    if row is None or row.get("approved_hash") is None:
        return "new"
    return "approved" if row["approved_hash"] == row["hash"] else "changed"


class McpGateway:
    def __init__(self, gateway):
        self.gw = gateway
        self.pins = Pins(gateway.store)
        self._client: httpx.AsyncClient | None = None
        self.transport = None  # tests put a fake server here

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, transport=self.transport,
                                             timeout=httpx.Timeout(300.0, connect=10.0))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ checks

    async def check_call(self, srv, team, p: Principal, msg: dict, call: Call) -> tuple[str, str] | None:
        """Why this tools/call is refused (a reason code, and a message for the agent), or None. Findings go on ``call``."""
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool = call.tool
        if not tool:
            return None  # the server answers a malformed call itself
        if not team.mcp.allows_tool(srv.name, tool):
            return "not_allowed", f"team '{p.team}' may not use {srv.name}/{tool}"
        if srv.pinning == "enforce":
            row = (await self.pins.rows(srv.name)).get(tool)
            status = pin_status(row) if row is not None else ("new" if srv.new_tools == "hold" else "approved")
            if status != "approved":
                return f"tool_{status}", (f"{srv.name}/{tool} changed since an admin approved it; an admin can review and approve the new definition "
                        "on the console's Tools page" if status == "changed" else
                        f"{srv.name}/{tool} is new and waits for an admin's approval on the console's Tools page")
        limit = team.mcp.calls_per_minute
        if limit:
            n = await self.gw.state.rate_hit(f"mcp:{p.user_id}", 60)
            if n > limit:
                return "rate_limited", f"{p.user_id} is limited to {limit} tool calls a minute; wait a minute and try again"
        args = params.get("arguments")
        text = _strings(args)
        for kind in hidden_kinds(text):
            call.findings[kind] = 1
        if team.mcp.secrets != "off":
            keys = secret_kinds(text)
            if keys:
                call.findings["secret_in_arguments"] = len(keys)
                if team.mcp.secrets == "block":
                    return "secret_in_arguments", (f"its arguments hold what looks like a key ({', '.join(sorted(keys))}); keys aren't sent to tool servers "
                            "through Merit Order")
        return None

    async def tools(self, srv, team, tools: list, findings: dict[str, int]) -> list:
        """A tools/list answer's tools: pinned, and only those the team may use (and, enforced, that are approved)."""
        out = []
        for t in tools:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                continue
            if not team.mcp.allows_tool(srv.name, t["name"]):
                continue
            for kind in description_findings(t):
                findings[kind] = findings.get(kind, 0) + 1
            if srv.pinning != "off":
                status = pin_status(await self.pins.see(srv, t))
                if status != "approved":
                    findings[f"tool_{status}"] = findings.get(f"tool_{status}", 0) + 1
                    if srv.pinning == "enforce":
                        continue
            out.append(t)
        return out

    # ------------------------------------------------------------------ recording

    def finish(self, srv, p: Principal, call: Call, status: str, reason: str | None = None, response_bytes: int | None = None) -> None:
        team = p.team
        m = self.gw.metrics
        m.inc("meritorder_mcp_calls_total", {"server": srv.name, "status": status, "team": team})
        m.observe("meritorder_mcp_call_seconds", {"server": srv.name}, time.perf_counter() - call.started)
        for kind, n in call.findings.items():
            m.inc("meritorder_mcp_findings_total", {"server": srv.name, "kind": kind, "team": team}, n)
        row = {"server": srv.name, "tool": call.tool[:200], "user_id": p.user_id, "team": team, "key_id": p.key_id, "status": status,
               "reason": reason, "duration_ms": int((time.perf_counter() - call.started) * 1000), "request_bytes": call.request_bytes,
               "response_bytes": response_bytes, "findings": dict(call.findings)}
        self.gw.spawn(self._log(row))

    async def _log(self, row: dict) -> None:
        try:
            await self.gw.store.mcp_log(row)
        except Exception:
            log.exception("could not log an MCP tool call")

    def note(self, srv, p: Principal, findings: dict[str, int]) -> None:
        for kind, n in findings.items():
            self.gw.metrics.inc("meritorder_mcp_findings_total", {"server": srv.name, "kind": kind, "team": p.team}, n)

    # ------------------------------------------------------------------ answers from the server

    async def answer(self, srv, team, p: Principal, msg: Any, calls: dict, methods: dict, size: int) -> Any:
        """One message from the server, as the client gets it."""
        if not isinstance(msg, dict) or "method" in msg:
            return msg  # the server's own requests and notifications pass through
        mid = msg.get("id")
        key = _id_key(mid)
        res = msg.get("result")
        method = methods.get(key)
        call = calls.pop(key, None)
        if isinstance(res, dict):
            if isinstance(res.get("tools"), list) and method in (None, "tools/list"):
                found: dict[str, int] = {}
                res = {**res, "tools": await self.tools(srv, team, res["tools"], found)}
                self.note(srv, p, found)
                msg = {**msg, "result": res}
            elif call is not None or (method is None and isinstance(res.get("content"), list)):
                msg, findings = self._result(team, msg, res)
                if call is None:
                    self.note(srv, p, findings)
                else:
                    for kind, n in findings.items():
                        call.findings[kind] = call.findings.get(kind, 0) + n
        if call is not None:
            status = "failed" if "error" in msg else "error" if isinstance(res, dict) and res.get("isError") else "ok"
            self.finish(srv, p, call, status, response_bytes=size)
        return msg

    def _result(self, team, msg: dict, res: dict) -> tuple[dict, dict[str, int]]:
        findings: dict[str, int] = {}
        text = _strings(res)
        for kind in hidden_kinds(text):
            findings[kind] = 1
        if team.mcp.secrets == "off":
            return msg, findings
        keys = secret_kinds(text)
        if not keys:
            return msg, findings
        findings["secret_in_result"] = len(keys)
        if team.mcp.secrets == "block":
            res, _ = redact(res)
            findings["secret_removed"] = len(keys)
            msg = {**msg, "result": res}
        return msg, findings

    # ------------------------------------------------------------------ sessions

    def _session_key(self, srv, sid: str) -> str:
        return f"mcp:session:{srv.name}:{hashlib.sha256(sid.encode()).hexdigest()}"

    async def session_owner(self, srv, sid: str) -> str | None:
        return await self.gw.state.get_json(self._session_key(srv, sid))

    async def bind(self, srv, sid: str, user_id: str) -> None:
        await self.gw.state.set_json(self._session_key(srv, sid), user_id, ttl=SESSION_TTL)

    async def unbind(self, srv, sid: str) -> None:
        await self.gw.state.set_json(self._session_key(srv, sid), None, ttl=1)

    # ------------------------------------------------------------------ the proxy

    def upstream_headers(self, srv, request: Request) -> dict[str, str]:
        h = {k: v for k in REQUEST_HEADERS if (v := request.headers.get(k))}
        h.update(srv.headers)
        a = srv.auth
        if a.type == "api_key":
            key = a.key()
            if not key:
                raise HTTPException(502, f"the gateway has no credential for MCP server '{srv.name}' (auth.value_env or value_file "
                                         "is unset)")
            h[a.header] = a.with_prefix(key)
        elif a.type == "forward":
            own = request.headers.get(a.forward_header)
            if own:
                h[a.header] = a.with_prefix(own)
        return h

    async def handle(self, request: Request, name: str) -> Response:
        cfg = self.gw.cfg
        srv = cfg.mcp.server(name)
        if srv is None:
            return _error(404, f"no MCP server named '{name}' is registered")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return _error(403, "requests from other sites aren't accepted")
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            if not bearer_token(headers):
                raise AuthError(401, "send your Merit Order key: 'Authorization: Bearer <key>'")
            p = await self.gw.authn.authenticate(headers)
        except AuthError as e:
            return _error(e.status, e.message, {"www-authenticate": 'Bearer realm="meritorder"'} if e.status == 401 else None)
        team = cfg.team(p.team)
        if not team.mcp.allows_server(srv.name):
            return _error(403, f"team '{p.team}' may not use MCP server '{srv.name}'")
        sid = request.headers.get("mcp-session-id")
        if sid:
            owner = await self.session_owner(srv, sid)
            if owner != p.user_id:
                # Unknown here (expired, or another person's): the client starts a new session, as the spec says for a 404.
                return _error(404, "session not found; start a new one")
        try:
            up_headers = self.upstream_headers(srv, request)
        except HTTPException as e:
            return _error(e.status_code, e.detail)

        calls: dict = {}
        methods: dict = {}
        content = None
        if request.method == "POST":
            content = await request.body()
            if len(content) > MAX_REQUEST:
                return _error(413, f"an MCP message may be at most {MAX_REQUEST // (1024 * 1024)} MB")
            try:
                parsed = json.loads(content)
            except ValueError:
                return _error(400, "the body isn't JSON")
            msgs = parsed if isinstance(parsed, list) else [parsed]
            refusals: dict = {}
            for m in msgs:
                if not isinstance(m, dict) or "method" not in m or "id" not in m:
                    continue
                key = _id_key(m["id"])
                methods[key] = m["method"]
                if m["method"] == "tools/call":
                    params = m.get("params") if isinstance(m.get("params"), dict) else {}
                    call = Call(tool=str(params.get("name") or ""), started=time.perf_counter(), request_bytes=len(content))
                    why = await self.check_call(srv, team, p, m, call)
                    if why:
                        self.finish(srv, p, call, "refused", reason=why[0])
                        refusals[key] = refused(m["id"], why[1])
                    else:
                        calls[key] = call
            if refusals:
                for c in calls.values():  # sent nowhere
                    self.finish(srv, p, c, "refused", reason="batch")
                if not isinstance(parsed, list):
                    return JSONResponse(next(iter(refusals.values())))
                return JSONResponse([refusals.get(_id_key(m.get("id"))) or
                                     rpc_error(m.get("id"), -32000, "not sent: another call in this batch was refused")
                                     for m in msgs if isinstance(m, dict) and "id" in m and "method" in m])

        client = self.client()
        req = client.build_request(request.method, srv.url, headers=up_headers, content=content,
                                   timeout=httpx.Timeout(srv.timeout_seconds, connect=10.0))
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            for c in calls.values():
                self.finish(srv, p, c, "failed", reason="unreachable")
            return _error(502, f"MCP server '{srv.name}' couldn't be reached ({type(e).__name__})")

        new_sid = resp.headers.get("mcp-session-id")
        if new_sid and new_sid != sid:
            await self.bind(srv, new_sid, p.user_id)
        elif sid and resp.status_code < 400:
            await self.bind(srv, sid, p.user_id)  # keeps it while it's used
        if sid and (resp.status_code == 404 or request.method == "DELETE" and resp.status_code < 300):
            await self.unbind(srv, sid)

        out_headers = {k: v for k in RESPONSE_HEADERS if (v := resp.headers.get(k))}
        if resp.status_code in (401, 403):
            await resp.aclose()
            for c in calls.values():
                self.finish(srv, p, c, "failed", reason="upstream_auth")
            hint = (f"send your own credential for it in {srv.auth.forward_header}" if srv.auth.type == "forward" else
                    "check the gateway's credential for it")
            return _error(502, f"MCP server '{srv.name}' refused the request ({resp.status_code}); {hint}")
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return StreamingResponse(self._events(srv, team, p, resp, calls, methods), status_code=resp.status_code,
                                     headers=out_headers)
        try:
            body = await _read(resp, MAX_RESPONSE)
        except ValueError as e:
            for c in calls.values():
                self.finish(srv, p, c, "failed", reason="too_large")
            return _error(502, str(e))
        if "application/json" in ctype and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if parsed is not None:
                if isinstance(parsed, list):
                    parsed = [await self.answer(srv, team, p, m, calls, methods, len(body)) for m in parsed]
                else:
                    parsed = await self.answer(srv, team, p, parsed, calls, methods, len(body))
                body = json.dumps(parsed, separators=(",", ":")).encode()
                out_headers.pop("content-length", None)
        for c in calls.values():
            self.finish(srv, p, c, "no_answer")
        return Response(body, status_code=resp.status_code, headers=out_headers)

    async def _events(self, srv, team, p: Principal, resp: httpx.Response, calls: dict, methods: dict):
        """An event stream from the server, as the client gets it: each event's JSON-RPC message passed through answer()."""
        done = False
        try:
            event: list[str] = []
            size = 0
            async for line in resp.aiter_lines():
                if line:
                    size += len(line)
                    if size > MAX_RESPONSE:
                        raise ValueError("an event from the MCP server is too large")
                    event.append(line)
                    continue
                if event:
                    yield await self._event(srv, team, p, event, calls, methods, size)
                event, size = [], 0
            if event:
                yield await self._event(srv, team, p, event, calls, methods, size)
            done = True
        except (httpx.HTTPError, ValueError) as e:
            log.warning("MCP server %s stream ended: %s", srv.name, e)
        finally:
            await resp.aclose()
            for c in list(calls.values()):
                self.finish(srv, p, c, "no_answer" if done else "failed", reason=None if done else "stream_broken")
            calls.clear()

    async def _event(self, srv, team, p: Principal, lines: list[str], calls: dict, methods: dict, size: int) -> bytes:
        data = [ln[5:].removeprefix(" ") for ln in lines if ln.startswith("data:")]
        others = [ln for ln in lines if not ln.startswith("data:")]
        if data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            if msg is not None:
                msg = await self.answer(srv, team, p, msg, calls, methods, size)
                data = [json.dumps(msg, separators=(",", ":"))]
        return ("\n".join(others + [f"data: {d}" for d in data]) + "\n\n").encode()


def _id_key(id_: Any) -> str:
    return json.dumps(id_)


async def _read(resp: httpx.Response, limit: int) -> bytes:
    chunks, size = [], 0
    try:
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise ValueError(f"the MCP server's answer is over {limit // (1024 * 1024)} MB")
            chunks.append(chunk)
    finally:
        await resp.aclose()
    return b"".join(chunks)


# ---------------------------------------------------------------------- routes


def server_view(srv, tools: int | None = None) -> dict:
    return {"name": srv.name, "description": srv.description, "url": srv.url, "auth": srv.auth.type,
            "forward_header": srv.auth.forward_header if srv.auth.type == "forward" else None, "pinning": srv.pinning,
            "new_tools": srv.new_tools, "untrusted_content": srv.untrusted_content, "path": f"/mcp/{srv.name}",
            **({"tools": tools} if tools is not None else {})}


def build_mcp_router(gateway) -> APIRouter:
    router = APIRouter()

    def mcp() -> McpGateway:
        return gateway.mcp

    @router.api_route("/mcp/{name}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def proxy(name: str, request: Request):
        return await mcp().handle(request, name)

    @router.get("/v1/mcp/servers")
    async def my_servers(request: Request):
        """The MCP servers the caller's team may use, with where to connect."""
        try:
            p = await gateway.authn.authenticate_console(request)
        except AuthError as e:
            raise HTTPException(e.status, e.message)
        team = gateway.cfg.team(p.team)
        return {"servers": [server_view(s) for s in gateway.cfg.mcp.servers if s.enabled and team.mcp.allows_server(s.name)]}

    async def staff(request: Request) -> Principal:
        try:
            p = await gateway.authn.authenticate_console(request)
        except AuthError as e:
            raise HTTPException(e.status, e.message)
        if p.role not in ("admin", "team_lead"):
            raise HTTPException(403, "admins and team leads only")
        return p

    @router.get("/v1/admin/mcp")
    async def overview(request: Request, days: int = Query(7, ge=1, le=90)):
        """Registered servers, their tools as pinned, and tool calls over the last ``days`` (a team lead's own team only)."""
        p = await staff(request)
        team = None if p.role == "admin" else p.team
        pins = await gateway.store.mcp_pins()
        per: dict[str, int] = {}
        for r in pins:
            per[r["server"]] = per.get(r["server"], 0) + 1
        since = datetime.now(UTC) - timedelta(days=days)
        return {"servers": [{**server_view(s, per.get(s.name, 0)), "enabled": s.enabled} for s in gateway.cfg.mcp.servers],
                "tools": [{"server": r["server"], "name": r["name"], "status": pin_status(r), "hash": r["hash"],
                           "first_seen": r["first_seen"], "changed_at": r["changed_at"], "approved_by": r["approved_by"],
                           "approved_at": r["approved_at"], "findings": sorted(description_findings(r["definition"] or {}))}
                          for r in pins],
                "counts": await gateway.store.mcp_call_counts(since, team), "days": days, "can_approve": p.role == "admin"}

    @router.get("/v1/admin/mcp/tools/{server}/{tool}")
    async def tool_detail(server: str, tool: str, request: Request):
        """A tool's definition now and as approved, so an admin can see what changed."""
        await staff(request)
        row = next((r for r in await gateway.store.mcp_pins(server) if r["name"] == tool), None)
        if row is None:
            raise HTTPException(404, "no such tool")
        return {**row, "status": pin_status(row), "findings": sorted(description_findings(row["definition"] or {}))}

    @router.post("/v1/admin/mcp/tools/{server}/{tool}/approve")
    async def approve(server: str, tool: str, request: Request):
        p = await staff(request)
        if p.role != "admin":
            raise HTTPException(403, "only admins can approve a tool")
        body = await request.json()
        h = body.get("hash") if isinstance(body, dict) else None
        if not isinstance(h, str) or not await gateway.store.mcp_approve(server, tool, h, p.user_id):
            raise HTTPException(409, "the tool's definition changed since you looked; review it again")
        mcp().pins.forget(server)
        return {"ok": True}

    @router.get("/v1/admin/mcp/calls")
    async def calls(request: Request, server: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        p = await staff(request)
        rows = await gateway.store.mcp_calls(limit, server=server, team=None if p.role == "admin" else p.team)
        return {"calls": rows}

    return router
