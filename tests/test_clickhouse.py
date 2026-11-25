"""The ClickHouse copy of the ledger without a ClickHouse: the column types, values in and out, the HTTP client (against
a mocked transport), the schema it creates, the copy's decisions (against an in-memory stand-in for ClickHouse's side),
which store answers which report, the config, the CLI, the worker and the console. test_clickhouse_live.py runs the same
code against real Postgres and ClickHouse."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from conftest import ADMIN_KEY, TEST_CONFIG, TEST_KEY, auth, ledger_row

from meritorder import cli, worker
from meritorder import clickhouse as chm
from meritorder.clickhouse import (COLUMN_TYPES, EVERYTHING, STATE_KEY, ClickHouse, ClickHouseError, Copier, Ledger, Reporting, column_types,
                                   decode_row, parse_time, plan_hour, rollup_types, row_binary)
from meritorder.config import ClickHouseConfig, build_config
from meritorder.configstore import secret_problems
from meritorder.store import LEDGER_FIELDS, SAVINGS_FIELDS, SUM_FIELDS, MemoryStore, PostgresStore, answered_by, collect_answers, complete_row

NOW = datetime(2026, 11, 13, 10, 30, tzinfo=UTC)
HOUR = datetime(2026, 11, 13, 10, tzinfo=UTC)


def cfg(**kw) -> ClickHouseConfig:
    return ClickHouseConfig(**{"url": "http://ch.test:8123", "database": "mo", **kw})


def minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------- types and values


def test_every_ledger_column_has_a_clickhouse_type(monkeypatch):
    assert [f for f, _ in column_types()] == LEDGER_FIELDS
    assert dict(column_types())["ts"] == "DateTime64(6, 'UTC')" and dict(column_types())["tags"] == "Map(String, String)"
    roll = dict(rollup_types())
    assert roll["hour"] == "DateTime('UTC')" and roll["requests"] == "UInt64" and all(f in roll for f in SUM_FIELDS)
    # A column added to the ledger without a ClickHouse type fails here (and so the schema can't be built) until it has one.
    monkeypatch.setattr(chm, "LEDGER_FIELDS", [*LEDGER_FIELDS, "carbon_grams"])
    with pytest.raises(KeyError, match="carbon_grams"):
        column_types()


def test_values_in_and_out():
    assert chm._param(datetime(2026, 11, 13, 5, 6, 7, 89, tzinfo=UTC)) == "2026-11-13 05:06:07.000089"
    assert chm._param("a\\b\tc\nd\re") == "a\\\\b\\tc\\nd\\re"
    assert chm._param(["x'y", datetime(2026, 1, 2, 3, 4, tzinfo=UTC)]) == "['x\\\\'y','2026-01-02 03:04:00']"
    assert chm._param(7) == "7"
    assert parse_time("2026-11-01") == datetime(2026, 11, 1, tzinfo=UTC)
    assert parse_time("2026-11-13 05:00:00") == datetime(2026, 11, 13, 5, tzinfo=UTC)
    assert parse_time("2026-11-13 05:00:00.000123") == datetime(2026, 11, 13, 5, 0, 0, 123, tzinfo=UTC)
    assert parse_time("2026-11-13T05:00:00+00:00") == datetime(2026, 11, 13, 5, tzinfo=UTC)
    assert parse_time(None) is None and parse_time(NOW) is NOW
    back = decode_row({"id": "00000000-0000-0000-0000-000000000001", "ts": "2026-11-13 10:30:00.000000", "tags": {"p": "x"}})
    assert back["id"] == uuid.UUID(int=1) and back["ts"] == NOW


# The same values as ClickHouse 26.9 itself writes them in RowBinary (SELECT ... FORMAT RowBinary), byte for byte.
ROW_BINARY = [
    ("UUID", uuid.UUID("80188cdb-bb42-4e20-af56-58be7ac047ad"), "204e42bbdb8c1880ad47c07abe5856af"),
    ("DateTime64(6, 'UTC')", datetime(2026, 11, 11, 9, 30, 43, 71422, tzinfo=UTC), "becd57d9fc5f0600"),
    ("Nullable(String)", None, "01"),
    ("LowCardinality(Nullable(String))", "héllo", "000668c3a96c6c6f"),
    ("Map(String, String)", {"project": "x", "k": ""}, "020770726f6a6563740178016b00"),
    ("Nullable(Int32)", 429, "00ad010000"), ("Nullable(Int32)", None, "01"), ("Nullable(Bool)", True, "0001"),
    ("Int64", -97617, "af82feffffffffff"), ("Float64", 6.010613289052169e-05, "ae371c004f830f3f"),
]


def test_rows_go_to_clickhouse_as_exact_bytes():
    for t, value, want in ROW_BINARY:
        assert chm._encoder(t)(value).hex() == want, t
    assert {t for _, t in column_types()} <= {t for t, _, _ in ROW_BINARY} | {"Nullable(Bool)"}  # every ledger type is written
    for t in set(COLUMN_TYPES.values()):
        chm._encoder(t)  # and has an encoder
    # A tag value that isn't text is kept as JSON, as in the Parquet export; a missing one is left out.
    assert chm._tags({"p": "x", "n": 3, "gone": None}) == chm._tags({"p": "x", "n": "3"})
    row = complete_row({"id": uuid.UUID(int=1), "ts": NOW, "user_id": "a", "status": 200})
    body = row_binary([row, row])
    assert len(body) % 2 == 0 and body[: len(body) // 2] == body[len(body) // 2:] and body.startswith(struct.pack("<QQ", 0, 1))


def test_rows_are_written_as_each_column_would_be():
    """row_binary packs the runs of token counts and costs together: byte for byte what each column's encoder writes."""
    rows = [complete_row({"id": uuid.uuid4(), "ts": NOW, "user_id": "ünïcode", "team": "t" * 200, "status": 529, "error": "x" * 127,
                          "route_reason": "y" * 128, "tags": {"project": "p", "n": 3, "gone": None}, "input_tokens": 2**40,
                          "actual_cost": 0.1 + 0.2, "savings_batch": 1e-17, "latency_ms": None, "stream": True, "upgraded": False}),
            complete_row({"id": str(uuid.UUID(int=7)), "ts": NOW, "served_model": None, "output_tokens": None, "baseline_cost": None})]
    each = b"".join(chm._encoder(t)(r.get(f)) for r in rows for f, t in column_types())
    assert row_binary(rows) == each
    assert len([p for p in chm._row_plan() if p[0] is not None]) == 4  # tokens, costs, the batch saving, OCR pages


# ---------------------------------------------------------------------------- the HTTP client


class FakeServer:
    """ClickHouse's HTTP interface, scripted: records each request and answers from `rules` (a substring of the SQL ->
    rows, or an httpx.Response)."""

    def __init__(self, rules: dict | None = None):
        self.rules = rules or {}
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()}
        body = request.content.decode(errors="replace")
        sql = params.get("query") or body
        self.requests.append({"sql": sql, "params": params, "body": body, "raw": request.content, "headers": dict(request.headers)})
        for needle, answer in self.rules.items():
            if needle in sql:
                if isinstance(answer, httpx.Response):
                    return answer
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(200, text="".join(json.dumps(r) + "\n" for r in answer))
        return httpx.Response(200, text="")

    def sql(self) -> list[str]:
        return [r["sql"] for r in self.requests]


