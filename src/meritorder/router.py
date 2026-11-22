"""Marginal-cost router (docs/03-routing.md).

For each candidate backend b over the session's expected remaining turns:

    cost(b) = rebuild(b) + expected_remaining_turns * per_turn(b)

* per_turn uses API prices, or for self-hosted backends a shadow price that is
  near zero while the replica has headroom and rises toward the API price as it fills.
* rebuild is the extra cost of re-processing the part of the prefix b no longer holds: zero when b holds a
  warm cache for this session, all of it when it's cold. On a GPU deployment the part still cached is per replica
  (affinity.py): the router prices each replica it can address, with its own load and warm share, and picks one.

Routes within the priority's premium of the cheapest are then ranked by measured speed (speed.py): with the
default `cost` priority that only decides between routes that cost the same (2%), so the faster one wins.

Prompt sizes are counted per model (tokens.py): the request's estimate, corrected by what that model's replies have
reported, so context-window checks, long-prompt prices and cost estimates use each model's own tokens.

With ``model_choice: cost_per_success`` an `auto` request may also go to a model a little below its level, ranked by
expected cost per solved task instead (``Router._per_success``): a model's chance of solving work at the level
(config.apply_chances) makes a cheaper, slightly weaker model worth it when it still costs less per success.

With ``model_choice: expected_cost`` (decision 62) routes are ranked by the expected total cost to finish the task
(``Router._expected_total``, success.py): the try, and if it fails, the person's time the request puts at stake
(``human_cost_usd`` scaled by its level and size, ``Router.stakes``, decision 67) and the escalation to a stronger model
from a cold cache. The tie band and the margin for moving a session are shares of the money in it (``Candidate.spend``),
not of the person's time. Inside a task the cascade (``Router._escalate``) moves the task up when what its tool results
show makes continuing cost more than escalating, at a turn where that's allowed.

With ``routing.learned`` on, the learned router (learned.py) gives its own level next to the rules' (``Router.level``):
in shadow mode it's recorded with what it would have picked and changes nothing; in route mode it's used when it's sure.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

from .affinity import Replica, Warmth, share_on
from .backends import Backend, describe
from .cache_shaping import HOUR, needs_cache_marker
from . import datapolicy
from .classifier import Floor, classify, parse_floor
from .config import PRIORITIES, UNPRICED, Config, ModelConfig, Price, Route, TeamConfig, needs_price
from .learned import LearnedCall, LearnedRouter, features
from .pauses import Pauses
from .sessions import Session
from .speed import Speeds
from .outcomes import correction
from .success import Chance, Difficulty, Option, Predictor, Stakes, TaskContext, continues, failures_in_row, plan, posterior, stakes
from .tokens import CLOSE_SHARE, Calibration, Estimate

log = logging.getLogger("meritorder.router")

# A prompt over this share of a model's window is too big for it: the reply needs room, and the token count is an
# estimate (calibrated per model from what its replies report, see tokens.py).
FIT_SHARE = 0.95


def asked_reply(body: dict) -> int:
    """The reply length the client asked for (its max_tokens), or 0 when it left that to the gateway."""
    try:
        return max(0, int(body.get("max_tokens") or body.get("max_completion_tokens") or 0))
    except (TypeError, ValueError):
        return 0


def reply_room(model: ModelConfig, body: dict) -> int:
    """Room the reply needs in this model's window: the client's max_tokens as the gateway sends it (no more than the
    model can write). Without one the gateway gives the reply what's left of the window (proxy.output_allowance), so
    FIT_SHARE's margin is all it needs."""
    asked = asked_reply(body)
    return min(asked, model.max_output_tokens) if asked and model.max_output_tokens else asked


def fits(model: ModelConfig, prompt_tokens: int, reply_tokens: int = 0) -> bool:
    """Whether the model's context window holds the prompt plus room for the reply. This, not a level, is what a long
    context changes: it rules out the models too small for it. A window that isn't known fits."""
    return not model.context_window or prompt_tokens + reply_tokens <= model.context_window * FIT_SHARE


def _needs(prompt_tokens: int, body: dict) -> str:
    asked = asked_reply(body)
    return f"about {prompt_tokens:,} tokens" + (f" plus a {asked:,}-token reply" if asked else "")


def _windows(models: list[ModelConfig]) -> str:
    return ", ".join(f"{m.name} ({m.context_window:,})" for m in models)


class RoutingError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error", headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.headers = headers or {}


@dataclass
class Candidate:
    model: ModelConfig
    route: Route
    backend: Backend
    cost: float
    warm: bool
    quality: int
    upgraded: bool = False
    queued: bool = False  # sent to a saturated backend because nothing else could take it
    replica: Replica | None = None  # the GPU replica to send it to (None: the deployment's endpoint picks)
    warmth: Warmth | None = None  # how much of the session's prefix is probably cached where it goes
    placement: str = ""  # why this replica, for the routing reason
    prompt_tokens: int = 0  # the prompt in this model's tokens (the estimate, calibrated for it); 0 = not counted
    # Routing by cost per success only: the model's chance of solving work at the request's level, and the expected cost
    # per solved task, which then ranks the routes instead of ``cost``.
    chance: float | None = None
    expected: float | None = None
    cold: float | None = None  # what the route costs from a cold cache (expected_cost: where an escalation would go)
    why: Chance | None = None  # expected_cost: what the chance is made of
    tokens: float | None = None  # expected_cost: the part of ``expected`` that's tokens (the try, and any redo's)

    @property
    def self_hosted(self) -> bool:
        return self.backend.kind == "self_hosted"

    @property
    def ranked_cost(self) -> float:
        """What routes are compared on: the expected cost per success where it's known, else the session's cost."""
        return self.expected if self.expected is not None else self.cost

    @property
    def spend(self) -> float:
        """The money in ``ranked_cost``: without the person's time that expected_cost adds. The tie band and the margin for
        moving a session are shares of this, so a person's cost of a failure never widens them (decision 67)."""
        return self.tokens if self.tokens is not None else self.ranked_cost


@dataclass
class Decision:
    requested: str
    auto: bool
    floor: Floor | None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""
    substituted_for: str | None = None  # the named model that couldn't run (team opted in to substitution)
    priority: str = "cost"
    last_resort: bool = False  # every route is saturated or over capacity: these are tried anyway
    learned: LearnedCall | None = None  # what the learned router said, when it's on and the rules gave a level
    # Exploration (decision 59): the arm this task start drew, for a team that explores: {"arm": served|down|up|canary,
    # "from": the level it would have had, "level": the level it got, "p": the chance of that arm, "model": a canary's}.
    explore: dict | None = None
    # Expected total cost (decision 62): the chosen model's and the runner-up's chance and expected costs, and why
    # ({"model", "p", "prior", "fitted", "expected", "tokens", "runner_up": {...}, "human_cost" (at stake), "stakes",
    # "difficulty", "why"}).
    predicted: dict | None = None
    # An escalation inside the task: {"n", "from", "to", "stage", "reason", "p_before", "p_now", "continue", "escalate"}.
    escalation: dict | None = None
    context: TaskContext | None = None  # the task's hashes and triage buckets, recorded on its route_features row


def shadow_multiplier(util: float, knee: float, floor: float) -> float | None:
    """Fraction of API price a self-hosted token "costs" at this utilization. None = full."""
    if util >= 1.0:
        return None
    if util <= knee:
        return floor
    return floor + (1.0 - floor) * ((util - knee) / (1.0 - knee)) ** 2


def _scaled(price: Price, m: float) -> Price:
    return Price(input=price.input * m, output=price.output * m, cache_read=price.read() * m, cache_write=price.write() * m,
                 cache_write_1h=price.write_1h() * m)


def _usd(x: float) -> str:
    """Two significant figures for small amounts ($0.0043), cents for the rest ($0.12, $1.50)."""
    if x <= 0:
        return "$0"
    return f"${x:,.{max(2, 1 - math.floor(math.log10(x)))}f}"


