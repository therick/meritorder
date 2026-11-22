export type Row = {
  key: string;
  requests: number;
  errors: number;
  upgraded: number;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  actual_cost: number;
  served_api_cost: number;
  baseline_cost: number;
  list_cost: number;
  savings_routing: number;
  savings_caching: number;
  savings_self_hosting: number;
  savings_batch: number;
  savings_compression: number;
  savings_compression_model: number;
  savings_response_cache: number;
  savings_total: number;
  /** The row's cost split (decision 64): APIs, and GPUs. A GPU request carries its share of the GPU's time; for the
   * whole organization the GPU time no request paid for (idle, starting, draining) is added as gpu_idle_cost, which is
   * in gpu_cost and actual_cost, and comes off savings_self_hosting. */
  api_cost: number;
  gpu_cost: number;
  gpu_idle_cost: number;
};

/** The summary row with GPU time no request paid for, when rows are teams, people, models or tags (spend.IDLE_KEY). */
export const GPU_IDLE_KEY = "GPU idle, unallocated";

/** Which store answered a report: "postgres", "clickhouse", both ("clickhouse+postgres"), or "memory" (no database). */
export type AnsweredBy = string | null;

export type Summary = { start: string; end: string; group_by: string; rows: Row[]; totals: Row; answered_by?: AnsweredBy };

/** routing.levels: how `auto` gives a request its level (LevelRules in src/meritorder/config.py). */
export type LevelRules = {
  general: number; short: number; short_tokens: number; analysis: number; analysis_words: string[];
  coding: number; coding_words: string[]; simple: number; simple_words: string[];
  hard_raise: number; hard_signals: number; hard_words: string[]; hardest: number; hardest_words: string[];
  /** A new prompt saying the last answer was wrong: this many levels up. */
  correction_raise: number;
  /** true: at least agent_tools tools puts coding at `agent`, whatever the wording (the rule before decision 61). */
  agent_by_tools: boolean; agent: number; agent_tools: number;
};
export type LevelModel = { name: string; score: number; price: number; chance?: number };
/** One exploration arm's success rate (benchmark.py): weighted by 1 / the arm's chance, with its effective sample size. */
export type ArmRate = { n: number; n_eff: number; rate: number | null; se: number | null };
export type ArmVerdict = { ratio: number | null; enough: boolean; clear: boolean; low?: number; high?: number };
export type LevelRecommendation = { task: string; level: number; to: number; direction: "lower" | "raise"; ratio: number; n: number[];
  text: string; applied: boolean;
  /** Set aside by an admin until its evidence changes (benchmark.dismissal): when, by whom, and the task starts explored since. */
  dismissed?: { at: string; by: string | null; new_outcomes: number } | null };
export type LevelOutcomeRow = {
  task: string; level: number; requests: number; traffic_share: number; spend: number; spend_share: number | null;
  arms: Record<"served" | "down" | "up" | "canary", ArmRate>; too_high: ArmVerdict; too_low: ArmVerdict;
  learned: { n: number; lower: number; higher: number }; coding: Record<string, number>; min_outcomes: number;
  recommendation: LevelRecommendation | null;
};
export type BenchmarkEvent = { at: string; kind: string; text: string; key?: string; model?: string };
export type LevelOutcomes = {
  days: number; since: string; team: string | null; levels: LevelOutcomeRow[]; recommendations: LevelRecommendation[];
  trend: { day: string; requests: number; cost: number; by_floor: Record<string, number> }[]; events: BenchmarkEvent[];
  explored_requests: number; requests: number; spend: number; level_map: Record<string, number>;
  settings: { min_outcomes: number; as_well: number; auto_apply: boolean; canary_outcomes: number; canary_days: number };
  exploring: string[]; recording: boolean; canaries: { task: string; level: number; model: string; since: string; reason: string; outcomes?: number }[];
  answered_by?: AnsweredBy;
};
export type LevelsView = {
  auto_model: string;
  rules: LevelRules;
  defaults: LevelRules;
  /** The word lists as people read them: the pattern for "refactor" is shown as "refactor". */
  labels: { coding_words: string[]; simple_words: string[]; hard_words: string[]; hardest_words: string[]; analysis_words: string[] };
  request_class_levels: Record<string, number>;
  /** How the viewer's team chooses among models: threshold, cost_per_success or expected_cost, with this lowest chance. */
  model_choice?: string;
  min_success_chance?: number;
  /** expected_cost: what a failed attempt costs a person on the viewer's team, and the escalations a task may make. */
  human_cost_usd?: number;
  max_escalations?: number;
  /** Per level: what it means, and the models good enough for it that the viewer's team may use, cheapest first.
   * `in_reach`: with cost per success, the models below the level whose chance there is at least the minimum. */
  levels: { level: number; means: string; models: { coding: LevelModel[]; general: LevelModel[] };
    in_reach?: { coding: LevelModel[]; general: LevelModel[] } }[];
};

/** The learned router (src/meritorder/learned_jobs.py status): its settings, the model gateways use, its versions. */
export type RouterHoldout = {
  requests: number; with_signal: number; loss: number; rules_loss: number; improvement: number; error: number; better: boolean;
  would_raise: number; would_lower: number;
};
/** How judged labels agree with the outcome signals on the requests that have both (learned_jobs.judged_agreement). */
export type JudgedAgreement = {
  labels: number; not_good_enough: number; with_signal: number; caught: number | null; false_alarm: number | null;
  agreement: number | null; weight: number | null;
};
export type RouterMetrics = {
  requests: number; observations: number; with_signal: number; signals: Record<string, number>; refusals: number;
  members?: number; holdout?: RouterHoldout; better?: boolean; judged?: JudgedAgreement;
};
/** Judged labels in the router report: the samples, verdicts, agreement, cost (from the ledger) and each team's cap. */
export type RouterJudged = JudgedAgreement & {
  samples: number; outcomes: Record<string, number>; why: Record<string, number>; recorded_cost: number;
  levels: { task: string; level: number; labels: number; not_good_enough: number }[];
  preferred: number; preferred_first: number | null; preferred_stronger: number | null;
  cost: { replay: number; judge: number; total: number };
  teams: { team: string; enabled: boolean; sample_rate: number; cap: number | null; spent_month: number; capped: boolean;
    stronger: string; judges: string[] }[];
};
/** POST /v1/admin/judged-labels/estimate: what a team's judged labels would cost a month, and what to say about it. */
export type JudgedEstimate = {
  team: string; learned_mode: string; days: number; requests: number; eligible: number; priced: number; eligible_month: number;
  samples_month: number; sample_rate: number; usd_month: number; replay_usd_month: number; judging_usd_month: number;
  per_sample_usd: number | null; stronger: Record<string, string[]>; judges: string[]; unpriced: string[];
  sent_to: { stronger: string[]; judges: string[] }; cap: number | null; budget_left_usd: number | null;
  confirm_above_usd: number; warning: string[];
};
export type RouterTraining = {
  version: number | null; active: boolean; why?: string | null; by: string; at: string; days: number; metrics: RouterMetrics;
};
export type RouterStatus = {
  mode: "off" | "shadow" | "route"; min_confidence: number; max_age_days: number | null; retrain_hours: number | null;
  train_days: number; train_requests: number; keep_days: number; feature_version: number;
  /** The version gateways should use, and the one this gateway has loaded (null: none, the rules decide). */
  active: number | null; loaded: number | null; load_error: string | null; trained_at: string | null; age_days: number | null;
  /** Why the loaded model isn't used right now (too old, other rules, another feature key), or null. */
  stale: string | null; examples: number | null; metrics: RouterMetrics | null;
  last_training?: RouterTraining | null; train_requested?: { by: string; at: string } | null;
  /** The success predictor's fitted layer (decision 62): its mode, the stored version and how it did against the prior. */
  success?: { mode: "off" | "shadow" | "route"; version: number | null; better: boolean | null; trained_at: string | null;
    metrics: SuccessMetrics | null };
  versions?: { version: number; created_at: string; created_by: string; active: boolean; metrics: RouterMetrics }[];
};
export type RouterGroup = {
  task: string; rules: number; learned: number; requests: number; used: number; priced: number; with_signal: number;
  rules_cost: number; learned_cost: number; signals: Record<string, number>; confidence: number | null;
  expected_rules: number | null; expected_learned: number | null;
};
export type RouterRow = {
  ts: string; team: string | null; user: string | null; task: string; version: number; rules: number; learned: number;
  confidence: number; used: boolean; outcome: string; rules_model: string | null; learned_model: string | null;
  rules_cost: number | null; learned_cost: number | null; acted: "rules" | "learned"; signals: string[]; fail: (number | null)[];
};
export type SuccessMetrics = {
  examples: number; mean_success: number | null; better?: boolean;
  holdout?: { examples: number; brier: number; prior_brier: number; difference: number; se: number };
};
/** Predicted against seen success, n task starts: their mean chance, the share without a failure signal, the Brier score. */
export type SuccessScore = { n: number; brier: number | null; predicted: number | null; seen: number | null };
export type SuccessBin = { from: number; to: number; n: number; predicted: number; seen: number };
/** Expected total cost (success.report): predicted against seen success, escalations, expected against actual task cost. */
export type SuccessReport = {
  predicted: SuccessScore & { bins: SuccessBin[] };
  fitted: SuccessScore & { bins: SuccessBin[] };
  by_model: (SuccessScore & { model: string })[];
  escalations: { tasks: number; escalated: number; rate: number | null; helped: number; helped_rate: number | null; extra_cost: number;
    by_reason: Record<string, number> };
  cost: { tasks: number; expected: number | null; actual: number | null };
};
/** /v1/reports/router: where the learned router and the rules disagreed, what each side's pick would cost, and the outcomes. */
export type RouterReport = {
  days: number; since: string; status: RouterStatus; requests: number; outcomes: Record<string, number>;
  compared: number; agreed: number; disagreed: number;
  cost: { rules: number; learned: number; difference: number; priced: number };
  signal_rates: Record<string, { requests: number; with_signal: number; rate: number | null }>;
  groups: RouterGroup[]; rows: RouterRow[]; judged: RouterJudged; answered_by?: AnsweredBy;
  success?: SuccessReport;
};

