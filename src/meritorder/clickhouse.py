"""An optional ClickHouse copy of the ledger, for very large deployments (docs/08-reporting.md, "Storage and scale";
docs/09-decisions.md, decision 47).

Postgres stays the record. The gateway writes every ledger row there as it does without ClickHouse, so budgets, the
request feed and an outage of ClickHouse never depend on it. The worker copies the rows here (``Copier``): every
``copy_seconds`` it compares each recent hour on both sides, minute by minute, and copies what's missing in batches;
an hour that holds duplicates, or whose rollup disagrees, is copied again whole. Reports over long ranges and tag
breakdowns read the copy (``Reporting``) once it covers their range, and fall back to Postgres when it doesn't or when
ClickHouse fails. Every query here returns what the Postgres query in store.py returns for the same rows.

ClickHouse is reached over its HTTP interface with httpx (no driver): ClickHouse 24.8 or newer; CI runs 26.3.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, AsyncIterator, Sequence

import httpx

from .store import GROUPS, LEDGER_FIELDS, ROLLUP_AFTER, SUM_FIELDS, _TAG, _finish_row, sort_summary

log = logging.getLogger("meritorder.clickhouse")

# Every ledger column's ClickHouse type, as Postgres stores it (store.py): nullable where Postgres is, low-cardinality
# for names. A column added to the ledger fails `column_types()` (and a test) until it has a type here.
_NAMES = "LowCardinality(Nullable(String))"
COLUMN_TYPES = {
    "id": "UUID",
    "ts": "DateTime64(6, 'UTC')",
    **dict.fromkeys(("user_id", "team", "key_id", "client_format", "requested_model", "floor", "served_model", "upstream_model",
                     "backend", "backend_kind", "service_tier"), _NAMES),
    **dict.fromkeys(("session_id", "route_reason", "error", "batch_id", "failed_over"), "Nullable(String)"),
    "tags": "Map(String, String)",  # a value that isn't text is kept as JSON, as in the Parquet export
    **dict.fromkeys(("status", "latency_ms", "ttft_ms"), "Nullable(Int32)"),
    **dict.fromkeys(("stream", "upgraded", "proxy_cache"), "Nullable(Bool)"),
    **dict.fromkeys(("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "pages"), "Int64"),
    **dict.fromkeys(("actual_cost", "served_api_cost", "baseline_cost", "list_cost", "savings_routing", "savings_caching",
                     "savings_self_hosting", "savings_batch", "savings_compression", "savings_compression_model",
                     "savings_response_cache"), "Float64"),
}
ROLLUP_KEYS = ("user_id", "team", "served_model", "backend")  # usage_hourly's, as in Postgres ('' for a missing one)
TS = "DateTime64(6, 'UTC')"
STATE_KEY = "clickhouse_copy"  # the copy's progress, in Postgres settings: {from, through, checked_at, error, ...}
CREATED = "created requests"  # what Ledger.migrate() says when ClickHouse had no ledger table
EVERYTHING = datetime(1970, 1, 1, tzinfo=UTC)  # `from` when ClickHouse holds every row: Postgres has none before the copy's start
RECHECK = timedelta(hours=3)  # each cycle compares the last 3 hours again: rows land late, as for the Postgres rollup
STATE_SECONDS = 15  # how long a gateway trusts what it last read of the copy's progress
RETRY_SECONDS = 30  # after a failed query, reports read Postgres this long before trying ClickHouse again
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def column_types() -> list[tuple[str, str]]:
    """Every ledger column with its ClickHouse type, in ledger order. A ledger column without one fails loudly here
    rather than being left out of the copy."""
    missing = [f for f in LEDGER_FIELDS if f not in COLUMN_TYPES]
    if missing:
        raise KeyError(f"ledger columns without a ClickHouse type: {', '.join(missing)} (add them to clickhouse.COLUMN_TYPES)")
    return [(f, COLUMN_TYPES[f]) for f in LEDGER_FIELDS]


def rollup_types() -> list[tuple[str, str]]:
    """usage_hourly's columns: the hour, the keys, the counts and the sums."""
    return ([("hour", "DateTime('UTC')")] + [(k, "LowCardinality(String)") for k in ROLLUP_KEYS]
            + [(c, "UInt64") for c in ("requests", "errors", "upgraded")] + [(f, COLUMN_TYPES[f]) for f in SUM_FIELDS])


class ClickHouseError(Exception):
    pass


# ---------------------------------------------------------------------------- values in and out


def _dt(d: datetime, frac: bool = True) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f" if frac else "%Y-%m-%d %H:%M:%S")


def _escaped(s: str) -> str:
    """A query parameter's value, escaped as ClickHouse reads parameters (its TSV escaping)."""
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _param(value: Any) -> str:
    if isinstance(value, datetime):
        return _dt(value)
    if isinstance(value, (list, tuple, set)):
        quoted = ("'" + (_dt(v, False) if isinstance(v, datetime) else str(v)).replace("\\", "\\\\").replace("'", "\\'") + "'"
                  for v in value)
        return _escaped("[" + ",".join(quoted) + "]")
    return _escaped(str(value))