class Router:
    def __init__(self, cfg: Config, backends: dict[str, Backend], speeds: Speeds | None = None, tokens: Calibration | None = None,
                 learned: LearnedRouter | None = None, pauses: Pauses | None = None, success: Predictor | None = None):
        self.cfg = cfg
        self.backends = backends
        self.speeds = speeds if speeds is not None else Speeds()
        self.tokens = tokens if tokens is not None else Calibration()  # outlives the configuration, like speeds
        self.learned = learned if learned is not None else LearnedRouter()  # the stored model, loaded by the gateway
        self.pauses = pauses if pauses is not None else Pauses()  # how often sessions pause 5-60 minutes (pauses.py)
        self.success = success if success is not None else Predictor()  # the fitted success layer, loaded by the gateway
        self.rand = random.random  # exploration's draws (tests replace it)

    def priority(self, team: TeamConfig, headers: dict[str, str]) -> str:
        """The request's X-MeritOrder-Priority, else the team's, else routing.priority."""
        asked = (headers.get("x-meritorder-priority") or "").strip().lower()
        if asked:
            if asked not in PRIORITIES:
                raise RoutingError(400, f"X-MeritOrder-Priority must be one of {', '.join(PRIORITIES)}, not '{asked}'",
                                   "invalid_request_error")
            return asked
        return team.priority or self.cfg.routing.priority

    def _seconds(self, c: Candidate) -> float:
        e = self.speeds.expected(c.backend.name, c.route.model, self.cfg.routing.est_output_tokens)
        return math.inf if e is None else e

    def _speed_note(self, c: Candidate) -> str:
        s = self._seconds(c)
        return f"~{s:.1f}s" if s != math.inf else "not measured yet"

    def _needs_check(self, c: Candidate, now: float) -> bool:
        s = self.speeds.get(c.backend.name, c.route.model)
        return s is None or s.stale(now)

    def rank(self, cands: list[Candidate], priority: str, new_session: bool, now: float) -> tuple[list[Candidate], str]:
        """Order cost-sorted candidates by the priority. Routes within its premium of the cheapest compete on
        speed (and, for `auto`, quality); the rest stay in cost order behind them. Routing by cost per success compares
        expected costs per success here instead, and by expected total cost the expected totals, with the premium a share of
        the cheapest's money (``Candidate.spend``), not of the person's time in its total."""
        r = self.cfg.routing
        limit = cands[0].ranked_cost + cands[0].spend * r.priority_premium_pct[priority] / 100 + 1e-9
        group = [c for c in cands if c.ranked_cost <= limit]
        rest = [c for c in cands if c.ranked_cost > limit]
        if len(group) < 2:
            return cands, ""
        if priority == "cost":
            # Close enough on price: the better model first (for `auto`), then the faster route.
            ranked = sorted(group, key=lambda c: (-c.quality, self._seconds(c)))
        else:
            ranked = sorted(group, key=lambda c: (self._seconds(c), -c.quality))
        lead, cheapest = ranked[0], group[0]
        note = ""
        if lead is not cheapest and lead.quality == cheapest.quality and self._seconds(lead) < self._seconds(cheapest):
            extra = (lead.spend / cheapest.spend - 1) * 100 if cheapest.spend > 0 else 0.0
            note = (f"; {priority} priority: {lead.backend.name} ({self._speed_note(lead)}) over {cheapest.backend.name} "
                    f"({self._speed_note(cheapest)})" + (f" for {extra:.0f}% more" if extra >= 1 else ""))

        if new_session and r.speed_check_every:
            # Keep measuring the alternatives, or the first route measured would win forever. Only new
            # sessions (no cache to lose) and only routes the priority already allows.
            self.speeds.decisions += 1
            pool = [c for c in ranked[1:] if priority != "cost" or c.quality == lead.quality]
            stale = [c for c in pool if self._needs_check(c, now)]
            if stale and self.speeds.decisions >= r.speed_check_every:
                self.speeds.decisions = 0

                def oldest(c: Candidate) -> tuple[bool, float]:
                    s = self.speeds.get(c.backend.name, c.route.model)
                    return (s is not None and s.measured, s.last if s else 0.0)

                pick = min(stale, key=oldest)  # never measured first, then the longest ago
                ranked.remove(pick)
                ranked.insert(0, pick)
                note = f"; checking {pick.backend.name}'s speed ({self._speed_note(pick)})"
        return ranked + rest, note

    def window_note(self, m: ModelConfig, tokens: int, reply: int = 0) -> str:
        """How full a model's window is and how the count was made, for routing reasons."""
        plus = f", plus a {reply:,}-token reply" if reply else ""
        return (f"{tokens:,} of {m.context_window:,} tokens{plus}, {(tokens + reply) / m.context_window:.0%}; "
                f"{self.tokens.describe(m.name)}")

    def _needs_each(self, models: list[ModelConfig], n, body: dict, what: str) -> str:
        """"a (w), b (w): <what> about N tokens (how it was counted)", one clause per count: each model has its own."""
        groups: dict[tuple[int, str], list[ModelConfig]] = {}
        for m in models:
            groups.setdefault((n(m), self.tokens.describe(m.name)), []).append(m)
        return "; ".join(f"{_windows(ms)}: {what} {_needs(count, body)} ({how})" for (count, how), ms in groups.items())

    def resolve(self, requested: str) -> ModelConfig | None:
        return self.cfg.model(requested)

    def cap_for_class(self, floor: Floor, headers: dict[str, str]) -> Floor:
        """Claude Code labels each request (``x-claude-code-request-class``). Side requests (session titles, summaries,
        permission checks) and the summary written when it compacts a conversation don't need the level of the
        work itself: ``routing.request_class_levels`` caps them. An explicit X-Route-Floor is never capped."""
        cls = (headers.get("x-claude-code-request-class") or "").lower()
        cap = self.cfg.routing.request_class_levels.get(cls)
        if cap is None or floor.explicit or floor.level <= cap:
            return floor
        return Floor(floor.task, cap, f"{floor.reason}; capped at {cap} for a Claude Code {cls} request")

    def classify(self, body: dict, headers: dict[str, str], prompt_tokens: int) -> Floor:
        """The request's level from its work (routing.levels), with Claude Code's side jobs capped."""
        return self.cap_for_class(classify(body, headers, prompt_tokens, self.cfg.routing.levels), headers)

    def level(self, body: dict, headers: dict[str, str], prompt_tokens: int, client: str = "") -> tuple[Floor, LearnedCall | None]:
        """The request's level, and what the learned router said about it when it's on. The rules give the level; in
        route mode the learned router's replaces it when it's sure, and Claude Code's caps apply to whichever is used. A
        level the client set (X-Route-Floor) is never second-guessed."""
        rules = classify(body, headers, prompt_tokens, self.cfg.routing.levels)
        settings = self.cfg.routing.learned
        if settings.mode == "off" or rules.explicit:
            return self.mapped(self.cap_for_class(rules, headers)), None
        call = self.learned.consult(rules, body, headers, prompt_tokens, client, settings, self.cfg.routing.levels)
        learned = call.learned_floor()
        capped_rules = self.cap_for_class(rules, headers)
        if learned is not None and learned.level != rules.level:
            capped_learned = self.cap_for_class(learned, headers)
            if (capped_rules.level, capped_learned.level) != (rules.level, learned.level):
                call.capped = (capped_rules.level, capped_learned.level)
            if call.used:
                return self.mapped(capped_learned), call
        return self.mapped(capped_rules), call

    def mapped(self, floor: Floor) -> Floor:
        """A level the outcome report's recommendation moved (routing.level_map, decision 59): the work the rules put at
        coding:3 routed at the level given. Never a client's own X-Route-Floor."""
        to = self.cfg.routing.level_map.get(str(floor))
        if to is None or to == floor.level or floor.explicit:
            return floor
        return Floor(floor.task, to, f"{floor.reason}; {floor} moved to {to} by routing.level_map (the outcome report)")

    def _explore(self, floor: Floor, team: TeamConfig, headers: dict[str, str], session: Session | None, now: float,
                 up: bool) -> tuple[Floor, dict | None, str | None]:
        """Draw this task start's exploration arm (decision 59): the level it's routed at, the arm (None when the team
        doesn't explore here) and a canary's model. Only a new task whose cache is gone anyway (a new session, or one
        with nothing warm), so no cache is thrown away; never a client's X-Route-Floor or Claude Code's capped classes;
        ``up`` False (a budget nearly spent) never explores up. Propensities are recorded so reports can weight by them."""
        x = team.exploration
        if not x.enabled or self.cfg.routing.learned.mode == "off" or floor.explicit or parse_floor(headers.get("x-route-floor", "")):
            return floor, None, None
        cls = (headers.get("x-claude-code-request-class") or "").lower()
        if cls in self.cfg.routing.request_class_levels:
            return floor, None, None
        if session is not None and any(t > now for t in session.warm_until.values()):
            return floor, None, None  # a cache is still warm somewhere: moving now would throw it away
        canary = next((c for c in self.cfg.canaries if c.get("task") == floor.task and c.get("level") == floor.level
                       and team.allows_model(str(c.get("model"))) and (m := self.cfg.model(str(c.get("model")))) is not None
                       and m.q(floor.task) >= floor.level), None)
        p_canary = x.canary_share if canary else 0.0
        ways = [w for w, ok in (("down", floor.level > 1), ("up", floor.level < 5)) if ok]
        each = (1 - p_canary) * x.share / len(ways)  # the whole share one way at level 1 or 5
        ways = [w for w in ways if w != "up" or up]  # a budget nearly spent: up's half stays served
        u = self.rand()
        base = {"from": floor.level, "share": x.share}
        if u < p_canary:
            note = f"exploring: canary {canary['model']} at {floor} ({p_canary:.0%} of task starts here until it's tried)"
            return (Floor(floor.task, floor.level, f"{floor.reason}; {note}"),
                    {**base, "arm": "canary", "level": floor.level, "p": round(p_canary, 6), "model": canary["model"]}, canary["model"])
        u -= p_canary
        for way in ways:
            if u < each:
                level = floor.level - 1 if way == "down" else floor.level + 1
                note = f"exploring: one level {way} from {floor} ({each:.1%} of task starts)"
                return Floor(floor.task, level, f"{floor.reason}; {note}"), {**base, "arm": way, "level": level, "p": round(each, 6)}, None
            u -= each
        served = 1 - p_canary - each * len(ways)
        return floor, {**base, "arm": "served", "level": floor.level, "p": round(served, 6),
                       "avoid": canary["model"] if canary else None}, None

    def record_features(self, body: dict, headers: dict[str, str], prompt_tokens: int, client: str) -> LearnedCall | None:
        """For a request that names its model: the features and the rules' level, as training data (a named model shows
        how a model scoring other than the level does on such work). No prediction: nothing would change."""
        settings = self.cfg.routing.learned
        if settings.mode == "off":
            return None
        rules = classify(body, headers, prompt_tokens, self.cfg.routing.levels)
        if rules.explicit:
            return None
        return LearnedCall(settings.mode, rules, features(rules, body, headers, prompt_tokens, client, self.learned.seed,
                                                         self.cfg.routing.levels.agent_tools),
                           self.learned.fv, outcome="named", note="")

    def _mock_only(self, m: ModelConfig) -> bool:
        """No route on a live backend other than a mock (mocks are for trying things out)."""
        return all((b := self.backends.get(r.backend)) is None or b.kind == "mock" for r in m.routes)

    def _expected_turns(self, session: Session | None) -> int:
        r = self.cfg.routing
        turns = session.turns if session else 0
        return max(r.min_expected_turns, min(r.max_expected_turns, turns or r.min_expected_turns))

    def estimate(
        self, price: Price, backend: Backend, prompt_tokens: int, session: Session | None, warm: float, turns: int, route_model: str = "",
        new_tokens: int | None = None, pause: float = 0.0,
    ) -> float:
        """``warm``: the share of the prefix already cached there (True/1: all of it, False/0: none). ``new_tokens``: what
        the conversation grew by since its last turn, in this model's tokens (default: from the session's last prompt size).
        ``pause``: the share of later turns that come after a 5-60 minute pause (pauses.py): each costs a rebuild where the
        cache lasts under an hour, and nothing where it lasts an hour (decision 60)."""
        r = self.cfg.routing
        new = r.est_new_tokens_per_turn
        if new_tokens is not None:
            new = new_tokens
        elif session and session.prefix_tokens:
            new = max(prompt_tokens - session.prefix_tokens, 200)
        cached = max(prompt_tokens - new, 0)
        # Steady state: the prefix is read from cache, new tokens are written to it. Only explicit caching (Claude:
        # Anthropic's API, Bedrock, Vertex, Foundry, or OpenRouter with a cache marker) charges for writes; everyone
        # else caches automatically and bills new tokens at the input price, whatever a price list says about writes.
        # A backend set to an hour gets 1-hour markers only once a session has come back (build_upstream's later_turn,
        # decision 53): a session's first turn, rebuild included, writes at the 5-minute price and the turns after it
        # at the 1-hour price, as the gateway sends them.
        explicit = backend.format == "anthropic" or needs_cache_marker(backend.cfg, route_model)
        if not explicit:
            first_write = later_write = price.input
        elif backend.cfg.cache_ttl_seconds >= HOUR:
            later_write = price.write_1h()
            first_write = later_write if session is not None and session.turns > 0 else price.write()
        else:
            first_write = later_write = price.write()

        def per_turn(write_price: float) -> float:
            return (cached * price.read() + new * write_price + r.est_output_tokens * price.output) / 1e6

        rebuild = (1.0 - min(max(float(warm), 0.0), 1.0)) * cached * max(first_write - price.read(), 0.0) / 1e6
        # A later turn after a pause longer than the cache lasts writes the prefix again: counted where that's under an
        # hour, at the expected share of such pauses. (Pauses over an hour rebuild everywhere, so they don't compare.)
        lost = pause * cached * max(later_write - price.read(), 0.0) / 1e6 if backend.cfg.cache_ttl_seconds < HOUR else 0.0
        return rebuild + min(turns, 1) * per_turn(first_write) + max(turns - 1, 0) * (per_turn(later_write) + lost)

    def place(self, backend: Backend, price: Price, prompt_tokens: int, session: Session | None, turns: int,
              utilization: dict[str, float], now: float, note: dict | None, route_model: str,
              new_tokens: int | None = None) -> Candidate | None:
        """Price a GPU deployment for this request: its cost, warm share and (when its replicas can be addressed)
        the replica to send it to. None when it can't take the request (draining, and the session isn't warm there).

        Each replica is priced like any route: the rebuild of what it no longer holds plus the turns at its own
        shadow price, so a warm replica that's busy can lose to an idle cold one. Staying on the session's replica
        needs the same margin as staying on a backend (``switch_margin_pct``, ``switch_margin_usd``). A full replica
        takes nothing new while another has room; when all are full, the request queues on the one with the
        shortest queue (``queued``). The model, route and quality are filled in by the caller."""
        r = self.cfg.routing

        def cand(cost: float, w: Warmth, replica: Replica | None = None, queued: bool = False, why: str = "") -> Candidate:
            return Candidate(model=None, route=None, backend=backend, cost=cost, warm=w.warm, quality=0,  # type: ignore[arg-type]
                             queued=queued, replica=replica, warmth=w, placement=why)

        def cost(mult: float | None, w: Warmth) -> float:
            return self.estimate(price if mult is None else _scaled(price, mult), backend, prompt_tokens, session, w.share, turns,
                                 route_model, new_tokens)

        reps = backend.addressable(now)
        if not reps:
            # The load balancer finds the replica holding the cache about one time in N; a draining deployment still
            # finishes every session cached on any of its replicas.
            if backend.draining and not share_on(session, backend.name, None, now, note).warm:
                return None
            w = share_on(session, backend.name, None, now, note, max(1, backend.replicas or 1))
            mult = shadow_multiplier(utilization.get(backend.name, 0.0), r.shadow_knee, r.shadow_floor)
            return cand(cost(mult, w), w, queued=mult is None)

        placed = ((session.placement or {}).get(backend.name) or {}).get("replica") if session else None
        options: list[tuple[float, Replica, Warmth, float]] = []
        full: list[tuple[Replica, Warmth, float]] = []
        for rep in reps:
            w = share_on(session, backend.name, rep, now, note)
            if backend.draining and not w.warm:
                continue  # a draining deployment only finishes sessions whose cache is there
            base = rep.utilization if rep.utilization is not None else utilization.get(backend.name, 0.0)
            load = max(base, utilization.get(f"{backend.name}#{rep.id}", 0.0))
            mult = shadow_multiplier(load, r.shadow_knee, r.shadow_floor)
            if mult is None:
                full.append((rep, w, load))
            else:
                options.append((cost(mult, w), rep, w, load))
        if not options and not full:
            return None

        def label(rep: Replica, w: Warmth, load: float) -> str:
            return f"replica {rep.id} ({w.describe()}, {load:.0%} load)"

        gone = ""
        if placed is not None and placed not in {rep.id for rep in reps}:
            was = next((x for x in backend.replica_set if x.id == placed), None)
            gone = f"replica {placed} is {was.unavailable() if was else 'gone'}; "
        if not options:
            rep, w, load = min(full, key=lambda x: (x[0].waiting or 0, -x[1].share))
            return cand(cost(None, w), w, rep, queued=True, why=f"{gone}every replica is full: queued on {label(rep, w, load)}")
        options.sort(key=lambda o: (o[0], o[3]))  # equal cost (below the shadow price's knee): the less loaded one
        best = options[0]
        current = next((o for o in options if o[1].id == placed), None)
        why = f"{gone}{label(best[1], best[2], best[3])}"
        if current is not None and current is not best:
            if current[2].share > 0 and best[0] + current[0] * r.switch_margin_pct / 100.0 + r.switch_margin_usd >= current[0]:
                best, why = current, f"staying on {label(current[1], current[2], current[3])}"
            else:
                why = f"{label(best[1], best[2], best[3])} over {label(current[1], current[2], current[3])}"
        elif current is None and any(rep.id == placed for rep, _, _ in full):
            why = f"replica {placed} is full; {why}"
        return cand(best[0], best[2], best[1], why=why)

    def model_choice(self, team: TeamConfig) -> tuple[str, float]:
        """The team's model_choice and min_success_chance, else the organization's (routing.*)."""
        r = self.cfg.routing
        return team.model_choice or r.model_choice, team.min_success_chance or r.min_success_chance

    def _in_reach(self, meeting: list[ModelConfig], floor: Floor, team: TeamConfig, minimum: float) -> tuple[list[ModelConfig], str | None]:
        """The models routing by cost per success may pick for this floor: every allowed model whose chance at the level
        is at least ``minimum``, whatever its score, and those without an estimate that meet the level (the threshold
        judges them). Returns ``meeting`` (the threshold's models) and why, when chances can't decide."""
        task, level = floor.task, floor.level
        if task not in self.cfg.threshold_chance:
            return meeting, f"no chances of success for {task}, so the threshold decides"
        reach = []
        for m in self.cfg.models:
            if m.kind != "chat" or m.q(task) <= 0 or not team.allows_model(m.name):
                continue
            chance = m.chances.get(task, {}).get(level)
            if (chance is not None and chance >= minimum) or (chance is None and m.q(task) >= level):
                reach.append(m)
        if not reach:
            return meeting, f"no model has at least a {minimum:.0%} chance at {floor}, so the threshold decides"
        return reach, None

    def _per_success(self, cands: list[Candidate], floor: Floor) -> Candidate:
        """Set each candidate's chance at the level and its expected cost per solved task; return the redo route.

        One try costs the route's estimate (the session from here, as ``estimate`` prices it, warm cache and all) and
        succeeds with the model's chance at the level. A failed try is still paid for, and the work is then done again
        on the redo route: the one the threshold would pick (the cheapest whose model meets the level; with none, the
        most likely to succeed), tried until it succeeds, so redoing costs its cost / its chance. Then

            expected = cost + (1 - chance) x redo cost / redo chance

        which for the redo route itself is its cost / its chance. A model without an estimate meets the level (``_in_reach``)
        and counts at the chance of a model exactly at it."""
        task, level = floor.task, floor.level
        just = self.cfg.threshold_chance[task]
        for c in cands:
            c.chance = c.model.chances.get(task, {}).get(level, just)
        at_level = [c for c in cands if c.model.q(task) >= level]
        redo = min(at_level, key=lambda c: (c.cost, -c.quality)) if at_level else min(cands, key=lambda c: (-(c.chance or 0), c.cost))
        again = redo.cost / (redo.chance or just)
        for c in cands:
            c.expected = c.cost + (1 - (c.chance or 0)) * again
        return redo

    def human_cost(self, team: TeamConfig) -> float:
        """What a failed attempt at the top of the scale (level 5, a full agent request) costs a person on this team
        (expected_cost), in USD. A request puts a share of it at stake (``stakes``)."""
        return team.human_cost_usd if team.human_cost_usd is not None else self.cfg.routing.human_cost_usd

    @staticmethod
    def stakes(floor: Floor, body: dict, prompt_tokens: int) -> Stakes:
        """What a failure of this request puts at stake, as a share of the person's cost (success.stakes, decision 67): its
        level, its size, and whether the person's newest prompt says the last answer was wrong."""
        return stakes(floor.level, prompt_tokens, correction(body) is not None)

    def carries_on(self, session: Session, body: dict, headers: dict[str, str], prompt_tokens: int) -> bool:
        """Whether a human message carries on the session's task (where the cascade may escalate it) rather than starting a
        new one: it says the last answer was wrong, or the code was left failing (success.continues) and the new prompt,
        classified first, is the same kind of work at no higher a level than the task's. A new, harder prompt after a
        failing run is a new task, routed at its own level."""
        state = session.outcome or {}
        if not continues(state):
            return False
        if state.get("correction"):
            return True
        task = parse_floor(session.floor or "")
        new = self.mapped(self.classify(body, headers, prompt_tokens))
        return task is not None and new.task == task.task and new.level <= task.level

    def cascades(self, team: TeamConfig) -> bool:
        """Whether this team's `auto` tasks may escalate inside a task: expected_cost, with escalations allowed."""
        return self.model_choice(team)[0] == "expected_cost" and self.cfg.routing.cascade.max_escalations > 0

    def difficulty(self, floor: Floor, learned: LearnedCall | None) -> Difficulty:
        """How hard the task is for the success predictor: the level it's routed at, or, with the learned router routing, its
        difficulty and spread, moved as the level was (Claude Code's caps, the level map, exploration)."""
        p = learned.prediction if learned is not None else None
        if p is None or self.cfg.routing.learned.mode != "route":
            return Difficulty(float(floor.level), 0.0, f"level {floor.level}")
        base = p.level if learned.used else learned.rules.level
        return Difficulty(p.difficulty + floor.level - base, p.spread, f"learned router v{p.version}")

    def _expected_total(self, cands: list[Candidate], floor: Floor, team: TeamConfig, learned: LearnedCall | None,
                        context: TaskContext | None, escalations: int, at_stake: Stakes) -> dict:
        """Set each candidate's chance and expected total cost to finish the task (success.plan), with the person's cost
        scaled by what the request puts at stake; returns what the plan needs for the reason: the difficulty, the person's
        cost at stake and each candidate's escalation target."""
        task = floor.task
        d = self.difficulty(floor, learned)
        mode = self.cfg.routing.success.mode
        just = self.cfg.threshold_chance.get(task)
        chances: dict[str, Chance] = {}
        for c in cands:
            if c.model.name not in chances:
                chances[c.model.name] = self.success.chance(c.model, task, d, context, mode, just)
            c.why = chances[c.model.name]
            c.chance = c.why.p
        full = self.human_cost(team)
        human = full * at_stake.share
        values = plan([Option(id(c), c.model.name, c.cost, c.cold if c.cold is not None else c.cost, c.chance or 0.0) for c in cands],
                      human, escalations)
        for c in cands:
            c.expected, c.tokens = values[id(c)].total, values[id(c)].tokens
        return {"difficulty": d, "human": human, "full": full, "stakes": at_stake, "values": values, "escalations": escalations}

    def _expected_note(self, d: Decision, cands: list[Candidate], plan_info: dict) -> str:
        """'expected total cost: claude-haiku ~$0.039 to finish: try $0.0060, chance 70% (...), difficulty 3.0 (level 3), a
        failure costs $0.11 of a person's time (of $1.00: 1/9 at level 3), then claude-sonnet; claude-sonnet ~$0.043,
        chance 79% (...)', and the same as a record (Decision.predicted)."""
        lead = cands[0]
        values = plan_info["values"]
        other = next((c for c in cands if c.model.name != lead.model.name), None)
        v = values[id(lead)]

        def rec(c: Candidate) -> dict:
            w = c.why
            return {"model": c.model.name, "p": round(c.chance or 0.0, 4), "prior": round(w.prior, 4) if w else None,
                    "fitted": round(w.fitted, 4) if w and w.fitted is not None else None, "expected": round(c.expected or 0.0, 6),
                    "tokens": round(values[id(c)].tokens, 6), "try": round(c.cost, 6), "why": w.why() if w else None}

        then = ("" if v.next is None else f", then {v.next.model} again" if v.next.model == lead.model.name else f", then {v.next.model}")
        at_stake: Stakes = plan_info["stakes"]
        scaled = f" (of {_usd(plan_info['full'])}: {at_stake.why()})" if at_stake.share < 1 and plan_info["full"] > 0 else ""
        note = (f"; expected total cost: {lead.model.name} ~{_usd(lead.expected or 0)} to finish: try {_usd(lead.cost)}, chance "
                f"{lead.why.why() if lead.why else '?'}, difficulty {plan_info['difficulty']}, a failure costs "
                f"{_usd(plan_info['human'])} of a person's time{scaled}{then}")
        if other is not None:
            note += f"; {other.model.name} ~{_usd(other.expected or 0)}, chance {other.why.why() if other.why else '?'}"
        d.predicted = {**rec(lead), "runner_up": rec(other) if other is not None else None, "human_cost": round(plan_info["human"], 6),
                       "stakes": round(at_stake.share, 6),
                       "difficulty": round(plan_info["difficulty"].value, 3), "spread": round(plan_info["difficulty"].spread, 3),
                       "escalations": plan_info["escalations"], "then": v.next.model if v.next is not None else None}
        return note

    def _escalate(self, requested: str, team: TeamConfig, body: dict, headers: dict[str, str], session: Session, prompt_tokens,
                  utilization: dict[str, float], now: float, queue_full, kv, client: str, stage: str) -> tuple[Decision | None, str]:
        """Inside a task (decision 62): whether to move it to a stronger model now. The task's model has the chance it had
        at the task's start, moved by what its tool results have shown (success.posterior); every other model its chance
        from the predictor. Continuing costs the rest of the task where it is, and a failure there escalates later anyway;
        escalating costs the rest of the task from a cold cache on the stronger model, with one escalation fewer left.
        Returns the escalated decision (None: continue) and a note for the reason either way."""
        task_state = session.task or {}
        floor = parse_floor(session.floor or "", session.floor_reason or "the task's level")
        if floor is None or not session.model:
            return None, ""
        used = int(task_state.get("escalations") or 0)
        left = self.cfg.routing.cascade.max_escalations - used
        if left <= 0:
            return None, f"; escalations used up ({used} of {self.cfg.routing.cascade.max_escalations})"
        try:
            alt = self._decide(requested, team.model_copy(update={"model_choice": "expected_cost"}), body, headers, session, prompt_tokens,
                               True, utilization, now, queue_full, kv, floor_override=floor, probe=True, client=client, escalations=left)
        except RoutingError:
            return None, ""
        if not alt.candidates or alt.last_resort or any(c.expected is None for c in alt.candidates):
            return None, ""
        current = [c for c in alt.candidates if c.model.name == session.model]
        p0 = float(task_state.get("p0") or (current[0].chance if current else 0.0) or 0.0)
        p_now, parts = posterior(p0, session.outcome)
        human = self.human_cost(team) * self.stakes(floor, body, Estimate.of(prompt_tokens).tokens).share
        stronger = [c for c in alt.candidates if c.model.name != session.model and (c.chance or 0.0) > p0]
        if not current or not stronger:
            return None, ""
        cur = min(current, key=lambda c: (not c.warm, c.cost))
        options = [Option(id(c), c.model.name, c.cost, c.cold if c.cold is not None else c.cost, c.chance or 0.0) for c in alt.candidates
                   if c.model.name != session.model]
        options.append(Option(id(cur), cur.model.name, cur.cost, cur.cold if cur.cold is not None else cur.cost, p_now))
        cont = plan(options, human, left)[id(cur)].total  # continue: a failure then escalates, with this many left
        esc = plan(options, human, left - 1)
        best = min(stronger, key=lambda c: esc[id(c)].total)
        e_esc = esc[id(best)].total
        evidence = ", ".join(f"{n} {v:+.1f}" for n, v in parts) or "no evidence yet"
        margin = self.cfg.routing.switch_margin_usd
        summary = (f"{session.model}'s chance now {p_now:.0%} (was {p0:.0%}: {evidence}); continuing ~{_usd(cont)} vs escalating to "
                   f"{best.model.name} ~{_usd(e_esc)} ({best.chance:.0%}, cache rebuild included)")
        if e_esc + margin >= cont:
            return None, f"; {summary}: continuing"
        alt.candidates = sorted((c for c in alt.candidates if c.model.name == best.model.name), key=lambda c: c.ranked_cost) + \
            [c for c in alt.candidates if c.model.name != best.model.name and c.model.name != session.model]
        why = "a correction" if (session.outcome or {}).get("correction") else (
            "tests failing" if (session.outcome or {}).get("last_test") == "fail" else
            "a fix loop" if ((session.outcome or {}).get("fix_loop") or 0) >= 3 else
            "failures in a row" if failures_in_row(session.outcome) else "the evidence")
        alt.escalation = {"n": used + 1, "from": session.model, "to": best.model.name, "stage": stage, "reason": why,
                          "p_before": round(p0, 4), "p_now": round(p_now, 4), "continue": round(cont, 6), "escalate": round(e_esc, 6)}
        at = "at the next human message" if stage == "human" else "after failures in a row"
        alt.reason = (f"escalated {at} ({used + 1} of {self.cfg.routing.cascade.max_escalations}): {summary}; "
                      f"at {floor} ({floor.reason})")
        if best.why is not None and best.why.parts:
            alt.reason += f"; {best.model.name}'s chance {best.why.why()}"
        return alt, ""

    def _per_success_note(self, cands: list[Candidate], redo: Candidate, floor: Floor) -> str:
        """'cost per success: gpt-terra, expected $0.12 per success (72% at coding:4) vs $0.20 (81%) on claude-fable'."""
        def chance(c: Candidate, at: str = "") -> str:
            return f"{c.chance:.0%}{at}" + ("" if floor.task in c.model.chances else ", no estimate")

        lead = cands[0]
        note = f"; cost per success: {lead.model.name}, expected {_usd(lead.ranked_cost)} per success ({chance(lead, f' at {floor}')})"
        other = redo if redo.model.name != lead.model.name else next((c for c in cands if c.model.name != lead.model.name), None)
        if other is not None:
            note += (f" vs {_usd(other.ranked_cost)} ({chance(other)}) on {other.model.name}"
                     + (", the cheapest at the level" if other is redo and other.model.q(floor.task) >= floor.level else ""))
        return note

    def decide(
        self,
        requested: str,
        team: TeamConfig,
        body: dict,
        headers: dict[str, str],
        session: Session | None,
        prompt_tokens: Estimate | int,
        boundary: bool,
        utilization: dict[str, float],
        now: float,
        queue_full: frozenset[str] | set[str] = frozenset(),
        kv: dict | None = None,
        client: str = "",
        batch: bool = False,
        explore: bool = False,
        explore_up: bool = True,
        context: TaskContext | None = None,
        cascade: str | None = None,
        level_cap: int | None = None,
    ) -> Decision:
        """Pick and rank candidates. ``prompt_tokens`` is the request's estimate (tokens.estimate; a plain number is
        taken as text): each model gets it in its own tokens. ``queue_full`` names saturated backends that also have
        no room left to queue requests; other saturated backends are kept as a last resort. ``kv``: the controller's
        KV-event notes for this session, by GPU deployment (kv_events.py). ``client``: the API the client called
        (openai, anthropic, responses), a feature for the learned router.

        ``batch``: one request of a batch (batches/). Only routes whose backend has a batch API qualify, priced at batch
        prices for the one request (no session to keep warm, no speed to weigh); levels (the learned router's too, in
        route mode), policies and the cost per success rules apply as for a live request.

        With the learned router on, the decision also says what it predicted (``Decision.learned``) and, where its level
        and the rules' differ, what the level that didn't act would have picked and cost (``_other_side``).

        ``explore``: a live request (the gateway's), so a team that explores may draw an exploration arm for a new task
        (``_explore``, ``Decision.explore``); ``explore_up`` False when a budget is nearly spent (then no task explores up or
        escalates).

        ``context``: the task's repository and project hashes and triage buckets, for the success predictor
        (expected_cost). ``cascade``: where a task of a team routing by expected cost may escalate (decision 62): "now"
        mid-task, if its failures in a row reach routing.cascade.failures_now; "human" at a human message that carries on
        the task (success.continues)."""
        d = self._decide(requested, team, body, headers, session, prompt_tokens, boundary, utilization, now, queue_full, kv,
                         client=client, batch=batch, explore=explore, explore_up=explore_up, context=context, cascade=cascade,
                         level_cap=level_cap)
        if d.learned is not None and d.learned.outcome != "named":
            self._other_side(d, team, body, headers, session, prompt_tokens, utilization, now, queue_full, kv, client, batch)
        return d

    def _other_side(self, d: Decision, team: TeamConfig, body: dict, headers: dict[str, str], session: Session | None,
                    prompt_tokens: Estimate | int, utilization: dict[str, float], now: float, queue_full, kv, client: str,
                    batch: bool = False) -> None:
        """Say what the learned router said, and price the level that didn't act: the learned one in shadow mode (or when
        unsure), the rules' when the learned one was used. A probe: nothing it does changes routing (no speed check is
        counted), and the decision's candidates are left as they are."""
        call = d.learned
        assert call is not None
        if call.note:
            d.reason += f"; {call.note}"
        learned = call.learned_floor()
        if learned is None or learned.level == call.rules.level or not d.candidates:
            return
        rules_cap, learned_cap = self.cap_for_class(call.rules, headers), self.cap_for_class(learned, headers)
        if rules_cap.level == learned_cap.level:
            d.reason += f"; capped at {rules_cap.level} either way for a Claude Code request of this class"
            return
        other = rules_cap if call.used else learned_cap
        try:
            alt = self._decide(d.requested, team, body, headers, session, prompt_tokens, True, utilization, now, queue_full, kv,
                               floor_override=other, probe=True, client=client, batch=batch)
        except RoutingError:
            alt = None
        chosen = d.candidates[0]
        call.cost = round(chosen.cost, 6)
        if alt is None or not alt.candidates:
            d.reason += f"; nothing could run at {other}"
            return
        a = alt.candidates[0]
        call.alt_model, call.alt_cost = a.model.name, round(a.cost, 6)
        model, p = self.learned.model, call.prediction
        if model is not None and p is not None:
            task = call.rules.task
            rules_pick, learned_pick = (a, chosen) if call.used else (chosen, a)
            call.fail = (model.fail_chance(task, p.difficulty, rules_pick.model.q(task), p.noticed),
                         model.fail_chance(task, p.difficulty, learned_pick.model.q(task), p.noticed))
        side = f"the rules' {rules_cap}" if call.used else f"at {learned_cap} it"
        d.reason += (f"; {side} would have picked {a.model.name} (~{_usd(a.cost)} for the task) instead of {chosen.model.name} "
                     f"(~{_usd(chosen.cost)})")

    def _decide(
        self,
        requested: str,
        team: TeamConfig,
        body: dict,
        headers: dict[str, str],
        session: Session | None,
        prompt_tokens: Estimate | int,
        boundary: bool,
        utilization: dict[str, float],
        now: float,
        queue_full: frozenset[str] | set[str] = frozenset(),
        kv: dict | None = None,
        floor_override: Floor | None = None,
        probe: bool = False,
        client: str = "",
        batch: bool = False,
        explore: bool = False,
        explore_up: bool = True,
        context: TaskContext | None = None,
        cascade: str | None = None,
        escalations: int | None = None,
        level_cap: int | None = None,
    ) -> Decision:
        """``decide`` without the learned router's other side. ``floor_override``: route a new task at this level
        instead of classifying it; ``probe``: count no speed check and log nothing (pricing the other side);
        ``escalations``: how many escalations the task has left, for expected_cost's plan (default: the cascade's maximum)."""
        r = self.cfg.routing
        tier = self.cfg.tier(requested)
        tier = tier if tier is not None and tier.level is not None else None  # a fixed tier is a named model (cfg.model)
        auto = requested == r.auto_model or requested == "" or tier is not None
        floor: Floor | None = None
        priority = self.priority(team, headers)
        est = Estimate.of(prompt_tokens)
        counts: dict[str, int] = {}

        def n(m: ModelConfig) -> int:
            if m.name not in counts:
                counts[m.name] = self.tokens.count(m.name, est)
            return counts[m.name]
        per_success = False  # this decision ranks models by expected cost per solved task (or expected total cost)
        choice, minimum = self.model_choice(team)
        learned: LearnedCall | None = None
        explored: dict | None = None  # the exploration arm this task start drew (``_explore``)
        canary: str | None = None
        again_kw = {"floor_override": floor_override, "probe": probe, "client": client, "batch": batch, "context": context,
                    "level_cap": level_cap}
        escalate_note = ""

        if auto:
            kept = (self.cfg.model(session.model) if session and session.model and not boundary and floor_override is None
                    and team.allows_model(session.model) else None)
            outgrown = kept is not None and not fits(kept, n(kept), reply_room(kept, body))
            # The level the session's task was given, and why (the task keeps it until the next human prompt).
            kept_floor = parse_floor(session.floor or "", session.floor_reason or "the task's level") if session else None
            if (kept is not None and not outgrown and cascade and session is not None and explore_up and not probe and not batch
                    and choice == "expected_cost" and (cascade == "human" or failures_in_row(session.outcome)
                                                       >= r.cascade.failures_now)):
                escalated, escalate_note = self._escalate(requested, team, body, headers, session, prompt_tokens, utilization, now,
                                                          queue_full, kv, client, cascade)
                if escalated is not None:
                    if not probe:
                        log.info("%s", escalated.reason)
                    return escalated
            if kept is not None and not outgrown:
                models = [kept]
                floor = kept_floor
                stage = "continuing the task: " if cascade == "human" else "mid-task: "
                reason = f"{stage}keeping session model {kept.name}" + (f" at {floor} ({floor.reason})" if floor else "") + escalate_note
            else:
                # A new task, or the conversation no longer fits the session's model: then the task keeps its level
                # and moves to a model with room (paying once to rebuild the cache) instead of failing.
                if floor_override is not None:
                    floor = floor_override
                elif outgrown and kept_floor:
                    floor = kept_floor
                else:
                    floor, learned = self.level(body, headers, est.tokens, client)
                    if tier is not None and not parse_floor(headers.get("x-route-floor", "")):
                        # A named tier (decision 72): its level, the task from the request unless the tier sets one.
                        floor = Floor(tier.task or floor.task, tier.level, f"tier '{requested}'", explicit=True)
                    if level_cap is not None and floor.level > level_cap:
                        # A team's budget step-down (decision 74): a budget that applies is nearly spent, so new tasks go
                        # to cheaper models. Above the client's own X-Route-Floor too: it's the organization's limit.
                        floor = Floor(floor.task, level_cap, f"{floor.reason}; capped at {level_cap} by budget_step_down", explicit=True)
                    if explore and not probe and not batch:
                        given = floor
                        floor, explored, canary = self._explore(floor, team, headers, session, now, explore_up)
                        if not any(m.kind == "chat" and m.q(floor.task) >= floor.level and team.allows_model(m.name)
                                   for m in self.cfg.models):
                            floor, explored, canary = given, None, None  # nothing the team may use at that level
                models = [m for m in self.cfg.models
                          if m.kind == "chat" and m.q(floor.task) >= floor.level and team.allows_model(m.name)]
                reason = f"classified {floor} ({floor.reason})"
                if outgrown and kept is not None:
                    reason = (f"session outgrew {kept.name}'s context window ({self.window_note(kept, n(kept), reply_room(kept, body))}); "
                              f"re-routed at {floor} ({floor.reason})")
                    if not probe:
                        log.info("%s", reason)
                if choice in ("cost_per_success", "expected_cost"):
                    # A level the client set (X-Route-Floor, also how a named model is substituted) is a minimum it asked
                    # for, so only models meeting it qualify, as with the threshold.
                    why: str | None = "the client set the level (X-Route-Floor), so the threshold decides"
                    if not (floor.explicit or parse_floor(headers.get("x-route-floor", ""))):
                        models, why = self._in_reach(models, floor, team, minimum)
                    per_success = why is None
                    if why:
                        reason += f"; {'expected total cost' if choice == 'expected_cost' else 'cost per success'}: {why}"
                if canary is not None:  # the canary arm: only the canary
                    models = [m for m in models if m.name == canary] or models
                elif explored and explored.get("avoid"):  # a canary takes only its own arm until it's been tried
                    models = [m for m in models if m.name != explored["avoid"]] or models
                if not models:
                    # Nothing meets the floor: fall back to the best allowed models.
                    allowed = [m for m in self.cfg.models if m.kind == "chat" and team.allows_model(m.name)]
                    best = max((m.q(floor.task) for m in allowed), default=0)
                    models = [m for m in allowed if m.q(floor.task) == best]
                    reason += f"; no model meets the floor, using best available (level {best})"
        else:
            model = self.resolve(requested)
            if model is None:
                raise RoutingError(404, f"model '{requested}' is not configured", "not_found_error")
            if model.kind != "chat":
                raise RoutingError(400, f"'{model.name}' is an embedding model; use /v1/embeddings", "invalid_request_error")
            if not team.allows_model(model.name):
                raise RoutingError(403, f"model '{model.name}' is not allowed for team '{team.name}'", "permission_error")
            models = [model]
            reason = f"harness requested {model.name}"
            if boundary and not probe and not batch:  # a batch line gets no route_features row (no outcome follows it)
                learned = self.record_features(body, headers, est.tokens, client)

        turns = self._expected_turns(session)
        pause = 0.0 if batch else self.pauses.share(team.name)
        task = floor.task if floor else "general"
        cands: list[Candidate] = []
        # A long context never raises the level; it rules out the models whose window can't hold it, counted in each
        # model's own tokens (the estimate, calibrated by what its replies report).
        too_small = [m for m in models if not fits(m, n(m), reply_room(m, body))]
        if auto and too_small and all(self._mock_only(m) for m in models if m not in too_small) \
                and not all(self._mock_only(m) for m in too_small):
            # Only mock models "fit" (their windows aren't known), and mocks never win against real models: so no
            # real model has room, the case below, rather than a canned answer.
            models = [m for m in models if not self._mock_only(m)]
            too_small = list(models)
        if too_small and len(too_small) < len(models):
            models = [m for m in models if m not in too_small]
            reason += f"; ruled out {self._needs_each(too_small, n, body, 'window too small for')}"
        elif too_small and not auto:
            named = models[0]  # a named model is the only one
            reason += (f"; {_windows(models)} may be too small for {_needs(n(named), body)}: sent anyway, the provider decides "
                       f"({self.tokens.describe(named.name)})")
        elif too_small:
            # Nothing at the level has room (a higher level has no more models than this one). The count is an
            # estimate, so don't refuse: send it to the largest window and let the provider decide. Its "prompt is too
            # long" reaches the client worded so that Claude Code compacts (proxy.py).
            largest = max(m.context_window or 0 for m in models)
            models = [m for m in models if m.context_window == largest]
            reason += (f"; no model at {floor} has room for {_needs(n(models[0]), body)}: sent to the largest window, "
                       f"{_windows(models)}, and the provider decides ({self.tokens.describe(models[0].name)})")
        # A count near the limit is where the estimate matters: say what it was and how it was made.
        close = [m for m in models if m.context_window and fits(m, n(m), reply_room(m, body))
                 and n(m) + reply_room(m, body) >= m.context_window * CLOSE_SHARE]
        if close:
            note = ", ".join(f"{m.name} ({self.window_note(m, n(m), reply_room(m, body))})" for m in close)
            reason += f"; context window check close: {note}"
            if not probe:
                log.info("%s request near the context window: %s", requested or r.auto_model, note)
        growth = max(est.tokens - session.prefix_tokens, 0) if session and session.prefix_tokens else None
        saturated: list[Candidate] = []
        cooling: list[Candidate] = []  # the model is over capacity on this backend: a last resort
        policy = datapolicy.from_headers(headers)  # the caller's data policy (decision 72)
        policy_why: dict[str, str] = {}
        for m in models:
            for route in m.routes:
                backend = self.backends.get(route.backend)
                if backend is None or not backend.available or not backend.healthy(now) or not team.allows_backend(route.backend):
                    continue
                if policy is not None and (no := policy.refuses(backend.cfg)):
                    policy_why[route.backend] = no
                    continue
                if batch:
                    if backend.batch_api is None or (not route.priced and (auto or needs_price(route, backend.kind))):
                        continue
                    out = asked_reply(body) or r.est_output_tokens
                    if backend.batch_api == "gpu":
                        # A running deployment, at its shadow price for its load now: near free in its headroom, where the
                        # worker runs batch work at low priority (batches/gpu.py). Never one that's full or draining.
                        mult = shadow_multiplier(utilization.get(backend.name, 0.0), r.shadow_knee, r.shadow_floor)
                        if mult is None or backend.draining or not self.cfg.batches.gpu.enabled:
                            continue
                        price = route.cost_price().at(n(m))
                        cost = (n(m) * price.input + out * price.output) / 1e6 * mult
                    else:
                        price = route.batch_price(backend.cfg).at(n(m))
                        cost = (n(m) * price.input + out * price.output) / 1e6
                    cands.append(Candidate(model=m, route=route, backend=backend, cost=cost, warm=False, quality=m.q(task), prompt_tokens=n(m),
                                           upgraded=bool(auto and floor and m.q(task) > floor.level)))
                    continue
                busy = backend.skipped(now, route.model)
                if busy is not None and not busy.capacity:
                    continue  # e.g. the deployment doesn't exist there
                if backend.draining and backend.kind != "self_hosted" and not (session and session.warm(backend.name, now)):
                    continue  # draining: only finish sessions whose cache is already there (GPUs: in place())
                if not route.priced and (auto or needs_price(route, backend.kind)):
                    continue  # an unknown price must never look free (a named GPU route is charged by its time)
                price = route.cost_price().at(n(m))  # long prompts can cost more per token
                new = None if growth is None else max(round(growth * self.tokens.ratio(m.name)), 200)
                quality = m.q(task)
                if backend.kind == "self_hosted":
                    # Shadow price at its load (per replica, when they can be addressed), and the share of the prefix
                    # still cached. Saturated: new work spills to other routes. If none can take it, the request
                    # queues here (in vLLM) rather than failing, unless that queue is full too.
                    c = self.place(backend, price, n(m), session, turns, utilization, now, (kv or {}).get(backend.name), route.model, new)
                    if c is None or (c.queued and backend.name in queue_full):
                        continue
                    c.model, c.route, c.quality, c.prompt_tokens = m, route, quality, n(m)
                    c.upgraded = bool(auto and floor and quality > floor.level)
                    (cooling if busy else saturated if c.queued else cands).append(c)
                    continue
                warm = bool(session and session.warm(backend.name, now))
                cost = self.estimate(price, backend, n(m), session, warm, turns, route.model, new, pause)
                (cooling if busy else cands).append(
                    Candidate(
                        model=m,
                        route=route,
                        backend=backend,
                        cost=cost,
                        warm=warm,
                        quality=quality,
                        prompt_tokens=n(m),
                        # From a cold cache, for expected_cost's escalations (a GPU's rebuild is at its shadow price: its cost).
                        cold=(self.estimate(price, backend, n(m), session, False, turns, route.model, new, pause) if warm else cost)
                        if choice == "expected_cost" else None,
                    )
                )
        if per_success and not cands:
            # Nothing in reach can take it right now (and the minimum may have ruled out a model the threshold allows):
            # the threshold decides, with its own last resorts.
            again = self._decide(requested, team.model_copy(update={"model_choice": "threshold"}), body, headers, session,
                                 prompt_tokens, boundary, utilization, now, queue_full, kv, **again_kw)
            again.reason += (f"; {'expected total cost' if choice == 'expected_cost' else 'cost per success'}: nothing in reach can run "
                             "now, so the threshold decides")
            return again
        if not cands and saturated and auto and session and session.model and not boundary:
            # The session's GPUs are full: another model with room beats waiting in a queue.
            again = self._decide(requested, team, body, headers, session, prompt_tokens, True, utilization, now, queue_full, kv, **again_kw)
            if not any(c.queued for c in again.candidates):
                again.reason = f"session model {session.model} is saturated; re-routed: {again.reason}"
                return again
        if not cands and not saturated and not cooling and policy_why:
            # Nothing left once the caller's data policy is applied: say so, rather than that nothing is available.
            raise RoutingError(403, datapolicy.refusal(policy, policy_why), "permission_error")
        if not cands and saturated:
            saturated.sort(key=lambda c: (c.cost, -c.quality))
            names = ", ".join(dict.fromkeys(c.backend.name for c in saturated))
            where = f" ({saturated[0].placement})" if saturated[0].placement else ""
            return Decision(requested=requested, auto=auto, floor=floor, candidates=saturated, last_resort=True,
                            reason=f"{reason}; every route is saturated, queued on {names}{where}", learned=learned)
        if not cands and cooling and auto and session and session.model and not boundary:
            # The session's model is over capacity everywhere: another model that can answer now beats retrying it.
            again = self._decide(requested, team, body, headers, session, prompt_tokens, True, utilization, now, queue_full, kv, **again_kw)
            if not again.last_resort:
                again.reason = f"session model {session.model} is over capacity everywhere; re-routed: {again.reason}"
                return again
        if not cands and cooling:
            # Every route is over capacity right now. Try the one that should free up first rather than refuse:
            # the alternative is an error the client would retry anyway.
            cooling.sort(key=lambda c: c.backend.skipped(now, c.route.model).until)
            names = ", ".join(dict.fromkeys(c.backend.name for c in cooling))
            return Decision(requested=requested, auto=auto, floor=floor, candidates=cooling, last_resort=True,
                            reason=f"{reason}; every route is over capacity, trying {names} anyway", learned=learned)
        if not cands:
            if auto and session and session.model and not boundary:
                # The session's model has nowhere to run (e.g. its GPUs were stopped): pick again.
                again = self._decide(requested, team, body, headers, session, prompt_tokens, True, utilization, now, queue_full, kv,
                                     **again_kw)
                again.reason = f"session model {session.model} is unavailable; re-routed: {again.reason}"
                return again
            if not auto and team.substitute_unavailable and len(models) == 1:
                sub = self._substitute(models[0], team, body, headers, session, prompt_tokens, utilization, now, queue_full, batch)
                if sub is not None:
                    return sub
            if batch:
                where = "" if auto or len(models) != 1 else f" for '{models[0].name}'"
                raise RoutingError(400, f"no route{where} has a batch API (or one this team may use): "
                                        f"{self._batch_reasons(models, team)}", "invalid_request_error")
            message, wait = self._unavailable_message(models, team, now, auto)
            raise RoutingError(503, message, "overloaded_error", {"retry-after": str(wait)} if wait else None)
        if auto and any(c.backend.kind != "mock" for c in cands):
            # Mocks are for trying things out; never let them win against real backends.
            cands = [c for c in cands if c.backend.kind != "mock"]

        redo, plan_info = None, None
        if per_success and floor and choice == "expected_cost":
            left = 0 if batch else (escalations if escalations is not None else r.cascade.max_escalations)
            plan_info = self._expected_total(cands, floor, team, learned, context, left, self.stakes(floor, body, est.tokens))
        elif per_success and floor:
            redo = self._per_success(cands, floor)
        cands.sort(key=lambda c: (c.ranked_cost, -c.quality))
        if len(cands) > 1 and not batch:
            cands, note = self.rank(cands, priority, not probe and (session is None or not session.backend), now)
            reason += note

        # Stickiness: only move a session off its current backend if it's clearly cheaper.
        if session and session.backend:
            current = next((c for c in cands if c.backend.name == session.backend and c.model.name == session.model), None)
            if current is not None and current is not cands[0]:
                margin = current.spend * r.switch_margin_pct / 100.0 + r.switch_margin_usd
                if cands[0].ranked_cost + margin >= current.ranked_cost:
                    cands.remove(current)
                    cands.insert(0, current)
                    reason += f"; staying on {current.backend.name} (move would not pay for cache rebuild)"

        if redo is not None and floor:
            reason += self._per_success_note(cands, redo, floor)
        if cands[0].placement:
            reason += f"; {cands[0].backend.name}: {cands[0].placement}"
        if explored is not None:
            explored.pop("avoid", None)
        d = Decision(requested=requested, auto=auto, floor=floor, candidates=cands, reason=reason, priority=priority,
                     learned=learned, explore=explored, context=context)
        if plan_info is not None:
            d.reason += self._expected_note(d, cands, plan_info)
        return d

    def _batch_reasons(self, models: list[ModelConfig], team: TeamConfig) -> str:
        """Why none of these models' routes can take a batch request, backend by backend."""
        why = {}
        for m in models:
            for route in m.routes:
                b = self.backends.get(route.backend)
                gpu = b is not None and b.batch_api == "gpu"
                why[route.backend] = ("not enabled" if b is None else f"not allowed for team '{team.name}'" if not team.allows_backend(route.backend)
                                      else b.batch_why if b.batch_api is None else UNPRICED if needs_price(route, b.kind)
                                      else "batch work on GPUs is off (batches.gpu.enabled)" if gpu and not self.cfg.batches.gpu.enabled
                                      else "its GPUs aren't running (batch work only uses GPUs live demand keeps running)"
                                      if gpu and not b.available else "draining" if gpu and b.draining
                                      else "full" if gpu else "unavailable right now")
        return "; ".join(f"{k}: {v}" for k, v in list(why.items())[:8]) or "no routes"

    def _substitute(self, named: ModelConfig, team: TeamConfig, body: dict, headers: dict[str, str], session: Session | None,
                    prompt_tokens: Estimate | int, utilization: dict[str, float], now: float,
                    queue_full: frozenset[str] | set[str] = frozenset(), batch: bool = False) -> Decision | None:
        """Route a named model that can't run to models at least as good for this task (never a weaker one): at its score
        rounded up to a whole level (scores have decimals, levels don't), as a floor the client set, so the threshold
        decides and nothing caps it."""
        task = classify(body, headers, Estimate.of(prompt_tokens).tokens, self.cfg.routing.levels).task
        level = min(5, max(1, math.ceil(named.q(task))))
        floor = Floor(task, level, f"{named.name}'s score for {task}, {named.q(task):g}", explicit=True)
        try:
            sub = self._decide(self.cfg.routing.auto_model, team, body, headers, session, prompt_tokens, True, utilization, now,
                               queue_full, floor_override=floor, batch=batch)
        except RoutingError:
            return None
        sub.candidates = [c for c in sub.candidates if c.model.name != named.name and c.model.q(task) >= level]
        if not sub.candidates:
            return None
        for c in sub.candidates:
            c.upgraded = False
        sub.auto = False  # the client named a model: no automatic effort changes or refusal retries
        sub.requested = named.name
        sub.substituted_for = named.name
        sub.reason = f"{named.name} has nowhere to run; substituted a model at least as good for {task} (level {level}): {sub.reason}"
        return sub

    def _unavailable_message(self, models: list[ModelConfig], team: TeamConfig, now: float, auto: bool) -> tuple[str, int | None]:
        """Say why nothing can serve the request, route by route, so people know what to fix or switch to. Also
        returns the seconds until a skipped route is tried again (for Retry-After), if any is."""
        waits = [o.until - now for m in models for r in m.routes
                 if (b := self.backends.get(r.backend)) is not None and (o := b.skipped(now, r.model)) is not None]
        wait = max(1, int(min(waits))) if waits else None
        if auto or len(models) != 1:
            return "no healthy backend is available for this request", wait
        why = []
        for route in models[0].routes:
            b = self.backends.get(route.backend)
            bc = self.cfg.backend(route.backend)
            out = b.skipped(now, route.model) if b is not None else None
            if b is None:
                why.append(f"{route.backend}: {(bc.disabled_reason if bc else None) or 'not enabled'}")
            elif not team.allows_backend(route.backend):
                why.append(f"{route.backend}: not allowed for team '{team.name}'")
            elif not b.available:
                why.append(f"{route.backend}: {getattr(b, 'unavailable_reason', None) or 'not running'}")
            elif needs_price(route, b.kind):
                why.append(f"{route.backend}: {UNPRICED}")
            elif out is not None:
                what = describe(out.status) if out.status else "failing"
                why.append(f"{route.backend}: {what}, trying again in {max(1, int(out.until - now))}s")
            elif b.draining:
                why.append(f"{route.backend}: draining, only finishing existing sessions")
            else:
                why.append(f"{route.backend}: saturated and its queue is full")
        return (f"'{models[0].name}' has nowhere to run right now ({'; '.join(why)}). "
                f"Use model '{self.cfg.routing.auto_model}' to let Merit Order pick another model, or try again later."), wait
