"""Model catalog helpers: live prices, context windows and output limits; discover backend models.

Routing compares prices on every request, so they come from live lists, refreshed by the worker
(``prices_refresh_minutes``), never typed in:

- OpenRouter routes: OpenRouter's public catalog, which is what OpenRouter charges.
- Direct provider APIs (Anthropic, OpenAI, the Gemini API, Bedrock, Vertex, Azure, Foundry, Mistral, ...): the price list
  LiteLLM maintains (MIT licensed; fetched as data, not a dependency), long-prompt tiers included. Where a model
  also has an OpenRouter route, the two are compared and a disagreement raises an alert.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import BackendConfig, Config

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_EMBEDDINGS = "https://openrouter.ai/api/v1/embeddings/models"
LITELLM_PRICES = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DISAGREE = 0.02  # first-party list prices that differ from OpenRouter's by more than this are flagged
# LiteLLM's key prefix for each provider API, by the backend's host (Anthropic, OpenAI and Bedrock use bare ids).
_PREFIXES = [("anthropic.com", ""), ("bedrock", ""), ("api.openai.com", ""), ("mistral.ai", "mistral/"), ("deepseek.com", "deepseek/"),
             ("moonshot", "moonshot/"), ("z.ai", "zai/"), ("minimax", "minimax/"), ("x.ai", "xai/"), ("groq.com", "groq/"),
             ("generativelanguage.googleapis.com", "gemini/")]
_TIER = re.compile(r"(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost|cache_creation_input_token_cost_above_1hr"
                   r"|cache_creation_input_token_cost)_above_(\d+)k_tokens")
_TIER_FIELD = {"input_cost_per_token": "input", "output_cost_per_token": "output", "cache_read_input_token_cost": "cache_read",
               "cache_creation_input_token_cost": "cache_write", "cache_creation_input_token_cost_above_1hr": "cache_write_1h"}


def price_source(b: BackendConfig) -> tuple[str, str]:
    """Where a backend's live prices come from, and its prefix in LiteLLM's list."""
    if b.price_source != "auto":
        return b.price_source, b.price_prefix or ""
    url = (b.base_url or "").lower()
    host = urlparse(url).hostname or ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter", ""
    if b.kind != "api":
        return "none", ""
    if b.price_prefix is not None:
        return "litellm", b.price_prefix
    if b.flavor == "vertex":
        return "litellm", "vertex_ai/"
    if "azure" in url:
        return "litellm", "azure_ai/" if b.format == "anthropic" else "azure/"
    for needle, prefix in _PREFIXES:
        if needle in host:
            return "litellm", prefix
    return "none", ""


def overridden(route) -> bool:
    """A price written into the configuration (a negotiated discount, say): the live lists leave it alone."""
    return route.price is not None and route.price.source == "config"


def _non_negative(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def ocr_price(entry: dict) -> dict | None:
    """An OCR model's LiteLLM entry (``mode: ocr``) as a price per page, annotated pages and the batch API's included."""
    page = _non_negative(entry.get("ocr_cost_per_page"))
    if page is None:
        return None
    price: dict = {"input": 0.0, "output": 0.0, "per_page": page}
    if (annotated := _non_negative(entry.get("annotation_cost_per_page"))) is not None:
        price["per_annotated_page"] = annotated
    if (b_page := _non_negative(entry.get("ocr_cost_per_page_batches"))) is not None:
        price["batch"] = {"input": 0.0, "output": 0.0, "per_page": b_page}
        if (b_annotated := _non_negative(entry.get("annotation_cost_per_page_batches"))) is not None:
            price["batch"]["per_annotated_page"] = b_annotated
    return price


def litellm_price(entry: dict) -> dict | None:
    """One LiteLLM entry as a price (dollars per million tokens, or per page for OCR), with its long-prompt tiers."""
    if entry.get("mode") == "ocr" or entry.get("ocr_cost_per_page") is not None:
        return ocr_price(entry)
    if entry.get("input_cost_per_token") is None and entry.get("output_cost_per_token") is None:
        return None
    price: dict = {"input": _per_million(entry.get("input_cost_per_token")) or 0.0, "output": _per_million(entry.get("output_cost_per_token")) or 0.0}
    for key, field in (("cache_read_input_token_cost", "cache_read"), ("cache_creation_input_token_cost", "cache_write"),
                       ("cache_creation_input_token_cost_above_1hr", "cache_write_1h")):  # Claude's 1-hour writes
        if _per_million(entry.get(key)) is not None:
            price[field] = _per_million(entry.get(key))
    tiers: dict[int, dict] = {}
    for key, value in entry.items():
        m = _TIER.fullmatch(key)
        if m and _per_million(value) is not None:
            above = int(m.group(2)) * 1000
            tiers.setdefault(above, {"above": above})[_TIER_FIELD[m.group(1)]] = _per_million(value)
    complete = [t for _, t in sorted(tiers.items()) if "input" in t and "output" in t]
    if complete:
        price["tiers"] = complete
    # The batch API's price, where the list has one (most direct provider APIs): what batches are priced and routed at.
    b_in, b_out = _per_million(entry.get("input_cost_per_token_batches")), _per_million(entry.get("output_cost_per_token_batches"))
    if b_in is not None and b_out is not None:
        price["batch"] = {"input": b_in, "output": b_out}
        for key, field in (("cache_read_input_token_cost_batches", "cache_read"), ("cache_creation_input_token_cost_batches", "cache_write")):
            if _per_million(entry.get(key)) is not None:
                price["batch"][field] = _per_million(entry.get(key))
    return price


def fetch_litellm() -> dict:
    resp = httpx.get(LITELLM_PRICES, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("LiteLLM's price list isn't a JSON object")
    return data


def _per_million(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v * 1e6, 6) if v >= 0 else None


def refresh_prices(cfg: Config, catalog: dict | None = None, write: bool = True, litellm: dict | None = None) -> tuple[Path, int, list[str], dict]:
    """Build prices.json from the live lists: OpenRouter's catalog for OpenRouter routes (prices, context windows,
    output limits from ``top_provider``, which models take a reasoning effort) and LiteLLM's list for direct provider
    APIs (prices with long-prompt tiers, and limits for models OpenRouter doesn't have). A list that can't be fetched
    is reported in ``sources`` and its routes left out, so the caller can keep their previous prices.

    Returns (path, number of routes priced, routes not found in their list, the data).
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    sources: dict[str, dict] = {}
    if catalog is None:
        try:
            catalog = {"data": []}
            for url in (OPENROUTER_MODELS, OPENROUTER_EMBEDDINGS):
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                catalog["data"] += resp.json().get("data", [])
            sources["openrouter"] = {"ok": True, "fetched_at": now}
        except (httpx.HTTPError, ValueError) as e:
            catalog, sources["openrouter"] = {"data": []}, {"ok": False, "fetched_at": now, "error": f"{type(e).__name__}: {e}"[:300]}
    else:
        sources["openrouter"] = {"ok": True, "fetched_at": now}
    by_id = {m["id"]: m for m in catalog.get("data", [])}
    kinds = {b.name: price_source(b) for b in cfg.backends}
    openrouter_backends = {name for name, (src, _) in kinds.items() if src == "openrouter"}
    wants_litellm = any(kinds.get(r.backend, ("none", ""))[0] == "litellm" for m in cfg.models for r in m.routes if not overridden(r))
    if litellm is None and wants_litellm:
        try:
            litellm = fetch_litellm()
            sources["litellm"] = {"ok": True, "fetched_at": now}
        except (httpx.HTTPError, ValueError) as e:
            litellm, sources["litellm"] = {}, {"ok": False, "fetched_at": now, "error": f"{type(e).__name__}: {e}"[:300]}
    elif litellm is not None:
        sources["litellm"] = {"ok": True, "fetched_at": now}
    litellm = litellm or {}

    prices: dict[str, dict] = {}
    contexts: dict[str, int] = {}
    outputs: dict[str, int] = {}
    reasoning: set[str] = set()
    missing: list[str] = []
    for m in cfg.models:
        for r in m.routes:
            src, prefix = kinds.get(r.backend, ("none", ""))
            if src == "litellm" and sources.get("litellm", {}).get("ok"):
                key = f"{prefix}{r.price_key or r.model}"
                entry = litellm.get(key)
                price = litellm_price(entry) if isinstance(entry, dict) else None
                if price is None:
                    if not overridden(r):
                        missing.append(f"{m.name}@{r.backend} ({key})")
                    continue
                prices[f"{r.backend}:{r.model}"] = {**price, "source": "litellm"}
                if entry.get("max_output_tokens") and m.name not in outputs:
                    outputs[m.name] = int(entry["max_output_tokens"])
                if entry.get("max_input_tokens") and m.name not in contexts:
                    contexts[m.name] = int(entry["max_input_tokens"])
                continue
            if r.backend not in openrouter_backends or not sources.get("openrouter", {}).get("ok"):
                continue
            entry = by_id.get(r.model)
            if entry is None:
                if not overridden(r):
                    missing.append(f"{m.name}@{r.backend} ({r.model})")
                continue
            p = entry.get("pricing") or {}
            price = {"input": _per_million(p.get("prompt")) or 0.0, "output": _per_million(p.get("completion")) or 0.0}
            if _per_million(p.get("input_cache_read")):
                price["cache_read"] = _per_million(p.get("input_cache_read"))
            if _per_million(p.get("input_cache_write")):
                price["cache_write"] = _per_million(p.get("input_cache_write"))
            prices[f"{r.backend}:{r.model}"] = {**price, "source": "openrouter"}
            if entry.get("context_length") and m.name not in contexts:
                contexts[m.name] = int(entry["context_length"])
            most = (entry.get("top_provider") or {}).get("max_completion_tokens")
            if most and m.name not in outputs:
                outputs[m.name] = int(most)
            if "reasoning" in (entry.get("supported_parameters") or []):
                reasoning.add(m.name)

    data = {
        "source": OPENROUTER_MODELS,
        "updated": now,
        "sources": sources,
        "disagreements": disagreements(cfg, prices),
        "prices": dict(sorted(prices.items())),
        "context": dict(sorted(contexts.items())),
        "max_output": dict(sorted(outputs.items())),  # most output tokens per response
        "reasoning": sorted(reasoning),  # models that accept a reasoning effort setting
    }
    path = Path(cfg.config_dir) / cfg.prices_file
    if write:
        try:
            kept = json.loads(path.read_text()).get("quality")  # quality scores share the file
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            kept = None
        if kept:
            data["quality"] = kept
        path.write_text(json.dumps(data, indent=2) + "\n")
    return path, len(prices), missing, data


def disagreements(cfg: Config, prices: dict[str, dict]) -> list[dict]:
    """Models whose direct-API list price (LiteLLM's) differs from OpenRouter's price for the same model. They're
    the same when OpenRouter passes the maker's price through, so a difference means one list is wrong or stale. Only
    the maker's own API (and Bedrock) is compared: Azure, Vertex and Foundry may charge regional premiums."""
    out = []
    direct = {b.name for b in cfg.backends if price_source(b) == ("litellm", "")}
    for m in cfg.models:
        own = [(r.backend, prices.get(f"{r.backend}:{r.model}")) for r in m.routes]
        listed = [(b, p) for b, p in own if p and p.get("source") == "litellm" and b in direct]
        openrouter = next((p for _, p in own if p and p.get("source") == "openrouter"), None)
        if not openrouter:
            continue
        for backend, p in listed:
            for field in ("input", "output"):
                a, b = p.get(field) or 0.0, openrouter.get(field) or 0.0
                if max(a, b) > 0 and abs(a - b) / max(a, b) > DISAGREE:
                    out.append({"model": m.name, "backend": backend, "field": field, "listed": a, "openrouter": b})
    return out


async def discover(cfg: Config, backends: dict) -> dict[str, list[str] | str]:
    """List the model IDs each enabled (non-mock) backend says it serves."""
    out: dict[str, list[str] | str] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for name, backend in backends.items():
            if backend.kind == "mock" or backend.cfg.flavor == "vertex":
                continue
            base = backend.cfg.base_url.rstrip("/")
            gemini = backend.cfg.api == "gemini"  # the Gemini API lists {"models": [{"name": "models/<id>"}]}, 50 a page by default
            url = f"{base}/v1/models" if backend.format == "anthropic" else f"{base}/models?pageSize=1000" if gemini else f"{base}/models"
            headers = {"anthropic-version": "2023-06-01"} if backend.format == "anthropic" else {}
            try:
                headers = await backend.auth.apply("GET", url, headers, b"")
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    out[name] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    continue
                data = resp.json().get("data", [])
                if gemini:
                    data = [{"id": str(m["name"]).removeprefix("models/")} for m in resp.json().get("models") or [] if m.get("name")]
                out[name] = sorted(str(m.get("id")) for m in data if m.get("id"))
            except Exception as e:  # report and keep going
                out[name] = f"{type(e).__name__}: {e}"
    return out