def parse_time(value: Any) -> datetime | None:
    """A DateTime, DateTime64 or Date from ClickHouse's JSON (always UTC here), or an ISO time from the settings."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value)
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# Rows go to ClickHouse as RowBinary, its native row format: a float arrives as its exact 8 bytes. As text (JSON), some
# ClickHouse versions round a 17-digit float to the neighbouring one, even with precise_float_parsing.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO = timedelta(microseconds=1)


def _varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


_LEN = [bytes((n,)) for n in range(128)]  # a short string's length is one byte: most names, ids and reasons
_SOME = [b"\x00" + bytes((n,)) for n in range(128)]  # the same after Nullable's "not null" byte


def _text(value: Any) -> bytes:
    b = value.encode() if type(value) is str else str(value).encode()
    n = len(b)
    return _LEN[n] + b if n < 128 else _varint(n) + b


def _maybe_text(value: Any) -> bytes:
    """Nullable(String), the most common column type here, in one step."""
    if value is None:
        return b"\x01"
    b = value.encode() if type(value) is str else str(value).encode()
    n = len(b)
    return _SOME[n] + b if n < 128 else b"\x00" + _varint(n) + b


_PACK_UUID, _PACK_TS = struct.Struct("<QQ").pack, struct.Struct("<q").pack


def _uuid(value: Any) -> bytes:
    u = value.int if type(value) is uuid.UUID else uuid.UUID(str(value)).int
    return _PACK_UUID(u >> 64, u & 0xFFFF_FFFF_FFFF_FFFF)


def _tags(tags: dict | None) -> bytes:
    """A Map(String, String): a value that isn't text is kept as JSON (as in the Parquet export); a missing one is left out."""
    pairs = [(str(k), v if isinstance(v, str) else json.dumps(v)) for k, v in (tags or {}).items() if v is not None]
    return _varint(len(pairs)) + b"".join(_text(k) + _text(v) for k, v in pairs)