export type Me = {
  user: string;
  /** From the person's record (sign-in or invite), when known: what the console shows instead of the id. */
  name?: string | null;
  email?: string | null;
  team: string;
  role: "user" | "team_lead" | "finance" | "admin";
  month: string;
  spend_usd: number;
  budget_usd: number | null;
  team_spend_usd: number | null;
  team_budget_usd: number | null;
  /** spend this month on APIs, and as shares of GPU time */
  api_usd: number;
  gpu_usd: number;
  baseline_usd: number;
  savings_usd: number;
  requests: number;
  by_model: Row[];
  /** How this person signed in (decision 69). Absent from older gateways and the demo's recorded answers. */
  sign_in?: SignInStatus;
};

/** "password" or "sso": a console session; "key": a session opened with an API key, or a key in the header. */
export type SignInStatus = { method: "password" | "sso" | "key" | null; has_password: boolean; two_factor?: boolean };
export type AuthConfig = { oidc: { issuer: string; client_id: string } | null; local?: { enabled: boolean };
  /** The console's SSO button: who people sign in with, and where it starts. Null when browser sign-in isn't set up. */
  sso?: { provider: "google" | "entra" | "oidc"; start: string } | null };
export type InviteInfo = { kind: "invite" | "reset"; email: string; team: string | null; role: string | null; expires_at: string;
  min_password_length: number };
/** An open invite or password-reset link. `token` and `path` only in the answer that created it: shown once. */
export type Invite = { id: string; user: string; kind: "invite" | "reset"; team: string | null; role: string | null;
  created_by: string | null; created_at: string | null; expires_at: string; token?: string; path?: string; note?: string };

export type LedgerRow = Row & {
  id: string;
  ts: string;
  user_id: string;
  team: string;
  requested_model: string;
  served_model: string;
  backend: string;
  floor: string | null;
  /** Why the request got its level and its model: the rules that applied, and models ruled out by their windows. */
  route_reason: string | null;
  status: number;
  error: string | null;
  latency_ms: number | null;
  ttft_ms: number | null;
  stream: boolean;
  /** "batch" for a request that went through a provider's batch API, in the batch `batch_id`. */
  service_tier: string | null;
  batch_id: string | null;
  key_id?: string | null;
  session_id?: string | null;
  tags?: Record<string, string>;
  client_format?: string | null;
  upstream_model?: string | null;
  backend_kind?: string | null;
  proxy_cache?: boolean;
  /** The routes tried first that failed ("backend/model status"); empty on rows from before it was recorded. */
  failed_over?: string | null;
};

/** The Requests view's filters and paging (GET /v1/reports/requests): every key is the API's own query parameter, and
 * all are optional. `tag` repeats ("project=apollo", or "explore" for any value). */
export type RequestQuery = {
  start?: string; end?: string; user?: string; team?: string; key?: string; session?: string;
  requested_model?: string; served_model?: string; backend?: string; format?: string; status?: string;
  task?: string; level?: string; reason?: string; tag?: string[];
  min_cost?: string; max_cost?: string; min_savings?: string; max_savings?: string; savings_cause?: string;
  min_latency?: string; max_latency?: string; stream?: string; batch?: string; cache_hit?: string; failover?: string;
  limit?: string; cursor?: string; sort?: "time" | "cost" | "savings" | "latency"; order?: "desc" | "asc";
};

/** A page of the ledger. `next` and `prev` are opaque cursors (null at either end); `total` is exact unless
 * `total_exact` is false, when it's the database's estimate ("about"). */
export type RequestsPage = {
  rows: LedgerRow[]; answered_by?: AnsweredBy;
  next?: string | null; prev?: string | null; limit?: number; sort?: string; order?: string;
  total?: number; total_exact?: boolean; user?: string | null; team?: string | null; start?: string | null; end?: string | null;
  sort_range_days?: number;
};

export function requestParams(q: RequestQuery): URLSearchParams {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (Array.isArray(v)) for (const x of v) { if (x) out.append(k, x); }
    else if (v != null && v !== "") out.set(k, String(v));
  }
  return out;
}

export type Deployment = {
  backend: string;
  model: string | null;
  enabled: boolean;
  managed: boolean;
  provisioner: string | null;
  state: string;
  reason?: string;
  endpoint?: string | null;
  replicas?: number;
  desired?: number;
  utilization?: number;
  waiting?: number; // requests queued in vLLM at the controller's last sample
  demand_usd_per_hour?: number;
  demand_tokens_per_second?: number;
  gpu_usd_per_hour?: number;
  updated?: string;
  planned?: number;
  note?: string | null;
  started_at?: number | null;
  warm_at?: number | null;
  run_cost?: number;
  month_cost?: number;
  only_route_for?: string[];
  burn_usd_per_hour?: number;
  /** Price of one replica for sizing: SkyPilot's catalog, a Kubernetes node type's configured rate (its share of the
   * node's GPUs), or the deployment's gpu_cost_per_hour. */
  price?: { usd_per_hour: number; source: "skypilot_catalog" | "node_type" | "configured"; cloud?: string; region?: string; instance_type?: string;
    spot?: boolean; configured_usd_per_hour?: number; node_type?: string; node_usd_per_hour?: number; node_gpus?: number; gpus?: number };
  status_message?: string | null; // what the provisioner says is holding a start up (no GPU node free, an image that won't pull)
  last_run?: { started_at: number; ended_at: number; cost: number } | null;
  replicas_info?: ReplicaInfo[];
  /** The last starts' times (launch request to ready, seconds), their average, and the last one's phases. */
  cold_start?: { samples: number[]; avg_seconds: number; last?: { total_seconds: number; at: number; cluster?: string | null; phases?: StartPhase[] | null } } | null;
  /** What demand one more replica needs to start: its price plus the margin, with the expected cold start paid back over hold_minutes. */
  start_needs?: { usd_per_hour: number; cold_start_minutes: number; basis: "measured" | "configured" | "static"; hold_minutes: number } | null;
  control?: GpuControlState | null;
  // Replica affinity: what the gateways know a session's warm cache from, and where sessions sit now.
  affinity?: { enabled: boolean; kv_events: boolean } | null;
  cache?: { affinity: boolean; source: "events" | "estimate" | "ttl"; kv_events: boolean; kv_events_problem: string | null };
  placements?: Placement[];
  replica_sessions?: Record<string, { sessions: number; warm: number }>;
  spec?: { accelerators: string; use_spot: boolean; clouds: string[]; regions: string[]; min_replicas: number; max_replicas: number;
    location: string | null; drain_minutes: number; kubernetes?: KubernetesSpec | null;
    /** SkyPilot: a replica stops by itself this long after the controller last renewed its lease, and is deleted after autostop_minutes idle. */
    lease_minutes?: number | null; autostop_minutes?: number | null;
    cold_start_minutes?: number; hold_minutes?: number; load_format?: string; weights_cache?: "none" | "volume";
    weights_cache_infra?: string | null; weights_cache_gb?: number | null; weights_cache_usd_per_month?: number | null } | null;
};