def client(server: FakeServer, **kw) -> ClickHouse:
    return ClickHouse(cfg(**kw), transport=httpx.MockTransport(server))


async def test_queries_send_sql_values_settings_and_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_PASSWORD", "s3cret")
    server = FakeServer({"SELECT x": [{"x": 1}, {"x": 2}]})
    ch = client(server, user="mo")
    assert await ch.query("SELECT x FROM t WHERE a = {a:String}", {"a": "it's"}) == [{"x": 1}, {"x": 2}]
    req = server.requests[0]
    assert req["sql"].endswith("FORMAT JSONEachRow") and req["params"]["param_a"] == "it's"
    assert req["params"]["output_format_json_quote_64bit_integers"] == "0" and req["params"]["wait_end_of_query"] == "1"
    assert req["headers"]["x-clickhouse-user"] == "mo" and req["headers"]["x-clickhouse-key"] == "s3cret"
    # A secret file wins over the variable, and is read again when it changes.
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    ch2 = client(server, auth={"value_env": "MERITORDER_CLICKHOUSE_PASSWORD", "value_file": str(secret)})
    await ch2.command("SELECT 1")
    assert server.requests[-1]["headers"]["x-clickhouse-key"] == "from-file"
    monkeypatch.delenv("MERITORDER_CLICKHOUSE_PASSWORD")
    await client(server).command("SELECT 1")
    assert "x-clickhouse-key" not in server.requests[-1]["headers"]  # no password at all
    await ch.close()


async def test_requests_to_clickhouse_stay_out_of_the_info_log(caplog):
    """httpx logs every request at INFO, with the URL: for ClickHouse that's each query's settings and values, and a whole
    INSERT statement, several times a second during a backfill. Those show at DEBUG only; other hosts' requests as before."""
    ch = client(FakeServer())
    other = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with caplog.at_level(logging.INFO):
        await ch.command("SELECT 1")
        await ch.insert("mo.requests", ["id"], b"")
        await other.get("https://openrouter.ai/api/v1/models")
    assert "ch.test" not in caplog.text and "openrouter.ai" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await ch.command("SELECT 1")
    assert "HTTP Request: POST http://ch.test:8123" in caplog.text
    await ch.close()
    await other.aclose()


async def test_errors_become_clickhouse_errors():
    server = FakeServer({"bad": httpx.Response(500, text="Code: 62. DB::Exception: Syntax error\nmore", headers={"x-clickhouse-exception-code": "62"}),
                         "auth": httpx.Response(401, text=""), "down": httpx.ConnectError("refused")})
    ch = client(server)
    with pytest.raises(ClickHouseError, match="error 62: Code: 62. DB::Exception: Syntax error$"):
        await ch.query("bad")
    with pytest.raises(ClickHouseError, match="HTTP 401: HTTP 401"):
        await ch.command("auth")
    with pytest.raises(ClickHouseError, match="can't reach ClickHouse at http://ch.test:8123: refused"):
        await ch.query("down")


async def test_streams_and_inserts():
    server = FakeServer({"SELECT rows": [{"a": 1}, {"a": 2}],
                         "SELECT broken": httpx.Response(200, text='{"a": 1}\nCode: 241. DB::Exception: Memory limit exceeded\n'),
                         "SELECT json-error": httpx.Response(200, text='{"exception": "Code: 241"}\n'),
                         "SELECT refused": httpx.Response(500, text="Code: 60. Unknown table"),
                         "SELECT gone": httpx.ReadError("reset"),
                         "version()": [{"v": "26.3.2.1"}]})
    ch = client(server)
    assert [r async for r in ch.stream("SELECT rows")] == [{"a": 1}, {"a": 2}]
    got = []
    with pytest.raises(ClickHouseError, match="failed while sending rows: Code: 241"):
        async for r in ch.stream("SELECT broken"):
            got.append(r)
    assert got == [{"a": 1}]  # the rows before the error came through; the export fails rather than ending early
    with pytest.raises(ClickHouseError, match="failed while sending rows"):
        [r async for r in ch.stream("SELECT json-error")]
    with pytest.raises(ClickHouseError, match="Unknown table"):
        [r async for r in ch.stream("SELECT refused")]
    with pytest.raises(ClickHouseError, match="can't reach"):
        [r async for r in ch.stream("SELECT gone")]
    await ch.insert("`mo`.requests", ["id", "n"], b"\x00\xff")
    ins = server.requests[-1]
    assert ins["sql"] == "INSERT INTO `mo`.requests (`id`, `n`) FORMAT RowBinary" and ins["raw"] == b"\x00\xff"
    assert ins["params"]["async_insert"] == "0"
    await ch.insert("`mo`.requests", ["id"], b"")
    assert server.requests[-1]["params"]["insert_deduplication_token"] != ins["params"]["insert_deduplication_token"]
    assert await ch.version() == "26.3.2.1"


# ---------------------------------------------------------------------------- schema and queries


async def test_schema_on_a_new_database():
    server = FakeServer()
    led = Ledger(client(server), cfg(ttl_days=90))
    assert await led.migrate() == [chm.CREATED] + [f"added requests.{f}" for f in LEDGER_FIELDS] + [
        f"added usage_hourly.{f}" for f, _ in rollup_types()] + [f"created {led.mv_name()}", "requests keeps 90 days"]
    sql = server.sql()
    assert sql[0].startswith("SELECT name FROM system.tables")  # whether the ledger's table was there before
    sql = sql[1:]
    assert sql[0] == "CREATE DATABASE IF NOT EXISTS `mo`"
    table = sql[1]
    assert table.startswith("CREATE TABLE IF NOT EXISTS `mo`.requests (`id` UUID, `ts` DateTime64(6, 'UTC'),")
    assert "PARTITION BY toYYYYMM(ts) ORDER BY (toDate(ts), team, user_id, served_model, backend, ts) TTL toDateTime(ts) + toIntervalDay(90)" in table
    assert "allow_nullable_key = 1" in table and "ENGINE = MergeTree" in table
    assert "ENGINE = SummingMergeTree PARTITION BY toYYYYMM(hour) ORDER BY (hour, team, user_id, served_model, backend)" in sql[2]
    view = next(s for s in sql if "MATERIALIZED VIEW" in s)
    assert "TO `mo`.usage_hourly AS SELECT toStartOfHour(toDateTime(ts)) AS hour, ifNull(user_id, '') AS user_id" in view
    assert "countIf(ifNull(status, 0) >= 400) AS errors" in view and "sum(savings_response_cache) AS savings_response_cache" in view


