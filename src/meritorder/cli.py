"""The ``meritorder`` command."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .harness import KEY_FILE, save_key, saved


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from .env without overriding variables already set."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):  # quoted: up to the closing quote; anything after it is a comment
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()  # `KEY=50  # note`, as Docker Compose reads it
        if key and value and key not in os.environ:
            os.environ[key] = value


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _store():
    from .store import make_store

    store = make_store(os.environ.get("MERITORDER_DATABASE_URL"))
    if store.kind != "postgres":
        raise SystemExit("this command needs Postgres: set MERITORDER_DATABASE_URL")
    return store


def _saved() -> dict:
    return saved()


def _gateway(args) -> str:
    return (getattr(args, "gateway", None) or os.environ.get("MERITORDER_URL") or _saved().get("gateway") or "http://localhost:8080").rstrip("/")


def _key(args) -> str:
    key = getattr(args, "key", None) or os.environ.get("MERITORDER_API_KEY") or _saved().get("api_key")
    if not key:
        raise SystemExit("no API key: pass --key, set MERITORDER_API_KEY, or run 'meritorder login'")
    return key


# --------------------------------------------------------------------- commands


def cmd_serve(args) -> None:
    import uvicorn

    from .guards import max_body_bytes

    if args.config:
        os.environ["MERITORDER_CONFIG"] = args.config
    workers = args.workers or int(os.environ.get("MERITORDER_WORKERS", "1"))
    if workers > 1 and not (os.environ.get("MERITORDER_DATABASE_URL") and os.environ.get("MERITORDER_REDIS_URL")):
        raise SystemExit("--workers > 1 needs Postgres and Redis (MERITORDER_DATABASE_URL, MERITORDER_REDIS_URL): "
                         "worker processes share state through them")
    port = args.port or int(os.environ.get("MERITORDER_PORT") or 8080)  # .env's port, as Docker Compose publishes it
    uvicorn.run("meritorder.app:create_app", factory=True, host=args.host, port=port, log_level=args.log_level.lower(),
                proxy_headers=True, workers=workers, ws_max_size=max_body_bytes())


def cmd_migrate(args) -> None:
    async def run():
        store = _store()
        await store.init()
        await store.migrate()
        await store.close()
        print("database is up to date")

    asyncio.run(run())


def cmd_worker(args) -> None:
    from .worker import run_worker

    asyncio.run(run_worker(once=args.once, interval=args.interval, path=args.config, backfill=args.backfill, metrics_port=args.metrics_port))


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1000
    return ""  # pragma: no cover


def _print_clickhouse(d: dict) -> None:
    from .clickhouse import EVERYTHING

    print(f"ClickHouse {d['url']}, database {d['database']} (user {d['user']}, {'password set' if d['password_set'] else 'no password'})")
    print(f"  reachable: version {d['version']}, {d['latency_ms']} ms" if d["ok"] else f"  not reachable: {d['error']}")
    tables = d.get("tables") or {}
    for name, label in (("requests", "ledger rows"), ("usage_hourly", "hourly rollup rows")):
        if name in tables:
            print(f"  {label}: {tables[name]['rows']:,} ({_size(tables[name]['bytes_on_disk'])} on disk)")
    copy = d.get("copy") or {}
    if copy.get("from"):
        behind = f", {copy['behind_seconds']:,} s behind" if copy.get("behind_seconds") is not None else ""
        frm = "in the ledger" if copy["from"] == EVERYTHING.isoformat() else f"from {copy['from']} on"
        print(f"  copy: every row {frm}, complete up to {copy.get('through')}{behind}")
    else:
        print("  copy: nothing copied yet (the worker copies new rows; `meritorder clickhouse backfill` copies older ones)")
    if copy.get("error"):
        print(f"  last copy failed at {copy.get('error_at')}: {copy['error']}")
    print("  reports: large reports read ClickHouse" if d["answering"] else f"  reports: Postgres answers them all: {d['answers']}")


def cmd_clickhouse(args) -> None:
    from .clickhouse import Copier
    from .configstore import ConfigFollower

    def day(value: str, what: str) -> datetime:
        try:
            return datetime.combine(datetime.fromisoformat(value).date(), datetime.min.time(), tzinfo=UTC)
        except ValueError:
            raise SystemExit(f"{what} must be YYYY-MM-DD, got {value!r}")

    async def run():
        store = _store()
        await store.init()
        try:
            cfg = await ConfigFollower(store, path=args.config).current()
            if cfg.reporting.clickhouse is None:
                raise SystemExit("reporting.clickhouse isn't set (or its url is empty): see docs/08-reporting.md, \"Storage and scale\"")
            store.use_clickhouse(cfg.reporting.clickhouse)
            rep = store.clickhouse
            if args.ch_cmd == "status":
                _print_clickhouse(await rep.describe())
                return
            first = await store.first_ledger_ts()
            if first is None and not args.start:
                print("the ledger is empty: nothing to copy")
                return
            start = day(args.start, "--start") if args.start else datetime.combine(first.date(), datetime.min.time(), tzinfo=UTC)
            end = day(args.end, "--end") if args.end else datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            if end <= start:
                raise SystemExit("--end must be after --start")
            print(f"copying the ledger from {start:%Y-%m-%d} to {end:%Y-%m-%d} (exclusive) to {rep.cfg.url}, database {rep.cfg.database}")

            def progress(d0, d1, res):
                extra = (f"; copied again: {', '.join(res.repaired)}" if res.repaired else "") + (
                    f"; ClickHouse has rows Postgres doesn't, left as they are: {', '.join(res.ahead)}" if res.ahead else "")
                print(f"  {d0:%Y-%m-%d}: {res.copied:,} rows copied{extra}", flush=True)

            total, note = await Copier(store, rep).backfill(start, end, progress)
            print(f"done: {total.copied:,} rows copied, {len(total.repaired)} hours copied again, {len(total.ahead)} hours where "
                  "ClickHouse has rows Postgres doesn't")
            if note:
                print(note)
        finally:
            await store.close()

    asyncio.run(run())


def cmd_controller(args) -> None:
    from .controller import run_controller

    asyncio.run(run_controller(once=args.once, interval=args.interval, path=args.config))


def cmd_gpu(args) -> None:
    """`meritorder gpu volumes`: the weights cache volumes (serve.weights_cache: volume), and deleting one. Runs where
    the controller does (SkyPilot and its cloud credentials, and the database)."""
    from . import skypilot

    async def run():
        store = _store()
        await store.init()
        try:
            if args.volumes_cmd == "delete":
                if not args.name:
                    raise SystemExit("say which volume: meritorder gpu volumes delete <name> (meritorder gpu volumes lists them)")
                if not args.yes:
                    raise SystemExit(f"This deletes {args.name} and the weights cached on it; the next start downloads them again. "
                                     "Run again with --yes to delete it.")
                await skypilot.delete_volume(store, args.name)
                print(f"deleted {args.name}")
                return
            found = await skypilot.list_volumes(store)
            if not found:
                print("no weights cache volumes")
            for v in found:
                cost = f"about ${v['usd_per_month']:,.2f}/month" if v.get("usd_per_month") is not None else "price not known"
                where = v.get("infra") or "/".join(x for x in (v.get("cloud"), v.get("region"), v.get("zone")) if x)
                used = ", ".join(v.get("usedby_clusters") or []) or "nothing"
                print(f"{v['name']}  {v.get('deployment') or '?'}  {where}  {v.get('size_gb') or v.get('size') or '?'} GB  {cost}  "
                      f"used by {used}" + ("" if v["in_skypilot"] else "  (not in SkyPilot any more)"))
        finally:
            await store.close()

    try:
        asyncio.run(run())
    except (ValueError, skypilot.SkyError) as e:
        raise SystemExit(str(e)) from None


def cmd_compressor(args) -> None:
    from .compressor_service import serve

    serve(args.host, args.port, args.model, args.device, args.revision, args.batch_size, args.log_level, args.threads)


def cmd_accounts(args) -> None:
    """Console accounts (decision 69): the first admin of a new install, and links for people who can't use the console's
    own Invite button. Runs against the database, like `keys create` (e.g. `docker compose exec gateway meritorder ...`)."""
    import getpass

    from .accounts import EMAIL, hash_password_sync, password_problem
    from .config import LocalAccountsConfig

    email = args.email.strip().lower()
    if not EMAIL.match(email):
        raise SystemExit(f"{args.email!r} isn't an email address")
    minimum = LocalAccountsConfig().min_password_length

    async def run():
        store = _store()
        await store.init()
        try:
            user = await store.get_user(email)
            if args.accounts_cmd == "create":
                if user is None:
                    await store.upsert_user(email, args.team, args.role, email=email, source="ensure")
                    await store.update_user(email, team=args.team, role=args.role)
                elif (user["team"], user["role"]) != (args.team, args.role):
                    await store.update_user(email, team=args.team, role=args.role)
                    print(f"{email} already existed; now on team {args.team} with role {args.role}")
                if args.link:
                    return await link(store, "invite")
                if args.password_stdin:
                    password = sys.stdin.readline().rstrip("\n")
                else:
                    password = getpass.getpass(f"Password for {email} (at least {minimum} characters): ")
                    if getpass.getpass("Again: ") != password:
                        raise SystemExit("the passwords didn't match; nothing was changed")
                if why := password_problem(password, email, minimum):
                    raise SystemExit(f"password not accepted: {why}")
                await store.set_password(email, hash_password_sync(password))
                await store.revoke_user_sessions(email)
                print(f"{email} can sign in to the console now ({args.role}, team {args.team}).")
            elif args.accounts_cmd == "reset":
                if user is None:
                    raise SystemExit(f"no one called {email}; create them with `meritorder accounts create`")
                await link(store, "reset" if await store.get_password(email) else "invite")
        finally:
            await store.close()

    async def link(store, kind: str):
        await store.revoke_user_invites(email)
        expires = datetime.now(UTC) + timedelta(hours=args.hours)
        _, token = await store.create_invite(email, kind, expires, created_by="cli", creator_role="admin")
        base = (args.url or "").rstrip("/")
        print(f"{'Invite' if kind == 'invite' else 'Password reset'} link for {email}, valid once until {expires:%Y-%m-%d %H:%M} UTC:")
        print(f"{base}/ui/#invite={token}" if base else f"<your gateway>/ui/#invite={token}")

    asyncio.run(run())


def cmd_keys(args) -> None:
    async def run():
        store = _store()
        await store.init()
        try:
            if args.keys_cmd == "create":
                expires = datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
                tags = dict(t.split("=", 1) for t in args.tag or [])
                await store.upsert_user(args.user, args.team, args.role, source="ensure")
                key_id, secret = await store.create_key(args.user, args.team, args.role, args.name, tags, expires)
                print(f"key id:  {key_id}")
                print(f"api key: {secret}")
                print("Store it now; it is not shown again.")
            elif args.keys_cmd == "list":
                for k in await store.list_keys(args.user):
                    state = "revoked" if k["revoked_at"] else ("expired" if k["expires_at"] and k["expires_at"] < datetime.now(UTC) else "active")
                    print(f"{k['id']:18} {k['user_id']:24} {k['team']:16} {k['role']:10} {state:8} {k.get('name') or ''}")
            elif args.keys_cmd == "revoke":
                print("revoked" if await store.revoke_key(args.key_id) else "no active key with that id")
        finally:
            await store.close()

    asyncio.run(run())


def _bills(gateway: str, key: str, args) -> None:
    """The ledger against providers' bills (finance and admins)."""
    params = {k: v for k, v in (("start", args.start), ("end", args.end), ("by", args.by)) if v}
    resp = httpx.get(f"{gateway}/v1/reports/bills", params=params, headers={"authorization": f"Bearer {key}"}, timeout=120)
    if resp.status_code != 200:
        raise SystemExit(f"{resp.status_code}: {resp.text}")
    rep = resp.json()

    def money(v, sign: bool = False) -> str:
        return "–" if v is None else f"{v:+,.2f}" if sign else f"{v:,.2f}"

    def pct(v) -> str:
        return "–" if v is None else (">+999%" if v > 0 else "<-999%") if abs(v) >= 1000 else f"{v:+.1f}%"

    label = {"not_billed": "not on the bill", "not_recorded": "not in the ledger", "unmatched": "not matched", "excluded": "left out"}

    print(f"Ledger against bills, {rep['start']} to {rep['end']} (totals cover settled days only)")
    if not rep["sources"]:
        print("  no bill sources configured (billing.sources; docs/08-reporting.md)")
    for s in rep["sources"]:
        t = s["settled"]
        state = "ok" if s["ok"] else f"failed: {s['error']}" if s["ok"] is False else "not fetched yet"
        print(f"\n  {s['name']} ({s['type']}: {', '.join(s['backends']) or 'no backends'}), {state}; "
              f"complete through {s['complete_through'] or '–'}")
        print(f"    recorded ${money(t['recorded'])}  billed ${money(t['billed'])}  difference {money(t['difference'], True)} "
              f"({pct(t['pct'])})  not matched ${money(t['billed_unmatched'])}  credits ${money(t['adjustments'])}")
        for note in s.get("notes") or []:  # e.g. an export with no files yet, or Cost Explorer without the model breakdown
            print(f"    note: {note}")
    if rep["rows"]:
        print(f"\n  {'period':10} {'source':12} {'model':24} {'recorded':>11} {'billed':>11} {'diff':>10} {'%':>8}  status")
        for r in rep["rows"]:
            over = " (over the alert threshold)" if r["over"] else ""
            print(f"  {r['period']:10} {r['source'][:12]:12} {r['model'][:24]:24} {money(r['recorded']):>11} {money(r['billed']):>11} "
                  f"{money(r['difference'], True):>10} {pct(r['pct']):>8}  {label.get(r['status'], r['status'])}{over}")
    if rep["unmatched"]:
        print("\n  On the bills, not matched to a model:")
        for u in rep["unmatched"][:args.limit]:
            print(f"    {u['source'][:12]:12} {money(u['cost']):>11}  {u['line'][:60]:60}  {label.get(u['status'], u['status'])}: {u['why']}")
        if len(rep["unmatched"]) > args.limit:
            print(f"    ... and {len(rep['unmatched']) - args.limit} more (--limit)")
    if rep["gpu"]:
        print(f"\n  {'GPU':10} {'deployment':24} {'recorded':>11} {'billed':>11} {'diff':>10} {'%':>8}")
        for g in rep["gpu"]:
            print(f"  {g['period']:10} {g['deployment'][:24]:24} {money(g['recorded']):>11} {money(g['billed']):>11} "
                  f"{money(g['difference'], True):>10} {pct(g['pct']):>8}{'  partial' if g['partial'] else ''}")
    for u in rep["uncovered"]:
        print(f"  not checked: {u['backend']} ${money(u['recorded'])} ({u['why']})")


