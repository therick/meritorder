# Roadmap

There are no dates here. A version ships when its checks pass. What is built today is in the
[README](README.md#whats-in-the-box), and what has been checked live is in
[docs/README.md](docs/README.md#whats-verified-and-what-still-needs-live-accounts).

## 0.0.1 (now)

The first public version. Every feature in the README is built and tested in CI, but most of it is tested
only against mocks of each provider's API, written from the provider's docs. Checked live so far:

- OpenRouter, Anthropic and OpenAI: the smoke test (OpenAI's Responses API included), `auto` routing, prompt
  caching in real sessions, and OpenRouter's recorded cost against what it billed.
- Claude Code sessions through the gateway on Claude Sonnet 5.5, GPT-5.6 Terra and Kimi K3.1.
- The Docker Compose stack from a fresh clone, with the gpu and clickhouse profiles.
- GPUs on Kubernetes on a local kind cluster (KServe and llm-d's InferencePool) with a stand-in vLLM.
- One SkyPilot replica on Lambda (one A10, gpt-oss-20b, real vLLM): the cold start's phases, the NVIDIA driver's
  forward compatibility, and GPU time counted in spend.
- Model-based compression on the published LLMLingua-2 models, on a laptop's CPU and GPU.
- ClickHouse and Postgres reports at 20M rows, and ClickHouse at 60M, on a small machine.

Treat 0.0.1 as pre-production. Try it on the mock backends first, and set an organization budget
(on the console's Controls page, or `MERITORDER_ORG_BUDGET_USD` in `.env` for the starting value) before
you send it real traffic.

## 0.1.0: the live checks

0.1.0 comes once each check below has passed against a real account or cluster. These are the items in the
"Still needs a live account or cluster" column of [docs/README.md](docs/README.md#whats-verified-and-what-still-needs-live-accounts).

**Providers**
- [ ] `scripts/smoke_providers.py` (plain, streaming, tool calls, embeddings, both formats) on every provider
  not yet checked: Bedrock, Foundry, Claude on Vertex AI (quota), the Gemini API, xAI, DeepSeek, Moonshot, Z.ai and
  MiniMax. Passed on 19 Nov: Mistral, Azure OpenAI, Gemini on Vertex AI.
- [ ] Gemini with a key and on Vertex AI: `thinkingLevel`, thought signatures, mid-stream errors.
- [ ] Anthropic prompt-caching savings checked against an invoice.
- [ ] The Responses API's stored mode on OpenAI: an expired `previous_response_id`, earlier turns in
  `input_items`, deleting a response mid-chain.
- [ ] Claude's web search through the gateway against the real API, including an organization that hasn't enabled
  it in the Claude Console.

**Batch APIs and bills**
- [ ] Each provider's real batch API: result and error file shapes, Bedrock's S3 layout, Azure's v1 fields,
  Gemini's output lines, Moonshot's window, xAI's discounted models.
- [ ] Each provider's real cost data in Reports → Bills (Anthropic, OpenAI, AWS Cost Explorer and CUR 2.0,
  GCP, Azure Cost Management and its exports).

**GPUs**
- [ ] The full `scripts/skypilot_check.py` cycle on Lambda: Docker images, the gate and its tunnel, the lease and
  autodown from the head node, the firewall rule, vLLM serving gpt-oss-20b on an A10, and nothing left after teardown.
  A replica has started and served there; the rest of the cycle hasn't passed yet.
- [ ] Then a real SkyPilot cycle on another cloud: start, serve, scale, spot preemption, drain, and prices against
  the bill.
- [ ] Azure's self-delete path with a scoped service principal in place of the copied `az login` cache.
- [ ] Vast's ports: whether a replica can be reached without the SSH tunnel.
- [ ] The weights cache volume on RunPod and a Kubernetes PVC, and `runai_streamer` or `fastsafetensors` with
  gpt-oss's MXFP4 weights.
- [ ] Real vLLM on GPUs beyond one start: `/dev/shm`, shutdown, other models and quantizations; replica affinity and
  KV events on real replicas; vLLM preempting low-priority batch work for live requests.
- [ ] Kubernetes: the sweep on a real cluster (KServe's labels on its predictor), llm-d's Gateway and endpoint picker,
  the GPU device plugin and a cluster autoscaler adding a node, NetworkPolicies, and EKS, GKE and AKS exec-plugin
  sign-in.
- [ ] A Helm install on a cluster with External Secrets, Reloader and workload identity.

**Sign-in**
- [ ] Entra sign-in, group mapping and SCIM from a real tenant.
- [ ] Google Workspace sign-in and Google Groups (the Groups Reader service account, Business and Enterprise
  editions) from a real domain.

**Routing and quality, on real traffic**
- [ ] Token calibration against providers' real counts.
- [ ] Cost-per-success chances against real outcomes, and what a failure puts at stake (a third as much per level
  below 5, less under 8,000 tokens) against real failure costs.
- [ ] How close the estimate for a broken-off stream comes to the provider's bill.
- [ ] The learned router's outcome signals: how often each occurs, and how often it really means a bad answer.
- [ ] Coding outcome signals on real agents (Claude Code, Codex, pi): how often each occurs, and its failure rate.
- [ ] Exploration on real traffic: whether 4% of task starts reaches 200 per side soon enough, whether "no failure
  signal" tracks "good enough", and each team's real pause share.
- [ ] Judged labels: how well real judges agree with people, and what samples really cost.
- [ ] Measured quality scores (`meritorder eval`) on real models.
- [ ] Model-based compression: whether agents still succeed on compressed results, and the compressor's
  latency on an NVIDIA GPU and an x86 CPU.
- [ ] A real coding session through pi.
- [ ] A real Codex CLI session through the gateway over WebSocket, and `meritorder setup codex` against
  Codex's real config.
- [ ] A real Codex `apply_patch` session on Claude or Kimi: whether the models follow the grammar from the tool's
  description.

**Scale**
- [ ] Load against Redis and Postgres over a real network.
- [ ] ClickHouse at hundreds of millions of rows, and on ClickHouse Cloud and replicated clusters.

## Next: request security

Every layer is opt-in per team and starts in shadow mode: it reports what it would do before it acts. Built so far
(decision 71, team `screening.mode: shadow`): hidden Unicode, provider-prefixed keys in prompts and tool calls, risky
tool calls (download piped to a shell, uploads, DNS exfiltration, credential reads, config writes), image links with
data, and untrusted content (web fetches) marking a session, all recorded as tags and metrics. Next: blocking where
shadow mode shows few false alarms, then the rest of this list.

The MCP gateway (decision 75) applies the same rules to tool traffic: tool definitions pinned, with changes held for
an admin; keys in tool arguments and results recorded or blocked; per-team tool allow-lists. Next there: OAuth sign-in
for agents, per-person OAuth tokens for servers that act as each person, one endpoint joining several servers' tools,
and limits per tool.

1. **Secret scanning on prompts.** API keys, tokens, private keys and database URLs found by pattern and entropy
   rules, then redacted, blocked or alerted on, per team. Only the kind and position are recorded, never the value.
   Responses are scanned too.
2. **Input normalization.** Base64 and hex decoded; invisible Unicode (tag characters, zero-width, bidi overrides)
   and mixed-script lookalikes flagged. Its results feed the other checks.
3. **Prompt-injection signals in agent sessions.** Risky tool calls (sending data to a URL taken from untrusted
   content, `curl | sh`, reading credential files) that follow untrusted content such as a web fetch are flagged or
   blocked. Optional hosted injection detectors run only on untrusted tool results.
4. **Parallel moderation.** OpenAI's moderation endpoint is called at the same time as the model, so it adds next
   to no latency. A flagged request goes only to models with strong safety training instead of being refused, or is
   refused if the team chooses.
5. **Async audit.** A sampled review after the fact gives each team its rates of injection attempts and secret
   leaks, with nothing added to the request path.

## Later ideas

Mentioned in the docs as not built. None is scheduled.

- Resuming a background response's stream (`starting_after`) ([04](docs/04-caching-and-sessions.md)).
- An embedding feature source for the learned router ([decision 44](docs/09-decisions.md)).
- Batch embeddings on self-hosted GPUs, and Gemini batch embeddings on Vertex AI
  ([02](docs/02-request-flow.md)).
- Sealing held batch content again with a new key after a key rotation; today a grace period keeps content
  sealed before the rotation readable ([02](docs/02-request-flow.md)).
