"""FastAPI application: OpenAI and Anthropic compatible endpoints plus reports and auth."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import Authenticator, AuthError
from .backends import build_backends
from .config import Config, build_config, default_config_path, load_raw_config
from .configstore import load_active
from .proxy import Gateway
from .responses_compact import ResponsesTools
from .responses_ws import ResponsesSocket
from .accounts import build_router as build_accounts_router
from . import datapolicy
from .accounts import password_key
from .proxy import MAX_TAGS
from .admin import build_router as build_admin_router
from .batches.api import build_router as build_batches_router
from .console import build_router as build_console_router
from .scim import build_router as build_scim_router
from .stored.api import build_router as build_stored_router
from .controls import build_router as build_controls_router
from .mcp import build_mcp_router
from .google_groups import follow as follow_google_groups
from .guards import BodyLimit, SecurityHeaders, max_body_bytes
from .telemetry import setup_tracing
from .reports import AnsweredBy, build_router as build_reports_router
from .router import Router
from .state import make_state
from .store import make_store

log = logging.getLogger("meritorder")
TOKENS_SAVE_SECONDS = 60
BATCH_POLL_SECONDS = 15


def build_gateway(cfg: Config, store=None, state=None) -> Gateway:
    store = store or make_store(os.environ.get("MERITORDER_DATABASE_URL"))
    state = state or make_state(os.environ.get("MERITORDER_REDIS_URL"))
    backends = build_backends(cfg)
    return Gateway(cfg, store, state, backends, Router(cfg, backends), Authenticator(cfg, store, state))


def create_app(cfg: Config | None = None, gateway: Gateway | None = None) -> FastAPI:
    raw = None
    if cfg is None and gateway is None:
        path = default_config_path()
        raw = load_raw_config(path)
        cfg = build_config(raw, path.parent)
    cfg = cfg or gateway.cfg
    gw = gateway or build_gateway(cfg)
    gw.file_raw = raw if raw is not None else getattr(gw, "file_raw", None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_tracing()
        await gw.store.init()
        if gw.store.kind == "memory":
            log.warning("no MERITORDER_DATABASE_URL set: using in-memory storage (data is lost on restart)")
        try:
            await load_active(gw)
        except Exception:
            log.exception("could not load the stored configuration; using the YAML file")
        try:
            await gw.load_token_calibration()
        except Exception:
            log.exception("could not load the token calibration; estimates start uncalibrated")
        enabled = sorted(gw.backends)
        log.info("Merit Order %s ready; backends: %s", __version__, ", ".join(enabled) or "(none)")
        async def publish_load():
            # Heartbeat plus in-flight counts, so other gateways see this one's load within seconds.
            while True:
                try:
                    await gw.state.publish_inflight(gw.gateway_id, gw.inflight.counts())
                except Exception:
                    log.exception("could not publish in-flight counts")
                await asyncio.sleep(2)

        async def follow_controller():
            while True:
                try:
                    stored = await gw.state.get_config_token()
                    if stored is not None and stored != getattr(gw, "config_token", None):
                        await load_active(gw)
                    await gw.refresh_deployments()
                except Exception:
                    log.exception("could not refresh configuration or deployments")
                await asyncio.sleep(5)

        async def save_learned():
            # What replies taught about token counts (tokens.py), so a restart doesn't start from scratch.
            while True:
                await asyncio.sleep(TOKENS_SAVE_SECONDS)
                try:
                    await gw.save_token_calibration()
                except Exception:
                    log.exception("could not save the token calibration")

        async def poll_batches():
            # Without Postgres there's no worker, so this process checks on its own batches (batches/poller.py).
            from .batches.poller import BatchPoller

            poller = BatchPoller(gw)
            while True:
                try:
                    await poller.run_once()
                except Exception:
                    log.exception("batch poll failed")
                await asyncio.sleep(BATCH_POLL_SECONDS)

        async def maintain_stored():
            # Without Postgres there's no worker: this process deletes stored responses past their retention itself.
            from .stored.service import PURGE_SECONDS

            while True:
                await asyncio.sleep(PURGE_SECONDS)
                try:
                    await gw.stored.maintain()
                except Exception:
                    log.exception("stored responses: maintenance failed")

        follower = asyncio.create_task(follow_controller())
        publisher = asyncio.create_task(publish_load())
        saver = asyncio.create_task(save_learned())
        groups = asyncio.create_task(follow_google_groups(gw))  # idles unless Google sign-in reads groups
        batches = asyncio.create_task(poll_batches()) if gw.store.kind == "memory" else None
        stored = asyncio.create_task(maintain_stored()) if gw.store.kind == "memory" else None
        yield
        follower.cancel()
        publisher.cancel()
        saver.cancel()
        groups.cancel()
        for task in (batches, stored):
            if task is not None:
                task.cancel()
        await gw.drain()
        try:
            await gw.save_token_calibration()
        except Exception:
            log.exception("could not save the token calibration")
        for b in gw.backends.values():
            await b.close()
        if gw.compressor is not None:
            await gw.compressor.close()
        if gw._mcp is not None:
            await gw._mcp.close()
        await gw.state.close()
        await gw.store.close()

    app = FastAPI(title="Merit Order", version=__version__, lifespan=lifespan)
    app.state.gateway = gw
    app.add_middleware(BodyLimit)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(AnsweredBy)  # reports say which store answered them

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await gw.handle(request, "openai")

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await gw.handle(request, "openai", wire="responses")

    @app.websocket("/v1/responses")
    async def responses_websocket(websocket: WebSocket):
        await ResponsesSocket(gw, websocket, max_body_bytes()).serve()

    tools = ResponsesTools(gw)

    @app.post("/v1/responses/compact")
    async def responses_compact(request: Request):
        return await tools.compact(request)

    @app.post("/v1/responses/input_tokens")
    async def responses_input_tokens(request: Request):
        return await tools.input_tokens(request)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await gw.embeddings(request)

    @app.post("/v1/ocr")
    async def ocr(request: Request):
        return await gw.ocr(request)

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await gw.handle(request, "anthropic")

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        return await gw.count_tokens(request)

    @app.get("/v1/models")
    async def models(request: Request):
        try:
            p = await gw.authn.authenticate({k.lower(): v for k, v in request.headers.items()})
        except AuthError as e:
            raise HTTPException(e.status, e.message)
        cfg = gw.cfg
        team = cfg.team(p.team)
        usable = [m for m in cfg.models if team.allows_model(m.name) and any(r.backend in gw.backends for r in m.routes)]
        if any(r.backend in gw.backends and gw.backends[r.backend].kind != "mock" for m in usable for r in m.routes):
            usable = [m for m in usable if any(gw.backends[r.backend].kind != "mock" for r in m.routes if r.backend in gw.backends)]
        # auto's window: the largest among models auto can pick (a model without a score never is), so a
        # harness doesn't let a conversation grow past what any of them holds.
        auto_ctx = max((m.context_window for m in usable
                        if m.context_window and m.kind == "chat" and any(v > 0 for v in m.quality.values())), default=None)
        data = [{"id": cfg.routing.auto_model, "object": "model", "created": 0, "owned_by": "meritorder", "context_window": auto_ctx}]
        data += [{"id": m.name, "object": "model", "created": 0, "owned_by": "meritorder", "context_window": m.context_window,
                  "quality": m.quality, "type": m.kind, "self_hostable": bool(m.hf_id)} for m in usable]
        return {"object": "list", "data": data}

    @app.get("/v1/routes")
    async def routes(request: Request):
        """The named routes (tiers) a caller can ask for by name, and where each can send a request now: candidate models,
        each route's provider, price, and data facts (zero retention, training, where they come from). Blackstart's
        route listing (decision 72): its data policy checks these before sending. Scoped to the caller's team's allowed
        models and backends."""
        try:
            p = await gw.authn.authenticate({k.lower(): v for k, v in request.headers.items()})
        except AuthError as e:
            raise HTTPException(e.status, e.message)
        cfg = gw.cfg
        team = cfg.team(p.team)
        now = time.time()

        def route_row(r) -> dict | None:
            b = gw.backends.get(r.backend)
            if b is None or not team.allows_backend(r.backend):
                return None
            price = r.price
            if price is not None and price.per_page is not None:  # an OCR model's
                cost = {"price_per_page": {"page": price.per_page, "annotated_page": price.page(True)}}
            else:
                cost = {"price_per_mtok": None if price is None else {
                    "input": price.input, "output": price.output,
                    "cache_read": price.cache_read if price.cache_read is not None else round(price.input * 0.1, 6)}}
            return {"backend": r.backend, "model": r.model, **datapolicy.facts(b.cfg), **cost, "available": bool(b.available and b.healthy(now))}

        def model_row(m) -> dict:
            return {"model": m.name, "kind": m.kind, "quality": m.quality, "routes": [x for r in m.routes if (x := route_row(r))]}

        out = {}
        for name, t in cfg.tiers.items():
            if cfg.tier(name) is None:
                continue  # a model has this name: the model wins
            row: dict = {"kind": t.kind, "level": t.level, "task": t.task}
            if t.level is not None:
                tasks = [t.task] if t.task else ["coding", "general"]
                row["candidates"] = {task: [model_row(m) for m in cfg.models if m.kind == "chat" and team.allows_model(m.name)
                                            and m.q(task) >= t.level and m.routes] for task in tasks}
                row["note"] = (f"auto at level {t.level}: the cheapest candidate that can serve the request, after the team's "
                               "rules and the caller's data policy; the task (coding or general) comes from the request"
                               if not t.task else f"auto at {t.task}:{t.level}")
            else:
                m = cfg.tier_model(name)
                row["candidates"] = {"any": [model_row(m)] if m is not None and team.allows_model(m.name) else []}
            out[name] = row
        return {"routes": out, "auto_model": cfg.routing.auto_model,
                "policy_headers": {"allowed_providers": datapolicy.PROVIDERS_HEADER, "zero_retention": datapolicy.ZERO_RETENTION_HEADER}}

    @app.get("/v1/capabilities")
    async def capabilities(request: Request):
        """What this gateway offers a service built on it (Blackstart's capability handshake, decision 70), and how to use
        each: where usage and cost come back, how to attribute calls, how to keep a cache warm. Needs a key, like
        /v1/models; the answer is the same for every caller."""
        try:
            await gw.authn.authenticate({k.lower(): v for k, v in request.headers.items()})
        except AuthError as e:
            raise HTTPException(e.status, e.message)
        embeddings = sorted(m.name for m in gw.cfg.models if m.kind == "embedding" and any(r.backend in gw.backends for r in m.routes))
        ocr_models = sorted(m.name for m in gw.cfg.models if m.kind == "ocr" and any(r.backend in gw.backends for r in m.routes))
        return {
            "name": "Merit Order", "version": __version__,
            "capabilities": {
                "usage_in_response": True,
                # Non-streaming replies carry it in a header; for a stream, look the request up by its id or a tag.
                "cost_in_response": {"header": "x-meritorder-cost-usd", "streaming": False,
                                     "lookup": "/v1/reports/requests?tag=<name>=<value>", "request_id_header": "x-meritorder-request-id"},
                "attribution_passthrough": {"header": "x-meritorder-tags", "format": "name=value,name=value",
                                            "max_tags": MAX_TAGS, "max_value_length": 200, "key_tags_win": True},
                "cache_affinity": {"header": "x-session-id"},
                "embedding_route": bool(embeddings),
                "embedding_models": embeddings,
                # Mistral's OCR API, plus a multipart upload and `text` (every page's markdown); priced per page.
                "ocr_route": {"path": "/v1/ocr", "upload": "multipart file", "models": ocr_models} if ocr_models else False,
                "route_listing": {"path": "/v1/routes", "tiers": sorted(n for n in gw.cfg.tiers if gw.cfg.tier(n) is not None)},
                "data_policy_enforcement": {"allowed_providers_header": datapolicy.PROVIDERS_HEADER,
                                            "zero_retention_header": datapolicy.ZERO_RETENTION_HEADER, "refusal": "403 data_policy"},
                "mcp_gateway": {"path": "/mcp/<server>", "servers": "/v1/mcp/servers", "transport": "streamable_http",
                                "servers_registered": len([s for s in gw.cfg.mcp.servers if s.enabled])},
                "health": "/readyz",
            },
        }

    @app.get("/metrics")
    async def metrics(request: Request):
        token = os.environ.get("MERITORDER_METRICS_TOKEN")
        if token and not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "metrics token required")
        return PlainTextResponse(gw.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/about")
    async def about():
        """What's running and where its source is, for anyone (no key): the AGPL's offer of the source to the people who use
        it over the network (console.source_url), and the console bundle's third-party notices."""
        return {"name": "Merit Order", "version": __version__, "license": "AGPL-3.0-only", "source_url": gw.cfg.console.source_url,
                "third_party_notices": "/ui/third-party-notices.txt"}

    @app.get("/readyz")
    async def readyz():
        try:
            ok = await gw.store.ping() and await gw.state.ping()
        except Exception as e:
            return JSONResponse({"status": "unavailable", "error": str(e)}, status_code=503)
        return {"status": "ok" if ok else "unavailable", "backends": sorted(gw.backends)}

    @app.get("/auth/config")
    async def auth_config():
        """How people sign in here, for `meritorder login` and the console's sign-in page (no key needed)."""
        o = gw.cfg.auth.oidc
        local = {"enabled": gw.cfg.auth.local.enabled}
        if not o:
            return {"oidc": None, "local": local, "sso": None}
        out = {"issuer": o.issuer, "client_id": o.client_id, "scopes": o.scopes}
        if o.public_client_secret:  # Google's device login sends it; it's public for that kind of client
            out["client_secret"] = o.public_client_secret
        sso = {"provider": o.provider, "start": "/auth/sso/start"} if o.browser_ready else None
        return {"oidc": out, "local": local, "sso": sso}

    @app.post("/auth/exchange")
    async def auth_exchange(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "request body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object with an id_token")
        if "password" in body:  # `meritorder login` on a gateway with local accounts
            return await password_key(gw, request, str(body.get("email") or ""), str(body.get("password") or ""),
                                      str(body.get("code") or "") or None)
        try:
            return await gw.authn.exchange(str(body.get("id_token") or ""))
        except AuthError as e:
            raise HTTPException(e.status, e.message)

    app.include_router(build_batches_router(gw))
    app.include_router(build_stored_router(gw))
    app.include_router(build_reports_router(gw))
    app.include_router(build_accounts_router(gw))
    app.include_router(build_admin_router(gw))
    app.include_router(build_console_router(gw))
    app.include_router(build_scim_router(gw))
    app.include_router(build_controls_router(gw))
    app.include_router(build_mcp_router(gw))

    if (UI_DIR / "index.html").exists():
        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    else:
        # A checkout run with `uv run meritorder serve` has no console until it's built (the Docker image always has one).
        @app.get("/ui", include_in_schema=False)
        @app.get("/ui/{rest:path}", include_in_schema=False)
        async def console_not_built(rest: str = ""):
            return HTMLResponse(UI_NOT_BUILT, status_code=404)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/ui/")
    return app


UI_DIR = Path(__file__).parent / "ui_dist"
UI_NOT_BUILT = """<!doctype html><html><head><meta charset="utf-8"><title>Merit Order</title></head><body>
<h1>The console isn't built</h1>
<p>This gateway runs from a checkout without the console. Build it once, then restart <code>meritorder serve</code>:</p>
<pre>cd ui &amp;&amp; npm ci &amp;&amp; npm run build</pre>
<p>The Docker image always includes it. The APIs work either way.</p>
</body></html>"""