async def test_schema_follows_changes(monkeypatch):
    led = Ledger(client(FakeServer()), cfg())
    current = led.mv_name()
    tables = [{"name": "requests", "create_table_query": "CREATE TABLE ... TTL toDateTime(ts) + toIntervalDay(30) SETTINGS"},
              {"name": "usage_hourly", "create_table_query": "CREATE TABLE ... SETTINGS"},
              {"name": "usage_hourly_mv_0123456789", "create_table_query": "CREATE MATERIALIZED VIEW"}, {"name": current, "create_table_query": ""}]
    have = [{"table": t, "name": f} for t, cols in (("requests", column_types()), ("usage_hourly", rollup_types())) for f, _ in cols
            if f != "route_reason"]
    server = FakeServer({"system.columns": have, "system.tables": tables})
    led = Ledger(client(server), cfg(rollup_ttl_days=800))
    assert await led.migrate() == ["added requests.route_reason", "dropped usage_hourly_mv_0123456789", "requests keeps everything",
                                   "usage_hourly keeps 800 days"]
    assert "ALTER TABLE `mo`.requests ADD COLUMN IF NOT EXISTS `route_reason` Nullable(String)" in server.sql()
    assert "ALTER TABLE `mo`.usage_hourly MODIFY TTL hour + toIntervalDay(800)" in server.sql()
    # A changed rollup (say, a new cost column) gets a view of its own name, and the old one goes first.
    monkeypatch.setattr(chm, "SUM_FIELDS", [*SUM_FIELDS, "carbon_grams"])
    assert led.mv_name() != current
    with pytest.raises(ValueError, match="plain name"):
        Ledger(client(FakeServer()), ClickHouseConfig.model_construct(url="http://x", database="mo; DROP", ttl_days=None))