def _money(v) -> str:
    return "–" if v is None else f"${v:,.4f}"


def _training(r: dict) -> None:
    """What `meritorder router train` did."""
    m = r.get("metrics") or {}
    sig = ", ".join(f"{n:,} {k.replace('_', ' ')}" for k, n in sorted((m.get("signals") or {}).items()))
    print(f"Learned router: {m.get('requests', 0):,} requests from the last {r.get('days')} days, "
          f"{m.get('with_signal', 0):,} with an outcome signal" + (f" ({sig})" if sig else ""))
    j = m.get("judged") or {}
    if j.get("labels"):
        agree = f"agreement with the outcome signals {j['agreement']:+.2f}" if j.get("agreement") is not None else "no outcome signal on them yet"
        print(f"  {j['labels']:,} judged labels ({j['not_good_enough']:,} not good enough), {agree}: weight {j['weight']:.2f}")
    h = m.get("holdout")
    if h:
        print(f"  the newest {h['requests']:,} held out: {h['loss']:.4f} against the rules' {h['rules_loss']:.4f} (lower is better); "
              f"at the confidence it needs it would raise {h['would_raise']:,} and lower {h['would_lower']:,} of them")
    if r.get("version"):
        print(f"  saved as version {r['version']}" + (", in use: gateways load it within seconds" if r["active"]
                                                     else f", not in use: {r['why']}"))
    elif r.get("dry_run") and not r.get("why"):
        print("  not saved (--dry-run)")
    else:
        print(f"  not saved: {r['why']}")


def _judged_report(j: dict) -> None:
    """The judged labels part of `meritorder router report`: how many, what they cost, how far they agree."""
    teams = j.get("teams") or []
    if not j.get("samples") and not teams:
        return
    cost = j.get("cost") or {}
    print(f"  judged labels: {j.get('labels', 0):,} labels from {j.get('samples', 0):,} samples "
          f"({', '.join(f'{k} {v:,}' for k, v in sorted((j.get('outcomes') or {}).items())) or 'none'}); "
          f"{j.get('not_good_enough', 0):,} answers not good enough; cost {_money(cost.get('total'))} "
          f"(replays {_money(cost.get('replay'))}, judging {_money(cost.get('judge'))})")
    if j.get("agreement") is not None:
        print(f"    agreement with the outcome signals: caught {j['caught']:.0%} of the answers with a signal, failed {j['false_alarm']:.0%} "
              f"of the rest (J {j['agreement']:+.2f}); weight in training {j['weight']:.2f}")
    elif j.get("weight") is not None:
        print(f"    no judged answer has an outcome signal yet: weight in training {j['weight']:.2f} (the prior)")
    if j.get("preferred_first") is not None:
        print(f"    the judge preferred the answer shown first {j['preferred_first']:.0%} of the time (about half: no position bias), "
              f"the stronger one {j['preferred_stronger']:.0%}")
    for t in teams:
        cap = f"${t['cap']:,.2f}" if t.get("cap") else "no cap"
        print(f"    team {t['team']}: {'on' if t['enabled'] else 'off'}, {t['sample_rate']:.2%} sampled, {_money(t['spent_month'])} of {cap} "
              f"this month{'  [cap reached: no new samples until next month]' if t.get('capped') else ''}")
    for why, n in (j.get("why") or {}).items():
        print(f"    {n:,} × {why}")


