"""The request pipeline (docs/02-request-flow.md).

auth -> budget -> session -> route -> shape/translate -> backend (with fallback)
-> stream back -> account (ledger, spend counters, session metadata).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import Authenticator, AuthError, Principal
from . import alerts, spend
from .affinity import UNREACHABLE_SECONDS, kv_session_id, mark, replicas_of
from .backends import Backend, BackendError, UpstreamResponse, describe
from .backends.base import MODEL_SCOPED_STATUS
from .cache_shaping import HOUR, has_cache_control, marker_ttls, needs_cache_marker, shape_anthropic, shape_openai
from .compression import Result as CompressionResult
from .compression import compress_tool_results, model_candidates
from .compressor import ModelCompressor
from .config import (BATCH_DISCOUNT, EFFORT_ORDER, Config, ModelConfig, Price, TeamConfig, batch_api, effort_format, needs_price,
                     unpriced_message, uses_reported_cost)
from .response_cache import cache_key, replay_stream, wanted
from .formats.responses import ChatToResponsesStream, ResponsesError, chat_to_responses, request_echo, responses_to_chat
from .formats.responses_tools import TOOLS_KEY, dropped_tools, fit_upstream
from .formats.responses_upstream import PROVIDER_CHAIN
from .formats.sse import SSEEvent, event
from .failover import check_body, error_in, start_stream
from .classifier import _recent_user_text, parse_floor
from .learned import KEY_ENV, KEY_SETTING, LearnedRouter, Model, new_key
from .judged import JUDGED_TAG, JudgedLabels, Sample, reply_text
from .formats.assemble import anthropic_message, openai_completion
from .gpu import active_control
from .formats.translate import (
    AnthropicToOpenAIStream,
    OpenAIToAnthropicStream,
    TranslationError,
    UsageTracker,
    anthropic_to_openai_request,
    anthropic_to_openai_response,
    openai_to_anthropic_request,
    openai_to_anthropic_response,
    server_tools,
    usage_from_anthropic,
    usage_from_openai,
)
from .metrics import Metrics, cost_kind
from .telemetry import record_span
from .pricing import Costs, Usage, attribute, chargeback, removed_cost
from .router import FIT_SHARE, Candidate, Decision, Router, RoutingError
from .sessions import SESSION_HEADERS, SESSION_TTL, Session, is_task_boundary, prefix_hash, session_id, session_key, thread_suffix
from .store import SAVINGS_FIELDS, month_start
from .tokens import SETTING as TOKENS_SETTING
from .pauses import SETTING as PAUSES_SETTING
from . import datapolicy, outcomes, screening
from .success import SETTING as SUCCESS_SETTING
from .success import VERSION_SETTING as SUCCESS_VERSION
from .success import Predictor, SuccessModel, TaskContext, project_hash, repo_hash, track, triage_buckets
from .tokens import Calibration, Estimate, count, estimate, reported_prompt_tokens, text_tokens

log = logging.getLogger("meritorder.proxy")


def _both(first, second):
    """One release for two in-flight slots (the deployment's and its replica's)."""

    def release() -> None:
        first()
        second()

    return release


def _then(close, release):
    """A stream's close callback that also releases its in-flight slot, exactly once."""
    async def wrapped() -> None:
        try:
            if close is not None:
                await close()
        finally:
            release()

    return wrapped


NEXT_BACKEND_ON = {401, 402, 403, 404}  # the backend's fault, not the request's: try the next one
# How providers say a prompt doesn't fit the model's context window (Anthropic, OpenAI, Gemini, OpenRouter, vLLM).
_TOO_LONG = re.compile(r"prompt is too long|input is too long|context.length|context.window|maximum context|"
                       r"too many (input )?tokens|exceeds the (maximum|max)", re.IGNORECASE)


def too_long(e: "BackendError") -> bool:
    """The prompt doesn't fit the model: a model with a bigger window can take it; the same one can't."""
    return e.status in (400, 413) and bool(_TOO_LONG.search(e.message or ""))
AFFORD_TRIES = 3  # retries of one route at what its account can afford (see _handle)
_AFFORD = re.compile(r"can only afford (\d+)")  # OpenRouter: "You requested up to 599400 tokens, but can only afford 538612"


def affordable(e: BackendError) -> int | None:
    """The output tokens a provider says its account can pay for, when it refuses a request for asking more (HTTP 402)."""
    m = _AFFORD.search(e.message or "") if e.status == 402 else None
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None
_TAG_PAIR = re.compile(r"^\s*([A-Za-z0-9_.-]{1,64})\s*=\s*([^,]{0,200}?)\s*$")


REFUSED = "refused by the model; retried on another model"  # the error recorded for a refused attempt
# On a 429 that a spending limit caused (a budget used up), unlike a rate limit: waiting won't clear it.
SPENDING_LIMIT = {"x-meritorder-limit": "budget"}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error", headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.headers = headers or {}


def error_body(fmt: str, status: int, message: str, kind: str) -> dict:
    if fmt == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "code": status}}


def error_response(fmt: str, status: int, message: str, kind: str = "api_error") -> JSONResponse:
    return JSONResponse(error_body(fmt, status, message, kind), status_code=status)




def fit_effort(effort: str | None, accepted: list[str]) -> str | None:
    """The client's effort if the model accepts it, else the highest accepted level below it (or the lowest)."""
    if not effort or not accepted or effort not in EFFORT_ORDER:
        return None
    if effort in accepted:
        return effort
    rank = EFFORT_ORDER.index(effort)
    known = sorted((e for e in accepted if e in EFFORT_ORDER), key=EFFORT_ORDER.index)
    below = [e for e in known if EFFORT_ORDER.index(e) <= rank]
    return (below[-1] if below else known[0]) if known else None


def route_reason(decision: Decision) -> str | None:
    """Why the request got its level and its model, as the ledger keeps it: rule names, levels, model names and windows,
    never prompt content (classifier.matched names the pattern, not the text)."""
    return (decision.reason or "")[:1000] or None


def kind_for_status(status: int) -> str:
    return {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
            413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}.get(status, "api_error")


MAX_TAGS = 32  # per request, from X-MeritOrder-Tags: enough for a pipeline's attribution (org, project, story, run, step...)


