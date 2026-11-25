"""Keeping prices live: the worker refreshes them from the live lists every ``prices_refresh_minutes``.

Gateways reload when the stored prices change, so a refresh that finds nothing new changes nothing. A list that
can't be fetched keeps its routes' last prices (and raises an alert); a price that changes, or a direct-API price
that disagrees with OpenRouter's for the same model, is reported on the console and to the alert webhook.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from . import alerts
from .catalog import price_source, refresh_prices
from .config import Config
from .configstore import current_token

log = logging.getLogger("meritorder.prices")

FIELDS = ("input", "output", "cache_read", "cache_write", "per_page", "per_annotated_page")
PRICE_SECTIONS = ("prices", "context", "max_output", "reasoning")


def _fmt(p: dict | None) -> str:
    if p and p.get("per_page") is not None:
        return f"${p['per_page']:g} a page"
    return "none" if not p else f"${p.get('input', 0):g}/${p.get('output', 0):g}"


def changes(old: dict, new: dict) -> list[str]:
    """What changed between two price lists, in words: "openrouter:x/y $0.3/$1.2 -> $0.05/$0.57"."""
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            out.append(f"{key}: {_fmt(a)} -> {_fmt(b)}")
        elif any(a.get(f) != b.get(f) for f in FIELDS) or a.get("tiers") != b.get("tiers") or a.get("batch") != b.get("batch"):
            out.append(f"{key}: {_fmt(a)} -> {_fmt(b)}" + (" (long-prompt prices changed)" if a.get("tiers") != b.get("tiers") else "")
                       + (f" (batch {_fmt(a.get('batch'))} -> {_fmt(b.get('batch'))})" if a.get("batch") != b.get("batch") else ""))
    return out


def keep_unfetched(cfg: Config, old: dict, data: dict) -> None:
    """A list that couldn't be fetched keeps its routes' previous prices (and limits), rather than leaving them unpriced."""
    failed = {name for name, s in data.get("sources", {}).items() if not s.get("ok")}
    if not failed or not old:
        return
    kinds = {b.name: price_source(b)[0] for b in cfg.backends}
    for key, price in (old.get("prices") or {}).items():
        source = price.get("source") or kinds.get(key.split(":", 1)[0], "openrouter")
        if source in failed and key not in data["prices"]:
            data["prices"][key] = price
    for section in ("context", "max_output"):
        for k, v in (old.get(section) or {}).items():
            data.setdefault(section, {}).setdefault(k, v)
    data["reasoning"] = sorted(set(data.get("reasoning") or []) | set(old.get("reasoning") or [])) if "openrouter" in failed else data.get("reasoning", [])


async def refresh_once(cfg: Config, store, state, fetch=refresh_prices) -> dict:
    """One refresh: fetch, store if anything changed, alert, and record when it last checked (``prices_status``)."""
    _, priced, missing, data = await asyncio.to_thread(fetch, cfg, None, False)
    old = await store.get_setting("prices") or {}
    keep_unfetched(cfg, old, data)
    if old.get("quality"):
        data["quality"] = old["quality"]  # quality scores share the stored catalog; they're refreshed on their own schedule
    changed = changes(old.get("prices") or {}, data["prices"]) if old else []
    if not old or changed or any(old.get(s) != data.get(s) for s in PRICE_SECTIONS):
        await store.set_setting("prices", data)
        # Gateways reload when the configuration token changes; it covers the stored prices too.
        await state.set_config_token(await current_token(store, data))
    if old and changed:
        shown = "; ".join(changed[:8]) + (f"; and {len(changed) - 8} more" if len(changed) > 8 else "")
        await alerts.notify(f"Prices changed ({len(changed)}): {shown}", state=state, title="Prices changed", level="info", page="models")
    for d in data.get("disagreements", []):
        if await alerts._once(state, f"alert:price-disagree:{d['model']}:{d['backend']}:{d['field']}:{d['listed']}:{d['openrouter']}", 86400):
            await alerts.notify(f"{d['model']} on {d['backend']}: the provider's list price for {d['field']} is ${d['listed']:g} per million tokens, "
                                f"but OpenRouter charges ${d['openrouter']:g}. One of the price lists is probably wrong or stale: check the "
                                "provider's pricing page, and set the route's price in the config if the list is wrong.",
                                state=state, title=f"Prices disagree for {d['model']}", level="warning", page="models")
    for name, s in data.get("sources", {}).items():
        if not s.get("ok") and await alerts._once(state, f"alert:price-source:{name}", 6 * 3600):
            await alerts.notify(f"Couldn't refresh prices from {name} ({s.get('error')}); routing keeps the last prices it had.",
                                state=state, title=f"Price list unavailable: {name}", level="warning", page="models")
    status = {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"), "sources": data.get("sources", {}),
              "disagreements": data.get("disagreements", []), "missing": missing, "priced_routes": priced, "changed": changed[:50]}
    await store.set_setting("prices_status", status)
    if changed:
        log.info("prices changed: %s", "; ".join(changed[:20]))
    return status


class PriceRefresher:
    """Calls ``refresh_once`` when ``prices_refresh_minutes`` have passed (and on the first call)."""

    def __init__(self) -> None:
        self.last = 0.0

    async def maybe(self, cfg: Config, store, state, now: float, fetch=refresh_prices) -> dict | None:
        if self.last and now - self.last < cfg.prices_refresh_minutes * 60:
            return None
        self.last = now
        try:
            return await refresh_once(cfg, store, state, fetch)
        except Exception:  # noqa: BLE001 (a bad refresh must not stop the worker; prices stay as they were)
            log.exception("price refresh failed")
            return None
