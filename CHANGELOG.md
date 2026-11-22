# Changelog

## Unreleased

- What agents bring, per team (`agent_tools`): the MCP servers, tools and skills Claude Code, Codex and pi offer the
  model, read from their requests. Shadow lists them on the Tools page and tags requests; enforce leaves out what the
  team doesn't allow, the same way every turn so prompt caches keep hitting (decision 76).
- MCP servers and each team's MCP access are set on the console: Configuration → MCP, and the team's settings.
- An MCP gateway: register MCP servers once (`mcp.servers`, with the gateway's credential or each person's own) and
  agents reach them at `/mcp/<server>` with their Merit Order key. Per-team server and tool allow-lists, tool
  definitions pinned with admin approval of changes, keys in tool arguments and results recorded or blocked, a
  per-person call limit, and a log of every call (never its arguments or results) on the console's new Tools page
  (decision 75). Needs migration 24 (`meritorder migrate`).
- Contributions now need a signed CLA (CLA.md); DCO sign-off is no longer required.
- People sign in to the console as themselves: an email address and password kept by Merit Order (invited with a
  one-time link from the People page, or `meritorder accounts create`), with a session in an HttpOnly cookie. API keys
  are for harnesses and services; one typed into the console opens a session tied to it, and the browser no longer
  stores it. Signing out ends the session on the gateway (decision 69). Needs migration 21 (`meritorder migrate`).
- The release workflow verifies each image it publishes (signature from this workflow on a tag, SBOM, provenance), and
  docs/07 says how to verify an image yourself.
- Budget step-down, per team (`budget_step_down`): from a share of any budget spent, new `auto` and tier tasks go to
  cheaper models instead of being refused at 100%, with an optional grace past the team's and people's budgets;
  tagged `budget_step_down` (decision 74).
- Two-factor sign-in for local accounts: authenticator-app codes and ten recovery codes, in the console and
  `meritorder login`; an admin can reset it for someone who lost their device. `MERITORDER_AUTH_KEY` seals the secrets
  (decision 73). Needs migration 23.
- Named tiers (`frontier`, `standard`, `cheap`, `embed`) a caller asks for by name; per-backend `provider`,
  `zero_retention` and `trains_on_data`; a caller's data policy per request (`X-MeritOrder-Allowed-Providers`,
  `X-MeritOrder-Zero-Retention: required`, refused with `403 data_policy`); `GET /v1/routes` lists each tier's candidates
  with providers, prices and data facts (decision 72).
- When a console session ends (expired, signed out elsewhere, disabled), the console goes back to its sign-in page and
  says why, instead of showing errors on every panel; signing in again returns to the same page.
- The console shows people by name or email address everywhere (header, reports, requests, keys, batches, GPUs,
  dispatch), never an identity provider's GUID; `GET /v1/reports/people` gives the names a viewer may see.
- Request screening, per team, in shadow mode (`screening.mode: shadow`): hidden Unicode, keys in prompts and tool
  calls, risky tool calls (download piped to a shell, uploads, DNS exfiltration, credential reads, config writes),
  image links with data, and untrusted web content marking a session; recorded as `screen` tags and
  `meritorder_screening_findings_total`, nothing blocked (decision 71).
- Console sign-in hardened after a security review: passwords only for accounts invited by email; a team lead's link
  can't lead to more than the user role; resets revoke `meritorder login` keys; parallel attempts count against the
  limit; link tokens out of URLs; `__Host-` session cookie over HTTPS. Needs migration 22.
- Claude Code's agent labels are recorded as tags (`cc_agent`, `cc_parent_agent`, `cc_agent_type`, `cc_request_class`),
  so reports show what each subagent and kind of request cost. A request carries up to 32 tags (was 10).
  `GET /v1/capabilities` tells a service built on the gateway (Blackstart) how usage, cost, attribution, cache
  affinity and embeddings work here (decision 70).
- The console signs in with your organization's SSO (Entra, Google, any OIDC provider) through a "Sign in with …"
  button: the authorization-code flow with PKCE. Register `<gateway>/auth/sso/callback` as a redirect URI; Google needs
  a Web application client (`MERITORDER_OIDC_BROWSER_CLIENT_ID`, `MERITORDER_OIDC_BROWSER_CLIENT_SECRET`).
  `meritorder login` asks for your email address and password on gateways without SSO.