def _encoder(t: str):
    """How one ClickHouse type is written in RowBinary."""
    if t.startswith("LowCardinality("):
        return _encoder(t[len("LowCardinality("):-1])  # the same as the type inside it
    if t == "Nullable(String)":
        return _maybe_text
    if t.startswith("Nullable("):
        inner = _encoder(t[len("Nullable("):-1])
        return lambda v: b"\x01" if v is None else b"\x00" + inner(v)
    simple = {
        "String": _text,
        "UUID": _uuid,
        "DateTime64(6, 'UTC')": lambda v: _PACK_TS((v - _EPOCH) // _MICRO),
        "Map(String, String)": _tags,
        "Int32": lambda v: struct.pack("<i", int(v)), "Int64": lambda v: struct.pack("<q", int(v or 0)),
        "Float64": lambda v: struct.pack("<d", float(v or 0)), "Bool": lambda v: b"\x01" if v else b"\x00",
    }
    return simple[t]


_FIXED = {"Int64": "q", "Float64": "d"}  # never null, so a run of them is one struct.pack


def _row_plan() -> list[tuple]:
    """How to write a row: each column's encoder, except that a run of token counts or costs is packed in one call. Half
    the copy's time went to writing rows a value at a time (docs/08-reporting.md, "Measured at scale")."""
    plan: list[tuple] = []
    run: list[tuple[str, str]] = []
    for f, t in [*column_types(), ("", "")]:  # an empty last column ends the last run
        if t in _FIXED:
            run.append((f, _FIXED[t]))
            continue
        if run:
            plan.append((struct.Struct("<" + "".join(c for _, c in run)).pack, tuple(f for f, _ in run), tuple(c == "q" for _, c in run)))
            run = []
        if f:
            plan.append((None, f, _encoder(t)))
    return plan


def row_binary(rows: list[dict]) -> bytes:
    """Ledger rows from Postgres in RowBinary, every column of the ledger in its order."""
    plan = _row_plan()
    out = bytearray()
    for r in rows:
        get = r.get
        for pack, field, enc in plan:
            if pack is None:
                out += enc(get(field))
            else:  # a run: `field` holds its columns, `enc` whether each is a whole number
                out += pack(*[int(get(f) or 0) if whole else float(get(f) or 0) for f, whole in zip(field, enc)])
    return bytes(out)


def decode_row(row: dict) -> dict:
    """A ledger row from ClickHouse with the Python types Postgres gives it (UUID, datetime, dict)."""
    row["id"] = uuid.UUID(row["id"])
    row["ts"] = parse_time(row["ts"])
    return row


# ---------------------------------------------------------------------------- HTTP


class _QuietRequests(logging.Filter):
    """httpx logs every request at INFO with its URL. For ClickHouse that's each query's settings and values, and whole
    INSERT statements, several a second while a backfill runs: they're logged only when the log level is DEBUG."""

    urls: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if record.levelno == logging.INFO and len(args) > 1 and str(args[1]).startswith(tuple(self.urls)):
            return logging.getLogger().isEnabledFor(logging.DEBUG)
        return True


_quiet = _QuietRequests()


class ClickHouse:
    """ClickHouse's HTTP interface. SQL goes in the body, values as query parameters (never into the SQL text)."""

    # Every query: 64-bit integers as JSON numbers, and errors as an HTTP status rather than half a result.
    SETTINGS = {"output_format_json_quote_64bit_integers": "0", "output_format_json_quote_64bit_floats": "0",
                "date_time_output_format": "simple"}

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.url = cfg.url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.url, timeout=cfg.timeout_seconds, transport=transport)
        _quiet.urls.add(self.url + "/")
        if _quiet not in logging.getLogger("httpx").filters:
            logging.getLogger("httpx").addFilter(_quiet)

    def _headers(self) -> dict[str, str]:
        headers = {"X-ClickHouse-User": self.cfg.user}
        password = self.cfg.auth.key()  # read on every call, so a rotated secret file takes effect
        if password:
            headers["X-ClickHouse-Key"] = password
        return headers

    def _params(self, params: dict | None, settings: dict | None) -> dict[str, str]:
        out = {**self.SETTINGS, **{k: str(v) for k, v in (settings or {}).items()}}
        out.update({f"param_{k}": _param(v) for k, v in (params or {}).items()})
        return out

    @staticmethod
    def _error(resp: httpx.Response, text: str) -> ClickHouseError:
        code = resp.headers.get("x-clickhouse-exception-code")
        first = text.strip().splitlines()[0] if text.strip() else f"HTTP {resp.status_code}"
        return ClickHouseError(f"ClickHouse {'error ' + code if code else f'HTTP {resp.status_code}'}: {first[:400]}")

    async def _post(self, body: bytes | str, params: dict[str, str]) -> httpx.Response:
        try:
            resp = await self.client.post("/", params=params, content=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise ClickHouseError(f"can't reach ClickHouse at {self.url}: {e or type(e).__name__}") from e
        if resp.status_code != 200:
            raise self._error(resp, resp.text)
        return resp

    async def command(self, sql: str, params: dict | None = None, settings: dict | None = None) -> None:
        await self._post(sql, self._params(params, {"wait_end_of_query": 1, **(settings or {})}))

    async def query(self, sql: str, params: dict | None = None, settings: dict | None = None) -> list[dict]:
        resp = await self._post(sql + "\nFORMAT JSONEachRow", self._params(params, {"wait_end_of_query": 1, **(settings or {})}))
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    async def stream(self, sql: str, params: dict | None = None) -> AsyncIterator[dict]:
        """Rows as ClickHouse sends them, for results too big to hold (exports). An error after the first rows arrives as
        text in the stream: it raises here rather than ending the export early."""
        try:
            async with self.client.stream("POST", "/", params=self._params(params, None), content=sql + "\nFORMAT JSONEachRow",
                                          headers=self._headers()) as resp:
                if resp.status_code != 200:
                    raise self._error(resp, (await resp.aread()).decode(errors="replace"))
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        raise ClickHouseError(f"ClickHouse failed while sending rows: {line.strip()[:400]}")
                    if not isinstance(row, dict) or "exception" in row:
                        raise ClickHouseError(f"ClickHouse failed while sending rows: {str(row)[:400]}")
                    yield row
        except httpx.HTTPError as e:
            raise ClickHouseError(f"can't reach ClickHouse at {self.url}: {e or type(e).__name__}") from e

    async def insert(self, table: str, columns: list[str], body: bytes) -> None:
        """One synchronous INSERT of RowBinary rows (row_binary) into these columns. ClickHouse's async inserts (the
        default from 26.3) are off here: the rows come in batches already. A token unique to this insert keeps ClickHouse
        from dropping it as a repeat of an earlier block with the same rows (a copy that repairs an hour sends them again)."""
        settings = {"async_insert": 0, "insert_deduplication_token": uuid.uuid4().hex}
        query = f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) FORMAT RowBinary"
        await self._post(body, self._params(None, {**settings, "query": query}))

    async def version(self) -> str:
        return str((await self.query("SELECT version() AS v"))[0]["v"])

    async def close(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------------------- schema and queries


def _ttl(expr: str, days: int | None) -> str:
    return f"{expr} + toIntervalDay({days})" if days else ""


class Ledger:
    """The ledger tables in ClickHouse: the schema, the report queries (each the twin of PostgresStore's), and what the
    copy needs (counts per minute, ids, inserts, deleting an hour to copy it again)."""

    def __init__(self, ch: ClickHouse, cfg):
        if not _IDENT.match(cfg.database):
            raise ValueError(f"reporting.clickhouse.database must be a plain name, got {cfg.database!r}")
        self.ch, self.cfg, self.db = ch, cfg, cfg.database
        self.requests, self.rollup = f"`{self.db}`.requests", f"`{self.db}`.usage_hourly"

    # ------------------------------------------------------------ schema

    def mv_select(self) -> str:
        keys = ", ".join(f"ifNull({k}, '') AS {k}" for k in ROLLUP_KEYS)
        sums = ", ".join(f"sum({f}) AS {f}" for f in SUM_FIELDS)
        return (f"SELECT toStartOfHour(toDateTime(ts)) AS hour, {keys}, count() AS requests, countIf(ifNull(status, 0) >= 400) AS errors, "
                f"countIf(ifNull(upgraded, false)) AS upgraded, {sums} FROM {self.requests} GROUP BY hour, {', '.join(ROLLUP_KEYS)}")

    def mv_name(self) -> str:
        """The materialized view's name carries a hash of its query, so a changed rollup replaces the view."""
        return "usage_hourly_mv_" + hashlib.sha256(self.mv_select().encode()).hexdigest()[:10]

    def ddl(self) -> list[str]:
        cols = ", ".join(f"`{f}` {t}" for f, t in column_types())
        ttl = _ttl("toDateTime(ts)", self.cfg.ttl_days)
        rcols = ", ".join(f"`{f}` {t}" for f, t in rollup_types())
        rttl = _ttl("hour", self.cfg.rollup_ttl_days)
        # Ordered for the reports' filters: a range of days, then a team (team leads), a person (everyone), a model and a
        # backend. Teams are few, so a filter on a person alone still skips most of a day. Months are partitions; with a
        # TTL, ClickHouse drops whole parts once every row in them has expired (ttl_only_drop_parts) rather than rewriting.
        return [
            f"CREATE DATABASE IF NOT EXISTS `{self.db}`",
            f"CREATE TABLE IF NOT EXISTS {self.requests} ({cols}) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) "
            f"ORDER BY (toDate(ts), team, user_id, served_model, backend, ts){' TTL ' + ttl if ttl else ''} "
            "SETTINGS allow_nullable_key = 1, ttl_only_drop_parts = 1",
            f"CREATE TABLE IF NOT EXISTS {self.rollup} ({rcols}) ENGINE = SummingMergeTree PARTITION BY toYYYYMM(hour) "
            f"ORDER BY (hour, team, user_id, served_model, backend){' TTL ' + rttl if rttl else ''} SETTINGS ttl_only_drop_parts = 1",
        ]

    async def migrate(self) -> list[str]:
        """Create what's missing and bring the rest up to date: new ledger columns, a changed rollup view, the TTLs.
        Returns what changed, CREATED first when there was no ledger table (an empty ClickHouse)."""
        existing = await self.ch.query("SELECT name FROM system.tables WHERE database = {db:String}", {"db": self.db})
        done = [] if any(r["name"] == "requests" for r in existing) else [CREATED]
        for sql in self.ddl():
            await self.ch.command(sql)
        have = await self.ch.query("SELECT table, name FROM system.columns WHERE database = {db:String}", {"db": self.db})
        cols = {(r["table"], r["name"]) for r in have}
        for table, types in (("requests", column_types()), ("usage_hourly", rollup_types())):
            for f, t in types:
                if (table, f) not in cols:
                    await self.ch.command(f"ALTER TABLE `{self.db}`.{table} ADD COLUMN IF NOT EXISTS `{f}` {t}")
                    done.append(f"added {table}.{f}")
        tables = {r["name"]: r["create_table_query"] for r in await self.ch.query(
            "SELECT name, create_table_query FROM system.tables WHERE database = {db:String}", {"db": self.db})}
        view = self.mv_name()
        for name in tables:
            if name.startswith("usage_hourly_mv_") and name != view:  # dropped first: two views would count every row twice
                await self.ch.command(f"DROP VIEW IF EXISTS `{self.db}`.`{name}`")
                done.append(f"dropped {name}")
        if view not in tables:
            await self.ch.command(f"CREATE MATERIALIZED VIEW IF NOT EXISTS `{self.db}`.`{view}` TO {self.rollup} AS {self.mv_select()}")
            done.append(f"created {view}")
        for table, expr, days in (("requests", "toDateTime(ts)", self.cfg.ttl_days), ("usage_hourly", "hour", self.cfg.rollup_ttl_days)):
            current = tables.get(table, "")
            if days and f"TTL {_ttl(expr, days)}" not in current:
                await self.ch.command(f"ALTER TABLE `{self.db}`.{table} MODIFY TTL {_ttl(expr, days)}")
                done.append(f"{table} keeps {days} days")
            elif not days and " TTL " in current:
                await self.ch.command(f"ALTER TABLE `{self.db}`.{table} REMOVE TTL")
                done.append(f"{table} keeps everything")
        return done

    async def sizes(self) -> dict[str, dict]:
        rows = await self.ch.query("SELECT table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes FROM system.parts "
                                   "WHERE database = {db:String} AND active GROUP BY table", {"db": self.db})
        return {r["table"]: {"rows": int(r["rows"]), "bytes_on_disk": int(r["bytes"])} for r in rows}

    # ------------------------------------------------------------ reports (twins of PostgresStore's)

    async def summary(self, start: datetime, end: datetime, group_by: str, user_id: str | None = None,
                      team: str | None = None, gpu_backends: Sequence[str] = ()) -> list[dict]:
        tag = _TAG.match(group_by)
        use_rollup = not tag and (end - start) > ROLLUP_AFTER
        table, ts_col = (self.rollup, "hour") if use_rollup else (self.requests, "ts")
        params: dict[str, Any] = {"start": start, "end": end}
        if tag:
            params["tag"] = tag.group(1)
            key = "if(mapContains(tags, {tag:String}), tags[{tag:String}], '(untagged)')"
        elif group_by == "none":
            key = "'total'"
        elif group_by in ("day", "hour", "month"):
            key = {"day": "toStartOfDay", "hour": "toStartOfHour", "month": "toStartOfMonth"}[group_by] + f"({ts_col})"
        elif use_rollup:
            key = f"nullIf({GROUPS[group_by]}, '')"  # the rollup keeps a missing key as '': NULL, as in the ledger and Postgres
        else:
            key = GROUPS[group_by]
        if use_rollup:
            counts = "sum(requests) AS requests, sum(errors) AS errors, sum(upgraded) AS upgraded"
        else:
            counts = "count() AS requests, countIf(ifNull(status, 0) >= 400) AS errors, countIf(ifNull(upgraded, false)) AS upgraded"
        where = [f"{ts_col} >= {{start:{TS}}}", f"{ts_col} < {{end:{TS}}}"]
        if user_id:
            params["user"] = user_id
            where.append("user_id = {user:String}")
        if team:
            params["team"] = team
            where.append("team = {team:String}")
        sums = ", ".join(f"sum({f}) AS {f}" for f in SUM_FIELDS)
        params["gpus"] = list(gpu_backends)
        gpu = "sumIf(actual_cost, ifNull(backend, '') IN {gpus:Array(String)}) AS gpu_cost"  # see PostgresStore.summary
        # The sums are aliased with their columns' names; gpu_cost needs the column, not the sum (ClickHouse would read
        # actual_cost in sumIf as the alias, an aggregate inside an aggregate).
        rows = await self.ch.query(f"SELECT {key} AS key, {counts}, {sums}, {gpu} FROM {table} WHERE {' AND '.join(where)} GROUP BY key "
                                   "SETTINGS prefer_column_name_to_alias = 1", params)
        out = []
        for r in rows:
            if group_by in ("day", "hour", "month"):
                r["key"] = parse_time(r["key"])
            out.append(_finish_row({k: (float(v) if k in SUM_FIELDS or k == "gpu_cost" else v) for k, v in r.items()}))
        return sort_summary(out)

    async def export_rows(self, start: datetime, end: datetime, team: str | None = None) -> AsyncIterator[dict]:
        """Rows in time order, a day at a time: the table is sorted by day first, so ClickHouse sorts one day, not the
        range. Ties in time are broken by id as text, as Postgres orders UUIDs."""
        cols = ", ".join(LEDGER_FIELDS)
        day = start
        while day < end:
            nxt = min(datetime.combine(day.astimezone(UTC).date(), datetime.min.time(), tzinfo=UTC) + timedelta(days=1), end)
            sql = (f"SELECT {cols} FROM {self.requests} WHERE ts >= {{start:{TS}}} AND ts < {{end:{TS}}}"
                   + (" AND team = {team:String}" if team else "") + " ORDER BY ts, toString(id)")
            async for r in self.ch.stream(sql, {"start": day, "end": nxt, **({"team": team} if team else {})}):
                yield decode_row(r)
            day = nxt

    async def demand_by_hour(self, since: datetime) -> list[dict]:
        mix = ", ".join(f"sum({f}_tokens) AS {f}" for f in ("input", "cache_read", "cache_write", "output"))
        rows = await self.ch.query(
            f"SELECT toStartOfHour(ts) AS hour, floor, served_model, requested_model, sum(served_api_cost) AS cost, "
            f"sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens) AS tokens, count() AS requests, {mix} "
            f"FROM {self.requests} WHERE ts >= {{since:{TS}}} AND NOT ifNull(upgraded, false) AND ifNull(status, 0) < 400 "
            "AND ifNull(service_tier, '') != 'batch' GROUP BY hour, floor, served_model, requested_model", {"since": since})
        for r in rows:
            r["hour"] = parse_time(r["hour"])
            r["cost"] = float(r["cost"])
        return rows

    async def ledger_costs(self, start: datetime, end: datetime, backends: list[str]) -> list[dict]:
        sums = ", ".join(f"sum({f}) AS {f}" for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "actual_cost"))
        rows = await self.ch.query(
            f"SELECT toDate(ts) AS day, backend, ifNull(served_model, '') AS served_model, ifNull(upstream_model, '') AS upstream_model, "
            f"ifNull(service_tier, '') AS service_tier, count() AS requests, {sums} FROM {self.requests} "
            f"WHERE ts >= {{start:{TS}}} AND ts < {{end:{TS}}} AND backend IN {{backends:Array(String)}} "
            "GROUP BY day, backend, served_model, upstream_model, service_tier",
            {"start": start, "end": end, "backends": list(backends)})
        for r in rows:
            r["day"] = date.fromisoformat(r["day"])
            r["actual_cost"] = float(r["actual_cost"])
        return rows

    # ------------------------------------------------------------ what the copy needs

    async def minute_counts(self, start: datetime, end: datetime) -> dict[datetime, tuple[int, int]]:
        """Rows and distinct ids per minute: more rows than ids means a row was copied twice."""
        rows = await self.ch.query(f"SELECT toStartOfMinute(ts) AS m, count() AS n, uniqExact(id) AS u FROM {self.requests} "
                                   f"WHERE ts >= {{start:{TS}}} AND ts < {{end:{TS}}} GROUP BY m", {"start": start, "end": end})
        return {parse_time(r["m"]): (int(r["n"]), int(r["u"])) for r in rows}

    async def hour_rollups(self, start: datetime, end: datetime) -> dict[datetime, int]:
        rows = await self.ch.query(f"SELECT hour, sum(requests) AS n FROM {self.rollup} WHERE hour >= {{start:{TS}}} "
                                   f"AND hour < {{end:{TS}}} GROUP BY hour", {"start": start, "end": end})
        return {parse_time(r["hour"]): int(r["n"]) for r in rows}

    async def ids_in(self, start: datetime, end: datetime, minutes: list[datetime]) -> set[str]:
        rows = await self.ch.query(f"SELECT toString(id) AS id FROM {self.requests} WHERE ts >= {{start:{TS}}} AND ts < {{end:{TS}}} "
                                   "AND toStartOfMinute(ts) IN {minutes:Array(DateTime('UTC'))}",
                                   {"start": start, "end": end, "minutes": sorted(minutes)})
        return {r["id"] for r in rows}

    async def insert(self, rows: list[dict]) -> None:
        await self.ch.insert(self.requests, list(LEDGER_FIELDS), row_binary(rows))

    async def delete_hour(self, hour: datetime) -> None:
        """Remove an hour from both tables, so it can be copied again whole. Lightweight deletes: the rows are marked
        at once and dropped by later merges; the rollup view doesn't see deletes, so its hour goes too."""
        end = hour + timedelta(hours=1)
        await self.ch.command(f"DELETE FROM {self.requests} WHERE ts >= {{start:{TS}}} AND ts < {{end:{TS}}}", {"start": hour, "end": end})
        await self.ch.command(f"DELETE FROM {self.rollup} WHERE hour = {{start:{TS}}}", {"start": hour})