async def test_report_queries_read_back_what_postgres_returns():
    day = {"key": "2026-11-13 00:00:00", "requests": 2, "errors": 1, "upgraded": 0, **{f: 1 for f in SUM_FIELDS}}
    server = FakeServer({
        "toStartOfDay(hour)": [day],
        "toStartOfMonth(ts)": [{**day, "key": "2026-11-01"}],
        "mapContains": [{**day, "key": "x"}, {**day, "key": "(untagged)"}],
        "AS key": [{**day, "key": None}, {**day, "key": "b"}, {**day, "key": "a"}],
        "GROUP BY hour, floor": [{"hour": "2026-11-13 10:00:00", "floor": None, "served_model": "big", "requested_model": "auto", "cost": 1,
                                  "tokens": 9, "requests": 1, "input": 9, "cache_read": 0, "cache_write": 0, "output": 0}],
        "GROUP BY day, backend": [{"day": "2026-11-13", "backend": "b", "served_model": "", "upstream_model": "", "requests": 1,
                                   "input_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "actual_cost": 2}],
        "ORDER BY ts, toString(id)": [{"id": str(uuid.UUID(int=5)), "ts": "2026-11-13 10:00:00.000001", "tags": {}}],
        "uniqExact(id)": [{"m": "2026-11-13 10:01:00", "n": 2, "u": 1}],
        "sum(requests) AS n": [{"hour": "2026-11-13 10:00:00", "n": 3}],
        "toString(id) AS id": [{"id": "a"}, {"id": "b"}],
        "system.parts": [{"table": "requests", "rows": 5, "bytes": 1234}],
    })
    led = Ledger(client(server), cfg())
    long = await led.summary(NOW - timedelta(days=30), NOW, "day", user_id="alice", team="eng")  # the hourly rollup
    assert long[0]["key"] == "2026-11-13T00:00:00+00:00" and long[0]["input_tokens"] == 1.0 and long[0]["savings_total"] == float(len(SAVINGS_FIELDS))
    req = server.requests[-1]
    assert "FROM `mo`.usage_hourly" in req["sql"] and req["params"]["param_user"] == "alice" and req["params"]["param_team"] == "eng"
    assert (await led.summary(NOW - timedelta(days=3), NOW, "month"))[0]["key"] == "2026-11-01T00:00:00+00:00"
    assert "FROM `mo`.requests" in server.requests[-1]["sql"] and "countIf(ifNull(upgraded, false))" in server.requests[-1]["sql"]
    assert [r["key"] for r in await led.summary(NOW - timedelta(days=30), NOW, "tag:project")] == ["(untagged)", "x"]
    assert server.requests[-1]["params"]["param_tag"] == "project" and "FROM `mo`.requests" in server.requests[-1]["sql"]
    assert [r["key"] for r in await led.summary(NOW - timedelta(days=1), NOW, "user")] == ["a", "b", None]  # as sort_summary orders
    [d] = await led.demand_by_hour(NOW - timedelta(days=30))
    assert d["hour"] == HOUR and d["cost"] == 1.0 and isinstance(d["cost"], float)
    [c] = await led.ledger_costs(NOW - timedelta(days=30), NOW, ["b"])
    assert c["day"] == date(2026, 11, 13) and c["actual_cost"] == 2.0 and server.requests[-1]["params"]["param_backends"] == "['b']"
    rows = [r async for r in led.export_rows(datetime(2026, 11, 11, 12, tzinfo=UTC), datetime(2026, 11, 13, 6, tzinfo=UTC), team="eng")]
    exports = [r for r in server.requests if "ORDER BY ts, toString(id)" in r["sql"]]
    assert [(e["params"]["param_start"], e["params"]["param_end"]) for e in exports] == [  # a day at a time
        ("2026-11-11 12:00:00.000000", "2026-11-12 00:00:00.000000"), ("2026-11-12 00:00:00.000000", "2026-11-13 00:00:00.000000"),
        ("2026-11-13 00:00:00.000000", "2026-11-13 06:00:00.000000")]
    assert rows[0]["id"] == uuid.UUID(int=5) and rows[0]["ts"] == datetime(2026, 11, 13, 10, 0, 0, 1, tzinfo=UTC)
    assert exports[0]["params"]["param_team"] == "eng"
    assert await led.minute_counts(HOUR, HOUR + timedelta(hours=1)) == {datetime(2026, 11, 13, 10, 1, tzinfo=UTC): (2, 1)}
    assert await led.hour_rollups(HOUR, HOUR + timedelta(hours=1)) == {HOUR: 3}
    assert await led.ids_in(HOUR, HOUR + timedelta(hours=1), [HOUR + timedelta(minutes=2), HOUR]) == {"a", "b"}
    assert server.requests[-1]["params"]["param_minutes"] == "['2026-11-13 10:00:00','2026-11-13 10:02:00']"
    await led.insert([complete_row({"id": uuid.UUID(int=9), "ts": NOW})])
    assert server.requests[-1]["raw"].startswith(struct.pack("<QQ", 0, 9)) and server.requests[-1]["sql"].startswith(
        "INSERT INTO `mo`.requests (`id`, `ts`, `user_id`,")
    await led.delete_hour(HOUR)
    assert server.sql()[-2:] == ["DELETE FROM `mo`.requests WHERE ts >= {start:DateTime64(6, 'UTC')} AND ts < {end:DateTime64(6, 'UTC')}",
                                 "DELETE FROM `mo`.usage_hourly WHERE hour = {start:DateTime64(6, 'UTC')}"]
    assert await led.sizes() == {"requests": {"rows": 5, "bytes_on_disk": 1234}}


# ---------------------------------------------------------------------------- the copy's decisions


def test_plan_hour():
    m1, m2 = HOUR, HOUR + timedelta(minutes=1)
    assert plan_hour({m1: 2}, {m1: (2, 2)}, 2).action == "ok"
    p = plan_hour({m1: 2, m2: 3}, {m1: (1, 1)}, 1)
    assert (p.action, p.minutes, p.partial) == ("copy", [m1, m2], [m1])
    assert plan_hour({m1: 2}, {m1: (3, 2)}, 3).action == "repair"  # a row twice
    assert plan_hour({m1: 2}, {m1: (2, 2)}, 1).action == "repair"  # the rollup lost rows
    assert plan_hour({}, {}, 4).action == "repair"  # a rollup for rows that aren't there
    assert plan_hour({m1: 1}, {m1: (2, 2)}, 2).action == "ahead"  # Postgres lost rows: ClickHouse keeps them
    assert plan_hour({}, {}, 0).action == "ok"


class FakeLedger:
    """ClickHouse's side of the copy, in memory: its rows (a row can be there twice) and the rollup its view keeps."""

    def __init__(self):
        self.rows: list[dict] = []
        self.rollup: dict[datetime, int] = defaultdict(int)
        self.inserts: list[int] = []
        self.migrations = 0
        self.fail: Exception | None = None

    async def migrate(self):
        if self.fail:
            raise self.fail
        self.migrations += 1
        return ["created the tables"] if self.migrations == 1 else []

    async def minute_counts(self, start, end):
        out: dict[datetime, list] = {}
        for r in self.rows:
            if start <= r["ts"] < end:
                n, ids = out.setdefault(minute(r["ts"]), [0, set()])
                out[minute(r["ts"])] = [n + 1, ids | {str(r["id"])}]
        return {m: (n, len(ids)) for m, (n, ids) in out.items()}

    async def hour_rollups(self, start, end):
        return {h: n for h, n in self.rollup.items() if start <= h < end and n}

    async def ids_in(self, start, end, minutes):
        return {str(r["id"]) for r in self.rows if start <= r["ts"] < end and minute(r["ts"]) in set(minutes)}

    async def insert(self, rows):
        self.inserts.append(len(rows))
        for r in rows:
            self.rows.append(dict(r))
            self.rollup[r["ts"].replace(minute=0, second=0, microsecond=0)] += 1

    async def delete_hour(self, hour):
        self.rows = [r for r in self.rows if r["ts"].replace(minute=0, second=0, microsecond=0) != hour]
        self.rollup.pop(hour, None)

    async def sizes(self):
        return {"requests": {"rows": len(self.rows), "bytes_on_disk": 100 * len(self.rows)}}


class _Hour:
    def __init__(self, src, hour):
        self.src, self.start, self.end = src, hour, hour + timedelta(hours=1)

    async def minutes(self):
        return await self.src.ledger_minutes(self.start, self.end)

    async def rows(self, minutes=None):
        want = None if minutes is None else set(minutes)
        for r in sorted(self.src.ledger, key=lambda r: (r["ts"], str(r["id"]))):
            if self.start <= r["ts"] < self.end and (want is None or minute(r["ts"]) in want):
                yield dict(r)


class Source(MemoryStore):
    """Postgres's side of the copy over the in-memory ledger, with the per-hour lock."""

    kind = "postgres"

    def __init__(self):
        super().__init__()
        self.held: set[datetime] = set()

    async def ledger_minutes(self, start, end):
        out: dict[datetime, int] = {}
        for r in self.ledger:
            if start <= r["ts"] < end:
                out[minute(r["ts"])] = out.get(minute(r["ts"]), 0) + 1
        return out

    @asynccontextmanager
    async def ledger_hour(self, hour, wait=False):
        yield None if hour in self.held and not wait else _Hour(self, hour)

    async def first_ledger_ts(self):
        return min((r["ts"] for r in self.ledger), default=None)

    def use_clickhouse(self, c):
        """As PostgresStore's, with ClickHouse's side in memory (kept across reloads, like the real tables)."""
        self.fake = getattr(self, "fake", None) or FakeLedger()
        self.clickhouse = None if c is None else reporting(self, c)
        if self.clickhouse:
            self.clickhouse.ledger = self.fake


def reporting(store, c: ClickHouseConfig | None = None, server: FakeServer | None = None) -> Reporting:
    rep = Reporting(c or cfg(batch_rows=3), store, transport=httpx.MockTransport(server or FakeServer({"version()": [{"v": "26.3.2.1"}]})))
    rep.ledger = FakeLedger()
    return rep


async def add(store, *minutes_ago: float, base: datetime = NOW) -> list[dict]:
    rows = [ledger_row(ts=base - timedelta(minutes=m)) for m in minutes_ago]
    for r in rows:
        await store.record(r)
    return rows


async def test_backfill_copies_once_and_again_copies_nothing():
    src = Source()
    await add(src, *range(0, 3000, 7))  # two days, every 7 minutes
    rep = reporting(src)
    res, note = await Copier(src, rep).backfill(datetime(2026, 11, 11, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC))
    assert res.copied == len(src.ledger) and note is None and rep.ledger.migrations == 1
    assert max(rep.ledger.inserts) == 3  # batch_rows
    assert sorted(str(r["id"]) for r in rep.ledger.rows) == sorted(str(r["id"]) for r in src.ledger)
    state = await src.get_setting(STATE_KEY)
    assert state["from"] == EVERYTHING.isoformat() and state["through"]  # from the first row: all of it; the worker carries on
    again, _ = await Copier(src, rep).backfill(datetime(2026, 11, 11, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC))
    assert again.copied == 0 and again.repaired == [] and again.ahead == []


async def test_a_copy_of_the_whole_ledger_answers_every_range():
    """Set up as the docs say (a backfill from the ledger's first day, or ClickHouse from the start), ClickHouse answers a
    report that starts before the ledger's first row: Postgres has nothing earlier. Found on a fresh clone, where
    `meritorder clickhouse status` said Postgres answered every report after a full backfill."""
    src = Source()
    await add(src, *range(0, 3000, 7))  # from 2026-11-11 08:30
    rep = reporting(src)
    await Copier(src, rep).backfill(datetime(2026, 11, 11, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC))
    assert (await src.get_setting(STATE_KEY))["from"] == EVERYTHING.isoformat()
    rep.forget_state()
    assert await rep.why_not(NOW - timedelta(days=365), now=NOW) is None

    # A backfill from after the first row covers from where it starts; the rest is still Postgres's.
    src = Source()
    await add(src, *range(0, 3000, 7))
    rep = reporting(src)
    await Copier(src, rep).backfill(datetime(2026, 11, 12, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC))
    assert (await src.get_setting(STATE_KEY))["from"] == "2026-11-12T00:00:00+00:00"

    # The worker's first copy of a new ledger (nothing older than the 3 hours it compares) covers everything too...
    src = Source()
    await add(src, 1, 5, 70)
    rep = reporting(src)
    await Copier(src, rep).run_once(NOW)
    assert (await src.get_setting(STATE_KEY))["from"] == EVERYTHING.isoformat()
    # ...and on a ledger with history, from where it starts. Once the older rows are gone from Postgres (old partitions
    # dropped after a backfill), it covers everything again.
    src = Source()
    old = await add(src, 1, 600)
    rep = reporting(src)
    await Copier(src, rep).run_once(NOW)
    assert (await src.get_setting(STATE_KEY))["from"] == "2026-11-13T07:00:00+00:00"
    src.ledger = [r for r in src.ledger if r["id"] != old[1]["id"]]
    await Copier(src, rep).run_once(NOW + timedelta(minutes=1))
    assert (await src.get_setting(STATE_KEY))["from"] == EVERYTHING.isoformat()


async def test_backfill_joins_what_clickhouse_already_covers():
    src = Source()
    await add(src, 0, base=datetime(2026, 10, 1, tzinfo=UTC))  # history before every range here: coverage starts where copies start
    rep = reporting(src)
    await src.set_setting(STATE_KEY, {"from": "2026-11-10T00:00:00+00:00", "through": NOW.isoformat()})
    seen = []
    _, note = await Copier(src, rep).backfill(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC),
                                              lambda d0, d1, res: seen.append(d0.day))
    assert seen == [1, 2] and "backfill --start 2026-11-03 --end 2026-11-11" in note  # a gap: coverage stays where it was
    assert (await src.get_setting(STATE_KEY))["from"] == "2026-11-10T00:00:00+00:00"
    _, note = await Copier(src, rep).backfill(datetime(2026, 11, 3, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC))
    assert note is None and (await src.get_setting(STATE_KEY))["from"] == "2026-11-03T00:00:00+00:00"
    _, note = await Copier(src, rep).backfill(datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))  # inside: no change
    assert note is None and (await src.get_setting(STATE_KEY))["from"] == "2026-11-03T00:00:00+00:00"


