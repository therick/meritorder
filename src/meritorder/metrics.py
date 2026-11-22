"""Prometheus metrics, with no extra dependency (text exposition format)."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable

from .store import SAVINGS_CAUSES

LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
HISTOGRAMS = {
    "meritorder_request_duration_seconds": ("End-to-end request duration.", LATENCY_BUCKETS),
    "meritorder_time_to_first_token_seconds": ("Time to the first streamed token.", TTFT_BUCKETS),
    "meritorder_compressor_duration_seconds": ("How long the compressor service took to answer (model-based compression).",
                                               (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)),
    "meritorder_outcome_seconds": ("Time spent reading a request's tool results for coding outcomes (outcomes.py).",
                                   (0.00005, 0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01)),
    "meritorder_mcp_call_seconds": ("MCP tool calls through the gateway, from the request to the server's answer, by server.",
                                    (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300)),
    "meritorder_screening_seconds": ("Time spent screening a request's new messages in shadow mode (screening.py).",
                                     (0.00005, 0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01)),
}


def _labels(d: dict[str, str]) -> str:
    if not d:
        return ""
    inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in sorted(d.items()))
    return "{" + inner + "}"


def cost_kind(cfg, backend: str | None) -> str:
    """What a request's cost is, for meritorder_cost_usd_total's ``kind``: ``gpu_share`` on a self-hosted backend the
    capacity controller runs (its GPU time is accrued, and the burn gauge counts it: spend.py), ``gpu`` on one it doesn't
    run, else ``api``."""
    b = cfg.backend(backend) if backend else None
    if b is None or b.kind != "self_hosted":
        return "api"
    return "gpu_share" if b.serve is not None else "gpu"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self.hist: dict[str, dict[tuple, list[float]]] = defaultdict(dict)
        self.hist_sum: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        # Called at scrape time for current values (in-flight requests, GPU state): (name, help, labels, value).
        self.gauges: Callable[[], list[tuple[str, str, dict, float]]] | None = None
        self.help = {
            "meritorder_requests_total": "Requests by served model, backend, and HTTP status.",
            "meritorder_tokens_total": "Tokens by model, backend, and kind (input, cache_read, cache_write, output).",
            "meritorder_cost_usd_total": "Actual cost in USD, by kind: api; gpu_share, a request's share of a GPU whose time the "
                                         "controller accrues (already in meritorder_gpu_usd_per_hour, so leave it out of totals); "
                                         "gpu, a request on a self-hosted backend no controller runs (its share is its cost).",
            "meritorder_baseline_usd_total": "What the same requests would have cost without Merit Order.",
            "meritorder_savings_usd_total": "Savings in USD by cause (compression: the rules; compression_model: model-based).",
            "meritorder_compression_model_results_total": "Tool results model-based compression looked at, by what happened: "
                                                          "compressed, kept (nothing to cut), reused (a stored decision), old "
                                                          "(already sent as it is), late (past the deadline), failed, no_prose, "
                                                          "store_error.",
            "meritorder_rejected_requests_total": "Requests answered with an error before reaching a backend (auth, budget, "
                                                  "rate limit, routing), by status and reason.",
            "meritorder_failovers_total": "Attempts that failed on a backend and moved on to the next source, by backend and status.",
            "meritorder_screening_findings_total": "Request screening's findings in shadow mode (decision 71), by kind and team: "
                                                   "hidden Unicode, keys, risky tool calls, untrusted content.",
            "meritorder_mcp_calls_total": "MCP tool calls through the gateway (decision 75), by server, team and status: ok, "
                                          "error (the tool said it failed), failed (no usable answer), refused (by the gateway), "
                                          "no_answer.",
            "meritorder_mcp_findings_total": "What the MCP gateway noticed in tool lists, calls and results, by server, kind and team: "
                                             "changed or new tool definitions, steering or hidden text in descriptions, keys, "
                                             "refusals.",
            "meritorder_gpu_cache_turns_total": "Turns on a GPU deployment by whether the session's cache was expected warm there "
                                                "and whether the reply said it was (hit: at least half the prompt read from the cache).",
            "meritorder_gpu_replica_unreachable_total": "Times a gateway couldn't connect to a GPU replica directly and used the "
                                                        "deployment's endpoint instead.",
            "meritorder_token_calibration_samples_total": "Provider prompt token counts that calibrated the gateway's estimate, by model.",
            "meritorder_responses_websocket_total": "The Responses API's WebSocket transport, by event: connected (a connection "
                                                    "opened), response (a turn sent in full or chained from stored mode), "
                                                    "local_chain (a turn rebuilt from its connection's memory), prepared "
                                                    "(generate: false, no model call).",
            "meritorder_stored_responses_total":"Responses API turns in a team's stored mode, by event: rebuilt (its input built from "
                                                 "what the gateway stored), stored (kept), provider_chain (sent as the provider's own "
                                                 "continuation), failed (couldn't be kept).",
            "meritorder_learned_router_total":"Requests the learned router saw, by mode and outcome (used, agrees, shadow, unsure, "
                                               "stale, missing, failed; named: a named model's request, recorded for training).",
            "meritorder_judged_labels_total": "Requests sampled for judged labels, by what came of them: judged, dropped (the "
                                              "replay couldn't start in time, failed, or the judge's reply was unusable), skipped "
                                              "(the answer's model was as strong), capped (the monthly cap was spent).",
            # Batch requests, counted by the process that settles them into the ledger: the worker (the gateway itself only
            # without Postgres). Never in the live counters above, so adding the two never counts a request twice.
            "meritorder_batch_requests_total": "Batch requests settled into the ledger, by served model, backend, the API that ran "
                                               "them (a provider's batch API, or gpu) and HTTP status.",
            "meritorder_batch_tokens_total": "Tokens of settled batch requests by model, backend, and kind (input, cache_read, "
                                             "cache_write, output).",
            "meritorder_batch_cost_usd_total": "What settled batch requests cost in USD, by kind (as meritorder_cost_usd_total).",
            "meritorder_batch_baseline_usd_total": "What the same batch requests would have cost without Merit Order (live API prices).",
            "meritorder_batch_savings_usd_total": "Savings of settled batch requests in USD by cause (batch: the provider's batch "
                                                  "discount; self_hosting: run on a GPU below the batch price; routing).",
            "meritorder_batch_gpu_requests_total": "Batch requests sent to a self-hosted GPU, by backend and what happened (ok, "
                                                   "retry, handed_off).",
            "meritorder_batch_handoffs_total": "Batch requests moved from a GPU to a provider's batch API, by GPU backend and why "
                                               "(deadline, stopped, failed).",
            "meritorder_batch_poll_errors_total": "Provider batch jobs that couldn't be checked, by backend and status.",
        }

    def inc(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        with self._lock:
            self.counters[name][tuple(sorted(labels.items()))] += value

    def observe(self, name: str, labels: dict[str, str], seconds: float) -> None:
        bounds = HISTOGRAMS[name][1]
        key = tuple(sorted(labels.items()))
        with self._lock:
            buckets = self.hist[name].setdefault(key, [0.0] * (len(bounds) + 1))
            for i, b in enumerate(bounds):
                if seconds <= b:
                    buckets[i] += 1
            buckets[-1] += 1
            self.hist_sum[name][key] += seconds

    def observe_latency(self, labels: dict[str, str], seconds: float) -> None:
        self.observe("meritorder_request_duration_seconds", labels, seconds)

    def record(self, row: dict, paid_as: str = "api") -> None:
        """One request's ledger row. ``paid_as``: what its cost is, the cost counter's ``kind`` label (cost_kind)."""
        base = {"model": row.get("served_model") or "", "backend": row.get("backend") or ""}
        self.inc("meritorder_requests_total", {**base, "status": str(row.get("status"))})
        for kind in ("input", "cache_read", "cache_write", "output"):
            n = row.get(f"{kind}_tokens") or 0
            if n:
                self.inc("meritorder_tokens_total", {**base, "kind": kind}, n)
        self.inc("meritorder_cost_usd_total", {**base, "kind": paid_as}, row.get("actual_cost") or 0)
        self.inc("meritorder_baseline_usd_total", base, row.get("baseline_cost") or 0)
        for cause in SAVINGS_CAUSES:
            v = row.get(f"savings_{cause}") or 0
            if v:
                self.inc("meritorder_savings_usd_total", {"cause": cause}, v)
        if row.get("latency_ms") is not None:
            self.observe_latency(base, row["latency_ms"] / 1000)
        if row.get("ttft_ms") is not None:
            self.observe("meritorder_time_to_first_token_seconds", base, row["ttft_ms"] / 1000)

    def record_batch(self, row: dict, api: str = "", paid_as: str = "api") -> None:
        """One settled batch request (its ledger row), in the batch counters only."""
        base = {"model": row.get("served_model") or "", "backend": row.get("backend") or ""}
        self.inc("meritorder_batch_requests_total", {**base, "api": api, "status": str(row.get("status"))})
        for kind in ("input", "cache_read", "cache_write", "output"):
            n = row.get(f"{kind}_tokens") or 0
            if n:
                self.inc("meritorder_batch_tokens_total", {**base, "kind": kind}, n)
        self.inc("meritorder_batch_cost_usd_total", {**base, "kind": paid_as}, row.get("actual_cost") or 0)
        self.inc("meritorder_batch_baseline_usd_total", base, row.get("baseline_cost") or 0)
        for cause in SAVINGS_CAUSES:
            v = row.get(f"savings_{cause}") or 0
            if v:
                self.inc("meritorder_batch_savings_usd_total", {"cause": cause}, v)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self.counters.items()):
                lines.append(f"# HELP {name} {self.help.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                for labels, v in sorted(series.items()):
                    lines.append(f"{name}{_labels(dict(labels))} {v:.10g}")
            for name, series in sorted(self.hist.items()):
                help_, bounds = HISTOGRAMS[name]
                lines.append(f"# HELP {name} {help_}")
                lines.append(f"# TYPE {name} histogram")
                for key, buckets in sorted(series.items()):
                    labels = dict(key)
                    for b, count in zip(bounds, buckets):
                        lines.append(f"{name}_bucket{_labels({**labels, 'le': str(b)})} {count:.10g}")
                    lines.append(f"{name}_bucket{_labels({**labels, 'le': '+Inf'})} {buckets[-1]:.10g}")
                    lines.append(f"{name}_sum{_labels(labels)} {self.hist_sum[name][key]:.10g}")
                    lines.append(f"{name}_count{_labels(labels)} {buckets[-1]:.10g}")
        if self.gauges is not None:
            by_name: dict[str, tuple[str, list]] = {}
            for name, help_, labels, value in self.gauges():
                by_name.setdefault(name, (help_, []))[1].append((labels, value))
            for name, (help_, series) in sorted(by_name.items()):
                lines.append(f"# HELP {name} {help_}")
                lines.append(f"# TYPE {name} gauge")
                for labels, value in series:
                    lines.append(f"{name}{_labels(labels)} {value:.10g}")
        return "\n".join(lines) + "\n"