# ---------------------------------------------------------------------------- the copy


def floor_hour(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def ceil_hour(t: datetime) -> datetime:
    f = floor_hour(t)
    return f if f == t else f + timedelta(hours=1)


@dataclass
class HourPlan:
    """What one hour needs: nothing ("ok"), its missing rows ("copy": `minutes` to read from Postgres, of which `partial`
    already have some rows in ClickHouse), copying again whole ("repair": duplicates, or a rollup that disagrees), or
    nothing it can do ("ahead": ClickHouse holds rows Postgres doesn't, e.g. after old partitions were dropped there)."""

    action: str
    minutes: list[datetime] = field(default_factory=list)
    partial: list[datetime] = field(default_factory=list)


def plan_hour(pg: dict[datetime, int], ch: dict[datetime, tuple[int, int]], rollup: int) -> HourPlan:
    """Compare one hour minute by minute: Postgres's row counts, ClickHouse's rows and distinct ids, and its rollup."""
    if any(u > pg.get(m, 0) for m, (_, u) in ch.items()):
        return HourPlan("ahead")
    if any(n > u for n, u in ch.values()) or rollup != sum(n for n, _ in ch.values()):
        return HourPlan("repair")
    missing = sorted(m for m, n in pg.items() if n > ch.get(m, (0, 0))[0])
    if not missing:
        return HourPlan("ok")
    return HourPlan("copy", missing, [m for m in missing if ch.get(m, (0, 0))[0] > 0])


def _by_hour(pg: dict[datetime, int], ch: dict[datetime, tuple[int, int]]) -> dict[datetime, tuple[dict, dict]]:
    hours: dict[datetime, tuple[dict, dict]] = {}
    for m, n in pg.items():
        hours.setdefault(floor_hour(m), ({}, {}))[0][m] = n
    for m, nu in ch.items():
        hours.setdefault(floor_hour(m), ({}, {}))[1][m] = nu
    return hours


@dataclass
class CopyResult:
    copied: int = 0
    repaired: list[str] = field(default_factory=list)
    ahead: list[str] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)  # hours another copy held: checked again next time

    def add(self, other: "CopyResult") -> None:
        self.copied += other.copied
        self.repaired += other.repaired
        self.ahead += other.ahead
        self.busy += other.busy