/** One phase of a replica's start, named after what ends it ("image pull", "weights download + weights load"). */
export type StartPhase = { phase: string; seconds: number };
/** A weights cache volume the controller made (kept until `meritorder gpu volumes delete`). */
export type GpuVolume = { name: string; deployment: string; infra: string | null; size_gb: number | null; usd_per_month: number | null; since: number };

/** Where a Kubernetes deployment runs: through KServe or llm-d, in which namespace (null: the controller's), on which node type. */
export type KubernetesSpec = { api: "kserve" | "llm-d"; namespace: string | null; node_type: string | null; node_usd_per_hour: number | null;
  node_gpus: number | null; inference_pool: string | null };

export type ReplicaInfo = { id: number | string; status: string; cloud?: string | null; region?: string | null; zone?: string | null;
  infra?: string | null; resources?: string | null; spot?: boolean; launched_at?: number | null; launched?: string | null; endpoint?: string | null;
  instance_type?: string | null; usd_per_hour?: number | null; url?: string | null; utilization?: number; waiting?: number;
  kv_capacity_tokens?: number; kv_usage?: number; prefix_hit_rate?: number; kv_events?: boolean;
  /** SkyPilot: the replica's cluster, seconds left on its lease, whether it's reached through an SSH tunnel, and what holds it up. */
  cluster?: string | null; lease_expires_in?: number | null; tunnel?: boolean; message?: string | null;
  /** SkyPilot: its start so far, phase by phase, and in all once ready (when this controller launched it). */
  startup?: { phases: StartPhase[]; total_seconds: number | null; download_seconds?: number; load_seconds?: number } | null };
/** A session's place on a GPU deployment: the replica that served its last turn, and how much of its prefix is
 * probably still cached there now (`share`), from KV events, an estimate from the replica's KV metrics, or the TTL. */
export type Placement = { id: string; session: string | null; thread: string | null; user: string | null; team: string | null;
  model: string | null; replica: string | number | null; at: number; tokens: number | null; expected: number | null; share: number;
  source: "events" | "estimate" | "ttl" | "cold"; replica_status: string | null };
export type GpuControlState = { mode: "off" | "on"; replicas?: number | null; drain?: boolean; until?: number | null; reason?: string | null;
  by?: string | null; at?: number | null; fleet?: boolean };
export type ControllerStatus = { alive: boolean; at: string | null; host: string | null; interval: number };
export type GpuReport = {
  deployments: Deployment[]; paused: GpuControlState | null; controller: ControllerStatus;
  fleet: { replicas: number; usd_per_hour: number; deployments: string[] };
  gpu_month: { spent: number; budget: number | null };
  limits: { max_gpu_usd_per_hour: number | null; max_gpu_replicas: number | null };
  daily: { day: string; backend: string; replica_hours: number; cost: number }[];
  volumes?: GpuVolume[];
};
export type GpuEvent = { id: number; ts: string; backend: string | null; kind: string; actor: string | null; detail: Record<string, unknown> };
export type Safeguards = { org_budget_usd_month: number | null; gpu_budget_usd_month: number | null; max_gpu_usd_per_hour: number | null;
  max_gpu_replicas: number | null; max_request_usd: number | null; alert_at: number[] };
export type SpendLine = { spent: number; projected: number; budget: number | null };
export type Controls = {
  month: { start: string; fraction: number }; safeguards: Safeguards;
  /** org: the ledger plus GPU time no request paid for (gpu_idle), split into APIs and GPUs. */
  spend: { org: SpendLine & { api: number; gpu: number; gpu_idle: number; reserved: number }; gpu: SpendLine & { by_backend: Record<string, number> } };
  fleet: { replicas: number; usd_per_hour: number; deployments: string[] };
  gpu_controls: { paused?: boolean; paused_by?: string; reason?: string };
  controller: ControllerStatus;
  teams: { name: string; spent: number; projected: number; budget_usd_month?: number | null; user_budget_usd_month?: number | null;
    max_session_usd?: number | null; rate_limits?: Record<string, number | null>; unconfigured?: boolean;
    /** What a failed attempt costs a person on the team (null: the organization's), and how the team's `auto` chooses. */
    human_cost_usd?: number | null; model_choice?: string }[];
  /** routing.human_cost_usd: the organization's cost of a failed attempt, for teams that don't set their own. */
  human_cost_usd?: number;
};
export type ControlResult = { controls: Record<string, unknown>; controller: ControllerStatus; warning?: string };

export type Range = { start: string; end: string };

/** Recorded (the ledger) against billed (the provider's own cost data). `pct` is the difference as a percent of recorded. */
export type BillTotals = { recorded: number; billed: number; billed_matched?: number; billed_unmatched: number; left_out: number;
  adjustments: number; difference: number; pct: number | null };
export type BillSourceStatus = { name: string; type: string; backends: string[]; ok: boolean | null; error: string | null;
  fetched_at: string | null; failed_at: string | null; covered_from: string | null; complete_through: string | null; notes: string[];
  filtered: boolean; partial_days: number; totals: BillTotals; settled: BillTotals };
export type BillRow = { period: string; source: string; type: string; model: string; recorded: number; billed: number; difference: number;
  pct: number | null; requests: number; lines: number; partial: boolean; over: boolean;
  status: "matched" | "partial" | "not_billed" | "not_recorded";
  recorded_tokens: Record<string, number>; billed_tokens: Record<string, number>; billed_by_kind: Record<string, number>;
  /** Of recorded and billed, what was through the provider's batch API. */
  recorded_batch: number; billed_batch: number };
export type BillLineOut = { source: string; line: string; status: "unmatched" | "excluded"; why: string; cost: number; days: number;
  first: string; last: string };
export type BillGpuRow = { period: string; deployment: string; recorded: number; billed: number; difference: number; pct: number | null;
  billed_by_source: Record<string, number>; partial: boolean; over: boolean };
export type Bills = { start: string; end: string; by: "day" | "month"; thresholds: { alert_pct: number; alert_min_usd: number };
  settled: BillTotals; sources: BillSourceStatus[]; rows: BillRow[]; unmatched: BillLineOut[]; gpu: BillGpuRow[];
  uncovered: { backend: string; recorded: number; why: string }[]; answered_by?: AnsweredBy };

/** A batch through the providers' batch APIs (Reports → Batches). `status` is OpenAI's word for it, whichever API made it. */
export type BatchStatus = "in_progress" | "cancelling" | "finalizing" | "completed" | "cancelled" | "expired" | "failed";
export type BatchCounts = { processing: number; succeeded: number; errored: number; canceled: number; expired: number };
export type BatchRow = { id: string; created_at: string; user: string; team: string; format: "openai" | "anthropic"; endpoint: string | null;
  status: BatchStatus; requests: number; counts: BatchCounts; providers: Record<string, number>; estimated_cost: number; reserved_cost: number;
  actual_cost: number; baseline_cost: number; savings_batch: number; savings_total: number; ended_at: string | null; expires_at: string | null;
  cancelled_by: string | null; purged: boolean; errors: string[]; can_cancel: boolean;
  /** Requests on self-hosted GPUs (still there, or run there), and what running there took off the batch price. */
  on_gpus: number; savings_self_hosting: number };
