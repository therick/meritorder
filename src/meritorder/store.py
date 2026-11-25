"""Durable storage: users, API keys, the per-request ledger, and report queries.

Postgres in real deployments (ledger partitioned by month, hourly rollups);
an in-memory version for development and tests. The ledger holds metadata
only, never prompt or response content. With `reporting.clickhouse` set, the
worker copies the ledger to ClickHouse and large reports read it from there
(clickhouse.py); Postgres stays the record.
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Sequence

from .batches.store import BATCH_SCHEMA, BATCH_SCHEMA_13, MemoryBatches, PostgresBatches
from .stored.store import STORED_SCHEMA, MemoryStored, PostgresStored

log = logging.getLogger("meritorder.store")

LEDGER_FIELDS = [
    "id", "ts", "user_id", "team", "key_id", "session_id", "tags", "client_format", "requested_model", "floor", "route_reason",
    "served_model", "upstream_model", "backend", "backend_kind", "status", "error", "stream", "upgraded", "proxy_cache",
    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "latency_ms", "ttft_ms",
    "actual_cost", "served_api_cost", "baseline_cost", "list_cost",
    "savings_routing", "savings_caching", "savings_self_hosting", "savings_compression", "savings_compression_model",
    "savings_response_cache",
    "service_tier", "batch_id", "savings_batch", "failed_over", "pages",
]
MIX_FIELDS = ("input", "cache_read", "cache_write", "output")  # the kinds of token a request's price is made of
SUM_FIELDS = [
    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
    "actual_cost", "served_api_cost", "baseline_cost", "list_cost",
    "savings_routing", "savings_caching", "savings_self_hosting", "savings_batch", "savings_compression", "savings_compression_model",
    "savings_response_cache", "pages",
]
# Savings by cause, in the order they're worked out (docs/08-reporting.md); together they make savings_total. compression
# is the rules', compression_model what model-based compression took out on top of them; batch is what a provider's
# batch API price took off its live price (batches/).
SAVINGS_CAUSES = ("routing", "caching", "self_hosting", "batch", "compression", "compression_model", "response_cache")
SAVINGS_FIELDS = [f"savings_{c}" for c in SAVINGS_CAUSES]
GROUPS = {"user": "user_id", "team": "team", "model": "served_model", "backend": "backend", "day": "day", "hour": "hour", "month": "month", "none": None}
_TAG = re.compile(r"^tag:([A-Za-z0-9_.-]{1,64})$")
# The learned router's record of a request (learned.py, proxy.Gateway.route_features): hashed features and named tags,
# never content; the outcome columns are filled in when the client's next request says something about the answer, and
# ``judged`` when the request was sampled for a judged label (judged.py): the judge's verdict, or why there's none.
ROUTE_FEATURE_FIELDS = ["request_id", "ts", "team", "user_id", "session_id", "thread", "fv", "task", "rules_level", "text_features",
                        "tags", "auto", "served_model", "served_score", "refused_scores", "status", "retried", "floor_raised", "learned",
                        "judged", "level", "arm", "arm_from", "propensity", "coding", "predicted", "context", "triage_features"]
ROUTER_MODEL_FIELDS = ("version", "created_at", "created_by", "active", "metrics")


def pack_buckets(buckets) -> bytes:
    """Hashed word buckets (0-65535) as little-endian 16-bit values: half the size of an integer array, and quick to read
    back into a compact array for training."""
    a = array.array("H", buckets)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def unpack_buckets(raw: bytes | None) -> array.array:
    a = array.array("H")
    a.frombytes(raw or b"")
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _route_filter(r: dict, since: datetime, until: datetime | None, fv: str | None, team: str | None) -> bool:
    return r["ts"] >= since and (until is None or r["ts"] < until) and (fv is None or r["fv"] == fv) and (team is None or r["team"] == team)


ROLLUP_AFTER = timedelta(days=7)  # summaries over longer ranges read the hourly rollup (tag breakdowns never do)

# Which store answered a report's ledger queries ("postgres", "clickhouse", "memory"). The reports API starts a set per
# request (reports.AnsweredBy) and says what's in it; stores add to it. Shared by reference, so queries in tasks count too.
_ANSWERED: ContextVar[set | None] = ContextVar("meritorder_answered_by", default=None)


def collect_answers() -> set[str]:
    seen: set[str] = set()
    _ANSWERED.set(seen)
    return seen


def note_answer(kind: str) -> None:
    seen = _ANSWERED.get()
    if seen is not None:
        seen.add(kind)


def answered_by(seen: set[str] | None = None) -> str | None:
    """"postgres", "clickhouse", or both as "clickhouse+postgres"; None when the request read no stored data."""
    seen = _ANSWERED.get() if seen is None else seen
    return "+".join(sorted(seen)) if seen else None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_secret(prefix: str) -> tuple[str, str]:
    """(id, secret) for a console session or an invite link: the secret goes to the browser once and is stored as a hash."""
    return f"{prefix}_{secrets.token_hex(6)}", secrets.token_urlsafe(32)


def new_api_key() -> tuple[str, str]:
    """Returns (key_id, secret). The secret is shown once and stored only as a hash."""
    return f"key_{secrets.token_hex(6)}", f"mo-{secrets.token_urlsafe(32)}"


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def validate_group(group_by: str) -> str:
    if group_by in GROUPS or _TAG.match(group_by):
        return group_by
    raise ValueError(f"group_by must be one of {', '.join(GROUPS)} or tag:<name>")


def complete_row(row: dict) -> dict:
    """Fill what a ledger row left out with the schema's defaults: a new id, now, no tags, zero counts."""
    out = {f: row.get(f) for f in LEDGER_FIELDS}
    out["id"] = out["id"] or uuid.uuid4()
    out["ts"] = out["ts"] or datetime.now(UTC)
    out["tags"] = out["tags"] or {}
    for f in SUM_FIELDS:
        out[f] = out[f] or 0
    for f in ("stream", "upgraded", "proxy_cache"):
        out[f] = bool(out[f])
    return {**row, **out}


BILL_FIELDS = ["source", "day", "line", "model", "kind", "deployment", "cost", "tokens", "detail"]
LEDGER_COST_FIELDS = ["requests", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "actual_cost"]


def merge_bill_lines(source: str, lines: list[dict]) -> list[dict]:
    """One row per (day, line): a provider can report the same line twice (pages, restated rows), and the table's key
    is (source, day, line). Costs and tokens add up."""
    out: dict[tuple, dict] = {}
    for line in lines:
        k = (line["day"], line["line"])
        cur = out.get(k)
        if cur is None:
            out[k] = {**{f: line.get(f) for f in BILL_FIELDS}, "source": source, "cost": float(line.get("cost") or 0),
                      "detail": dict(line.get("detail") or {})}
            continue
        cur["cost"] += float(line.get("cost") or 0)
        if line.get("tokens") is not None:
            cur["tokens"] = (cur.get("tokens") or 0) + int(line["tokens"])
    return list(out.values())


def _finish_row(row: dict) -> dict:
    row["savings_total"] = sum(row.get(f) or 0 for f in SAVINGS_FIELDS)
    for k, v in list(row.items()):
        if isinstance(v, float):
            row[k] = round(v, 6)
        elif isinstance(v, datetime):
            row[k] = v.isoformat()
    return row


def _tag_text(v: Any) -> str:
    """A tag's value as Postgres's ``tags->>'name'`` gives it: a string as it is, anything else as JSON text."""
    return v if isinstance(v, str) else json.dumps(v)


def sort_summary(rows: list[dict]) -> list[dict]:
    """Summary rows by key as text (byte order, not a database's collation), a missing key last: the same order whichever
    store answered."""
    return sorted(rows, key=lambda r: (r["key"] is None, str(r["key"])))


# ---------------------------------------------------------------- the Requests view: filters, sorts and pages

# What the Requests view can sort by, as SQL over the ledger's columns. Time is the (ts, id) primary key, so a page is an
# index range at any size. The others have no index: they're sorted within a range of at most SORT_RANGE (reports.py).
REQUEST_SORTS = {"time": "ts", "cost": "actual_cost", "savings": "(" + " + ".join(SAVINGS_FIELDS) + ")",
                 "latency": "coalesce(latency_ms, -1)"}
SORT_RANGE = timedelta(days=7)
COUNT_EXACT = 10_000  # counts up to this are exact; past it the planner's estimate is given ("about")


@dataclass
class RequestFilter:
    """Which ledger rows the Requests view shows: every field is AND-ed, and None means "any". The reports API sets
    `user_id` and `team` from the caller's role last, so no other filter can widen what they see. `tags` maps a tag name
    to its value, or to None for "has the tag". `level` and `task` read the ledger's `floor` ("coding:3")."""

    start: datetime | None = None
    end: datetime | None = None
    user_id: str | None = None
    team: str | None = None
    key_id: str | None = None
    session_id: str | None = None
    requested_model: str | None = None
    served_model: str | None = None
    backend: str | None = None
    client_format: str | None = None
    status: str | None = None  # "success", "error", a code ("429"), or a class ("5xx")
    task: str | None = None
    level: int | None = None
    reason: str | None = None  # route_reason contains this, ignoring case
    tags: dict[str, str | None] = field(default_factory=dict)
    min_cost: float | None = None
    max_cost: float | None = None
    min_savings: float | None = None
    max_savings: float | None = None
    savings_cause: str | None = None  # one of SAVINGS_CAUSES: rows where it isn't zero
    min_latency: int | None = None
    max_latency: int | None = None
    stream: bool | None = None
    batch: bool | None = None
    cache_hit: bool | None = None  # prompt cache read, or served from the response cache
    failover: bool | None = None  # another route was tried first and failed (ledger rows from migration 19 on)

    def matches(self, r: dict) -> bool:
        """The memory store's twin of `sql()`: the same rows for the same filter."""
        status = r.get("status") or 0
        floor = (r.get("floor") or "").partition(":")
        tags = r.get("tags") or {}
        checks = [
            self.start is None or r["ts"] >= self.start, self.end is None or r["ts"] < self.end,
            *[want is None or r.get(col) == want for col, want in self._equal()],
            self.status is None or _status_matches(self.status, status),
            self.task is None or (bool(r.get("floor")) and floor[0] == self.task),
            self.level is None or (bool(r.get("floor")) and floor[2] == str(self.level)),
            self.reason is None or self.reason.lower() in (r.get("route_reason") or "").lower(),
            all((name in tags) if value is None else tags.get(name) == value for name, value in self.tags.items()),
            self.min_cost is None or (r.get("actual_cost") or 0) >= self.min_cost,
            self.max_cost is None or (r.get("actual_cost") or 0) <= self.max_cost,
            self.min_savings is None or savings_of(r) >= self.min_savings,
            self.max_savings is None or savings_of(r) <= self.max_savings,
            self.savings_cause is None or (r.get(f"savings_{self.savings_cause}") or 0) != 0,
            self.min_latency is None or (r.get("latency_ms") is not None and r["latency_ms"] >= self.min_latency),
            self.max_latency is None or (r.get("latency_ms") is not None and r["latency_ms"] <= self.max_latency),
            self.stream is None or bool(r.get("stream")) == self.stream,
            self.batch is None or (r.get("service_tier") == "batch") == self.batch,
            self.cache_hit is None or ((r.get("cache_read_tokens") or 0) > 0 or r.get("backend_kind") == "cache") == self.cache_hit,
            self.failover is None or bool(r.get("failed_over")) == self.failover,
        ]
        return all(checks)

    def _equal(self) -> list[tuple[str, Any]]:
        return [("user_id", self.user_id), ("team", self.team), ("key_id", self.key_id), ("session_id", self.session_id),
                ("requested_model", self.requested_model), ("served_model", self.served_model), ("backend", self.backend),
                ("client_format", self.client_format)]

    def sql(self, args: list) -> list[str]:
        """WHERE clauses for Postgres, adding their parameters to `args`. Column names are fixed here; every value is a
        parameter."""

        def p(value) -> str:
            args.append(value)
            return f"${len(args)}"

        out = []
        if self.start is not None:
            out.append(f"ts >= {p(self.start)}")
        if self.end is not None:
            out.append(f"ts < {p(self.end)}")
        out += [f"{col} = {p(want)}" for col, want in self._equal() if want is not None]
        if self.status is not None:
            out.append(_status_sql(self.status, p))
        if self.task is not None:
            out.append(f"split_part(floor, ':', 1) = {p(self.task)}")
        if self.level is not None:
            out.append(f"split_part(floor, ':', 2) = {p(str(self.level))}")
        if self.reason is not None:
            escaped = self.reason.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            out.append(f"route_reason ILIKE {p('%' + escaped + '%')}")
        for name, value in self.tags.items():
            out.append(f"tags ? {p(name)}" if value is None else f"tags @> {p({name: value})}::jsonb")
        savings = REQUEST_SORTS["savings"]
        for expr, op, value in (("actual_cost", ">=", self.min_cost), ("actual_cost", "<=", self.max_cost),
                                (savings, ">=", self.min_savings), (savings, "<=", self.max_savings),
                                ("latency_ms", ">=", self.min_latency), ("latency_ms", "<=", self.max_latency)):
            if value is not None:
                out.append(f"{expr} {op} {p(value)}")
        if self.savings_cause is not None:
            out.append(f"savings_{self.savings_cause} <> 0")  # a name from SAVINGS_CAUSES (checked by the API)
        if self.stream is not None:
            out.append("coalesce(stream, false)" if self.stream else "NOT coalesce(stream, false)")
        if self.batch is not None:
            out.append("service_tier = 'batch'" if self.batch else "service_tier IS DISTINCT FROM 'batch'")
        if self.cache_hit is not None:
            hit = "(cache_read_tokens > 0 OR backend_kind = 'cache')"
            out.append(hit if self.cache_hit else f"NOT coalesce({hit}, false)")
        if self.failover is not None:
            out.append("coalesce(failed_over, '') <> ''" if self.failover else "coalesce(failed_over, '') = ''")
        return out


_STATUS = re.compile(r"^(success|error|[1-5]xx|[1-5][0-9]{2})$")


def valid_status(value: str) -> bool:
    return bool(_STATUS.match(value))