async def test_each_cycle_copies_new_and_late_rows_and_repairs_hours():
    src = Source()
    rep = reporting(src)
    copier = Copier(src, rep)
    await add(src, 1, 5, 70)
    res = await copier.run_once(NOW)
    assert res.copied == 3
    state = await src.get_setting(STATE_KEY)
    assert state["from"] == EVERYTHING.isoformat() and state["through"] == NOW.isoformat() and state["error"] is None
    # A row stamped two hours ago lands late: the last 3 hours are always compared again.
    late = (await add(src, 120))[0]
    assert (await copier.run_once(NOW + timedelta(minutes=1))).copied == 1
    # Some rows of a minute are there already: only the others are sent.
    more = await add(src, 1)
    assert (await copier.run_once(NOW + timedelta(minutes=2))).copied == 1 and rep.ledger.inserts[-1] == 1
    # A row twice: the hour is copied again whole.
    rep.ledger.rows.append(dict(more[0]))
    rep.ledger.rollup[HOUR] += 1
    res = await copier.run_once(NOW + timedelta(minutes=3))
    assert res.repaired == [HOUR.isoformat()] and res.copied == 3  # the hour's three rows
    after = await rep.ledger.minute_counts(HOUR, HOUR + timedelta(hours=1))
    assert {m: n for m, (n, _) in after.items()} == await src.ledger_minutes(HOUR, HOUR + timedelta(hours=1))
    # The rollup lost an hour (its view was being replaced): copied again too.
    rep.ledger.rollup[HOUR] -= 2
    assert (await copier.run_once(NOW + timedelta(minutes=4))).repaired == [HOUR.isoformat()]
    # A row Postgres lost: ClickHouse keeps it, and the state says so.
    src.ledger = [r for r in src.ledger if r["id"] != late["id"]]
    res = await copier.run_once(NOW + timedelta(minutes=5))
    assert res.ahead == [(HOUR - timedelta(hours=2)).isoformat()] and (await src.get_setting(STATE_KEY))["ahead"] == res.ahead


async def test_cycles_catch_up_after_an_outage_and_skip_hours_another_copy_holds():
    src = Source()
    rep = reporting(src)
    await src.set_setting(STATE_KEY, {"from": "2026-11-01T00:00:00+00:00", "through": (NOW - timedelta(days=2)).isoformat()})
    await add(src, *range(0, 3 * 24 * 60, 120))
    res = await Copier(src, rep).run_once(NOW)
    copied = sum(1 for r in src.ledger if r["ts"] >= (NOW - timedelta(days=2)).replace(minute=0))
    assert res.copied == copied and (await src.get_setting(STATE_KEY))["through"] == NOW.isoformat()
    # Another copy holds an hour: it's left for next time, and the copy isn't complete past it.
    await add(src, 3)
    src.held.add(HOUR)
    res = await Copier(src, rep).run_once(NOW + timedelta(minutes=1))
    assert res.busy == [HOUR.isoformat()] and res.copied == 0
    state = await src.get_setting(STATE_KEY)
    assert state["through"] == NOW.isoformat() and state["busy"] == [HOUR.isoformat()]
    src.held.clear()
    assert (await Copier(src, rep).run_once(NOW + timedelta(minutes=2))).copied == 1


async def test_an_empty_clickhouse_starts_the_copy_over():
    """The copy's progress lives in Postgres, so it outlives the ClickHouse it describes: a new server, another database, or
    one that lost its data starts with no tables. Reports must not read it for days it doesn't hold."""
    src = Source()
    await add(src, 1, 5 * 24 * 60)  # a row now and one five days ago
    await src.set_setting(STATE_KEY, {"from": "2026-11-01T00:00:00+00:00", "through": (NOW - timedelta(minutes=1)).isoformat()})
    server = FakeServer()  # no tables at all
    rep = Reporting(cfg(database="fresh"), src, transport=httpx.MockTransport(server))
    res = await Copier(src, rep).run_once(NOW)
    assert res.copied == 1  # the last 3 hours, as every cycle
    rep.forget_state()
    assert "ClickHouse holds rows from 2026-11-13 07:00 UTC on" in await rep.why_not(NOW - timedelta(days=7), now=NOW)
    _, note = await Copier(src, rep).backfill(datetime(2026, 11, 8, tzinfo=UTC), NOW)  # a backfill joins it up again
    assert note is None
    rep.forget_state()
    assert await rep.why_not(NOW - timedelta(days=5), now=NOW) is None
    await rep.close()


async def test_tables_lost_while_the_worker_runs_are_made_again_without_a_restart():
    """A ClickHouse restarted without its volume, or a dropped database: the next cycle that fails checks the schema again."""
    src = Source()
    await add(src, 1)
    server = FakeServer({"system.tables": [{"name": "requests", "create_table_query": ""}, {"name": "usage_hourly", "create_table_query": ""}]})
    rep = Reporting(cfg(), src, transport=httpx.MockTransport(server))
    await Copier(src, rep).run_once(NOW)
    server.rules = {"INSERT INTO": httpx.Response(404, text="Code: 60. DB::Exception: Table mo.requests does not exist")}
    await add(src, 0.5)
    with pytest.raises(ClickHouseError, match="does not exist"):
        await Copier(src, rep).run_once(NOW + timedelta(minutes=1))
    server.rules, seen = {}, len(server.requests)  # ClickHouse answers again, with no tables
    await Copier(src, rep).run_once(NOW + timedelta(minutes=2))
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS `mo`.requests") for s in server.sql()[seen:])
    await rep.close()


async def test_a_failed_cycle_is_saved_for_the_console():
    src = Source()
    rep = reporting(src)
    rep.ledger.fail = ClickHouseError("can't reach ClickHouse at http://ch.test:8123: refused")
    with pytest.raises(ClickHouseError):
        await Copier(src, rep).run_once(NOW)
    state = await src.get_setting(STATE_KEY)
    assert state["error"].startswith("can't reach ClickHouse") and state["error_at"] == NOW.isoformat()
    rep.ledger.fail = None
    await Copier(src, rep).run_once(NOW)
    assert (await src.get_setting(STATE_KEY))["error"] is None


# ---------------------------------------------------------------------------- which store answers