class Copier:
    """Copies the ledger from Postgres to ClickHouse, an hour at a time, under a Postgres lock on the hour so two
    workers (or a worker and a backfill) never copy the same rows."""

    def __init__(self, store, reporting: "Reporting"):
        self.store, self.reporting, self.ledger = store, reporting, reporting.ledger

    async def _sync_hour(self, hour: datetime, wait: bool) -> CopyResult:
        res = CopyResult()
        end = hour + timedelta(hours=1)
        async with self.store.ledger_hour(hour, wait) as pg:
            if pg is None:
                res.busy.append(hour.isoformat())
                return res
            counts = await pg.minutes()
            plan = plan_hour(counts, await self.ledger.minute_counts(hour, end), (await self.ledger.hour_rollups(hour, end)).get(hour, 0))
            if plan.action == "ok":
                return res
            if plan.action == "ahead":
                res.ahead.append(hour.isoformat())
                return res
            skip: set[str] = set()
            minutes: list[datetime] | None = None  # None: every row of the hour
            if plan.action == "repair":
                await self.ledger.delete_hour(hour)
                res.repaired.append(hour.isoformat())
            else:
                minutes = None if len(plan.minutes) == len(counts) else plan.minutes
                if plan.partial:
                    skip = await self.ledger.ids_in(hour, end, plan.partial)
            batch: list[dict] = []
            async for row in pg.rows(minutes):
                if str(row["id"]) in skip:
                    continue
                batch.append(row)
                if len(batch) >= self.reporting.cfg.batch_rows:
                    await self.ledger.insert(batch)
                    res.copied += len(batch)
                    batch = []
            if batch:
                await self.ledger.insert(batch)
                res.copied += len(batch)
        return res

    async def sync(self, start: datetime, end: datetime, wait: bool = False) -> CopyResult:
        """Make the hours from `start` to `end` the same on both sides. A cheap comparison of counts first (one query each
        side), then only the hours that differ are locked, compared again and copied. Whole hours, so a row stamped a
        little ahead of this machine's clock is compared like any other."""
        res = CopyResult()
        start, end = floor_hour(start), ceil_hour(end)
        pg = await self.store.ledger_minutes(start, end)
        ch = await self.ledger.minute_counts(start, end)
        rollups = await self.ledger.hour_rollups(start, end)
        hours = _by_hour(pg, ch)
        for hour in rollups:
            hours.setdefault(hour, ({}, {}))
        for hour in sorted(hours):
            p, c = hours[hour]
            if plan_hour(p, c, rollups.get(hour, 0)).action != "ok":
                res.add(await self._sync_hour(hour, wait))
        return res

    async def run_once(self, now: datetime | None = None) -> CopyResult:
        """One worker cycle: everything since the copy was last complete, and always the last 3 hours, a day at a time.
        Progress is saved after each day, so a long catch-up after an outage keeps what it did."""
        now = now or datetime.now(UTC)
        total = CopyResult()
        try:
            await self.reporting.ensure_schema()  # first: an empty ClickHouse starts the progress over
            state = await self.store.get_setting(STATE_KEY) or {}
            through = parse_time(state.get("through"))
            start = floor_hour(min(through, now - RECHECK) if through else now - RECHECK)
            covered = (await self.covered(parse_time(state.get("from")) or start)).isoformat()  # a first copy: from where it starts
            while start < now:
                chunk_end = min(start + timedelta(days=1), now)
                res = await self.sync(start, chunk_end)
                total.add(res)
                patch: dict[str, Any] = {"checked_at": now.isoformat(), "error": None, "copied": total.copied,
                                         "repaired": total.repaired[-24:], "ahead": total.ahead[-24:], "busy": total.busy}
                if not res.busy:
                    patch.update({"through": chunk_end.isoformat(), "from": covered})
                await self.store.patch_setting(STATE_KEY, patch)
                if res.busy:
                    break
                start = chunk_end
        except Exception as e:
            self.reporting.schema_unknown()  # it may have lost its tables (a restart without its volume): check again next time
            await self.store.patch_setting(STATE_KEY, {"error": str(e)[:500], "error_at": now.isoformat()})
            raise
        self.reporting.forget_state()
        return total

    async def backfill(self, start: datetime, end: datetime, progress=None) -> tuple[CopyResult, str | None]:
        """Copy [start, end) whole days at a time, waiting for hours the worker is copying. Idempotent: what's already in
        ClickHouse stays, and only what's missing (or duplicated) is copied. Returns the result and a note when the range
        doesn't join what ClickHouse already covers."""
        await self.reporting.ensure_schema()
        total = CopyResult()
        day = start
        while day < end:
            nxt = min(day + timedelta(days=1), end)
            res = await self.sync(day, nxt, wait=True)
            total.add(res)
            if progress:
                progress(day, nxt, res)
            day = nxt
        state = await self.store.get_setting(STATE_KEY) or {}
        covered = parse_time(state.get("from"))
        joined = covered is None or start < covered <= end  # this range reaches what ClickHouse held already
        frm = await self.covered(start if joined else covered)
        note = None
        if covered is None:
            await self.store.patch_setting(STATE_KEY, {"from": frm.isoformat(), "through": min(end, datetime.now(UTC)).isoformat()})
        elif frm != covered:
            await self.store.patch_setting(STATE_KEY, {"from": frm.isoformat()})
        elif end < covered:
            note = (f"ClickHouse holds every row from {covered:%Y-%m-%d %H:%M} UTC on. Reports start reading it before that once the "
                    f"gap is copied too: backfill --start {end:%Y-%m-%d} --end {covered + timedelta(days=1):%Y-%m-%d}")
        self.reporting.forget_state()
        return total, note

    async def covered(self, start: datetime) -> datetime:
        """Where ClickHouse's copy starts, given that it holds every row from `start` on: the beginning when Postgres has
        no row before `start` (a new ledger, a backfill from its first day, or old partitions dropped once copied), so a
        report from any date reads it; else `start`."""
        first = await self.store.first_ledger_ts()
        return EVERYTHING if first is None or first >= start else start