def _router_report(gateway: str, key: str, args) -> None:
    resp = httpx.get(f"{gateway}/v1/reports/router", params={"days": args.days, "limit": args.limit},
                     headers={"authorization": f"Bearer {key}"}, timeout=60)
    if resp.status_code != 200:
        raise SystemExit(f"{resp.status_code}: {resp.text}")
    rep = resp.json()
    s = rep["status"]
    model = (f"model v{s['loaded']}, trained {s['age_days']} days ago on {s['examples']:,} requests" if s.get("loaded")
             else s.get("load_error") or "no model in use")
    print(f"Learned router: mode {s['mode']}; {model}" + (f"; stale: {s['stale']}" if s.get("stale") else ""))
    print(f"  last {rep['days']} days: {rep['requests']:,} requests seen, {rep['compared']:,} with a prediction, "
          f"{rep['agreed']:,} agreeing with the rules, {rep['disagreed']:,} not")
    if rep["outcomes"]:
        print("  " + ", ".join(f"{k}: {v:,}" for k, v in sorted(rep["outcomes"].items())))
    c = rep["cost"]
    if c["priced"]:
        print(f"  where they disagree ({c['priced']:,} priced): the rules' picks {_money(c['rules'])}, the learned router's "
              f"{_money(c['learned'])} for the tasks, a difference of {_money(c['difference'])}")
    rates = rep["signal_rates"]
    if rates:
        print("  failure signals on the side that acted: " + ", ".join(
            f"{k} {v['with_signal']:,} of {v['requests']:,}" for k, v in rates.items()))
    if rep["groups"]:
        print(f"\n  {'task':8} {'rules':>5} {'learned':>7} {'requests':>9} {'used':>5} {'signals':>8} {'rules $':>11} {'learned $':>11} "
              f"{'exp. rules':>10} {'exp. learned':>12}")
        for g in rep["groups"]:
            def pct(x):
                return "–" if x is None else f"{x:.0%}"
            print(f"  {g['task']:8} {g['rules']:>5} {g['learned']:>7} {g['requests']:>9,} {g['used']:>5,} {g['with_signal']:>8,} "
                  f"{_money(g['rules_cost']):>11} {_money(g['learned_cost']):>11} {pct(g['expected_rules']):>10} {pct(g['expected_learned']):>12}")
    _judged_report(rep.get("judged") or {})
    for r in rep["rows"][:args.limit]:
        print(f"  {r['ts'][:19]} {r['user'] or '':20} {r['task']}:{r['rules']} → {r['task']}:{r['learned']} "
              f"({r['confidence']:.0%}, v{r['version']}) "
              f"{r['rules_model'] or '–'} {_money(r['rules_cost'])} / {r['learned_model'] or '–'} {_money(r['learned_cost'])}"
              f"{'  [used]' if r['used'] else ''}{'  signals: ' + ', '.join(r['signals']) if r['signals'] else ''}")


def cmd_router(args) -> None:
    """The learned router: train it, see the shadow report, or choose the version gateways use."""
    if args.router_cmd == "report":
        return _router_report(_gateway(args), _key(args), args)

    async def run():
        import getpass

        from .configstore import ConfigFollower, current_token
        from .learned_jobs import train_and_save
        from .state import make_state

        store = _store()
        await store.init()
        state = make_state(os.environ.get("MERITORDER_REDIS_URL"))
        try:
            cfg = await ConfigFollower(store, path=args.config).current()
            if args.router_cmd == "train":
                _training(await train_and_save(cfg, store, state, f"cli:{getpass.getuser()}", days=args.days,
                                               min_examples=args.min_examples, dry_run=args.dry_run))
                return
            version = None if args.version == "none" else int(args.version)
            if not await store.activate_router_model(version):
                raise SystemExit(f"there's no learned router model v{version}")
            await state.set_config_token(await current_token(store, await store.get_setting("prices")))
            print("no learned router model in use: the rules decide" if version is None else f"gateways now use model v{version}")
        finally:
            await state.close()
            await store.close()

    asyncio.run(run())


def cmd_usage(args) -> None:
    gateway, key = _gateway(args), _key(args)
    if args.bills:
        return _bills(gateway, key, args)
    resp = httpx.get(f"{gateway}/v1/reports/me", headers={"authorization": f"Bearer {key}"}, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f"{resp.status_code}: {resp.text}")
    me = resp.json()
    budget = f" of ${me['budget_usd']:,.2f}" if me.get("budget_usd") is not None else ""
    print(f"{me['user']} ({me['team']}), {me['month']}")
    print(f"  spent     ${me['spend_usd']:,.4f}{budget}")
    if me.get("gpu_usd"):  # requests served on self-hosted GPUs: their share of the GPU time
        print(f"            APIs ${me['api_usd']:,.4f} · GPUs ${me['gpu_usd']:,.4f} (your requests' share of GPU time)")
    print(f"  baseline  ${me['baseline_usd']:,.4f}   (what the same work would cost without Merit Order)")
    print(f"  saved     ${me['savings_usd']:,.4f}   across {me['requests']} requests")
    if me["by_model"]:
        print(f"\n  {'model':28} {'requests':>9} {'cost':>12} {'saved':>12}")
        for r in me["by_model"]:
            print(f"  {str(r['key']):28} {r['requests']:>9} {r['actual_cost']:>12.4f} {r['savings_total']:>12.4f}")


def cmd_ocr(args) -> None:
    """A document's text through the gateway's /v1/ocr: a local file is uploaded; a URL is passed on for the provider to read."""
    gateway, key = _gateway(args), _key(args)
    fields: dict = {}
    if args.model:
        fields["model"] = args.model
    if args.pages:
        fields["pages"] = args.pages
    if args.blocks:
        fields["include_blocks"] = True
    if args.confidence:
        fields["confidence_scores_granularity"] = args.confidence
    if args.tables:
        fields["table_format"] = args.tables
    if args.images:
        fields["include_image_base64"] = True
    if args.schema:
        try:
            schema = json.loads(Path(args.schema).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"--schema: {e}")
        fields["document_annotation_format"] = schema if schema.get("type") == "json_schema" else {
            "type": "json_schema", "json_schema": {"name": Path(args.schema).stem, "schema": schema}}
        if args.prompt:
            fields["document_annotation_prompt"] = args.prompt
    full = args.json or args.schema or args.blocks or args.confidence or args.images
    headers = {"authorization": f"Bearer {key}", **({} if full else {"accept": "text/markdown"})}
    url = f"{gateway}/v1/ocr"
    if args.document.startswith(("https://", "http://")):
        image = args.document.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".avif"))
        kind = "image_url" if image else "document_url"
        resp = httpx.post(url, headers=headers, json={**fields, "document": {"type": kind, kind: args.document}}, timeout=900)
    else:
        path = Path(args.document)
        if not path.is_file():
            raise SystemExit(f"no such file: {path}")
        form = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else str(v) for k, v in fields.items()}
        with path.open("rb") as f:
            resp = httpx.post(url, headers=headers, data=form, files={"file": (path.name, f)}, timeout=900)
    if resp.status_code != 200:
        try:
            message = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = resp.text[:500]
        raise SystemExit(f"{resp.status_code}: {message}")
    out = json.dumps(resp.json(), indent=2, ensure_ascii=False) if full else resp.text
    if args.out:
        Path(args.out).write_text(out + ("" if out.endswith("\n") else "\n"))
        print(f"{args.out}: {resp.headers.get('x-meritorder-pages', '?')} pages, ${float(resp.headers.get('x-meritorder-cost-usd') or 0):.4f}",
              file=sys.stderr)
    else:
        print(out)


def _anthropic_batch(batch_id: str) -> bool:
    return batch_id.startswith("msgbatch_")


