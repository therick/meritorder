"""Build the console demo's sample data: ui/src/demo/data.json, every answer the console asks for, from the real gateway.

The demo (npm run build:demo in ui/, docs/07-deployment.md#the-console-demo) is the console with no gateway behind it: it
answers its API calls in the browser from this file. To keep the numbers what Merit Order would really show, the data
isn't written by hand. This script builds a gateway in memory from config/meritorder.yaml (its models, prices.json and
quality scores) with a made-up organization on top, sends a month and a half of invented traffic through the real
router (levels, exploration, the learned router in shadow mode) and the real pricing (savings by cause), then calls
every endpoint the console uses, as an admin, and writes down the answers.

Nothing leaves this machine: there are no provider keys, every network connection but to localhost is refused, and HOME
points at an empty directory so nothing reads your cloud credentials. Every name and address is invented (example.com).

    uv run python scripts/build_demo_data.py            # writes ui/src/demo/data.json
    uv run python scripts/build_demo_data.py --seed 8   # another organization's month

The same seed gives the same organization and traffic. Timestamps are relative to when it runs: the demo's clock starts
at the moment recorded here (``now`` in the file), so the pages read as they did then.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import socket
import sys
import tempfile
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ui" / "src" / "demo" / "data.json"
ADMIN_KEY = "mo-demo-admin-key-for-the-browser-only"  # the demo signs in as this key; it never leaves the browser

# Backends the demo has keys for (invented: nothing is ever sent), and the GPU deployments it runs.
DEMO_ENV = {
    "ANTHROPIC_API_KEY": "demo-not-a-key", "OPENAI_API_KEY": "demo-not-a-key", "OPENROUTER_API_KEY": "demo-not-a-key",
    "GEMINI_API_KEY": "demo-not-a-key", "AZURE_OPENAI_API_KEY": "demo-not-a-key",
    "AZURE_OPENAI_ENDPOINT": "https://example-openai.openai.azure.com",
    "AWS_ACCESS_KEY_ID": "AKIADEMONOTAKEY", "AWS_SECRET_ACCESS_KEY": "demo-not-a-key", "AWS_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "MERITORDER_VERTEX": "false", "MERITORDER_FOUNDRY_CLAUDE": "false",
    # Foundry for OCR only (Mistral OCR 4.0): turning on Mistral's own API would add its chat models to the traffic.
    "MERITORDER_FOUNDRY_MISTRAL": "true", "AZURE_FOUNDRY_ENDPOINT": "https://example-foundry.services.ai.azure.com",
    "AZURE_FOUNDRY_API_KEY": "demo-not-a-key", "MERITORDER_SKYPILOT": "true",
    "MERITORDER_SKYPILOT_SMALL": "true", "MERITORDER_KUBERNETES": "true", "MERITORDER_FEATURE_KEY": "demo-feature-key",
    "MERITORDER_ORG_BUDGET_USD": "", "MERITORDER_GPU_BUDGET_USD": "", "MERITORDER_MAX_REQUEST_USD": "25",
    "E2E_DEMO_ADMIN_KEY": ADMIN_KEY, "MERITORDER_RESPONSES_KEY": "demo-responses-key-not-a-secret",
}

# The organization: four teams, invented people, what kind of work each sends.
PEOPLE = {
    "platform": [("ana.silva", "Ana Silva", "team_lead"), ("raj.patel", "Raj Patel", "user"), ("kim.nguyen", "Kim Nguyen", "user"),
                 ("tom.berg", "Tom Berg", "user"), ("ola.okafor", "Ola Okafor", "user"), ("alex.morgan", "Alex Morgan", "admin")],
    "mobile": [("lee.wong", "Lee Wong", "team_lead"), ("sam.ortiz", "Sam Ortiz", "user"), ("eva.novak", "Eva Novak", "user"),
               ("max.keller", "Max Keller", "user")],
    "data-science": [("priya.rao", "Priya Rao", "team_lead"), ("jon.fischer", "Jon Fischer", "user"), ("mei.chen", "Mei Chen", "user"),
                     ("dan.cohen", "Dan Cohen", "finance")],
    "support-bots": [("svc-helpdesk", "Helpdesk bot", "user"), ("svc-triage", "Ticket triage bot", "user"),
                     ("nora.hill", "Nora Hill", "team_lead")],
}
ADMIN = ("alex.morgan", "Alex Morgan")  # the demo's own user, an admin in platform
DOMAIN = "example.com"
PROJECTS = {"platform": ["checkout-rewrite", "infra", "search"], "mobile": ["ios-app", "android-app"],
            "data-science": ["churn-model", "eval-suite", "forecasting"], "support-bots": ["helpdesk", "triage"]}

TEAMS = {
    "platform": {"budget_usd_month": 6000, "user_budget_usd_month": 1200, "max_session_usd": 40,
                 "exploration": {"enabled": True, "share": 0.3}, "compression": {"enabled": True},
                 "judged_labels": {"enabled": True, "sample_rate": 0.02, "max_usd_month": 150,
                                   "judges": ["gpt-sol", "claude-sonnet"]}},
    "mobile": {"budget_usd_month": 2500, "user_budget_usd_month": 800, "exploration": {"enabled": True, "share": 0.3},
               "stored_responses": {"enabled": True, "retention_days": 30}},
    "data-science": {"budget_usd_month": 1800, "user_budget_usd_month": 600, "baseline_model": "claude-opus",
                     "exploration": {"enabled": True, "share": 0.3}},
    "support-bots": {"budget_usd_month": 400, "rate_limits": {"team_rpm": 600, "user_rpm": 120},
                     "allowed_models": ["claude-haiku", "gpt-luna", "deepseek-v4.1-flash", "gemini-flash", "qwen3.8-flash"],
                     "response_cache": {"enabled": True}, "compression": {"enabled": True}},
}

# Work: (name, share by team, the request it sends, prompt tokens (lognormal mu, sigma), output tokens, its true level).
# The true level is what the work really needs: where the rules give more, one level down does as well.
WORK = [
    # Claude Code and other agents: many tools, long prompts mostly read from cache.
    ("agent-fix", {"platform": 0.35, "mobile": 0.35}, "fix the failing test in {file} and run the suite", 10.6, 6.6, 4, 12),
    ("agent-feature", {"platform": 0.05, "mobile": 0.05}, "implement the {thing} endpoint, refactor the handler and add tests", 11.0, 7.2, 5, 12),
    ("agent-rename", {"platform": 0.15, "mobile": 0.15}, "rename {thing} to {thing}_id across the module", 10.2, 5.8, 2, 8),
    ("review", {"platform": 0.1, "mobile": 0.1}, "review this diff for bugs before I merge", 9.4, 6.4, 3, 0),
    ("question", {"platform": 0.1, "mobile": 0.1, "data-science": 0.15}, "what does {thing} mean in this context?", 7.0, 5.6, 1, 0),
    ("sql", {"data-science": 0.3, "platform": 0.05}, "write a SQL query for monthly {thing} by region", 8.0, 6.0, 2, 0),
    ("analysis", {"data-science": 0.3}, "analyze the trade-offs of these {thing} forecasts and compare the models", 9.2, 7.0, 4, 0),
    ("notebook", {"data-science": 0.25, "platform": 0.05}, "debug this pandas code that computes {thing}", 9.0, 6.2, 2, 2),
    ("ticket-reply", {"support-bots": 0.6}, "draft a short reply to this customer ticket about {thing}", 7.6, 5.4, 1, 0),
    ("ticket-triage", {"support-bots": 0.4}, "classify this ticket: billing, bug, or how-to", 7.2, 3.0, 1, 0),
]
THINGS = ["invoice", "refund", "session", "checkout", "user", "order", "payment", "report", "token", "cart", "shipment", "plan"]
FILES = ["billing/api.py", "auth/session.ts", "search/index.go", "cart/models.py", "app/Views/Order.swift", "ledger/rollup.rs"]
PER_DAY = {"platform": 1500, "mobile": 700, "data-science": 350, "support-bots": 1100}
GPU_RUNS = {"gpu-qwen-coder": (8, 20), "gpu-qwen-coder-k8s": None, "gpu-small": None}  # weekday hours (UTC) each runs


def guard_network() -> None:
    """Refuse every connection but to this machine: the script never reaches a provider, a cloud, or a price list."""
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise ConnectionRefusedError(f"build_demo_data never connects out ({host})")
        return real(self, address)

    socket.socket.connect = connect
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if host not in ("127.0.0.1", "::1", "localhost", None):
            raise socket.gaierror(f"build_demo_data never looks up {host}")
        return real_getaddrinfo(host, *a, **kw)

    socket.getaddrinfo = getaddrinfo


def email(user: str) -> str:
    return f"{user}@{DOMAIN}" if not user.startswith("svc-") else user


def demo_raw() -> dict:
    """The shipped configuration with the demo organization on top: its teams, keys, budgets and GPU fleet."""
    from meritorder.config import load_raw_config

    raw = load_raw_config(ROOT / "config" / "meritorder.yaml")
    raw["backends"] = {k: v for k, v in raw["backends"].items() if v.get("kind") != "mock" and k != "local-vllm"}
    for m in raw["models"].values():
        m["routes"] = [r for r in m.get("routes", []) if r["backend"] != "local-vllm"]
    raw["models"] = {k: v for k, v in raw["models"].items() if not k.startswith("mock-")}
    # The demo organization ran its codebase eval on the model its GPUs serve (Epoch has no score for it) and applied the
    # result (Models → Evals → Apply scores). Coding 2.5: it does simple coding well and falls short on ordinary and agent
    # work, so `auto` sends it level 1-2 coding while the GPUs run and level 3 and up goes to API models. The suite measures
    # coding only, so general stays unmeasured and `auto` doesn't pick it for general work.
    raw["models"]["qwen3-coder-next"]["quality_override"] = {"coding": 2.5}
    raw["models"]["gpt-oss-20b"]["quality_override"] = {"general": 2.0}
    raw["teams"] = TEAMS
    raw["safeguards"] = {"org_budget_usd_month": 14000, "gpu_budget_usd_month": 4000, "max_gpu_usd_per_hour": 60,
                         "max_gpu_replicas": 6, "max_request_usd": 25, "alert_at": [0.8, 1.0]}
    raw["routing"] = {**raw["routing"], "learned": {"mode": "shadow"}, "benchmark": {"min_outcomes": 40, "as_well": 0.92}}
    raw["kubernetes"] = {**raw.get("kubernetes", {}), "namespace": "gpus"}
    for name in ("gpu-qwen-coder", "gpu-qwen-coder-k8s"):
        # Measured on the organization's own traffic: a mixture of experts with 3B active parameters, 4 H100s a replica.
        raw["backends"][name]["capacity_tokens_per_second"] = 20000
    raw["batches"] = {"gpu": {"enabled": True}}
    raw["billing"] = {"sources": [
        {"name": "anthropic-usage", "type": "anthropic", "backends": ["anthropic"], "auth": {"type": "api_key", "value_env": "ANTHROPIC_ADMIN_KEY"}},
        {"name": "openai-costs", "type": "openai", "backends": ["openai"], "auth": {"type": "api_key", "value_env": "OPENAI_ADMIN_KEY"}},
        {"name": "aws-cur", "type": "aws_cur", "backends": ["bedrock"],
         "bucket": "example-billing-exports", "prefix": "cur/meritorder"},
    ]}
    raw["reporting"] = {}
    raw["compressor"] = {}
    raw["auth"] = {"static_keys": [{"key_env": "E2E_DEMO_ADMIN_KEY", "user": email(ADMIN[0]), "team": "platform", "role": "admin"}]}
    return raw


def preset_ranges(now: datetime) -> dict[str, tuple[str, str]]:
    """The console's period presets (presetRange in ui/src/api.ts), on the demo's clock."""
    tomorrow = (now + timedelta(days=1)).date()
    first = now.date().replace(day=1)
    last_first = (first - timedelta(days=1)).replace(day=1)
    return {"month": (first.isoformat(), tomorrow.isoformat()),
            "7d": ((tomorrow - timedelta(days=7)).isoformat(), tomorrow.isoformat()),
            "30d": ((tomorrow - timedelta(days=30)).isoformat(), tomorrow.isoformat()),
            "last-month": (last_first.isoformat(), first.isoformat())}


class Demo:
    def __init__(self, seed: int, now: datetime):
        from meritorder.app import build_gateway
        from meritorder.config import build_config
        from meritorder.state import MemoryState
        from meritorder.store import MemoryStore

        self.rng = random.Random(seed)
        self.now = now
        self.raw = demo_raw()
        self.cfg = build_config(self.raw, ROOT / "config", quiet=True)
        self.gw = build_gateway(self.cfg, store=MemoryStore(), state=MemoryState())
        self.gw.file_raw = self.raw
        self.store, self.state = self.gw.store, self.gw.state
        self.ids = random.Random(seed + 1)

    def uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self.ids.getrandbits(128), version=4)

    # ------------------------------------------------------------------ people and keys

    async def people(self) -> None:
        for team, people in PEOPLE.items():
            for user, name, role in people:
                await self.store.upsert_user(email(user), team, role, email(user) if "@" in email(user) else None, name, source="local")
        for team, people in PEOPLE.items():
            for user, _, role in people:
                await self.store.create_key(email(user), team, role, name="laptop" if not user.startswith("svc") else "production",
                                            tags={"project": PROJECTS[team][0]})
        ci_id, _ = await self.store.create_key("ci-runner", "platform", "user", name="ci", tags={"project": "infra"},
                                               expires_at=self.now + timedelta(days=60))
        old_id, _ = await self.store.create_key(email("tom.berg"), "platform", "user", name="old laptop")
        await self.store.revoke_key(old_id)
        await self.store.update_user(email("eva.novak"), budget_usd_month=200)
        # People joined over the last months and were last seen recently; keys were made when they joined.
        for i, u in enumerate(self.store.users.values()):
            u["created_at"] = self.now - timedelta(days=40 + 7 * i, hours=i)
            u["last_seen"] = self.now - timedelta(minutes=self.rng.randrange(2, 600 if i % 5 else 4000))
            u["managed"] = "local"
        for k in self.store.keys.values():
            joined = self.store.users.get(k["user_id"], {}).get("created_at") or self.now - timedelta(days=12)
            k["created_at"] = joined + timedelta(minutes=5) if k["name"] != "old laptop" else joined
            if k["revoked_at"]:
                k["revoked_at"] = self.now - timedelta(days=9)

    # ------------------------------------------------------------------ traffic

    def body(self, work: tuple, rng: random.Random) -> dict:
        name, _, text, _, _, _, tools = work
        text = text.format(file=rng.choice(FILES), thing=rng.choice(THINGS))
        body: dict = {"messages": [{"role": "user", "content": text}]}
        if tools:
            body["tools"] = [{"name": n, "description": n, "input_schema": {"type": "object"}}
                             for n in ["bash", "read", "edit", "grep", "glob", "write", "web_fetch", "todo", "task", "notebook", "ls", "test"][:tools]]
        return body

    def requests_plan(self) -> list[dict]:
        """Every request of the period, oldest first: when, who, what work."""
        rng = self.rng
        start = datetime(self.now.year, self.now.month, 1, tzinfo=UTC) - timedelta(days=1)
        start = start.replace(day=1)
        plan = []
        day = start
        growth = 0.0
        while day <= self.now:
            weekday = day.weekday() < 5
            growth = (day - start).days / 45  # adoption grows over the period
            for team, n in PER_DAY.items():
                scale = (1.0 if weekday else (0.6 if team == "support-bots" else 0.15)) * (0.75 + 0.5 * growth)
                count = int(n * scale * rng.uniform(0.85, 1.15))
                works = [w for w in WORK if team in w[1]]
                for _ in range(count):
                    hour = rng.choices(range(24), weights=[1, 1, 1, 1, 1, 1, 2, 4, 8, 10, 10, 9, 8, 9, 10, 10, 9, 7, 5, 3, 2, 2, 1, 1])[0]
                    if team == "support-bots":
                        hour = rng.randrange(24)
                    ts = day.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))
                    if ts > self.now:
                        continue
                    work = rng.choices(works, weights=[w[1][team] for w in works])[0]
                    user = rng.choice([p for p in PEOPLE[team] if p[2] != "finance"])[0]
                    plan.append({"ts": ts, "team": team, "user": email(user), "work": work})
            day += timedelta(days=1)
        # The last few minutes are busy, so Dispatch has a full feed and a merit stack.
        for i in range(70):
            team = rng.choices(list(PER_DAY), weights=list(PER_DAY.values()))[0]
            works = [w for w in WORK if team in w[1]]
            work = rng.choices(works, weights=[w[1][team] for w in works])[0]
            user = rng.choice([p for p in PEOPLE[team] if p[2] != "finance"])[0]
            plan.append({"ts": self.now - timedelta(seconds=rng.randrange(1, 900)), "team": team, "user": email(user), "work": work})
        plan.sort(key=lambda p: p["ts"])
        return plan

    def gpu_on(self, backend: str, ts: datetime) -> bool:
        hours = GPU_RUNS.get(backend)
        if not hours:
            return False
        if ts >= self.now - timedelta(hours=5, minutes=5):
            return True  # the run going on now (gpus: started 5h12m ago, warm 7 minutes later)
        today = self.now.replace(hour=0, minute=0)
        return (ts.weekday() < 5 and hours[0] <= ts.hour < hours[1] and self.now - timedelta(days=24) <= ts < today)

    async def set_gpus(self, on: set[str]) -> None:
        for b in self.cfg.backends:
            if b.kind == "self_hosted" and b.serve is not None and b.enabled:
                live = b.name in on
                await self.state.set_json(f"deploy:{b.name}", {"state": "warm" if live else "api_only", "replicas": 2 if live else 0,
                                                               "burn_usd_per_hour": 12.84 if live else 0,
                                                               "endpoint": f"https://{b.name}.gpus.example.com/v1" if live else None})
        await self.gw.refresh_deployments()

    async def budgets(self) -> None:
        """Set each team's budget from what it spends, so the month's meters read as a real organization's: platform close
        to its limit, the others comfortably under. Applied as the gateway applies a saved configuration."""
        from meritorder.config import build_config
        from meritorder.store import month_start

        start = month_start(self.now)
        spent: dict[str, float] = {}
        for r in self.store.ledger:
            if r["ts"] >= start:
                spent[r["team"]] = spent.get(r["team"], 0.0) + (r["actual_cost"] or 0.0)
        frac = (self.now - start).total_seconds() / (30 * 86400)
        share = {"platform": 0.97, "mobile": 0.7, "data-science": 0.8, "support-bots": 0.45}  # the month projected, of its budget
        for team, s in share.items():
            projected = spent.get(team, 0.0) / max(frac, 0.05)
            budget = max(50, round(projected / s / 50) * 50)
            self.raw["teams"][team]["budget_usd_month"] = budget
            if "user_budget_usd_month" in self.raw["teams"][team]:
                self.raw["teams"][team]["user_budget_usd_month"] = max(25, round(budget / len(PEOPLE[team]) * 1.5 / 25) * 25)
        from meritorder.spend import budget_spend

        total = (await budget_spend(self.store, self.cfg, "org", now=self.now))["spent"]  # the ledger and the GPUs' time, as the org budget counts it
        gpu = sum(c for (h, _), (_, c) in self.store.__dict__.get("_gpu_usage", {}).items() if h >= start)
        self.raw["safeguards"]["org_budget_usd_month"] = max(100, round(total / max(frac, 0.05) * 1.25 / 100) * 100)
        self.raw["safeguards"]["gpu_budget_usd_month"] = max(100, round(gpu / max(frac, 0.05) * 1.3 / 100) * 100)
        self.cfg = build_config(self.raw, ROOT / "config", quiet=True)
        await self.gw.apply_config(self.cfg, None)
        self.gw.file_raw = self.raw
        self.spent = spent

    async def traffic(self) -> None:
        from meritorder.learned_jobs import train_and_save
        from meritorder.pricing import Usage, attribute, chargeback, compression_savings
        from meritorder.proxy import api_price, route_reason

        plan = self.requests_plan()
        await self.gw.load_learned(None)  # the feature key (MERITORDER_FEATURE_KEY), as a gateway loads it at start
        cut = self.now - timedelta(days=8)  # the learned router is trained here, then watches in shadow mode
        trained = False
        gpu_state: frozenset | None = None
        rng = self.rng
        self.recent: list[dict] = []
        self.gpu_hours: set[tuple[datetime, str]] = set()
        for p in plan:
            if not trained and p["ts"] >= cut:
                await self.store.rollup(full=True)
                out = await train_and_save(self.cfg, self.store, self.state, email(ADMIN[0]), now=cut)
                if out.get("version"):
                    await self.store.activate_router_model(out["version"])
                    await self.gw.load_learned(out["version"])
                trained = True
            on = frozenset(b for b in GPU_RUNS if self.gpu_on(b, p["ts"]))
            if on and p["ts"] < self.now - timedelta(hours=5, minutes=5):
                self.gpu_hours.update((p["ts"].replace(minute=0, second=0), b) for b in on)
            if on != gpu_state:
                await self.set_gpus(set(on))
                gpu_state = on
            name, _, _, mu, out_mu, true_level, _ = p["work"]
            team = self.cfg.team(p["team"])
            body = self.body(p["work"], rng)
            prompt = int(min(rng.lognormvariate(mu, 0.7), 180_000))
            named = rng.random() < 0.06 and p["team"] != "support-bots"
            requested = rng.choice(["claude-sonnet", "claude-opus", "gpt-sol"]) if named else "auto"
            d = self.gw.router.decide(requested, team, body, {"user-agent": "claude-cli/2.1.0 (external, cli)"} if p["work"][6] else {},
                                      None, prompt, True, {}, p["ts"].timestamp(), client="anthropic", explore=True)
            if not d.candidates:
                continue
            cand = d.candidates[0]
            model, backend = cand.model, cand.backend
            # Tokens: agents read most of a long prompt from cache; one-shot requests mostly don't.
            cached = int(prompt * (rng.uniform(0.75, 0.95) if p["work"][6] >= 8 else rng.uniform(0.0, 0.4)))
            explicit = backend.format == "anthropic"
            write = int((prompt - cached) * (0.8 if explicit else 0.0))
            usage = Usage(input_tokens=prompt - cached - write, cache_read_tokens=cached, cache_write_tokens=write,
                          output_tokens=int(min(rng.lognormvariate(out_mu, 0.6), 30_000)))
            failed = rng.random() < 0.006
            status = rng.choice([529, 500, 429]) if failed else 200
            self_hosted = backend.kind == "self_hosted"
            served_price = api_price(self.cfg, model) if self_hosted else cand.route.cost_price()
            per_hour = getattr(backend, "live_cost_per_hour", None) or backend.cfg.gpu_cost_per_hour  # as Gateway.costs prices it
            self_cost = chargeback(per_hour, backend.cfg.capacity_tokens_per_second, usage) if self_hosted else None
            baseline = self.cfg.model(team.baseline_model or self.cfg.baseline_model)
            costs = attribute(usage, served_price, api_price(self.cfg, baseline), proxy_cache=p["work"][6] >= 8, self_hosted_cost=self_cost)
            if failed:
                usage = Usage()
                costs = attribute(usage, served_price, None, False)
            compression = 0.0
            if team.compression.enabled and p["work"][6] and not failed:
                removed = int(cached * rng.uniform(0.02, 0.08))  # taken out of earlier turns: history, as the gateway counts it
                compression = compression_savings(served_price, 0, removed)
                costs.baseline += compression
            cached_reply = team.response_cache.enabled and rng.random() < 0.12 and not failed
            row = {
                "id": self.uuid(), "ts": p["ts"], "user_id": p["user"], "team": p["team"], "key_id": "demo",
                "session_id": f"s-{rng.randrange(1, 5000)}", "client_format": "anthropic" if p["work"][6] else rng.choice(["openai", "anthropic"]),
                "tags": {"project": rng.choice(PROJECTS[p["team"]]), **({"explore": d.explore["arm"]} if d.explore and d.explore["arm"] != "served" else {}),
                         **({"compression": "rules"} if compression else {})},
                "requested_model": requested, "floor": str(d.floor) if d.floor else None, "route_reason": route_reason(d),
                "served_model": model.name, "upstream_model": cand.route.model, "backend": "response-cache" if cached_reply else backend.name,
                "backend_kind": "cache" if cached_reply else backend.kind, "status": status,
                "error": {529: "overloaded_error: Overloaded", 500: "api_error: Internal server error", 429: "rate_limit_error: slow down"}.get(status),
                "stream": True, "upgraded": cand.upgraded, "proxy_cache": p["work"][6] >= 8,
                "input_tokens": usage.input_tokens, "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens, "output_tokens": usage.output_tokens,
                "latency_ms": int(rng.lognormvariate(8.4 if usage.output_tokens > 800 else 7.4, 0.5)),
                "ttft_ms": int(rng.lognormvariate(6.4 if not self_hosted else 5.6, 0.4)),
                "actual_cost": costs.actual, "served_api_cost": costs.served_api, "baseline_cost": costs.baseline,
                "list_cost": costs.list_price, "savings_routing": costs.savings_routing, "savings_caching": costs.savings_caching,
                "savings_self_hosting": costs.savings_self_hosting, "savings_compression": compression, "savings_compression_model": 0.0,
                "savings_response_cache": 0.0,
            }
            if cached_reply:
                row.update(savings_response_cache=row["actual_cost"], actual_cost=0.0, savings_routing=0.0, savings_caching=0.0,
                           savings_self_hosting=0.0, savings_compression=0.0, latency_ms=int(rng.uniform(8, 40)), ttft_ms=None)
                row["savings_response_cache"] = row["baseline_cost"]
            await self.store.record(row)
            self.recent.append(row)
            if d.learned is not None and requested == "auto":
                await self.features(row, d, model, true_level, rng)
        await self.set_gpus(set())
        await self.store.rollup(full=True)

    async def features(self, row: dict, d, model, true_level: int, rng: random.Random) -> None:
        """The learned router's row for a task start, with what came of it: a model short of the work's true level fails
        more often, and a failure is seen (sent again, a raised level, tests still failing) most of the time."""
        call = d.learned
        task = call.features.task
        score = model.q(task) or 1
        level = d.floor.level if d.floor else true_level
        gap = true_level - min(score, level + 0.5)
        fail = 1 / (1 + math.exp(-(2.2 * gap - 2.4)))
        failed = rng.random() < fail
        seen = rng.random() < (0.7 if failed else 0.03)
        coding = {}
        if task == "coding" and row["client_format"] == "anthropic":
            tests = rng.randint(1, 6)
            coding = {"tests": tests, "tests_failed": tests if failed and rng.random() < 0.5 else 0, "turns": rng.randint(3, 30),
                      "builds_failed": 1 if failed and rng.random() < 0.2 else 0, "correction": failed and rng.random() < 0.3}
        x = d.explore
        await self.store.record_route_features({
            "request_id": row["id"], "ts": row["ts"], "team": row["team"], "user_id": row["user_id"], "session_id": row["session_id"],
            "thread": uuid.UUID(int=rng.getrandbits(128)).hex[:16], "fv": call.fv, "task": task, "rules_level": call.features.rules,
            "text_features": call.features.text, "tags": call.features.tags, "auto": True, "served_model": model.name,
            "served_score": model.q(task) or None, "refused_scores": [], "status": row["status"],
            "retried": seen and rng.random() < 0.7, "floor_raised": (level + 1 if level < 5 else None) if seen and rng.random() < 0.4 else None,
            "learned": call.record() if call.outcome != "named" else None, "level": level,
            "arm": x.get("arm") if x else None, "arm_from": x.get("from") if x else None, "propensity": x.get("p") if x else None,
            "coding": coding or None, "judged": await self.judged(row, model, failed, rng),
        })

    async def judged(self, row: dict, model, failed: bool, rng: random.Random) -> dict | None:
        """A sampled task's judged label (judged.py): a stronger model answered it too, and a judge compared the two. Its
        replay and judging are the team's own spend, as the gateway records them."""
        from meritorder.judged import JUDGED_TAG, internal_user

        s = self.cfg.team(row["team"]).judged_labels
        if not s.enabled or rng.random() >= s.sample_rate or row["status"] >= 400:
            return None
        good = (not failed) if rng.random() > 0.1 else rng.random() < 0.5
        stronger = "claude-opus" if model.name != "claude-opus" else "claude-fable"
        judge = rng.choice(s.judges or ["gpt-sol"])
        replay, judging = rng.uniform(0.01, 0.06), rng.uniform(0.004, 0.012)
        for kind, cost, served in (("replay", replay, stronger), ("judge", judging, judge)):
            backend = "anthropic" if served.startswith("claude") else "openai"
            await self.store.record({"id": self.uuid(), "ts": row["ts"] + timedelta(seconds=rng.randrange(20, 300)),
                                     "user_id": internal_user(row["team"]), "team": row["team"], "key_id": None,
                                     "tags": {JUDGED_TAG: kind}, "client_format": "anthropic", "requested_model": served, "served_model": served,
                                     "upstream_model": next(r.model for r in self.cfg.model(served).routes if r.backend == backend),
                                     "backend": backend, "backend_kind": "api", "status": 200,
                                     "input_tokens": int(cost * 2e5), "output_tokens": int(cost * 2e4), "actual_cost": cost,
                                     "served_api_cost": cost, "baseline_cost": cost, "list_cost": cost})
        return {"outcome": "judged", "good_enough": good, "stronger_good_enough": True, "first": rng.choice(["cheaper", "stronger"]),
                "better": "same" if good else "stronger", "served": model.name, "stronger": stronger, "judge": judge,
                "cost": round(replay + judging, 6)}


    # ------------------------------------------------------------------ batches

    async def batches(self) -> None:
        """A few batches through the providers' batch APIs (and the GPUs), with their ledger rows at batch prices."""
        from meritorder.pricing import Usage, attribute
        from meritorder.proxy import api_price

        rng = self.rng
        jobs = [("data-science", email("mei.chen"), "anthropic", "claude-sonnet", "anthropic", 1800, 26, "completed"),
                ("data-science", email("jon.fischer"), "openai", "gpt-terra", "openai", 4200, 18, "completed"),
                ("platform", email("raj.patel"), "openai", "qwen3-coder-next", "gpu-qwen-coder", 950, 9, "completed"),
                ("support-bots", email("nora.hill"), "anthropic", "claude-haiku", "anthropic", 12000, 4, "completed"),
                ("data-science", email("priya.rao"), "anthropic", "claude-opus", "anthropic", 600, 2, "cancelled"),
                ("platform", email("kim.nguyen"), "openai", "gpt-terra", "openai", 2400, 0.4, "in_progress"),
                ("data-science", email("mei.chen"), "anthropic", "claude-sonnet", "anthropic", 3100, 0.1, "in_progress"),
                ("support-bots", email("svc-triage"), "openai", "gpt-luna", "openai", 800, 35, "expired")]
        for team, user, fmt, model_name, backend, n, days_ago, outcome in jobs:
            created = self.now - timedelta(days=days_ago, minutes=rng.randrange(600))
            model = self.cfg.model(model_name)
            route = next((r for r in model.routes if r.backend == backend), model.routes[0])
            live_price = api_price(self.cfg, model) if backend.startswith("gpu-") else route.cost_price()
            batch_price = live_price.model_copy(update={"input": live_price.input / 2, "output": live_price.output / 2})
            baseline = api_price(self.cfg, self.cfg.model(self.cfg.team(team).baseline_model or self.cfg.baseline_model))
            done = n if outcome == "completed" else int(n * (0.35 if outcome == "in_progress" else 0.6))
            totals = dict.fromkeys(("actual", "baseline", "batch", "total", "self"), 0.0)
            batch_id = f"msgbatch_{self.uuid().hex[:24]}" if fmt == "anthropic" else f"batch_{self.uuid().hex[:24]}"
            for i in range(done):
                usage = Usage(input_tokens=int(rng.lognormvariate(7.6, 0.5)), output_tokens=int(rng.lognormvariate(6.0, 0.5)))
                gpu = backend.startswith("gpu-")
                costs = attribute(usage, live_price, baseline, False, self_hosted_cost=usage.total_tokens / 20000 * 6.4 / 3600 if gpu else None,
                                  batch_price=batch_price)
                ts = created + timedelta(minutes=rng.uniform(5, 600 if outcome == "completed" else 30))
                if ts > self.now:
                    ts = self.now - timedelta(minutes=1)
                c = costs.as_dict()
                await self.store.record({
                    "id": self.uuid(), "ts": ts, "user_id": user, "team": team, "key_id": "demo", "session_id": None,
                    "tags": {"project": rng.choice(PROJECTS[team])}, "client_format": fmt, "requested_model": model_name, "floor": None,
                    "route_reason": f"named model {model_name}; " + (f"batch work on {backend}" if gpu else f"batch: {backend}'s batch API"),
                    "served_model": model_name, "upstream_model": route.model, "backend": backend,
                    "backend_kind": "self_hosted" if gpu else "api", "status": 200, "error": None, "stream": False, "upgraded": False,
                    "proxy_cache": False, "input_tokens": usage.input_tokens, "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "output_tokens": usage.output_tokens, "latency_ms": None, "ttft_ms": None, "service_tier": "batch", "batch_id": batch_id,
                    **{k: c[v] for k, v in (("actual_cost", "actual"), ("served_api_cost", "served_api"), ("baseline_cost", "baseline"),
                                            ("list_cost", "list_price"), ("savings_routing", "savings_routing"),
                                            ("savings_caching", "savings_caching"), ("savings_self_hosting", "savings_self_hosting"),
                                            ("savings_batch", "savings_batch"))},
                    "savings_compression": 0.0, "savings_compression_model": 0.0, "savings_response_cache": 0.0,
                })
                totals["actual"] += c["actual"]
                totals["baseline"] += c["baseline"]
                totals["batch"] += c["savings_batch"]
                totals["self"] += c["savings_self_hosting"]
                totals["total"] += c["savings_total"]
            per = totals["actual"] / max(done, 1)
            ended = outcome != "in_progress"
            await self.store.create_batch({
                "id": batch_id, "created_at": created, "user_id": user, "team": team, "key_id": "demo",
                "tags": {"project": rng.choice(PROJECTS[team])}, "client_format": fmt,
                "endpoint": "/v1/messages" if fmt == "anthropic" else "/v1/chat/completions",
                "status": "ended" if ended else "running", "outcome": outcome if ended else None,
                "expires_at": created + timedelta(hours=24), "ended_at": created + timedelta(hours=rng.uniform(1, 20)) if ended else None,
                "cancelled_by": user if outcome == "cancelled" else None,
                "cancel_requested_at": created + timedelta(hours=1) if outcome == "cancelled" else None,
                "request_count": n,
                "counts": {"processing": n - done if not ended else 0, "succeeded": done if outcome != "expired" else int(done * 0.9),
                           "errored": 0 if outcome != "expired" else done - int(done * 0.9),
                           "canceled": n - done if outcome == "cancelled" else 0, "expired": n - done if outcome == "expired" else 0},
                "providers": {backend: n}, "estimated_cost": per * n * 1.15, "reserved_cost": per * n * 1.15 if not ended else 0.0,
                "actual_cost": totals["actual"], "baseline_cost": totals["baseline"], "savings_batch": totals["batch"],
                "savings_total": totals["total"], "savings_self_hosting": totals["self"],
                "errors": {"data": [{"message": "the batch expired before every request ran (24 hours)"}]} if outcome == "expired" else {},
            }, [], [])

    async def ocr(self) -> None:
        """Documents read through /v1/ocr: invoices and contracts the data-science and support teams send, priced per page
        (the routes' prices in prices.json), with the ledger rows the gateway would write."""
        from meritorder.store import SAVINGS_FIELDS

        rng = self.rng
        model = self.cfg.model("mistral-ocr-4-0")
        route = model.routes[0]
        people = [("data-science", email("mei.chen")), ("data-science", email("jon.fischer")), ("support-bots", email("nora.hill"))]
        for _ in range(140):
            team, user = rng.choice(people)
            pages = max(1, int(rng.lognormvariate(1.6, 0.9)))
            annotated = rng.random() < 0.2
            cost = pages * route.cost_price().page(annotated)
            ts = self.now - timedelta(days=rng.uniform(0, 44), minutes=rng.randrange(600))
            await self.store.record({
                "id": self.uuid(), "ts": ts, "user_id": user, "team": team, "key_id": "demo", "session_id": None,
                "tags": {"project": rng.choice(PROJECTS[team]), **({"ocr_annotated": "1"} if annotated else {})}, "client_format": "ocr",
                "requested_model": rng.choice(["mistral-ocr-4-0", "mistral-ocr-4-0", "ocr"]), "floor": None, "route_reason": None,
                "served_model": model.name, "upstream_model": route.model, "backend": route.backend, "backend_kind": "api", "status": 200,
                "error": None, "stream": False, "upgraded": False, "proxy_cache": False, "input_tokens": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "output_tokens": 0, "latency_ms": int(900 + pages * rng.uniform(250, 600)), "ttft_ms": None,
                "actual_cost": cost, "served_api_cost": cost, "baseline_cost": cost, "list_cost": cost, "pages": pages,
                **dict.fromkeys(SAVINGS_FIELDS, 0.0),
            })

    # ------------------------------------------------------------------ GPUs

    async def gpus(self) -> None:
        """The GPU fleet now: one deployment running on spot (two replicas), one starting on Kubernetes, one stopped; its
        hours this month, and the activity log behind it."""
        rng, now = self.rng, self.now
        t = now.timestamp()
        started = t - 5 * 3600 - 720
        yesterday_evening = (now - timedelta(days=1)).replace(hour=20, minute=2).timestamp()  # when the last run drained
        await self.state.set_json("controller:heartbeat", {"at": t - 14, "host": "meritorder-worker-7c9f4", "interval": 30})

        def replica(i: int, zone: str, util: float, waiting: int) -> dict:
            return {"id": i, "status": "READY", "cloud": "aws", "region": "us-east-1", "zone": zone, "spot": True,
                    "resources": "1x AWS(p5.24xlarge[Spot], {'H100': 4})", "instance_type": "p5.24xlarge", "usd_per_hour": 6.42,
                    "launched_at": started + 60 * i, "endpoint": f"https://10.0.{i}.17:8000", "url": f"https://10.0.{i}.17:8000/v1",
                    "utilization": util, "waiting": waiting, "kv_capacity_tokens": 1_900_000, "kv_usage": util * 0.8,
                    "prefix_hit_rate": 0.83, "kv_events": True, "cluster": f"mo-a1b2-gpu-qwen-coder-{i}",
                    "lease_expires_in": 540 - 60 * i, "tunnel": False, "message": None}

        price_spot = {"usd_per_hour": 6.42, "source": "skypilot_catalog", "cloud": "aws", "region": "us-east-1",
                      "instance_type": "p5.24xlarge", "spot": True, "configured_usd_per_hour": 16.0, "gpus": 4}
        await self.state.set_json("deploy:gpu-qwen-coder", {
            "state": "warm", "since": started + 420, "replicas": 2, "desired": 2, "planned": 2, "endpoint": "https://10.0.1.17:8000/v1",
            "utilization": 0.64, "waiting": 1, "demand_usd_per_hour": 38.6, "demand_tokens_per_second": 31_000.0,
            "gpu_usd_per_hour": 6.42, "burn_usd_per_hour": 12.84, "price": price_spot, "started_at": started, "warm_at": started + 420,
            "run_cost": round((t - started) / 3600 * 12.84, 2), "note": "demand pays for 2 replicas (scale_up_after_minutes 10)",
            "updated": datetime.fromtimestamp(t - 14, UTC).isoformat(timespec="seconds"), "status_message": None,
            "replicas_info": [replica(1, "us-east-1a", 0.71, 1), replica(2, "us-east-1d", 0.57, 0)],
            "cache": {"affinity": True, "source": "events", "kv_events": True, "kv_events_problem": None},
            "last_run": {"started_at": started - 86400 + 3 * 3600, "ended_at": started - 86400 + 15 * 3600, "cost": 141.2},
        })
        await self.state.set_json("deploy:gpu-qwen-coder-k8s", {
            "state": "provisioning", "since": t - 260, "replicas": 1, "desired": 1, "planned": 1, "endpoint": None,
            "gpu_usd_per_hour": 27.52, "burn_usd_per_hour": 27.52, "started_at": t - 260, "run_cost": round(260 / 3600 * 27.52, 2),
            "price": {"usd_per_hour": 27.52, "source": "node_type", "node_type": "p5.48xlarge", "node_usd_per_hour": 55.04, "node_gpus": 8,
                      "gpus": 4, "configured_usd_per_hour": 16.0},
            "demand_usd_per_hour": 0.0, "demand_tokens_per_second": 0.0, "note": "pinned on by alex.morgan@example.com",
            "status_message": "0/6 nodes are available: 6 Insufficient nvidia.com/gpu. The cluster autoscaler is adding a p5.48xlarge node.",
            "updated": datetime.fromtimestamp(t - 14, UTC).isoformat(timespec="seconds"),
            "replicas_info": [{"id": "qwen-coder-predictor-7d9f8-x2k4q", "status": "PENDING", "infra": "kubernetes", "resources": "4x H100",
                               "instance_type": "p5.48xlarge", "usd_per_hour": 27.52, "launched_at": t - 250,
                               "message": "waiting for a GPU node (Insufficient nvidia.com/gpu)"}],
        })
        await self.state.set_json("deploy:gpu-small", {
            "state": "api_only", "since": t - 2 * 86400, "replicas": 0, "desired": 0, "planned": 0, "endpoint": None,
            "gpu_usd_per_hour": 1.29, "burn_usd_per_hour": 0.0, "demand_usd_per_hour": 0.31, "demand_tokens_per_second": 40.0,
            "price": {"usd_per_hour": 1.29, "source": "skypilot_catalog", "cloud": "lambda", "region": "us-east-1",
                      "instance_type": "gpu_1x_a10", "spot": False, "configured_usd_per_hour": 1.29, "gpus": 1},
            "note": "demand $0.31/h at API prices doesn't pay for a replica at $1.29/h",
            "updated": datetime.fromtimestamp(t - 14, UTC).isoformat(timespec="seconds"),
            "last_run": {"started_at": t - 3 * 86400, "ended_at": t - 2 * 86400, "cost": 4.1},
        })
        # Admin controls, through the real endpoints: the Kubernetes deployment pinned on for a load test.
        await self.post(f"/v1/admin/gpu/gpu-qwen-coder-k8s/control",
                        {"mode": "on", "replicas": 1, "hours": 4, "reason": "load test for the mobile release"})
        events = [
            (3 * 86400, "state", "gpu-small", "controller", {"from": "warm", "to": "draining", "reason": "demand fell under the break-even"}),
            (2 * 86400, "state", "gpu-small", "controller", {"from": "draining", "to": "api_only", "reason": "drained"}),
            (2 * 86400 - 30, "sweep", "gpu-small", "controller", {"cluster": "mo-a1b2-gpu-small-1", "deleted": True}),
            (t - yesterday_evening, "state", "gpu-qwen-coder", "controller", {"from": "warm", "to": "draining", "reason": "after hours: demand fell"}),
            (t - yesterday_evening - 1500, "state", "gpu-qwen-coder", "controller", {"from": "draining", "to": "api_only", "reason": "drained"}),
            (t - started, "state", "gpu-qwen-coder", "controller", {"from": "api_only", "to": "provisioning",
                                                                     "reason": "demand $31.20/h at API prices pays for a replica at $6.42/h"}),
            (t - started - 420, "state", "gpu-qwen-coder", "controller", {"from": "provisioning", "to": "warm", "reason": "1 replica ready"}),
            (t - started - 1500, "scale", "gpu-qwen-coder", "controller", {"replicas": 2, "reason": "utilization 0.86 for 10 minutes"}),
            (3300, "error", "gpu-qwen-coder", "controller", {"action": "scale", "error": "spot capacity not available in us-east-1c; retried in us-east-1d"}),
            (3240, "status", "gpu-qwen-coder", "controller", {"message": "replica 2 preempted (spot); relaunching in us-east-1d"}),
            (260, "state", "gpu-qwen-coder-k8s", "controller", {"from": "api_only", "to": "provisioning", "reason": "pinned on"}),
            (250, "status", "gpu-qwen-coder-k8s", "controller", {"message": "0/6 nodes are available: 6 Insufficient nvidia.com/gpu."}),
        ]
        store_events = self.store.__dict__.setdefault("_gpu_events", [])
        pinned = store_events[:]  # the control just recorded, by the real endpoint
        store_events.clear()
        for ago, kind, backend, actor, detail in sorted(events, key=lambda e: -e[0]):
            store_events.append({"id": len(store_events) + 1, "ts": datetime.fromtimestamp(t - ago, UTC), "backend": backend,
                                 "kind": kind, "actor": actor, "detail": detail})
        for e in pinned:
            store_events.append({**e, "id": len(store_events) + 1, "ts": datetime.fromtimestamp(t - 265, UTC)})
        store_events.sort(key=lambda e: e["ts"])
        for i, e in enumerate(store_events):
            e["id"] = i + 1
        await self.gw.refresh_deployments()

    async def gpu_catalog(self) -> None:
        """What GPU sizing reads (Models → Self-hosting): a SkyPilot catalog of GPU offers and each open model's size, as
        the controller would have published them. The prices are illustrative, of the kind SkyPilot lists."""
        from meritorder import gpu_sizing as gs

        offers = []
        for cloud, acc, count, od, spot, region, itype in [
            ("aws", "L4", 1, 0.80, 0.29, "us-east-1", "g6.xlarge"), ("aws", "A10G", 1, 1.01, 0.38, "us-east-1", "g5.xlarge"),
            ("aws", "L40S", 1, 1.86, 0.71, "us-east-1", "g6e.xlarge"), ("aws", "L40S", 4, 10.49, 3.95, "us-east-1", "g6e.12xlarge"),
            ("aws", "A100-80GB", 8, 40.97, 13.3, "us-east-1", "p4de.24xlarge"), ("aws", "H100", 1, 6.88, 2.6, "us-east-1", "p5.4xlarge"),
            ("aws", "H100", 4, 27.5, 6.42, "us-east-1", "p5.24xlarge"), ("aws", "H100", 8, 55.04, 12.9, "us-east-1", "p5.48xlarge"),
            ("aws", "H200", 8, 84.8, 24.1, "us-east-1", "p5en.48xlarge"), ("gcp", "L4", 1, 0.71, 0.22, "us-central1", "g2-standard-4"),
            ("gcp", "A100-80GB", 1, 5.07, 1.57, "us-central1", "a2-ultragpu-1g"), ("gcp", "H100", 8, 88.25, 21.4, "us-central1", "a3-highgpu-8g"),
            ("lambda", "A10", 1, 0.75, None, "us-east-1", "gpu_1x_a10"), ("lambda", "H100", 1, 2.49, None, "us-east-1", "gpu_1x_h100_sxm5"),
            ("lambda", "H100", 8, 23.92, None, "us-east-1", "gpu_8x_h100_sxm5"), ("lambda", "GH200", 1, 1.49, None, "us-east-3", "gpu_1x_gh200"),
        ]:
            offers.append({"cloud": cloud, "accelerator": acc, "count": count,
                           "on_demand": {"usd_per_hour": od, "region": region, "instance_type": itype},
                           "spot": {"usd_per_hour": spot, "region": region, "instance_type": itype} if spot else None})
        fetched = (self.now - timedelta(hours=2)).isoformat(timespec="seconds")
        await self.store.set_setting("gpu_catalog", {"offers": offers, "fetched_at": fetched, "clouds": ["aws", "gcp", "lambda"],
                                                     "catalog_updated": {"aws": fetched, "gcp": fetched, "lambda": fetched},
                                                     "skypilot": "0.11.2"})
        sizes = {}
        for m in self.cfg.models:
            if m.kind != "chat" or not m.hf_id:
                continue
            total, active = gs._name_params(m.hf_id)
            if total is None:
                total = {"Qwen/Qwen3-Coder-Next": 80e9, "openai/gpt-oss-20b": 21e9, "deepseek-ai/DeepSeek-V4-Pro": 1.6e12}.get(m.hf_id, 230e9)
            active = active or {"Qwen/Qwen3-Coder-Next": 3e9, "openai/gpt-oss-20b": 3.6e9}.get(m.hf_id, total / 12)
            bits = 4 if "gpt-oss" in m.hf_id else 8 if total > 200e9 else 16
            weight = int(total * bits / 8)
            sizes[m.hf_id] = {"weight_bytes": weight, "bytes_by_bits": {str(b): weight if b == bits else 0 for b in (16, 8, 4)},
                              "params_by_bits": {str(b): int(total) if b == bits else 0 for b in (16, 8, 4)}, "bits": bits,
                              "published_bits": bits, "total_params": int(total), "active_params": int(active),
                              "basis": f"weights from Hugging Face (file sizes), parameters from its name ({gs.BITS_NAME[bits]})"}
        await self.store.set_setting("model_sizes", {"fetched_at": fetched, "version": gs.SIZES_VERSION, "models": sizes})

    # ------------------------------------------------------------------ bills

    async def bills(self) -> None:
        """The providers' own cost data, close to the ledger day by day (as real bills are), with a credit and a line
        that isn't Merit Order's."""
        from collections import defaultdict

        rng = self.rng
        per: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0, 0, 0])
        for r in self.store.ledger:
            if r["backend"] in ("anthropic", "openai", "bedrock") and r["actual_cost"]:
                k = (r["backend"], r["ts"].date(), r["upstream_model"] or r["served_model"], r.get("service_tier") == "batch")
                p = per[k]
                p[0] += r["actual_cost"]
                p[1] += r["input_tokens"]
                p[2] += r["cache_read_tokens"]
                p[3] += r["cache_write_tokens"]
                p[4] += r["output_tokens"]
        source_of = {"anthropic": "anthropic-usage", "openai": "openai-costs", "bedrock": "aws-cur"}
        lines: dict[str, list[dict]] = defaultdict(list)
        today = self.now.date()
        start = min(k[1] for k in per) if per else today
        for (backend, day, model, batch), (cost, i, cr, cw, o) in per.items():
            if (today - day).days < 1:
                continue  # the providers haven't reported today yet
            drift = rng.gauss(1.0, 0.012)
            if backend == "openai" and day == today - timedelta(days=6):
                drift = 1.21  # a day the bill is well over what was recorded: Bills flags it
            src = source_of[backend]
            kinds = [("input", i), ("cache_read", cr), ("cache_write", cw), ("output", o)]
            tokens = sum(n for _, n in kinds) or 1
            for kind, n in kinds:
                if n:
                    lines[src].append({"source": src, "day": day, "line": f"{model} {kind.replace('_', ' ')}" + (" (batch)" if batch else ""),
                                       "model": model, "kind": kind, "deployment": None, "cost": round(cost * drift * n / tokens, 6),
                                       "tokens": n, "detail": {"currency": "USD", **({"tier": "batch"} if batch else {})}})
        for day in (today - timedelta(days=d) for d in range(1, (today - start).days + 1)):
            hours = sum(1 for (h, b) in self.gpu_hours if h.date() == day)
            if hours:
                lines["aws-cur"].append({"source": "aws-cur", "day": day, "line": "EC2 p5.24xlarge spot (gpu-qwen-coder)", "model": None,
                                         "kind": "gpu", "deployment": "gpu-qwen-coder", "cost": round(hours * 6.42 * rng.gauss(1.0, 0.01), 4),
                                         "tokens": None, "detail": {"currency": "USD"}})
        lines["anthropic-usage"].append({"source": "anthropic-usage", "day": today - timedelta(days=12), "line": "Credit: service issue",
                                         "model": None, "kind": "adjustment", "deployment": None, "cost": -25.0, "tokens": None, "detail": {}})
        lines["openai-costs"].append({"source": "openai-costs", "day": today - timedelta(days=3), "line": "gpt-image-2 images", "model": "gpt-image-2",
                                      "kind": "other", "deployment": None, "cost": 3.8, "tokens": None, "detail": {"currency": "USD"}})
        status = {}
        for src, ls in lines.items():
            await self.store.replace_bill_lines(src, start, today, ls)
            status[src] = {"ok": True, "error": None, "fetched_at": (self.now - timedelta(hours=2, minutes=7)).isoformat(),
                           "covered_from": start.isoformat(), "complete_through": (today - timedelta(days=2)).isoformat(), "notes": []}
        status["aws-cur"]["notes"] = ["AWS's Cost and Usage Report runs a day behind; the last two days are partial"]
        await self.store.set_setting("billing_status", status)

    # ------------------------------------------------------------------ evals

    async def evals(self) -> None:
        """Two finished eval runs on the frontier suite and the organization's own codebase suite, scored by the real
        scoring (evals/runner.py), with the scores as latest."""
        from meritorder.evals import jobs
        from meritorder.evals.runner import TaskResult, load_suite, score

        rng = self.rng
        runs = [("frontier", ["claude-opus", "claude-sonnet", "gpt-terra", "gemini-carbon", "kimi-k3.1"], 6, email("alex.morgan")),
                ("codebase", ["claude-sonnet", "claude-haiku", "qwen3-coder-next", "gpt-terra"], 3, email("ana.silva"))]
        latest = None
        for suite_name, models, days_ago, who in runs:
            suite = load_suite(suite_name)
            results = []
            for m in models:
                model = self.cfg.model(m)
                for t in suite["tasks"]:
                    q = model.q(t["task"]) or 2.0
                    p = 1 / (1 + math.exp(-(1.6 * (q - int(t["level"])) + 0.9)))
                    passed = rng.random() < p
                    kind = t.get("kind", "answer")
                    out_tokens = int(rng.lognormvariate(7.5 if kind == "agent" else 6.5, 0.5))
                    in_tokens = int(rng.lognormvariate(10.5 if kind == "agent" else 7.5, 0.5))
                    price = next((r.cost_price() for r in model.routes if r.backend in self.gw.backends and r.price), None)
                    cost = (in_tokens * (price.input if price else 1) + out_tokens * (price.output if price else 5)) / 1e6
                    secs = rng.lognormvariate(4.0 if kind == "agent" else 2.5, 0.5)
                    results.append(TaskResult(
                        model=m, task_id=t["id"], task=t["task"], level=int(t["level"]), passed=passed,
                        reasons=[] if passed else [rng.choice(["2 of 9 hidden tests failed", "wrong final answer", "missed a required step",
                                                               "judges: fail, fail", "timed out after 40 turns"])],
                        cost=round(cost, 6), latency_ms=int(secs * 1000), kind=kind, turns=rng.randint(4, 40) if kind == "agent" else 1,
                        detail="", backend=next((r.backend for r in model.routes if r.backend in self.gw.backends), None),
                        output_tokens=out_tokens, input_tokens=in_tokens, model_ms=int(secs * 850),
                        tool_calls=rng.randint(3, 30) if kind == "agent" else 0, answer="(answers aren't kept in the demo)"))
            scores = score(results)
            run_id = f"ev_{self.uuid().hex[:12]}"
            created = self.now - timedelta(days=days_ago, hours=2)
            await self.store.create_eval_run({
                "id": run_id, "created_at": created, "created_by": who, "team": "platform", "status": "done", "executor": "worker",
                "request": {"models": models, "suite": suite_name, "repeat": 1, "only": None, "judges": ["gpt-sol", "claude-sonnet"],
                            "max_cost_usd": 60.0, "estimate_usd": round(sum(r.cost for r in results) * 1.1, 2), "tasks": len(suite["tasks"])},
                "progress": {"total": len(results), "done": len(results), "passed": sum(r.passed for r in results)},
                "scores": scores, "results": jobs.result_dicts(results), "cost": round(sum(r.cost for r in results), 6),
                "started_at": created + timedelta(minutes=1), "finished_at": created + timedelta(minutes=48),
                "applied_version": 3 if suite_name == "codebase" else None,
            })
            latest = jobs.merge_latest(latest, run_id, suite_name, scores)
            for e in latest["models"].values():
                if e["run_id"] == run_id:
                    e["ran_at"] = (created + timedelta(minutes=48)).isoformat(timespec="seconds")
        await self.store.set_setting("evals", latest)
        await self.store.set_setting("eval_runner", {"sandbox": True, "why": None, "gateway": "http://meritorder:8080",
                                                      "seen_at": (self.now - timedelta(seconds=20)).isoformat()})

    # ------------------------------------------------------------------ config history, the live feed, alerts

    async def history(self) -> None:
        """Configuration versions as people saved them from the console."""
        import copy

        versions = []
        v1 = copy.deepcopy(self.raw)
        for t in v1["teams"].values():
            t.pop("exploration", None)
        v1["models"]["qwen3-coder-next"].pop("quality_override", None)
        versions.append((v1, email("alex.morgan"), "First configuration from the shipped meritorder.yaml", 34))
        v2 = copy.deepcopy(v1)
        for name in ("platform", "mobile", "data-science"):
            v2["teams"][name]["exploration"] = self.raw["teams"][name]["exploration"]
        versions.append((v2, email("ana.silva"), "Edited teams.platform, teams.mobile, teams.data-science: explore levels", 20))
        versions.append((copy.deepcopy(self.raw), email("ana.silva"), "quality_override from eval run on codebase: qwen3-coder-next coding 2.5", 3))
        versions.append((copy.deepcopy(self.raw), email("alex.morgan"), "Controls: org budget, GPU budget", 1))
        configs = self.store.__dict__.setdefault("configs", [])
        for body, who, comment, days in versions:
            configs.append({"id": len(configs) + 1, "created_at": self.now - timedelta(days=days, hours=3), "author": who,
                            "comment": comment, "body": body})
        self.gw.config_version = len(configs)
        # What the outcome report's watcher noticed along the way (benchmark.Watcher), for the Levels trend.
        at = lambda days, hours=0: (self.now - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")  # noqa: E731
        await self.store.set_setting("routing_benchmark", {"events": [
            {"at": at(24, 5), "kind": "scores", "text": "scores changed: gemini-carbon coding 4.2 -> 4.4; claude-sonnet general 3.6 -> 3.5"},
            {"at": at(17, 2), "kind": "catalog", "text": "models added: kimi-k3.1"},
            {"at": at(3, 2), "kind": "eligible", "key": "coding:2", "model": "qwen3-coder-next", "text": "qwen3-coder-next now qualifies for coding:2"},
            {"at": at(3, 2), "kind": "cheapest", "key": "coding:2", "model": "qwen3-coder-next", "was": "deepseek-v4.1-flash",
             "text": "qwen3-coder-next is now the cheapest at coding:2 (was deepseek-v4.1-flash)"},
        ]})

    async def live(self) -> None:
        """What Dispatch reads from the shared state: the feed, backend health and speeds of the last minutes, and alerts."""
        import meritorder.state as state_module

        now = time.time()
        recent = [r for r in self.recent if r["ts"] >= self.now - timedelta(minutes=15)]
        real_time = state_module.time

        class At:  # the state module's clock, set to when each request happened, so health lands in its minute
            t = now

            @classmethod
            def time(cls) -> float:
                return cls.t

        state_module.time = At
        try:
            for r in sorted(recent, key=lambda r: r["ts"]):
                At.t = now - (self.now - r["ts"]).total_seconds()
                self.gw.observe(r)
                await self.gw.drain()
                if r["status"] < 400 and r["backend_kind"] != "cache":
                    self.gw.router.speeds.record(r["backend"], r["upstream_model"], (r["latency_ms"] or 1000) / 1000,
                                                 (r["ttft_ms"] or 300) / 1000, r["output_tokens"], At.t)
        finally:
            state_module.time = real_time
        await self.state.publish_inflight("gw-1", {})
        await self.state.publish_inflight("gw-2", {})
        for ago, level, title, text, page, team in [
            (5 * 3600, "info", "GPU gpu-qwen-coder: provisioning", "GPU tier: gpu-qwen-coder (qwen3-coder-next) api_only -> provisioning: demand "
             "$31.20/h at API prices vs $6.42/h per replica.", "gpus", None),
            (2 * 3600, "info", "New cheapest at coding:2", "qwen3-coder-next on gpu-qwen-coder is now the cheapest model at coding:2.", "models", None),
            (40 * 60, "warning", "Bill over the ledger", "openai-costs billed 21% more than recorded on one day; see Reports → Bills.", "reports", None),
        ]:
            await self.state.push_alert({"ts": datetime.fromtimestamp(now - ago, UTC).isoformat(), "level": level, "title": title, "text": text,
                                         "page": page, "team": team})

    async def post(self, path: str, body: dict) -> dict:
        import httpx

        from meritorder.app import create_app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gateway=self.gw)), base_url="http://demo") as c:
            r = await c.post(path, json=body, headers={"authorization": f"Bearer {ADMIN_KEY}"})
            if r.status_code >= 400:
                raise SystemExit(f"POST {path}: {r.status_code} {r.text[:300]}")
            return r.json()

    # ------------------------------------------------------------------ the answers

    def calls(self) -> list[tuple[str, str, dict, dict | None]]:
        """(method, path, query, body) of every call the console makes, with each value its controls offer."""
        out: list[tuple[str, str, dict, dict | None]] = []

        def get(path: str, **q):
            out.append(("GET", path, {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in q.items()}, None))

        for path in ("/v1/reports/me", "/v1/reports/levels", "/v1/reports/gpu", "/v1/reports/gpu/candidates", "/v1/admin/keys",
                     "/v1/admin/evals", "/v1/admin/evals/suites", "/v1/admin/evals/runs", "/v1/admin/config", "/v1/admin/reporting",
                     "/v1/console/overview", "/v1/console/backends", "/v1/console/teams", "/v1/console/attention",
                     "/v1/admin/compressor", "/v1/admin/config/source", "/v1/admin/config/versions", "/v1/admin/users",
                     "/v1/admin/controls", "/about"):
            get(path)
        get("/v1/reports/requests", limit=200)
        get("/v1/console/feed", limit=40)
        get("/v1/admin/gpu/events", limit=50)
        for minutes in (15, 60, 240):
            get("/v1/console/merit", minutes=minutes)
        for days in (7, 30, 90):
            get("/v1/reports/levels/outcomes", days=days)
        for days in (1, 7, 30):
            get("/v1/reports/router", days=days)
        for start, end in preset_ranges(self.now).values():
            for group in ("day", "model", "team", "user", "backend", "tag:project"):
                get("/v1/reports/summary", start=start, end=end, group_by=group)
            for by in ("day", "month"):
                get("/v1/reports/bills", start=start, end=end, by=by)
            get("/v1/reports/batches", start=start, end=end)
        for weights in ("published", "fp8", "4bit"):
            for spot in (True, False) if weights == "published" else (True,):
                get("/v1/reports/gpu/breakeven", days=7, utilization=0.7, efficiency=0.1, spot=spot, weights=weights)
        for m in self.cfg.models:
            if m.kind == "chat" and m.hf_id:
                get("/v1/reports/gpu/whatif", model=m.name, gpu_usd_per_hour=16, capacity_tokens_per_second=4000, days=30)
        return out

    async def record(self) -> dict:
        import httpx

        from meritorder.app import create_app

        app = create_app(gateway=self.gw)
        answers: dict[str, object] = {}
        headers = {"authorization": f"Bearer {ADMIN_KEY}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as c:
            async def call(method: str, path: str, q: dict, body: dict | None = None):
                r = await c.request(method, path, params=q, json=body, headers=headers)
                if r.status_code >= 400:
                    raise SystemExit(f"{method} {path}?{urlencode(q)}: {r.status_code} {r.text[:300]}")
                key = f"{method} {path}" + (f"?{urlencode(sorted(q.items()))}" if q else "")
                answers[key] = r.json()
                return answers[key]

            for method, path, q, body in self.calls():
                await call(method, path, q, body)
            # Answers that depend on what's listed: every eval suite's tasks, each run, each config version.
            for s in answers["GET /v1/admin/evals/suites"]["suites"]:
                tasks = await call("GET", "/v1/admin/evals/tasks", {"suite": s["name"]})
                for t in tasks["tasks"]:
                    detail = await call("GET", f"/v1/admin/evals/tasks/{t['id']}", {"suite": s["name"]})
                    size = (400, 6) if s["name"] == "codebase" else (1000, 12)  # the codebase suite's tasks carry whole repositories
                    detail.update({k: trim(v, *size) for k, v in detail.items()})
            for run in answers["GET /v1/admin/evals/runs"]["runs"]:
                await call("GET", f"/v1/admin/evals/runs/{run['id']}", {})
            for v in answers["GET /v1/admin/config/versions"]["versions"]:
                await call("GET", f"/v1/admin/config/versions/{v['id']}", {})
            source = answers["GET /v1/admin/config/source"]
            valid = await call("POST", "/v1/admin/config/validate", {}, {"document": source["document"]})
            valid.pop("document", None), valid.pop("yaml", None)  # the demo answers with the document it was sent
            frontier = next(r for r in answers["GET /v1/admin/evals/runs"]["runs"] if r["request"]["suite"] == "frontier")
            await call("POST", "/v1/admin/evals/estimate", {}, {"models": frontier["request"]["models"], "suite": "frontier", "repeat": 1})
            for team in TEAMS:
                body = {"team": team, "settings": self.raw["teams"][team].get("judged_labels") or {"enabled": True}, "days": 14}
                answers[f"POST /v1/admin/judged-labels/estimate?team={team}"] = await call("POST", "/v1/admin/judged-labels/estimate", {}, body)
            answers.pop("POST /v1/admin/judged-labels/estimate")
        return answers


def as_deployed(answers: dict) -> dict:
    """The answers as a deployed gateway gives them: this script keeps everything in memory, a real one in Postgres."""
    def fix(v):
        if isinstance(v, dict):
            return {k: ("postgres" if k == "answered_by" and x == "memory" else False if k == "memory" and x is True else fix(x))
                    for k, x in v.items()}
        return [fix(x) for x in v] if isinstance(v, list) else v

    out = fix(answers)
    out["GET /v1/admin/reporting"].update(store="postgres", note=None, postgres={"ok": True, "error": None})
    out["GET /v1/admin/config/source"]["storage"] = "postgres"
    return out


def rounded(v):
    """Floats to six significant digits: the console shows none of the rest, and the file is half the size."""
    if isinstance(v, float):
        return float(f"{v:.6g}")
    if isinstance(v, list):
        return [rounded(x) for x in v]
    if isinstance(v, dict):
        return {k: rounded(x) for k, x in v.items()}
    return v


def trim(v, limit: int = 1500, items: int = 15):
    """A long text cut short, and a long list or mapping to its first entries, saying so: an eval task's files are the
    real console's to show in full."""
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"\n… ({len(v) - limit:,} more characters in the real console)"
    if isinstance(v, list):
        return [trim(x, limit, items) for x in v[:items]]
    if isinstance(v, dict):
        out = {k: trim(x, limit, items) for k, x in list(v.items())[:items]}
        if len(v) > items:
            out[f"… {len(v) - items} more"] = "in the real console"
        return out
    return v


async def main(seed: int) -> None:
    home = tempfile.mkdtemp(prefix="meritorder-demo-home-")
    os.environ["HOME"] = home  # nothing reads ~/.aws, ~/.sky or a gcloud login
    for name in [k for k in os.environ if k.startswith(("AWS_", "AZURE_", "GOOGLE_", "MERITORDER_", "SKYPILOT_")) or "_KEY" in k]:
        del os.environ[name]  # only the demo's own settings: none of this machine's accounts or profiles
    os.environ.update(DEMO_ENV)
    guard_network()
    sys.path.insert(0, str(ROOT / "src"))
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    demo = Demo(seed, now)
    t0 = time.time()
    await demo.people()
    await demo.traffic()
    await demo.batches()
    await demo.ocr()
    demo.store.ledger.sort(key=lambda r: r["ts"])  # written as it happened: the batches and judged labels came in between
    await demo.store.rollup(full=True)
    for (hour, backend) in sorted(demo.gpu_hours):  # what the controller accrues for each replica-hour
        await demo.store.add_gpu_usage(backend, hour, 3600, 6.42)
    run_hours = (now.timestamp() - (now.timestamp() - 5 * 3600 - 720)) / 3600
    await demo.store.add_gpu_usage("gpu-qwen-coder", now - timedelta(hours=1), run_hours * 2 * 3600, run_hours * 2 * 6.42)
    await demo.budgets()
    await demo.history()
    await demo.gpu_catalog()
    await demo.gpus()
    await demo.bills()
    await demo.evals()
    await demo.live()
    print(f"{len(demo.store.ledger)} requests in {time.time() - t0:.1f}s", file=sys.stderr)
    answers = as_deployed(await demo.record())
    data = {"now": now.isoformat(), "seed": seed, "answers": answers}
    text = json.dumps(rounded(data), separators=(",", ":"), sort_keys=True, default=str)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + "\n")
    sizes = sorted(((len(json.dumps(v, default=str)), k) for k, v in answers.items()), reverse=True)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(answers)} answers, {len(text) / 1e6:.2f} MB; largest:", file=sys.stderr)
    for n, k in sizes[:12]:
        print(f"  {n / 1e3:8.1f} kB  {k}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, default=7)
    asyncio.run(main(ap.parse_args().seed))