async def test_when_clickhouse_answers():
    src = Source()
    rep = reporting(src, cfg(ttl_days=30, rollup_ttl_days=400, copy_seconds=600))
    month = NOW.replace(day=1, hour=0, minute=30)
    assert "nothing has been copied yet" in await rep.why_not(month, now=NOW)
    await src.set_setting(STATE_KEY, {"from": "2026-10-01T00:00:00+00:00", "through": (NOW - timedelta(hours=1)).isoformat(),
                                      "error": "timed out"})
    rep.forget_state()
    assert await rep.why_not(month, now=NOW) == "the copy is behind: complete up to 2026-11-13 09:30 UTC (timed out)"
    assert rep.max_lag() == timedelta(minutes=30)  # 3 copy intervals, at least 5 minutes
    await src.set_setting(STATE_KEY, {"from": "2026-10-01T00:00:00+00:00", "through": NOW.isoformat()})
    rep.forget_state()
    assert await rep.why_not(month, now=NOW) is None and await rep.reader(month, True, now=NOW) is rep.ledger
    assert await rep.why_not(month, big=False) == "short ranges read Postgres"
    assert "ClickHouse holds rows from 2026-10-01 00:00 UTC on" in await rep.why_not(datetime(2026, 9, 1, tzinfo=UTC), now=NOW)
    assert "keeps ledger rows 30 days" in await rep.why_not(NOW - timedelta(days=30), now=NOW)  # its TTL may have dropped some
    assert await rep.why_not(NOW - timedelta(days=30), raw=False, now=NOW) is None  # the rollup is kept longer
    rep.failed(ClickHouseError("boom"))
    assert await rep.why_not(month, now=NOW) == "ClickHouse failed just now: boom" and await rep.reader(month, True, now=NOW) is None
    rep.cfg = cfg(reads="none")
    assert "reads is none" in await rep.why_not(month)
    rep.cfg = cfg(reads="all")
    rep.failed_at = -1e9
    assert await rep.why_not(month, big=False, now=NOW) is None


async def test_describe_for_the_console(monkeypatch):
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_PASSWORD", "s3cr3t-xyz")
    src = Source()
    rep = reporting(src)
    await src.set_setting(STATE_KEY, {"from": "2026-10-01T00:00:00+00:00", "through": (NOW - timedelta(seconds=12)).isoformat()})
    d = await rep.describe(now=NOW)
    assert d["ok"] and d["version"] == "26.3.2.1" and d["password_set"] and "s3cr3t-xyz" not in json.dumps(d)
    assert d["copy"]["behind_seconds"] == 12 and d["answering"] and d["answers"] is None and d["tables"]["requests"]["rows"] == 0
    down = reporting(src, server=FakeServer({"version()": httpx.ConnectError("refused")}))
    d = await down.describe(now=NOW)
    assert not d["ok"] and "refused" in d["error"] and not d["answering"] and "can't be reached" in d["answers"]
    await rep.close()


async def test_a_new_configuration_replaces_the_client():
    store = Source()
    a = chm.update(None, cfg(), store)
    assert chm.update(a, cfg(), store) is a
    closed = []
    a.close = lambda: closed.append(1) or asyncio.sleep(0)
    b = chm.update(a, cfg(reads="all"), store)
    await asyncio.sleep(0)
    assert b is not a and closed == [1]
    assert chm.update(b, None, store) is None
    c = Reporting(cfg(), store)
    c.close = lambda: closed.append(2) or asyncio.sleep(0)

    def outside_a_loop():
        return chm.update(c, None, store)

    assert await asyncio.to_thread(outside_a_loop) is None  # nothing to close it on there: it's left to the collector


class Answers:
    """A ClickHouse ledger for PostgresStore's routing, answering with what it was asked."""

    def __init__(self, fail: Exception | None = None, fail_after: int | None = None):
        self.fail, self.fail_after, self.calls = fail, fail_after, []

    async def summary(self, *args, **kw):
        self.calls.append(("summary", args, kw))
        if self.fail:
            raise self.fail
        return [{"key": "from-clickhouse"}]

    async def demand_by_hour(self, since):
        return [{"hour": since}]

    async def ledger_costs(self, start, end, backends):
        return [{"backends": backends}]

    async def export_rows(self, start, end, team=None):
        for i in range(3):
            if self.fail and self.fail_after == i:
                raise self.fail
            yield {"n": i}


class EmptyExport:
    async def export_rows(self, start, end, team=None):
        return
        yield


async def test_postgres_store_asks_clickhouse_first():
    store = PostgresStore("postgresql://unused")
    rep = reporting(Source())
    store.clickhouse = rep
    ans = Answers()
    picked = []

    async def reader(start, big, raw=True):
        picked.append((big, raw))
        return ans

    rep.reader = reader
    seen = collect_answers()
    assert await store.summary(NOW - timedelta(days=30), NOW, "team", user_id="a") == [{"key": "from-clickhouse"}]
    assert ans.calls[-1] == ("summary", (NOW - timedelta(days=30), NOW, "team"), {"user_id": "a", "team": None, "gpu_backends": ()})
    await store.summary(NOW - timedelta(days=1), NOW, "tag:project")
    assert picked == [(True, False), (True, True)] and seen == {"clickhouse"} and answered_by() == "clickhouse"
    assert await store.demand_by_hour(NOW - timedelta(days=30)) == [{"hour": NOW - timedelta(days=30)}] and picked[-1] == (True, True)
    assert await store.ledger_costs(NOW - timedelta(days=2), NOW, ["b"]) == [{"backends": ["b"]}] and picked[-1] == (False, True)
    assert [r async for r in store.export_rows(NOW - timedelta(days=30), NOW)] == [{"n": 0}, {"n": 1}, {"n": 2}]
    # ClickHouse failing after the first rows: the export fails (half an export would look complete).
    rep.reader = lambda *a, **k: asyncio.sleep(0, Answers(ClickHouseError("gone"), fail_after=1))
    with pytest.raises(ClickHouseError):
        [r async for r in store.export_rows(NOW - timedelta(days=30), NOW)]
    # Failing before the first row: Postgres exports it all.
    store.pool = FakePool(FakeConn())
    rep.reader = lambda *a, **k: asyncio.sleep(0, Answers(ClickHouseError("gone"), fail_after=0))
    seen = collect_answers()
    assert [r async for r in store.export_rows(NOW - timedelta(days=30), NOW, team="eng")] == [{"id": uuid.UUID(int=1), "ts": HOUR}]
    assert seen == {"postgres"} and rep.error == "gone" and store.pool.conn.sql[-1][1][2] == "eng"
    rep.reader = lambda *a, **k: asyncio.sleep(0, EmptyExport())
    seen = collect_answers()
    assert [r async for r in store.export_rows(NOW - timedelta(days=30), NOW)] == [] and seen == {"clickhouse"}
    store.pool = None
    # A failing query: Postgres answers (here there's no pool, so it fails there), and ClickHouse rests a while.
    for err in (ClickHouseError("down"), KeyError("odd reply")):
        rep.reader = reader
        ans.fail = err
        rep.failed_at = -1e9
        assert await store._from_clickhouse(NOW, True, True, lambda ch: ch.summary()) is None
        assert rep.error == str(err)
    store.clickhouse = None
    assert await store._from_clickhouse(NOW, True, True, lambda ch: ch.summary()) is None
    store.use_clickhouse(cfg())
    assert isinstance(store.clickhouse, Reporting)
    await store.close()


