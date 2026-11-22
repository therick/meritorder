"""Background jobs: monthly ledger partitions, hourly rollups for reports, this month's spend counters checked against
the ledger, alerts, live prices and quality scores, providers'
bills to check the ledger against, eval runs queued from the console, training the learned router, the providers'
batch jobs (checked until they end, then settled into the ledger) and batch work on self-hosted GPUs, retention and key
rotation for the Responses API's stored mode, and with `reporting.clickhouse` the ledger's copy in ClickHouse. With
MERITORDER_WORKER_METRICS_PORT set, it serves the batch requests it settles as Prometheus metrics there (``GET /metrics``)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta

from .alerts import check_budgets, check_spikes, check_unpriced, weekly_digest
from .batches.poller import BatchPoller, WorkerContext, batch_loop, serve_metrics
from .billing import BillRefresher
from .config import Config
from .configstore import ConfigFollower
from .counters import reconcile as reconcile_spend
from .evals.jobs import worker_loop as eval_loop
from .benchmark import Watcher as BenchmarkWatcher
from .learned_jobs import RouterTrainer
from .live_prices import PriceRefresher
from .quality_data import QualityRefresher
from .state import make_state
from .store import make_store

log = logging.getLogger("meritorder.worker")
IDLE_SECONDS = 30  # how often the copy loop looks again while reporting.clickhouse isn't set


async def copy_once(store) -> None:
    """One copy of new ledger rows to ClickHouse (clickhouse.Copier), when reporting.clickhouse is set. A failure is
    saved with the copy's progress, where the console shows it; the next cycle tries again."""
    from .clickhouse import ClickHouseError, Copier

    rep = store.clickhouse
    if rep is None:
        return
    try:
        res = await Copier(store, rep).run_once()
    except ClickHouseError as e:
        log.warning("copying the ledger to ClickHouse failed: %s", e)
        return
    except Exception:
        log.exception("copying the ledger to ClickHouse failed")
        return
    if res.copied or res.repaired or res.ahead:
        log.info("copied %d ledger rows to ClickHouse%s%s", res.copied, f"; copied again: {', '.join(res.repaired)}" if res.repaired else "",
                 f"; ClickHouse has rows Postgres doesn't for {', '.join(res.ahead)}" if res.ahead else "")


async def copy_loop(store) -> None:
    """The copy runs on its own schedule (reporting.clickhouse.copy_seconds), so catching up after an outage doesn't hold
    up alerts and prices, and they don't hold up the copy."""
    while True:
        await copy_once(store)
        rep = store.clickhouse
        await asyncio.sleep(rep.cfg.copy_seconds if rep else IDLE_SECONDS)


async def maintain_stored(ctx: WorkerContext, cfg: Config) -> dict | None:
    """Stored responses past their team's retention are deleted (with the providers' copies), background responses whose
    gateway stopped are marked failed, and values an old key sealed are sealed again with the current one. Nothing to do
    while no team has stored mode on and nothing is stored."""
    from .stored.service import StoredResponses

    await ctx.use(cfg)
    try:
        return await StoredResponses(ctx).maintain(ctx.backends)
    except Exception:
        log.exception("stored responses: maintenance failed")
        return None


async def run_worker(once: bool = False, interval: int = 60, cfg: Config | None = None, backfill: bool = False, path: str | None = None,
                     metrics_port: int | None = None) -> None:
    store = make_store(os.environ.get("MERITORDER_DATABASE_URL"))
    if store.kind != "postgres":
        raise SystemExit("the worker needs Postgres: set MERITORDER_DATABASE_URL")
    state = make_state(os.environ.get("MERITORDER_REDIS_URL"))
    await store.init()
    follower = ConfigFollower(store, path=path, cfg=cfg)
    prices = PriceRefresher()
    quality = QualityRefresher()
    bills = BillRefresher()
    router = RouterTrainer()
    benchmark = BenchmarkWatcher()  # catalog events, canaries and level recommendations (decision 59)
    evals = copier = None
    if not once and os.environ.get("MERITORDER_EVAL_RUNS", "on").lower() not in ("0", "off", "false", "no"):
        # Eval runs started on the console's Evals page. They call the gateway like any client would.
        evals = asyncio.create_task(eval_loop(store, os.environ.get("MERITORDER_GATEWAY_URL", "http://127.0.0.1:8080")))
    # Batch jobs at the providers and batch work on GPUs, every 15 seconds (a lease per job lets several workers share
    # them); --once does one pass. The batch requests this worker settles are its metrics.
    ctx = WorkerContext(store, state)
    port = metrics_port if metrics_port is not None else int(os.environ.get("MERITORDER_WORKER_METRICS_PORT") or 0)
    server = await serve_metrics(ctx.metrics, port) if port and not once else None
    if server is not None:
        log.info("worker metrics on :%d/metrics", port)
    batches = asyncio.create_task(batch_loop(store, state, follower, ctx=ctx)) if not once else None
    once_batches = ctx if once else None
    stored = WorkerContext(store, state)  # the Responses API's stored mode: retention, stale background responses, key rotation
    try:
        while True:
            try:
                cfg = await follower.current()
                store.use_clickhouse(cfg.reporting.clickhouse)
                if not once and copier is None:
                    copier = asyncio.create_task(copy_loop(store))
                await store.ensure_partitions()
                rows = await store.rollup(full=backfill)
                backfill = False
                log.info("rollup refreshed %d hourly rows", rows)
                await reconcile_spend(store, state, cfg, lease_seconds=max(5, interval - 5))  # Redis's counters back to the ledger
                await check_budgets(cfg, store, state)
                await check_unpriced(cfg, state)
                await check_spikes(store, state)
                await prices.maybe(cfg, store, state, time.monotonic())
                await quality.maybe(cfg, store, state, time.monotonic())
                await bills.maybe(cfg, store, state)
                await router.maybe(cfg, store, state)
                await benchmark.maybe(cfg, store, state, raw=((await store.active_config()) or {}).get("body") or follower.file_raw)
                await maintain_stored(stored, cfg)
                await store.purge_mcp_calls(datetime.now(UTC) - timedelta(days=cfg.mcp.log_days))  # the MCP tool-call log
                if once_batches is not None:
                    await once_batches.use(cfg)
                    await BatchPoller(once_batches).run_once()
                if once:
                    await copy_once(store)  # after the batch pass, so the rows it settled are copied too
                now = datetime.now(UTC)
                if now.weekday() == 0 and now.hour >= 9:  # Monday morning, once per week
                    await weekly_digest(cfg, store, state, now)
            except Exception:
                log.exception("worker cycle failed")
            if once:
                return
            await asyncio.sleep(interval)
    finally:
        for task in (evals, copier, batches):
            if task is not None:
                task.cancel()
        if server is not None:
            server.close()
        if once_batches is not None:
            await once_batches.close()
        await stored.close()
        await store.close()
        await state.close()
