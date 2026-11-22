"""A request's thinking settings, fitted to the Claude model it's sent to.

Clients written for older Claude models send ``thinking: {type: "enabled", budget_tokens: N}``, which the current models
refuse with a 400 (Claude API reference, read 22 November 2026):

* ``budget_tokens`` is rejected by Fable 5/5.1/5.2 and Mythos, Opus 5.5, 5, 4.8 and 4.7, Sonnet 5.5 and 5, and Haiku 5.5.
  Opus 4.6 and Sonnet 4.6 still take it (deprecated); Haiku 4.5 and older need it.
* ``{type: "disabled"}`` is rejected by Fable and Mythos, Opus 5.5 and Sonnet 5.5 (whose way to turn thinking off is
  ``{type: "between_tools"}``, at effort high or below), and by Haiku 5.5 and Opus 5 at effort xhigh or max.

So a request for an older model's settings, sent to a current model (routing.upgrade_versions, or a client that never
updated), would fail. Here ``enabled`` becomes ``adaptive``, its budget becoming an effort when the request names none, and
``disabled`` becomes what the model takes instead. Models that take the request as it is aren't touched.
"""

from __future__ import annotations

import re

_ID = re.compile(r"claude-(fable|mythos|opus|sonnet|haiku)-(\d+)(?:[-.](\d+))?")


def _version(model_id: str) -> tuple[str, int, int] | None:
    m = _ID.search((model_id or "").lower())
    if not m:
        return None
    minor = int(m.group(3)) if m.group(3) and len(m.group(3)) <= 2 else 0  # "claude-opus-4-20250514": a date, not a minor
    return m.group(1), int(m.group(2)), minor


def budget_refused(model_id: str) -> bool:
    """Whether the model refuses ``thinking: {type: "enabled", budget_tokens}``."""
    v = _version(model_id)
    if v is None:
        return False
    family, major, minor = v
    if family in ("fable", "mythos"):
        return True
    if family == "opus":
        return (major, minor) >= (4, 7)
    return major >= 5  # sonnet, haiku


def disabled_refused(model_id: str, effort: str | None) -> bool:
    """Whether the model refuses ``thinking: {type: "disabled"}`` (at this effort)."""
    v = _version(model_id)
    if v is None:
        return False
    family, major, minor = v
    if family in ("fable", "mythos") or (family in ("opus", "sonnet") and (major, minor) >= (5, 5)):
        return True
    if (family == "haiku" and major >= 5) or (family == "opus" and major >= 5):
        return effort in ("xhigh", "max")
    return False


def effort_for_budget(budget: object, levels: list[str]) -> str | None:
    """An effort level for an old thinking budget: a rough equivalence (there's no official one), at the nearest level
    the model accepts."""
    try:
        n = int(budget)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    want = "low" if n < 4096 else "medium" if n < 16384 else "high" if n < 32768 else "xhigh"
    if not levels or want in levels:
        return want
    order = ["low", "medium", "high", "xhigh", "max"]
    below = [lv for lv in order[: order.index(want)] if lv in levels]
    return below[-1] if below else next((lv for lv in order if lv in levels), None)


def fit_thinking(up: dict, model_id: str, levels: list[str]) -> str | None:
    """Fits ``up`` (an Anthropic Messages request for ``model_id``) in place; says what changed, or None."""
    thinking = up.get("thinking")
    if not isinstance(thinking, dict):
        return None
    kind = thinking.get("type")
    config = up.get("output_config") if isinstance(up.get("output_config"), dict) else {}
    effort = config.get("effort")
    if kind == "enabled" and budget_refused(model_id):
        fitted = {"type": "adaptive", **({"display": thinking["display"]} if thinking.get("display") else {})}
        up["thinking"] = fitted
        if not effort and (e := effort_for_budget(thinking.get("budget_tokens"), levels)):
            up["output_config"] = {**config, "effort": e}
            return f"thinking budget {thinking.get('budget_tokens')} sent as adaptive thinking at effort {e}"
        return "thinking budget sent as adaptive thinking"
    if kind == "disabled" and disabled_refused(model_id, effort):
        v = _version(model_id)
        if v and v[0] == "sonnet" and effort not in ("xhigh", "max"):
            up["thinking"] = {"type": "between_tools"}
            return "thinking off sent as between_tools"
        up.pop("thinking", None)
        if not effort or effort in ("xhigh", "max"):
            up["output_config"] = {**config, "effort": "low" if "low" in (levels or ["low"]) else effort}
        return "thinking off sent as low effort (this model always thinks)"
    return None