## 0.0.1 (2026-11-15)

The first public release. Merit Order is an LLM gateway: your tools send requests to it as they would to OpenAI or
Anthropic, and it sends each one to the cheapest model and provider good enough for the work, keeps a ledger of what
everything cost and saved, and can run open models on your own GPUs when that's cheaper.

**This is pre-production.** Most of it is tested only against mocks: payloads written from each provider's docs, a
fake vLLM, and in-memory or CI-run Postgres and Redis. It has run for real against OpenRouter, Anthropic and OpenAI,
with Claude Code, in Docker Compose, with GPUs on a local kind cluster, and with one SkyPilot replica on a Lambda GPU.
Everything else (other providers, the rest of the GPU cycle on real clouds, Entra and Google sign-in, batch APIs,
bills) still needs a live check; `docs/README.md` lists what's been verified, and `ROADMAP.md` what 0.1.0 needs.
Licensed under the GNU AGPL-3.0-only.

### Using it

- **Speaks the APIs your tools already use:** OpenAI Chat Completions and Responses, Anthropic Messages, and
  embeddings, streamed or not. A request in one format can go to a provider that speaks another; the gateway
  translates both ways. Works unchanged with Claude Code, Codex CLI, pi, and the OpenAI and Anthropic SDKs
  (`meritorder setup claude-code`, `setup codex`, `setup pi`).
- **Ask for a model, or for `auto`.** A named model is never silently swapped (substituting a model that has nowhere
  to run is opt-in, and never to a weaker one). With `auto`, the gateway picks.
- **Backends:** Anthropic, Amazon Bedrock, Microsoft Foundry, Vertex AI, the Gemini API (its native API, which keeps
  thought signatures and cached-token counts), Azure OpenAI, OpenAI (through its Responses API, so GPT tool calls with
  reasoning work), OpenRouter, xAI, DeepSeek, Moonshot, Z.ai, MiniMax, Mistral, and any OpenAI-compatible server. Bedrock,
  Vertex and Azure work keyless with workload identity.
- **Models as of November 2026:** Claude Fable 5.2, Opus 5.5, Sonnet 5.5 and Haiku 5.5; GPT-6 Astra, GPT-6.1 Sol,
  GPT-6.1 Terra and GPT-6 Luna; Gemini 4 Carbon and Argon, 3.8 Flash and 3.5 Flash-Lite; Grok 4.7; DeepSeek V4.1
  Flash; Mistral's models; and open models (Qwen, Kimi and others) on OpenRouter or your own GPUs.
- **The Responses API in full:** stored mode, opt-in per team with a retention you set (`previous_response_id`,
  conversations, background responses; the gateway keeps the conversation sealed with AES-GCM, decision 51); the
  WebSocket transport at `/v1/responses` that Codex CLI chains over; `/v1/responses/compact`, whose summary any
  backend can read; and `/v1/responses/input_tokens` (decision 54).
- **Codex CLI's own tools on every backend:** `apply_patch` and Codex's other custom tools, namespaced tools (subagents,
  MCP servers), tool search and local shell reach Claude and Chat Completions backends as function tools, and come back
  in Codex's own shapes. Claude (not on Bedrock) gets live web search as its own server tool. Hosted tools with no
  equivalent are left out and named in `x-meritorder-dropped-tools` (decision 57).
- **Batches:** OpenAI's `/v1/files` and `/v1/batches` and Anthropic's Message Batches, for work that can wait a day
  at about half price. Each request is routed on its own to a provider's batch API (Anthropic, OpenAI, Azure, Gemini,
  Bedrock, Vertex AI, Mistral, Moonshot, xAI, OpenRouter), or runs in the headroom of GPUs you already pay for, at low
  priority (decision 49). Batch work never starts a GPU.

### How it routes

- **Levels from the work.** Each `auto` request gets a level, 1 to 5, for coding or general work, from its wording,
  its tools and the client's hint headers, never from how long the conversation is. Tools say the work is coding; the
  task's wording says how hard (decision 61). The rules are configurable (`routing.levels`), and every request records
  why it got its level and model.
- **Scores from public benchmarks.** Each model's 1-5 score is its chance of solving a task as hard as FrontierSWE,
  from Epoch AI's data, against a fixed bar: 5 is what the best model solved on 12 November 2026, so a new, stronger
  model doesn't lower everyone else's score (decision 58). The Artificial Analysis index is the alternative. Refreshed
  daily.