class FakeConn:
    """Enough of an asyncpg connection to see what the copy asks Postgres (test_clickhouse_live.py runs it for real)."""

    def __init__(self, locked: bool = False):
        self.locked, self.sql = locked, []

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        return [{"m": HOUR, "n": 3}]

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        return not self.locked if "try_advisory" in sql else HOUR

    async def execute(self, sql, *args):
        self.sql.append((sql, args))

    @asynccontextmanager
    async def transaction(self):
        yield

    async def cursor(self, sql, *args, prefetch=None):
        self.sql.append((sql, args))
        yield {"id": uuid.UUID(int=1), "ts": HOUR}


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_what_the_copy_asks_postgres():
    from meritorder.store import COPY_LOCK

    conn = FakeConn()
    store = PostgresStore("postgresql://unused")
    store.pool = FakePool(conn)
    assert await store.ledger_minutes(HOUR, HOUR + timedelta(hours=1)) == {HOUR: 3}
    assert "date_trunc('minute', ts)" in conn.sql[-1][0] and conn.sql[-1][1] == (HOUR, HOUR + timedelta(hours=1))
    assert await store.first_ledger_ts() == HOUR
    await store.patch_setting(STATE_KEY, {"through": "x"})
    assert "SET value = settings.value || EXCLUDED.value" in conn.sql[-1][0] and conn.sql[-1][1] == (STATE_KEY, {"through": "x"})
    slot = int(HOUR.timestamp()) // 3600
    async with store.ledger_hour(HOUR) as hour:
        assert conn.sql[-1] == ("SELECT pg_try_advisory_xact_lock($1::int4, $2::int4)", (COPY_LOCK, slot))
        assert await hour.minutes() == {HOUR: 3}
        assert [r async for r in hour.rows()] == [{"id": uuid.UUID(int=1), "ts": HOUR}]
        assert conn.sql[-1][0].endswith("WHERE ts >= $1 AND ts < $2 ORDER BY ts, id") and conn.sql[-1][1] == (HOUR, HOUR + timedelta(hours=1))
        [r async for r in hour.rows([HOUR])]
        assert "date_trunc('minute', ts) = ANY($3::timestamptz[])" in conn.sql[-1][0] and conn.sql[-1][1][2] == [HOUR]
    async with store.ledger_hour(HOUR, wait=True) as hour:
        assert conn.sql[-1] == ("SELECT pg_advisory_xact_lock($1::int4, $2::int4)", (COPY_LOCK, slot)) and hour is not None
    store.pool = FakePool(FakeConn(locked=True))
    async with store.ledger_hour(HOUR) as hour:
        assert hour is None  # another copy holds it


async def test_memory_store_ignores_clickhouse(caplog):
    store = MemoryStore()
    with caplog.at_level(logging.WARNING):
        store.use_clickhouse(cfg())
        store.use_clickhouse(cfg())
    assert caplog.text.count("ClickHouse needs Postgres") == 1 and store.clickhouse is None and store.clickhouse_ignored
    await store.patch_setting("k", {"a": 1})
    await store.patch_setting("k", {"b": 2})
    assert await store.get_setting("k") == {"a": 1, "b": 2}
    seen = collect_answers()
    await store.summary(NOW - timedelta(days=1), NOW, "none")
    assert seen == {"memory"}


# ---------------------------------------------------------------------------- reports API and console


async def test_reports_say_which_store_answered(client, gateway):
    await gateway.store.record(ledger_row(user_id="alice", team="default"))
    kind = gateway.store.kind
    r = await client.get("/v1/reports/summary", params={"group_by": "user"}, headers=auth(TEST_KEY))
    assert r.headers["x-meritorder-answered-by"] == kind and r.json()["answered_by"] == kind
    r = await client.get("/v1/reports/export.jsonl", headers=auth(ADMIN_KEY))
    assert r.headers["x-meritorder-answered-by"] == kind and len(r.text.splitlines()) == 1
    assert (await client.get("/v1/reports/requests", headers=auth(TEST_KEY))).json()["answered_by"] == kind
    r = await client.get("/v1/reports/levels", headers=auth(TEST_KEY))
    assert "x-meritorder-answered-by" not in r.headers  # rules and prices only: no stored data
    assert "x-meritorder-answered-by" not in (await client.get("/healthz")).headers
    empty = await client.get("/v1/reports/export.jsonl", params={"start": "2020-01-01", "end": "2020-01-02"}, headers=auth(ADMIN_KEY))
    assert empty.status_code == 200 and empty.text == ""


async def test_console_reporting_status(client, gateway):
    r = await client.get("/v1/admin/reporting", headers=auth(ADMIN_KEY))
    d = r.json()
    assert d["store"] == gateway.store.kind and d["postgres"]["ok"] and d["configured"] is False and d["clickhouse"] is None
    assert (await client.get("/v1/admin/reporting", headers=auth(TEST_KEY))).status_code == 403
    gateway.cfg.reporting.clickhouse = cfg(url="http://127.0.0.1:9", timeout_seconds=2)
    gateway.follow_reporting(gateway.cfg)
    d = (await client.get("/v1/admin/reporting", headers=auth(ADMIN_KEY))).json()
    assert d["configured"]
    if gateway.store.kind == "memory":
        assert "ClickHouse needs Postgres" in d["note"] and d["clickhouse"] is None
    else:
        assert d["clickhouse"]["ok"] is False and "can't reach ClickHouse" in d["clickhouse"]["error"]

    async def broken():
        raise OSError("no route to database")

    gateway.store.ping = broken
    assert (await client.get("/v1/admin/reporting", headers=auth(ADMIN_KEY))).json()["postgres"] == {"ok": False, "error": "no route to database"}


# ---------------------------------------------------------------------------- configuration


def test_configuration(monkeypatch, tmp_path):
    raw = {"backends": {}, "models": {}, "reporting": {"clickhouse": {"url": "${MERITORDER_CLICKHOUSE_URL:-}"}}}
    monkeypatch.delenv("MERITORDER_CLICKHOUSE_URL", raising=False)
    assert build_config(raw, tmp_path).reporting.clickhouse is None  # unset: off
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_URL", "https://ch.example.com:8443/")
    c = build_config(raw, tmp_path).reporting.clickhouse
    assert c.url == "https://ch.example.com:8443" and c.database == "meritorder" and c.reads == "large" and c.ttl_days is None
    assert c.auth.value_env == "MERITORDER_CLICKHOUSE_PASSWORD"
    for bad in ({"url": "ch.example.com"}, {"url": "http://x", "database": "a-b"}, {"url": "http://x", "ttl_days": 0}, {"url": "http://x", "reads": "some"}):
        with pytest.raises(ValueError):
            build_config({**raw, "reporting": {"clickhouse": bad}}, tmp_path)
    assert secret_problems({"reporting": {"clickhouse": {"url": "http://x", "auth": {"value": "hunter2"}}}}) == [
        "reporting.clickhouse.auth.value holds a literal password; use value_env or value_file"]
    assert "holds a password" in secret_problems({"reporting": {"clickhouse": {"url": "https://u:p@ch.example.com"}}})[0]
    assert secret_problems({"reporting": {"clickhouse": {"url": "${MERITORDER_CLICKHOUSE_URL:-}",
                                                         "auth": {"value": "${MERITORDER_CLICKHOUSE_PASSWORD}"}}}}) == []
    # The shipped configuration turns it on from the environment, as Docker Compose and the Helm chart do.
    from meritorder.config import load_config

    shipped = load_config("config/meritorder.yaml").reporting.clickhouse
    assert shipped is not None and shipped.url == "https://ch.example.com:8443"
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_URL", "")
    assert load_config("config/meritorder.yaml").reporting.clickhouse is None