/** Admins: where the gateway holds batch content (uploads, GPU work), sealed, and the batch key's rotation. */
export type BatchStorage = { held_in: string; key: string | null; previous_key: string | null; previous_readable_until: string | null;
  previous_usable: boolean; files_held: number; files_under_previous: number; files_elsewhere: number;
  gpu: { enabled: boolean; handoff_hours: number; priority: number } };
export type BatchesReport = { start: string; end: string; rows: BatchRow[]; retention_days: number; gpu_backends: string[];
  storage?: BatchStorage;
  totals: { batches: number; requests: number; running: number; on_gpus: number; estimated_cost: number; reserved_cost: number;
    actual_cost: number; savings_batch: number; savings_self_hosting: number; savings_total: number } };

export type ConsoleOverview = {
  scope: string; budget: number | null; forecast: number | null; requests_per_minute: number; gateways: number; config_version: number | null;
  /** set aside this month by running batches: budgets count it, spend doesn't yet */
  reserved?: number;
  today: Totals; month: Totals;
};
/** spend = api + gpu; gpu_idle (organization only) is the part of gpu no request paid for. saved can be negative. */
export type Totals = { spend: number; api: number; gpu: number; gpu_idle: number; baseline: number; saved: number; requests: number;
  cache_hit_rate: number; savings_by_cause?: Record<string, number> };
export type MeritTier = { model: string; tokens: number; tokens_per_second: number; cost: number; price_per_m: number; requests: number; gpu: boolean };
export type Merit = { minutes: number; demand_tokens_per_second: number; tiers: MeritTier[]; clearing_price_per_m: number | null; blended_price_per_m: number | null };
export type FeedEvent = { ts: string; user: string; team: string; kind: string; requested: string; model: string; backend: string;
  backend_kind: string; floor: string | null; reason?: string | null; status: number; upgraded: boolean; tokens: number; cached: number; cost: number; saved: number;
  latency_ms: number | null; error: string | null };
export type BackendStatus = { name: string; kind: string; format: string; api?: string; host: string | null; enabled: boolean; live: boolean; reason: string | null;
  circuit_open: boolean; draining: boolean; effort_format: string; models: string[]; requests: number; errors: number; error_rate: number;
  /** The provider's batch API, or why the backend has none (batches go only where there's one). */
  batch?: { api: string | null; why: string };
  avg_latency_ms: number | null; avg_ttft_ms: number | null;
  /** Why the whole backend is being skipped, and models skipped on it (over capacity, or not deployed there). */
  skipped?: Skip | null; skipped_models?: Record<string, Skip>;
  /** Measured speed per upstream model (this gateway process), used to rank routes that cost about the same. */
  speeds?: Record<string, RouteSpeed> };
export type RouteSpeed = { first_token_ms: number; tokens_per_second: number | null; samples: number; seconds_ago: number };
/** A model's results on one task type. `score`: 1–5 on routing's scale, from hard tasks (levels 4–5), with its 90% range
 * (`low`–`high`); null with `why` when there's too little to go on. */
export type EvalTaskScore = { levels: Record<string, number>; counts?: Record<string, number>; hard?: { passed: number; runs: number };
  score?: number | null; low?: number | null; high?: number | null; why?: string; pass_rate: number; efficiency?: EvalEfficiency };
export type EvalApplyTarget = "quality" | "quality_override";
export type EvalApplyChange = { model: string; task: string; from: number | null; to: number; low: number; high: number; in_use: number | null };
export type EvalApplyResult = { version: number | null; changed: string[]; changes: EvalApplyChange[]; target: EvalApplyTarget; source: string };
/** What a model's tasks took: its own time to completion, turns, tool calls, tokens, and cost per task and per task solved. */
export type EvalEfficiency = { median_s: number; p90_s: number; turns: number; tool_calls: number; output_tokens: number; input_tokens: number;
  cost_per_task: number; cost_per_pass: number | null; cut_off: number };
export type EvalModelScore = { tasks: Record<string, EvalTaskScore>; pass_rate: number; errors?: number; skipped?: number; cost: number;
  avg_latency_ms?: number; failed?: { id: string; why: string }[]; efficiency?: EvalEfficiency };
export type EvalRequest = { models: string[]; suite: string; repeat: number; only?: string[] | null; judges?: string[] | null;
  /** The most the run may spend: no new task starts once it has. Required to start a run. */
  max_cost_usd?: number | null };
/** A task as the Tasks tab lists it. */
export type EvalTaskBrief = { id: string; task: string; level: number; kind: string; summary: string; graded_by: string; prompt_words: number;
  generated: boolean; max_turns: number | null };
export type EvalTool = { name: string; description?: string; parameters?: Doc };
/** One task in full: what the model gets, and everything it's graded with (which the model never sees). */
export type EvalTaskDetail = { id: string; suite: string; task: string; level: number; kind: string; summary: string | null; graded_by: string;
  prompt: string; effort: string | null; max_tokens: number | null; generated: { by: string; args: Doc } | null; checks: Doc[]; tools: EvalTool[];
  code: { tests: string; tests_count: number; forbid: string[]; timeout: number; per_test_timeout: number; reference: string | null; wrong: string | null } | null;
  agent: { env: string; about: string; grading: string; env_args: Doc; max_turns: number; needs_sandbox: boolean;
    visible: Record<string, string>; grader: Record<string, string> } | null;
  judged: { rubric: string[]; reference_answer: string | null } | null;
  results: (EvalResult & { run_id: string; ran_at?: string })[] };
export type EvalRunner = { sandbox: boolean; why: string | null; gateway: string; seen_at: string };
export type EvalSuites = { suites: { name: string; tasks: number; kinds: Record<string, number>; levels: Record<string, number>; task_types: string[] }[];
  runner: EvalRunner | null; judges: string[]; default_judges: string[] };
export type EvalEstimate = { usd: number; by_model: Record<string, number>; judging_usd: number; tasks: number; runs: number; needs_sandbox: number;
  judged: number; unpriced: string[]; judges: string[]; runner: EvalRunner | null;
  default_max_cost_usd: number; budget_left_usd: number | null; confirm_above_usd: number; warning: string[] };
export type EvalCriterion = { n: number; met: boolean; why: string; criterion: string };
export type EvalResult = { model: string; task_id: string; task: string; level: number; passed: boolean; reasons: string[]; cost: number;
  latency_ms: number; error: string | null; kind: string; turns: number; detail: string; skipped: boolean;
  /** Recorded since output limits were lifted; older runs lack them. */
  effort?: string | null; backend?: string | null; output_tokens?: number; input_tokens?: number; model_ms?: number; tool_calls?: number;
  truncated?: boolean; answer?: string; steps?: string[];
  judging?: { judge: string; criteria: EvalCriterion[] }[] };
export type EvalRun = { id: string; created_at: string; created_by: string; team: string;
  status: "queued" | "running" | "cancelling" | "done" | "failed" | "cancelled"; executor: "worker" | "cli";
  request: EvalRequest & { repeat: number; judges: string[]; estimate_usd?: number; tasks?: number };
  progress: { total?: number; done?: number; passed?: number; cap?: number; stopped?: string }; scores: Record<string, EvalModelScore> | null; results?: EvalResult[] | null;
  error: string | null; cost: number; started_at: string | null; finished_at: string | null; applied_version: number | null };
export type Skip = { why: string; status: number; seconds_left: number; capacity: boolean };
export type AttentionItem = { level: "critical" | "warning" | "info"; title: string; detail: string; page?: string | null; ts?: string };
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Doc = Record<string, any>;
export type ConfigSource = { source: "file" | "database"; version: number | null; yaml: string; document: Doc; storage: string; summary: ConfigSummary };
export type Validation = { ok: boolean; error?: string; summary?: ConfigSummary; document?: Doc; yaml?: string };
export type Person = { id: string; email: string | null; display_name: string | null; team: string; role: string; managed: "idp" | "local";
  disabled: boolean; active_keys: number; last_seen: string | null; spend_month: number; requests_month: number;
  /** Signs in with a password kept here; has two-factor on (decision 73). */
  has_password?: boolean; two_factor?: boolean;
  /** This person's own monthly limit; null means default_budget_usd_month applies (null there too: no per-person limit). */
  budget_usd_month: number | null; default_budget_usd_month: number | null;
  /** Their last Google Groups reading, when the gateway reads Google Groups (null: not signed in with Google since). */
  google?: GoogleReading | null;
  /** Only in the answer to disabling someone: how many responses they kept in stored mode were deleted with it. */
  stored_responses_deleted?: number };