def parse_tags(value: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in (value or "").split(",")[:MAX_TAGS]:
        m = _TAG_PAIR.match(part)
        if m:
            tags[m.group(1)] = m.group(2)
    return tags


# Set by the gateway on the ledger row, never by a client or a key: how tool results were compressed (rules or model),
# which of the gateway's own requests for judged labels it was (replay or judge), and a summary it wrote for the Responses
# API's compaction (responses_compact.py), and the exploration arm of a task start and the level it would have had
# (decision 59), and an escalation inside a task: how many so far and the model it left (decision 62).
RESERVED_TAGS = ("compression", "compaction", JUDGED_TAG, "explore", "explore_from", "escalation", "escalated_from", "usage",
                 "screen", "screen_untrusted", "budget_step_down")


# Claude Code's own labels (its LLM-gateway headers; the last two with CLAUDE_CODE_GATEWAY_HINT_HEADERS=1, which `meritorder
# setup claude-code` sets), recorded as tags so reports split cost by agent, subagent type and kind of request.
CLAUDE_CODE_TAGS = {"x-claude-code-agent-id": "cc_agent", "x-claude-code-parent-agent-id": "cc_parent_agent",
                    "x-claude-code-agent-type": "cc_agent_type", "x-claude-code-request-class": "cc_request_class"}


def request_tags(headers: dict, principal: Principal) -> dict[str, str]:
    """A request's ledger tags: Claude Code's labels, then the client's X-MeritOrder-Tags, then its key's, without the ones
    the gateway sets; and on the gateway's own requests (judged labels' replays and judging), which they are, so reports
    can split them out."""
    labels = {tag: str(headers[h]).strip()[:200] for h, tag in CLAUDE_CODE_TAGS.items() if str(headers.get(h) or "").strip()}
    tags = {**labels, **parse_tags(headers.get("x-meritorder-tags", "")), **principal.tags}
    for k in RESERVED_TAGS:
        tags.pop(k, None)
    if principal.internal:
        tags[JUDGED_TAG] = principal.internal
    return tags


def _month_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


GPU_IDLE_TTL = 60  # seconds a gateway keeps the month's unpaid GPU time before reading it again (Gateway.gpu_idle)
EXPLORE_UP_SHARE = 0.9  # exploration never goes a level up once any budget that applies is this far spent


def budget_caps(pre: dict, p: Principal, team: TeamConfig, cfg: Config) -> list[tuple[str, float | None, float]]:
    """Each budget that applies to the request: (which, its cap, what's spent), from admit's one round trip."""
    month = _month_key()
    spent = pre.get("spend") or {}
    org = spent.get(f"{month}:org:all")
    return [("organization", cfg.safeguards.org_budget_usd_month, (org or 0.0) + (pre.get("gpu_idle") or 0.0)),
            ("person", p.monthly_limit(team.user_budget_usd_month), spent.get(f"{month}:user:{p.user_id}") or 0.0),
            ("team", team.budget_usd_month, spent.get(f"{month}:team:{p.team}") or 0.0),
            ("session", team.max_session_usd, pre.get("session_spend") or 0.0)]


def step_down(pre: dict, p: Principal, team: TeamConfig, cfg: Config) -> tuple[int, str] | None:
    """The level new `auto` tasks are capped at, and why, once a budget that applies is ``budget_step_down.at`` spent."""
    s = team.budget_step_down
    if s is None:
        return None
    worst = max(((used / cap, which) for which, cap, used in budget_caps(pre, p, team, cfg) if cap), default=None)
    if worst is None or worst[0] < s.at:
        return None
    return s.level, f"the {worst[1]}'s budget is {worst[0]:.0%} spent"


def steps_down(cfg: Config, team: TeamConfig, requested: str) -> bool:
    """Whether the team steps down and this request is one it applies to: `auto`, or a tier with a level."""
    if team.budget_step_down is None:
        return False
    tier = cfg.tier(requested)
    return requested in ("", cfg.routing.auto_model) or (tier is not None and tier.level is not None)


def headroom(pre: dict, p: Principal, team: TeamConfig, cfg: Config) -> bool:
    """Whether every budget that applies to the request (the organization's, the person's, the team's, the session's)
    is under 90% spent: exploring a level up costs more, and must never be what uses a budget up."""
    month = _month_key()
    spent = pre.get("spend") or {}
    org = spent.get(f"{month}:org:all")
    caps = [(cfg.safeguards.org_budget_usd_month, None if org is None else org + (pre.get("gpu_idle") or 0.0)),
            (p.monthly_limit(team.user_budget_usd_month), spent.get(f"{month}:user:{p.user_id}")),
            (team.budget_usd_month, spent.get(f"{month}:team:{p.team}")),
            (team.max_session_usd, pre.get("session_spend"))]
    return all(cap is None or (used or 0.0) < EXPLORE_UP_SHARE * cap for cap, used in caps)


@dataclass
class Ctx:
    principal: Principal
    team: TeamConfig
    decision: Decision
    cand: Candidate
    client_fmt: str
    session: Session
    body: dict
    prompt_tokens: int  # the request's estimate, model-neutral (tokens.Estimate.tokens)
    proxy_cache: bool
    stream: bool
    include_usage: bool
    t0: float
    tags: dict = field(default_factory=dict)
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    compression: CompressionResult = field(default_factory=CompressionResult)
    cache_key: str | None = None
    wire: str = ""  # "responses" when the client called /v1/responses (pipeline runs as "openai")
    kv_id: str = ""  # the conversation's id on GPU servers (affinity.kv_session_id)
    upstream_effort: str | None = None
    refused_by: list[str] = field(default_factory=list)
    trace_carrier: dict = field(default_factory=dict)  # W3C traceparent/tracestate from the client
    failed_over: list[str] = field(default_factory=list)  # "backend/model status" tried first and failed
    sent_at: float = 0.0  # monotonic time this route was called (t0 plus any failed attempts before it)
    output_limit: tuple[int, str] | None = None  # most output tokens allowed, and why (Gateway.output_allowance)
    estimate: Estimate | None = None  # the request's token estimate, before compression (for calibration)
    sample: Sample | None = None  # sampled for a judged label (judged.py): its prompt and answer, in memory only
    stored: Any = None  # stored.service.StoredTurn: a /v1/responses turn in its team's stored mode, saved when it's done
    echo: dict | None = None  # /v1/responses: what the response object repeats from the request (request_echo)
    # The provider was sent only what it didn't hold of the conversation (its own previous_response_id): its prompt count
    # includes what it kept (reasoning items) and isn't the gateway's estimate's to learn from.
    provider_chained: bool = False
    cache_ttls: list[int] = field(default_factory=list)  # each cache marker sent upstream, in seconds (cache_shaping.marker_ttls)
    # expected_cost (decision 62): what this request takes off its routing savings for the failed attempt it escalated from,
    # shared by its attempts (one item, or none): the answer's row takes it (never a refused attempt's), once.
    # And the conversation's spend counters as admitted ("task:", "task-base:"), None when its team doesn't route that way.
    charge: list[float] = field(default_factory=list)
    task_spent: dict | None = None

    @property
    def sent_estimate(self) -> Estimate:
        """The estimate of what went upstream: compression took tokens out."""
        return (self.estimate or Estimate(text=self.prompt_tokens)).less_text(self.compression.removed_tokens)

    @property
    def label(self) -> str:
        return self.cand.model.name if self.decision.auto else (self.decision.requested or self.cand.model.name)


class InFlight:
    """Requests this gateway process has open per backend. Entries older than the backend timeout are
    dropped, so a leaked release (a stream the client never started reading) can't pin a count."""

    def __init__(self) -> None:
        self._open: dict[str, dict[int, float]] = {}
        self._seq = 0

    def acquire(self, backend: str, ttl: float):
        self._seq += 1
        token, deadline = self._seq, time.monotonic() + ttl
        self._open.setdefault(backend, {})[token] = deadline

        def release() -> None:
            self._open.get(backend, {}).pop(token, None)

        return release

    def count(self, backend: str) -> int:
        open_ = self._open.get(backend)
        if not open_:
            return 0
        now = time.monotonic()
        for token in [t for t, deadline in open_.items() if deadline < now]:
            del open_[token]
        return len(open_)

    def counts(self) -> dict[str, int]:
        return {b: self.count(b) for b in list(self._open)}


class Gateway:
    def __init__(self, cfg: Config, store, state, backends: dict[str, Backend], router: Router, authn: Authenticator):
        self.cfg = cfg
        self.store = store
        self.state = state
        self.backends = backends
        self.router = router
        self.authn = authn
        self.metrics = Metrics()
        self.config_version: int | None = None
        self.file_cfg = cfg  # the YAML-file config, used until a version is saved in the database
        self._tasks: set[asyncio.Task] = set()
        self._retired: dict[asyncio.Task, list[Backend]] = {}  # replaced backends, closed after a grace period
        self.gateway_id = f"{socket.gethostname()}-{os.getpid()}"
        self.inflight = InFlight()
        self._batches = None
        self._stored = None
        self._gpu_idle: tuple[str, float, float] | None = None  # (month, when read, amount): see gpu_idle
        self.metrics.gauges = self.gauges
        # Prompt-token calibration per model (tokens.py): shared with the router, kept across reloads, saved to the store.
        self.tokens = router.tokens if router is not None else Calibration()
        # The learned router's model and feature key (learned.py): kept across reloads, reloaded when a new one is trained.
        self.learned = router.learned if router is not None else LearnedRouter()
        self.judged = JudgedLabels(self)  # judged labels for the learned router: samples, replays and judging (judged.py)
        self.compressor = self.build_compressor(cfg)
        self.follow_reporting(cfg)

    def build_compressor(self, cfg: Config) -> ModelCompressor | None:
        """The client for model-based compression's service, when the configuration names one."""
        return ModelCompressor(cfg.compressor, self.state, self.metrics) if cfg.compressor.url else None

    async def compress(self, body: dict, fmt: str, team: TeamConfig) -> tuple[dict, CompressionResult]:
        """Tool-result compression for one request: the team's rules, then model-based compression of what's left
        (compressor.py), which looks up or makes each result's stored decision first."""
        cfg = team.compression
        compressed, outcomes = None, None
        items, rules = model_candidates(body, fmt, cfg)
        if items and self.compressor is not None:
            compressed, outcomes = await self.compressor.compress(team.name, cfg.model, items)
        send_body, result = compress_tool_results(body, fmt, cfg, compressed, rules)
        for outcome, n in (outcomes or {}).items():
            self.metrics.inc("meritorder_compression_model_results_total", {"result": outcome}, n)
        return send_body, result

    def follow_reporting(self, cfg: Config) -> None:
        """Large reports read ClickHouse when `reporting.clickhouse` is set (clickhouse.py)."""
        if hasattr(self.store, "use_clickhouse"):
            self.store.use_clickhouse(cfg.reporting.clickhouse)

    @property
    def batches(self):
        """Batches through the providers' batch APIs (batches/service.py)."""
        if self._batches is None:
            from .batches.service import Batches

            self._batches = Batches(self)
        return self._batches

    @property
    def stored(self):
        """The Responses API's stored mode (stored/service.py): opt-in per team, where the gateway holds content."""
        if self._stored is None:
            from .stored.service import StoredResponses

            self._stored = StoredResponses(self)
        return self._stored

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def apply_config(self, cfg: Config, version: int | None) -> None:
        """Swap in a new configuration. In-flight requests finish on the objects they started with."""
        from .backends import build_backends

        old = self.backends
        backends = build_backends(cfg)
        retired_compressor = None
        if cfg.compressor != self.cfg.compressor:
            retired_compressor, self.compressor = self.compressor, self.build_compressor(cfg)
        self.cfg, self.backends = cfg, backends
        # Measured speeds, token ratios and the learned router's model outlive it.
        self.router = Router(cfg, backends, self.router.speeds, self.tokens, self.learned, self.router.pauses, self.router.success)
        self.authn = Authenticator(cfg, self.store, self.state)
        self.follow_reporting(cfg)
        self.config_version = version
        await self.refresh_deployments()

        retired = list(old.values())

        async def close_later():
            await asyncio.sleep(300)  # long generations can stream for minutes
            for b in retired:
                await b.close()
            if retired_compressor is not None:
                await retired_compressor.close()

        task = asyncio.create_task(close_later())
        self._retired[task] = retired
        task.add_done_callback(lambda t: self._retired.pop(t, None))
        log.info("configuration version %s applied; backends: %s", version, ", ".join(sorted(backends)) or "(none)")

    def observe(self, row: dict, trace_carrier: dict | None = None, spend_keys: list[str] | None = None,
                extra: dict[str, float] | None = None) -> None:
        """Telemetry: metrics, an OpenTelemetry span, and (in one Redis round trip) the console's shared
        request feed, backend health, and the spend counters."""
        self.metrics.record(row, cost_kind(self.cfg, row.get("backend")))
        try:
            record_span(row, trace_carrier)
        except Exception:
            log.exception("could not record a trace span")
        tokens = sum(row.get(f) or 0 for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
        savings = sum(row.get(f) or 0 for f in SAVINGS_FIELDS)
        event = {
            "ts": row["ts"].isoformat() if hasattr(row.get("ts"), "isoformat") else row.get("ts"),
            "user": row.get("user_id"), "team": row.get("team"), "kind": row.get("client_format"),
            "requested": row.get("requested_model"), "model": row.get("served_model"), "backend": row.get("backend"),
            "backend_kind": row.get("backend_kind"), "floor": row.get("floor"), "reason": row.get("route_reason"), "status": row.get("status"),
            "upgraded": row.get("upgraded"), "tokens": tokens, "cached": row.get("cache_read_tokens") or 0,
            "cost": round(row.get("actual_cost") or 0, 6), "saved": round(savings, 6),
            "latency_ms": row.get("latency_ms"), "error": row.get("error"),
        }
        health = None
        if row.get("backend") and row.get("backend_kind") != "cache":
            health = (row["backend"], (row.get("status") or 0) < 400, row.get("latency_ms"), row.get("ttft_ms"))
        amount = (row.get("actual_cost") or 0) if spend_keys else 0
        self.spawn(self.state.record_request(event, health, spend_keys or [], amount, extra))

    async def refresh_deployments(self) -> None:
        """Pick up endpoints and states the capacity controller published, and admin GPU controls."""
        await refresh_deployments(self.backends, self.state, self.store)

    async def gpu_utilization(self) -> dict[str, float]:
        """Utilization per GPU deployment as routing sees it now: the controller's sample and every gateway's live requests."""
        return self.load(await self.state.gpu_load())[0]

    async def load_token_calibration(self) -> None:
        """Start from the token ratios saved by earlier runs (and other gateways), and every gateway's session pauses."""
        self.tokens.load(await self.store.get_setting(TOKENS_SETTING))
        self.router.pauses.load(await self.store.get_setting(PAUSES_SETTING))

    async def save_token_calibration(self) -> None:
        """Save what this process learned since its last save, keeping other gateways' ratios for other models; and its
        session pause counts (pauses.py), reading every other gateway's back."""
        pauses = self.router.pauses
        doc = await self.store.get_setting(PAUSES_SETTING)
        pauses.load(doc)
        if pauses.dirty:
            pauses.dirty = False
            await self.store.set_setting(PAUSES_SETTING, pauses.merged(doc))
        if not self.tokens.dirty:
            return
        doc = self.tokens.merged(await self.store.get_setting(TOKENS_SETTING))
        self.tokens.dirty.clear()
        await self.store.set_setting(TOKENS_SETTING, doc)

    def set_learned(self, learned: LearnedRouter) -> None:
        self.learned = learned
        self.router.learned = learned

    async def load_learned(self, version: int | None) -> None:
        """Follow the stored learned-router model (``version``: the active one, None for none) while the learned router is
        on, with the key its features are hashed with: MERITORDER_FEATURE_KEY, else one kept in the store (made once)."""
        if self.cfg.routing.learned.mode == "off":
            return
        current = self.learned
        key = os.environ.get(KEY_ENV) or await self.store.get_setting(KEY_SETTING)
        if not key:
            await self.store.add_setting(KEY_SETTING, new_key())  # the first gateway to get here makes it; the rest read it
            key = await self.store.get_setting(KEY_SETTING)
        loaded = current.model.version if current.model else None
        if current.key_loaded and version == loaded and not current.error and current.seed == LearnedRouter(key=key).seed:
            return
        model, error = None, None
        if version is not None:
            row = await self.store.router_model(version)
            try:
                if row is None:
                    raise LookupError("it isn't in the store")
                model = Model(row["doc"], version, row.get("created_at"))
            except Exception as e:  # noqa: BLE001 (the rules decide meanwhile, and the routing reason says why)
                log.exception("could not load learned router model v%s", version)
                error = f"couldn't load model v{version} ({type(e).__name__})"
        self.set_learned(LearnedRouter(model, key, error))
        log.info("learned router: %s", f"model v{version}" if model else error or "no model yet")

    async def load_success(self, version: int | None) -> None:
        """Follow the success predictor's fitted layer (success.py, decision 62) while routing.success.mode is on: its stored
        document when its version changes. Off, or none trained: the benchmark prior alone (the routing reason says so)."""
        current = self.router.success
        if self.cfg.routing.success.mode == "off" or version is None:
            if current.model is not None or current.error:
                self.router.success = Predictor()
            return
        if current.model is not None and current.model.version == version:
            return
        model, error = None, None
        try:
            doc = await self.store.get_setting(SUCCESS_SETTING)
            if not doc:
                raise LookupError("it isn't in the store")
            model = SuccessModel(doc, version)
        except Exception as e:  # noqa: BLE001 (the benchmark prior decides meanwhile)
            log.exception("could not load the success model v%s", version)
            error = f"couldn't load success model v{version} ({type(e).__name__})"
        self.router.success = Predictor(model, error)
        log.info("success predictor: %s", f"fitted layer v{version}" if model else error)

    def learn_tokens(self, model: str, sent: Estimate, reported: int) -> None:
        """A provider's prompt token count for a request: calibrates this model's estimates (tokens.Calibration)."""
        if self.tokens.record(model, sent, reported):
            self.metrics.inc("meritorder_token_calibration_samples_total", {"model": model})

    def gauges(self) -> list[tuple[str, str, dict, float]]:
        """Current values for /metrics. GPU figures come from the controller's published state, so every
        gateway reports the same numbers: aggregate them with max(), not sum()."""
        now = time.time()
        out = []
        for b in self.backends.values():
            lab = {"backend": b.name}
            out.append(("meritorder_inflight_requests", "Requests this gateway process has open to the backend.", lab,
                        self.inflight.count(b.name)))
            out.append(("meritorder_backend_available", "1 if the backend can take new requests (enabled, running, circuit closed).",
                        lab, 1.0 if b.available and b.healthy(now) and not b.draining else 0.0))
            rec = getattr(b, "deploy", None)
            if b.kind == "self_hosted" and rec is not None:
                running = rec.get("state") in ("provisioning", "warm", "draining")
                out.append(("meritorder_gpu_replicas", "Replicas the controller has running (billing) for the deployment.", lab,
                            float(rec.get("replicas") or 0) if running else 0.0))
                out.append(("meritorder_gpu_usd_per_hour", "What the deployment's running replicas cost per hour.", lab,
                            float(rec.get("burn_usd_per_hour") or 0) if running else 0.0))
                if rec.get("utilization") is not None:
                    out.append(("meritorder_gpu_utilization", "vLLM load at the controller's last sample (0-1).", lab, float(rec["utilization"])))
                if rec.get("waiting") is not None:
                    out.append(("meritorder_gpu_waiting_requests", "Requests queued in vLLM at the controller's last sample.", lab,
                                float(rec["waiting"])))
        for m in self.cfg.models:
            if m.name in self.tokens.models:
                out.append(("meritorder_token_ratio", "Prompt tokens the model's replies report per token the gateway estimated "
                            "(the calibration applied to its estimates).", {"model": m.name}, self.tokens.ratio(m.name)))
        return out

    def load(self, pre: dict) -> tuple[dict[str, float], set[str]]:
        """Utilization per self-hosted backend for routing, and the saturated ones whose queue is full.

        The controller samples vLLM's metrics once per cycle; with ``max_concurrency`` set, every gateway
        also counts in-flight requests (its own live, other gateways' as published every couple of
        seconds), so a burst saturates the backend for routing at once."""
        util = dict(pre.get("util") or {})
        others = [counts for gw, counts in (pre.get("inflight") or {}).items() if gw != self.gateway_id]
        full: set[str] = set()
        for b in self.backends.values():
            if b.kind != "self_hosted":
                continue
            replicas = max(1, b.replicas or 1)
            n = self.inflight.count(b.name) + sum(c.get(b.name, 0) for c in others)
            if b.cfg.max_concurrency:
                slots = b.cfg.max_concurrency * replicas
                util[b.name] = max(util.get(b.name, 0.0), n / slots)
                waiting = max(0, n - slots)
                for r in b.replica_set:  # requests sent to one replica directly (affinity) fill that replica
                    key = f"{b.name}#{r.id}"
                    mine = self.inflight.count(key) + sum(c.get(key, 0) for c in others)
                    if mine:
                        util[key] = mine / b.cfg.max_concurrency
            else:
                # vLLM's waiting count from the controller's last sample. Without one from the last 5 minutes (the controller
                # stopped, say), every request the gateways have in flight there may be waiting: the bound holds, not lifts.
                waiting = b.reported_waiting if b.reported_waiting is not None else n
            if waiting >= b.cfg.max_waiting * replicas:
                full.add(b.name)
        return util, full

    async def drain(self) -> None:
        """Finish background work (ledger writes, telemetry). Backends retired by a config change are
        closed now rather than after their grace period, so shutdown doesn't wait for it."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for task, backends in list(self._retired.items()):
            task.cancel()
            for b in backends:
                await b.close()
        self._retired.clear()

    # ------------------------------------------------------------------ failures

    def backend_failed(self, backend: Backend, model: str, e: BackendError, served: ModelConfig | None) -> None:
        """Count a failure against the backend (or the model on it), and alert when it starts being skipped.
        ``served`` is the model the request was for: the alert lists its other live routes, whatever this one
        request was limited to (a pinned backend, a team's allowed backends)."""
        now = time.time()
        self.metrics.inc("meritorder_failovers_total", {"backend": backend.name, "status": str(e.status)})
        out = backend.record_failure(now, model, e.status, e.retry_after, e.message)
        if out is None:
            return
        scoped = e.status in MODEL_SCOPED_STATUS
        alternatives = list(dict.fromkeys(
            r.backend for r in (served.routes if served else [])
            if r.backend != backend.name and r.backend in self.backends and self.backends[r.backend].available
            and self.backends[r.backend].skipped(now, r.model) is None))
        log.warning("skipping %s for %.0fs: %s", f"{backend.name} ({model})" if scoped else backend.name, out.until - now, e.message)
        self.spawn(alerts.outage(self.state, backend.name, model if scoped else None, f"{describe(e.status)} ({e.message})",
                                 out.until - now, alternatives))

    # ------------------------------------------------------------------ budgets

    async def spend(self, kind: str, ident: str) -> float:
        """This month's spend for the organization, a person or a team, as budgets count it: the ledger, plus what running
        batches have reserved (batches/service.py). Redis keeps it; a missing counter is seeded from Postgres."""
        key = f"{_month_key()}:{kind}:{ident}"
        value = await self.state.get_spend(key)
        if value is None:
            # What requests add while the ledger is read is kept aside and added to the seed, so none is lost. The counter
            # is spend.budget_spend's ledger + reserved (admit() adds the GPU time no request paid for itself), and the
            # worker checks it against the same sum each cycle (counters.py), correcting one that drifted.
            await self.state.begin_seed(key)
            s = await spend.budget_spend(self.store, self.cfg, kind, None if kind == "org" else ident)
            value = await self.state.seed_spend(key, s["ledger"] + s["reserved"])
        return value

    async def gpu_idle(self) -> float:
        """This month's GPU time no request paid for (spend.py): the organization's budget counts it on top of the ledger,
        so a GPU left idle uses the budget up as its bill grows. Read at most once a minute in each gateway process: the
        controller adds GPU time once a cycle. If the store can't answer, the last amount read stands."""
        month, now = _month_key(), time.monotonic()
        cached = self._gpu_idle
        if cached is None or cached[0] != month or now - cached[1] >= GPU_IDLE_TTL:
            try:
                value = await spend.gpu_idle(self.store, self.cfg, month_start())
            except Exception:  # noqa: BLE001 (budgets go on with the last amount; the next minute tries again)
                log.exception("couldn't read GPU spend for the organization's budget")
                value = cached[2] if cached is not None and cached[0] == month else 0.0
            self._gpu_idle = cached = (month, now, value)
        return cached[2]

    async def admit(self, p: Principal, team: TeamConfig, prompt_tokens: int, sid: str | None = None,
                    thread: str | None = None, kv_id: str | None = None, extra_keys: list[str] | None = None,
                    stepping: bool = False) -> dict:
        """Budgets, rate limits, and the session cap from one Redis round trip. Returns what it fetched
        (session, session spend, GPU utilization, KV-event notes) for routing. ``extra_keys``: other spend counters to read
        in the same trip (a conversation's own, for expected_cost's task costs)."""
        month = _month_key()
        keys = {"org": f"{month}:org:all", "user": f"{month}:user:{p.user_id}", "team": f"{month}:team:{p.team}"}
        rl = team.rate_limits
        checks = [
            (rl.user_rpm, f"rpm:user:{p.user_id}", 1, f"{p.user_id} is limited to {rl.user_rpm} requests per minute"),
            (rl.team_rpm, f"rpm:team:{p.team}", 1, f"team '{p.team}' is limited to {rl.team_rpm} requests per minute"),
            (rl.user_tpm, f"tpm:user:{p.user_id}", prompt_tokens, f"{p.user_id} is limited to {rl.user_tpm:,} tokens per minute" if rl.user_tpm else ""),
            (rl.team_tpm, f"tpm:team:{p.team}", prompt_tokens, f"team '{p.team}' is limited to {rl.team_tpm:,} tokens per minute" if rl.team_tpm else ""),
        ]
        checks = [c for c in checks if c[0] is not None]
        pre = await self.state.admit(list(keys.values()) + list(extra_keys or []), [(key, 60, amount) for _, key, amount, _ in checks],
                                     sid, thread, kv_id)
        for kind, key in keys.items():
            if pre["spend"][key] is None:  # first request this month, or Redis was flushed: seed from the ledger
                pre["spend"][key] = await self.spend(kind, "all" if kind == "org" else (p.user_id if kind == "user" else p.team))
        spent = pre["spend"]
        cap = self.cfg.safeguards.org_budget_usd_month
        if cap is not None:
            # The organization's spend: the ledger's counter, and GPU time no request paid for (decision 64).
            pre["gpu_idle"] = idle = await self.gpu_idle()
            if spent[keys["org"]] + idle >= cap:
                raise GatewayError(429, f"the organization's monthly LLM budget of ${cap:,.2f} is used up (spent "
                                        f"${spent[keys['org']] + idle:,.2f}, GPU time included); "
                                        "an admin can raise it on the console's Controls page", "rate_limit_error", SPENDING_LIMIT)
        # A team stepping down (decision 74) may let stepped-down requests go past its and its people's budgets by
        # grace_share; the organization's budget and the session limit stay hard.
        grace = 1 + (team.budget_step_down.grace_share if stepping and team.budget_step_down else 0.0)
        limit = p.monthly_limit(team.user_budget_usd_month)
        if limit is not None and spent[keys["user"]] >= limit * grace:
            raise GatewayError(429, f"monthly budget of ${limit:,.2f} reached for {p.user_id} "
                                    f"(spent ${spent[keys['user']]:,.2f}); ask an admin to raise it", "rate_limit_error", SPENDING_LIMIT)
        if team.budget_usd_month is not None and spent[keys["team"]] >= team.budget_usd_month * grace:
            raise GatewayError(429, f"team '{p.team}' reached its monthly budget of ${team.budget_usd_month:,.2f}", "rate_limit_error",
                               SPENDING_LIMIT)
        for limit, key, _, message in checks:
            if pre["rates"].get(key, 0) > limit:
                retry = max(1, 60 - int(time.time()) % 60)
                raise GatewayError(429, f"rate limit: {message}; retry in {retry}s", "rate_limit_error", {"retry-after": str(retry)})
        if sid is not None and team.max_session_usd is not None:
            s_spent = pre["session_spend"] or 0.0
            if s_spent >= team.max_session_usd:
                raise GatewayError(429, f"this session has spent ${s_spent:,.2f}, reaching the ${team.max_session_usd:,.2f} per-session limit. "
                                        f"Start a new session, or ask an admin to raise max_session_usd.", "rate_limit_error", SPENDING_LIMIT)
        return pre

    @staticmethod
    def spend_keys(p: Principal, session_id: str | None = None, thread: str | None = None) -> list[str]:
        """The counters a request's cost adds to. ``thread``: the conversation's own (``task:``), from which a task's cost is
        its difference between the task's start and end (expected_cost, decision 62)."""
        month = _month_key()
        keys = [f"{month}:user:{p.user_id}", f"{month}:team:{p.team}", f"{month}:org:all"]
        return keys + ([f"session:{session_id}"] if session_id else []) + ([f"task:{thread}"] if thread else [])

    # ------------------------------------------------------------------ entry points

    def reject(self, fmt: str, status: int, message: str, kind: str, headers: dict | None = None) -> Response:
        """An error answered before any backend was called (auth, budget, rate limit, routing): counted in
        /metrics, since it never reaches the ledger. Labels are status and error kind only (fixed sets)."""
        self.metrics.inc("meritorder_rejected_requests_total", {"status": str(status), "reason": kind})
        resp = error_response(fmt, status, message, kind)
        resp.headers.update(headers or {})
        return resp

    async def handle(self, request: Request, client_fmt: str, wire: str = "") -> Response:
        t0 = time.monotonic()
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            principal = await self.authn.authenticate(headers)
        except AuthError as e:
            return self.reject(client_fmt, e.status, e.message, "authentication_error")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.reject(client_fmt, 400, "request body must be valid JSON", "invalid_request_error")
        return await self.respond(principal, body, headers, client_fmt, t0, wire)

    async def respond(self, principal: Principal, body: Any, headers: dict, client_fmt: str, t0: float, wire: str = "",
                      tags: dict | None = None) -> Response:
        """A request read and authenticated, through the pipeline. HTTP, the Responses API's WebSocket transport
        (responses_ws.py) and compaction (responses_compact.py) all come here, so each is routed, admitted, metered and
        stored the same way. ``tags``: ledger tags the gateway sets (RESERVED_TAGS)."""
        turn, echo = None, None
        if wire == "responses":
            from .stored.service import StoredError

            if not isinstance(body, dict):
                return self.reject(client_fmt, 400, "request body must be a JSON object", "invalid_request_error")
            try:
                # A turn that chains or belongs to a conversation becomes the stateless request it amounts to: the stored
                # history and its own input (stored/service.py). Everything after this sees only that.
                turn = await self.stored.begin(principal, body)
                echo = request_echo(body)
                body = self.stored.rebuilt(turn, body) if turn is not None else responses_to_chat(body)
            except StoredError as e:
                return self.reject(client_fmt, e.status, e.message, e.kind)
            except ResponsesError as e:
                return self.reject(client_fmt, 400, str(e), "invalid_request_error")
            if turn is not None and turn.session_id and not any(headers.get(h) for h in SESSION_HEADERS):
                headers["x-meritorder-session"] = turn.session_id  # the chain's or conversation's: it stays on its route
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return self.reject(client_fmt, 400, "request body must be a JSON object with a 'messages' list", "invalid_request_error")

        async def run() -> Response:
            try:
                return await self._handle(principal, body, headers, client_fmt, t0, wire, turn, echo, tags)
            except (GatewayError, RoutingError) as e:
                return self.reject(client_fmt, e.status, e.message, e.kind, getattr(e, "headers", {}))

        if turn is not None and turn.background:
            return JSONResponse(await self.stored.start_background(turn, run))
        return await run()

    async def count_tokens(self, request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            principal = await self.authn.authenticate(headers)
            body = await request.json()
        except AuthError as e:
            return self.reject("anthropic", e.status, e.message, "authentication_error")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.reject("anthropic", 400, "request body must be valid JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return self.reject("anthropic", 400, "request body must be a JSON object", "invalid_request_error")
        team = self.cfg.team(principal.team)
        requested = str(body.get("model") or "")
        model = self.cfg.model(requested)
        if model is None and requested not in ("", self.cfg.routing.auto_model) and self.cfg.tier(requested) is None:
            # As /v1/messages says (router.decide): a count for a model that can't serve the request would mislead.
            return self.reject("anthropic", 404, f"model '{requested}' is not configured", "not_found_error")
        if model is not None and not team.allows_model(model.name):
            return self.reject("anthropic", 403, f"model '{model.name}' is not allowed for team '{team.name}'", "permission_error")
        try:
            await self.admit(principal, team, 0)  # budgets and request-rate limits; counting uses no tokens
        except GatewayError as e:
            return self.reject("anthropic", e.status, e.message, e.kind, e.headers)
        models = [model] if model else [m for m in self.cfg.models if team.allows_model(m.name)]
        est = estimate({k: body.get(k) for k in ("system", "messages", "tools")})
        now = time.time()
        for m in models:
            for route in m.routes:
                backend = self.backends.get(route.backend)
                # The prompt goes to the backend, so the team's backend policy (e.g. data residency) applies.
                if (backend is None or backend.format != "anthropic" or backend.kind == "mock" or not team.allows_backend(route.backend)
                        or not backend.available or not backend.healthy(now, route.model)):
                    continue
                try:
                    result = await backend.count_tokens({**body, "model": route.model}, headers)
                except BackendError:
                    continue
                if result:
                    if isinstance(result.get("input_tokens"), int):  # an exact count: calibrates the estimate for free
                        self.learn_tokens(m.name, est, result["input_tokens"])
                    return JSONResponse(result)
        return JSONResponse({"input_tokens": self.tokens.count(model.name, est) if model else est.tokens})

    async def embeddings(self, request: Request) -> Response:
        """/v1/embeddings: exact model only (vectors from different models aren't comparable);
        the cheapest healthy backend serving that model wins, with fallback."""
        t0 = time.monotonic()
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            principal = await self.authn.authenticate(headers)
            body = await request.json()
        except AuthError as e:
            return self.reject("openai", e.status, e.message, "authentication_error")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.reject("openai", 400, "request body must be valid JSON", "invalid_request_error")
        if not isinstance(body, dict) or "input" not in body:
            return self.reject("openai", 400, "request body must include 'model' and 'input'", "invalid_request_error")
        team = self.cfg.team(principal.team)
        model = self.cfg.model(str(body.get("model") or ""))
        try:
            if model is None or model.kind != "embedding":
                raise GatewayError(404, f"embedding model '{body.get('model')}' is not configured", "not_found_error")
            if not team.allows_model(model.name):
                raise GatewayError(403, f"model '{model.name}' is not allowed for team '{team.name}'", "permission_error")
            await self.admit(principal, team, count(body.get("input")))
        except GatewayError as e:
            return self.reject("openai", e.status, e.message, e.kind, e.headers)
        now = time.time()
        usable = [r for r in model.routes
                  if r.backend in self.backends and self.backends[r.backend].format == "openai" and hasattr(self.backends[r.backend], "embed")
                  and self.backends[r.backend].healthy(now) and team.allows_backend(r.backend)]
        if (policy := datapolicy.from_headers(headers)) is not None:  # the caller's data policy (decision 72)
            why = {r.backend: no for r in usable if (no := policy.refuses(self.backends[r.backend].cfg))}
            usable = [r for r in usable if r.backend not in why]
            if why and not usable:
                return self.reject("openai", 403, datapolicy.refusal(policy, why), "permission_error")
        # An unknown price must never look free: a route with none isn't used (GPUs are charged by their time instead).
        unpriced = [r for r in usable if needs_price(r, self.backends[r.backend].kind)]
        usable = [r for r in usable if r not in unpriced]
        if unpriced and not usable:
            return self.reject("openai", 503, unpriced_message(model.name, unpriced), "overloaded_error")
        routes = sorted((r for r in usable if self.backends[r.backend].healthy(now, r.model)), key=lambda r: (not r.priced, r.cost_price().input))
        if not routes:  # every route is over capacity: try the one that frees up first rather than refuse
            busy = [(o.until, r) for r in usable if (o := self.backends[r.backend].skipped(now, r.model)) and o.capacity]
            routes = [r for _, r in sorted(busy, key=lambda x: x[0])]
        last: BackendError | None = None
        for route in routes[: self.cfg.routing.max_attempts]:
            backend = self.backends[route.backend]
            try:
                resp = await backend.embed({**body, "model": route.model})
                check_body(resp, "openai", backend.name)
            except BackendError as e:
                last = e
                if e.retryable or e.status in NEXT_BACKEND_ON:
                    self.backend_failed(backend, route.model, e, model)
                    continue
                break
            if backend.record_success(route.model):
                self.spawn(alerts.recovered(self.state, backend.name, None))
                self.spawn(alerts.recovered(self.state, backend.name, route.model))
            data = resp.body or {}
            tokens = int((data.get("usage") or {}).get("prompt_tokens") or (data.get("usage") or {}).get("total_tokens") or 0)
            if backend.kind == "self_hosted" and not route.priced:
                cost = chargeback(getattr(backend, "live_cost_per_hour", None) or backend.cfg.gpu_cost_per_hour,
                                  backend.cfg.capacity_tokens_per_second, Usage(input_tokens=tokens))
            else:
                cost = tokens * route.cost_price().input / 1e6
            row = {f: 0 for f in ("cache_read_tokens", "cache_write_tokens", "output_tokens", "list_cost", *SAVINGS_FIELDS)}
            row.update(id=uuid.uuid4(), ts=datetime.now(UTC), user_id=principal.user_id, team=principal.team, key_id=principal.key_id,
                       session_id=None, tags=request_tags(headers, principal),
                       client_format="embeddings", requested_model=str(body.get("model")), floor=None, served_model=model.name,
                       upstream_model=route.model, backend=backend.name, backend_kind=backend.kind, status=200, error=None,
                       stream=False, upgraded=False, proxy_cache=False, input_tokens=tokens,
                       latency_ms=int((time.monotonic() - t0) * 1000), ttft_ms=None,
                       actual_cost=cost, served_api_cost=cost, baseline_cost=cost)
            row["list_cost"] = cost
            self.observe(row, spend_keys=self.spend_keys(principal))
            self.spawn(self.store.record(row))
            data["model"] = model.name
            return JSONResponse(data, headers={"x-meritorder-model": model.name, "x-meritorder-backend": backend.name,
                                               "x-meritorder-cost-usd": f"{cost:.8f}"})
        if last is None:
            return self.reject("openai", 503, f"no healthy backend serves '{model.name}'", "overloaded_error")
        return error_response("openai", last.status, last.message, kind_for_status(last.status))

    # ------------------------------------------------------------------ pipeline

    async def _handle(self, principal: Principal, body: dict, headers: dict, client_fmt: str, t0: float, wire: str = "",
                      turn=None, echo: dict | None = None, gateway_tags: dict | None = None) -> Response:
        """``turn``: a /v1/responses turn in stored mode (stored.service.StoredTurn), already rebuilt into ``body``;
        ``echo``: what a /v1/responses reply repeats from its request."""
        team = self.cfg.team(principal.team)
        stream = bool(body.get("stream"))
        sid, _ = session_id(headers, body, client_fmt, principal.user_id)
        thread = sid + thread_suffix(headers)  # the conversation within the session (a subagent, a side request)
        est = estimate(body)  # once per request: each model then gets it in its own tokens (Router, tokens.Calibration)
        prompt_tokens = est.tokens
        kv_id = kv_session_id(session_key(principal.user_id, thread))
        cascade = self.router.cascades(team)  # this team's tasks may escalate (expected_cost, decision 62)
        tracked = self.router.model_choice(team)[0] == "expected_cost"  # its tasks' costs and tool results are followed
        task_keys = [f"task:{kv_id}", f"task-base:{kv_id}"] if tracked else []
        stepping = steps_down(self.cfg, team, str(body.get("model") or ""))
        pre = await self.admit(principal, team, prompt_tokens, session_key(principal.user_id, sid),
                               session_key(principal.user_id, thread), kv_id, task_keys, stepping=stepping)
        spent = {k: (pre.get("spend") or {}).get(k) or 0.0 for k in task_keys}
        now = time.time()
        session = Session.from_dict(pre["session"], sid, thread)
        is_new = session.turns == 0
        if not is_new and session.last_seen:
            self.router.pauses.observe(team.name, now - session.last_seen, now)  # how long people pause between turns
        compacted = session.compacted(body)
        if compacted:
            session.warm_until, session.placement = {}, {}
            boundary = True
        else:
            boundary = is_new or is_task_boundary(body)
        utilization, queue_full = self.load(pre)
        learning = self.cfg.routing.learned.mode != "off" and bool(session.learn_row)
        if not is_new and (learning or tracked):
            self.follow_up(session, body, headers, compacted, boundary, prompt_tokens, spent if tracked else None)
        # A human message that carries on a task of a team routing by expected cost (a correction, or the code left failing
        # and a prompt no harder than the task's level) is the cascade's first chance to escalate: the task, its model and
        # its evidence go on (decisions 62 and 67).
        continuing = (boundary and cascade and not is_new and not compacted and bool(session.model) and session.task is not None
                      and self.router.carries_on(session, body, headers, prompt_tokens))
        if boundary and not continuing:
            session.learn_row = None  # a new task: what follows is about this one (set again if it gets a row)
            session.explore = session.outcome = session.task = None
        context = None
        if boundary and not continuing and tracked and self.learned.key_loaded:
            seed = self.learned.seed
            context = TaskContext(team.name, repo_hash(body, seed), project_hash(request_tags(headers, principal), seed),
                                  triage_buckets(_recent_user_text(body, 1), seed) if self.cfg.routing.success.mode != "off" else [])
        # Exploration (decision 59): only on live traffic people sent, never one pinned to a backend; never up when a budget
        # is nearly spent.
        explore = not (headers.get("x-meritorder-backend") or principal.internal or principal.tags.get("eval_run"))
        stepped = step_down(pre, principal, team, self.cfg) if stepping else None  # budget_step_down (decision 74)
        decision = self.router.decide(
            explore=explore and team.exploration.enabled,
            explore_up=headroom(pre, principal, team, self.cfg),
            level_cap=stepped[0] if stepped else None,
            requested=str(body.get("model") or ""),
            team=team,
            body=body,
            headers=headers,
            session=None if is_new else session,
            prompt_tokens=est,
            boundary=boundary and not continuing,
            utilization=utilization,
            now=now,
            queue_full=queue_full,
            kv=pre.get("kv"),
            client=wire or client_fmt,
            context=context,
            cascade=("human" if continuing else "now" if cascade and not boundary else None),
        )
        if decision.learned is not None:
            self.metrics.inc("meritorder_learned_router_total", {"mode": decision.learned.mode, "outcome": decision.learned.outcome})
        pinned = headers.get("x-meritorder-backend")
        if pinned:
            # An eval run's key may too: a run measures each model on one route (its own provider's API).
            if not (principal.at_least("admin") or principal.tags.get("eval_run")):
                raise GatewayError(403, "only admins can pin a backend with X-MeritOrder-Backend", "permission_error")
            decision.candidates = [c for c in decision.candidates if c.backend.name == pinned]
            if not decision.candidates:
                live = self.backends.get(pinned)
                out = live.skipped(now) if live else None
                if out is None and live is not None:
                    out = next(iter(live.cooling_models(now).values()), None)
                if out is not None:
                    wait = max(1, int(out.until - now))
                    raise GatewayError(503, f"backend '{pinned}' is being skipped for {wait}s more after it failed "
                                            f"({describe(out.status) if out.status else 'repeated errors'}): {out.message}",
                                       "overloaded_error", {"retry-after": str(wait)})
                state = (getattr(live, "deploy", None) or {}).get("state") or "api_only"
                serves = any(r.backend == pinned for r in (self.cfg.model(str(body.get("model") or "")) or ModelConfig(name="")).routes)
                if live is not None and live.cfg.serve is not None and serves and state not in ("warm", "draining"):
                    # A GPU deployment that's starting or stopped: wait, rather than a request that's wrong. Gateways see
                    # the controller's states within 5 seconds.
                    what = "it's starting" if state == "provisioning" else "it isn't running; it starts when demand pays for it"
                    raise GatewayError(503, f"backend '{pinned}' isn't serving now: {what}", "overloaded_error", {"retry-after": "5"})
                raise GatewayError(400, f"backend '{pinned}' does not serve this model", "invalid_request_error")
        max_usd = self.cfg.safeguards.max_request_usd

        def tokens(c: Candidate) -> int:
            """The prompt in this candidate's model's tokens (the router counted it; the estimate if not)."""
            return c.prompt_tokens or self.tokens.count(c.model.name, est)

        def worst(c) -> float:
            if c.backend.kind == "self_hosted":
                return 0.0  # paid for by the hour, not per request
            price = c.route.cost_price().at(tokens(c))
            return (tokens(c) * price.input + self.output_allowance(body, c, tokens(c))[0] * price.output) / 1e6

        if max_usd is not None:
            # A client's own max_tokens is a promise: refuse what could cost more than the limit. Without one, the
            # reply may be as long as the model allows, less whatever wouldn't fit under the limit (output_allowance).
            within = [c for c in decision.candidates if worst(c) <= max_usd and self.output_allowance(body, c, tokens(c))[0] > 0]
            if not within and ((decision.explore and decision.explore.get("arm") != "served") or decision.escalation):
                # Exploring or escalating must never be why a request is refused: route it as it would have gone.
                decision = self.router.decide(str(body.get("model") or ""), team, body, headers, None if is_new else session, est,
                                              boundary and not continuing, utilization, now, queue_full, pre.get("kv"),
                                              client=wire or client_fmt, context=context, level_cap=stepped[0] if stepped else None)
                within = [c for c in decision.candidates if worst(c) <= max_usd and self.output_allowance(body, c, tokens(c))[0] > 0]
            if not within and decision.candidates:
                cheapest = min(decision.candidates, key=worst)
                lowest = worst(cheapest)
                asked = body.get("max_tokens") or body.get("max_completion_tokens")
                what = f"up to {int(asked):,} output" if asked else "before any output"
                raise GatewayError(400, f"this request could cost ${lowest:,.2f} ({tokens(cheapest):,} input tokens, {what}), "
                                        f"above the ${max_usd:,.2f} per-request limit. Lower max_tokens, shorten the context, or use a cheaper model.",
                                   "invalid_request_error")
            decision.candidates = within
        tags = {**request_tags(headers, principal), **(gateway_tags or {})}
        if decision.floor is not None and "capped at" in decision.floor.reason and "budget_step_down" in decision.floor.reason:
            tags["budget_step_down"] = str(decision.floor.level)  # this task went to cheaper models to save the budget
        if team.screening.mode == "shadow":
            tags.update(self.screen(session, body, team.name))
        arm = decision.explore if boundary or decision.explore else session.explore
        if arm and arm.get("arm") != "served" and decision.floor is not None:
            tags["explore"], tags["explore_from"] = arm["arm"], f"{decision.floor.task}:{arm['from']}"  # every turn of the task
        charge = 0.0
        if decision.escalation is not None:
            # The escalation's first request carries what the failed attempt would have cost on the baseline model, taken off
            # its routing savings and its baseline: without Merit Order that attempt wouldn't have been made (decision 62).
            task = session.task or {}
            charge = max(spent.get(f"task-base:{kv_id}", 0.0) - float(task.get("base0") or 0.0), 0.0)
            session.task = {**task, "escalations": decision.escalation["n"], "from": [*task.get("from", []), decision.escalation["from"]],
                            "reasons": [*task.get("reasons", []), decision.escalation["reason"]],
                            "charged": round(float(task.get("charged") or 0.0) + charge, 6),
                            "base0": spent.get(f"task-base:{kv_id}", 0.0)}  # a later escalation charges only what came after
        if session.task and session.task.get("escalations"):
            tags["escalation"], tags["escalated_from"] = str(session.task["escalations"]), session.task["from"][-1]
        charge_once = [charge] if charge else []  # every attempt's Ctx shares it; the answer's row takes it
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        rc_key = None
        if wanted(team.response_cache, headers) and wire != "responses":
            rc_key = cache_key(client_fmt, body, principal.team, principal.user_id, team.response_cache)
            hit = await self.state.get_json(rc_key)
            if hit:
                return self.cached_response(hit, principal, decision, client_fmt, session, stream, tags, t0)

        # Judged labels (opt-in per team): sampled now, while the prompt is in memory; replayed after the answer, in the
        # background. The body isn't changed by what follows (translations and cache markers make copies).
        sample = await self.judged.pick(principal, team, decision, est, headers, tags, client_fmt, body) if decision.learned else None

        # The same history every turn (cache-safe): the rules are deterministic, and the model's decisions are stored.
        send_body, compression = await self.compress(body, client_fmt, team)

        last_error: BackendError | None = None
        failed: set[str] = set()
        failed_routes: set[tuple[str, str]] = set()
        failed_over: list[str] = []
        refused: list[str] = []
        attempts = 0
        queue = list(decision.candidates)
        # A provider that reserves credit for the whole output (OpenRouter) refuses a request whose maximum its account
        # can't cover, and says what it can: the same route is tried again with that, a real limit, not a guess. Up to
        # AFFORD_TRIES times, since other requests in flight reserve credit too and what's affordable can shrink meanwhile.
        afford: dict[int, int] = {}
        afford_tries: dict[int, int] = {}
        while queue:
            cand = queue.pop(0)
            if cand.backend.name in failed or (cand.backend.name, cand.route.model) in failed_routes:
                continue  # same credentials and endpoint (or the same busy model): it would fail the same way
            if cand.model.name in refused:
                continue  # the same model would refuse again
            if attempts >= self.cfg.routing.max_attempts:
                break
            attempts += 1
            upstream_body, proxy_cache = self.build_upstream(send_body, client_fmt, cand, thread, decision, session, tokens(cand),
                                                             stored=turn)
            output_limit = self.output_allowance(body, cand, tokens(cand))
            if id(cand) in afford:
                upstream_body.pop("max_completion_tokens", None)
                upstream_body["max_tokens"] = afford[id(cand)]
                output_limit = (afford[id(cand)], "provider credit")
            ctx = Ctx(principal, team, decision, cand, client_fmt, session, body, prompt_tokens, proxy_cache, stream,
                      include_usage, t0, tags, compression=compression, cache_key=rc_key, wire=wire, kv_id=kv_id,
                      charge=charge_once, task_spent=spent if tracked else None,
                      output_limit=output_limit, estimate=est, stored=turn, echo=echo, sample=sample,
                      upstream_effort=((upstream_body.get("output_config") or {}).get("effort") or upstream_body.get("reasoning_effort")
                             or (upstream_body.get("reasoning") or {}).get("effort")),
                      cache_ttls=marker_ttls(upstream_body),
                      trace_carrier={k: headers[k] for k in ("traceparent", "tracestate") if k in headers})
            ttl = cand.backend.cfg.timeout_seconds + 60
            release = self.inflight.acquire(cand.backend.name, ttl)
            if cand.replica is not None:
                release = _both(release, self.inflight.acquire(f"{cand.backend.name}#{cand.replica.id}", ttl))
            # A GPU gets the replica chosen for the session (else its endpoint picks) and the session's id, which vLLM
            # puts on its KV events.
            target = {"base_url": cand.replica.url if cand.replica else None, "session_tag": kv_id} if cand.self_hosted else {}
            sent_at = ctx.sent_at = time.monotonic()
            try:
                resp = await cand.backend.send(upstream_body, stream, headers, **target)
                # Errors after a 200 (Anthropic's in-stream overload, OpenRouter's error chunks) and streams that
                # never start are failures too: nothing has reached the client yet, so the next source can take it.
                if resp.events is not None:
                    limit = cand.backend.cfg.first_byte_timeout()
                    resp = await start_stream(resp, cand.backend.format, cand.backend.name,
                                              max(1.0, limit - (time.monotonic() - sent_at)) if limit else None)
                else:
                    check_body(resp, cand.backend.format, cand.backend.name)
            except BackendError as e:
                release()
                last_error = e
                log.warning("backend %s (%s) failed (%s): %s", cand.backend.name, cand.route.model, e.status, e.message)
                if e.unreachable and cand.replica is not None:
                    # This gateway can't reach the replica directly (a private address, a firewall): skip it for a
                    # while and send this request to the deployment's endpoint, which picks a replica itself.
                    self.metrics.inc("meritorder_gpu_replica_unreachable_total", {"backend": cand.backend.name})
                    cand.backend.unreachable[cand.replica.id] = time.time() + UNREACHABLE_SECONDS
                    cand.replica = None
                    queue.insert(0, cand)
                    attempts -= 1
                    continue
                n = affordable(e)
                if n and afford_tries.get(id(cand), 0) < AFFORD_TRIES and output_limit[1] != "client" and n < output_limit[0]:
                    afford[id(cand)] = n
                    afford_tries[id(cand)] = afford_tries.get(id(cand), 0) + 1
                    queue.insert(0, cand)
                    attempts -= 1  # the same attempt, asking for what the account can pay for
                    continue
                if too_long(e):
                    # The request's size, not the backend: only a model with a bigger window can take it. For auto,
                    # try one (re-deciding if the session was pinned to this model); a named model can't move.
                    window = cand.model.context_window
                    reported = reported_prompt_tokens(e.message)
                    if reported:  # the provider said how big the prompt really is: the next estimate for this model learns
                        self.learn_tokens(cand.model.name, ctx.sent_estimate, reported)

                    def roomier(c, window=window, name=cand.model.name) -> bool:
                        return c.model.name != name and (window is None or (c.model.context_window or math.inf) > window)

                    bigger = [c for c in queue if roomier(c)]
                    if decision.auto and not bigger and window and not pinned:
                        # Count the prompt as at least too big for this window, whatever the estimate says.
                        need = int(window / FIT_SHARE) + 1
                        bigger_est = est if est.tokens >= need else Estimate(text=need - est.media[0], media=est.media)
                        again = self.router.decide(
                            requested=decision.requested, team=team, body=body, headers=headers,
                            session=None if is_new else session, prompt_tokens=bigger_est,
                            boundary=True, utilization=utilization, now=now, queue_full=queue_full, kv=pre.get("kv"))
                        bigger = [c for c in again.candidates if roomier(c) and (max_usd is None or worst(c) <= max_usd)]
                    if decision.auto and bigger:
                        failed_over.append(f"{cand.backend.name}/{cand.route.model} prompt too long")
                        queue = bigger
                        continue
                    break
                self.spawn(self.state.record_backend(cand.backend.name, False, int((time.monotonic() - t0) * 1000), None))
                if e.retryable or e.status in NEXT_BACKEND_ON:
                    self.backend_failed(cand.backend, cand.route.model, e, cand.model)
                    if e.status in MODEL_SCOPED_STATUS:
                        failed_routes.add((cand.backend.name, cand.route.model))
                    else:
                        failed.add(cand.backend.name)
                    failed_over.append(f"{cand.backend.name}/{cand.route.model} {e.status}")
                    continue
                break
            except BaseException:
                release()
                raise
            if resp.events is None:
                release()
            else:
                resp._close = _then(resp._close, release)  # streams count until they finish
            ctx.provider_chained = resp.provider_chained
            if cand.backend.record_success(cand.route.model):
                self.spawn(alerts.recovered(self.state, cand.backend.name, None))
                self.spawn(alerts.recovered(self.state, cand.backend.name, cand.route.model))
            ctx.refused_by = list(refused)
            ctx.failed_over = failed_over
            # Another model still to be tried: one tried already (and failed) would only bring back its error.
            others = [c for c in queue if c.model.name not in refused and c.model.name != cand.model.name
                      and c.backend.name not in failed and (c.backend.name, c.route.model) not in failed_routes]
            if decision.auto and team.refusal_fallback and others and attempts < self.cfg.routing.max_attempts:
                was_refused, resp, usage = await self.detect_refusal(resp, cand.backend.format, stream)
                if was_refused:
                    log.info("%s refused an auto request; retrying on another model", cand.model.name)
                    refused.append(cand.model.name)
                    # Its cost with no baseline of its own: the answer's row carries the one baseline the request has.
                    self.spawn(self.persist(ctx, usage, self.costs(ctx, usage, 200).extra_attempt(), 200, REFUSED, None))
                    continue
            # Update session metadata before responding: a harness may send its next turn
            # the instant the last byte arrives, and it must see this turn's routing.
            try:
                await self.update_session(ctx)
            except Exception:
                log.exception("failed to update session %s", thread)
            if stream:
                return self.stream_response(resp, ctx)
            return await self.json_response(resp, ctx)

        assert last_error is not None
        self.spawn(self.persist(ctx, Usage(), Costs(0, 0, 0, 0, 0, 0, 0), last_error.status, last_error.message, None))
        message = last_error.message
        if too_long(last_error) and not re.search(r"prompt is too long", message, re.IGNORECASE):
            # Say it the way Anthropic's API does, whichever provider refused: Claude Code compacts the conversation
            # when it sees "prompt is too long", and only then.
            message = f"prompt is too long: {message}"
        resp = error_response(client_fmt, last_error.status, message, kind_for_status(last_error.status))
        if last_error.retry_after:  # pass the provider's advice on to the client (Claude Code and the SDKs honor it)
            resp.headers["retry-after"] = str(max(1, round(last_error.retry_after)))
        return resp

    def follow_up(self, session: Session, body: dict, headers: dict, compacted: bool, boundary: bool = False,
                  prompt_tokens: int = 0, spent: dict | None = None) -> None:
        """What the client does after an answer, counted against the task it followed (the learned router's labels): the
        same request sent again (every message unchanged: a regenerated answer), or a higher X-Route-Floor (or
        X-Route-Hint: smart) than the task got. Only the session's hashes and counts are compared, never the text.
        ``spent``: the conversation's spend counters, for a team routing by expected cost (its tasks' costs)."""
        msgs = body.get("messages") or []
        retried = bool(session.prefix_hash) and len(msgs) == session.msg_count and not compacted
        before = parse_floor(session.floor or "")
        asked = parse_floor(headers.get("x-route-floor", ""))
        raised = None
        if asked and before and asked.level > before.level:
            raised = asked.level
        elif before and (headers.get("x-route-hint") or "").lower() == "smart":
            raised = min(5, before.level + 1)
        if (retried or raised) and session.learn_row:
            self.spawn(self.store.mark_route_outcome(session.learn_row, retried=retried, floor_raised=raised))
        if not compacted and not retried:
            self.coding_outcome(session, body, boundary, prompt_tokens, spent, headers)

    def screen(self, session: Session, body: dict, team: str) -> dict[str, str]:
        """Request screening in shadow mode (screening.py): what's new since the session's last turn, as ledger tags and
        metrics. Never changes or refuses the request; a failure in it is logged and ignored."""
        t0 = time.perf_counter()
        try:
            msgs = body.get("messages") or []
            since = session.msg_count if session.msg_count <= len(msgs) else 0  # a rewritten history is read again
            found, session.untrusted = screening.scan(body, since, session.untrusted)
        except Exception as e:  # noqa: BLE001
            log.warning("request screening failed: %s", e)
            return {}
        for kind, n in found.items():
            self.metrics.inc("meritorder_screening_findings_total", {"kind": kind, "team": team}, n)
        self.metrics.observe("meritorder_screening_seconds", {}, time.perf_counter() - t0)
        return screening.tags(found, session.untrusted)

    def coding_outcome(self, session: Session, body: dict, boundary: bool, prompt_tokens: int, spent: dict | None = None,
                       headers: dict | None = None) -> None:
        """What the task's tool results show (outcomes.py): read from what's new since its last turn, kept in the session's
        state, and written to the task's row when a test or build result (or a fix loop) changes it, and when the task
        ends: then with its turns and tokens, and whether the person's next prompt says the answer was wrong. For a team
        routing by expected cost, the task's end also records what it cost and its escalations (decision 62)."""
        t0 = time.perf_counter()
        if boundary:
            had = session.outcome is not None
            state = dict(session.outcome or outcomes.new_state())
            state["correction"] = outcomes.correction(body)
            session.outcome = state  # a message that carries on the task keeps it (Router.carries_on)
            if session.learn_row and (had or state["correction"]):
                summary = outcomes.summary(state)
                task = session.task or {}
                if spent is not None and task and not self.router.carries_on(session, body, headers or {}, prompt_tokens):
                    key = next((k for k in spent if k.startswith("task:")), None)
                    summary.update(ended=True, cost=round(max((spent.get(key) or 0.0) - float(task.get("spend0") or 0.0), 0.0), 6),
                                   escalations=int(task.get("escalations") or 0), escalation_reasons=list(task.get("reasons") or []),
                                   escalation_cost=float(task.get("charged") or 0.0))
                self.spawn(self.store.mark_coding(session.learn_row, summary))
        else:
            state, changed = outcomes.update(session.outcome, body, session.msg_count, prompt_tokens)
            session.outcome = state
            if changed and session.learn_row:
                self.spawn(self.store.mark_coding(session.learn_row, outcomes.summary(state)))
        self.metrics.observe("meritorder_outcome_seconds", {}, time.perf_counter() - t0)

    def route_features(self, ctx: Ctx, status: int) -> dict | None:
        """The learned router's row for this request: its features, the rules' level, the model that served it and that
        model's score, and what the learned router said (learned.py). No content: hashed buckets and named tags."""
        call = ctx.decision.learned
        if call is None or "eval" in ctx.tags or ctx.principal.tags.get("eval_run"):
            return None  # eval runs repeat the same prompts in new sessions: they'd look like people giving up
        if ctx.principal.internal:
            return None  # judged labels' replays and judging: the learned router never trains on its own requests
        cap = self.cfg.routing.request_class_levels.get(next((t[6:] for t in call.features.tags if t.startswith("class:")), ""))
        if cap is not None and cap <= 1:
            return None  # Claude Code side requests capped at 1: whatever their level, they go to level-1 models
        task = call.features.task
        refused = [m.q(task) for name in ctx.refused_by if (m := self.cfg.model(name)) is not None]
        uid = ctx.principal.user_id
        return {"request_id": ctx.request_id, "ts": datetime.now(UTC), "team": ctx.principal.team, "user_id": uid,
                "session_id": ctx.session.id, "thread": hashlib.sha256(session_key(uid, ctx.session.state_id).encode()).hexdigest()[:16],
                "fv": call.fv, "task": task, "rules_level": call.features.rules, "text_features": call.features.text,
                "tags": call.features.tags, "auto": ctx.decision.auto, "served_model": ctx.cand.model.name,
                "served_score": ctx.cand.model.q(task) or None, "refused_scores": refused, "status": status,
                "learned": call.record() if call.outcome != "named" else None,
                "level": ctx.decision.floor.level if ctx.decision.auto and ctx.decision.floor else None,
                "arm": x.get("arm") if (x := ctx.decision.explore) else None, "arm_from": x.get("from") if x else None,
                "propensity": x.get("p") if x else None, "predicted": ctx.decision.predicted,
                "context": ctx.decision.context.record() if ctx.decision.context else None,
                "triage_features": ctx.decision.context.triage if ctx.decision.context else []}

    async def detect_refusal(self, resp: UpstreamResponse, fmt: str, stream: bool) -> tuple[bool, UpstreamResponse, Usage]:
        """Did the model decline (Claude `stop_reason: refusal`, OpenAI `content_filter`) before producing content?

        Streams are buffered only until the first content arrives; a refusal after content has started
        streams through unchanged.
        """
        if not stream:
            d = resp.body or {}
            if fmt == "anthropic":
                return d.get("stop_reason") == "refusal", resp, usage_from_anthropic(d.get("usage"))
            choice = (d.get("choices") or [{}])[0]
            return choice.get("finish_reason") == "content_filter", resp, usage_from_openai(d.get("usage"))
        assert resp.events is not None
        it = resp.events.__aiter__()
        buffered: list[SSEEvent] = []
        tracker = UsageTracker(fmt)
        was_refused = False
        broke: Exception | None = None
        try:
            while len(buffered) < 200:
                ev = await it.__anext__()
                buffered.append(ev)
                tracker.feed(ev)
                d = ev.json() or {}
                if fmt == "anthropic":
                    kind, delta = d.get("type"), d.get("delta") or {}
                    if kind == "content_block_delta" and (delta.get("text") or delta.get("partial_json")):
                        break
                    if kind == "message_delta" and delta.get("stop_reason") == "refusal":
                        was_refused = True
                        break
                    if kind == "message_stop":
                        break
                else:
                    choice = (d.get("choices") or [{}])[0] if d.get("choices") else {}
                    delta = choice.get("delta") or {}
                    if delta.get("content") or delta.get("tool_calls"):
                        break
                    if choice.get("finish_reason") == "content_filter":
                        was_refused = True
                        break
                    if ev.data.strip() == "[DONE]":
                        break
        except StopAsyncIteration:
            pass
        except Exception as e:  # noqa: BLE001 (the stream broke while we looked: it breaks where the client reads it)
            broke = e
        if was_refused:
            if fmt != "anthropic":
                # OpenAI-style streams send their usage in a chunk after the finish reason: the prompt was billed.
                try:
                    for _ in range(5):
                        ev = await it.__anext__()
                        tracker.feed(ev)
                        if ev.data.strip() == "[DONE]" or (ev.json() or {}).get("usage"):
                            break
                except Exception:  # noqa: BLE001 (the stream ended or broke: what was read is all there is)
                    pass
            await resp.aclose()
            return True, resp, tracker.usage

        async def replay():
            for ev in buffered:
                yield ev
            if broke is not None:
                raise broke  # stream_response tells the client, records the request and closes the stream
            async for ev in it:
                yield ev

        resp.events = replay()
        return False, resp, tracker.usage

    def output_allowance(self, body: dict, cand: Candidate, prompt_tokens: int = 0, price: Price | None = None) -> tuple[int, str]:
        """The most output tokens this request may produce on this candidate (``prompt_tokens``: the prompt in its
        model's tokens), and what set it:

        - ``client``: the request's own max_tokens;
        - ``model-capped``: the request asked for more than the model can write, so the model's maximum;
        - ``model``: no max_tokens, so as much as the model can write (its max_output_tokens);
        - ``context``: no max_tokens, and the model's maximum doesn't fit in what's left of its context window;
        - ``cost limit``: no max_tokens, and the model's maximum would cost more than the per-request limit;
        - ``default``: no max_tokens and the model's maximum isn't known (default_max_tokens).

        (``provider credit``, set in the request loop: the provider's account can pay for no more than this.)

        ``price``: what the route costs (default its live price; a batch request's is its batch price).
        """
        asked = body.get("max_tokens") or body.get("max_completion_tokens")
        most = cand.model.max_output_tokens
        if asked:
            n = int(asked)
            return (most, "model-capped") if most and n > most else (n, "client")
        n, source = (most, "model") if most else (self.cfg.default_max_tokens, "default")
        if cand.model.context_window:
            # Providers refuse a prompt plus max_tokens over the context window. The prompt count is an estimate, so
            # allow for it running low: by a third until the model's replies have calibrated it, then by their spread.
            room = cand.model.context_window - int(prompt_tokens * (1 + self.tokens.margin(cand.model.name)))
            if room < n:
                n, source = max(room, 1), "context"
        max_usd = self.cfg.safeguards.max_request_usd
        price = (price or cand.route.cost_price()).at(prompt_tokens)
        if max_usd is not None and cand.backend.kind != "self_hosted" and price.output > 0:
            room = int((max_usd - prompt_tokens * price.input / 1e6) * 1e6 / price.output)
            if room < n:
                return max(room, 0), "cost limit"
        return n, source

    def build_upstream(self, body: dict, client_fmt: str, cand: Candidate, sid: str, decision: Decision | None = None,
                       session: Session | None = None, prompt_tokens: int = 0, batch: bool = False, stored=None) -> tuple[dict, bool]:
        """The request as this candidate's backend takes it. ``batch``: one request of a batch, sent through the provider's
        batch API: no cache markers added (there's no next turn to read them), and the cost limit counts its batch price.
        ``stored``: a stored-mode turn (stored.service.StoredTurn), which a backend with ``responses_store`` stores too, and
        gets as the continuation of the response it stored for the turn before, when that was on the same model there."""
        backend = cand.backend
        model = (cand.route.batch_model or cand.route.model) if batch else cand.route.model
        try:
            if client_fmt == backend.format:
                up = dict(body)
            elif backend.format == "anthropic":
                up = openai_to_anthropic_request(body, self.cfg.default_max_tokens, model)
            else:
                # Claude through OpenRouter reads the client's own cache markers where they were put (decision 04's rule:
                # the client's caching is left to the client); elsewhere they'd be unknown fields.
                up = anthropic_to_openai_request(body, backend.cfg.max_tokens_field, needs_cache_marker(backend.cfg, model))
        except TranslationError as e:
            raise GatewayError(400, f"{backend.name}/{model}: {e}") from e
        up["model"] = model
        dropped = fit_upstream(up, body, backend.format, backend.cfg.api, backend.cfg.base_url)  # a Responses turn's tools
        if client_fmt == "anthropic" and backend.format != "anthropic":
            dropped += [(name, "an Anthropic server tool") for name in server_tools(body)]
        if dropped:
            log.info("%s/%s goes without %s", backend.name, cand.route.model, "; ".join(f"{n} ({why})" for n, why in dropped))
        limit, source = self.output_allowance(body, cand, prompt_tokens, cand.route.batch_price(backend.cfg) if batch else None)
        if backend.format == "anthropic" or source in ("model-capped", "context", "cost limit") or (source == "model" and backend.kind == "api"):
            # Anthropic requires a limit. Elsewhere one is sent only to change what the client asked for: the model's
            # maximum instead of more than it allows or when unset (some providers default lower, e.g. 4,096), or
            # what fits in the context window or under the per-request cost limit. A self-hosted server already
            # defaults to the rest of the context window.
            up.pop("max_completion_tokens", None)
            up["max_tokens"] = limit
        later_turn = session is not None and session.turns > 0  # a 1-hour cache only once a session has come back
        if batch:
            proxy_cache = False
        elif backend.format == "anthropic":
            proxy_cache = shape_anthropic(up, backend.cfg, later_turn)
        else:
            proxy_cache = shape_openai(up, backend.cfg, sid, later_turn) and not has_cache_control(body)
        if cand.upgraded and backend.kind == "self_hosted":
            up["priority"] = 10  # vLLM priority scheduling: paid traffic goes first
        effort = self.choose_effort(body, client_fmt, cand, decision, session) if decision and session else None
        if not effort and client_fmt == "anthropic" and backend.format != "anthropic":
            # The client's own effort (Claude Code sends one) has no field in Chat Completions: carry it over, at the
            # nearest level this model accepts. Unknown levels aren't sent: some models reject the field.
            effort = fit_effort((body.get("output_config") or {}).get("effort"), cand.model.effort_levels)
        style = effort_format(backend.cfg)
        if effort and style == "anthropic":
            up["output_config"] = {**(up.get("output_config") or {}), "effort": effort}
        elif effort and style == "openai":
            up["reasoning_effort"] = effort
        elif effort and style == "openrouter":
            up.pop("reasoning_effort", None)
            up["reasoning"] = {**(up.get("reasoning") or {}), "effort": effort}
        if stored is not None and stored.store and backend.cfg.responses_store and backend.cfg.api == "responses" and backend.format == client_fmt:
            prev = stored.provider_previous
            same = prev is not None and prev["backend"] == backend.name and prev["model"] == cand.route.model and stored.held
            up[PROVIDER_CHAIN] = {"previous_response_id": prev["id"] if same else None, "held": stored.held if same else 0}
        return up, proxy_cache

    def choose_effort(self, body: dict, client_fmt: str, cand: Candidate, decision: Decision, session: Session) -> str | None:
        """Lower reasoning effort for simple `auto` tasks; never overrides the client; stable per session."""
        if not decision.auto or not cand.model.effort_levels or effort_format(cand.backend.cfg) == "none":
            return None
        if client_fmt == "anthropic":
            client_set = (body.get("output_config") or {}).get("effort")
        else:
            client_set = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort")
        if client_set:
            return None
        if session.effort and session.model == cand.model.name:
            return session.effort if session.effort in cand.model.effort_levels else None
        level = decision.floor.level if decision.floor else None
        effort = self.cfg.routing.effort_by_floor.get(level) if level else None
        return effort if effort in cand.model.effort_levels else None

    def response_headers(self, ctx: Ctx, costs: Costs | None) -> dict[str, str]:
        h = {
            "x-meritorder-model": ctx.cand.model.name,
            "x-meritorder-backend": ctx.cand.backend.name,
            "x-meritorder-session": ctx.session.id,
            "x-meritorder-request-id": str(ctx.request_id),
        }
        if ctx.decision.floor:
            h["x-meritorder-floor"] = str(ctx.decision.floor)
        if ctx.refused_by:
            h["x-meritorder-refused-by"] = ",".join(ctx.refused_by)
        if ctx.failed_over:
            h["x-meritorder-failed-over"] = ", ".join(ctx.failed_over)
        if ctx.decision.substituted_for:
            h["x-meritorder-substituted-for"] = ctx.decision.substituted_for
        if ctx.cand.queued:
            h["x-meritorder-queued"] = "1"
        cfg = ctx.cand.backend.cfg
        dropped = dropped_tools(ctx.body, ctx.cand.backend.format, cfg.api, cfg.base_url)
        if ctx.client_fmt == "anthropic" and ctx.cand.backend.format != "anthropic":
            dropped += [(name, "") for name in server_tools(ctx.body)]
        if dropped:
            h["x-meritorder-dropped-tools"] = ", ".join(dict.fromkeys(name for name, _ in dropped))
        if ctx.output_limit:
            h["x-meritorder-max-tokens"] = str(ctx.output_limit[0])
            h["x-meritorder-max-tokens-source"] = ctx.output_limit[1]
        if costs is not None:
            h["x-meritorder-cost-usd"] = f"{costs.actual:.6f}"
            h["x-meritorder-baseline-usd"] = f"{costs.baseline:.6f}"
            h["x-meritorder-savings-usd"] = f"{costs.savings_total:.6f}"
        return h

    def responses_extra(self, ctx: Ctx) -> tuple[str | None, dict]:
        """A /v1/responses reply's id (a stored turn's own) and what its object carries from the request."""
        if ctx.stored is not None:
            return ctx.stored.rid, ctx.stored.extra
        return None, {**(ctx.echo or {}), "store": False}

    async def json_response(self, resp: UpstreamResponse, ctx: Ctx) -> Response:
        data = resp.body or {}
        backend_fmt = ctx.cand.backend.format
        usage = usage_from_anthropic(data.get("usage")) if backend_fmt == "anthropic" else usage_from_openai(data.get("usage"))
        if ctx.client_fmt == backend_fmt:
            out = data
        elif ctx.client_fmt == "openai":
            out = anthropic_to_openai_response(data, ctx.label)
        else:
            out = openai_to_anthropic_response(data, ctx.label)
        if ctx.wire == "responses":
            rid, extra = self.responses_extra(ctx)
            out = chat_to_responses(out, ctx.label, rid, extra, ctx.body.get(TOOLS_KEY))
            if ctx.stored is not None and not await self.stored.save(ctx.stored, out, ctx, resp.provider_response_id):
                out["store"] = False  # couldn't be kept: the client mustn't count on chaining from it
        if ctx.sample is not None:
            ctx.sample.answer = reply_text(data, backend_fmt)  # in memory, for the judge (judged.py)
        costs = self.costs(ctx, usage, 200)
        self.spawn(self.persist(ctx, usage, costs, 200, None, None))
        if ctx.cache_key:
            ttl = ctx.team.response_cache.ttl_seconds
            self.spawn(self.state.set_json(ctx.cache_key, {"response": out, "cost": costs.actual, "model": ctx.cand.model.name}, ttl))
        return JSONResponse(out, headers=self.response_headers(ctx, costs))

    def cached_response(self, hit: dict, principal: Principal, decision: Decision, client_fmt: str, session: Session,
                        stream: bool, tags: dict, t0: float) -> Response:
        """Serve an exact-match cache hit: no backend call, cost 0, savings = what it cost the first time. Eval traffic and
        judged labels' replays get no savings from it, as from routing and caching (``costs``)."""
        rid = uuid.uuid4()
        saved = 0.0 if "eval" in tags or JUDGED_TAG in tags else float(hit.get("cost") or 0)
        row = {f: 0 for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "actual_cost",
                              "served_api_cost", "list_cost", *SAVINGS_FIELDS)}
        row.update(id=rid, ts=datetime.now(UTC), user_id=principal.user_id, team=principal.team, key_id=principal.key_id,
                   session_id=session.id, tags=tags, client_format=client_fmt, requested_model=decision.requested,
                   floor=str(decision.floor) if decision.floor else None, route_reason=route_reason(decision),
                   served_model=hit.get("model"), upstream_model=None,
                   backend="response-cache", backend_kind="cache", status=200, error=None, stream=stream, upgraded=False,
                   proxy_cache=False, latency_ms=int((time.monotonic() - t0) * 1000), ttft_ms=None,
                   baseline_cost=saved, savings_response_cache=saved)
        self.observe(row)
        self.spawn(self.store.record(row))
        headers = {"x-meritorder-backend": "response-cache", "x-meritorder-model": str(hit.get("model")),
                   "x-meritorder-request-id": str(rid), "x-meritorder-cost-usd": "0.000000",
                   "x-meritorder-savings-usd": f"{saved:.6f}"}
        if not stream:
            return JSONResponse(hit["response"], headers=headers)

        async def gen():
            for ev in replay_stream(client_fmt, hit["response"]):
                yield ev.encode()

        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    def stream_response(self, resp: UpstreamResponse, ctx: Ctx) -> Response:
        backend_fmt = ctx.cand.backend.format
        tracker = UsageTracker(backend_fmt)
        if ctx.client_fmt == backend_fmt:
            converter = None
        elif ctx.client_fmt == "openai":
            converter = AnthropicToOpenAIStream(ctx.label, ctx.include_usage)
        else:
            converter = OpenAIToAnthropicStream(ctx.label)
        to_responses = (ChatToResponsesStream(ctx.label, *self.responses_extra(ctx), tools=ctx.body.get(TOOLS_KEY))
                        if ctx.wire == "responses" else None)

        def emit(events: list[SSEEvent], status: int = 200, error: str | None = None) -> list[SSEEvent]:
            if to_responses is None:
                return events
            out = []
            for e in events:
                if status >= 400 and e.data.strip() == "[DONE]":
                    # The provider reported an error in the stream: the response ends failed, not completed.
                    out += to_responses.fail_events(error or "upstream error")
                else:
                    out += to_responses.feed(e)
            return out

        kept: list[bool] = []

        async def keep() -> None:
            """A stored turn is kept before the client sees it end (it may send the next turn at once), once."""
            if ctx.stored is not None and to_responses is not None and to_responses.final is not None and not kept:
                kept.append(True)
                await self.stored.save(ctx.stored, to_responses.final, ctx, resp.provider_response_id)

        sample = ctx.sample
        if sample is not None:
            sample.events = []  # a sampled answer's events, assembled when it ends (judged.py); memory only

        async def gen():
            ttft: int | None = None
            status, error = 200, None
            try:
                assert resp.events is not None
                async for ev in resp.events:
                    tracker.feed(ev)
                    if status < 400 and '"error"' in ev.data and (found := error_in(ev.json(), backend_fmt)):
                        # The provider failed after the stream started: the client gets its error as it came, and the
                        # ledger records a failure, not a 200.
                        status, error = found[0], f"{ctx.cand.backend.name}: {found[1]}"
                    if sample is not None and sample.events is not None and (d := ev.json()) is not None:
                        sample.events.append(d)
                    if converter is None:
                        if ctx.client_fmt == "openai" and not ctx.include_usage and _usage_only(ev):
                            continue
                        out: list[SSEEvent] = [ev]
                    else:
                        out = converter.feed(ev)
                    out = emit(out, status, error)
                    await keep()  # when that was the end of the response
                    for o in out:
                        if ttft is None:
                            ttft = int((time.monotonic() - ctx.t0) * 1000)
                        yield o.encode()
                # After an error in the stream there is no normal finish: each API's stream ends with its error.
                tail = converter.finish_events() if converter is not None and status < 400 else []
                if to_responses is not None:
                    tail = emit(tail, status, error)
                    tail += to_responses.fail_events(error or "upstream error") if status >= 400 else to_responses.finish_events()
                    await keep()
                for o in tail:
                    yield o.encode()
            except (asyncio.CancelledError, GeneratorExit):  # the client went away (HTTP), or stopped reading (WebSocket)
                status, error = 499, "client disconnected"
                raise
            except Exception as e:  # upstream broke mid-stream
                status, error = 502, f"{type(e).__name__}: {e}"
                log.warning("stream from %s failed: %s", ctx.cand.backend.name, error)
                if to_responses is not None:  # the Responses API's own way of saying so, kept as failed when stored
                    tail = to_responses.fail_events(f"upstream stream failed: {error}")
                    await keep()
                    for o in tail:
                        yield o.encode()
                else:
                    body = error_body(ctx.client_fmt, 502, f"upstream stream failed: {error}", "api_error")
                    yield event(body, "error" if ctx.client_fmt == "anthropic" else None).encode()
            finally:
                try:
                    await resp.aclose()
                except Exception as e:  # noqa: BLE001 (closing a broken upstream mustn't lose the request's ledger row)
                    log.warning("closing the stream from %s failed: %s", ctx.cand.backend.name, e)
                finally:  # runs on a cancellation during the close too
                    if sample is not None and sample.events is not None:
                        events, sample.events = sample.events, None
                        if status == 200:
                            whole = anthropic_message(events) if backend_fmt == "anthropic" else openai_completion(events)
                            sample.answer = reply_text(whole, backend_fmt)
                    usage, estimated = self.broken_off_usage(ctx, tracker, status)
                    costs = self.costs(ctx, usage, status)
                    self.spawn(self.persist(ctx, usage, costs, status, error, ttft, estimated=estimated))

        headers = {**self.response_headers(ctx, None), "cache-control": "no-cache", "x-accel-buffering": "no"}
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    # ------------------------------------------------------------------ accounting

    def broken_off_usage(self, ctx: Ctx, tracker: UsageTracker, status: int) -> tuple[Usage, bool]:
        """What a stream that broke off used, and whether that's an estimate. The provider bills the whole prompt and
        what it wrote before it noticed, but OpenAI-style backends report usage only in the last chunk (and Claude's
        message_start says 1 output token), so a client that went away (499) or a stream that broke (502) would be
        recorded as nearly free. Where the provider didn't say, the prompt is the request's calibrated estimate, all of
        it as uncached input (what the cache held isn't known), and the output is what streamed, counted. The estimate
        stands in for a count the provider didn't send, never on top of one it did."""
        usage = tracker.usage
        if status not in (499, 502):
            return usage, False
        usage, estimated = replace(usage), False
        name = ctx.cand.model.name
        if not usage.prompt_tokens:
            usage.input_tokens = self.tokens.count(name, ctx.sent_estimate)
            estimated = True
        written = tracker.streamed
        out = round(text_tokens(written) * self.tokens.ratio(name)) if written and not tracker.output_reported else 0
        if out > usage.output_tokens:
            usage.output_tokens = out
            estimated = True
        return usage, estimated

    def api_price(self, model: ModelConfig) -> Price:
        """What this model costs per token through an API (used for counterfactuals)."""
        return api_price(self.cfg, model)

    def costs(self, ctx: Ctx, usage: Usage, status: int) -> Costs:
        if usage.total_tokens == 0:
            return Costs(0, 0, 0, 0, 0, 0, 0)
        backend = ctx.cand.backend
        served_price = self.api_price(ctx.cand.model) if backend.kind == "self_hosted" else ctx.cand.route.cost_price()
        self_cost = None
        if backend.kind == "self_hosted":
            self_cost = chargeback(getattr(backend, "live_cost_per_hour", None) or backend.cfg.gpu_cost_per_hour, backend.cfg.capacity_tokens_per_second, usage)
        baseline_model = self.cfg.model(ctx.team.baseline_model or self.cfg.baseline_model)
        # Eval traffic would have run on the same models without Merit Order, and judged labels' replays and judging are
        # the gateway's own: no routing or caching savings (or losses) for either. Without Merit Order they wouldn't
        # have run at all, so the caching the gateway added to them saved nobody anything.
        own = "eval" in ctx.tags or JUDGED_TAG in ctx.tags
        # A baseline with no known price makes no routing savings claim (rather than one against $0).
        baseline_price = known_api_price(self.cfg, baseline_model) if baseline_model and not own else None
        if usage.cache_write_tokens and usage.cache_write_1h_tokens is None and ctx.cache_ttls and all(t >= HOUR for t in ctx.cache_ttls):
            # The backend didn't split its cache writes by lifetime, but every marker asked for an hour: all of them were.
            usage = replace(usage, cache_write_1h_tokens=usage.cache_write_tokens)
        billed = usage.billed_usd if uses_reported_cost(backend.cfg) else None
        costs = attribute(usage, served_price, baseline_price, ctx.proxy_cache and not own, self_cost, billed)
        c = ctx.compression
        if c.removed_tokens:
            # Tokens we removed would have been billed: new ones as input, older ones mostly as cache reads, at the price for
            # the prompt they'd have made (a long-prompt tier). The same for both methods, each its own cause: the rules',
            # then what the model took out of what they left.
            costs.savings_compression_model = removed_cost(served_price, usage, c.model_tokens_new, c.model_tokens_history)
            costs.savings_compression = removed_cost(served_price, usage, c.model_tokens_new + c.removed_tokens_new,
                                                     c.model_tokens_history + c.removed_tokens_history) - costs.savings_compression_model
            costs.baseline += costs.savings_compression + costs.savings_compression_model
        return costs

    def record_speed(self, ctx: Ctx, usage: Usage, ttft_ms: int | None) -> None:
        """Time a successful reply from when its route was called, for routing by speed."""
        if not ctx.sent_at:
            return
        before = ctx.sent_at - ctx.t0  # gateway work and failed attempts before this route
        self.router.speeds.record(ctx.cand.backend.name, ctx.cand.route.model, time.monotonic() - ctx.sent_at,
                                  None if ttft_ms is None else ttft_ms / 1000 - before, usage.output_tokens, time.time())

    def check_warmth(self, ctx: Ctx, usage: Usage) -> None:
        """Count whether a GPU turn found the cache where it was expected, so the warm-cache estimates can be checked
        against what vLLM reports (``--enable-prompt-tokens-details``). Until a deployment has reported a cache hit
        once, its misses aren't counted: it may not report cached tokens at all."""
        cand = ctx.cand
        if not cand.self_hosted or cand.warmth is None or usage.prompt_tokens < 1024:
            return
        hit = usage.cache_read_tokens * 2 >= usage.prompt_tokens
        cand.backend.reports_cached = cand.backend.reports_cached or usage.cache_read_tokens > 0
        if cand.backend.reports_cached:
            self.metrics.inc("meritorder_gpu_cache_turns_total", {"backend": cand.backend.name, "expected": "warm" if cand.warmth.warm else "cold",
                                                                  "result": "hit" if hit else "miss"})

    async def persist(self, ctx: Ctx, usage: Usage, costs: Costs, status: int, error: str | None, ttft: int | None,
                      estimated: bool = False) -> None:
        """``estimated``: the usage is the gateway's estimate, not the provider's count (broken_off_usage); the row is
        tagged ``usage: estimated``."""
        cand = ctx.cand
        if status < 400 and not error and usage.output_tokens:
            self.record_speed(ctx, usage, ttft)
            self.check_warmth(ctx, usage)
        # The provider's own prompt count calibrates the estimate. Not from mocks (theirs is made up), nor when a
        # server-side tool (web search) added content the request didn't carry.
        # Nor from a provider that continued its own stored response: its count includes what it kept (reasoning items).
        if (status < 400 and not error and usage.prompt_tokens and cand.backend.kind != "mock" and not usage.web_search_requests
                and not ctx.provider_chained):
            self.learn_tokens(cand.model.name, ctx.sent_estimate, usage.prompt_tokens)
        method = ctx.compression.method
        tags = {**ctx.tags, "compression": method} if method else ctx.tags
        if estimated:
            tags = {**tags, "usage": "estimated"}
        row = {
            "id": ctx.request_id,
            "ts": datetime.now(UTC),
            "user_id": ctx.principal.user_id,
            "team": ctx.principal.team,
            "key_id": ctx.principal.key_id,
            "session_id": ctx.session.id,
            # How tool results were compressed (rules, or model when it cut anything), to filter reports by (decision 46).
            "tags": tags,
            "client_format": ctx.client_fmt,
            "requested_model": ctx.decision.requested,
            "floor": str(ctx.decision.floor) if ctx.decision.floor else None,
            "route_reason": route_reason(ctx.decision),
            "served_model": cand.model.name,
            "upstream_model": cand.route.model,
            "backend": cand.backend.name,
            "backend_kind": cand.backend.kind,
            "status": status,
            "error": (error or "")[:500] or None,
            # The routes tried first that failed ("backend/model status"), as the x-meritorder-failed-over header says.
            "failed_over": ", ".join(ctx.failed_over)[:1000] or None,
            "stream": ctx.stream,
            "upgraded": cand.upgraded,
            "proxy_cache": ctx.proxy_cache,
            "input_tokens": usage.input_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": int((time.monotonic() - ctx.t0) * 1000),
            "ttft_ms": ttft,
            "actual_cost": costs.actual,
            "served_api_cost": costs.served_api,
            "baseline_cost": costs.baseline,
            "list_cost": costs.list_price,
            "savings_routing": costs.savings_routing,
            "savings_caching": costs.savings_caching,
            "savings_self_hosting": costs.savings_self_hosting,
            "savings_compression": costs.savings_compression,
            "savings_compression_model": costs.savings_compression_model,
            "savings_response_cache": costs.savings_response_cache,
        }
        if ctx.charge and error != REFUSED:
            # An escalation's first request: the failed attempt it replaces is charged against routing (decision 62), once,
            # on the answer's row (never a refused attempt's; the attempts share one charge, so it can't be taken twice).
            charged = ctx.charge.pop()
            row["savings_routing"] -= charged
            row["baseline_cost"] -= charged
        thread = ctx.kv_id if ctx.task_spent is not None else None
        extra = {f"task-base:{thread}": costs.baseline} if thread else None
        # Spend counters were seeded from the ledger when the request was admitted; one round trip adds to them.
        self.observe(row, ctx.trace_carrier, spend_keys=self.spend_keys(ctx.principal, session_key(ctx.principal.user_id, ctx.session.id),
                                                                        thread), extra=extra)
        try:
            await self.store.record(row)
        except Exception:
            log.exception("failed to write ledger row %s", ctx.request_id)
        features = self.route_features(ctx, status) if error != REFUSED else None
        replay = None
        if features is not None and ctx.sample is not None:
            # What the row records about its judged label (pending, or why there's none), and the replay to run once it's
            # written, in the background.
            features["judged"], replay = self.judged.settle(ctx.sample, ctx.request_id, cand.model, status, error)
        if features is not None:
            try:
                await self.store.record_route_features(features)
            except Exception:
                log.exception("failed to write the learned router's row for %s", ctx.request_id)
        if replay is not None:
            self.spawn(replay)

    async def update_session(self, ctx: Ctx) -> None:
        s = ctx.session
        now = time.time()
        backend = ctx.cand.backend
        msgs = ctx.body.get("messages") or []
        if s.model != ctx.cand.model.name:
            s.effort = None
        s.model = ctx.cand.model.name
        s.backend = backend.name
        sent = ctx.upstream_effort
        if sent and not s.effort:
            s.effort = sent
        if ctx.decision.floor:
            s.floor = str(ctx.decision.floor)
            s.floor_reason = ctx.decision.floor.reason
        if ctx.decision.learned is not None:
            s.learn_row = str(ctx.request_id)  # the task's row: what follows the answer is counted against it
        if ctx.decision.explore is not None:
            s.explore = {k: ctx.decision.explore.get(k) for k in ("arm", "from", "level", "p")}
        pred = ctx.decision.predicted
        if (ctx.task_spent is not None and s.task is None and ctx.decision.auto) or (ctx.decision.escalation is not None and s.task is not None):
            # A new task of a team routing by expected cost: its model's chance and expected cost, and the conversation's
            # spend so far, which its cost at the end is counted from; an escalation: the new model's chance, and its
            # evidence from here (decision 62). The simulator keeps them with the same function (routing_eval.RouterCascade).
            spent = ctx.task_spent or {}
            s.task, s.outcome = track(s.task, s.outcome, ctx.cand.model.name, ctx.cand.chance, pred, ctx.decision.escalation is not None,
                                      spend0=next((v for k, v in spent.items() if k.startswith("task:")), 0.0),
                                      base0=next((v for k, v in spent.items() if k.startswith("task-base:")), 0.0))
        s.turns += 1
        s.last_seen = now
        s.msg_count = len(msgs)
        s.prefix_hash = prefix_hash(ctx.body, len(msgs))
        s.prefix_tokens = ctx.prompt_tokens
        caches = backend.format == "openai" or ctx.proxy_cache or has_cache_control(ctx.body)
        if caches:
            # Claude keeps what the last marker covers (most of the conversation) as long as that marker asked: a client's
            # own 5-minute markers stay 5 minutes on a backend set to an hour. Elsewhere, the backend's lifetime.
            ttl = ctx.cache_ttls[-1] if ctx.cache_ttls and backend.kind != "self_hosted" else backend.cfg.cache_ttl_seconds
            s.warm_until[backend.name] = now + ttl
        s.warm_until = {k: v for k, v in s.warm_until.items() if v > now}
        if backend.kind == "self_hosted":
            # Where this turn ran, so the next can go back there and tell how much of its cache is left.
            s.placement[backend.name] = mark(ctx.cand.replica, now, ctx.prompt_tokens)
            self.spawn(self.state.hset_json(f"placements:{backend.name}", ctx.kv_id, {
                **s.placement[backend.name], "session": s.id[:12], "thread": (s.thread or "")[len(s.id):][:40] or None,
                "user": ctx.principal.user_id, "team": ctx.principal.team, "model": ctx.cand.model.name,
                "expected": round(ctx.cand.warmth.share, 3) if ctx.cand.warmth else None,
                "source": ctx.cand.warmth.source if ctx.cand.warmth else None}))
        s.placement = {k: v for k, v in s.placement.items() if v.get("at", 0) > now - SESSION_TTL}
        await self.state.put_session(session_key(ctx.principal.user_id, s.state_id), s.to_dict(), SESSION_TTL)


async def refresh_deployments(backends: dict[str, Backend], state, store=None) -> None:
    """Pick up endpoints and states the capacity controller published, and admin GPU controls (gateways, and the worker
    for batch work on GPUs). With SkyPilot deployments, also this install's replica key secret, from ``store``, once."""
    if store is not None and any(b.cfg.serve and b.cfg.serve.provisioner == "skypilot" for b in backends.values()):
        from .skypilot import ensure_identity

        await ensure_identity(store)
    controls = await state.get_json("gpu:controls") or {}
    for backend in backends.values():
        if backend.kind != "self_hosted" or backend.cfg.serve is None:
            continue
        rec = await state.get_json(f"deploy:{backend.name}") or {}
        backend.deploy = rec
        live = rec.get("state") in ("warm", "draining") and bool(rec.get("endpoint"))
        if live:
            backend.cfg.base_url = rec["endpoint"]
        # Charge self-hosted tokens at what the GPUs cost now (SkyPilot's catalog), not the configured guess.
        backend.live_cost_per_hour = rec["burn_usd_per_hour"] / rec["replicas"] if live and rec.get("burn_usd_per_hour") and rec.get("replicas") else None
        backend.available = live
        backend.replicas = rec.get("replicas") if live else None
        # vLLM's waiting-request count from the controller's last sample (bounds the last-resort queue).
        backend.reported_waiting = rec.get("waiting") if live and time.time() - (rec.get("sampled_at") or 0) < 300 else None
        backend.draining = rec.get("state") == "draining"
        backend.replica_set = replicas_of(rec) if live else []
        backend.unavailable_reason = None if live else ("GPUs are starting" if rec.get("state") == "provisioning"
                                                        else "GPUs are not running (demand doesn't pay for them right now)")
        # An admin stop applies to routing at once, before the controller acts on it.
        control = ({"mode": "off", "drain": controls.get("drain", False), "by": controls.get("paused_by"), "reason": controls.get("reason")}
                   if controls.get("paused") else active_control(controls, backend.name, time.time()))
        if control and control.get("mode") == "off":
            who = f"stopped by {control.get('by') or 'an admin'}" + (f": {control['reason']}" if control.get("reason") else "")
            if control.get("drain", True):
                backend.draining = True
            else:
                backend.available = False
            backend.unavailable_reason = who


def known_api_price(cfg: Config, model: ModelConfig) -> Price | None:
    """What a model costs per token through an API, live: the counterfactual for savings. Else the price on any of its
    routes (a GPU route's API-equivalent rate); None when no route has one, so no counterfactual is made up as $0."""
    priced = [r for r in model.routes if r.priced]
    for route in priced:
        bc = cfg.backend(route.backend)
        if bc is not None and bc.kind == "api":
            return route.cost_price()
    return priced[0].cost_price() if priced else None


def api_price(cfg: Config, model: ModelConfig) -> Price:
    """``known_api_price``, or no price (every rate 0) when nothing is known."""
    return known_api_price(cfg, model) or Price()


def api_batch_price(cfg: Config, model: ModelConfig) -> Price:
    """What a provider's batch API would charge for a model: the batch price of its API route (the listed one, else that
    backend's own discount), preferring a route whose backend has a batch API. The counterfactual for batch work on GPUs."""
    api = [(r, bc) for r in model.routes if r.priced and (bc := cfg.backend(r.backend)) is not None and bc.kind == "api"]
    for route, bc in sorted(api, key=lambda x: batch_api(x[1])[0] is None):
        return route.batch_price(bc)
    return api_price(cfg, model).for_batch(BATCH_DISCOUNT)


def _usage_only(ev: SSEEvent) -> bool:
    d = ev.json()
    return bool(d) and not d.get("choices") and bool(d.get("usage"))