# ---------------------------------------------------------------------------- which store answers


class Reporting:
    """One process's ClickHouse: the client, the ledger tables, and the decision whether a report reads them."""

    def __init__(self, cfg, store, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg, self.store = cfg, store
        self.client = ClickHouse(cfg, transport)
        self.ledger = Ledger(self.client, cfg)
        self._state: dict | None = None
        self._state_at = -1e9
        self.failed_at = -1e9
        self.error: str | None = None
        self._schema_ok = False

    async def ensure_schema(self) -> list[str]:
        if self._schema_ok:
            return []
        changes = await self.ledger.migrate()
        if changes:
            log.info("ClickHouse schema: %s", "; ".join(changes))
        if CREATED in changes:
            # The progress is kept in Postgres and outlives the ClickHouse it describes: a new server, another database, or
            # one that lost its data holds none of it. Start over, so reports read Postgres until the rows are there again.
            await self.store.patch_setting(STATE_KEY, {"from": None, "through": None})
            self.forget_state()
        self._schema_ok = True
        return changes

    def schema_unknown(self) -> None:
        self._schema_ok = False

    def forget_state(self) -> None:
        self._state_at = -1e9

    async def state(self) -> dict:
        """The copy's progress as the worker last saved it (re-read at most every STATE_SECONDS)."""
        if time.monotonic() - self._state_at > STATE_SECONDS:
            self._state = await self.store.get_setting(STATE_KEY) or {}
            self._state_at = time.monotonic()
        return self._state or {}

    def max_lag(self) -> timedelta:
        return timedelta(seconds=max(300.0, 3 * self.cfg.copy_seconds))

    async def why_not(self, start: datetime, big: bool = True, raw: bool = True, now: datetime | None = None) -> str | None:
        """Why a report from `start` on doesn't read ClickHouse, or None when it does. `big`: a long range or a tag
        breakdown; `raw`: it reads ledger rows rather than the hourly rollup."""
        now = now or datetime.now(UTC)
        if self.cfg.reads == "none":
            return "reporting.clickhouse.reads is none: ClickHouse gets the copy, Postgres answers every report"
        if not big and self.cfg.reads != "all":
            return "short ranges read Postgres"
        if time.monotonic() - self.failed_at < RETRY_SECONDS:
            return f"ClickHouse failed just now: {self.error}"
        state = await self.state()
        covered, through = parse_time(state.get("from")), parse_time(state.get("through"))
        if covered is None or through is None:
            return "nothing has been copied yet (the worker copies new rows; meritorder clickhouse backfill copies older ones)"
        if now - through > self.max_lag():
            return f"the copy is behind: complete up to {through:%Y-%m-%d %H:%M} UTC" + (f" ({state['error']})" if state.get("error") else "")
        if start < covered:
            return f"ClickHouse holds rows from {covered:%Y-%m-%d %H:%M} UTC on (meritorder clickhouse backfill copies earlier ones)"
        days = self.cfg.ttl_days if raw else self.cfg.rollup_ttl_days
        if days and start < now - timedelta(days=days - 1):
            return f"ClickHouse keeps {'ledger rows' if raw else 'hourly rollups'} {days} days (reporting.clickhouse.{'ttl_days' if raw else 'rollup_ttl_days'})"
        return None

    async def reader(self, start: datetime, big: bool, raw: bool = True, now: datetime | None = None) -> Ledger | None:
        return None if await self.why_not(start, big, raw, now=now) else self.ledger

    def failed(self, e: Exception) -> None:
        self.failed_at, self.error = time.monotonic(), str(e)
        log.warning("ClickHouse failed, reports read Postgres for %ds: %s", RETRY_SECONDS, e)

    async def describe(self, now: datetime | None = None) -> dict:
        """What the console's Config page shows: the settings (never the password), whether ClickHouse answers, how far
        the copy has got, and the tables' size."""
        now = now or datetime.now(UTC)
        c = self.cfg
        out: dict[str, Any] = {"url": c.url, "database": c.database, "user": c.user, "password_set": bool(c.auth.key()),
                               "reads": c.reads, "ttl_days": c.ttl_days, "rollup_ttl_days": c.rollup_ttl_days,
                               "copy_seconds": c.copy_seconds, "ok": False, "version": None, "error": None, "latency_ms": None,
                               "tables": {}}
        t0 = time.monotonic()
        try:
            out["version"] = await self.client.version()
            out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            out["ok"] = True
            out["tables"] = await self.ledger.sizes()
        except ClickHouseError as e:
            out["error"] = str(e)
        self.forget_state()
        state = await self.state()
        through = parse_time(state.get("through"))
        out["copy"] = {**state, "behind_seconds": round((now - through).total_seconds()) if through else None}
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        out["answers"] = None if not out["ok"] else await self.why_not(month, big=True, raw=False, now=now)
        out["answering"] = out["ok"] and out["answers"] is None
        if not out["ok"]:
            out["answers"] = "ClickHouse can't be reached"
        return out

    async def close(self) -> None:
        await self.client.close()


def update(current: Reporting | None, cfg, store, transport: httpx.AsyncBaseTransport | None = None) -> Reporting | None:
    """The Reporting for a (re)loaded configuration: the same one while `reporting.clickhouse` is unchanged, a new one when
    it changes (the old client is closed), none when it's gone."""
    if current is not None and cfg is not None and current.cfg == cfg:
        return current
    if current is not None:
        try:
            asyncio.get_running_loop().create_task(current.close())
        except RuntimeError:  # no event loop: nothing to close it on, and nothing is using it
            pass
    return Reporting(cfg, store, transport) if cfg is not None else None