/** Which sign-in mapping gave a team or role. */
export type MappedFrom = { by: "address" | "group" | "domain" | "default"; key: string | null };
export type GoogleReading = {
  /** Every group for admins; a team lead sees only the groups the mappings use (`hidden` counts the rest). */
  groups: { email: string; name: string; direct: boolean; mapped: boolean }[]; hidden: number;
  /** live: read on the last try. last_known: Google didn't answer, the last groups read still stand. none: no groups known, email rules only. */
  using: "live" | "last_known" | "none"; error: string | null;
  fetched_at: string | null; checked_at: string | null; failing_since: string | null;
  /** What the mappings give (the person's team differs when an admin pinned it). */
  team: string; role: string; team_from: MappedFrom; role_from: MappedFrom | null };
export type PersonChange = { team?: string; role?: string; disabled?: boolean; follow_idp?: boolean; budget_usd_month?: number | null };
export type ConfigSummary = { backends_enabled: string[]; backends_disabled: Record<string, string>; models: number; embedding_models: number;
  unpriced_routes: string[]; teams: string[]; effort: Record<string, string> };
export type TeamStatus = { name: string; configured: boolean; budget_usd_month: number | null; user_budget_usd_month: number | null;
  max_session_usd: number | null; rate_limits: Record<string, number | null>; baseline_model: string | null; allowed_models: string[];
  allowed_backends: string[]; compression: boolean; compression_model: boolean; response_cache: boolean; stored_responses: StoredSize;
  judged_labels?: { enabled: boolean; sample_rate: number; max_usd_month: number | null }; spend: number; saved: number; requests: number };
/** A team's stored mode for the Responses API and what it holds: counts and sealed sizes, never content. */
export type StoredSize = { enabled: boolean; retention_days: number | null; readers: string[]; responses: number; conversations: number;
  items: number; bytes: number; people: number; running: number; old_key: number; oldest: string | null };
/** The key stored responses are sealed with: its public id, never the key. */
export type StoredKey = { configured: boolean; env: string; file: string | null; id: string | null; old_keys: number; memory: boolean };
/** Model-based compression's service (GET /v1/admin/compressor): whether it answers, with which model, who uses it. */
export type CompressorStatus = { configured: boolean; teams: string[]; url?: string; model?: string; ttl_seconds?: number; reachable?: boolean;
  loaded?: string; version?: string; device?: string; matches?: boolean; error?: string; last_error?: string | null };
export type ConfigVersion = { id: number; created_at: string; author: string; comment: string | null };

/** What one GPU replica needs to pay for itself (tokens per minute and requests per hour at API prices), beside traffic. */
export type BreakevenNeed = { capacity_tokens_per_minute: number | null; gpu_usd_per_million: number | null; api_usd_per_million: number | null;
  tokens_per_minute: number | null; requests_per_hour: number | null; pays: boolean };
export type BreakevenTraffic = { tokens_per_minute: number; peak_tokens_per_minute: number; requests_per_hour: number; usd_per_hour: number };
/** The GPU replica a figure is for: sized to the model and priced from SkyPilot's catalog, or its own deployment's. */
export type BreakevenGpu = { model: string; deployment: string | null; usd_per_hour: number; capacity_tokens_per_second: number;
  estimated_capacity_tokens_per_second: number | null; capacity_basis: "configured" | "estimated"; accelerator?: string | null; count?: number | null;
  memory_gb?: number | null; cloud?: string | null; region?: string | null; instance_type?: string | null; spot?: boolean | null;
  source?: "catalog" | "controller" | "node_type" | "configured"; on_demand_usd_per_hour?: number | null; spot_usd_per_hour?: number | null;
  computes_in?: string | null } & BreakevenWeightsServed;
/** The precision a replica serves the weights in ("4-bit", "FP8", "16-bit"), what they're published in, and a warning when
 * they're served below it (quality scores are for the published weights). */
export type BreakevenWeightsServed = { weights?: string | null; published_weights?: string | null; quality_note?: string | null };
/** How the weights are served: as published, a 16-bit checkpoint in FP8, or everything above 4 bits in 4. */
export type BreakevenWeights = "published" | "fp8" | "4bit";
/** Per task type, the best open model that has a replica (the highest level GPUs could serve), and a better one that can't run. */
export type BreakevenHostable = { level: number | null; model: string | null; quality: number | null; gpu: BreakevenGpu | null;
  better_unrunnable: { model: string; quality: number; why: string | null } | null };
export type BreakevenGroup = BreakevenNeed & { task: string; level: number; floor: string; cheapest: { model: string; backend: string } | null;
  open_models: string[]; gpu: BreakevenGpu | null; why: string | null; traffic: BreakevenTraffic };
export type BreakevenModel = BreakevenNeed & { model: string; hf_id: string; quality: Record<string, number>; basis: string;
  size: ({ weight_bytes: number; total_params: number; active_params: number; basis: string } & BreakevenWeightsServed) | null;
  gpu: BreakevenGpu | null; why: string | null;
  own_api_usd_per_million: number | null; traffic: BreakevenTraffic };
export type BreakevenOptions = { days: number; utilization: number; efficiency: number; spot: boolean; weights: BreakevenWeights; clouds: string[] };
export type Breakeven = { days: number; hours: number; answered_by?: AnsweredBy;
  assumptions: { utilization: number; margin: number; efficiency: number; spot: boolean; weights: BreakevenWeights; clouds: string[]; fit: number };
  hostable: Record<string, BreakevenHostable>;
  mix: { input: number; cache_read: number; cache_write: number; output: number; tokens_per_request: number | null; source: "traffic" | "assumed" };
  prices: { fetched_at: string | null; catalog_updated: Record<string, string> | null; skypilot: string | null; clouds: string[]; problem: string | null;
    refreshing: boolean; offers: number };
  sizes: { fetched_at: string | null };
  groups: BreakevenGroup[]; models: BreakevenModel[] };

export type WhatIf = {
  model: string; days: number; hours_with_traffic: number; hours_gpus_would_run: number; peak_replicas: number;
  addressable_api_cost: number; gpu_cost: number; net_savings: number; projected_monthly_savings: number;
  share_of_tokens_on_gpu: number; self_hostable: boolean; note: string; answered_by?: AnsweredBy;
};

export type ApiKey = {
  id: string; user_id: string; team: string; role: string; name: string | null; tags: Record<string, string>;
  created_at: string; expires_at: string | null; revoked_at: string | null; last_used_at?: string | null; status: string;
  /** The person's role on their record, and what the key acts as: the lower of the two (a record never raises a key). */
  user_role?: string | null; acts_as?: string;
};
export type NewKeyResult = { id: string; api_key: string; user: string; team: string; role: string; expires_at: string | null };
export type PriceTier = { above: number; input: number; output: number; cache_read?: number | null; cache_write?: number | null; cache_write_1h?: number | null };
/** Dollars per million tokens. `source`: openrouter or litellm (live lists the worker refreshes) or config (an override). */
export type Price = { input: number; output: number; cache_read: number | null; cache_write: number | null; cache_write_1h?: number | null; tiers?: PriceTier[]; source?: string | null };
/** When prices were last checked against the live lists, and what that found. */
export type PriceStatus = { checked_at?: string; refresh_minutes: number; priced_routes?: number; missing?: string[]; changed?: string[];
  sources?: Record<string, { ok: boolean; fetched_at?: string; error?: string }>;
  disagreements?: { model: string; backend: string; field: string; listed: number; openrouter: number }[] };
/** How a model's quality score for a task type was worked out (Epoch: its chance on the reference task). */
export type QualityDetail = { chance: number; ability?: number; eci?: number | null; basis: string;
  evidence: Record<string, { score: number; variant: string; ability?: number }> };