def _status_matches(want: str, status: int) -> bool:
    if want == "success":
        return status < 400
    if want == "error":
        return status >= 400
    if want.endswith("xx"):
        return status // 100 == int(want[0])
    return status == int(want)


def _status_sql(want: str, p) -> str:
    """As summaries count errors: a status of 400 or more. A row without a status counts as a success."""
    if want == "success":
        return "coalesce(status, 0) < 400"
    if want == "error":
        return "status >= 400"
    if want.endswith("xx"):
        lo = int(want[0]) * 100
        return f"status >= {p(lo)} AND status < {p(lo + 100)}"
    return f"status = {p(int(want))}"


def savings_of(r: dict) -> float:
    """A row's savings, added in the order Postgres adds them (REQUEST_SORTS["savings"]), so a sort and a cursor agree."""
    total = 0.0
    for f in SAVINGS_FIELDS:
        total += r.get(f) or 0
    return total


def _sort_value(r: dict, sort: str):
    if sort == "time":
        return r["ts"]
    if sort == "cost":
        return r.get("actual_cost") or 0
    if sort == "savings":
        return savings_of(r)
    return r["latency_ms"] if r.get("latency_ms") is not None else -1


def _as_uuid(v) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _ledger_out(r: dict) -> dict:
    return _finish_row({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()})


# ====================================================================== memory


KEEP = object()  # update_user: leave this field as it is (None clears a person's monthly limit)
ANY_VERSION = object()  # save_config: save whatever the latest version is (no base_version check)


class ConfigConflict(Exception):
    """A config save based on a version that's no longer the latest: someone else saved since it was read."""

    def __init__(self, base: int | None, current: int | None) -> None:
        super().__init__(f"the configuration was saved as version {current} since version {base or 'from the file'} was read")
        self.base, self.current = base, current