- **Cheapest for the rest of the session.** The router prices each route for the rest of the conversation, counting
  what it costs to rebuild a prompt cache before moving a session, and leaves a session where its cache is unless a
  move clearly pays (decision 6). It also counts what a 5-minute cache loses when people pause, from each team's own
  pauses (decision 60). Prices are live, from OpenRouter's catalog and LiteLLM's list, long-prompt tiers included.
- **The lowest expected total cost to finish the task, by default.** A model's expected cost is its try plus its
  chance of failing times what a failure would put at stake: a person's time, $1 for a failed level-5 agent task by
  default, a third as much for each level below, and less for a short request (decisions 62 and 67). Easy work goes to
  the cheapest model that meets its level; hard work pays for a better chance. On the routing simulator it spends 60%
  less per finished task than the plain threshold, and no more than the threshold on any kind of work.
- **Speed and failover.** Routes that cost about the same go to the faster one, measured from real replies
  (`balanced` and `speed` priorities pay more for speed). Failover catches errors before and after the first byte,
  skips a busy model on one backend for the provider's `Retry-After`, and alerts on outages.
- **Opt-in extras:** the plain threshold (the cheapest model at or above the level) or expected cost per solved
  task (decision 43) in place of the default; escalation to a stronger model inside a failing task; a learned router
  that corrects the rules' level from your own traffic's outcomes, run in shadow mode first (decision 44), with judged
  labels from a stronger model and a judge (decision 52); refusal retries and automatic reasoning effort for `auto`.
- **Your own traffic as the benchmark (opt-in).** Exploration sends a few of a team's new tasks one level up or down
  (decision 59). Coding outcome signals come from what the agent's tool results show: tests and builds, fix loops,
  corrections. Reports → Levels then says, per level, whether it's too high or too low for the work, and an admin
  can apply its recommendation.
- **Long conversations** that outgrow a model's window move to one with room, at the same level, even mid-task.

### Caching and sessions

- Prompt caching turned on where the client didn't ask for it, without rewriting history; Claude through OpenRouter
  is cached too. Claude's 1-hour cache from a session's second turn on the backends set to an hour, priced at its own
  write price (decision 53).
- Sessions keep their model within a task. Claude Code's subagents and side requests keep their own routing state,
  and its titles, summaries and compactions are capped at lower levels.
- Compression of tool results: rule-based by default, and opt-in model-based (LLMLingua-2) as its own
  `meritorder compressor` service. Each result is decided once and stored by content hash, so the prompt cache holds.
- An opt-in response cache.

### Your own GPUs

- A capacity controller starts and stops vLLM on cloud GPUs through SkyPilot (one plain cluster per replica, on
  every cloud where SkyPilot can run a Docker image and have the machine delete itself), or on your own Kubernetes
  cluster through KServe or llm-d, when demand makes them cheaper than the APIs; prices come from SkyPilot's catalog
  or the node types you list.
- A cloud replica stops itself when its controller stops renewing a lease, so a GPU can't keep billing after the
  controller is gone (decision 56).
- GPUs are priced by how busy they are, so spare capacity is used first, and a full GPU queues work only as a last
  resort. The gateway sends a session back to the replica that holds its KV cache.
- The console shows each deployment and replica, its cost and sessions, and lets admins stop, drain and pin them.
  Models → GPU break-even shows the traffic a replica needs to pay for itself.

### Spend, savings and reports

- Budgets per organization, GPU fleet, team, person and session; rate limits; a per-request cost limit. Evals and
  judged labels warn before they spend and have a spending cap.
- A ledger row per request, with what it would have cost on the team's baseline model, and the savings split by
  cause: routing, caching, self-hosting, batch discount, compression, response cache.
- Reports in the console and API; CSV, JSONL and Parquet export; alerts and weekly digests to a webhook; Prometheus
  metrics with a Grafana dashboard; OpenTelemetry traces. An optional ClickHouse copy of the ledger for very large
  deployments (decision 47).
- Bills checked against the ledger every 6 hours, by provider, model and day: Anthropic, OpenAI, AWS (Cost Explorer
  or CUR 2.0), GCP's billing export, Azure (Cost Management or its exports), and GPU time by tag.

### Evals