export type QualityStatus = { source: string; scale: string; anchor?: Record<string, number> | null; reference: Record<string, string>; evidence?: Record<string, string[]>;
  refresh_hours: number; updated?: string | null;
  status?: { ok: boolean; fetched_at?: string; error?: string } | null; attribution?: string | null; missing: string[]; stale: boolean };
/** Who people sign in with, for the People page's wording. */
export type Idp = "google" | "entra" | "oidc";
/** How a model's prompt token counts are calibrated: reported tokens per estimated token, learned from its replies. */
export type TokenCalibration = { ratio: number; source: "model" | "family" | "none"; family: string; samples: number;
  spread: number | null; margin: number; updated: number | null };
/** GET /v1/admin/reporting: where the ledger and reports live (clickhouse.Reporting.describe). */
export type ClickHouseCopy = { from?: string | null; through?: string | null; checked_at?: string | null; behind_seconds: number | null;
  copied?: number; error?: string | null; error_at?: string | null; repaired?: string[]; ahead?: string[]; busy?: string[] };
export type ClickHouseStatus = { url: string; database: string; user: string; password_set: boolean; reads: "large" | "all" | "none";
  ttl_days: number | null; rollup_ttl_days: number | null; copy_seconds: number; ok: boolean; version: string | null; error: string | null;
  latency_ms: number | null; tables: Record<string, { rows: number; bytes_on_disk: number }>; copy: ClickHouseCopy; answering: boolean;
  answers: string | null };
export type ReportingStatus = { store: string; postgres: { ok: boolean; error: string | null }; configured: boolean; note: string | null;
  clickhouse: ClickHouseStatus | null };

export type AdminConfig = {
  baseline_model: string | null;
  oidc: boolean;
  prices?: PriceStatus;
  quality?: QualityStatus;
  backends: { name: string; kind: string; format: string; enabled: boolean; live: boolean; base_url: string | null }[];
  models: { name: string; kind?: string; quality: Record<string, number>; quality_sources?: Record<string, string>;
    quality_detail?: Record<string, QualityDetail>; context_window: number | null; hf_id: string | null; tokens?: TokenCalibration;
    /** Per task type, the chance of solving work at each level 1–5, used when routing by cost per success. */
    chances?: Record<string, Record<string, number>>;
    routes: { backend: string; model: string; live: boolean; price: Price | null }[] }[];
  teams: { name: string; budget_usd_month: number | null; user_budget_usd_month: number | null; baseline_model: string | null;
    allowed_models: string[]; allowed_backends: string[] }[];
};

export const CAUSES = [
  ["savings_routing", "Routing"],
  ["savings_caching", "Prompt caching"],
  ["savings_self_hosting", "Self-hosting"],
  ["savings_batch", "Batch discount"],
  ["savings_compression", "Compression (rules)"],
  ["savings_compression_model", "Compression (model)"],
  ["savings_response_cache", "Response cache"],
] as const;

/** The MCP gateway (decision 75): GET /v1/admin/mcp. */
export type McpServer = { name: string; description: string; url: string; auth: "none" | "api_key" | "forward"; forward_header: string | null;
  pinning: "off" | "shadow" | "enforce"; new_tools: "allow" | "hold"; untrusted_content: boolean; path: string; tools?: number; enabled?: boolean };
export type McpTool = { server: string; name: string; status: "approved" | "new" | "changed"; hash: string; first_seen: string;
  changed_at: string | null; approved_by: string | null; approved_at: string | null; findings: string[] };
export type McpCount = { server: string; tool: string; status: string; calls: number; avg_ms: number | null };
export type McpOverview = { servers: McpServer[]; tools: McpTool[]; counts: McpCount[]; days: number; can_approve: boolean };
export type McpToolDetail = McpTool & { definition: Record<string, unknown>; approved_definition: Record<string, unknown> | null };
export type McpCall = { id: number; ts: string; server: string; tool: string; user_id: string | null; team: string | null; status: string;
  reason: string | null; duration_ms: number | null; findings: Record<string, number> };

export class ApiError extends Error {
  /** The gateway wants an authenticator code too (two-factor sign-in, decision 73). */
  needsCode = false;
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** The header the console sends on every call (auth.CSRF_HEADER): with the session cookie, a change needs it, and no
 * other site's form or link can send it. */
export const CONSOLE_HEADER = "x-meritorder-console";
/** Dispatched on window when a console call finds the session ended (App listens and shows the sign-in page). */
export const SESSION_ENDED = "meritorder:session-ended";

async function call(path: string, init: RequestInit = {}, key: string | null = null): Promise<Response> {
  const headers: Record<string, string> = { [CONSOLE_HEADER]: "1" };
  if (key) headers.authorization = `Bearer ${key}`;
  if (init.body) headers["content-type"] = "application/json";
  const resp = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      message = body.detail ?? body.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    // A 401 on a session call (not sign-in itself, nor a key the demo sends): the session has ended (expired, signed out
    // elsewhere, disabled). The console goes back to its sign-in page instead of showing the error on every panel.
    if (resp.status === 401 && !key && !path.startsWith("/auth/")) window.dispatchEvent(new Event(SESSION_ENDED));
    const err = new ApiError(resp.status, message);
    err.needsCode = resp.headers.get("x-meritorder-two-factor") === "required";
    throw err;
  }
  return resp;
}

const post = async <T>(path: string, body: unknown = {}): Promise<T> =>
  (await call(path, { method: "POST", body: JSON.stringify(body) })).json() as Promise<T>;

/** Signing in and out (accounts.py). The session lives in an HttpOnly cookie the console never sees. */
export const auth = {
  config: async () => (await call("/auth/config")).json() as Promise<AuthConfig>,
  session: async () => (await call("/auth/session")).json() as Promise<{ signed_in: boolean; reason?: string | null }>,
  login: (body: { email: string; password: string; code?: string } | { api_key: string }) => post<{ user: string; role: string }>("/auth/login", body),
  logout: () => post<{ signed_out: boolean }>("/auth/logout"),
  // The token goes in the body, never the URL, so no access log or proxy records it.
  invite: (token: string) => post<InviteInfo>("/auth/invite/info", { token }),
  acceptInvite: (token: string, body: { password: string; name?: string }) =>
    post<{ user: string; role: string }>("/auth/invite/accept", { token, ...body }),
  changePassword: (current: string, next: string) => post<{ changed: boolean; other_sessions_ended: number }>("/auth/password", { current, new: next }),
  twoFactorSetup: () => post<{ secret: string; uri: string }>("/auth/two-factor/setup"),
  twoFactorEnable: (code: string) => post<{ enabled: boolean; recovery_codes: string[] }>("/auth/two-factor/enable", { code }),
  twoFactorDisable: (password: string) => post<{ enabled: boolean }>("/auth/two-factor/disable", { password }),
};

export class Api {
  /** `key`: only the demo passes one. The console itself is signed in by its session cookie. */
  constructor(public key: string | null = null) {}

  request(path: string, init: RequestInit = {}): Promise<Response> {
    return call(path, init, this.key);
  }

  async get<T>(path: string): Promise<T> {
    return (await this.request(path)).json() as Promise<T>;
  }

  me() {
    return this.get<Me>("/v1/reports/me");
  }

  mcp(days = 7) {
    return this.get<McpOverview>(`/v1/admin/mcp?days=${days}`);
  }

  mcpTool(server: string, tool: string) {
    return this.get<McpToolDetail>(`/v1/admin/mcp/tools/${encodeURIComponent(server)}/${encodeURIComponent(tool)}`);
  }

  approveMcpTool(server: string, tool: string, hash: string) {
    return this.send<{ ok: boolean }>("POST", `/v1/admin/mcp/tools/${encodeURIComponent(server)}/${encodeURIComponent(tool)}/approve`, { hash });
  }

  mcpCalls(limit = 100) {
    return this.get<{ calls: McpCall[] }>(`/v1/admin/mcp/calls?limit=${limit}`);
  }

