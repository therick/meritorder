# Merit Order docs

Merit Order is a self-hosted LLM gateway. Its job is to **lower LLM spend for day-to-day use** (coding agents
such as pi and Claude Code, internal apps, SDKs) without users changing how they work.

> **Pre-production: version 0.0.1.** Most of Merit Order is tested only against mocks of each provider's
> API, written from the provider's docs. What has been checked live, and what hasn't, is in
> [What's verified](#whats-verified-and-what-still-needs-live-accounts) below: OpenRouter, Anthropic and
> OpenAI, Claude Code sessions, the Docker Compose stack, GPUs on a local kind cluster and one SkyPilot
> replica on a Lambda GPU have been; the other providers, the rest of the GPU cycle on real clouds, Entra and
> Google sign-in, batch APIs and bills have not. Version 0.1.0 comes
> once the checks in the right-hand column pass ([ROADMAP.md](../ROADMAP.md)).

The name comes from power grids. Operators dispatch generators in **merit order**: the cheapest marginal
cost first, with expensive "peaker" plants switched on only when demand needs them. Merit Order does the
same with tokens. When steady load justifies it, your own GPUs become the cheap baseload. API endpoints are
always available as the peakers that cover spikes and everything else. The router sends every session to
the cheapest capacity that meets its quality bar.

It does three things:

1. **Routes** each session to the cheapest model and backend that meets the task's quality bar.
2. **Runs open-weight models on your own GPUs** (SkyPilot, Kubernetes, or servers you run) when sustained
   load makes that cheaper than paying per token, and turns them off when it doesn't. API endpoints
   (Bedrock, Vertex AI, Foundry, ...) are always on, as the fallback and the overflow.
3. **Saves tokens** with provider prompt caching, cache-aware routing, careful compression and batch prices.

Around that: sign-in (Entra, Google Workspace, generic OIDC, SCIM), budgets and hard safeguards, a
**savings ledger** that shows what every request would have cost without the gateway, and a web console
to watch and control all of it: spend, savings, where every GPU runs and what it costs, and the switches
to stop it.

## Read in this order

| Doc | What you'll understand |
|---|---|
| [00 How it all works](00-how-it-all-works.md) | **Start here.** How your tools connect, how a request gets a level, how models get scores, why the cheapest qualifying route wins, how long conversations are handled, when your GPUs start and stop, and setting up Claude Code, with real numbers ([also as a standalone page](html/how-it-all-works.html)) |
| [01 Architecture](01-architecture.md) | The processes (gateway, worker, GPU controller, compressor), the stores, and what each one owns |
| [02 Request flow](02-request-flow.md) | What happens to one request, start to finish; batches |
| [03 Routing](03-routing.md) | How a model and backend are picked: levels, scores, marginal cost and switching cost, the learned router |
| [04 Caching and sessions](04-caching-and-sessions.md) | Who owns what state, why caches don't move between backends, the Responses API's stored mode |
| [05 GPU autoscaling](05-gpu-autoscaling.md) | When GPUs come up and down, and overflow to APIs |
| [06 Auth and spend](06-auth-and-spend.md) | Console sign-in (accounts and SSO), virtual keys, roles, budgets and safeguards |
| [07 Deployment](07-deployment.md) | Docker Compose or Kubernetes, configuration, secrets, scaling, supply-chain security |
| [08 Reporting](08-reporting.md) | Spend and savings by user, team, project and time; bills; exports; ClickHouse |
| [09 Decisions](09-decisions.md) | What was decided, why, and what's still open |
| [10 How it decides](10-how-it-decides.md) | **Every rule, as diagrams:** which model, GPU or API, retries, when GPUs start and stop, and what happens when things go wrong |
| [11 Evals](11-evals.md) | Model quality: scores from published benchmarks, running evals from the console or CLI, the task kinds, judges, the sandbox, and how results become scores |
| [Feature sheet](feature-sheet.md) | A 5-minute read: each way it saves money, with a worked example; how it judges and routes; how it compares with other gateways and routers; its limits |

Outside docs/: [README.md](../README.md) (quickstart), [ROADMAP.md](../ROADMAP.md),
[CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md) and [CHANGELOG.md](../CHANGELOG.md).

Every doc is also a web page in [`html/`](html/): one page per doc (`html/routing.html`, ...; this file is
`html/overview.html`), all of them on one page (`html/index.html`), and the start-here explainer with
diagrams (`html/how-it-all-works.html`, written by hand). `scripts/build_docs_html.py` rebuilds the generated
pages from these files. It needs the `markdown` package:
`uv run --with markdown python scripts/build_docs_html.py`.

## Six ideas behind the design

1. **Marginal cost, not list price.** A GPU that is already running is a sunk cost for that hour, so its
   spare capacity is close to free. Use it, even for a *better* model than the task needs.
2. **Moving a session costs money.** Prompt caches live on the backend. Switching backends means paying
   again for the whole context, so the router counts that before moving anything.
3. **The harness owns the conversation.** By default the gateway stores routing *metadata* (hashes,
   timestamps, which backend), never conversation content. The exceptions are opt-in or explicit: the
   response cache, the Responses API's stored mode, and batch files held (sealed) until a provider or GPU
   takes them.
4. **APIs are the floor, GPUs are an optimization.** Every model that runs on your GPUs should also have an
   API route, so GPUs never become a single point of failure. The console warns about any model that
   doesn't before anyone stops its GPUs.
5. **Clone and run.** `docker compose up` gives you a working gateway: on mock backends with no keys, on
   real ones with a single key. GPUs, sign-in and Kubernetes are opt-in layers.
6. **Every cost is visible next to the switch that controls it.** Anything that spends money (a team, a
   GPU, the whole organization) shows what it costs and can be capped or stopped from the console.

## Glossary

| Term | Meaning |
|---|---|
| **Harness** | The client driving the model: pi, Claude Code, Cursor, an SDK app |
| **Session** | One conversation or agent run; the unit of stickiness and caching |
| **Backend** | A configured place models run: `anthropic`, `bedrock`, `openrouter`, or your own GPUs (`gpu-qwen-coder`) |
| **Route** | One model on one backend, with its price; a model can have several |
| **Level** | What a request sent with `model: "auto"` needs, per kind of work, 1 to 5 (`coding:3`); set from its wording and tools, never its length. Also called the quality floor (`X-Route-Floor`) |
| **Score** | A model's quality for a kind of work, 1 to 5, from published benchmarks (Epoch AI by default) or measured with `meritorder eval` |
| **Headroom** | Spare capacity on a running GPU replica before it misses its latency target |
| **Shadow price** | The internal price for self-hosted tokens: about 0 when idle, rising toward the API price as the replica fills |
| **Rebuild cost** | The cost to process a session's context again on a backend whose cache is cold |
| **Counterfactual cost** | What a request would have cost on the baseline model (`baseline_model`, set for the organization or a team) at its API price |
| **Ledger** | One row per request: who, which model and backend, tokens, cost, counterfactual cost and savings by cause; never content |

## Status

| Area | Built | Not yet |
|---|---|---|
| Gateway | OpenAI Chat + Responses (HTTP and WebSocket, with `compact` and `input_tokens`), PDFs and tool-result images translated between formats, forced tool choice and refusals carried across, a broken-off stream charged at an estimate (`usage: estimated`), Codex CLI's own tools on every backend (custom tools such as `apply_patch`, namespaced tools, tool search, local shell; web search on Claude; tested against mocks), Anthropic Messages, embeddings; pass-through and translation; Anthropic, Bedrock, Foundry, Vertex (Claude, and Gemini through its native API or Vertex's OpenAI-compatible endpoint), the Gemini API, Azure OpenAI, OpenAI, xAI, OpenRouter, direct open-model vendors, any OpenAI-compatible server; OpenAI and Anthropic batch APIs routed to ten providers' batch APIs or to running GPUs' headroom; the Responses API's stored mode (opt-in per team: `previous_response_id`, conversations, background responses) | Resuming a background response's stream (`starting_after`) |
| Routing | Rule classifier setting levels from the work (tools say it's coding, the task's wording how hard; configurable `routing.levels`, a reason recorded per request), by default the lowest expected total cost to finish the task with what a failure puts at stake (a person's time scaled by level and size; decisions 62 and 67), never more than the threshold per task class on the routing simulator (CI runs 1,000 tasks and fails past 10% over), the threshold or cost per success per team, an opt-in cascade inside a failing task, marginal-cost router with rebuild cost, stickiness, window checks against each model's real window, token counts calibrated per model, unpriced routes refused, re-routing sessions that outgrow their model's context window, refusal fallback, opt-in substitution, automatic reasoning effort, live prices (OpenRouter, LiteLLM; every 15 minutes), quality scores from published benchmarks against a fixed bar (Epoch AI by default; daily) or measured (`meritorder eval`), opt-in learned router that corrects levels from outcomes (shadow mode, `meritorder router`), opt-in judged labels for it (a sample replayed and graded, with a required monthly cap), a first turn's cache estimate that counts each team's pauses | |
| Routing benchmark from your own traffic (exploration, coding outcomes, level report) | Built, tested against mocks: opt-in exploration per team (a few new tasks one level up or down, canaries for a new cheapest model), coding outcome signals from agents' tool results, Reports → Levels saying whether each level is too high or too low, applied as `routing.level_map` | |
| GPUs | Break-even controller; vLLM on plain SkyPilot clusters, one per replica, with a keyed gate, a dead-man lease and a sweep, on every cloud SkyPilot 0.14.2 supports with Docker images and autodown (AWS, GCP, Azure, Lambda, RunPod, Nebius, Kubernetes and CoreWeave, Vast, Fluidstack, Paperspace, DigitalOcean, Mithril, Seeweb, Slurm with Pyxis): read from SkyPilot's source, one replica run on Lambda; cold starts timed per phase and paid back in the break-even, an opt-in weights cache volume; a GPU with no known price never starts; leases renewed through Postgres, Redis and SkyPilot outages; Kubernetes (KServe or llm-d), stopped on a config change and swept when no deployment owns its resources, static servers, SkyPilot catalog (refreshed in the background, one lookup at a time) and node-type prices, batch work in running GPUs' headroom, replica affinity (KV events or an estimate), stop / drain / pin / stop-all, fleet safeguards, activity log | Batch embeddings on GPUs |
| Token savings | Automatic prompt caching, sticky sessions, deterministic compression, opt-in model-based compression (LLMLingua-2, its own service), batch prices, batch work on running GPUs, opt-in response cache | |
| Identity and spend | Keys, Entra/OIDC and Google Workspace login, Google Groups, SCIM, roles (a person's record lowers a key's role, never raises it), People page, team/user/org/GPU budgets (one total everywhere: ledger, batch reservations and, for the organization, GPU time), rate limits, per-session and per-request caps, spend counters checked against the ledger; console-saved config reads only allowed secrets, bounded inputs (`vllm_args`, backend answers, the gate's connections, WebSocket queues), CSV cells escaped | |
| Reporting | Ledger, savings by cause (self-hosting after GPU cost), spend as APIs and GPUs with GPU idle time charged to the organization, reports API, console, CSV/JSONL/Parquet export to S3, Prometheus with a Grafana dashboard, OpenTelemetry, alerts and weekly digests to a webhook; OpenRouter costs taken from what it billed; bills from Anthropic, OpenAI, AWS (Cost Explorer or CUR 2.0), GCP and Azure (Cost Management or exports) checked against the ledger (batch tier included); the worker's batch metrics; an optional ClickHouse copy of the ledger that long-range reports and tag breakdowns read | |

### What's verified, and what still needs live accounts

The left column is checked in CI on every change, or by hand where noted. The right column is what 0.1.0
waits for ([ROADMAP.md](../ROADMAP.md#010-the-live-checks)).

| Verified (CI on every change, or by hand where noted) | Still needs a live account or cluster |
|---|---|
| 2,319 tests (1,856 run anywhere; the other 463, mostly the real Redis and Postgres variants, run in CI; CI fails below 95% line coverage), each gateway, storage, and state test run against both in-memory and real Redis + Postgres | The smoke test on Bedrock, Foundry, Claude on Vertex, the Gemini API, xAI, DeepSeek, Moonshot, Z.ai and MiniMax: `scripts/smoke_providers.py` (plain, streaming, tool calls, embeddings, both formats) once keys and quotas are in place |
| Provider HTTP and streaming parsed from real wire bytes; retries, failover, circuit breaker | Anthropic prompt-caching savings checked against an invoice; a broken-off stream's estimate against the provider's bill |
| OpenRouter, by hand: the smoke test, `auto` routing, provider prompt caching in a real session, and recorded cost equal to what OpenRouter billed for every request, streamed or not | |
| OIDC verification with a real signing key (expired, forged, wrong audience or issuer, alg confusion), incl. tokens shaped like Google's (domain, unverified email, both issuer forms) | Entra sign-in, group mapping, and SCIM from a real tenant; Google Workspace sign-in and Google Groups (Groups Reader service account, Business vs Enterprise editions) from a real domain |
| Gemini's native API, the seven bill sources (AWS's CUR 2.0 and Azure's exports included), and vLLM KV events: requests and replies written from each provider's docs, served by mocks (KV events over a real ZeroMQ socket) | Gemini with a key and on Vertex (`thinkingLevel`, thought signatures, mid-stream errors); each provider's real cost data in Reports → Bills; replica affinity and KV events on real vLLM replicas |
| GPUs on Kubernetes, on a local kind cluster with KServe 0.21, llm-d's InferencePool and a stand-in vLLM: up, scale, status, requests, metrics, drain, down, five kinds of start failure, and the Role (`scripts/kind/`, by hand, 14 Nov) | Real vLLM on GPUs beyond one start (`/dev/shm`, shutdown, other models and quantizations); the weights cache volume on RunPod and a Kubernetes PVC; the Kubernetes sweep on a real cluster; llm-d's Gateway and endpoint picker; the GPU device plugin and a cluster autoscaler adding a node; NetworkPolicies; EKS, GKE and AKS exec-plugin sign-in |
| Token estimates and cost-per-success chances from published tokenizer behavior and benchmark scores; routing on the simulator (thousands of synthetic tasks through the real router and pricing; CI runs 1,000 and fails if a task class spends more than 10% above the threshold) | Token calibration against providers' real counts; cost-per-success chances against real outcomes; what a failure puts at stake against real failure costs |
| Batch APIs through the official OpenAI and Anthropic SDKs, against mocks of ten providers' batch APIs written from their references; batch work on GPUs against a fake vLLM | Each provider's real batch API: result and error file shapes, Bedrock's S3 layout, Azure's v1 fields, Gemini's output lines, Moonshot's window, xAI's discounted models; vLLM preempting low-priority batch work for live requests |
| The Responses API's stored mode through the official openai SDK, against mocks of Anthropic's Messages API and OpenAI's Responses API (chaining across providers, failover mid-chain, OpenAI continuing its own response) | OpenAI's real behaviour for an expired `previous_response_id`, earlier turns in `input_items`, and deleting a response mid-chain |
| The learned router on synthetic traffic: training recovers a known rule, shadow mode never changes a decision, every fallback; judged labels with a canned judge, learning a rule outcome signals alone can't | How often each outcome signal occurs on real traffic, and how often it really means a bad answer; real coding outcome signal rates on real agents, and exploration's recommendations on real traffic; how well real judges agree with people, and what samples really cost |
| Model-based compression on the published LLMLingua-2 models, by hand on a laptop's CPU and GPU: rates per kind of tool output, latency, memory; every identifier, path, number, quote and error line intact; cache stability across turns and two gateways | Whether agents still succeed on compressed results; compressor latency on an NVIDIA GPU and an x86 CPU |
| ClickHouse: every report gives the same numbers from ClickHouse and Postgres, against a real ClickHouse server in CI; copy speed, size on disk and report times measured at 20M rows in both stores and 60M in ClickHouse, on a small machine ([08](08-reporting.md)) | At hundreds of millions of rows; ClickHouse Cloud and replicated clusters |
| Console in a browser: every page and role, keys, budgets, config, sign-in with a password or a key, invite links (Playwright) | A real coding session through pi |
| Claude Code sessions through the gateway on Claude Sonnet 5.5, GPT-5.6 Terra, and Kimi K3.1: tools, streaming, cache reads (by hand) | A real Codex CLI session over the Responses API's WebSocket transport; a real Codex `apply_patch` session on Claude or Kimi |
| The Responses API over WebSocket, `compact` and `input_tokens`: against mocks and the official openai SDK's WebSocket client on localhost | OpenAI's own WebSocket behaviour |
| Codex CLI's tools against mocks: Codex-shaped requests, `apply_patch` round trips on Claude, a Chat Completions backend and a Responses backend, streamed and not; two WebSocket turns sending the same bytes upstream | Claude's web search live (and an org that hasn't enabled it); OpenAI taking `namespace` and `tool_search` tools from the gateway |
| Anthropic and OpenAI, by hand: the smoke test (incl. OpenAI's Responses API), gateway-added Claude caching (turns 2+ at a tenth of the price) | |
| Mistral, Gemini on Vertex AI (Vertex's OpenAI-compatible endpoint) and Azure OpenAI (a GPT-5 nano deployment): the smoke test, every check passing (19 Nov) | |
| The MCP gateway, by hand: a real Claude Code session (its model calls and its MCP tools both through the gateway) against DeepWiki's public server over Streamable HTTP event streams: tools pinned, calls logged, the session marked untrusted after a tool result (22 Nov) | Servers that need a credential (GitHub, Linear), `forward` with a personal token |
| Entra: the console's "Sign in with Microsoft" against a real tenant, a person created from the token (19 Nov) | `meritorder login` against a real tenant; group mapping and SCIM |
| Docker Compose stack: migrate, gateway, worker, a request end to end; a fresh clone's quickstart, the console, `setup`, and the gpu and clickhouse profiles (by hand, 13 Nov) | A real SkyPilot cycle (`scripts/skypilot_check.py`) on Lambda, then another cloud: start, serve, scale, spot preemption, drain, the lease and autodown, prices vs. the bill |
| Helm chart rendered with a chart-made Secret and with External Secrets (plus secret files and workload identity), and validated (kubeconform, ExternalSecret CRD schema); two secret sources at once are refused | Helm install on a cluster with External Secrets, Reloader, and workload identity |
| Grafana dashboard queries against real Prometheus and Grafana (by hand) | Measured quality scores (`meritorder eval`) on real models |
| SkyPilot replicas against a stand-in SDK; the gate and its lease for real against a stand-in vLLM; each cloud's behaviour read from SkyPilot 0.14.2's source. One real replica on Lambda (one A10, gpt-oss-20b, real vLLM, by hand): the cold start's phases, the NVIDIA driver's forward compatibility, the image's broken torchcodec, and GPU time counted in spend | Azure's self-delete path (the copied `az login` cache; then a scoped service principal); Vast's ports (always tunneled today) |
| Load: about 1,100 requests/s per process, 7,100 with 8 (by hand, mock backends); re-run 14 Nov in memory with each new feature on: short turns +26% CPU, agent turns −6% after a fix, learned router +35–40% ([07](07-deployment.md)) | Load against Redis and Postgres over a real network |