class MemoryStore(MemoryBatches, MemoryStored):
    kind = "memory"

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}
        self.ledger: list[dict] = []
        MemoryBatches.__init__(self)
        MemoryStored.__init__(self)
        self.clickhouse = None  # never: the copy is made from Postgres
        self.clickhouse_ignored = False

    def use_clickhouse(self, cfg) -> None:
        """`reporting.clickhouse` needs Postgres: the worker copies the ledger from there. In memory it's ignored."""
        if cfg is not None and not self.clickhouse_ignored:
            log.warning("reporting.clickhouse is set, but the ledger is in memory: ClickHouse needs Postgres (MERITORDER_DATABASE_URL)")
        self.clickhouse_ignored = cfg is not None

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def upsert_user(self, user_id: str, team: str, role: str, email: str | None = None, display_name: str | None = None,
                          source: str = "idp") -> None:
        """source="idp": a login refreshes team/role unless an admin pinned them; "ensure": only create if missing."""
        u = self.users.get(user_id)
        if u is None:
            self.users[user_id] = {"id": user_id, "team": team, "role": role, "email": email, "display_name": display_name,
                                   "managed": "idp", "disabled_at": None, "budget_usd_month": None, "created_at": datetime.now(UTC)}
            return
        if source == "idp" and u.get("managed", "idp") == "idp":
            u.update(team=team, role=role)
        if email:
            u["email"] = email
        if display_name:
            u["display_name"] = display_name

    async def get_user(self, user_id: str) -> dict | None:
        u = self.users.get(user_id)
        return dict(u) if u else None

    async def update_user(self, user_id: str, team: str | None = None, role: str | None = None, disabled: bool | None = None,
                          managed: str | None = None, budget_usd_month=KEEP) -> dict | None:
        u = self.users.get(user_id)
        if u is None:
            return None
        if budget_usd_month is not KEEP:
            u["budget_usd_month"] = budget_usd_month
        if team is not None or role is not None:
            u["managed"] = "local"
        if team is not None:
            u["team"] = team
        if role is not None:
            u["role"] = role
        if managed is not None:
            u["managed"] = managed
        if disabled is not None:
            u["disabled_at"] = datetime.now(UTC) if disabled else None
        return dict(u)

    async def find_user_by_email(self, email: str) -> dict | None:
        return next((dict(u) for u in self.users.values() if (u.get("email") or "").lower() == email.lower()), None)

    async def scim_groups(self) -> list[dict]:
        return [dict(g) for g in self.__dict__.setdefault("groups", {}).values()]

    async def scim_group(self, gid: str) -> dict | None:
        g = self.__dict__.setdefault("groups", {}).get(gid)
        return dict(g) if g else None

    async def save_scim_group(self, group: dict) -> None:
        self.__dict__.setdefault("groups", {})[group["id"]] = dict(group)

    async def delete_scim_group(self, gid: str) -> bool:
        return self.__dict__.setdefault("groups", {}).pop(gid, None) is not None

    async def list_users(self, team: str | None = None) -> list[dict]:
        out = []
        for u in self.users.values():
            if team and u["team"] != team:
                continue
            keys = [k for k in self.keys.values() if k["user_id"] == u["id"]]
            out.append({**u, "active_keys": sum(1 for k in keys if not k["revoked_at"]),
                        "last_seen": max((k.get("last_used_at") for k in keys if k.get("last_used_at")), default=None),
                        "has_password": u["id"] in self.__dict__.setdefault("passwords", {}),
                        "two_factor": bool((self.__dict__.setdefault("totp", {}).get(u["id"]) or {}).get("enabled_at"))})
        return out

    async def create_key(self, user_id: str, team: str, role: str, name: str | None = None, tags: dict | None = None,
                         expires_at: datetime | None = None) -> tuple[str, str]:
        key_id, secret = new_api_key()
        self.keys[hash_key(secret)] = {
            "id": key_id, "user_id": user_id, "team": team, "role": role, "name": name, "tags": tags or {},
            "created_at": datetime.now(UTC), "expires_at": expires_at, "revoked_at": None,
        }
        return key_id, secret

    async def find_key(self, key_hash: str) -> dict | None:
        return self.keys.get(key_hash)

    async def list_keys(self, user_id: str | None = None, team: str | None = None) -> list[dict]:
        return [dict(k) for k in self.keys.values() if (user_id is None or k["user_id"] == user_id) and (team is None or k["team"] == team)]

    async def get_key(self, key_id: str) -> dict | None:
        return next((dict(k) for k in self.keys.values() if k["id"] == key_id), None)

    async def revoke_key(self, key_id: str) -> bool:
        for k in self.keys.values():
            if k["id"] == key_id and k["revoked_at"] is None:
                k["revoked_at"] = datetime.now(UTC)
                return True
        return False

    # ----------------------------------------------------------------- console sign-in (decision 69)

    async def set_password(self, user_id: str, password_hash: str, max_role: str | None = None) -> None:
        self.__dict__.setdefault("passwords", {})[user_id] = password_hash
        self.__dict__.setdefault("password_caps", {})[user_id] = max_role

    async def get_password(self, user_id: str) -> str | None:
        return self.__dict__.setdefault("passwords", {}).get(user_id)

    async def get_credential(self, user_id: str) -> dict | None:
        h = self.__dict__.setdefault("passwords", {}).get(user_id)
        if not h:
            return None
        t = self.__dict__.setdefault("totp", {}).get(user_id) or {}
        return {"password_hash": h, "max_role": self.__dict__.setdefault("password_caps", {}).get(user_id),
                "totp_secret": t.get("secret"), "totp_enabled_at": t.get("enabled_at"), "recovery_codes": list(t.get("codes") or [])}

    async def set_totp(self, user_id: str, secret: str | None, enabled: bool, recovery_codes: list[str] | None = None) -> None:
        self.__dict__.setdefault("totp", {})[user_id] = {"secret": secret, "enabled_at": datetime.now(UTC) if enabled else None,
                                                          "codes": list(recovery_codes or [])}

    # The MCP gateway (mcp.py): pinned tool definitions and the tool-call log.

    async def mcp_pins(self, server: str | None = None) -> list[dict]:
        rows = self.__dict__.setdefault("_mcp_tools", {})
        return [dict(r) for (srv, _), r in sorted(rows.items()) if server is None or srv == server]

    async def mcp_pin(self, server: str, name: str, hash_: str, definition: dict, approve: bool) -> dict:
        rows = self.__dict__.setdefault("_mcp_tools", {})
        now = datetime.now(UTC)
        r = rows.get((server, name))
        if r is None:
            r = rows[(server, name)] = {"server": server, "name": name, "hash": hash_, "definition": definition, "first_seen": now,
                                        "approved_hash": hash_ if approve else None,
                                        "approved_definition": definition if approve else None, "changed_at": None,
                                        "approved_by": None, "approved_at": None}
        elif r["hash"] != hash_:
            r.update(hash=hash_, definition=definition, changed_at=now)
        return dict(r)

    async def mcp_approve(self, server: str, name: str, hash_: str, by: str) -> bool:
        r = self.__dict__.setdefault("_mcp_tools", {}).get((server, name))
        if r is None or r["hash"] != hash_:
            return False
        r.update(approved_hash=hash_, approved_definition=r["definition"], approved_by=by, approved_at=datetime.now(UTC))
        return True

    async def mcp_log(self, row: dict) -> None:
        log_ = self.__dict__.setdefault("_mcp_calls", [])
        log_.append({"id": len(log_) + 1, "ts": datetime.now(UTC), "findings": {}, **row})

    async def mcp_calls(self, limit: int = 100, server: str | None = None, team: str | None = None,
                        since: datetime | None = None) -> list[dict]:
        rows = [r for r in self.__dict__.get("_mcp_calls", []) if (server is None or r["server"] == server)
                and (team is None or r.get("team") == team) and (since is None or r["ts"] >= since)]
        return [dict(r) for r in reversed(rows)][:limit]

    async def mcp_call_counts(self, since: datetime, team: str | None = None) -> list[dict]:
        out: dict[tuple, dict] = {}
        for r in await self.mcp_calls(10**9, team=team, since=since):
            k = (r["server"], r["tool"], r["status"])
            o = out.setdefault(k, {"server": k[0], "tool": k[1], "status": k[2], "calls": 0, "ms": 0, "timed": 0})
            o["calls"] += 1
            if r.get("duration_ms") is not None:
                o["ms"] += r["duration_ms"]
                o["timed"] += 1
        return [{**{k: v for k, v in o.items() if k not in ("ms", "timed")},
                 "avg_ms": round(o["ms"] / o["timed"]) if o["timed"] else None} for o in out.values()]

    async def purge_mcp_calls(self, before: datetime) -> int:
        rows = self.__dict__.setdefault("_mcp_calls", [])
        kept = [r for r in rows if r["ts"] >= before]
        self._mcp_calls = kept
        return len(rows) - len(kept)

    async def use_recovery_code(self, user_id: str, code_hash: str) -> bool:
        t = self.__dict__.setdefault("totp", {}).get(user_id) or {}
        if code_hash in (t.get("codes") or []):
            t["codes"].remove(code_hash)
            return True
        return False

    async def create_session(self, user_id: str, method: str, expires_at: datetime, key_hash: str | None = None,
                             user_agent: str | None = None, max_role: str | None = None) -> str:
        sid, token = new_secret("ses")
        self.__dict__.setdefault("sessions", {})[hash_key(token)] = {
            "id": sid, "user_id": user_id, "method": method, "key_hash": key_hash, "user_agent": user_agent, "max_role": max_role,
            "created_at": datetime.now(UTC), "expires_at": expires_at, "revoked_at": None}
        return token

    async def find_session(self, token_hash: str) -> dict | None:
        row = self.__dict__.setdefault("sessions", {}).get(token_hash)
        return dict(row) if row else None

    async def revoke_session(self, token_hash: str) -> bool:
        row = self.__dict__.setdefault("sessions", {}).get(token_hash)
        if row and row["revoked_at"] is None:
            row["revoked_at"] = datetime.now(UTC)
            return True
        return False

    async def revoke_user_sessions(self, user_id: str, keep: str | None = None) -> int:
        n = 0
        for h, row in self.__dict__.setdefault("sessions", {}).items():
            if row["user_id"] == user_id and row["revoked_at"] is None and h != keep:
                row["revoked_at"] = datetime.now(UTC)
                n += 1
        return n

    async def create_invite(self, user_id: str, kind: str, expires_at: datetime, team: str | None = None, role: str | None = None,
                            created_by: str | None = None, creator_role: str | None = None) -> tuple[str, str]:
        iid, token = new_secret("inv")
        self.__dict__.setdefault("invites", {})[hash_key(token)] = {
            "id": iid, "user_id": user_id, "kind": kind, "team": team, "role": role, "created_by": created_by, "creator_role": creator_role,
            "created_at": datetime.now(UTC), "expires_at": expires_at, "used_at": None, "revoked_at": None}
        return iid, token

    async def find_invite(self, token_hash: str) -> dict | None:
        row = self.__dict__.setdefault("invites", {}).get(token_hash)
        return dict(row) if row else None

    async def use_invite(self, token_hash: str) -> bool:
        """Marks it used if it still can be: one use, before it expires, unless revoked. True if this call used it."""
        row = self.__dict__.setdefault("invites", {}).get(token_hash)
        if not row or row["used_at"] or row["revoked_at"] or row["expires_at"] <= datetime.now(UTC):
            return False
        row["used_at"] = datetime.now(UTC)
        return True

    async def list_invites(self, team: str | None = None) -> list[dict]:
        now = datetime.now(UTC)
        rows = [dict(r) for r in self.__dict__.setdefault("invites", {}).values()
                if not r["used_at"] and not r["revoked_at"] and r["expires_at"] > now and (team is None or r["team"] == team)]
        return sorted(rows, key=lambda r: r["created_at"])

    async def revoke_invite(self, invite_id: str) -> dict | None:
        for r in self.__dict__.setdefault("invites", {}).values():
            if r["id"] == invite_id and r["revoked_at"] is None and r["used_at"] is None:
                r["revoked_at"] = datetime.now(UTC)
                return dict(r)
        return None

    async def revoke_user_invites(self, user_id: str) -> None:
        for r in self.__dict__.setdefault("invites", {}).values():
            if r["user_id"] == user_id and r["revoked_at"] is None and r["used_at"] is None:
                r["revoked_at"] = datetime.now(UTC)

    async def record(self, row: dict) -> None:
        self.ledger.append(complete_row(row))

    async def spend_since(self, field: str, value: str, since: datetime) -> float:
        note_answer("memory")
        return sum(r["actual_cost"] for r in self.ledger if r.get(field) == value and r["ts"] >= since)

    async def spend_all_since(self, since: datetime) -> float:
        note_answer("memory")
        return sum(r["actual_cost"] or 0 for r in self.ledger if r["ts"] >= since)

    async def spend_by_tag(self, users: list[str], tag: str, since: datetime) -> dict[str, float]:
        """What these users spent since then, by the value of one tag (judged labels' replays and judging)."""
        note_answer("memory")
        out: dict[str | None, float] = defaultdict(float)
        for r in self.ledger:
            tags = r.get("tags") or {}
            if r.get("user_id") in users and r["ts"] >= since and tag in tags:  # Postgres: tags ? tag, whatever the value
                out[None if tags[tag] is None else _tag_text(tags[tag])] += r["actual_cost"] or 0
        return dict(out)

    async def floor_daily(self, since: datetime, team: str | None = None) -> list[dict]:
        """Requests and spend by day and level (the ledger's floor), and by exploration arm (the explore tag): the outcome
        report's traffic, spend shares and trend (benchmark.py)."""
        note_answer("memory")
        out: dict[tuple, list[float]] = defaultdict(lambda: [0, 0.0])
        for r in self.ledger:
            if r["ts"] >= since and r.get("floor") and (team is None or r.get("team") == team):
                k = (r["ts"].date().isoformat(), r["floor"], (r.get("tags") or {}).get("explore") or "")
                out[k][0] += 1
                out[k][1] += r["actual_cost"] or 0.0
        return [{"day": d, "floor": f, "arm": a, "requests": int(n), "cost": c} for (d, f, a), (n, c) in sorted(out.items())]

    # GPU time (cost of running replicas, accrued by the capacity controller) and the GPU activity log.

    async def add_gpu_usage(self, backend: str, ts: datetime, seconds: float, cost: float) -> None:
        hour = ts.replace(minute=0, second=0, microsecond=0)
        u = self.__dict__.setdefault("_gpu_usage", {}).setdefault((hour, backend), [0.0, 0.0])
        u[0] += seconds
        u[1] += cost

    async def gpu_spend_since(self, since: datetime) -> dict[str, float]:
        note_answer("memory")
        out: dict[str, float] = {}
        for (hour, backend), (_, cost) in self.__dict__.get("_gpu_usage", {}).items():
            if hour >= since.replace(minute=0, second=0, microsecond=0):
                out[backend] = out.get(backend, 0.0) + cost
        return out

    async def gpu_daily(self, since: datetime) -> list[dict]:
        note_answer("memory")
        days: dict = {}
        for (hour, backend), (secs, cost) in self.__dict__.get("_gpu_usage", {}).items():
            if hour >= since:
                d = days.setdefault((hour.date().isoformat(), backend), [0.0, 0.0])
                d[0] += secs / 3600
                d[1] += cost
        return [{"day": k[0], "backend": k[1], "replica_hours": v[0], "cost": v[1]} for k, v in sorted(days.items())]

    async def gpu_hourly(self, start: datetime, end: datetime) -> list[dict]:
        """What GPU time cost by hour and backend, for the hours from the one ``start`` falls in up to ``end``. The hour is
        the unit GPU time is kept in, so a window starting inside an hour counts that whole hour (as gpu_spend_since)."""
        note_answer("memory")
        lo = start.replace(minute=0, second=0, microsecond=0)
        return [{"hour": hour, "backend": backend, "replica_seconds": secs, "cost": cost}
                for (hour, backend), (secs, cost) in sorted(self.__dict__.get("_gpu_usage", {}).items()) if lo <= hour < end]

    async def gpu_ledger(self, start: datetime, end: datetime, backends: Sequence[str], raw: bool = False) -> list[dict]:
        """The ledger's rows on these backends by hour: how many, what they were charged (their shares of GPU time), and
        what the same tokens cost through an API. spend.py sets the shares against the GPU time the controller accrued."""
        note_answer("memory")
        out: dict[tuple, dict] = {}
        names = set(backends)
        for r in self.ledger:
            if start <= r["ts"] < end and r.get("backend") in names:
                hour = r["ts"].astimezone(UTC).replace(minute=0, second=0, microsecond=0)
                g = out.setdefault((hour, r["backend"]), {"hour": hour, "backend": r["backend"], "requests": 0, "actual_cost": 0.0,
                                                          "served_api_cost": 0.0})
                g["requests"] += 1
                g["actual_cost"] += r.get("actual_cost") or 0
                g["served_api_cost"] += r.get("served_api_cost") or 0
        return [out[k] for k in sorted(out)]

    async def gpu_event(self, kind: str, backend: str | None = None, actor: str | None = None, detail: dict | None = None) -> None:
        ev = self.__dict__.setdefault("_gpu_events", [])
        ev.append({"id": len(ev) + 1, "ts": datetime.now(UTC), "backend": backend, "kind": kind, "actor": actor, "detail": detail or {}})

    async def gpu_events(self, limit: int = 100, backend: str | None = None) -> list[dict]:
        ev = [e for e in self.__dict__.get("_gpu_events", []) if backend is None or e["backend"] == backend]
        return list(reversed(ev))[:limit]

    # Providers' own cost data (billing.py): one row per (source, day, line), replaced on every fetch of a range.

    async def replace_bill_lines(self, source: str, start: date, end: date, lines: list[dict]) -> int:
        kept = [r for r in self.__dict__.setdefault("_bills", []) if not (r["source"] == source and start <= r["day"] < end)]
        rows = merge_bill_lines(source, lines)
        self._bills = kept + rows
        return len(rows)

    async def bill_lines(self, start: date, end: date, source: str | None = None) -> list[dict]:
        note_answer("memory")
        rows = [dict(r) for r in self.__dict__.get("_bills", []) if start <= r["day"] < end and (source is None or r["source"] == source)]
        return sorted(rows, key=lambda r: (r["day"], r["source"], r["line"]))

    async def ledger_costs(self, start: datetime, end: datetime, backends: list[str]) -> list[dict]:
        """Recorded cost and tokens by UTC day, backend, model and upstream model id, for comparing with bills."""
        note_answer("memory")
        groups: dict[tuple, dict] = {}
        for r in self.ledger:
            if not (start <= r["ts"] < end) or r.get("backend") not in backends:
                continue
            k = (r["ts"].astimezone(UTC).date(), r.get("backend"), r.get("served_model") or "", r.get("upstream_model") or "",
                 r.get("service_tier") or "")
            g = groups.setdefault(k, {"day": k[0], "backend": k[1], "served_model": k[2], "upstream_model": k[3], "service_tier": k[4],
                                      **dict.fromkeys(LEDGER_COST_FIELDS, 0)})
            g["requests"] += 1
            for f in LEDGER_COST_FIELDS[1:]:
                g[f] += r.get(f) or 0
        return list(groups.values())

    def _key(self, r: dict, group_by: str) -> Any:
        """The group a row falls in, as PostgresStore.summary answers it: a tag's value as text (a tag that's there with
        an empty value is its own group), and a day, hour or month as the UTC timestamp it starts at."""
        m = _TAG.match(group_by)
        if m:
            tags = r.get("tags") or {}
            return _tag_text(tags[m.group(1)]) if tags.get(m.group(1)) is not None else "(untagged)"
        if group_by == "none":
            return "total"
        if group_by in ("day", "hour", "month"):
            ts = r["ts"].astimezone(UTC).replace(minute=0, second=0, microsecond=0)
            if group_by in ("day", "month"):
                ts = ts.replace(hour=0)
            if group_by == "month":
                ts = ts.replace(day=1)
            return ts.isoformat()
        return r.get(GROUPS[group_by])

    async def summary(self, start: datetime, end: datetime, group_by: str, user_id: str | None = None,
                      team: str | None = None, gpu_backends: Sequence[str] = ()) -> list[dict]:
        """Ledger sums by ``group_by``. ``gpu_backends``: the self-hosted backends, whose rows' cost is also summed as
        ``gpu_cost`` (those requests' shares of GPU time; spend.py adds the GPU time no request covered)."""
        validate_group(group_by)
        note_answer("memory")
        gpus = set(gpu_backends)
        groups: dict[Any, dict] = defaultdict(lambda: {"requests": 0, "errors": 0, "upgraded": 0, **{f: 0 for f in SUM_FIELDS}, "gpu_cost": 0.0})
        for r in self.ledger:
            if not (start <= r["ts"] < end):
                continue
            if user_id and r.get("user_id") != user_id:
                continue
            if team and r.get("team") != team:
                continue
            g = groups[self._key(r, group_by)]
            g["requests"] += 1
            g["errors"] += 1 if (r.get("status") or 0) >= 400 else 0
            g["upgraded"] += 1 if r.get("upgraded") else 0
            for f in SUM_FIELDS:
                g[f] += r.get(f) or 0
            if r.get("backend") in gpus:
                g["gpu_cost"] += r.get("actual_cost") or 0
        rows = [_finish_row({"key": k, **v}) for k, v in groups.items()]
        return sort_summary(rows)

    async def recent(self, limit: int, user_id: str | None = None, team: str | None = None) -> list[dict]:
        note_answer("memory")
        rows = [r for r in self.ledger if (not user_id or r.get("user_id") == user_id) and (not team or r.get("team") == team)]
        # Newest first by time, then id, as Postgres orders them: a row written late (a batch settled, a retried write) sits
        # where its time puts it, not where it was appended.
        rows = sorted(rows, key=lambda r: (r["ts"], _as_uuid(r["id"])), reverse=True)[:limit]
        return [_finish_row({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()}) for r in rows]

    def _requests(self, f: RequestFilter, sort: str, desc: bool) -> list[dict]:
        rows = [r for r in self.ledger if f.matches(r)]
        return sorted(rows, key=lambda r: (_sort_value(r, sort), r["ts"], _as_uuid(r["id"])), reverse=desc)

    async def requests_page(self, f: RequestFilter, sort: str = "time", desc: bool = True, cursor: tuple | None = None,
                            forward: bool = True, limit: int = 50) -> list[dict]:
        """A page of the Requests view in display order, each row with its `_sort` value (PostgresStore.requests_page)."""
        note_answer("memory")
        rows = self._requests(f, sort, desc)
        if cursor is not None:
            c = (cursor[0], cursor[1], _as_uuid(cursor[2]))
            key = lambda r: (_sort_value(r, sort), r["ts"], _as_uuid(r["id"]))  # noqa: E731
            later = (lambda r: key(r) < c) if desc else (lambda r: key(r) > c)
            if forward:
                rows = [r for r in rows if later(r)][:limit]
            else:
                rows = [r for r in rows if not later(r) and key(r) != c][-limit:]
        else:
            rows = rows[:limit]
        return [{**_ledger_out(dict(r)), "_sort": _sort_value(r, sort)} for r in rows]

    async def requests_count(self, f: RequestFilter, cap: int = COUNT_EXACT) -> tuple[int, bool]:
        note_answer("memory")
        return sum(1 for r in self.ledger if f.matches(r)), True

    async def requests_rows(self, f: RequestFilter, sort: str = "time", desc: bool = True):
        """Every row the filter matches, in the view's order: the Requests view's CSV."""
        note_answer("memory")
        for r in self._requests(f, sort, desc):
            yield {k: r.get(k) for k in LEDGER_FIELDS}

    async def needed_demand(self, model: str, since: datetime) -> float:
        return sum(r["served_api_cost"] for r in self.ledger if r.get("served_model") == model and not r.get("upgraded") and r["ts"] >= since
                   and r.get("service_tier") != "batch")

    async def demand_by_hour(self, since: datetime) -> list[dict]:
        note_answer("memory")
        groups: dict[tuple, dict] = {}
        for r in self.ledger:
            if r["ts"] < since or r.get("upgraded") or (r.get("status") or 0) >= 400 or r.get("service_tier") == "batch":
                continue
            hour = r["ts"].replace(minute=0, second=0, microsecond=0)
            k = (hour, r.get("floor"), r.get("served_model"), r.get("requested_model"))
            g = groups.setdefault(k, {"hour": hour, "floor": k[1], "served_model": k[2], "requested_model": k[3], "cost": 0.0, "tokens": 0,
                                      "requests": 0, **dict.fromkeys(MIX_FIELDS, 0)})
            g["cost"] += r.get("served_api_cost") or 0
            g["tokens"] += sum(r.get(f"{f}_tokens") or 0 for f in MIX_FIELDS)
            g["requests"] += 1
            for f in MIX_FIELDS:
                g[f] += r.get(f"{f}_tokens") or 0
        return list(groups.values())

    async def export_rows(self, start: datetime, end: datetime, team: str | None = None):
        note_answer("memory")
        for r in sorted(self.ledger, key=lambda x: x["ts"]):
            if start <= r["ts"] < end and (team is None or r.get("team") == team):
                yield {k: r.get(k) for k in LEDGER_FIELDS}

    async def demand_rows(self, since: datetime) -> list[dict]:
        """Spend and tokens since ``since`` grouped by (floor, served_model, requested_model), excluding free upgrades."""
        groups: dict[tuple, dict] = {}
        for r in self.ledger:
            if r["ts"] < since or r.get("upgraded") or (r.get("status") or 0) >= 400 or r.get("service_tier") == "batch":
                continue
            k = (r.get("floor"), r.get("served_model"), r.get("requested_model"))
            g = groups.setdefault(k, {"floor": k[0], "served_model": k[1], "requested_model": k[2], "cost": 0.0, "tokens": 0})
            g["cost"] += r.get("served_api_cost") or 0  # API-equivalent: stable whether or not GPUs serve it
            g["tokens"] += sum(r.get(f) or 0 for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
        return list(groups.values())

    async def rollup(self, full: bool = False) -> int:
        return 0

    # --- config versions (the in-memory store keeps them for the life of the process)
    async def save_config(self, body: dict, author: str, comment: str | None, base_version: object = ANY_VERSION) -> int:
        """``base_version``: the version the change was made to (None: the file, before any was saved). Another latest
        version raises ConfigConflict, so two editors can't silently overwrite each other's change."""
        self.__dict__.setdefault("configs", [])
        latest = self.configs[-1]["id"] if self.configs else None
        if base_version is not ANY_VERSION and base_version != latest:
            raise ConfigConflict(base_version, latest)  # type: ignore[arg-type]
        vid = len(self.configs) + 1
        self.configs.append({"id": vid, "created_at": datetime.now(UTC), "author": author, "comment": comment, "body": body})
        return vid

    async def active_config(self) -> dict | None:
        return (self.__dict__.get("configs") or [None])[-1]

    async def config_versions(self, limit: int = 50) -> list[dict]:
        return [{k: v for k, v in c.items() if k != "body"} for c in reversed(self.__dict__.get("configs") or [])][:limit]

    async def get_config(self, vid: int) -> dict | None:
        return next((c for c in self.__dict__.get("configs") or [] if c["id"] == vid), None)

    async def get_setting(self, key: str):
        return self.__dict__.setdefault("settings", {}).get(key)

    async def set_setting(self, key: str, value) -> None:
        self.__dict__.setdefault("settings", {})[key] = value

    async def add_setting(self, key: str, value) -> None:
        """Set it only if it isn't set yet (several gateways may try at once: the first one wins)."""
        self.__dict__.setdefault("settings", {}).setdefault(key, value)

    async def patch_setting(self, key: str, patch: dict) -> None:
        """Merge these top-level fields into a setting that's a mapping (creating it)."""
        settings = self.__dict__.setdefault("settings", {})
        settings[key] = {**(settings.get(key) or {}), **patch}

    async def drop_setting_entry(self, key: str, section: str, name: str, expect=None) -> bool:
        """Remove ``value[section][name]`` from a setting that's a mapping, leaving every other field as it is now, and only
        while it still equals ``expect`` (when given). True when it was removed."""
        settings = self.__dict__.setdefault("settings", {})
        inner = (settings.get(key) or {}).get(section)
        if not isinstance(inner, dict) or name not in inner or (expect is not None and inner[name] != expect):
            return False
        settings[key] = {**settings[key], section: {k: v for k, v in inner.items() if k != name}}
        return True

    # The learned router: request features and outcomes, and trained models (learned.py, learned_jobs.py).
    async def record_route_features(self, row: dict) -> None:
        rows = self.__dict__.setdefault("_route_features", {})
        full = {f: row.get(f) for f in ROUTE_FEATURE_FIELDS}
        full.update(retried=bool(row.get("retried")), refused_scores=list(row.get("refused_scores") or []))
        rows[str(row["request_id"])] = full

    async def mark_route_outcome(self, request_id: str, retried: bool = False, floor_raised: int | None = None) -> bool:
        row = self.__dict__.setdefault("_route_features", {}).get(str(request_id))
        if row is None:
            return False
        row["retried"] = row["retried"] or retried
        if floor_raised is not None:
            row["floor_raised"] = max(row["floor_raised"] or 0, floor_raised)
        return True

    async def mark_coding(self, request_id: str, coding: dict) -> bool:
        """What a task's tool results showed (outcomes.py): counts and flags, replacing what was there."""
        row = self.__dict__.setdefault("_route_features", {}).get(str(request_id))
        if row is None:
            return False
        row["coding"] = dict(coding)
        return True

    async def mark_judged(self, request_id: str, judged: dict) -> bool:
        """A sampled request's judged label (judged.py): the verdict, or why there's none."""
        row = self.__dict__.setdefault("_route_features", {}).get(str(request_id))
        if row is None:
            return False
        row["judged"] = dict(judged)
        return True

    async def judged_traffic(self, team: str, since: datetime) -> list[dict]:
        """A team's recorded task starts since then, with what each sent and got (the ledger's token counts): what the
        judged labels' estimate prices (judged.estimate)."""
        note_answer("memory")
        ledger = {str(r["id"]): r for r in self.ledger if r["ts"] >= since - timedelta(days=1) and r.get("team") == team}
        out = []
        for f in self.__dict__.get("_route_features", {}).values():
            r = ledger.get(str(f["request_id"]))
            if f["team"] != team or f["ts"] < since or r is None:
                continue
            out.append({"task": f["task"], "rules_level": f["rules_level"], "tags": list(f["tags"] or []), "served_model": f["served_model"],
                        "status": f["status"], "prompt_tokens": (r.get("input_tokens") or 0) + (r.get("cache_read_tokens") or 0)
                        + (r.get("cache_write_tokens") or 0), "output_tokens": r.get("output_tokens") or 0})
        return out

    async def route_features(self, since: datetime, until: datetime | None = None, fv: str | None = None, team: str | None = None,
                             text: bool = True, limit: int | None = None) -> list[dict]:
        """Rows oldest first; ``limit``: only the newest that many. ``text=False`` leaves out the hashed words."""
        note_answer("memory")
        rows = sorted((dict(r) for r in self.__dict__.setdefault("_route_features", {}).values() if _route_filter(r, since, until, fv, team)),
                      key=lambda r: r["ts"])
        rows = rows[-limit:] if limit else rows
        if not text:
            for r in rows:
                r.pop("text_features")
                r.pop("triage_features", None)
        return rows

    async def prune_route_features(self, before: datetime) -> int:
        rows = self.__dict__.setdefault("_route_features", {})
        old = [k for k, r in rows.items() if r["ts"] < before]
        for k in old:
            del rows[k]
        return len(old)

    async def save_router_model(self, doc: dict, metrics: dict, author: str, active: bool) -> int:
        models = self.__dict__.setdefault("_router_models", [])
        version = len(models) + 1
        if active:
            for m in models:
                m["active"] = False
        models.append({"version": version, "created_at": datetime.now(UTC), "created_by": author, "active": active, "doc": doc,
                       "metrics": metrics})
        return version

    async def router_model(self, version: int) -> dict | None:
        return next((dict(m) for m in self.__dict__.get("_router_models", []) if m["version"] == version), None)

    async def router_model_version(self) -> int | None:
        """The model gateways use: the active one, or None."""
        return next((m["version"] for m in self.__dict__.get("_router_models", []) if m["active"]), None)

    async def router_models(self, limit: int = 20) -> list[dict]:
        rows = sorted(self.__dict__.get("_router_models", []), key=lambda m: m["version"], reverse=True)[:limit]
        return [{k: m[k] for k in ROUTER_MODEL_FIELDS} for m in rows]

    async def activate_router_model(self, version: int | None) -> bool:
        """Make this version the one gateways use (None: none). False when there's no such version."""
        models = self.__dict__.setdefault("_router_models", [])
        if version is not None and not any(m["version"] == version for m in models):
            return False
        for m in models:
            m["active"] = m["version"] == version
        return True

    # Eval runs: queued from the console, run by the worker (or by `meritorder eval`), results kept here.
    async def create_eval_run(self, run: dict) -> dict:
        row = {"created_at": datetime.now(UTC), "progress": {}, "scores": None, "results": None, "error": None, "cost": 0.0,
               "started_at": None, "finished_at": None, "applied_version": None, **run}
        self.__dict__.setdefault("eval_runs", {})[row["id"]] = row
        return dict(row)

    async def get_eval_run(self, run_id: str) -> dict | None:
        row = self.__dict__.setdefault("eval_runs", {}).get(run_id)
        return dict(row) if row else None

    async def list_eval_runs(self, limit: int | None = 50) -> list[dict]:
        rows = sorted(self.__dict__.setdefault("eval_runs", {}).values(), key=lambda r: r["created_at"], reverse=True)[:limit]
        return [{k: v for k, v in r.items() if k != "results"} for r in rows]

    async def update_eval_run(self, run_id: str, **fields) -> dict | None:
        row = self.__dict__.setdefault("eval_runs", {}).get(run_id)
        if not row:
            return None
        row.update({k: v for k, v in fields.items() if k in EVAL_RUN_FIELDS})
        return dict(row)

    async def claim_eval_run(self) -> dict | None:
        """The oldest queued run for the worker, marked running."""
        queued = sorted((r for r in self.__dict__.setdefault("eval_runs", {}).values() if r["status"] == "queued" and r["executor"] == "worker"),
                        key=lambda r: r["created_at"])
        if not queued:
            return None
        queued[0].update(status="running", started_at=datetime.now(UTC))
        return dict(queued[0])

    async def ensure_partitions(self, months_ahead: int = 2) -> None:
        return None


# ====================================================================== postgres

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    email text,
    display_name text,
    team text NOT NULL,
    role text NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_keys (
    id text PRIMARY KEY,
    key_hash text UNIQUE NOT NULL,
    user_id text NOT NULL,
    team text NOT NULL,
    role text NOT NULL,
    name text,
    tags jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz
);
CREATE TABLE IF NOT EXISTS requests (
    id uuid NOT NULL,
    ts timestamptz NOT NULL,
    user_id text,
    team text,
    key_id text,
    session_id text,
    tags jsonb NOT NULL DEFAULT '{}',
    client_format text,
    requested_model text,
    floor text,
    served_model text,
    upstream_model text,
    backend text,
    backend_kind text,
    status int,
    error text,
    stream boolean,
    upgraded boolean,
    proxy_cache boolean,
    input_tokens bigint NOT NULL DEFAULT 0,
    cache_read_tokens bigint NOT NULL DEFAULT 0,
    cache_write_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    latency_ms int,
    ttft_ms int,
    actual_cost double precision NOT NULL DEFAULT 0,
    served_api_cost double precision NOT NULL DEFAULT 0,
    baseline_cost double precision NOT NULL DEFAULT 0,
    list_cost double precision NOT NULL DEFAULT 0,
    savings_routing double precision NOT NULL DEFAULT 0,
    savings_caching double precision NOT NULL DEFAULT 0,
    savings_self_hosting double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, id)
) PARTITION BY RANGE (ts);
CREATE TABLE IF NOT EXISTS requests_default PARTITION OF requests DEFAULT;
CREATE INDEX IF NOT EXISTS requests_user_ts ON requests (user_id, ts);
CREATE INDEX IF NOT EXISTS requests_team_ts ON requests (team, ts);
CREATE INDEX IF NOT EXISTS requests_model_ts ON requests (served_model, ts);
CREATE TABLE IF NOT EXISTS usage_hourly (
    hour timestamptz NOT NULL,
    user_id text NOT NULL,
    team text NOT NULL,
    served_model text NOT NULL,
    backend text NOT NULL,
    requests bigint NOT NULL,
    errors bigint NOT NULL,
    upgraded bigint NOT NULL,
    input_tokens bigint NOT NULL,
    cache_read_tokens bigint NOT NULL,
    cache_write_tokens bigint NOT NULL,
    output_tokens bigint NOT NULL,
    actual_cost double precision NOT NULL,
    served_api_cost double precision NOT NULL,
    baseline_cost double precision NOT NULL,
    list_cost double precision NOT NULL,
    savings_routing double precision NOT NULL,
    savings_caching double precision NOT NULL,
    savings_self_hosting double precision NOT NULL,
    PRIMARY KEY (hour, user_id, team, served_model, backend)
);
"""
SCHEMA_V2 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS savings_compression double precision NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS savings_response_cache double precision NOT NULL DEFAULT 0;
ALTER TABLE usage_hourly ADD COLUMN IF NOT EXISTS savings_compression double precision NOT NULL DEFAULT 0;
ALTER TABLE usage_hourly ADD COLUMN IF NOT EXISTS savings_response_cache double precision NOT NULL DEFAULT 0;
"""
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS config_versions (
    id serial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    author text NOT NULL,
    comment text,
    body jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""
SCHEMA_V4 = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS managed text NOT NULL DEFAULT 'idp';
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
"""
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS scim_groups (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    external_id text,
    members jsonb NOT NULL DEFAULT '[]',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_email ON users (lower(email));
"""
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS gpu_usage (
    hour timestamptz NOT NULL,
    backend text NOT NULL,
    replica_seconds double precision NOT NULL DEFAULT 0,
    cost double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, backend)
);
CREATE TABLE IF NOT EXISTS gpu_events (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    backend text,
    kind text NOT NULL,
    actor text,
    detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS gpu_events_ts ON gpu_events (ts DESC);
"""
SCHEMA_V7 = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS budget_usd_month double precision CHECK (budget_usd_month >= 0);
"""
SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    team text NOT NULL,
    status text NOT NULL,
    executor text NOT NULL,
    request jsonb NOT NULL,
    progress jsonb NOT NULL DEFAULT '{}',
    scores jsonb,
    results jsonb,
    error text,
    cost double precision NOT NULL DEFAULT 0,
    started_at timestamptz,
    finished_at timestamptz,
    applied_version integer
);
CREATE INDEX IF NOT EXISTS eval_runs_created ON eval_runs (created_at DESC);
"""
SCHEMA_V9 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS route_reason text;
"""
SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS bill_lines (
    source text NOT NULL,
    day date NOT NULL,
    line text NOT NULL,
    model text,
    kind text NOT NULL,
    deployment text,
    cost double precision NOT NULL DEFAULT 0,
    tokens bigint,
    detail jsonb NOT NULL DEFAULT '{}',
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, day, line)
);
CREATE INDEX IF NOT EXISTS bill_lines_day ON bill_lines (day);
"""
# Batches (batches/): the ledger marks rows that went through a provider's batch API and the discount they got; the
# tables hold each batch's metadata, its provider jobs and one row a request (never content), and uploaded input files
# (encrypted, until their batch is handed to the providers).
SCHEMA_V11 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS service_tier text;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS batch_id text;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS savings_batch double precision NOT NULL DEFAULT 0;
ALTER TABLE usage_hourly ADD COLUMN IF NOT EXISTS savings_batch double precision NOT NULL DEFAULT 0;
""" + BATCH_SCHEMA
# The learned router (decision 44): what it knew about each request it saw and what followed, and its trained models. One
# row per task start: hashed word buckets and named tags, never content.
SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS route_features (
    request_id uuid PRIMARY KEY,
    ts timestamptz NOT NULL,
    team text,
    user_id text,
    session_id text,
    thread text,
    fv text NOT NULL,
    task text NOT NULL,
    rules_level smallint NOT NULL,
    text_features bytea NOT NULL DEFAULT ''::bytea,
    tags text[] NOT NULL DEFAULT '{}',
    auto boolean NOT NULL DEFAULT true,
    served_model text,
    served_score double precision,
    refused_scores double precision[] NOT NULL DEFAULT '{}',
    status integer,
    retried boolean NOT NULL DEFAULT false,
    floor_raised smallint,
    learned jsonb
);
CREATE INDEX IF NOT EXISTS route_features_ts ON route_features (ts);
CREATE TABLE IF NOT EXISTS router_models (
    version serial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    active boolean NOT NULL DEFAULT false,
    doc jsonb NOT NULL,
    metrics jsonb NOT NULL DEFAULT '{}'
);
"""
SCHEMA_V13 = BATCH_SCHEMA_13  # batches: uploads in the operator's bucket, key rotation, batch work on GPUs (decision 49)
# The Responses API's stored mode (stored/, decision 51): responses, conversations and their items, each one's content
# sealed (AES-GCM); owner, chain, status, where it ran and sizes in the clear.
SCHEMA_V14 = STORED_SCHEMA
SCHEMA_V15 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS savings_compression_model double precision NOT NULL DEFAULT 0;
ALTER TABLE usage_hourly ADD COLUMN IF NOT EXISTS savings_compression_model double precision NOT NULL DEFAULT 0;
"""
# Judged labels (decision 52): a sampled request's verdict, next to its hashed features. Never content: the prompt and
# both answers live only in the gateway's memory while the replay and the judge run.
SCHEMA_V16 = """
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS judged jsonb;
"""
# Your own traffic as the routing benchmark (decision 59): the level a task start was routed at, its exploration arm (the
# level it would have had, and the chance of the arm it drew, for weighting), and what its tool results showed (counts
# and flags from outcomes.py, never content).
SCHEMA_V18 = """
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS level smallint;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS arm text;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS arm_from smallint;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS propensity double precision;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS coding jsonb;
"""
# The Requests view (docs/08-reporting.md): which routes failed before the one that served a request (as the
# x-meritorder-failed-over header says), and indexes for its key and session filters. Time with team, user or served
# model already has one (migration 1); a page by time is a range of the (ts, id) primary key. On a large ledger the two
# indexes take a while to build, and writes to the ledger wait meanwhile: run `meritorder migrate` in a quiet hour.
SCHEMA_V19 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS failed_over text;
CREATE INDEX IF NOT EXISTS requests_key_ts ON requests (key_id, ts);
CREATE INDEX IF NOT EXISTS requests_session_ts ON requests (session_id, ts);
"""
# Expected total cost (decision 62): what the success predictor said at the task's start (the chosen model's and the
# runner-up's chance and expected cost), the task's repository and project as keyed hashes, and the opening prompt's
# triage buckets (hashed word triples and path parts). Never content.
SCHEMA_V20 = """
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS predicted jsonb;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS context jsonb;
ALTER TABLE route_features ADD COLUMN IF NOT EXISTS triage_features bytea NOT NULL DEFAULT ''::bytea;
"""
# Console sign-in (decision 69): local passwords (scrypt hashes), browser sessions and invite links, each secret stored
# only as a hash. A session either stands for its person (password or SSO sign-in) or for the key it was opened with.
SCHEMA_V21 = """
CREATE TABLE IF NOT EXISTS local_credentials (
    user_id text PRIMARY KEY,
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS console_sessions (
    id text PRIMARY KEY,
    token_hash text UNIQUE NOT NULL,
    user_id text NOT NULL,
    method text NOT NULL,
    key_hash text,
    user_agent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS console_sessions_user ON console_sessions (user_id);
CREATE TABLE IF NOT EXISTS invites (
    id text PRIMARY KEY,
    token_hash text UNIQUE NOT NULL,
    user_id text NOT NULL,
    kind text NOT NULL,
    team text,
    role text,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    revoked_at timestamptz
);
"""
# Console sign-in hardening (decision 69): who made a link (a team lead's links only set a password capped at the user
# role), that cap on the password and on sessions it opens.
SCHEMA_V22 = """
ALTER TABLE invites ADD COLUMN IF NOT EXISTS creator_role text;
ALTER TABLE local_credentials ADD COLUMN IF NOT EXISTS max_role text;
ALTER TABLE console_sessions ADD COLUMN IF NOT EXISTS max_role text;
"""
# Two-factor sign-in for local accounts (decision 73): the authenticator's secret (sealed when MERITORDER_AUTH_KEY is
# set), when it was turned on, and the recovery codes' hashes.
SCHEMA_V23 = """
ALTER TABLE local_credentials ADD COLUMN IF NOT EXISTS totp_secret text;
ALTER TABLE local_credentials ADD COLUMN IF NOT EXISTS totp_enabled_at timestamptz;
ALTER TABLE local_credentials ADD COLUMN IF NOT EXISTS recovery_codes jsonb NOT NULL DEFAULT '[]';
"""
# The MCP gateway (decision 75): each server's tool definitions as pinned (the hash an admin approved, and the one last
# seen), and the tool-call log (who called what, how it went; never the arguments or results).
SCHEMA_V24 = """
CREATE TABLE IF NOT EXISTS mcp_tools (
    server text NOT NULL,
    name text NOT NULL,
    hash text NOT NULL,
    approved_hash text,
    definition jsonb NOT NULL DEFAULT '{}',
    approved_definition jsonb,
    first_seen timestamptz NOT NULL DEFAULT now(),
    changed_at timestamptz,
    approved_by text,
    approved_at timestamptz,
    PRIMARY KEY (server, name)
);
CREATE TABLE IF NOT EXISTS mcp_calls (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    server text NOT NULL,
    tool text NOT NULL,
    user_id text,
    team text,
    key_id text,
    status text NOT NULL,
    reason text,
    duration_ms integer,
    request_bytes integer,
    response_bytes integer,
    findings jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mcp_calls_ts ON mcp_calls (ts);
CREATE INDEX IF NOT EXISTS mcp_calls_team_ts ON mcp_calls (team, ts);
"""
# Pages read by OCR (/v1/ocr, decision 77), on a request and in the hourly rollup.
SCHEMA_V25 = """
ALTER TABLE requests ADD COLUMN IF NOT EXISTS pages integer NOT NULL DEFAULT 0;
ALTER TABLE usage_hourly ADD COLUMN IF NOT EXISTS pages bigint NOT NULL DEFAULT 0;
"""
MIGRATIONS = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5), (6, SCHEMA_V6), (7, SCHEMA_V7),
              (8, SCHEMA_V8), (9, SCHEMA_V9), (10, SCHEMA_V10), (11, SCHEMA_V11), (12, SCHEMA_V12), (13, SCHEMA_V13),
              (14, SCHEMA_V14), (15, SCHEMA_V15), (16, SCHEMA_V16), (18, SCHEMA_V18), (19, SCHEMA_V19),
              (20, SCHEMA_V20), (21, SCHEMA_V21), (22, SCHEMA_V22), (23, SCHEMA_V23), (24, SCHEMA_V24), (25, SCHEMA_V25)]
EVAL_RUN_FIELDS = ("status", "progress", "scores", "results", "error", "cost", "started_at", "finished_at", "applied_version")

_SUMS_SQL = ", ".join(f"coalesce(sum({f}), 0) AS {f}" for f in SUM_FIELDS)
COPY_LOCK = 0x4D4F  # advisory lock class for copying an hour of the ledger to ClickHouse (key: hours since 1970)
PARTITION_LOCK = 0x4D50  # advisory lock class for creating a month's ledger partition (key: year * 12 + month)
BILL_LOCK = 0x4D42  # advisory lock class for replacing a bill source's lines (key: the source's name, hashed)
CONFIG_LOCK = 0x4D43  # advisory lock class for saving a config version (key: 0), so a base_version check and its insert are one step


class _HourRows:
    """One hour of the ledger, read inside the transaction that holds its copy lock (PostgresStore.ledger_hour)."""

    def __init__(self, conn, hour: datetime):
        self.conn, self.start, self.end = conn, hour, hour + timedelta(hours=1)

    async def minutes(self) -> dict[datetime, int]:
        rows = await self.conn.fetch("SELECT date_trunc('minute', ts) AS m, count(*) AS n FROM requests WHERE ts >= $1 AND ts < $2 "
                                     "GROUP BY 1", self.start, self.end)
        return {r["m"]: int(r["n"]) for r in rows}

    async def rows(self, minutes: list[datetime] | None = None):
        """The hour's rows (or only those in `minutes`), with a server-side cursor."""
        cols = ", ".join(LEDGER_FIELDS)
        sql = f"SELECT {cols} FROM requests WHERE ts >= $1 AND ts < $2"
        args: list[Any] = [self.start, self.end]
        if minutes is not None:
            sql += " AND date_trunc('minute', ts) = ANY($3::timestamptz[])"
            args.append(minutes)
        async for r in self.conn.cursor(sql + " ORDER BY ts, id", *args, prefetch=5000):
            yield dict(r)


class PostgresStore(PostgresBatches, PostgresStored):
    kind = "postgres"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool = None
        self.clickhouse = None  # clickhouse.Reporting, with reporting.clickhouse set

    def use_clickhouse(self, cfg) -> None:
        """Follow `reporting.clickhouse` (None: off). Called when a configuration is applied."""
        from . import clickhouse

        self.clickhouse = clickhouse.update(self.clickhouse, cfg, self)

    async def _from_clickhouse(self, start: datetime, big: bool, raw: bool, call):
        """The answer from ClickHouse when it should give it (clickhouse.Reporting.why_not), else None: Postgres answers.
        A failed query falls back to Postgres, and reports read Postgres for a while before trying ClickHouse again."""
        from .clickhouse import ClickHouseError

        rep = self.clickhouse
        if rep is None or (ledger := await rep.reader(start, big, raw)) is None:
            return None
        try:
            out = await call(ledger)
        except Exception as e:  # noqa: BLE001 (any failure there: Postgres has the same rows)
            if not isinstance(e, ClickHouseError):
                log.exception("ClickHouse query failed")
            rep.failed(e)
            return None
        note_answer("clickhouse")
        return out

    async def init(self) -> None:
        import asyncpg

        async def setup(conn):
            await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

        # Reports group by day/hour/month in UTC regardless of the database server's timezone.
        # Per process. Behind many gateway processes, put PgBouncer in front so the total stays within max_connections.
        size = int(os.environ.get("MERITORDER_DB_POOL_SIZE", "10"))
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=size, init=setup, server_settings={"timezone": "UTC"})

    async def close(self) -> None:
        if self.clickhouse:
            await self.clickhouse.close()
        if self.pool:
            await self.pool.close()

    async def ping(self) -> bool:
        async with self.pool.acquire() as c:
            return await c.fetchval("SELECT 1") == 1

    async def migrate(self) -> None:
        async with self.pool.acquire() as c:
            await c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            applied = {r["version"] for r in await c.fetch("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                async with c.transaction():
                    await c.execute(sql)
                    await c.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
        await self.ensure_partitions()

    async def ensure_partitions(self, months_ahead: int = 2) -> None:
        start = month_start()
        async with self.pool.acquire() as c:
            for i in range(months_ahead + 1):
                lo = (start + timedelta(days=32 * i)).replace(day=1)
                hi = (lo + timedelta(days=32)).replace(day=1)
                name = f"requests_{lo:%Y_%m}"
                exists = await c.fetchval("SELECT to_regclass($1)", name)
                if exists:
                    continue
                # Rows that landed in the default partition for this range must move first.
                async with c.transaction():
                    # The gateways (at start) and the workers (each cycle) all do this: one at a time, and only the first
                    # creates it, rather than the others failing on a table that now exists.
                    await c.execute("SELECT pg_advisory_xact_lock($1::int4, $2::int4)", PARTITION_LOCK, lo.year * 12 + lo.month)
                    # pg_class itself, read with this statement's snapshot: to_regclass reads the catalog cache, which an
                    # advisory lock doesn't refresh, so it could still miss the table the lock's last holder made.
                    if await c.fetchval("SELECT EXISTS (SELECT 1 FROM pg_class WHERE relname = $1 AND relnamespace = "
                                        "(SELECT oid FROM pg_namespace WHERE nspname = current_schema()))", name):
                        continue
                    await c.execute(f"CREATE TABLE {name} (LIKE requests INCLUDING DEFAULTS)")
                    await c.execute(
                        f"WITH moved AS (DELETE FROM requests_default WHERE ts >= $1 AND ts < $2 RETURNING *) "
                        f"INSERT INTO {name} SELECT * FROM moved", lo, hi)
                    await c.execute(f"ALTER TABLE requests ATTACH PARTITION {name} FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')")

    # ----------------------------------------------------------------- users & keys

    async def upsert_user(self, user_id, team, role, email=None, display_name=None, source: str = "idp") -> None:
        """source="idp": a login refreshes team/role unless an admin pinned them; "ensure": only create if missing."""
        refresh = "team=EXCLUDED.team, role=EXCLUDED.role," if source == "idp" else ""
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO users (id, team, role, email, display_name) VALUES ($1,$2,$3,$4,$5)
                   ON CONFLICT (id) DO UPDATE SET
                   team=CASE WHEN users.managed='idp' AND $6 THEN EXCLUDED.team ELSE users.team END,
                   role=CASE WHEN users.managed='idp' AND $6 THEN EXCLUDED.role ELSE users.role END,
                   email=coalesce(EXCLUDED.email, users.email), display_name=coalesce(EXCLUDED.display_name, users.display_name),
                   updated_at=now()""",
                user_id, team, role, email, display_name, bool(refresh))

    async def get_user(self, user_id: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        return dict(row) if row else None

    async def update_user(self, user_id, team=None, role=None, disabled=None, managed=None, budget_usd_month=KEEP) -> dict | None:
        sets, args = [], [user_id]
        if budget_usd_month is not KEEP:
            args.append(budget_usd_month)
            sets.append(f"budget_usd_month=${len(args)}")
        if team is not None:
            args.append(team)
            sets.append(f"team=${len(args)}")
        if role is not None:
            args.append(role)
            sets.append(f"role=${len(args)}")
        if (team is not None or role is not None) and managed is None:
            sets.append("managed='local'")  # an admin's edit sticks across IdP logins, unless told to follow the IdP
        if managed is not None:
            args.append(managed)
            sets.append(f"managed=${len(args)}")
        if disabled is not None:
            sets.append("disabled_at=now()" if disabled else "disabled_at=NULL")
        if not sets:
            return await self.get_user(user_id)
        async with self.pool.acquire() as c:
            row = await c.fetchrow(f"UPDATE users SET {', '.join(sets)}, updated_at=now() WHERE id=$1 RETURNING *", *args)
        return dict(row) if row else None

    async def find_user_by_email(self, email: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM users WHERE lower(email)=lower($1) LIMIT 1", email)
        return dict(row) if row else None

    async def scim_groups(self) -> list[dict]:
        async with self.pool.acquire() as c:
            return [dict(r) for r in await c.fetch("SELECT * FROM scim_groups ORDER BY display_name")]

    async def scim_group(self, gid: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM scim_groups WHERE id=$1", gid)
        return dict(row) if row else None

    async def save_scim_group(self, group: dict) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO scim_groups (id, display_name, external_id, members) VALUES ($1,$2,$3,$4)
                   ON CONFLICT (id) DO UPDATE SET display_name=EXCLUDED.display_name, external_id=EXCLUDED.external_id,
                   members=EXCLUDED.members, updated_at=now()""",
                group["id"], group["display_name"], group.get("external_id"), group.get("members") or [])

    async def delete_scim_group(self, gid: str) -> bool:
        async with self.pool.acquire() as c:
            return (await c.execute("DELETE FROM scim_groups WHERE id=$1", gid)).endswith("1")

    async def list_users(self, team: str | None = None) -> list[dict]:
        sql = """SELECT u.*, count(k.id) FILTER (WHERE k.revoked_at IS NULL) AS active_keys, max(k.last_used_at) AS last_seen,
                        bool_or(l.user_id IS NOT NULL) AS has_password, bool_or(l.totp_enabled_at IS NOT NULL) AS two_factor
                 FROM users u LEFT JOIN api_keys k ON k.user_id = u.id LEFT JOIN local_credentials l ON l.user_id = u.id""" + (
                 " WHERE u.team = $1" if team else "") + """
                 GROUP BY u.id ORDER BY u.id"""
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *([team] if team else []))
        return [dict(r) for r in rows]

    async def create_key(self, user_id, team, role, name=None, tags=None, expires_at=None) -> tuple[str, str]:
        key_id, secret = new_api_key()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO api_keys (id, key_hash, user_id, team, role, name, tags, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                key_id, hash_key(secret), user_id, team, role, name, tags or {}, expires_at)
        return key_id, secret

    async def find_key(self, key_hash: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM api_keys WHERE key_hash=$1", key_hash)
            if row:
                await c.execute("UPDATE api_keys SET last_used_at=now() WHERE id=$1 AND (last_used_at IS NULL OR last_used_at < now() - interval '5 minutes')", row["id"])
        return dict(row) if row else None

    async def list_keys(self, user_id: str | None = None, team: str | None = None) -> list[dict]:
        where, args = [], []
        if user_id:
            args.append(user_id)
            where.append(f"user_id = ${len(args)}")
        if team:
            args.append(team)
            where.append(f"team = ${len(args)}")
        sql = ("SELECT id, user_id, team, role, name, tags, created_at, expires_at, revoked_at, last_used_at FROM api_keys "
               + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at")
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def get_key(self, key_id: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id, user_id, team, role, name, tags, created_at, expires_at, revoked_at FROM api_keys WHERE id=$1", key_id)
        return dict(row) if row else None

    async def revoke_key(self, key_id: str) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE api_keys SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL", key_id)
        return res.endswith("1")

    # ----------------------------------------------------------------- console sign-in (decision 69)

    async def set_password(self, user_id: str, password_hash: str, max_role: str | None = None) -> None:
        async with self.pool.acquire() as c:
            await c.execute("""INSERT INTO local_credentials (user_id, password_hash, max_role) VALUES ($1,$2,$3)
                               ON CONFLICT (user_id) DO UPDATE SET password_hash=EXCLUDED.password_hash, max_role=EXCLUDED.max_role,
                               updated_at=now()""", user_id, password_hash, max_role)

    async def get_credential(self, user_id: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("""SELECT password_hash, max_role, totp_secret, totp_enabled_at, recovery_codes
                                      FROM local_credentials WHERE user_id=$1""", user_id)
        return dict(row) if row else None

    async def set_totp(self, user_id: str, secret: str | None, enabled: bool, recovery_codes: list[str] | None = None) -> None:
        async with self.pool.acquire() as c:
            await c.execute("""UPDATE local_credentials SET totp_secret=$2, totp_enabled_at=CASE WHEN $3 THEN now() END,
                               recovery_codes=$4, updated_at=now() WHERE user_id=$1""", user_id, secret, enabled, list(recovery_codes or []))

    # The MCP gateway (mcp.py): pinned tool definitions and the tool-call log.

    async def mcp_pins(self, server: str | None = None) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM mcp_tools WHERE ($1::text IS NULL OR server=$1) ORDER BY server, name", server)
        return [dict(r) for r in rows]

    async def mcp_pin(self, server: str, name: str, hash_: str, definition: dict, approve: bool) -> dict:
        """Pins a tool first seen (approved at once unless it must wait for an admin), or records a new definition."""
        async with self.pool.acquire() as c:
            row = await c.fetchrow("""INSERT INTO mcp_tools (server, name, hash, definition, approved_hash, approved_definition)
                                      VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN $3 END, CASE WHEN $5 THEN $4::jsonb END)
                                      ON CONFLICT (server, name) DO UPDATE SET hash=EXCLUDED.hash, definition=EXCLUDED.definition,
                                          changed_at=CASE WHEN mcp_tools.hash <> EXCLUDED.hash THEN now() ELSE mcp_tools.changed_at END
                                      RETURNING *""", server, name, hash_, definition, approve)
        return dict(row)

    async def mcp_approve(self, server: str, name: str, hash_: str, by: str) -> bool:
        """Approves the definition an admin saw (its hash), so one that changed again since isn't approved unseen."""
        async with self.pool.acquire() as c:
            res = await c.execute("""UPDATE mcp_tools SET approved_hash=hash, approved_definition=definition, approved_by=$4,
                                     approved_at=now() WHERE server=$1 AND name=$2 AND hash=$3""", server, name, hash_, by)
        return res.endswith("1")

    async def mcp_log(self, row: dict) -> None:
        cols = ("server", "tool", "user_id", "team", "key_id", "status", "reason", "duration_ms", "request_bytes", "response_bytes",
                "findings")
        async with self.pool.acquire() as c:
            await c.execute(f"INSERT INTO mcp_calls ({', '.join(cols)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
                            *[row.get(k) if k != "findings" else (row.get(k) or {}) for k in cols])

    async def mcp_calls(self, limit: int = 100, server: str | None = None, team: str | None = None,
                        since: datetime | None = None) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("""SELECT * FROM mcp_calls WHERE ($2::text IS NULL OR server=$2) AND ($3::text IS NULL OR team=$3)
                                    AND ($4::timestamptz IS NULL OR ts >= $4) ORDER BY ts DESC, id DESC LIMIT $1""",
                                 limit, server, team, since)
        return [dict(r) for r in rows]

    async def mcp_call_counts(self, since: datetime, team: str | None = None) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("""SELECT server, tool, status, count(*) AS calls, round(avg(duration_ms))::int AS avg_ms
                                    FROM mcp_calls WHERE ts >= $1 AND ($2::text IS NULL OR team=$2)
                                    GROUP BY 1, 2, 3""", since, team)
        return [{**dict(r), "calls": int(r["calls"])} for r in rows]

    async def purge_mcp_calls(self, before: datetime) -> int:
        async with self.pool.acquire() as c:
            res = await c.execute("DELETE FROM mcp_calls WHERE ts < $1", before)
        return int(res.split()[-1])

    async def use_recovery_code(self, user_id: str, code_hash: str) -> bool:
        """Removes the code if it's there: true once per code, even with two sign-ins at the same moment."""
        async with self.pool.acquire() as c:
            res = await c.execute("""UPDATE local_credentials SET recovery_codes = recovery_codes - $2::text
                                     WHERE user_id=$1 AND recovery_codes ? $2::text""", user_id, code_hash)
        return res.endswith("1")

    async def get_password(self, user_id: str) -> str | None:
        async with self.pool.acquire() as c:
            return await c.fetchval("SELECT password_hash FROM local_credentials WHERE user_id=$1", user_id)

    async def create_session(self, user_id, method, expires_at, key_hash=None, user_agent=None, max_role=None) -> str:
        sid, token = new_secret("ses")
        async with self.pool.acquire() as c:
            await c.execute("""INSERT INTO console_sessions (id, token_hash, user_id, method, key_hash, user_agent, expires_at, max_role)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""", sid, hash_key(token), user_id, method, key_hash, user_agent, expires_at,
                            max_role)
        return token

    async def find_session(self, token_hash: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM console_sessions WHERE token_hash=$1", token_hash)
        return dict(row) if row else None

    async def revoke_session(self, token_hash: str) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE console_sessions SET revoked_at=now() WHERE token_hash=$1 AND revoked_at IS NULL", token_hash)
        return res.endswith("1")

    async def revoke_user_sessions(self, user_id: str, keep: str | None = None) -> int:
        async with self.pool.acquire() as c:
            res = await c.execute("""UPDATE console_sessions SET revoked_at=now()
                                     WHERE user_id=$1 AND revoked_at IS NULL AND token_hash IS DISTINCT FROM $2""", user_id, keep)
        return int(res.split()[-1])

    async def create_invite(self, user_id, kind, expires_at, team=None, role=None, created_by=None, creator_role=None) -> tuple[str, str]:
        iid, token = new_secret("inv")
        async with self.pool.acquire() as c:
            await c.execute("""INSERT INTO invites (id, token_hash, user_id, kind, team, role, created_by, expires_at, creator_role)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""", iid, hash_key(token), user_id, kind, team, role, created_by,
                            expires_at, creator_role)
        return iid, token

    async def find_invite(self, token_hash: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM invites WHERE token_hash=$1", token_hash)
        return dict(row) if row else None

    async def use_invite(self, token_hash: str) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("""UPDATE invites SET used_at=now() WHERE token_hash=$1 AND used_at IS NULL
                                     AND revoked_at IS NULL AND expires_at > now()""", token_hash)
        return res.endswith("1")

    async def list_invites(self, team: str | None = None) -> list[dict]:
        sql = ("SELECT id, user_id, kind, team, role, created_by, created_at, expires_at FROM invites "
               "WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now()" + (" AND team = $1" if team else "")
               + " ORDER BY created_at")
        async with self.pool.acquire() as c:
            return [dict(r) for r in await c.fetch(sql, *([team] if team else []))]

    async def revoke_invite(self, invite_id: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("""UPDATE invites SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL AND used_at IS NULL
                                      RETURNING id, user_id, kind, team, role""", invite_id)
        return dict(row) if row else None

    async def revoke_user_invites(self, user_id: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute("UPDATE invites SET revoked_at=now() WHERE user_id=$1 AND revoked_at IS NULL AND used_at IS NULL", user_id)

    # ----------------------------------------------------------------- ledger

    async def record(self, row: dict) -> None:
        cols = ", ".join(LEDGER_FIELDS)
        params = ", ".join(f"${i + 1}" for i in range(len(LEDGER_FIELDS)))
        async with self.pool.acquire() as c:
            row = complete_row(row)
            await c.execute(f"INSERT INTO requests ({cols}) VALUES ({params})", *[row[f] for f in LEDGER_FIELDS])

    async def spend_since(self, field: str, value: str, since: datetime) -> float:
        if field not in ("user_id", "team"):
            raise ValueError(field)
        note_answer("postgres")  # budgets read Postgres always: it has every row the moment it's written
        async with self.pool.acquire() as c:
            return float(await c.fetchval(f"SELECT coalesce(sum(actual_cost),0) FROM requests WHERE {field}=$1 AND ts >= $2", value, since))

    async def spend_all_since(self, since: datetime) -> float:
        note_answer("postgres")
        async with self.pool.acquire() as c:
            return float(await c.fetchval("SELECT coalesce(sum(actual_cost),0) FROM requests WHERE ts >= $1", since))

    async def spend_by_tag(self, users: list[str], tag: str, since: datetime) -> dict[str, float]:
        """See MemoryStore.spend_by_tag. From Postgres, by user and time (its index), as budgets read spend."""
        note_answer("postgres")
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT tags->>$3 AS value, coalesce(sum(actual_cost),0) AS cost FROM requests "
                                 "WHERE user_id = ANY($1::text[]) AND ts >= $2 AND tags ? $3 GROUP BY 1", users, since, tag)
        return {r["value"]: float(r["cost"]) for r in rows}

    async def floor_daily(self, since: datetime, team: str | None = None) -> list[dict]:
        """See MemoryStore.floor_daily. From Postgres, by time (the ledger's index), like the learned router's report."""
        note_answer("postgres")
        where, args = ["ts >= $1", "floor IS NOT NULL"], [since]
        if team is not None:
            args.append(team)
            where.append("team = $2")
        sql = (f"SELECT to_char(date_trunc('day', ts AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day, floor, coalesce(tags->>'explore', '') AS arm, "
               f"count(*) AS requests, coalesce(sum(actual_cost), 0) AS cost FROM requests WHERE {' AND '.join(where)} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *args)
        return [{"day": r["day"], "floor": r["floor"], "arm": r["arm"], "requests": int(r["requests"]), "cost": float(r["cost"])} for r in rows]

    async def add_gpu_usage(self, backend: str, ts: datetime, seconds: float, cost: float) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO gpu_usage (hour, backend, replica_seconds, cost) VALUES (date_trunc('hour', $1::timestamptz), $2, $3, $4) "
                "ON CONFLICT (hour, backend) DO UPDATE SET replica_seconds = gpu_usage.replica_seconds + EXCLUDED.replica_seconds, "
                "cost = gpu_usage.cost + EXCLUDED.cost", ts, backend, seconds, cost)

    async def gpu_spend_since(self, since: datetime) -> dict[str, float]:
        note_answer("postgres")
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT backend, sum(cost) AS cost FROM gpu_usage WHERE hour >= date_trunc('hour', $1::timestamptz) GROUP BY backend", since)
        return {r["backend"]: float(r["cost"]) for r in rows}

    async def gpu_daily(self, since: datetime) -> list[dict]:
        note_answer("postgres")
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT date_trunc('day', hour)::date AS day, backend, sum(replica_seconds)/3600 AS replica_hours, sum(cost) AS cost "
                                 "FROM gpu_usage WHERE hour >= $1 GROUP BY 1, 2 ORDER BY 1, 2", since)
        return [{"day": r["day"].isoformat(), "backend": r["backend"], "replica_hours": float(r["replica_hours"]), "cost": float(r["cost"])} for r in rows]

    async def gpu_hourly(self, start: datetime, end: datetime) -> list[dict]:
        note_answer("postgres")
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT hour, backend, replica_seconds, cost FROM gpu_usage WHERE hour >= date_trunc('hour', $1::timestamptz) "
                                 "AND hour < $2 ORDER BY hour, backend", start, end)
        return [{"hour": r["hour"], "backend": r["backend"], "replica_seconds": float(r["replica_seconds"]), "cost": float(r["cost"])} for r in rows]

    async def gpu_ledger(self, start: datetime, end: datetime, backends: Sequence[str], raw: bool = False) -> list[dict]:
        """See MemoryStore.gpu_ledger. Over more than ROLLUP_AFTER it reads the hourly rollup, as summary does, so the two
        agree; shorter ranges read the ledger itself, and so does ``raw`` (budgets, which read every row the moment it's
        written, as spend_all_since does)."""
        note_answer("postgres")
        if not backends:
            return []
        if end - start > ROLLUP_AFTER and not raw:
            sql = ("SELECT hour, backend, sum(requests) AS requests, sum(actual_cost) AS actual_cost, sum(served_api_cost) AS served_api_cost "
                   "FROM usage_hourly WHERE hour >= $1 AND hour < $2 AND backend = ANY($3::text[]) GROUP BY 1, 2 ORDER BY 1, 2")
        else:
            sql = ("SELECT date_trunc('hour', ts) AS hour, backend, count(*) AS requests, coalesce(sum(actual_cost), 0) AS actual_cost, "
                   "coalesce(sum(served_api_cost), 0) AS served_api_cost FROM requests WHERE ts >= $1 AND ts < $2 "
                   "AND backend = ANY($3::text[]) GROUP BY 1, 2 ORDER BY 1, 2")
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, start, end, list(backends))
        return [{"hour": r["hour"], "backend": r["backend"], "requests": int(r["requests"]), "actual_cost": float(r["actual_cost"]),
                 "served_api_cost": float(r["served_api_cost"])} for r in rows]

    async def gpu_event(self, kind: str, backend: str | None = None, actor: str | None = None, detail: dict | None = None) -> None:
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO gpu_events (kind, backend, actor, detail) VALUES ($1,$2,$3,$4)", kind, backend, actor, detail or {})

    async def gpu_events(self, limit: int = 100, backend: str | None = None) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM gpu_events WHERE ($2::text IS NULL OR backend = $2) ORDER BY ts DESC LIMIT $1", limit, backend)
        return [dict(r) for r in rows]

    async def replace_bill_lines(self, source: str, start: date, end: date, lines: list[dict]) -> int:
        rows = merge_bill_lines(source, lines)
        cols = ", ".join(BILL_FIELDS)
        params = ", ".join(f"${i + 1}" for i in range(len(BILL_FIELDS)))
        async with self.pool.acquire() as c:
            async with c.transaction():
                # One replacement of a source's lines at a time (the worker's refresh, an admin's "Refresh now", another
                # worker): the second's delete would miss the first's new rows, and its insert would collide with them.
                await c.execute("SELECT pg_advisory_xact_lock($1::int4, hashtext($2))", BILL_LOCK, source)
                await c.execute("DELETE FROM bill_lines WHERE source = $1 AND day >= $2 AND day < $3", source, start, end)
                if rows:
                    await c.executemany(f"INSERT INTO bill_lines ({cols}) VALUES ({params})", [[r[f] for f in BILL_FIELDS] for r in rows])
        return len(rows)

    async def bill_lines(self, start: date, end: date, source: str | None = None) -> list[dict]:
        note_answer("postgres")
        cols = ", ".join(BILL_FIELDS)
        async with self.pool.acquire() as c:
            rows = await c.fetch(f"SELECT {cols} FROM bill_lines WHERE day >= $1 AND day < $2 AND ($3::text IS NULL OR source = $3) "
                                 "ORDER BY day, source, line", start, end, source)
        return [{**dict(r), "cost": float(r["cost"])} for r in rows]

    async def ledger_costs(self, start: datetime, end: datetime, backends: list[str]) -> list[dict]:
        rows = await self._from_clickhouse(start, end - start > ROLLUP_AFTER, True, lambda ch: ch.ledger_costs(start, end, backends))
        if rows is not None:
            return rows
        note_answer("postgres")
        sums = ", ".join(f"coalesce(sum({f}), 0) AS {f}" for f in LEDGER_COST_FIELDS[1:])
        sql = (f"SELECT date_trunc('day', ts)::date AS day, backend, coalesce(served_model, '') AS served_model, "
               f"coalesce(upstream_model, '') AS upstream_model, coalesce(service_tier, '') AS service_tier, count(*) AS requests, {sums} "
               "FROM requests WHERE ts >= $1 AND ts < $2 AND backend = ANY($3::text[]) GROUP BY 1, 2, 3, 4, 5")
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, start, end, list(backends))
        return [{**dict(r), "requests": int(r["requests"]), "actual_cost": float(r["actual_cost"]),
                 **{f: int(r[f]) for f in LEDGER_COST_FIELDS[1:-1]}} for r in rows]

    async def summary(self, start, end, group_by, user_id=None, team=None, gpu_backends: Sequence[str] = ()) -> list[dict]:
        validate_group(group_by)
        tag = _TAG.match(group_by)
        use_rollup = not tag and (end - start) > ROLLUP_AFTER
        rows = await self._from_clickhouse(start, bool(tag) or use_rollup, not use_rollup,
                                           lambda ch: ch.summary(start, end, group_by, user_id=user_id, team=team, gpu_backends=gpu_backends))
        if rows is not None:
            return rows
        note_answer("postgres")
        table, ts_col = ("usage_hourly", "hour") if use_rollup else ("requests", "ts")
        if tag:
            key_expr = f"coalesce(tags->>'{tag.group(1)}', '(untagged)')"
        elif group_by == "none":
            key_expr = "'total'"
        elif group_by in ("day", "hour", "month"):
            key_expr = f"date_trunc('{group_by}', {ts_col})"
        elif use_rollup:
            key_expr = f"nullif({GROUPS[group_by]}, '')"  # the rollup keeps a missing key as '' (its primary key): NULL, as in the ledger
        else:
            key_expr = GROUPS[group_by]
        if use_rollup:
            counts = "coalesce(sum(requests),0) AS requests, coalesce(sum(errors),0) AS errors, coalesce(sum(upgraded),0) AS upgraded"
        else:
            counts = "count(*) AS requests, count(*) FILTER (WHERE status >= 400) AS errors, count(*) FILTER (WHERE upgraded) AS upgraded"
        where = [f"{ts_col} >= $1", f"{ts_col} < $2"]
        args: list[Any] = [start, end]
        if user_id:
            args.append(user_id)
            where.append(f"user_id = ${len(args)}")
        if team:
            args.append(team)
            where.append(f"team = ${len(args)}")
        args.append(list(gpu_backends))
        gpu = f"coalesce(sum(actual_cost) FILTER (WHERE backend = ANY(${len(args)}::text[])), 0) AS gpu_cost"
        sql = f"SELECT {key_expr} AS key, {counts}, {_SUMS_SQL}, {gpu} FROM {table} WHERE {' AND '.join(where)} GROUP BY 1"
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *args)
        return sort_summary([_finish_row({k: (float(v) if k in SUM_FIELDS or k == "gpu_cost" else v) for k, v in dict(r).items()})
                             for r in rows])

    async def recent(self, limit: int, user_id=None, team=None) -> list[dict]:
        """The newest rows: always from Postgres, which has them first and finds them by its (ts, id) index."""
        note_answer("postgres")
        where, args = [], []
        if user_id:
            args.append(user_id)
            where.append(f"user_id = ${len(args)}")
        if team:
            args.append(team)
            where.append(f"team = ${len(args)}")
        args.append(limit)
        sql = f"SELECT * FROM requests {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY ts DESC, id DESC LIMIT ${len(args)}"
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *args)
        return [_finish_row({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(r).items()}) for r in rows]

    # Postgres types of each sort's value, for the cursor's parameters in a row comparison.
    _SORT_TYPES = {"time": "timestamptz", "cost": "double precision", "savings": "double precision", "latency": "int"}

    def _requests_sql(self, f: RequestFilter, sort: str, desc: bool, cursor: tuple | None, forward: bool) -> tuple[str, list]:
        """The Requests view's query and parameters, read toward smaller keys or larger ones: a page before the cursor is
        read the other way and reversed after."""
        args: list[Any] = []
        where = f.sql(args)
        expr = REQUEST_SORTS[sort]
        down = desc == forward
        if cursor is not None:
            args += [cursor[1], _as_uuid(cursor[2])]
            ts_id = f"${len(args) - 1}::timestamptz, ${len(args)}::uuid"
            if sort == "time":
                where.append(f"(ts, id) {'<' if down else '>'} ({ts_id})")
            else:
                args.append(cursor[0])
                where.append(f"({expr}, ts, id) {'<' if down else '>'} (${len(args)}::{self._SORT_TYPES[sort]}, {ts_id})")
        way = "DESC" if down else "ASC"
        order = f"ts {way}, id {way}" if sort == "time" else f"{expr} {way}, ts {way}, id {way}"
        cols = ", ".join(LEDGER_FIELDS)
        return f"SELECT {cols}, {expr} AS _sort FROM requests {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY {order}", args

    async def requests_page(self, f: RequestFilter, sort: str = "time", desc: bool = True, cursor: tuple | None = None,
                            forward: bool = True, limit: int = 50) -> list[dict]:
        """A page of the Requests view in display order: the rows after `cursor` (a row's (sort value, ts, id)), or before
        it when not `forward`. Keyset paging, never OFFSET: a page by time is a range of the (ts, id) primary key (or of a
        team, user or model index) however deep it is, and rows written meanwhile can't shift it. Always Postgres, which
        has every row the moment it's written."""
        note_answer("postgres")
        sql, args = self._requests_sql(f, sort, desc, cursor, forward)
        args.append(limit)
        async with self.pool.acquire() as c:
            rows = await c.fetch(f"{sql} LIMIT ${len(args)}", *args)
        out = [{**_ledger_out({k: v for k, v in dict(r).items() if k != "_sort"}), "_sort": r["_sort"]} for r in rows]
        return out if forward else out[::-1]

    async def requests_count(self, f: RequestFilter, cap: int = COUNT_EXACT) -> tuple[int, bool]:
        """How many rows match: exact up to `cap` (a count that stops there), else the planner's estimate (not exact)."""
        note_answer("postgres")
        args: list[Any] = []
        where = f.sql(args)
        body = f"FROM requests {'WHERE ' + ' AND '.join(where) if where else ''}"
        async with self.pool.acquire() as c:
            n = await c.fetchval(f"SELECT count(*) FROM (SELECT 1 {body} LIMIT {int(cap) + 1}) s", *args)
            if n <= cap:
                return int(n), True
            try:
                plan = await c.fetchval(f"EXPLAIN (FORMAT JSON) SELECT 1 {body}", *args)
                estimate = int((json.loads(plan) if isinstance(plan, str) else plan)[0]["Plan"]["Plan Rows"])
            except Exception:  # noqa: BLE001 (an estimate is a nicety: "more than" the cap still holds)
                log.warning("couldn't estimate the Requests view's count", exc_info=True)
                estimate = 0
        return max(estimate, int(cap) + 1), False

    async def requests_rows(self, f: RequestFilter, sort: str = "time", desc: bool = True):
        """Every row the filter matches, in the view's order, with a server-side cursor: the Requests view's CSV."""
        note_answer("postgres")
        sql, args = self._requests_sql(f, sort, desc, None, True)
        async with self.pool.acquire() as c:
            async with c.transaction():
                async for r in c.cursor(sql, *args, prefetch=2000):
                    row = dict(r)
                    row.pop("_sort", None)
                    yield row

    async def demand_rows(self, since: datetime) -> list[dict]:
        sql = """SELECT floor, served_model, requested_model, coalesce(sum(served_api_cost),0) AS cost,
                        coalesce(sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens),0) AS tokens
                 FROM requests WHERE ts >= $1 AND NOT coalesce(upgraded,false) AND coalesce(status,0) < 400
                 AND service_tier IS DISTINCT FROM 'batch'
                 GROUP BY 1,2,3"""
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, since)
        return [{**dict(r), "cost": float(r["cost"]), "tokens": int(r["tokens"])} for r in rows]

    async def demand_by_hour(self, since: datetime) -> list[dict]:
        rows = await self._from_clickhouse(since, datetime.now(UTC) - since > ROLLUP_AFTER, True, lambda ch: ch.demand_by_hour(since))
        if rows is not None:
            return rows
        note_answer("postgres")
        mix = ", ".join(f"coalesce(sum({f}_tokens),0) AS {f}" for f in MIX_FIELDS)
        sql = f"""SELECT date_trunc('hour', ts) AS hour, floor, served_model, requested_model,
                        coalesce(sum(served_api_cost),0) AS cost,
                        coalesce(sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens),0) AS tokens,
                        count(*) AS requests, {mix}
                 FROM requests WHERE ts >= $1 AND NOT coalesce(upgraded,false) AND coalesce(status,0) < 400
                 AND service_tier IS DISTINCT FROM 'batch'
                 GROUP BY 1,2,3,4"""
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, since)
        return [{**dict(r), "cost": float(r["cost"]), "tokens": int(r["tokens"]), "requests": int(r["requests"]),
                 **{f: int(r[f]) for f in MIX_FIELDS}} for r in rows]

    async def export_rows(self, start: datetime, end: datetime, team: str | None = None):
        """Stream ledger rows with a server-side cursor (constant memory for big ranges), or from ClickHouse for a long
        range once it has them. ClickHouse failing before the first row falls back to Postgres; after it, the export fails."""
        ch = await self.clickhouse.reader(start, end - start > ROLLUP_AFTER) if self.clickhouse else None
        if ch is not None:
            sent = False
            try:
                async for r in ch.export_rows(start, end, team):
                    if not sent:
                        note_answer("clickhouse")
                        sent = True
                    yield r
                if not sent:
                    note_answer("clickhouse")
                return
            except Exception as e:
                if sent:
                    raise
                self.clickhouse.failed(e)
        note_answer("postgres")
        cols = ", ".join(LEDGER_FIELDS)
        sql = f"SELECT {cols} FROM requests WHERE ts >= $1 AND ts < $2" + (" AND team = $3" if team else "") + " ORDER BY ts, id"
        args = [start, end] + ([team] if team else [])
        async with self.pool.acquire() as c:
            async with c.transaction():
                async for r in c.cursor(sql, *args, prefetch=2000):
                    yield dict(r)

    async def needed_demand(self, model: str, since: datetime) -> float:
        async with self.pool.acquire() as c:
            return float(await c.fetchval(
                "SELECT coalesce(sum(served_api_cost),0) FROM requests WHERE served_model=$1 AND NOT coalesce(upgraded,false) AND ts >= $2 "
                "AND service_tier IS DISTINCT FROM 'batch'",
                model, since))

    # --- config versions
    async def save_config(self, body: dict, author: str, comment: str | None, base_version: object = ANY_VERSION) -> int:
        """``base_version`` (see MemoryStore.save_config) is checked and the version inserted under a transaction-level
        advisory lock, so of two saves based on the same version exactly one wins."""
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", CONFIG_LOCK)
                if base_version is not ANY_VERSION:
                    latest = await c.fetchval("SELECT max(id) FROM config_versions")
                    if base_version != latest:
                        raise ConfigConflict(base_version, latest)  # type: ignore[arg-type]
                return await c.fetchval("INSERT INTO config_versions (author, comment, body) VALUES ($1,$2,$3) RETURNING id",
                                        author, comment, body)

    async def active_config(self) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    async def config_versions(self, limit: int = 50) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT id, created_at, author, comment FROM config_versions ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get_config(self, vid: int) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM config_versions WHERE id=$1", vid)
        return dict(row) if row else None

    async def get_setting(self, key: str):
        async with self.pool.acquire() as c:
            return await c.fetchval("SELECT value FROM settings WHERE key=$1", key)

    async def set_setting(self, key: str, value) -> None:
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                            key, value)

    async def add_setting(self, key: str, value) -> None:
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO NOTHING", key, value)

    async def patch_setting(self, key: str, patch: dict) -> None:
        """Merge these top-level fields into a setting that's a mapping, in one statement: the worker's copy and a
        backfill both update the ClickHouse copy's progress without overwriting each other's fields."""
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE "
                            "SET value = settings.value || EXCLUDED.value, updated_at=now()", key, patch)

    async def drop_setting_entry(self, key: str, section: str, name: str, expect=None) -> bool:
        """Remove ``value[section][name]`` in one statement, leaving every other field as it is now (a GPU control saved
        meanwhile), and only while it still equals ``expect`` (when given). True when it was removed."""
        async with self.pool.acquire() as c:
            done = await c.fetchval("UPDATE settings SET value = value #- ARRAY[$2, $3]::text[], updated_at=now() "
                                    "WHERE key=$1 AND value #> ARRAY[$2, $3]::text[] IS NOT NULL "
                                    "AND ($4::jsonb IS NULL OR value #> ARRAY[$2, $3]::text[] = $4::jsonb) RETURNING true",
                                    key, section, name, expect)
        return bool(done)

    async def record_route_features(self, row: dict) -> None:
        row = {**row, "retried": bool(row.get("retried")), "refused_scores": list(row.get("refused_scores") or []),
               "text_features": pack_buckets(row.get("text_features") or []),
               "triage_features": pack_buckets(row.get("triage_features") or [])}
        cols = ", ".join(ROUTE_FEATURE_FIELDS)
        params = ", ".join(f"${i + 1}" for i in range(len(ROUTE_FEATURE_FIELDS)))
        async with self.pool.acquire() as c:
            await c.execute(f"INSERT INTO route_features ({cols}) VALUES ({params}) ON CONFLICT (request_id) DO NOTHING",
                            *[row.get(f) for f in ROUTE_FEATURE_FIELDS])

    async def mark_route_outcome(self, request_id: str, retried: bool = False, floor_raised: int | None = None) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE route_features SET retried = retried OR $2, floor_raised = CASE WHEN $3::smallint IS NULL "
                                  "THEN floor_raised ELSE greatest(coalesce(floor_raised, 0), $3::smallint) END WHERE request_id = $1",
                                  uuid.UUID(str(request_id)), retried, floor_raised)
        return res.endswith(" 1")

    async def mark_coding(self, request_id: str, coding: dict) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE route_features SET coding = $2 WHERE request_id = $1", uuid.UUID(str(request_id)), coding)
        return res.endswith(" 1")

    async def mark_judged(self, request_id: str, judged: dict) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE route_features SET judged = $2 WHERE request_id = $1", uuid.UUID(str(request_id)), judged)
        return res.endswith(" 1")

    async def judged_traffic(self, team: str, since: datetime) -> list[dict]:
        """See MemoryStore.judged_traffic. The ledger is read by team and time (its index), from a day before the features'
        range, so the join stays within the partitions it needs."""
        note_answer("postgres")
        sql = """SELECT f.task, f.rules_level, f.tags, f.served_model, f.status,
                        r.input_tokens + r.cache_read_tokens + r.cache_write_tokens AS prompt_tokens, r.output_tokens
                 FROM route_features f JOIN requests r ON r.id = f.request_id AND r.team = $1 AND r.ts >= $2::timestamptz - interval '1 day'
                 WHERE f.team = $1 AND f.ts >= $2"""
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, team, since)
        return [{**dict(r), "tags": list(r["tags"] or []), "prompt_tokens": int(r["prompt_tokens"] or 0),
                 "output_tokens": int(r["output_tokens"] or 0)} for r in rows]

    async def route_features(self, since: datetime, until: datetime | None = None, fv: str | None = None, team: str | None = None,
                             text: bool = True, limit: int | None = None) -> list[dict]:
        """Rows oldest first, read with a cursor so the hashed words go straight into compact arrays. Always from Postgres:
        they're routing data, not ledger rows, and ClickHouse never has a copy (decision 47)."""
        note_answer("postgres")
        where, args = ["ts >= $1"], [since]
        for col, value, op in (("ts", until, "<"), ("fv", fv, "="), ("team", team, "=")):
            if value is not None:
                args.append(value)
                where.append(f"{col} {op} ${len(args)}")
        cols = [f for f in ROUTE_FEATURE_FIELDS if text or f not in ("text_features", "triage_features")]
        sql = f"SELECT {', '.join(cols)} FROM route_features WHERE {' AND '.join(where)} ORDER BY ts DESC"
        if limit:
            args.append(limit)
            sql += f" LIMIT ${len(args)}"
        out = []
        async with self.pool.acquire() as c:
            async with c.transaction():
                async for r in c.cursor(sql, *args, prefetch=1000):
                    row = dict(r)
                    if text:
                        row["text_features"] = unpack_buckets(row["text_features"])
                        row["triage_features"] = unpack_buckets(row.get("triage_features"))
                    out.append(row)
        out.reverse()
        return out

    async def prune_route_features(self, before: datetime) -> int:
        async with self.pool.acquire() as c:
            res = await c.execute("DELETE FROM route_features WHERE ts < $1", before)
        return int(res.split()[-1]) if res else 0

    async def save_router_model(self, doc: dict, metrics: dict, author: str, active: bool) -> int:
        async with self.pool.acquire() as c:
            async with c.transaction():
                if active:
                    await c.execute("UPDATE router_models SET active = false WHERE active")
                return await c.fetchval("INSERT INTO router_models (created_by, active, doc, metrics) VALUES ($1,$2,$3,$4) RETURNING version",
                                        author, active, doc, metrics)

    async def router_model(self, version: int) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM router_models WHERE version=$1", version)
        return dict(row) if row else None

    async def router_model_version(self) -> int | None:
        async with self.pool.acquire() as c:
            return await c.fetchval("SELECT version FROM router_models WHERE active ORDER BY version DESC LIMIT 1")

    async def router_models(self, limit: int = 20) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(f"SELECT {', '.join(ROUTER_MODEL_FIELDS)} FROM router_models ORDER BY version DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def activate_router_model(self, version: int | None) -> bool:
        async with self.pool.acquire() as c:
            async with c.transaction():
                if version is not None and not await c.fetchval("SELECT 1 FROM router_models WHERE version=$1", version):
                    return False
                await c.execute("UPDATE router_models SET active = coalesce(version = $1, false)", version)
        return True

    # --- copying the ledger to ClickHouse (clickhouse.Copier)

    async def first_ledger_ts(self) -> datetime | None:
        async with self.pool.acquire() as c:
            return await c.fetchval("SELECT min(ts) FROM requests")

    async def ledger_minutes(self, start: datetime, end: datetime) -> dict[datetime, int]:
        """Rows per minute: what the copy compares with ClickHouse's counts."""
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT date_trunc('minute', ts) AS m, count(*) AS n FROM requests WHERE ts >= $1 AND ts < $2 GROUP BY 1",
                                 start, end)
        return {r["m"]: int(r["n"]) for r in rows}

    @asynccontextmanager
    async def ledger_hour(self, hour: datetime, wait: bool = False):
        """One hour of the ledger for copying, under a transaction-level advisory lock on the hour, so two copies never
        send the same rows. Yields None when another copy holds the hour and `wait` is false."""
        slot = int(hour.timestamp()) // 3600
        async with self.pool.acquire() as c:
            async with c.transaction():
                if wait:
                    await c.execute("SELECT pg_advisory_xact_lock($1::int4, $2::int4)", COPY_LOCK, slot)
                elif not await c.fetchval("SELECT pg_try_advisory_xact_lock($1::int4, $2::int4)", COPY_LOCK, slot):
                    yield None
                    return
                yield _HourRows(c, hour)

    async def create_eval_run(self, run: dict) -> dict:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("INSERT INTO eval_runs (id, created_by, team, status, executor, request, started_at) "
                                   "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *", run["id"], run["created_by"], run["team"], run["status"],
                                   run["executor"], run["request"], run.get("started_at"))
        return dict(row)

    async def get_eval_run(self, run_id: str) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM eval_runs WHERE id=$1", run_id)
        return dict(row) if row else None

    async def list_eval_runs(self, limit: int | None = 50) -> list[dict]:
        """Newest first; ``limit=None`` lists them all."""
        cols = "id, created_at, created_by, team, status, executor, request, progress, scores, error, cost, started_at, finished_at, applied_version"
        async with self.pool.acquire() as c:
            rows = await c.fetch(f"SELECT {cols} FROM eval_runs ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def update_eval_run(self, run_id: str, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EVAL_RUN_FIELDS}
        if not fields:
            return await self.get_eval_run(run_id)
        sets = ", ".join(f"{k}=${i}" for i, k in enumerate(fields, 2))
        async with self.pool.acquire() as c:
            row = await c.fetchrow(f"UPDATE eval_runs SET {sets} WHERE id=$1 RETURNING *", run_id, *fields.values())
        return dict(row) if row else None

    async def claim_eval_run(self) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "UPDATE eval_runs SET status='running', started_at=now() WHERE id = (SELECT id FROM eval_runs WHERE status='queued' "
                "AND executor='worker' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *")
        return dict(row) if row else None

    async def rollup(self, full: bool = False) -> int:
        """Recompute hourly rollups from the raw ledger.

        Normally the last 3 hours (late rows land within that window). With ``full``,
        or when the rollup table is empty, everything is recomputed, a month at a time: one statement over a
        year of a large ledger aggregates millions of groups at once and spills them to disk again and again (measured
        on 20M rows: over 17 minutes and not done, against under 5 a month at a time; docs/08-reporting.md).
        """
        sums = ", ".join(f"coalesce(sum({f}),0)" for f in SUM_FIELDS)
        cols = ", ".join(SUM_FIELDS)
        updates = ", ".join(f"{f}=EXCLUDED.{f}" for f in ["requests", "errors", "upgraded", *SUM_FIELDS])
        sql = f"""
            INSERT INTO usage_hourly (hour, user_id, team, served_model, backend, requests, errors, upgraded, {cols})
            SELECT date_trunc('hour', ts), coalesce(user_id,''), coalesce(team,''), coalesce(served_model,''), coalesce(backend,''),
                   count(*), count(*) FILTER (WHERE status >= 400), count(*) FILTER (WHERE upgraded), {sums}
            FROM requests WHERE ts >= $1 AND ts < $2
            GROUP BY 1,2,3,4,5
            ON CONFLICT (hour, user_id, team, served_model, backend) DO UPDATE SET {updates}"""
        never = datetime(9999, 1, 1, tzinfo=UTC)
        async with self.pool.acquire() as c:
            if not full and await c.fetchval("SELECT EXISTS (SELECT 1 FROM usage_hourly)"):
                since = (datetime.now(UTC) - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
                res = await c.execute(sql, since, never)
                return int(res.split()[-1]) if res else 0
            first = await c.fetchval("SELECT min(ts) FROM requests")
            month, rows = (month_start(first) if first else never), 0
            while month < never:
                nxt = (month + timedelta(days=32)).replace(day=1)
                last = nxt > datetime.now(UTC)  # the last month takes everything after it too, rows ahead of this clock included
                res = await c.execute(sql, month, never if last else nxt)
                rows += int(res.split()[-1]) if res else 0
                month = never if last else nxt
        return rows


def make_store(database_url: str | None):
    return PostgresStore(database_url) if database_url else MemoryStore()