- `meritorder eval` and the console's Models → Evals page measure models on graded suites: `starter`, `frontier`
  (code checked by hidden tests in a sandbox, tool use, long documents, judged open-ended work), and `codebase` (26 real
  changes from this repository's history, graded like SWE-bench). Scores can be applied as a config version.

### Identity and access

- Microsoft Entra ID and Google Workspace sign-in (generic OIDC), exchanged for short-lived keys; groups (Entra's, or
  Google Groups through the Cloud Identity API) map to teams and roles; SCIM provisioning. Roles: user, team lead,
  finance, admin.

### Running it

- Docker Compose and a Helm chart (External Secrets, Reloader, workload identity). Multi-arch images on GHCR with an
  SBOM and provenance, signed with cosign: `ghcr.io/therick/meritorder:0.0.1`, `-gpu` and `-compressor`.
- The configuration lives in Postgres as versions, edited in the console and followed by every process; the YAML file
  is where it starts.
- **Python 3.14.** The images (`python:3.14-slim`, including SkyPilot's environment in `-gpu` and torch and LLMLingua
  in `-compressor`) and CI run on Python 3.14. Python 3.12 and 3.13 still work (`requires-python = ">=3.12"`), and a CI
  job runs the tests on 3.12.

### Changes in the last days before release

**APIs and Codex CLI**

- **The Responses API over WebSocket (decision 54).** Codex CLI talks to `wss://<gateway>/v1/responses`. Each
  turn goes through the same path as `POST /v1/responses`: auth, budgets, routing, the ledger. Without stored mode, a
  connection chains its turns from memory until it closes. Upstream calls stay HTTP.
- **Compaction and token counts.** `POST /v1/responses/compact` writes the summary with a routed model, and any
  backend can read it on the next turn. `POST /v1/responses/input_tokens` returns the gateway's estimate.
- **Codex CLI's sessions.** Its `session-id` header now groups requests into a session, and a subagent's
  `thread-id` gets its own routing state. `meritorder setup codex` writes a WebSocket provider into Codex's
  `config.toml`.
- **Codex CLI's tools (decision 57).** `apply_patch` (with its grammar in the description), Code Mode's `exec`,
  namespaced tools, tool search, local shell and `additional_tools` now reach Claude and Chat Completions backends as
  function tools. Their calls go back to Codex in its own shapes, and the history converts to the same bytes every
  turn, so the prompt cache holds. Claude (not Bedrock) gets live web search. Other hosted tools are left out, named in
  `x-meritorder-dropped-tools`, and logged. Fixed: in a Claude stream, a server tool's arguments were added to the
  first client tool call.

**Routing**

- **A fixed score bar (decision 58).** Scores used to be relative to the best model, so a stronger release
  lowered every other score and moved unchanged work to dearer models. Now `quality.scale: absolute` (the default)
  measures every model against a fixed anchor: what Claude Fable 5.2 solved on 12 November 2026. No score changed on
  the day of the switch. Configs that set `best` or `range` keep them.
- **Exploration (decision 59).** Opt-in per team, off by default, and only while the learned router is on.
  About 4% of a team's new tasks with no warm cache go one level up or down, at random. It never applies to a named model, a client's `X-Route-Floor`, batches,
  evals or pinned backends, never goes outside the team's models, and never goes up past 90% of a budget. A model that
  becomes the cheapest at a level, or newly qualifies for it, is tried first: on exploring teams it gets 20% of that
  level's task starts (a canary) until it has 30 outcomes there or 14 days have passed, and an alert says so.
- **Coding outcome signals.** From Claude Code's, Codex's and pi's tool calls and results: tests and builds
  after the last edit, fix loops, turns and tokens to the end of a task, and a correction in the next prompt. Only
  counts, flags and rule names are kept. They feed the learned router's labels.
- **Is each level right for the work?.** Reports → Levels shows, per task type and level, the success rate of
  each exploration arm, weighted by its chance, and says whether the level looks too high or too low, once each side
  has 200 task starts. *Apply* writes `routing.level_map` as a new config version; with
  `routing.benchmark.auto_apply`, the worker applies it when the learned router agrees. Alerts go to the webhook.
- **A first turn's cache estimate counts pauses (decision 60).** A later turn after a 5 to 60 minute pause
  rebuilds a 5-minute cache but not an hour's. The router counts each team's own pause share (one turn in ten until it
  has data), so a new Claude Haiku session now starts on the direct Claude backends at the default `cost` priority.
- **docs/00's numbers recomputed from the code.**

**Your own GPUs**

- **SkyPilot as plain clusters (decision 56).** One SkyPilot cluster per replica, named for the install and the
  deployment, in place of SkyServe and its always-on controller VM. Each replica is priced at its own catalog price.
- **A keyed gate.** A small gate in front of vLLM is the only thing on the open port. It lets in only the
  deployment's key, on every path, and knows only the key's hash, so the key never passes through SkyPilot.
- **A dead-man lease.** The controller renews each replica's lease every cycle. When it runs out (15 minutes),
  the gate stops vLLM, the task ends, and SkyPilot's autodown deletes the machine 5 minutes later.
- **A sweep.** Every cycle and at startup, this install's clusters that no deployment wants are torn down and
  logged.
- **Every cloud SkyPilot supports.** Audited from SkyPilot 0.14.2's source, cloud by cloud (docs/05, "Which
  clouds"). Replicas run on AWS, GCP, Azure, Lambda, RunPod, Nebius, Kubernetes (CoreWeave included), Vast, Fluidstack,
  Paperspace, DigitalOcean, Mithril, Seeweb and Slurm with Pyxis. Clouds where SkyPilot can't run a Docker image or
  make the machine delete itself are refused when the config loads, with the reason. Vast, Mithril and Slurm replicas
  are reached through an SSH tunnel. The config load warns about long-lived keys SkyPilot copies to replicas, and about
  spot machines that stop instead of being deleted (Azure, Nebius).
- **Fixes.** A failed start is torn down before its cooldown, and a failed scale keeps the previous count.
  Regions pair only with their own cloud, so the launch and the price agree. The Helm chart gives the controller a
  writable home and credential mounts.
- **`scripts/skypilot_check.py`** runs one real cycle on any cloud (`--cloud`, `--yes-spend`) and checks afterwards
  that nothing is left. It has started a real replica on Lambda; the full cycle hasn't passed yet.
- **Real NVIDIA drivers.** When a machine's driver is older than the image's CUDA, the replica's gate starts vLLM with
  NVIDIA's forward-compatibility libraries. vLLM's CUDA 12.9 image bundles a torchcodec built for CUDA 13 that stopped
  vLLM from starting; the gate removes an optional package that can't load before it starts vLLM. Both were found on
  Lambda. A replica's failure reason is kept, and a failed start ends its pin.

**Routing**

- **Coding levels from the task's wording (decision 61).** A coding agent sends dozens of tools with every request, so
  the tool count no longer makes a task level 4. "Write tests" or "fix the typo" is coding 2, ordinary work 3, hard
  wording (race condition, migration, security) one or two higher, and the hardest wording ("design a...",
  architecture) 5. A prompt saying the last answer was wrong raises the next task one level. Claude Code's system
  reminders and Codex's AGENTS.md aren't read as the task's wording. `agent_by_tools: true` brings back the old rule.
- **Expected total cost is the default (decision 62).** `auto` picks the model with the lowest expected cost to
  finish the task, a person's time included (`human_cost_usd`, per team on Controls). The chance comes from the
  benchmarks, and from a fitted layer once it has learned from your outcomes and proven itself on held-out tasks. An
  opt-in cascade moves a failing task to a stronger model when that's cheaper than carrying on; it's off by default.
- **What a failure puts at stake (decision 67).** Charging every failure the full person's cost sent trivial work to
  strong models: a one-line question went to GPT-6.1 Terra. Now the person's cost is what a failed level-5 task puts at
  stake, each level below puts a third as much, and a request under 8,000 tokens less; after a correction it counts
  as level 5. The tie band and the margin for moving a session are counted on money only. Every routing reason shows
  the stake.
- **A routing simulator.** `scripts/routing_sim.py` replays thousands of synthetic tasks through the real router and
  pricing, with no model calls, and writes a scorecard (`docs/routing-eval/`). CI runs 1,000 tasks and fails if any
  task class with 20 or more tasks spends more than 10% above the threshold per finished task.

**Your own GPUs: cold starts and safety**

- **Cold starts timed and paid back (decision 63).** The first real start on Lambda (one A10, gpt-oss-20b) took about
  11 minutes, billed and serving nothing. Each start is now timed phase by phase (machine, image, weights, vLLM) and
  shown on the GPUs page. A replica that isn't running yet must pay back its cold start within the hold time before
  demand starts it. An opt-in weights cache (`serve.weights_cache: volume`, Kubernetes and RunPod) keeps the model on a
  volume between starts; its price counts as GPU spend, and only `meritorder gpu volumes delete` removes it.
- **A GPU with no known price never starts.** A billed replica priced at 0 or unknown is never launched, and a
  Kubernetes deployment without a priced node type is refused when the config loads.
- **Kubernetes deployments are stopped and swept.** A deployment dropped or moved by a config change is stopped, and
  each cycle deletes this install's labelled resources that no deployment owns. A stop waits until the pods are gone.
- **Leases hold through outages.** The controller renews leases before it touches Postgres, renews what it can with
  Redis down, and times out a hung SkyPilot call, so a store or SkyPilot outage doesn't stop healthy replicas.
- **Other fixes.** Ending a pin no longer undoes a Stop all. The burn cap counts actual burn, not only the expected
  price. A vLLM that exits after it was ready is torn down at once and relaunched once, then backs off. The controller
  checks every cycle that it still holds its lock, and stops acting if it can't. Draining deployments don't launch.
  GPU time is counted over the whole gap after an outage, not capped at a day.

**Spend, savings and money**

- **GPUs count at what they cost to run (decision 64).** Spend totals, savings, the org budget, alerts, digests,
  reports, CSV, Prometheus and Grafana now include the GPU time the controller accrues, idle and starting included,
  and savings are counted after it, below zero when GPUs cost more than the API calls they replaced (a live day read
  "$0.0001 spent, −95%"; it is $1.62 spent, saved −$1.62). Spend shows as APIs · GPUs. Teams are charged their requests'
  shares of GPU time; the rest is the organization's, "GPU idle, unallocated".
- **One budget total.** Every budget surface (admission, Controls, attention, alerts, the eval budget check) counts the
  same total: the ledger, running batches' reservations (shown on their own) and, for the organization, GPU time.
- **An unknown price is refused, not served free (decision 66).** A request for a model whose routes have no known
  price gets a 503 that says so, and the worker raises an alert. GPU routes are still used: they're charged by GPU
  time.
- **Savings counted once.** A refused attempt no longer counts the baseline twice. Response-cache hits for evals and
  judged labels claim no savings. Compression savings use the long-prompt price when it applies. A GPU batch is
  compared with the provider's real batch price, and a baseline model with no price makes no savings claim.
- **Spend counters checked against the ledger.** Each worker cycle recomputes this month's spend per organization,
  team and person and corrects a Redis counter that stays off. Redis runs without persistence in Compose.
- **Data fixes.** Turning a team's stored mode off deletes its stored content. Rows sealed with a key that's gone no
  longer stall the reseal. A failed batch insert gives its reservation back. Exports are written to a temporary file
  and moved into place. Two people saving the config at once get a conflict instead of one losing the other's changes.

**API fidelity**

- **A broken-off stream is charged an estimate.** When a client disconnects mid-stream, the prompt and the output
  streamed so far are counted and the row is tagged `usage: estimated`, instead of being recorded as nearly free.
- **Files and images cross formats.** PDFs translate between Anthropic, Chat Completions and Responses; images and
  documents in an Anthropic tool result reach OpenAI-format models. File ids that can't cross are refused with a 400.
- **Forced tool choice reaches Claude** where the model allows it, and becomes `auto` with a system-prompt note where
  it doesn't. Refusals reach Responses and Anthropic clients as refusals. A stream that fails mid-way ends with the
  error instead of finishing normally. `count_tokens` answers 404 for an unknown model. Responses usage carries cache
  writes and reasoning tokens. Anthropic server tools dropped on an OpenAI-format backend are named in
  `x-meritorder-dropped-tools`. Cache markers stay on the parts they were on for Claude through OpenRouter.

**Security and robustness (decision 65)**

- **A person's record lowers a key's role, never raises it.** A key keeps the role it was issued with when its person
  is promoted, static keys follow the People page (disabled means refused), and disabled people can't sign in.
- **Tighter access.** Finance can't read the live request feed. Team leads revoke only their own keys and their team's
  user keys, and can't mint keys that never expire. CSV cells that start like a formula are escaped.
- **A config saved from the console reads only allowed secrets.** Key fields may name only provider key variables or
  `MERITORDER_BACKEND_*`, secret files only under `MERITORDER_SECRETS_DIR`, and `${NAME}` only the shipped config's
  names. `MERITORDER_CONFIG_SECRET_ENVS` and `MERITORDER_CONFIG_ENVS` widen the lists.
- **Inputs bounded.** `vllm_args` are validated and quoted, backend answers are capped at 512 MB, the replica gate
  limits waiting connections, SSH tunnels bind to one address, the Responses WebSocket queue is bounded, and batch
  result URLs no longer trust a client's forwarded headers. Bad routing and budget settings are refused when the config
  loads. CI adds `pip-audit`, and the Helm chart an optional NetworkPolicy.

**Console**

- A new look, and Reports → Requests pages through the whole ledger, filtered and sorted on the gateway, with a detail
  drawer and CSV.
- A console demo: the real console built with sample data and no gateway, hosted at https://dev.meritorder.dev.

**Docs and project files**

- **License and project files:** AGPL-3.0-only with a `NOTICE`; CONTRIBUTING (DCO sign-off), a code of conduct,
  SECURITY, issue and PR templates, and CODEOWNERS, so changes to `main` need a maintainer's review under the branch
  protection rules.
- **Docs checked against the code:** every doc checked and corrected, new diagrams, a README and docs
  index for first-time readers, a ROADMAP, and an example AWS account ID in place of a real one.
- **A feature sheet:** [docs/feature-sheet.md](docs/feature-sheet.md), a 5-minute read on how Merit Order saves
  money, how it judges and routes, and how it compares with nine other gateways and routers.

**Release preparation**

- Version 0.0.1 everywhere it's written; a default Helm install pulls the `0.0.1` images the release publishes.
  The release workflow's `-gpu` and `-compressor` images no longer push a plain `latest` over the main image's.
- AGPL-3.0-only recorded as decision 55.
- A "Source" link in the console's footer and `GET /about`, set by `console.source_url` (`MERITORDER_SOURCE_URL`), so
  anyone running a changed version can point it at their own source. The console's build writes its bundled
  packages' licenses to `/ui/third-party-notices.txt`. NOTICE says the `-compressor` image carries NVIDIA's CUDA
  libraries under NVIDIA's license.
- A new Claude session is priced as the gateway sends it: its first turn's cache writes at the 5-minute price, not
  the hour's. A new Claude Haiku session on the direct Claude backends looked 44% dearer than on OpenRouter; now the
  only difference is the hour's premium on later turns, about 5%.
- Batch work a GPU hands to a provider is cancelled at the batch's 24-hour deadline if it's still running, so the
  client's deadline holds; OpenRouter, which can't cancel, is no longer a GPU's fallback.
- A GPU without `max_concurrency` whose controller sample is stale still has its queue bounded.
- The level rules read the last messages a person wrote, skipping user messages made only of tool results (the
  learned router's feature version is now 3: retrain it).
- Clearer messages: the Postgres-only admin actions each say what needs it, and the parquet, compressor and tracing
  extras say how to install them from a clone. `meritorder eval` and the API default to the same suites.

### Known limitations

- Not checked against live accounts: providers other than OpenRouter, Anthropic and OpenAI; real cloud GPUs
  beyond one Lambda replica (no scaling, preemption, lease expiry or bill on a real cloud yet, and the per-cloud
  behaviour is read from SkyPilot's source); Entra,
  Google sign-in and SCIM; batch APIs; bill sources; the Responses API's stored mode, its WebSocket transport and
  Codex's tools with a real Codex CLI; Claude's web search. They're tested against payloads written from each
  provider's docs.
- What a failure puts at stake was tuned on the routing simulator, not measured on real failures.
- Cost-per-success chances come from benchmark scores, and the learned router's labels from outcome signals and
  judges; neither has been checked against real outcomes. How often the coding signals occur on real agents, and
  whether exploration's 4% reaches 200 task starts per side soon enough, aren't known yet. ClickHouse is measured at 60M rows, not hundreds of
  millions. Model-based compression is measured on the real models, but not for whether agents still succeed.
- The `codebase` eval suite is public now, so models trained after this release may have seen its answers.
- Not built: resuming background Responses streams, and batch embeddings on GPUs.