  /** Names and email addresses of the people this viewer may see, by id (people.tsx). */
  people() {
    return this.get<{ people: Record<string, { name: string | null; email: string | null }> }>("/v1/reports/people");
  }

  summary(range: Range, groupBy: string, filters: Record<string, string> = {}) {
    const q = new URLSearchParams({ start: range.start, end: range.end, group_by: groupBy, ...filters });
    return this.get<Summary>(`/v1/reports/summary?${q}`);
  }

  /** A page of the ledger, newest first unless sorted otherwise; with no filters, the latest `limit` requests. */
  requests(limit = 100, query: RequestQuery = {}) {
    return this.get<RequestsPage>(`/v1/reports/requests?${requestParams({ limit: String(limit), ...query })}`);
  }

  /** Every row the Requests view's filters match, as CSV (streamed by the gateway). */
  async downloadRequestsCsv(query: RequestQuery) {
    const { limit: _l, cursor: _c, ...rest } = query;
    const blob = await (await this.request(`/v1/reports/requests.csv?${requestParams(rest)}`)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `meritorder-requests-${(query.start ?? new Date().toISOString()).slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  levels() {
    return this.get<LevelsView>("/v1/reports/levels");
  }

  /** Your own traffic as the routing benchmark: success by exploration arm per task type and level (decision 59). */
  levelOutcomes(days = 30) {
    return this.get<LevelOutcomes>(`/v1/reports/levels/outcomes?days=${days}`);
  }

  applyLevel(task: string, level: number, to: number) {
    return this.send<{ version: number | null; changed: string[]; level_map: Record<string, number> }>("POST", "/v1/admin/levels/apply",
      { task, level, to });
  }

  /** Set a recommendation aside until its evidence changes, or bring it back (`undo`). */
  dismissLevel(rec: Pick<LevelRecommendation, "task" | "level" | "to" | "direction">, undo = false) {
    return this.send<{ dismissed: Record<string, unknown> }>("POST", "/v1/admin/levels/dismiss",
      { task: rec.task, level: rec.level, to: rec.to, direction: rec.direction, undo });
  }

  routerReport(days: number) {
    return this.get<RouterReport>(`/v1/reports/router?days=${days}`);
  }

  trainRouter() {
    return this.send<{ requested: boolean }>("POST", "/v1/admin/router/train");
  }

  useRouterModel(version: number | null) {
    return this.send<RouterStatus>("POST", "/v1/admin/router/use", { version });
  }

  /** The expected monthly cost of a team's judged labels with these settings, from its recorded traffic. */
  judgedEstimate(team: string, settings: Doc, days = 14) {
    return this.send<JudgedEstimate>("POST", "/v1/admin/judged-labels/estimate", { team, settings, days });
  }

  bills(range: Range, by: "day" | "month") {
    return this.get<Bills>(`/v1/reports/bills?${new URLSearchParams({ start: range.start, end: range.end, by })}`);
  }

  batches(range: Range) {
    return this.get<BatchesReport>(`/v1/reports/batches?${new URLSearchParams({ start: range.start, end: range.end })}`);
  }

  cancelBatch(b: Pick<BatchRow, "id" | "format">) {
    return this.send<unknown>("POST", b.format === "anthropic" ? `/v1/messages/batches/${b.id}/cancel` : `/v1/batches/${b.id}/cancel`);
  }

  refreshBills() {
    return this.send<{ sources: Record<string, { ok: boolean; error?: string | null; lines?: number }> }>("POST", "/v1/reports/bills/refresh");
  }

  async downloadBills(range: Range, by: "day" | "month") {
    const q = new URLSearchParams({ start: range.start, end: range.end, by });
    const blob = await (await this.request(`/v1/reports/bills.csv?${q}`)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `meritorder-bills-${range.start}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  keys() {
    return this.get<{ keys: ApiKey[] }>("/v1/admin/keys");
  }

  async createKey(body: { user: string; team?: string; role: string; name?: string; expires_days?: number }) {
    return (await this.request("/v1/admin/keys", { method: "POST", body: JSON.stringify(body) })).json() as Promise<NewKeyResult>;
  }

  async revokeKey(id: string) {
    return (await this.request(`/v1/admin/keys/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
  }

  evals() {
    return this.get<{ models: Record<string, EvalModelScore & { ran_at: string; suite: string; run_id?: string; runs?: string[] }> }>("/v1/admin/evals");
  }

  evalSuites() { return this.get<EvalSuites>("/v1/admin/evals/suites"); }
  evalEstimate(req: EvalRequest) { return this.send<EvalEstimate>("POST", "/v1/admin/evals/estimate", req); }
  startEval(req: EvalRequest) { return this.send<EvalRun>("POST", "/v1/admin/evals/runs", req); }
  evalTasks(suite: string) { return this.get<{ suite: string; tasks: EvalTaskBrief[] }>(`/v1/admin/evals/tasks?suite=${encodeURIComponent(suite)}`); }
  evalTask(suite: string, id: string) {
    return this.get<EvalTaskDetail>(`/v1/admin/evals/tasks/${encodeURIComponent(id)}?suite=${encodeURIComponent(suite)}`);
  }
  evalRuns() { return this.get<{ runs: EvalRun[]; runner: EvalRunner | null }>("/v1/admin/evals/runs"); }
  evalRun(id: string) { return this.get<EvalRun>(`/v1/admin/evals/runs/${encodeURIComponent(id)}`); }
  cancelEval(id: string) { return this.send<EvalRun>("POST", `/v1/admin/evals/runs/${encodeURIComponent(id)}/cancel`); }
  applyEval(id: string, opts: { models?: string[]; target?: EvalApplyTarget; dry_run?: boolean } = {}) {
    return this.send<EvalApplyResult>("POST", `/v1/admin/evals/runs/${encodeURIComponent(id)}/apply`, opts);
  }

  adminConfig() {
    return this.get<AdminConfig>("/v1/admin/config");
  }

  reporting() {
    return this.get<ReportingStatus>("/v1/admin/reporting");
  }

  gpuCandidates() {
    return this.get<{ models: { id: string; hf_id: string; quality: Record<string, number> }[] }>("/v1/reports/gpu/candidates");
  }

  gpuBreakeven(o: BreakevenOptions) {
    const q = new URLSearchParams({ days: String(o.days), utilization: String(o.utilization), efficiency: String(o.efficiency),
      spot: String(o.spot), weights: o.weights, ...(o.clouds.length ? { clouds: o.clouds.join(",") } : {}) });
    return this.get<Breakeven>(`/v1/reports/gpu/breakeven?${q}`);
  }

  refreshGpuPrices() {
    // 202 {requested}: the gateway has no SkyPilot, so the GPU controller was asked to get them.
    return this.send<{ offers?: number; models?: number; unsized?: string[]; requested?: boolean; controller?: boolean }>(
      "POST", "/v1/reports/gpu/breakeven/refresh");
  }

  whatIf(model: string, gpuUsdPerHour: number, capacity: number, days: number) {
    const q = new URLSearchParams({ model, gpu_usd_per_hour: String(gpuUsdPerHour), capacity_tokens_per_second: String(capacity), days: String(days) });
    return this.get<WhatIf>(`/v1/reports/gpu/whatif?${q}`);
  }

  overview() { return this.get<ConsoleOverview>("/v1/console/overview"); }
  merit(minutes = 15) { return this.get<Merit>(`/v1/console/merit?minutes=${minutes}`); }
  feed(limit = 60) { return this.get<{ events: FeedEvent[] }>(`/v1/console/feed?limit=${limit}`); }
  backends() { return this.get<{ minutes: number; backends: BackendStatus[] }>("/v1/console/backends"); }
  teams() { return this.get<{ teams: TeamStatus[]; stored_key: StoredKey }>("/v1/console/teams"); }
  compressor() { return this.get<CompressorStatus>("/v1/admin/compressor"); }
  attention() { return this.get<{ items: AttentionItem[] }>("/v1/console/attention"); }
  configSource() { return this.get<ConfigSource>("/v1/admin/config/source"); }
  configVersions() { return this.get<{ active: number | null; versions: ConfigVersion[] }>("/v1/admin/config/versions"); }
  configVersion(id: number) { return this.get<ConfigVersion & { yaml: string }>(`/v1/admin/config/versions/${id}`); }
  async send<T>(method: string, path: string, body?: unknown): Promise<T> {
    return (await this.request(path, { method, body: body === undefined ? undefined : JSON.stringify(body) })).json() as Promise<T>;
  }
  validateConfig(input: { yaml?: string; document?: Doc }) { return this.send<Validation>("POST", "/v1/admin/config/validate", input); }
  /** ``baseVersion``: the version the editor opened (null: the file). If someone saved since, the gateway answers 409. */
  saveConfig(input: { yaml?: string; document?: Doc }, comment: string, baseVersion: number | null) {
    return this.send<{ version: number; summary: ConfigSummary }>("PUT", "/v1/admin/config", { ...input, comment, base_version: baseVersion });
  }
  invites() {
    return this.get<{ invites: Invite[] }>("/v1/admin/invites");
  }

  async createInvite(body: { email: string; team?: string; role?: string }) {
    return (await this.request("/v1/admin/invites", { method: "POST", body: JSON.stringify(body) })).json() as Promise<Invite>;
  }

  async withdrawInvite(id: string) {
    return (await this.request(`/v1/admin/invites/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
  }

  async resetTwoFactor(user: string) {
    return (await this.request(`/v1/admin/users/${encodeURIComponent(user)}/two-factor/reset`, { method: "POST", body: "{}" })).json();
  }

  async resetLink(user: string) {
    return (await this.request(`/v1/admin/users/${encodeURIComponent(user)}/reset`, { method: "POST", body: "{}" })).json() as Promise<Invite>;
  }

  users() {
    return this.get<{ users: Person[]; teams: string[]; oidc: boolean; idp: Idp | null; scim: boolean; google_groups?: boolean;
      google_groups_refresh_seconds?: number | null }>("/v1/admin/users");
  }
  updateUser(id: string, patch: PersonChange) {
    return this.send<Person>("PATCH", `/v1/admin/users/${encodeURIComponent(id)}`, patch);
  }
  restoreConfig(id: number, baseVersion: number | null) {
    return this.send<{ version: number }>("POST", `/v1/admin/config/versions/${id}/restore`, { base_version: baseVersion });
  }
  refreshQuality() { return this.send<{ ok: boolean; stored?: boolean; changed?: string[]; missing?: string[]; error?: string; why?: string }>("POST", "/v1/admin/quality/refresh"); }
  refreshPrices() {
    return this.send<{ priced_routes: number; missing: string[]; updated: string; changed: string[]; disagreements: PriceStatus["disagreements"] }>("POST", "/v1/admin/prices/refresh");
  }
  async downloadLedger(range: Range) {
    const q = new URLSearchParams({ start: range.start, end: range.end });
    const blob = await (await this.request(`/v1/reports/export.jsonl?${q}`)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `meritorder-ledger-${range.start}.jsonl`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  controls() { return this.get<Controls>("/v1/admin/controls"); }
  gpuEvents(limit = 100) { return this.get<{ events: GpuEvent[] }>(`/v1/admin/gpu/events?limit=${limit}`); }
  gpuControl(backend: string, body: { mode: "auto" | "off" | "on"; replicas?: number; drain?: boolean; hours?: number; reason?: string }) {
    return this.send<ControlResult>("POST", `/v1/admin/gpu/${encodeURIComponent(backend)}/control`, body);
  }
  stopAllGpus(reason: string, drain: boolean) { return this.send<ControlResult>("POST", "/v1/admin/gpu/stop-all", { reason, drain }); }
  resumeGpus() { return this.send<ControlResult>("POST", "/v1/admin/gpu/resume"); }
  /** Change a few values in the active configuration and save it as a new version. */
  async patchConfig(changes: [(string | number)[], unknown][], comment: string) {
    const src = await this.configSource();
    let doc = src.document;
    for (const [path, value] of changes) doc = setPath(doc, path, value);
    const v = await this.validateConfig({ document: doc });
    if (!v.ok) throw new Error(v.error ?? "the configuration doesn't validate");
    return this.saveConfig({ document: doc }, comment, src.version);
  }
  gpu() {
    return this.get<GpuReport>("/v1/reports/gpu");
  }

  async downloadCsv(range: Range, groupBy: string) {
    const q = new URLSearchParams({ start: range.start, end: range.end, group_by: groupBy });
    const blob = await (await this.request(`/v1/reports/summary.csv?${q}`)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `meritorder-${groupBy.replace(":", "-")}-${range.start}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }
}

// ------------------------------------------------------------------ formatting

export const usd = (v: number) => {
  if (v === 0) return "$0";
  const abs = Math.abs(v);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : abs >= 0.01 ? 3 : 4;
  return v.toLocaleString(undefined, { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
};
export const ms = (v: number | null | undefined) => (v == null ? "–" : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)} s`);
export const num = (v: number) => v.toLocaleString(undefined, { maximumFractionDigits: 0 });
export const compact = (v: number) => Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(v);
/** A size in bytes, in the largest unit that keeps it at least 1 (1,024 bytes a KB). */
export const bytes = (v: number) => {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return i === 0 ? `${v.toLocaleString()} bytes` : `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[i]}`;
};
/** One line about what a team keeps in stored mode: counts and sealed size, never content. */
export function storedLine(s: StoredSize | undefined): string {
  if (!s || (!s.responses && !s.conversations)) return "nothing stored";
  const parts = [`${num(s.responses)} response${s.responses === 1 ? "" : "s"}`];
  if (s.conversations) parts.push(`${num(s.conversations)} conversation${s.conversations === 1 ? "" : "s"} (${num(s.items)} items)`);
  return `${parts.join(", ")}, ${bytes(s.bytes)} sealed, for ${num(s.people)} ${s.people === 1 ? "person" : "people"}`;
}
export const pct = (v: number) => (Number.isFinite(v) ? `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%` : "–");

export const savingsRate = (r: Pick<Row, "baseline_cost" | "savings_total">) => (r.baseline_cost > 0 ? r.savings_total / r.baseline_cost : 0);
/** Spend against the cost without Merit Order, signed: "−95%" when it cost less, "+40%" when more, and "736× as much"
 * when far more (GPUs that ran for little traffic). null without a baseline. */
export const vsBaseline = (spend: number, baseline: number): string | null => {
  if (baseline <= 0) return null;
  const r = spend / baseline;
  if (r <= 1) return `−${pct(1 - r)}`;
  return r < 10 ? `+${pct(r - 1)}` : `${compact(r)}× as much`;
};
export const cacheHitRate = (r: Pick<Row, "input_tokens" | "cache_read_tokens" | "cache_write_tokens">) => {
  const prompt = r.input_tokens + r.cache_read_tokens + r.cache_write_tokens;
  return prompt > 0 ? r.cache_read_tokens / prompt : 0;
};

const iso = (d: Date) => d.toISOString().slice(0, 10);

export function presetRange(preset: string, now = new Date()): Range {
  const tomorrow = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  if (preset === "7d") return { start: iso(new Date(tomorrow.getTime() - 7 * 864e5)), end: iso(tomorrow) };
  if (preset === "30d") return { start: iso(new Date(tomorrow.getTime() - 30 * 864e5)), end: iso(tomorrow) };
  if (preset === "last-month") {
    return { start: iso(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1))), end: iso(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))) };
  }
  return { start: iso(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))), end: iso(tomorrow) };
}

/** Immutable set of a nested path; `undefined` deletes the key. */
export function setPath(doc: Doc, path: (string | number)[], value: unknown): Doc {
  const root = structuredClone(doc ?? {});
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let cur: any = root;
  for (const k of path.slice(0, -1)) {
    if (cur[k] == null || typeof cur[k] !== "object") cur[k] = {};
    cur = cur[k];
  }
  const last = path[path.length - 1];
  if (value === undefined) delete cur[last]; else cur[last] = value;
  return root;
}

export const ago = (unixSeconds: number | null | undefined, now = Date.now() / 1000) => {
  if (!unixSeconds) return "–";
  const s = Math.max(0, now - unixSeconds);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`;
};