def _batch_get(gateway: str, key: str, batch_id: str) -> dict:
    path = f"/v1/messages/batches/{batch_id}" if _anthropic_batch(batch_id) else f"/v1/batches/{batch_id}"
    resp = httpx.get(f"{gateway}{path}", headers={"authorization": f"Bearer {key}"}, timeout=60)
    if resp.status_code != 200:
        raise SystemExit(f"{resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _batch_status(b: dict) -> tuple[str, dict[str, int], bool]:
    """(status, counts, ended) for either API's batch object."""
    if b.get("type") == "message_batch":
        c = b.get("request_counts") or {}
        return b["processing_status"], {k: int(c.get(k) or 0) for k in ("processing", "succeeded", "errored", "canceled", "expired")}, \
            b["processing_status"] == "ended"
    c = b.get("request_counts") or {}
    status = b["status"]
    return status, {"total": int(c.get("total") or 0), "completed": int(c.get("completed") or 0), "failed": int(c.get("failed") or 0)}, \
        status in ("completed", "failed", "expired", "cancelled")


def _batch_summary(b: dict) -> str:
    status, counts, _ = _batch_status(b)
    mo = b.get("meritorder") or {}
    where = ", ".join(f"{k} {v}" for k, v in (mo.get("providers") or {}).items()) or "no provider"
    money = f"estimated ${mo.get('estimated_cost_usd', 0):,.4f}"
    if mo.get("cost_usd"):
        money += f", cost ${mo['cost_usd']:,.4f}, saved ${mo.get('savings_usd', 0):,.4f} (batch discount ${mo.get('batch_discount_usd', 0):,.4f})"
    return f"{b['id']}  {status}  {' '.join(f'{k} {v}' for k, v in counts.items())}  [{where}]  {money}"


def cmd_batch(args) -> None:
    gateway, key = _gateway(args), _key(args)
    headers = {"authorization": f"Bearer {key}"}
    with httpx.Client(timeout=600, headers=headers) as c:
        if args.batch_cmd == "submit":
            data = Path(args.file).read_bytes()
            first = next((json.loads(x) for x in data.decode("utf-8-sig").splitlines() if x.strip()), None)
            if not isinstance(first, dict):
                raise SystemExit(f"{args.file} has no JSON lines")
            if "params" in first:  # Anthropic Message Batches: one {custom_id, params} a line
                rows = [json.loads(x) for x in data.decode("utf-8-sig").splitlines() if x.strip()]
                resp = c.post(f"{gateway}/v1/messages/batches", json={"requests": rows})
            else:  # OpenAI: upload the file, then make the batch from it
                up = c.post(f"{gateway}/v1/files", files={"file": (Path(args.file).name, data, "application/jsonl")}, data={"purpose": "batch"})
                if up.status_code != 200:
                    raise SystemExit(f"upload failed: {up.status_code}: {up.text[:500]}")
                endpoint = args.endpoint or first.get("url") or "/v1/chat/completions"
                resp = c.post(f"{gateway}/v1/batches", json={"input_file_id": up.json()["id"], "endpoint": endpoint, "completion_window": "24h"})
            if resp.status_code != 200:
                raise SystemExit(f"{resp.status_code}: {resp.text[:1000]}")
            b = resp.json()
            print(_batch_summary(b))
            print(f"Watch it with: meritorder batch watch {b['id']}")
            if not args.watch:
                return
            args.batch_id = b["id"]
        if args.batch_cmd in ("submit", "watch"):
            last = None
            while True:
                b = _batch_get(gateway, key, args.batch_id)
                line = _batch_summary(b)
                if line != last:
                    print(f"{datetime.now().strftime('%H:%M:%S')}  {line}", flush=True)
                    last = line
                if _batch_status(b)[2]:
                    print(f"Done. Results: meritorder batch results {b['id']} --out results.jsonl")
                    return
                time.sleep(args.interval)
        if args.batch_cmd == "list":
            for path in ("/v1/batches", "/v1/messages/batches"):
                resp = c.get(f"{gateway}{path}", params={"limit": args.limit})
                if resp.status_code != 200:
                    raise SystemExit(f"{resp.status_code}: {resp.text[:500]}")
                for b in resp.json().get("data") or []:
                    print(_batch_summary(b))
            return
        if args.batch_cmd == "storage":
            resp = c.get(f"{gateway}/v1/reports/batches", params={"limit": 1})
            if resp.status_code != 200:
                raise SystemExit(f"{resp.status_code}: {resp.text[:500]}")
            st = resp.json().get("storage")
            if not st:
                raise SystemExit("only admins see where batch content is held")
            print(f"held in: {st['held_in']}  ({st['files_held']} uploads waiting for their batch"
                  + (f", {st['files_elsewhere']} of them in an earlier storage place" if st.get("files_elsewhere") else "") + ")")
            print(f"batch key: {st['key'] or 'not set (MERITORDER_BATCH_KEY)'}")
            if st.get("previous_key"):
                state = "readable" if st.get("previous_usable") else "no longer read"
                print(f"previous key: {st['previous_key']}, {state} until {st.get('previous_readable_until') or '?'}; "
                      f"{st['files_under_previous']} uploads still sealed with it")
            g = st.get("gpu") or {}
            print(f"batch work on GPUs: {'on' if g.get('enabled') else 'off'}" + (
                f", handed to a provider's batch API after {g['handoff_hours']:g} h, vLLM priority {g['priority']}" if g.get("enabled") else ""))
            return
        if args.batch_cmd == "cancel":
            path = f"/v1/messages/batches/{args.batch_id}/cancel" if _anthropic_batch(args.batch_id) else f"/v1/batches/{args.batch_id}/cancel"
            resp = c.post(f"{gateway}{path}")
            if resp.status_code != 200:
                raise SystemExit(f"{resp.status_code}: {resp.text[:500]}")
            print(_batch_summary(resp.json()))
            return
        # results: Anthropic's results file, or OpenAI's output and error files (both unless --errors-only)
        b = _batch_get(gateway, key, args.batch_id)
        if not _batch_status(b)[2]:
            raise SystemExit(f"{args.batch_id} hasn't finished yet ({_batch_status(b)[0]})")
        if _anthropic_batch(args.batch_id):
            urls = [b.get("results_url") or f"{gateway}/v1/messages/batches/{args.batch_id}/results"]
        else:
            ids = ([] if args.errors_only else [b.get("output_file_id")]) + [b.get("error_file_id")]
            urls = [f"{gateway}/v1/files/{i}/content" for i in ids if i]
        out = open(args.out, "wb") if args.out else sys.stdout.buffer
        n = 0
        try:
            for url in urls:
                with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise SystemExit(f"{r.status_code}: {r.read()[:500].decode(errors='replace')}")
                    for line in r.iter_lines():
                        if line.strip():
                            out.write(line.encode() + b"\n")
                            n += 1
        finally:
            if args.out:
                out.close()
        if args.out:
            print(f"{n} results written to {args.out}")


def cmd_login(args) -> None:
    gateway = (args.gateway or os.environ.get("MERITORDER_URL") or "http://localhost:8080").rstrip("/")
    with httpx.Client(timeout=30) as c:
        cfg = c.get(f"{gateway}/auth/config").json()
        oidc = cfg.get("oidc")
        local = (cfg.get("local") or {}).get("enabled", False)
        if getattr(args, "password", False) or (not oidc and local):
            return _password_login(c, gateway, args)
        if not oidc:
            raise SystemExit("this gateway has no sign-in configured; ask an admin for an API key")
        disc = c.get(oidc["issuer"].rstrip("/") + "/.well-known/openid-configuration").json()
        if not disc.get("device_authorization_endpoint"):
            raise SystemExit(f"{oidc['issuer']} doesn't offer device login (no device_authorization_endpoint in its discovery document)")
        dev = c.post(disc["device_authorization_endpoint"], data={"client_id": oidc["client_id"], "scope": " ".join(oidc["scopes"])}).json()
        if "device_code" not in dev:
            raise SystemExit(f"login failed: {dev.get('error_description') or dev.get('error') or dev}")
        # RFC 8628 says verification_uri; Google sends verification_url.
        print(dev.get("message") or f"Open {dev.get('verification_uri') or dev.get('verification_url')} and enter code {dev['user_code']}")
        interval = int(dev.get("interval", 5))
        deadline = time.time() + int(dev.get("expires_in", 900))
        poll = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": dev["device_code"], "client_id": oidc["client_id"]}
        if oidc.get("client_secret"):  # Google's device clients need theirs (public for that client type)
            poll["client_secret"] = oidc["client_secret"]
        while time.time() < deadline:
            time.sleep(interval)
            tok = c.post(disc["token_endpoint"], data=poll).json()
            if "id_token" in tok:
                break
            err = tok.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                raise SystemExit(f"login failed: {tok.get('error_description') or err}")
        else:
            raise SystemExit("login timed out")
        resp = c.post(f"{gateway}/auth/exchange", json={"id_token": tok["id_token"]})
        if resp.status_code != 200:
            raise SystemExit(f"key exchange failed: {resp.status_code} {resp.text}")
        result = resp.json()
    _logged_in(gateway, result)


def _logged_in(gateway: str, result: dict) -> None:
    save_key(gateway, result["api_key"], {k: v for k, v in result.items() if k != "api_key"})
    print(f"\nLogged in as {result['user']} (team {result['team']}); key expires {result['expires_at'][:10]}.")
    print(f"Key saved to {KEY_FILE}\n")
    print("Next: `meritorder setup pi`, `meritorder setup claude-code` or `meritorder setup codex`.")


def _password_login(c: httpx.Client, gateway: str, args) -> None:
    """A gateway with local accounts (decision 69): the email address and password from the console get a key."""
    import getpass

    email = getattr(args, "email", None) or input("Email address: ").strip()
    password = getpass.getpass("Password: ")
    resp = c.post(f"{gateway}/auth/exchange", json={"email": email, "password": password})
    if resp.status_code == 401 and resp.headers.get("x-meritorder-two-factor") == "required":  # two-factor is on
        code = input("Code from your authenticator app (or a recovery code): ").strip()
        resp = c.post(f"{gateway}/auth/exchange", json={"email": email, "password": password, "code": code})
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        raise SystemExit(f"login failed: {detail}")
    _logged_in(gateway, resp.json())


def cmd_use_key(args) -> None:
    """Save an API key an admin gave you (instead of OIDC login)."""
    save_key(_gateway(args), args.api_key)
    print(f"Key saved to {KEY_FILE}")


def cmd_token(args) -> None:
    print(_key(args))


def _models(gateway: str, key: str) -> list[dict]:
    resp = httpx.get(f"{gateway}/v1/models", headers={"authorization": f"Bearer {key}"}, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f"could not list models from {gateway}: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"]


def cmd_setup(args) -> None:
    from .harness import claude_config_dir, codex_home, write_claude_code, write_codex, write_pi

    gateway, key = _gateway(args), _key(args)
    if not KEY_FILE.exists():
        save_key(gateway, key)
    if args.harness == "pi":
        models = _models(gateway, key)
        path = write_pi(gateway, models)
        print(f"Wrote the 'meritorder' provider to {path} ({len(models)} models).")
        print("In pi, open /model and pick 'auto (Merit Order)' or a specific model.")
        return
    model = None if args.model.lower() in ("", "none", "keep") else args.model
    window = None
    if args.harness == "codex":
        if model:
            listed = {m["id"]: m for m in _models(gateway, key)}
            if model not in listed:
                raise SystemExit(f"'{model}' isn't a model this gateway serves you; choose one of: {', '.join(sorted(listed))}")
            window = listed[model].get("context_window")
        home = Path(args.config_dir).expanduser() if args.config_dir else codex_home()
        path = write_codex(gateway, home / "config.toml", model, window)
        print(f"Updated {path}: Codex uses the 'meritorder' provider ({gateway.rstrip('/')}/v1, over WebSocket), key read from {KEY_FILE}.")
        if model:
            print(f"Sessions start on '{model}'" + (f", with its {window:,}-token context window." if window else "."))
        if home != codex_home():
            print(f"Start Codex with `CODEX_HOME={home} codex` to use it.")
        return
    if model and not model.lower().startswith("claude-"):
        listed = {m["id"]: m for m in _models(gateway, key)}
        if model not in listed:
            raise SystemExit(f"'{model}' isn't a model this gateway serves you; choose one of: {', '.join(sorted(listed))}")
        window = listed[model].get("context_window")
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else claude_config_dir()
    path = write_claude_code(gateway, config_dir / "settings.json", model, window)
    print(f"Updated {path}: ANTHROPIC_BASE_URL={gateway}, apiKeyHelper reads {KEY_FILE}.")
    if model is None:
        print("Claude Code keeps its own model names (claude-sonnet-*, claude-opus-*, ...): each goes to that model.")
    elif window:
        print(f"New sessions start on '{model}' (also under /model), with its {window:,}-token context window.")
    else:
        print(f"New sessions start on '{model}'.")
    default = Path.home() / ".claude"
    if config_dir == default:
        print("This changes every Claude Code session that uses ~/.claude. To keep a separate profile instead, run "
              "`meritorder setup claude-code --config-dir ~/.claude-meritorder` and start it with "
              "`CLAUDE_CONFIG_DIR=~/.claude-meritorder claude`.")
    elif config_dir != claude_config_dir():
        print(f"Start Claude Code with `CLAUDE_CONFIG_DIR={config_dir} claude` to use it.")


def cmd_export(args) -> None:
    """Download the ledger for a date range to a file or S3 (s3://bucket/key), as JSON lines, CSV, or Parquet. Rows are
    written as they stream in, to ``<out>.partial`` next to the destination, which replaces it only once the export is
    whole: an export that fails part way leaves no file of its own, and whatever was at the destination stays."""
    from .export import CONTENT_TYPES, LedgerWriter, need_pyarrow

    if args.format == "parquet":
        need_pyarrow()  # before downloading anything
    gateway, key = _gateway(args), _key(args)
    params = {k: v for k, v in (("start", args.start), ("end", args.end)) if v}
    to_s3 = args.out.startswith("s3://")
    path = Path(f".meritorder-export-{os.getpid()}.{args.format}" if to_s3 else f"{args.out}.partial")
    writer, done = None, False
    try:
        with httpx.stream("GET", f"{gateway}/v1/reports/export.jsonl", params=params, headers={"authorization": f"Bearer {key}"},
                          timeout=None) as resp:
            if resp.status_code != 200:
                raise SystemExit(f"{resp.status_code}: {resp.read().decode()[:300]}")
            writer = LedgerWriter(args.format, path)
            for line in resp.iter_lines():
                if line:
                    writer.write(line)
        writer.close()
        if to_s3:
            from botocore.session import Session

            bucket, _, obj = args.out[5:].partition("/")
            client = Session().create_client("s3", endpoint_url=os.environ.get("MERITORDER_S3_ENDPOINT") or None)
            with open(path, "rb") as f:  # streamed from disk, not read into memory
                client.put_object(Bucket=bucket, Key=obj, Body=f, ContentType=CONTENT_TYPES[args.format])
        else:
            os.replace(path, args.out)  # atomic on one filesystem: the old file until here, the whole new one after
        done = True
    finally:
        if writer is not None:
            writer.close()
            if to_s3 or not done:
                path.unlink(missing_ok=True)
    print(f"exported {writer.rows} requests to {args.out}")


def cmd_eval(args) -> None:
    """Measure model quality on graded suites; suggest (or apply) quality scores. Runs on this machine, with a
    Docker sandbox for model-written code, and records the run so it shows on the console's Evals page."""
    from .evals import jobs
    from .evals.runner import MIN_HARD, RunContext, load_suite, needs, run_suite, score
    from .evals.sandbox import DockerSandbox

    gateway, key = _gateway(args), _key(args)
    h = {"authorization": f"Bearer {key}"}
    admin_cfg = httpx.get(f"{gateway}/v1/admin/config", headers=h, timeout=30)
    if admin_cfg.status_code != 200:
        raise SystemExit(f"needs an admin key ({admin_cfg.status_code}: {admin_cfg.text[:120]})")
    view = {m["name"]: m for m in admin_cfg.json()["models"]}
    live = {n: m for n, m in view.items() if any(r["live"] for r in m.get("routes", []))}
    models = [m.strip() for m in args.models.split(",")] if args.models else [
        n for n, m in live.items() if not n.startswith("mock-") and m.get("kind") != "embedding"]
    missing = [m for m in models if m not in live]
    if missing:
        raise SystemExit(f"no live route for: {', '.join(missing)} (see `meritorder check`)")
    only = [p.strip() for p in args.only.split(",")] if args.only else None
    suite = jobs.select(load_suite(args.suite), only)
    if not suite["tasks"]:
        raise SystemExit("no tasks match --only")
    judges = jobs.pick_judges(view, [j.strip() for j in args.judges.split(",")] if args.judges else None)
    sandbox = None
    if args.sandbox == "docker":
        sandbox = DockerSandbox()
        why = sandbox.available()
        if why:
            print(f"No sandbox: {why}. Code tasks, and agent tasks that run code, are skipped.")
            sandbox = None
    est = jobs.estimate(view, suite, models, args.repeat, judges)
    kinds = [needs(t) for t in suite["tasks"]]
    print(f"Suite '{suite['name']}': {len(suite['tasks'])} tasks x {args.repeat} on {len(models)} models: {', '.join(models)}")
    if kinds.count("judges"):
        print(f"Judges for {kinds.count('judges')} open-ended tasks: {', '.join(judges) or 'none available (those tasks are skipped)'}"
              + (" (each answer by the two from other model families)" if len(judges) > 2 else ""))
    cap = args.max_cost or jobs.default_cap(est["usd"])
    budget_left = None
    try:
        controls = httpx.get(f"{gateway}/v1/admin/controls", headers=h, timeout=30)
        org = controls.json().get("spend", {}).get("org", {}) if controls.status_code == 200 else {}
        if org.get("budget") is not None:
            budget_left = org["budget"] - org.get("spent", 0)
    except (httpx.HTTPError, ValueError):
        pass
    for line in jobs.cost_warning(est, cap, budget_left):
        print(line)
    if not args.yes:
        if est["usd"] >= jobs.TYPE_YES_ABOVE_USD:
            answer = input(f"This spends real money: about ${est['usd']:,.2f}, at most ${cap:,.2f}. Type yes to run it: ").strip().lower()
            if answer != "yes":
                raise SystemExit("cancelled")
        elif input("Run it? [y/N] ").strip().lower() != "y":
            raise SystemExit("cancelled")

    request = {"models": models, "suite": args.suite, "repeat": args.repeat, "only": only, "judges": judges,
               "concurrency": args.concurrency, "executor": "cli", "max_cost_usd": cap}
    created = httpx.post(f"{gateway}/v1/admin/evals/runs", headers=h, json=request, timeout=60)
    run_id = created.json().get("id") if created.status_code == 201 else None
    if run_id is None and created.status_code != 404:
        print(f"Not recorded on the gateway ({created.status_code}: {created.text[:160]}); running anyway.")
    # The gateway records each model's effort per level and its route; work them out here if it couldn't.
    recorded = created.json().get("request", {}) if run_id else {}
    from .config import RoutingConfig

    effort = recorded.get("effort") or jobs.efforts(view, models, admin_cfg.json().get("effort_by_floor") or RoutingConfig().effort_by_floor)
    backends = recorded.get("backends") or jobs.routes(view, sorted(set(models) | set(judges)))
    formats = recorded.get("formats") or jobs.formats(view, backends)
    print("Each model runs on " + ", ".join(f"{m}: {backends.get(m, 'any route')}" for m in models)
          + "; effort by level " + ", ".join(f"{lvl}: {e}" for lvl, e in sorted(next(iter(effort.values()), {}).items())) + ".")

    total = len(suite["tasks"]) * args.repeat * len(models)
    done, state = [0], {"cost": 0.0, "last": 0.0, "stop": False, "capped": False}

    def report(**fields):
        if run_id is None:
            return None
        try:
            r = httpx.patch(f"{gateway}/v1/admin/evals/runs/{run_id}", headers=h, json=fields, timeout=60)
            return r.json().get("status") if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def progress(res):
        done[0] += 1
        state["cost"] += res.cost
        mark = "pass" if res.passed else ("skip" if res.skipped else "ERROR" if res.error else "fail")
        extra = f" {res.detail}" if res.detail else ""
        print(f"  [{done[0]}/{total}] {res.model:22} {res.task_id:24} {mark}{extra}")
        if time.monotonic() - state["last"] > 5:
            state["last"] = time.monotonic()
            if report(progress={"total": total, "done": done[0]}, cost=round(state["cost"], 6)) in ("cancelling", "cancelled"):
                state["stop"] = True

    async def stop() -> bool:
        if not state["stop"] and state["cost"] >= cap:  # the spending cap: no new task starts
            state["stop"] = state["capped"] = True
            print(f"  Reached the ${cap:,.2f} spending cap: no more tasks start; those running finish.")
        return state["stop"]

    ctx = RunContext(sandbox=sandbox, judges=judges, effort=effort, backends=backends, formats=formats)
    results = asyncio.run(run_suite(gateway, key, models, suite, args.concurrency, args.repeat, progress, ctx=ctx, should_stop=stop))
    scores = score(results)
    print(f"\n{'model':22} {'pass':>6} {'coding':>12} {'general':>12} {'cost':>9} {'per solve':>10} {'median':>7} {'p90':>7} "
          f"{'turns':>6} {'tools':>6} {'out tok':>8}  configured")
    for m, s in scores.items():
        configured = live[m].get("quality") or {}
        e = s.get("efficiency") or {}

        def cell(task):
            v = s["tasks"].get(task)
            if not v:
                return "-"
            return f"{v['score']:.1f} ({v['low']:.1f}-{v['high']:.1f})" if v.get("score") is not None else f"few ({v['hard']['runs']})"
        per = f"{e['cost_per_pass']:.4f}" if e.get("cost_per_pass") is not None else "-"
        print(f"{m:22} {s['pass_rate']:>6.0%} {cell('coding'):>12} {cell('general'):>12} {s['cost']:>9.4f} {per:>10} {e.get('median_s', 0):>6.1f}s "
              f"{e.get('p90_s', 0):>6.1f}s {e.get('turns', 0):>6.1f} {e.get('tool_calls', 0):>6.1f} {e.get('output_tokens', 0):>8,}  {configured}")
        for f in s["failed"][: args.show_failures]:
            print(f"    x {f['id']}: {f['why'][:110]}")
    print(f"Scores: 1-5 like routing's, from hard tasks (levels 4-5): the best model here 5, others in proportion to how many they pass;\n"
          f"90% range in brackets. few (n): under {MIN_HARD} hard tasks graded. The console scores these with every model's latest result.")
    print("Per solve: cost divided by tasks passed. Median and p90: the model's time to complete a task (grading not included).")
    skipped = sum(1 for r in results if r.skipped)
    if skipped:
        print(f"{skipped} task runs were skipped and don't count (no sandbox or judges, or a provider error that outlasted the wait).")
    payload = {"suite": suite["name"], "ran_at": datetime.now(UTC).isoformat(timespec="seconds"), "models": scores}
    if args.out:
        Path(args.out).write_text(json.dumps({**payload, "results": jobs.result_dicts(results)}, indent=2, default=str))
        print(f"Results saved to {args.out}")
    final = "cancelled" if state["stop"] and not state["capped"] else "done"  # stopped at the cap, what ran still counts
    note = jobs.capped_note(cap, sum(r.cost for r in results), total - len(results)) if state["capped"] else None
    if note:
        print(f"The run {note}.")
    if run_id is not None:
        report(status=final, scores=json.loads(json.dumps(scores, default=str)), results=json.loads(json.dumps(jobs.result_dicts(results), default=str)),
               progress={"total": total, "done": len(results), **({"cap": cap, "stopped": "spending cap"} if note else {"cap": cap})},
               cost=round(sum(r.cost for r in results), 6), error=note)
        print(f"Recorded as run {run_id} (console: Models → Evals).")
    else:
        httpx.put(f"{gateway}/v1/admin/evals", headers=h, json=json.loads(json.dumps(payload, default=str)), timeout=30)
    if args.apply and final == "done":
        if run_id is not None:
            r = httpx.post(f"{gateway}/v1/admin/evals/runs/{run_id}/apply", headers=h, json={}, timeout=60)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            print(f"Applied: config version {body.get('version')}" if r.status_code == 200 and body.get("version")
                  else "Nothing to change." if r.status_code == 200 else f"Not applied: {r.status_code} {r.text[:200]}")
        else:
            print("Not applied: this gateway can't record eval runs; upgrade it to use --apply.")
    elif not args.apply:
        print("Apply the measured scores on the console (Models → Evals) or re-run with --apply.")


def cmd_prices(args) -> None:
    """Write the live catalog to prices.json: prices and limits, and quality scores from the configured source."""
    import json

    from .catalog import refresh_prices
    from .config import load_config
    from .quality_data import refresh_quality

    cfg = load_config(args.config)
    path, n, missing, data = refresh_prices(cfg)
    print(f"Wrote {n} route prices to {path}")
    if missing:
        print("Not in their price lists (check the model IDs):")
        for m in missing:
            print(f"  {m}")
    q = refresh_quality(cfg, aa_key=os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY"))
    if not q["status"]["ok"]:
        print(f"Quality scores not refreshed: {q['status'].get('error')}")
        return
    data["quality"] = q
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Quality scores for {len(q['scores'])} models from {cfg.quality.source} ({cfg.quality.scale} scale)"
          + (f"; not covered, so auto won't pick them without a quality_override: {', '.join(q['missing'])}" if q["missing"] else ""))


def cmd_discover(args) -> None:
    from .backends import build_backends
    from .catalog import discover
    from .config import load_config

    cfg = load_config(args.config)
    backends = build_backends(cfg)
    results = asyncio.run(discover(cfg, {k: v for k, v in backends.items() if not args.backend or k in args.backend}))
    for name, models in results.items():
        if isinstance(models, str):
            print(f"{name}: {models}")
            continue
        shown = [m for m in models if not args.grep or args.grep.lower() in m.lower()]
        print(f"{name}: {len(shown)} models")
        for m in shown[: args.limit]:
            print(f"  {m}")


def cmd_config_check(args) -> None:
    from .backends import build_backends
    from .config import load_config

    cfg = load_config(args.config)
    backends = build_backends(cfg)
    print("Backends:")
    for b in cfg.backends:
        fmt = "gemini" if b.api == "gemini" else b.format  # Gemini's native API, though the gateway hands it Chat Completions
        where = b.base_url or (f"Vertex AI, {b.project}, {b.region or 'global'}" if b.flavor == "vertex" else "")
        print(f"  {'ON ' if b.name in backends else 'off'}  {b.name:18} {fmt:9} {b.kind:11} {where}")
    print("\nModels (usable routes):")
    for m in cfg.models:
        usable = [r.backend for r in m.routes if r.backend in backends]
        print(f"  {m.name:22} quality={m.quality}  routes: {', '.join(usable) or '(none available)'}")
    oidc = cfg.auth.oidc
    if oidc:
        print(f"\nSign-in: {oidc.provider}, {oidc.issuer}")
        if oidc.reads_google_groups:  # what the gateway would call Google as; nothing is sent
            from .google_groups import Credentials, GroupsError

            creds = Credentials(oidc.google_groups)
            try:
                who = f"key in {creds.where()} (service account {creds.account})" if creds.key() else \
                    "no key: the identity of the machine or pod (GKE Workload Identity, Compute Engine, Cloud Run)"
            except GroupsError as e:
                who = str(e)
            print(f"  Google Groups: on; {who}; {len(oidc.team_from_groups)} group(s) mapped to teams, "
                  f"{len(oidc.role_from_groups)} to roles")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="meritorder", description="Merit Order LLM gateway")
    p.add_argument("--log-level", default=os.environ.get("MERITORDER_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the gateway")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, help="default MERITORDER_PORT or 8080")
    s.add_argument("--config")
    s.add_argument("--workers", type=int, help="processes (one core each); default MERITORDER_WORKERS or 1")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("migrate", help="create or upgrade the database schema").set_defaults(func=cmd_migrate)

    w = sub.add_parser("worker", help="run background jobs: ledger rollups, alerts, live prices and scores, bill checks, queued evals, "
                                      "learned-router training, batch jobs, stored-response retention, the ClickHouse copy")
    w.add_argument("--once", action="store_true", help="run each job once and exit")
    w.add_argument("--interval", type=int, default=60, help="seconds between passes (default 60)")
    w.add_argument("--config")
    w.add_argument("--backfill", action="store_true", help="recompute all hourly rollups from the raw ledger")
    w.add_argument("--metrics-port", type=int, help="serve the batch requests it settles as Prometheus metrics on this port "
                                                    "(default MERITORDER_WORKER_METRICS_PORT; unset: off)")
    w.set_defaults(func=cmd_worker)

    ch = sub.add_parser("clickhouse", help="the ledger's copy in ClickHouse (reporting.clickhouse): status, or copy existing rows")
    chsub = ch.add_subparsers(dest="ch_cmd", required=True)
    chs = chsub.add_parser("status", help="whether ClickHouse is reachable, how far the copy has got, and the tables' size")
    chs.add_argument("--config")
    chb = chsub.add_parser("backfill", help="copy existing ledger rows to ClickHouse; running it again copies only what's missing")
    chb.add_argument("--start", help="YYYY-MM-DD, inclusive (default: the ledger's first day)")
    chb.add_argument("--end", help="YYYY-MM-DD, exclusive (default: tomorrow)")
    chb.add_argument("--config")
    ch.set_defaults(func=cmd_clickhouse)

    c = sub.add_parser("controller", help="run the GPU capacity controller")
    c.add_argument("--config")
    c.add_argument("--once", action="store_true")
    c.add_argument("--interval", type=int, default=60)
    c.set_defaults(func=cmd_controller)

    g = sub.add_parser("gpu", help="GPU deployments' weights cache volumes: list them, or delete one (the sweep never does)")
    gsub = g.add_subparsers(dest="gpu_cmd", required=True)
    gv = gsub.add_parser("volumes", help="list this install's weights cache volumes, what they cost, and what uses them")
    gv.add_argument("volumes_cmd", nargs="?", choices=["list", "delete"], default="list")
    gv.add_argument("name", nargs="?", help="delete: the volume's name (mo-<install>-<deployment>-hf)")
    gv.add_argument("--yes", action="store_true", help="delete: really delete it")
    gv.set_defaults(func=cmd_gpu)

    cp = sub.add_parser("compressor", help="run the LLMLingua-2 service for model-based compression (needs the compressor extra)")
    cp.add_argument("--host", default="127.0.0.1")
    cp.add_argument("--port", type=int, default=8090)
    cp.add_argument("--model", default=os.environ.get("MERITORDER_COMPRESSOR_MODEL", "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"),
                    help="a Hugging Face model id or a local folder; the name must contain xlm-roberta-large or "
                         "bert-base-multilingual-cased (default: MERITORDER_COMPRESSOR_MODEL or the XLM-RoBERTa model)")
    cp.add_argument("--revision", default=os.environ.get("MERITORDER_COMPRESSOR_REVISION"), help="pin the model to this commit")
    cp.add_argument("--device", default=os.environ.get("MERITORDER_COMPRESSOR_DEVICE", "auto"), help="auto, cpu, cuda, cuda:1 or mps")
    cp.add_argument("--batch-size", type=int, default=50, help="512-token chunks the model takes at once (llmlingua's default)")
    cp.add_argument("--threads", type=int, default=int(os.environ.get("MERITORDER_COMPRESSOR_THREADS") or 0),
                    help="CPU threads for the model (default: MERITORDER_COMPRESSOR_THREADS, else the container's CPU limit, "
                         "else torch's own choice)")
    cp.set_defaults(func=cmd_compressor)

    k = sub.add_parser("keys", help="manage API keys")
    ksub = k.add_subparsers(dest="keys_cmd", required=True)
    kc = ksub.add_parser("create")
    kc.add_argument("--user", required=True)
    kc.add_argument("--team", default="default")
    kc.add_argument("--role", default="user", choices=["user", "team_lead", "finance", "admin"])
    kc.add_argument("--name")
    kc.add_argument("--tag", action="append", help="default tag for requests with this key, e.g. project=checkout")
    kc.add_argument("--expires-days", type=int)
    kl = ksub.add_parser("list")
    kl.add_argument("--user")
    kr = ksub.add_parser("revoke")
    kr.add_argument("key_id")
    k.set_defaults(func=cmd_keys)

    ac = sub.add_parser("accounts", help="console sign-in accounts: create one (the first admin), or print an invite or reset link")
    acsub = ac.add_subparsers(dest="accounts_cmd", required=True)
    acc = acsub.add_parser("create", help="create a person who signs in with a password (asked for, never on the command line)")
    acc.add_argument("email")
    acc.add_argument("--team", default="default")
    acc.add_argument("--role", default="user", choices=["user", "team_lead", "finance", "admin"])
    acc.add_argument("--password-stdin", action="store_true", help="read the password from standard input (for scripts)")
    acc.add_argument("--link", action="store_true", help="print a one-time link for them to set their own password instead")
    acr = acsub.add_parser("reset", help="print a one-time link that sets a new password")
    acr.add_argument("email")
    for a in (acc, acr):
        a.add_argument("--url", help="the gateway's address, to print the whole link (e.g. https://llm.example.com)")
        a.add_argument("--hours", type=int, default=72, help="how long the link works (default 72)")
    ac.set_defaults(func=cmd_accounts)

    u = sub.add_parser("usage", help="show your spend and savings this month, or (--bills) the ledger against providers' bills")
    u.add_argument("--gateway")
    u.add_argument("--key")
    u.add_argument("--bills", action="store_true", help="recorded against billed, by provider, model and day (finance and admins)")
    u.add_argument("--start", help="--bills: YYYY-MM-DD (default: first of this month)")
    u.add_argument("--end", help="--bills: YYYY-MM-DD, exclusive (default: tomorrow)")
    u.add_argument("--by", choices=["day", "month"], help="--bills: one row per model and day (default) or month")
    u.add_argument("--limit", type=int, default=20, help="--bills: how many unmatched bill lines to show")
    u.set_defaults(func=cmd_usage)

    oc = sub.add_parser("ocr", help="a document's text (PDF, image, Word, PowerPoint): a local file or a URL, through /v1/ocr",
                        description="Prints the document's text as markdown. --json (or any option asking for more) prints the "
                                    "whole reply: pages, tables, boxes, confidence, annotations. Agents can run it from their "
                                    "shell; Merit Order's screening counts what it returns as untrusted content.")
    oc.add_argument("document", help="a file, or an http(s) URL the provider fetches")
    oc.add_argument("--model", help="an OCR model (default: the ocr tier, the cheapest)")
    oc.add_argument("--pages", help='pages to read, from 0: "0-3,7"')
    oc.add_argument("--json", action="store_true", help="print the whole reply as JSON")
    oc.add_argument("--blocks", action="store_true", help="paragraph-level boxes (include_blocks)")
    oc.add_argument("--confidence", choices=["page", "block", "word"], help="confidence scores at this granularity")
    oc.add_argument("--tables", choices=["markdown", "html"], help="tables apart from the text, in this format (table_format)")
    oc.add_argument("--images", action="store_true", help="the page's images, base64 (include_image_base64)")
    oc.add_argument("--schema", help="a JSON Schema file: fields to read from the document (document_annotation_format; priced higher)")
    oc.add_argument("--prompt", help="--schema: what to look for (document_annotation_prompt)")
    oc.add_argument("--out", help="write to this file rather than standard output")
    oc.add_argument("--gateway")
    oc.add_argument("--key")
    oc.set_defaults(func=cmd_ocr)

    bt = sub.add_parser("batch", help="send work that can wait to the providers' batch APIs (about half price), and get the results")
    bsub = bt.add_subparsers(dest="batch_cmd", required=True)
    bs = bsub.add_parser("submit", help="submit a JSONL file: OpenAI batch lines (custom_id, method, url, body) or Anthropic ones (custom_id, params)")
    bs.add_argument("file")
    bs.add_argument("--endpoint", help="OpenAI lines: the endpoint they call (default: the first line's url)")
    bs.add_argument("--watch", action="store_true", help="then wait until it ends")
    bw = bsub.add_parser("watch", help="show a batch's progress until it ends")
    bw.add_argument("batch_id")
    for x in (bs, bw):
        x.add_argument("--interval", type=float, default=30, help="seconds between checks")
    bl = bsub.add_parser("list", help="your batches, newest first")
    bl.add_argument("--limit", type=int, default=20)
    br = bsub.add_parser("results", help="download an ended batch's results (JSON lines, in the API's own format)")
    br.add_argument("batch_id")
    br.add_argument("--out", help="file to write (default: standard output)")
    br.add_argument("--errors-only", action="store_true", help="OpenAI batches: only the error file")
    bc = bsub.add_parser("cancel", help="cancel a batch; what already ran is kept")
    bc.add_argument("batch_id")
    bst = bsub.add_parser("storage", help="admins: where uploads and GPU batch work are held, and the batch key's rotation")
    for x in (bs, bw, bl, br, bc, bst):
        x.add_argument("--gateway")
        x.add_argument("--key")
    bt.set_defaults(func=cmd_batch)

    lg = sub.add_parser("login", help="sign in (your organization's SSO, or your email address and password) and get an API key")
    lg.add_argument("--gateway")
    lg.add_argument("--password", action="store_true", help="sign in with your email address and password even where SSO is set up")
    lg.add_argument("--email", help="your email address (asked for if not given)")
    lg.set_defaults(func=cmd_login)

    uk = sub.add_parser("use-key", help="save an API key an admin gave you")
    uk.add_argument("api_key")
    uk.add_argument("--gateway")
    uk.set_defaults(func=cmd_use_key)

    tk = sub.add_parser("token", help="print your saved API key")
    tk.add_argument("--key")
    tk.set_defaults(func=cmd_token)

    st = sub.add_parser("setup", help="point a coding harness at Merit Order")
    st.add_argument("harness", choices=["pi", "claude-code", "codex"])
    st.add_argument("--gateway")
    st.add_argument("--key")
    st.add_argument("--model", default="auto",
                    help="claude-code, codex: the model sessions start on (default: auto; 'none' keeps the harness's own)")
    st.add_argument("--config-dir", help="claude-code: the settings folder (default: $CLAUDE_CONFIG_DIR or ~/.claude); "
                    "codex: the folder of config.toml (default: $CODEX_HOME or ~/.codex)")
    st.set_defaults(func=cmd_setup)

    ex = sub.add_parser("export", help="export per-request ledger rows (no content) to a file or s3://bucket/key")
    ex.add_argument("--start", help="YYYY-MM-DD (default: first of this month)")
    ex.add_argument("--end", help="YYYY-MM-DD, exclusive (default: tomorrow)")
    ex.add_argument("--format", choices=["jsonl", "csv", "parquet"], default="jsonl",
                    help="parquet needs the parquet extra (pyarrow): `uv sync --extra parquet` in a clone; the image doesn't have it")
    ex.add_argument("--out", required=True)
    ex.add_argument("--gateway")
    ex.add_argument("--key")
    ex.set_defaults(func=cmd_export)

    ev = sub.add_parser("eval", help="measure model quality on graded suites and suggest quality scores")
    ev.add_argument("--models", help="comma-separated model names (default: every chat model with a live route)")
    ev.add_argument("--suite", default="starter,frontier",
                    help="built-in suites (starter, frontier, codebase), task directories or YAML files, comma-separated (default: starter,frontier)")
    ev.add_argument("--only", help="only tasks whose id matches these comma-separated patterns, e.g. 'c5-*,a4-*'")
    ev.add_argument("--repeat", type=int, default=1, help="run each task this many times (smooths out randomness)")
    ev.add_argument("--concurrency", type=int, default=4)
    ev.add_argument("--judges", help="models that grade open-ended tasks, comma-separated (default: claude-opus,gpt-sol,gemini-carbon; "
                    "each answer is graded by the two from other model families)")
    ev.add_argument("--sandbox", choices=["docker", "none"], default="docker", help="where model-written code runs (none skips those tasks)")
    ev.add_argument("--show-failures", type=int, default=3)
    ev.add_argument("--out", help="save scores and every result as JSON")
    ev.add_argument("--apply", action="store_true", help="write the measured scores into the config as each model's quality")
    ev.add_argument("--yes", action="store_true", help="don't ask before spending money (the estimate is still shown, and the cap applies)")
    ev.add_argument("--max-cost", type=float, help="the most the run may spend, in USD: no new task starts once it has "
                    "(default: 1.5 times the estimate)")
    ev.add_argument("--gateway")
    ev.add_argument("--key")
    ev.set_defaults(func=cmd_eval)

    rt = sub.add_parser("router", help="the learned router: train it, see where it disagrees with the rules, choose its version")
    rsub = rt.add_subparsers(dest="router_cmd", required=True)
    rtr = rsub.add_parser("train", help="train a model on the recorded requests (needs Postgres); used only if it beats the rules")
    rtr.add_argument("--days", type=int, help="how far back to read (default: routing.learned.train_days)")
    rtr.add_argument("--min-examples", type=int, default=200, help="don't train on fewer requests with an outcome than this")
    rtr.add_argument("--dry-run", action="store_true", help="train and report, but don't save the model")
    rtr.add_argument("--config")
    rrp = rsub.add_parser("report", help="where the learned router and the rules disagree, what it would cost, and the outcomes")
    rrp.add_argument("--days", type=int, default=7)
    rrp.add_argument("--limit", type=int, default=20, help="how many disagreements to list")
    rrp.add_argument("--gateway")
    rrp.add_argument("--key")
    ru = rsub.add_parser("use", help="choose the model version gateways use ('none': the rules decide)")
    ru.add_argument("version")
    ru.add_argument("--config")
    rt.set_defaults(func=cmd_router)

    pr = sub.add_parser("prices", help="refresh prices, limits and quality scores into prices.json")
    pr.add_argument("action", choices=["refresh"])
    pr.add_argument("--config")
    pr.set_defaults(func=cmd_prices)

    md = sub.add_parser("models", help="list the models each configured backend serves")
    md.add_argument("action", choices=["discover"])
    md.add_argument("--backend", action="append")
    md.add_argument("--grep")
    md.add_argument("--limit", type=int, default=40)
    md.add_argument("--config")
    md.set_defaults(func=cmd_discover)

    cc = sub.add_parser("check", help="show which backends and models are usable with the current config and credentials")
    cc.add_argument("--config")
    cc.set_defaults(func=cmd_config_check)

    load_dotenv(os.environ.get("MERITORDER_ENV_FILE", ".env"))
    args = p.parse_args(argv)
    _setup_logging(args.log_level.upper())
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