def test_a_password_in_the_url_is_refused_wherever_it_comes_from(monkeypatch, tmp_path):
    """The stored configuration refuses a literal one (secret_problems), but the shipped config reads the URL from
    MERITORDER_CLICKHOUSE_URL, and ClickHouse Cloud hands out URLs with the password in them. Taken as it is, the password
    would show on the console's Config → Reporting, in `meritorder clickhouse` output and in every "can't reach" error."""
    raw = {"backends": {}, "models": {}, "reporting": {"clickhouse": {"url": "${MERITORDER_CLICKHOUSE_URL:-}"}}}
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_URL", "https://default:pw42@ch:8443")
    assert secret_problems(raw) == []  # a reference to a variable: nothing literal to refuse
    with pytest.raises(ValueError, match="reporting.clickhouse.url holds a password") as e:
        build_config(raw, tmp_path)
    assert "pw42" not in str(e.value)  # nor does the error repeat it
    monkeypatch.setenv("MERITORDER_CLICKHOUSE_URL", "https://abc.clickhouse.cloud:8443")
    assert build_config(raw, tmp_path).reporting.clickhouse.url == "https://abc.clickhouse.cloud:8443"


# ---------------------------------------------------------------------------- CLI and worker


def test_cli(monkeypatch, capsys):
    monkeypatch.delenv("MERITORDER_CLICKHOUSE_PASSWORD", raising=False)
    src = Source()
    monkeypatch.setattr(cli, "_store", lambda: src)

    class Follower:
        def __init__(self, store, path=None):
            pass

        async def current(self):
            return build_config({"backends": {}, "models": {}, "reporting": {"clickhouse": c}} if c else {"backends": {}, "models": {}})

    monkeypatch.setattr("meritorder.configstore.ConfigFollower", Follower)
    c = None
    with pytest.raises(SystemExit, match="reporting.clickhouse isn't set"):
        cli.main(["clickhouse", "status"])
    c = {"url": "http://ch.test:8123", "batch_rows": 2}
    cli.main(["clickhouse", "backfill"])
    assert "the ledger is empty" in capsys.readouterr().out
    asyncio.run(add(src, 30, 60 * 24 + 5, base=datetime.now(UTC)))
    cli.main(["clickhouse", "backfill"])
    out = capsys.readouterr().out
    assert "copying the ledger from" in out and "done: 2 rows copied, 0 hours copied again" in out
    cli.main(["clickhouse", "backfill", "--start", "2026-01-01", "--end", "2026-01-02"])
    out = capsys.readouterr().out
    assert "  2026-01-01: 0 rows copied" in out and "done: 0 rows copied" in out and "gap" not in out  # it holds all of it already
    for args, msg in ((["--start", "2026-13-01"], "--start must be YYYY-MM-DD"), (["--start", "2026-01-02", "--end", "2026-01-01"], "after")):
        with pytest.raises(SystemExit, match=msg):
            cli.main(["clickhouse", "backfill", *args])
    cli.main(["clickhouse", "status"])
    out = capsys.readouterr().out
    assert "ClickHouse http://ch.test:8123, database meritorder (user default, no password)" in out
    assert "reachable: version 26.3.2.1" in out and "ledger rows: 2 (200 bytes on disk)" in out
    assert "copy: every row in the ledger, complete up to" in out and "reports: large reports read ClickHouse" in out


def test_cli_status_lines(capsys):
    base = {"url": "http://x", "database": "mo", "user": "u", "password_set": True, "ok": False, "error": "refused", "version": None,
            "latency_ms": None, "tables": {}, "copy": {"error": "refused", "error_at": "2026-11-13T10:00:00+00:00"}, "answering": False,
            "answers": "ClickHouse can't be reached"}
    cli._print_clickhouse(base)
    out = capsys.readouterr().out
    assert "not reachable: refused" in out and "nothing copied yet" in out and "last copy failed at 2026-11-13T10:00:00+00:00: refused" in out
    assert "Postgres answers them all: ClickHouse can't be reached" in out
    cli._print_clickhouse({**base, "copy": {"from": "2026-10-01T00:00:00+00:00", "through": "2026-11-13T10:00:00+00:00", "behind_seconds": 4}})
    assert "copy: every row from 2026-10-01T00:00:00+00:00 on, complete up to 2026-11-13T10:00:00+00:00, 4 s behind" in capsys.readouterr().out
    assert [cli._size(n) for n in (999, 1_500, 2_500_000, 3e9, 4e12, 5e15)] == ["999 bytes", "1.5 KB", "2.5 MB", "3.0 GB", "4.0 TB", "5,000.0 TB"]


async def test_worker_copy(monkeypatch, caplog):
    src = Source()
    await worker.copy_once(src)  # off: nothing to do
    src.use_clickhouse(cfg())
    await add(src, 1, base=datetime.now(UTC))
    with caplog.at_level(logging.INFO):
        await worker.copy_once(src)
    assert "copied 1 ledger rows to ClickHouse" in caplog.text
    for err, logged in ((ClickHouseError("refused"), "failed: refused"), (RuntimeError("bug"), "Traceback")):
        async def broken(self, now=None, err=err):
            raise err

        monkeypatch.setattr(Copier, "run_once", broken)
        caplog.clear()
        await worker.copy_once(src)
        assert logged in caplog.text

    class Stop(Exception):
        pass

    naps = []

    async def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            raise Stop

    monkeypatch.setattr(worker.asyncio, "sleep", nap)
    with pytest.raises(Stop):
        await worker.copy_loop(src)
    src.clickhouse = None
    naps.clear()
    with pytest.raises(Stop):
        await worker.copy_loop(src)
    assert naps == [worker.IDLE_SECONDS, worker.IDLE_SECONDS]


async def test_worker_cycle_follows_reporting(monkeypatch):
    from test_worker_controller import pg_memory_store

    store = pg_memory_store()
    monkeypatch.setattr(worker, "make_store", lambda url: store)

    class Idle:  # no live prices, scores or bills here: nothing leaves the machine
        async def maybe(self, *args, **kw):
            return None

    for name in ("PriceRefresher", "QualityRefresher", "BillRefresher"):
        monkeypatch.setattr(worker, name, Idle)
    copies = []

    async def copy_once(s):
        copies.append(s.clickhouse_ignored)

    monkeypatch.setattr(worker, "copy_once", copy_once)
    raw = {**yaml.safe_load(yaml.safe_dump(TEST_CONFIG)), "reporting": {"clickhouse": {"url": "http://ch.test:8123"}}}
    await worker.run_worker(once=True, cfg=build_config(raw, "."))
    assert copies == [True]  # the configuration reached the store, then the copy ran once
