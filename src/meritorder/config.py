"""Configuration: one YAML file plus environment variables for secrets.

Strings in the YAML may reference environment variables as ``${NAME}`` or
``${NAME:-default}``. Backends whose credentials or URLs are missing are
disabled automatically, so one config file works with whatever keys you have.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import os
import re
import shlex
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import sky_clouds
from .compression import CompressionConfig
from .compressor import CompressorConfig
from .response_cache import ResponseCacheConfig

log = logging.getLogger(__name__)

Role = Literal["user", "team_lead", "finance", "admin"]
Priority = Literal["cost", "balanced", "speed"]
PRIORITIES = ("cost", "balanced", "speed")
# How `auto` chooses among the models for a request's level: the cheapest whose score meets it, or the lowest expected
# cost per solved task (router.py, docs/10-how-it-decides.md).
ModelChoice = Literal["threshold", "cost_per_success", "expected_cost"]


class PriceTier(BaseModel):
    """Prices for requests whose prompt is longer than ``above`` tokens (e.g. Claude Haiku 5.5 above 100K,
    OpenAI's GPT-6.x above 272K). Dollars per million tokens."""

    above: int
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    cache_write_1h: float | None = None


class BatchPrice(BaseModel):
    """A provider's batch API prices for a model, dollars per million tokens. Cache prices left out scale with input."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)


class Price(BaseModel):
    """Dollars per million tokens."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float | None = None  # default: 0.1 x input
    cache_write: float | None = None  # default: input (no write premium)
    # Claude's 1-hour cache writes (a marker with "ttl": "1h"). Default: 2 x input, Anthropic's price on every platform.
    cache_write_1h: float | None = None
    # Server-side tools billed per call, on top of tokens: Anthropic web search is $10 per 1,000.
    # (Web fetch adds only tokens. Code-execution container time isn't modeled.)
    web_search_per_1k: float = 10.0
    tiers: list[PriceTier] = []  # long-prompt prices, which replace the ones above when they apply
    # Where the price came from: "openrouter" or "litellm" (live lists, refreshed by the worker), or "config"
    # (written into the configuration: an override such as a negotiated discount, or a price no list has).
    source: str | None = None
    # The provider's batch API price for the same model (LiteLLM's *_batches prices, or one written in the config). Unset:
    # the backend's published batch discount applies to every price above (batch_price).
    batch: BatchPrice | None = None

    def read(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.1

    def write(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input

    def write_1h(self) -> float:
        return self.cache_write_1h if self.cache_write_1h is not None else self.input * 2

    def for_batch(self, discount: float) -> "Price":
        """The price through the provider's batch API: its listed batch prices, or every price less ``discount`` (0.5 is
        half off). Prices the list leaves out (cache reads and writes, long-prompt tiers) move with the input price."""
        b = self.batch
        ratio_in = (b.input / self.input if self.input else 1 - discount) if b else 1 - discount
        ratio_out = (b.output / self.output if self.output else 1 - discount) if b else 1 - discount
        tiers = [t.model_copy(update={"input": t.input * ratio_in, "output": t.output * ratio_out,
                                      "cache_read": None if t.cache_read is None else t.cache_read * ratio_in,
                                      "cache_write": None if t.cache_write is None else t.cache_write * ratio_in,
                                      "cache_write_1h": None if t.cache_write_1h is None else t.cache_write_1h * ratio_in})
                 for t in self.tiers]
        read = b.cache_read if b and b.cache_read is not None else None if self.cache_read is None else self.cache_read * ratio_in
        write = b.cache_write if b and b.cache_write is not None else None if self.cache_write is None else self.cache_write * ratio_in
        write_1h = None if self.cache_write_1h is None else self.cache_write_1h * ratio_in
        return Price(input=b.input if b else self.input * ratio_in, output=b.output if b else self.output * ratio_out,
                     cache_read=read, cache_write=write, cache_write_1h=write_1h, web_search_per_1k=self.web_search_per_1k, tiers=tiers,
                     source=f"{self.source or 'config'} batch" + ("" if b else f" ({discount:.0%} off)"))

    def at(self, prompt_tokens: int) -> "Price":
        """The price for a request with this many prompt tokens (cached ones included), long-context tiers applied."""
        tier = max((t for t in self.tiers if prompt_tokens > t.above), key=lambda t: t.above, default=None)
        if tier is None:
            return self
        b = self.batch  # a listed batch price moves with the tier, as the provider's standard price does
        batch = None if b is None else BatchPrice(input=b.input * tier.input / self.input if self.input else b.input,
                                                  output=b.output * tier.output / self.output if self.output else b.output)
        return self.model_copy(update={"input": tier.input, "output": tier.output, "tiers": [], "batch": batch,
                                       "cache_read": tier.cache_read if tier.cache_read is not None else (
                                           None if self.cache_read is None else self.cache_read * tier.input / self.input if self.input else None),
                                       "cache_write": tier.cache_write if tier.cache_write is not None else (
                                           None if self.cache_write is None else self.cache_write * tier.input / self.input if self.input else None),
                                       "cache_write_1h": tier.cache_write_1h if tier.cache_write_1h is not None else (
                                           None if self.cache_write_1h is None else self.cache_write_1h * tier.input / self.input if self.input else None)})


_REQUEST_LOGGING_FLAGS = ("--enable-log-requests", "--enable-log-outputs")
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")  # reasoning effort levels, lowest first


def _logs_requests(token: str) -> bool:
    """A vLLM argument that turns on request logging: the flag, spelled with underscores, or a prefix argparse expands."""
    flag = token.split("=", 1)[0].replace("_", "-")
    return flag.startswith("--enable-log") and any(f.startswith(flag) for f in _REQUEST_LOGGING_FLAGS)


class BackendAuth(BaseModel):
    type: Literal["none", "api_key", "aws_sigv4", "azure_ad", "google"] = "none"
    header: str = "authorization"  # api_key: header to put the key in
    prefix: str | None = None  # api_key: defaults to "Bearer " for the authorization header
    value_env: str | None = None  # api_key: env var holding the key
    # api_key: file holding the key (e.g. a mounted Kubernetes Secret). Re-read when it changes, so a
    # rotated key takes effect without a restart. Falls back to value_env if the file is missing or empty.
    value_file: str | None = None
    value: str | None = None  # api_key: literal key (prefer value_env)
    service: str | None = None  # aws_sigv4
    region: str | None = None  # aws_sigv4
    profile: str | None = None  # aws_sigv4
    scope: str | None = None  # azure_ad

    def key(self) -> str | None:
        if self.value:
            return self.value
        if self.value_file:
            key = read_secret_file(self.value_file)
            if key:
                return key
        if self.value_env:
            return os.environ.get(self.value_env) or None
        return None

    def describe(self) -> str:
        """Where the key is expected, for "not set" messages."""
        where = [w for w in (self.value_file, self.value_env) if w]
        return " or ".join(where) or "api key"


_SECRET_FILES: dict[str, tuple[tuple[int, int], str | None]] = {}


def read_secret_file(path: str) -> str | None:
    """The file's contents, stripped; cached until its modification time or size changes.

    Kubernetes updates a mounted Secret by swapping a symlink, so ``stat`` (which follows it) sees the change.
    """
    try:
        st = os.stat(path)
    except OSError:
        _SECRET_FILES.pop(path, None)
        return None
    sig = (st.st_mtime_ns, st.st_size)
    cached = _SECRET_FILES.get(path)
    if cached and cached[0] == sig:
        return cached[1]
    try:
        value = Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        value = None
    _SECRET_FILES[path] = (sig, value)
    return value


class KubernetesServe(BaseModel):
    """provisioner=kubernetes: how one deployment runs on the cluster (docs/05-gpu-autoscaling.md#kubernetes)."""

    # kserve: an InferenceService (serving.kserve.io/v1beta1) whose predictor is the vLLM server. llm-d: a Deployment of
    # vLLM pods that an InferencePool you installed with llm-d's router selects; its endpoint picker spreads requests.
    api: Literal["kserve", "llm-d"] = "kserve"
    namespace: str | None = None  # default: kubernetes.namespace
    node_type: str | None = None  # a key of kubernetes.node_types: pods run on that node type and are priced at its rate
    node_selector: dict[str, str] = {}
    tolerations: list[dict[str, Any]] = []
    gpu_resource: str = "nvidia.com/gpu"  # amd.com/gpu for AMD
    cpu: str | None = None  # requests and limits for the vLLM container, e.g. "16"
    memory: str | None = None  # e.g. "200Gi"
    # The /v1 URL gateways and the controller use. kserve: blank takes the InferenceService's cluster address.
    # llm-d: required, the Gateway in front of the InferencePool (e.g. http://llm-d-gateway.llm-d.svc.cluster.local/v1).
    endpoint: str | None = None
    inference_pool: str | None = None  # llm-d: the InferencePool; its selector labels the pods and its target port is vLLM's
    hf_token_secret: str | None = None  # a Secret with an HF_TOKEN key, for gated models
    cache_pvc: str | None = None  # a PersistentVolumeClaim for the Hugging Face cache, so restarts don't download again
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}  # merged over the defaults (KServe: deploymentMode Standard, autoscalerClass none)
    termination_grace_seconds: int = Field(default=120, ge=0)  # a replica removed by a scale-down finishes its requests

    @model_validator(mode="after")
    def _llmd_needs_its_router(self) -> "KubernetesServe":
        if self.api == "llm-d" and not (self.inference_pool and self.endpoint):
            raise ValueError("api llm-d needs inference_pool (the InferencePool your llm-d router serves) and endpoint (its Gateway's /v1 URL)")
        return self


class NodeType(BaseModel):
    """What one node of a type costs, since a cluster has no price catalog: list price, reserved rate, or chargeback."""

    usd_per_hour: float = Field(ge=0)  # the whole node
    gpus: int = Field(default=8, ge=1)  # GPUs on one node: a replica pays its share
    spot_usd_per_hour: float | None = Field(default=None, ge=0)  # for nodes labeled as spot or preemptible


class KubernetesConfig(BaseModel):
    """The cluster that provisioner=kubernetes deployments run on."""

    # Default namespace for GPU deployments. Blank: MERITORDER_GPU_NAMESPACE, else the controller's own (in the cluster),
    # else the kubeconfig context's, else "default".
    namespace: str | None = None
    # Development only: a kubeconfig file and context. In the cluster the controller uses its service account.
    kubeconfig: str | None = None
    context: str | None = None
    node_types: dict[str, NodeType] = {}
    node_type_label: str = "node.kubernetes.io/instance-type"  # the node label node_type matches


class ServeConfig(BaseModel):
    """How to run a self-hosted model on GPUs (used by the capacity controller)."""

    provisioner: Literal["skypilot", "static", "kubernetes"] = "skypilot"
    model: str  # Merit Order model name whose hf_id gets served
    served_model_name: str | None = None  # name vLLM exposes; defaults to the route's model id
    accelerators: str = "H100:8"
    use_spot: bool = False
    clouds: list[str] = []  # empty: let SkyPilot choose the cheapest
    # Regions SkyPilot may use. Each one pairs only with the cloud it belongs to (catalog.region_clouds); empty: any.
    regions: list[str] = []
    image: str = "vllm/vllm-openai:v0.32.0-cu129"  # pin a version you have tested
    vllm_args: str = ""
    disk_size_gb: int = 1000
    min_replicas: int = 0  # >0 keeps capacity warm regardless of demand
    max_replicas: int = 4
    target_utilization: float = 0.7  # size replicas so they run at about this load
    scale_up_after_minutes: int = 10  # demand must justify GPUs this long before provisioning
    hold_minutes: int = 30  # keep GPUs at least this long once warm
    drain_minutes: int = 15  # how long a draining deployment keeps serving warm sessions
    provision_timeout_minutes: int = 45
    margin: float = 0.2  # break-even hysteresis
    static_url: str | None = None  # provisioner=static: the server's /v1 URL (or the load balancer in front of its replicas)
    # provisioner=static: each replica's own /v1 URL, when they can be reached one by one. Lets gateways send a session
    # back to the replica that holds its KV cache (replica_affinity); static_url, if set, is used when none answers.
    static_urls: list[str] = []
    location: str | None = None  # provisioner=static: where it runs, shown in the console (e.g. "on-prem, rack 4")
    # Send a session's turns to the replica that served its last turn, while it's ready and the cost of staying (its
    # load) doesn't outweigh rebuilding the cache on another. Needs replicas the gateway can address: static_urls,
    # SkyPilot's clusters, or Kubernetes pods (gateways in the cluster). Off: the load balancer (KServe's Service, or
    # yours) picks the replica for every request. Off by default behind llm-d, whose picker does it. SkyPilot needs it on.
    replica_affinity: bool = True
    # Follow vLLM's KV-cache events (ZeroMQ, `--kv-events-config`) from each replica, so the controller sees when a
    # session's cached blocks are evicted. Needs the `kv-events` extra (pyzmq, msgpack) where the controller runs, and
    # vLLM 0.30 or later for events tagged with the session. The events carry token ids, so the port must be reachable
    # only from the controller (a private network). Off: eviction is estimated from each replica's KV-cache metrics.
    kv_events: bool = False
    kv_events_port: int = 5557
    kubernetes: KubernetesServe | None = None  # provisioner=kubernetes (defaults: KServe in kubernetes.namespace)
    # provisioner=skypilot: each replica is its own SkyPilot cluster, and a watchdog on it (replica_gate.py) stops vLLM
    # when the controller hasn't renewed its lease for lease_minutes; SkyPilot then deletes the cluster after
    # autostop_minutes with nothing running. Both run on the machine itself, so a controller that died, a laptop that
    # sleeps or a lost ~/.sky can't leave a GPU billing for more than about lease_minutes + autostop_minutes.
    lease_minutes: int = Field(default=15, ge=3, le=24 * 60)
    autostop_minutes: int = Field(default=5, ge=1, le=24 * 60)
    port: int = Field(default=8000, ge=1, le=65535)  # skypilot: the one port each replica opens (the watchdog, in front of vLLM)
    # skypilot: reach a replica through an SSH tunnel from the controller instead of its open port. auto: only where the
    # port can't be reached (clouds where SkyPilot opens none, such as Vast and Mithril; Lambda's us-south-1, which has no
    # firewall rules; a security group that doesn't let the controller in). never: such clouds can't be reached at all.
    ssh_tunnel: Literal["auto", "always", "never"] = "auto"
    # How long a start takes, from the launch until vLLM answers, until the controller has measured it (the average of
    # this deployment's last starts replaces it). A start pays for this time without serving, so the break-even counts
    # it: a replica that isn't running yet must pay for it over hold_minutes (docs/05-gpu-autoscaling.md#the-break-even-test).
    cold_start_minutes: float = Field(default=12.0, ge=0)
    # How vLLM loads the weights (`--load-format`). auto: vLLM's default loader, which every model works with. Others
    # (runai_streamer, fastsafetensors) read safetensors faster where the image has them (docs/05, "Faster starts").
    load_format: str = "auto"
    # skypilot: keep the Hugging Face cache on a SkyPilot volume where the GPUs run, so a start after the first loads the
    # weights from it instead of downloading them. Volumes are on Kubernetes (a PVC) and RunPod (a network volume) only,
    # in SkyPilot 0.14.2; the volume bills every month until `meritorder gpu volumes delete` removes it.
    weights_cache: Literal["none", "volume"] = "none"
    # weights_cache: volume: where the volume lives (SkyPilot's infra: kubernetes, kubernetes/<context>, or
    # runpod/<region>/<data center>), how big it is, and what it costs a GB a month (None: RunPod's published price;
    # on Kubernetes, your storage class's, which isn't known unless set).
    weights_cache_infra: str | None = None
    weights_cache_gb: int | None = Field(default=None, ge=1)
    weights_cache_usd_per_gb_month: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _weights_cache_where_skypilot_has_volumes(self) -> "ServeConfig":
        """A weights cache volume exists only where SkyPilot has volumes, in one place, and a replica runs only where
        its volume is: SkyPilot refuses a task whose volume and resources disagree."""
        if self.weights_cache != "volume":
            return self
        why = sky_clouds.volume_problem(self.provisioner, self.weights_cache_infra, self.weights_cache_gb, self.clouds, self.regions)
        if why:
            raise ValueError(f"serve.weights_cache is volume, but {why}")
        return self

    @model_validator(mode="after")
    def _llmd_picks_its_replicas(self) -> "ServeConfig":
        """Behind llm-d, its endpoint picker already sends each request to the replica with the most of the prompt
        cached; the gateway picking pods itself would go around the Gateway. So affinity is off unless set."""
        if (self.provisioner == "kubernetes" and self.kubernetes and self.kubernetes.api == "llm-d"
                and "replica_affinity" not in self.model_fields_set):
            self.replica_affinity = False
        return self

    @field_validator("vllm_args")
    @classmethod
    def _no_request_logging(cls, v: str) -> str:
        """vLLM's request logging writes prompts and outputs to its log, which the replica keeps (`sky logs`, the gate's
        tail in the GPU activity log): it stays off. Checked on the arguments vLLM will get (shlex tokens, as both
        provisioners pass them), with underscores read as dashes and abbreviations caught, as vLLM's parser does."""
        try:
            tokens = shlex.split(v)
        except ValueError as e:
            raise ValueError(f"vllm_args isn't a valid argument list ({e}); quote values as a shell would")
        bad = [t for t in tokens if _logs_requests(t)]
        if bad:
            raise ValueError(f"vllm_args can't turn on request logging ({', '.join(bad)}): it would write prompts to the replica's log")
        return v

    @model_validator(mode="after")
    def _skypilot_replicas_are_addressed(self) -> "ServeConfig":
        """A SkyPilot deployment has no load balancer in front of its replicas: gateways pick one for every request, so
        they have to address them one by one."""
        if self.provisioner == "skypilot" and not self.replica_affinity:
            raise ValueError("SkyPilot deployments need replica_affinity: each replica is its own cluster with no load balancer in "
                             "front, so the gateway picks the replica (the least loaded, or the one holding the session's cache)")
        if self.provisioner == "skypilot" and self.kv_events and self.kv_events_port == self.port:
            raise ValueError("kv_events_port and port must differ")
        bad = sky_clouds.unusable(self.clouds) if self.provisioner == "skypilot" else []
        if bad:
            raise ValueError("serve.clouds names clouds a SkyPilot replica can't run on: " + "; ".join(bad))
        ssh_only = [c for c in self.clouds if sky_clouds.reach(c) == "tunnel"] if self.provisioner == "skypilot" else []
        if ssh_only and self.ssh_tunnel == "never":
            raise ValueError(f"ssh_tunnel is never, but SkyPilot opens no ports on {', '.join(ssh_only)}: its replicas are reached "
                             "only through the controller's SSH tunnel")
        return self

    @model_validator(mode="after")
    def _kserve_says_what_a_replica_uses(self) -> "ServeConfig":
        """KServe gives a predictor without cpu or memory its own defaults, as both requests and limits (1 CPU and 2 GiB in
        KServe 0.21, checked on kind), and vLLM can't load a model in that. A plain Deployment (llm-d) gets no defaults."""
        k = self.kubernetes or KubernetesServe()
        if self.provisioner == "kubernetes" and k.api == "kserve" and not (k.cpu and k.memory):
            raise ValueError("KServe deployments need cpu and memory under serve.kubernetes (what one replica may use on its node, "
                             "e.g. cpu: \"32\", memory: \"256Gi\"): without them KServe applies its own defaults, 1 CPU and 2 GiB, "
                             "too little for vLLM")
        return self


BatchApi = Literal["anthropic", "openai", "azure", "moonshot", "mistral", "openrouter", "xai", "gemini", "bedrock", "vertex", "gpu", "mock"]
# What each batch API takes off the standard price, as the providers publish it (November 2026): half, for every token
# type, on Anthropic, OpenAI, Azure OpenAI (global and data zone batch), Mistral, OpenRouter (for models with a batch
# variant), the Gemini API, Bedrock and Vertex AI; 40% on Moonshot (a batch costs 60%); 20% on xAI, for the Grok models it
# lists. Used when the live price list has no batch price for a route.
BATCH_DISCOUNT = 0.5
BATCH_DISCOUNTS = {"moonshot": 0.4, "xai": 0.2}


class BatchBackend(BaseModel):
    """A backend's batch API (docs/02-request-flow.md#batches-work-that-can-wait). Found from the backend's address unless ``api`` says.
    Bedrock and Vertex AI read a batch's input from a bucket and write its results there, so they need ``bucket``."""

    enabled: bool | None = None  # None: on wherever the backend has a batch API the gateway speaks (and its bucket is set)
    api: Literal["auto"] | BatchApi = "auto"
    # When the price list has no batch price. None: the provider's published discount (BATCH_DISCOUNTS, else BATCH_DISCOUNT).
    discount: float | None = Field(default=None, ge=0, lt=1)
    # bedrock: s3://bucket/prefix; vertex: gs://bucket/prefix. Input and results are written under it, one folder a job, and
    # deleted with the batch after batches.retention_days.
    bucket: str | None = None
    role_arn: str | None = None  # bedrock: the service role Bedrock assumes to read and write the bucket
    region: str | None = None  # bedrock: the control plane's region (default: the backend's); vertex: a region with batch
    # prediction (default: the backend's, or us-central1 for a global backend)
    # The most requests and bytes one provider job takes, and the fewest (Bedrock's minimum). None: the provider's own.
    max_requests: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    min_requests: int | None = Field(default=None, ge=1)

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, v: str | None) -> str | None:
        if v and not v.startswith(("s3://", "gs://")):
            raise ValueError("batch.bucket must be s3://bucket/prefix (Bedrock) or gs://bucket/prefix (Vertex AI)")
        return v.rstrip("/") if v else v


class BackendConfig(BaseModel):
    name: str = ""
    enabled: bool = True
    kind: Literal["api", "self_hosted", "mock"] = "api"
    format: Literal["anthropic", "openai"]
    base_url: str = ""
    auth: BackendAuth = BackendAuth()
    flavor: Literal["standard", "vertex"] = "standard"  # vertex: Claude, or Gemini with api: gemini, on Vertex AI
    project: str | None = None  # vertex
    region: str | None = None  # vertex
    anthropic_version: str | None = None  # force this anthropic-version header
    forward_anthropic_beta: bool = True
    # With forwarding on: None forwards every beta the client sends; a list forwards only those (prefix
    # match). Some betas change pricing (e.g. 1M-token context) in ways Merit Order's cost model doesn't track.
    anthropic_betas: list[str] | None = None
    supports_prompt_cache_key: bool = False
    # How this backend takes reasoning effort: anthropic -> output_config.effort, openai -> reasoning_effort,
    # openrouter -> reasoning.effort, none -> never sent. "auto" picks from the format and URL.
    effort_format: Literal["auto", "anthropic", "openai", "openrouter", "none"] = "auto"
    max_tokens_field: str = "max_tokens"
    # Where requests sent here go, for a caller's data policy (decision 72, GET /v1/routes): the provider's name
    # (default: from the address, e.g. anthropic, bedrock, azure_openai, vertex), and what your agreement with it says.
    # None means unknown, and a caller that requires zero retention won't be sent here.
    provider: str | None = None
    zero_retention: bool | None = None  # the provider keeps no prompts or outputs (your agreement or its ZDR terms say so)
    trains_on_data: bool | None = None  # the provider may train on what's sent
    # Where this backend's live prices come from: "openrouter" (OpenRouter's catalog), "litellm" (the price list
    # LiteLLM maintains, for direct provider APIs), or "none" (self-hosted, priced by the GPU hour). "auto" picks
    # from the URL. price_prefix is the provider's prefix in LiteLLM's list (e.g. "vertex_ai/"); None picks it.
    price_source: Literal["auto", "openrouter", "litellm", "none"] = "auto"
    price_prefix: str | None = None
    # OpenAI format only: which API to call. "responses" sends Chat Completions requests to /responses (needed for
    # function tools with reasoning on OpenAI's GPT-5.6 models); "gemini" to Gemini's native generateContent, on the
    # Gemini API (base_url) or, with flavor: vertex, on Vertex AI. Either way the replies are converted back.
    api: Literal["chat", "responses", "gemini"] = "chat"
    # api: responses only. For teams in the Responses API's stored mode, let the provider store their responses too
    # (`store: true` upstream), so a next turn that lands here again can chain with the provider's own response id and keep
    # its reasoning items and prompt cache, instead of the gateway sending the whole conversation again. The provider then
    # holds the content as well, for its own retention (OpenAI: 30 days), and the gateway deletes it there with its own
    # copy. Off: the gateway rebuilds every turn and the provider stores nothing (`store: false`).
    responses_store: bool = False
    # What a request cost: "reported" uses the amount the backend bills and returns with the response
    # (OpenRouter's usage.cost, which reflects whichever provider it picked); "catalog" prices the tokens
    # from the catalog. "auto" picks reported for OpenRouter and catalog otherwise.
    billed_cost: Literal["auto", "reported", "catalog"] = "auto"
    cache_ttl_seconds: int = 300
    timeout_seconds: float = 600.0
    # Streams only: give up on this backend (and try the next one) if it hasn't started answering by then: response
    # headers plus the first event (not the first token, so slow reasoning models are fine). None: wait timeout_seconds.
    # Default 120 s for APIs; off for self-hosted GPUs, where a request may wait in vLLM's queue on purpose.
    first_byte_timeout_seconds: float | None = Field(default=120.0, gt=0)

    def first_byte_timeout(self) -> float | None:
        if self.kind == "self_hosted" and "first_byte_timeout_seconds" not in self.model_fields_set:
            return None
        return self.first_byte_timeout_seconds
    extra_headers: dict[str, str] = {}
    # self-hosted economics
    gpu_cost_per_hour: float = 0.0  # per replica
    capacity_tokens_per_second: float = 2000.0  # per replica, measured at your latency target
    # Requests one replica runs at once (vLLM --max-num-seqs, or what its KV cache fits). When set, gateways
    # count their in-flight requests and treat the backend as saturated at this many per replica, within
    # seconds, instead of waiting for the controller's next metrics sample.
    max_concurrency: int | None = None
    # When this backend is saturated and no other route can take a request, the request still goes here and
    # waits in vLLM's queue, up to this many waiting per replica. Beyond that the gateway answers 503.
    max_waiting: int = 32
    serve: ServeConfig | None = None  # set: the controller provisions this backend on demand
    batch: BatchBackend = BatchBackend()  # the provider's batch API, for work that can wait (about half price)
    disabled_reason: str | None = None  # set at load time when credentials or URL are missing
    # mock
    mock_latency_ms: int = 15
    mock_min_cache_tokens: int = 0  # prompts shorter than this aren't cached (Anthropic: 1024 on most models)

    @field_validator("api")
    @classmethod
    def _gemini_takes_chat(cls, v: str, info) -> str:
        if v == "gemini" and info.data.get("format") != "openai":
            raise ValueError("api: gemini needs format: openai (the gateway sends it Chat Completions and converts them)")
        return v

    @model_validator(mode="after")
    def _store_needs_responses(self) -> "BackendConfig":
        if self.responses_store and self.api != "responses":
            raise ValueError("responses_store needs api: responses (only OpenAI's Responses API stores responses)")
        return self


class Route(BaseModel):
    backend: str
    model: str  # model id at the backend
    # None: the live price (prices.json, refreshed by the worker). Set: an override, e.g. a negotiated discount.
    price: Price | None = None
    # The model's name in the live price list, when the route's model id isn't it (an Azure deployment name).
    price_key: str | None = None
    # The model id the backend's batch API takes, when it isn't ``model``: an Azure global-batch deployment, a Bedrock model
    # id or inference profile for batch inference.
    batch_model: str | None = None

    @property
    def priced(self) -> bool:
        return self.price is not None

    def cost_price(self) -> Price:
        return self.price or Price()

    def batch_price(self, backend: "BackendConfig | None") -> Price:
        """What this route costs through its backend's batch API: the listed batch price, or the backend's discount."""
        return self.cost_price().for_batch(batch_discount(backend))


UNPRICED = "no price known for this route yet; set `price:` or wait for the price list"


def needs_price(route: Route, kind: str) -> bool:
    """A route that can't be used for want of a price: none is known, and its backend (``kind``) charges per token: an
    API, or a mock, which stands in for one (give it a ``price:``, as the shipped mocks have). Self-hosted routes are
    charged by GPU time, so their price is only a counterfactual. An unknown price must never look free."""
    return not route.priced and kind != "self_hosted"


def unpriced_message(model: str, routes: list[Route]) -> str:
    """The 503 for a request whose model has only routes with no known price."""
    return (f"'{model}' can't be served: {UNPRICED} ({', '.join(r.backend for r in routes)}: `meritorder prices refresh` "
            "fetches it). An unknown price is never served as free.")


class ModelConfig(BaseModel):
    name: str = ""
    kind: Literal["chat", "embedding"] = "chat"
    aliases: list[str] = []
    # 1-5 per task type (decimals allowed). Used with `quality.source: config`, or until a live source has been
    # fetched; once it has, a task type it doesn't cover scores 0 and `auto` won't pick the model for it.
    # quality_override always wins.
    quality: dict[str, float] = {}
    quality_override: dict[str, float] = {}
    benchmark_name: str | None = None  # the model's name in benchmark data, when its route ids don't match it
    quality_sources: dict[str, str] = {}  # set when loaded: where each score came from (epoch, artificial_analysis, config, override)
    hf_id: str | None = None  # Hugging Face repo, used when self-hosting the weights
    context_window: int | None = None  # tokens; filled from prices.json when not set
    # The most output (thinking plus reply) the model can write in one response; filled from prices.json when not set.
    # Requests that don't set max_tokens get this, so nothing is cut short by a limit the model doesn't have.
    max_output_tokens: int | None = None
    effort_levels: list[str] = []  # reasoning effort levels the model accepts; empty = don't set effort
    license: str | None = None
    # Set when loaded: per task type, the chance of solving work at each level (1-5), from the score in use and what the
    # quality source says a 5 solves (``apply_chances``). A task type missing here has no estimate, and routing by cost
    # per success uses the threshold for this model.
    chances: dict[str, dict[int, float]] = {}
    routes: list[Route]

    def matches(self, requested: str) -> bool:
        if requested == self.name:
            return True
        return any(fnmatch.fnmatchcase(requested, pattern) for pattern in self.aliases)

    def q(self, task: str) -> float:
        return self.quality.get(task, self.quality.get("general", 0))


class QualityConfig(BaseModel):
    """Where `auto` gets each model's quality scores (see quality_data.py)."""

    # epoch: Epoch AI's benchmark data; artificial_analysis: the AA Intelligence Index (ARTIFICIAL_ANALYSIS_API_KEY);
    # config: the scores written in each model's `quality` (or applied from eval runs).
    source: Literal["epoch", "artificial_analysis", "config"] = "epoch"
    # absolute (default): 5 x a model's chance on the reference work / ``anchor``, the chance that a 5 stands for, fixed,
    # so a level means the same work over time and a new, stronger model changes nobody else's score (decision 58).
    # best: 5 x a model's chance / the best configured model's (the best is 5; a stronger model lowers everyone else's).
    # range: the weakest configured model with data is 1, the best 5.
    scale: Literal["absolute", "best", "range"] = "absolute"
    # With scale absolute: per task type, the chance on the reference work that scores 5. The defaults are what the best
    # model solved when the scale was fixed (Claude Fable 5.2 on FrontierSWE-hard tasks, 12 November 2026), so scores
    # came out then as on the best-anchored scale. A model above it scores 5. A task type missing here uses the best's.
    anchor: dict[str, float] = {"coding": 0.6994, "general": 0.7685}
    refresh_hours: float = Field(default=24.0, gt=0)
    # Epoch: per task type, the benchmark whose difficulty scores are measured against (a model's chance of solving a
    # task that hard), and the benchmarks whose results show a model's strength at that kind of work.
    # FrontierSWE for both: hard enough that Fable 5.2's chance of solving a task that hard is 70% and Haiku 5.5's 41%
    # (its raw FrontierSWE score is 44%; the chance comes from the fitted ability), so the scale spreads
    # models out, while ordinary work (level 3) still goes to models like Sonnet. A harder reference spreads them more.
    reference: dict[str, str] = {"coding": "FrontierSWE", "general": "FrontierSWE"}
    evidence: dict[str, list[str]] = {"coding": ["FrontierSWE", "FrontierCode"], "general": []}
    # Older benchmarks of the kind, used only for a model with no evidence and no ECI (some open models): they predict
    # FrontierSWE/FrontierCode results no better than the ECI, so they never replace it.
    fallback: dict[str, list[str]] = {"coding": ["Terminal Bench", "GSO-Bench", "MirrorCode"], "general": []}
    # With source config: per task type, the best measured model's pass rate on hard eval tasks, which is what a measured 5
    # means. Written when an eval run's scores are applied as `quality`; it's what turns those scores into chances of
    # success for routing by cost per success. Unset: no chances, so the threshold decides.
    measured_best_rate: dict[str, float] = {}

    @field_validator("anchor")
    @classmethod
    def _anchors(cls, v: dict[str, float]) -> dict[str, float]:
        bad = {k: r for k, r in v.items() if not 0 < float(r) < 1}
        if bad:
            raise ValueError(f"quality.anchor is the chance on the reference work that scores 5, above 0 and below 1: {bad}")
        return v

    def five(self, task: str, top: float) -> float:
        """The chance on the reference work that scores 5 for this task type: the anchor on the absolute scale (where one
        is set), else the best model's, ``top``."""
        return self.anchor.get(task, top) if self.scale == "absolute" else top

    @field_validator("measured_best_rate")
    @classmethod
    def _rates(cls, v: dict[str, float]) -> dict[str, float]:
        bad = {k: r for k, r in v.items() if not 0 < float(r) <= 1}
        if bad:
            raise ValueError(f"measured best rates are pass rates, above 0 and at most 1: {bad}")
        return v


class RateLimits(BaseModel):
    """Per-minute limits. Tokens are estimated from the request (prompt) before it's sent."""

    user_rpm: int | None = Field(default=None, ge=0)
    user_tpm: int | None = Field(default=None, ge=0)
    team_rpm: int | None = Field(default=None, ge=0)
    team_tpm: int | None = Field(default=None, ge=0)


class StoredResponsesConfig(BaseModel):
    """A team's stored mode for OpenAI's Responses API (decision 51, docs/04-caching-and-sessions.md#stateful-apis): the
    gateway keeps each response's input and output, encrypted, so clients can chain turns with ``previous_response_id``
    or ``conversation`` and read responses back. This is where the gateway holds conversation content, so it's off by
    default and needs a retention the operator chose: there is no default that keeps content."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Days after a chain's or conversation's last turn before everything in it is deleted. Required when enabled.
    retention_days: float | None = Field(default=None, gt=0)
    # Roles that may read and chain the stored responses of people on this team besides their owner (admin: from any
    # team; the others: on this team). Empty: only the person who made a response, admins included.
    readers: list[Role] = []

    @field_validator("retention_days", mode="before")
    @classmethod
    def _unset_when_blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _retention_required(self) -> "StoredResponsesConfig":
        if self.enabled and self.retention_days is None:
            raise ValueError("stored_responses needs retention_days when enabled: how long the gateway keeps what it stores "
                             "after a conversation's last turn (there is no default)")
        return self


class JudgedLabelsConfig(BaseModel):
    """Judged labels for the learned router (judged.py, decision 52): a small sample of the team's requests is replayed
    on a stronger model, and a judge model from another family says whether the answer the request got was good enough,
    given the stronger one. Off by default. **Sampled prompts and answers are sent to the stronger model and the judge**,
    models the request's own routing didn't choose; nothing of them is stored but the judge's verdict. The replays and
    judging are requests through the gateway, spending against the team's budget, this cap and the organization's.
    Samples are taken only while the learned router is on (routing.learned.mode shadow or route)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # The share of the team's eligible task starts sampled. Small, so the cap is reached rarely, and it shows when it is.
    sample_rate: float = Field(default=0.01, gt=0, le=1)
    # The stronger model: a model's name, or "auto" for what `auto` picks at stronger_level for the request's task type.
    stronger: str = "auto"
    stronger_level: int = Field(default=5, ge=1, le=5)
    # Judges, tier-matched, one per family (the evals' default judges, evals/jobs.py DEFAULT_JUDGES). Each sample is
    # graded by the first whose family differs from both answers' models, else from the answer's own model's.
    judges: list[str] = ["claude-opus", "gpt-sol", "gemini-carbon"]
    # The most replays and judging may spend a month, in USD. Required to turn this on: there's no default.
    max_usd_month: float | None = Field(default=None, gt=0)
    # A sample whose replay can't start within this many seconds of its answer is dropped, prompt and all.
    window_seconds: float = Field(default=60.0, gt=0)
    # Claude Code request classes never sampled: side requests (titles, summaries) and compaction summaries.
    skip_classes: list[str] = ["auxiliary", "compaction"]
    # Requests the rules put below this level aren't sampled (a short question on its own is level 1).
    min_level: int = Field(default=2, ge=1, le=5)

    @field_validator("max_usd_month", mode="before")
    @classmethod
    def _unset_when_blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("skip_classes")
    @classmethod
    def _classes(cls, v: list[str]) -> list[str]:
        return [c.lower() for c in v]

    @model_validator(mode="after")
    def _needs_a_cap(self) -> "JudgedLabelsConfig":
        if self.enabled and self.max_usd_month is None:
            raise ValueError("judged labels need a monthly spending cap to be turned on (max_usd_month, in USD); there's no default")
        if self.enabled and not self.judges:
            raise ValueError("judged labels need at least one judge model (judges)")
        return self


class ExplorationConfig(BaseModel):
    """Exploration (decision 59, docs/03-routing.md#exploration): a small, random share of the team's `auto` task starts
    is routed one level down or up from the level the rules (or the learned router) gave, so the outcome report can say,
    without bias, whether a level is too high or too low for the work. Off by default: it knowingly sends some work to a
    weaker model, and some to a dearer one. Acts only while the learned router records outcomes (routing.learned.mode
    shadow or route). Never for a named model, a client's X-Route-Floor, Claude Code's capped side requests, or past the
    team's allowed models; never up when a budget is nearly spent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # The share of eligible task starts explored, half one level down and half one level up (all of it the one way at
    # level 1 or 5).
    share: float = Field(default=0.04, ge=0, le=0.5)
    # A canary (a model newly cheapest or newly eligible at a level) takes this share of the level's task starts, and
    # none of the others, until it has routing.benchmark.canary_outcomes recorded task starts there (or canary_days pass).
    canary_share: float = Field(default=0.2, ge=0, le=0.5)


class BudgetStepDown(BaseModel):
    """Cheaper models instead of a refusal as a budget runs out (decision 74). Once any budget that applies to a request
    (the organization's, the team's, the person's, the session's) is ``at`` spent, `auto` and tier requests start new
    tasks at ``level`` at most. A named model is served as asked. At 100% requests are refused as before, unless
    ``grace_share`` lets stepped-down requests go on past the team's and the person's budgets by that share of them (0.1:
    10% more); the organization's budget and the session limit stay hard."""

    at: float = Field(default=0.8, gt=0, le=1)
    level: int = Field(default=1, ge=1, le=5)
    grace_share: float = Field(default=0.0, ge=0, le=1)


class TierConfig(BaseModel):
    """A named route a caller asks for by name, as a model (decision 72): Blackstart's tiers (frontier, standard, cheap,
    embed) are route names, and the gateway decides what each sends to. A chat tier is `auto` at a fixed level (the
    task, coding or general, from the request unless set here); an embedding tier is one model, by default the cheapest
    priced embedding model."""

    kind: Literal["chat", "embedding"] = "chat"
    level: int | None = Field(default=None, ge=1, le=5)
    task: Literal["coding", "general"] | None = None
    model: str | None = None  # a fixed model instead (chat or embedding)

    @model_validator(mode="after")
    def _one_way(self):
        if self.kind == "chat" and (self.level is None) == (self.model is None):
            raise ValueError("a chat tier has either a level (1 to 5) or a model")
        return self


DEFAULT_TIERS = {"frontier": TierConfig(level=5), "standard": TierConfig(level=3), "cheap": TierConfig(level=1),
                 "embed": TierConfig(kind="embedding")}


class ScreeningConfig(BaseModel):
    """Request screening (decision 71): hidden Unicode, keys, and risky tool calls in what an agent's requests carry.
    "shadow" records what it finds (ledger tags, metrics) and changes nothing; "off" doesn't look."""

    mode: Literal["off", "shadow"] = "off"


class TeamMcp(BaseModel):
    """What a team may use through the MCP gateway (mcp.py, decision 75)."""

    servers: list[str] = ["*"]  # registered servers it may connect to: globs, with !exclusions
    # Tools it may see and call, as "server/tool" globs with !exclusions, e.g. ["*", "!github/delete_*"].
    tools: list[str] = ["*"]
    calls_per_minute: int | None = Field(default=None, ge=1)  # each person's tool calls, across servers; None: no limit
    # Keys (screening.SECRETS) in tool arguments and results. shadow records them; block refuses a call whose arguments
    # carry one and replaces one in a result with a marker; off doesn't look.
    secrets: Literal["off", "shadow", "block"] = "shadow"

    def allows_server(self, server: str) -> bool:
        return _allowed(server, self.servers)

    def allows_tool(self, server: str, tool: str) -> bool:
        return _allowed(server, self.servers) and _allowed(f"{server}/{tool}", self.tools)


class TeamConfig(BaseModel):
    name: str = ""
    mcp: TeamMcp = TeamMcp()
    compression: CompressionConfig = CompressionConfig()
    screening: ScreeningConfig = ScreeningConfig()
    budget_step_down: BudgetStepDown | None = None  # off unless set (decision 74)
    rate_limits: RateLimits = RateLimits()
    response_cache: ResponseCacheConfig = ResponseCacheConfig()
    stored_responses: StoredResponsesConfig = StoredResponsesConfig()  # the Responses API's stored mode (off by default)
    budget_usd_month: float | None = Field(default=None, ge=0)
    user_budget_usd_month: float | None = Field(default=None, ge=0)  # default monthly limit for each person; a person's own replaces it
    max_session_usd: float | None = Field(default=None, ge=0)  # stop a single session (e.g. an agent in a loop) at this spend
    refusal_fallback: bool = True  # `auto` requests a model refuses are retried on the next eligible model
    # A request naming a model that has nowhere to run (e.g. its GPUs are stopped) goes to a model at least as
    # good for the task instead of failing. Off by default: a different model can behave differently with tools.
    substitute_unavailable: bool = False
    priority: Priority | None = None  # cost, balanced or speed; unset uses routing.priority
    model_choice: ModelChoice | None = None  # threshold, cost_per_success or expected_cost; unset uses routing.model_choice
    min_success_chance: float | None = Field(default=None, gt=0, lt=1)  # unset uses routing.min_success_chance
    # What a failed level-5 attempt costs a person on this team, in USD (expected_cost, decisions 62 and 67); unset uses
    # routing.human_cost_usd.
    human_cost_usd: float | None = Field(default=None, ge=0)
    baseline_model: str | None = None
    allowed_models: list[str] = ["*"]
    allowed_backends: list[str] = ["*"]
    # Judged labels for the learned router: a sample replayed on a stronger model and graded by a judge. Off by default.
    judged_labels: JudgedLabelsConfig = JudgedLabelsConfig()
    # Exploration: a few task starts one level down or up, so the outcome report can say if a level is too high or low.
    exploration: ExplorationConfig = ExplorationConfig()

    def allows_model(self, model: str) -> bool:
        return _allowed(model, self.allowed_models)

    def allows_backend(self, backend: str) -> bool:
        return _allowed(backend, self.allowed_backends)


def _allowed(name: str, patterns: list[str]) -> bool:
    """Glob allow-list with ``!pattern`` exclusions, e.g. ["*", "!openrouter-free", "!deepseek"]."""
    if any(p.startswith("!") and fnmatch.fnmatchcase(name, p[1:]) for p in patterns):
        return False
    return any(not p.startswith("!") and fnmatch.fnmatchcase(name, p) for p in patterns)


class StaticKey(BaseModel):
    key: str | None = None
    key_env: str | None = None
    user: str
    team: str = "default"
    role: Role = "user"
    budget_usd_month: float | None = Field(default=None, ge=0)  # this person's monthly limit; overrides the team's per-person one

    def resolved(self) -> str | None:
        if self.key:
            return self.key
        if self.key_env:
            return os.environ.get(self.key_env) or None
        return None


GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")  # Google's ID tokens carry either form


class GoogleGroupsConfig(BaseModel):
    """Google sign-in only: read each person's Google Groups from the Cloud Identity Groups API and map them with
    team_from_groups and role_from_groups, keyed by group address (docs/06-auth-and-spend.md#google-groups). The gateway
    calls as a service account holding the Groups Reader admin role; no domain-wide delegation."""

    enabled: bool = True
    # The service account's key (the JSON file Google gives you): a file, re-read when it changes (a mounted Secret), or
    # an environment variable holding the JSON. With neither, the metadata server's identity is used (GKE Workload
    # Identity, Compute Engine, Cloud Run). Like a backend's value_file and value_env: the key itself is never stored.
    credentials_file: str | None = None
    credentials_env: str | None = None
    # Read only groups with Google's security label. Use it when the Groups Reader role is limited to security groups.
    security_groups_only: bool = False
    # Re-read the groups of people using the gateway this often, so a removal takes effect (seconds).
    refresh_seconds: int = Field(default=60, ge=1)
    # When Google can't be reached, keep each person's last known groups this long, then map by email and domain alone.
    keep_last_known_hours: float = Field(default=24, gt=0)

    @field_validator("credentials_file", "credentials_env", mode="before")
    @classmethod
    def _unset_when_blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class OIDCConfig(BaseModel):
    issuer: str
    client_id: str
    audience: str | None = None  # defaults to client_id
    # Google's device-login clients ("TVs and Limited Input devices") send their secret when they fetch the token, and
    # Google treats it as public for that kind of client. The gateway hands it to `meritorder login` (GET /auth/config),
    # so never put a confidential client's secret here.
    public_client_secret: str | None = None
    groups_claim: str = "groups"
    email_claim: str = "preferred_username"
    # Group -> team or role: Entra group object IDs (from the token), or with google_groups, group addresses. Team: the
    # first group in this list the person is in. Matching ignores case.
    team_from_groups: dict[str, str] = {}
    role_from_groups: dict[str, Role] = {}
    google_groups: GoogleGroupsConfig | None = None
    # For tokens without groups (Google's): an address ("alice@example.com") or a whole domain ("@example.com") -> team or
    # role. Applied only to an `email` the token marks verified (Entra's never are), so a made-up address never grants a role.
    team_from_emails: dict[str, str] = {}
    role_from_emails: dict[str, Role] = {}
    # Google Workspace domains whose accounts may sign in (the token's `hd` claim). Required with Google: its issuer is
    # shared by every Google account, so without it anyone with a Google account could get a key.
    hosted_domains: list[str] = []
    default_team: str = "default"
    key_ttl_days: int = 30
    scopes: list[str] = ["openid", "profile", "email", "offline_access"]
    # Signing in to the console in the browser (authorization code with PKCE, decision 69). The identity provider must
    # accept <gateway>/auth/sso/callback as a redirect URI: in Entra, add it under the app registration's Web platform
    # (with a client secret) or Mobile and desktop (none); Google needs its own "Web application" OAuth client and secret.
    browser: bool = True
    browser_client_id: str | None = None  # the client for the browser, when it isn't client_id (Google's always is another)
    browser_client_secret_env: str | None = None  # the environment variable holding that client's secret, if it has one
    redirect_uri: str | None = None  # defaults to the address the browser used + /auth/sso/callback

    @property
    def browser_secret(self) -> str | None:
        return os.environ.get(self.browser_client_secret_env) or None if self.browser_client_secret_env else None

    @property
    def browser_ready(self) -> bool:
        """Whether the console offers the SSO button: on, and (Google) with the Web client and its secret it needs."""
        return self.browser and (not self.is_google or bool(self.browser_client_id and self.browser_secret))

    @property
    def is_google(self) -> bool:
        return self.issuer.rstrip("/") in GOOGLE_ISSUERS

    @property
    def provider(self) -> Literal["google", "entra", "oidc"]:
        """For the console's wording: who people sign in with."""
        return "google" if self.is_google else "entra" if "login.microsoftonline.com" in self.issuer.lower() else "oidc"

    @field_validator("public_client_secret", "browser_client_id", "browser_client_secret_env", "redirect_uri", mode="before")
    @classmethod
    def _secret_unset_when_blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("hosted_domains", mode="before")
    @classmethod
    def _domain_list(cls, v):
        """``${MERITORDER_OIDC_HOSTED_DOMAINS}`` reads as a comma-separated list; empty means none."""
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, list | tuple):
            return [] if v is None else v  # anything else: pydantic says it isn't a list
        return [str(d).strip().lower().lstrip("@") for d in v if str(d).strip()]

    @property
    def reads_google_groups(self) -> bool:
        return bool(self.google_groups and self.google_groups.enabled)

    @model_validator(mode="after")
    def _google(self):
        if self.reads_google_groups:
            if not self.is_google:
                raise ValueError("google_groups works only with Google sign-in (issuer https://accounts.google.com)")
            odd = sorted(g for g in [*self.team_from_groups, *self.role_from_groups] if "@" not in g)
            if odd:
                raise ValueError(f"with google_groups, team_from_groups and role_from_groups are keyed by group address "
                                 f"(eng@example.com); not an address: {', '.join(odd)}")
        if not self.is_google:
            return self
        if not self.hosted_domains:
            raise ValueError("Google sign-in needs hosted_domains (your Google Workspace domains, or "
                             "MERITORDER_OIDC_HOSTED_DOMAINS): without it any Google account could sign in")
        if "scopes" not in self.model_fields_set:  # Google's device login allows only these; it refuses offline_access
            self.scopes = ["openid", "email", "profile"]
        return self


class LocalAccountsConfig(BaseModel):
    """People who sign in to the console with an email address and a password Merit Order keeps (as a scrypt hash), for
    teams without an identity provider. Invited by an admin or team lead with a one-time link (decision 69)."""

    enabled: bool = True
    min_password_length: int = Field(default=12, ge=8, le=128)
    invite_hours: int = Field(default=72, ge=1, le=24 * 30)


class AuthConfig(BaseModel):
    static_keys: list[StaticKey] = []
    oidc: OIDCConfig | None = None
    local: LocalAccountsConfig = LocalAccountsConfig()
    # How long a console sign-in lasts before the person signs in again. Signing out, being disabled or (for a session
    # opened with a key) the key's revocation ends it sooner.
    session_hours: int = Field(default=12, ge=1, le=24 * 30)


# Wording that gives a request its level (LevelRules). Python regular expressions, matched ignoring case.
CODING_WORDS = ["```", r"\bdef\s", r"\bclass\s", r"\bimport\s", r"\bfunction\b", r"\bconst\s", "=>", "Traceback", "stack trace",
                r"\.py\b", r"\.ts\b", r"\.go\b", r"\.rs\b", r"\.java\b", r"\bcompile", r"\brefactor", r"\bunit test", r"\bbug\b",
                r"\bregex\b", r"\bSQL\b"]
# Coding work a cheaper model does well (decision 61): tests, renames, typos, formatting and lint, docstrings and comments,
# explaining code, version bumps. Kinds of task, not how big the person says it is: "a quick fix" is often not.
SIMPLE_WORDS = [r"\bwrite (unit )?tests", r"\badd (unit )?tests", r"\b(add|write) a (unit )?test\b", r"\bfix (the )?failing tests?\b",
                r"\btest coverage", r"\brename", r"\btypo", r"\bspelling", r"\breformat", r"\bformatting", r"\blint", r"\bdocstring",
                r"\badd comments", r"\bexplain\b", r"\bwhat does this (code|function) do", r"\bunused import", r"\btype hints",
                r"\bbump\b", r"\bupgrade (the )?dependenc", r"\bchangelog", r"\breadme\b"]
# Hard coding work, a level up (hard_raise): multi-file changes, migrations, performance, concurrency, security, a
# cause nobody has found yet (symptoms under load, at random, on retry, when requests overlap).
HARD_WORDS = [r"\brace condition", r"\bdeadlock", r"\bsecurity", r"\bvulnerab", r"\bmigrat", r"\bperformance", r"\boptimi[sz]e",
              r"\bconcurren", r"\bdistributed", r"\bmulti-?file", r"\bcross-?cutting",
              r"\bacross (the |all |multiple |several )?(files|modules|services|packages|codebase|repo)",
              r"\bevery caller", r"\ball (the )?callers", r"\bcall sites", r"\bdependency injection", r"\bbehind an interface",
              r"\binto separate modules", r"\bupgrade (us |it |the \w+ )?from\b", r"\bwithout downtime", r"\bzero-?downtime",
              r"\bunder load", r"\bat random\b", r"\bon retry", r"\bidempoten", r"\bstale data",
              r"\b(two|concurrent|parallel|simultaneous) (requests|workers|writes|users)", r"\bintermittent", r"\bflaky",
              r"\bmemory leak", r"\bcan'?t figure out"]
# The hardest: designing a system, hard algorithms and proofs, refactoring a whole codebase.
HARDEST_WORDS = [r"\barchitect", r"\bdesign (a|the)", r"\bsystem design", r"\bprove\b", r"\bNP-hard", r"\bformal(ly)? verif",
                 r"\blarge-?scale", r"\b(whole|entire) (codebase|repo)", r"\bcodebase-?wide"]
ANALYSIS_WORDS = [r"\banaly[sz]e", r"\bcompare", r"\bstrategy", r"\bplan\b", r"\btrade-?offs?", r"\bevaluate", r"\breason"]


def check_patterns(patterns: list[str]) -> list[str]:
    """Each must be a regular expression that can't match empty text (it would match every request), and together
    they must compile as one (the classifier searches them in one pass)."""
    out = []
    for p in patterns:
        p = str(p)
        if not p.strip():
            raise ValueError("an empty pattern would match every request")
        try:
            rx = re.compile(p, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"{p!r} isn't a valid regular expression: {e}") from None
        if rx.search(""):
            raise ValueError(f"{p!r} matches empty text, so it would match every request")
        out.append(p)
    try:
        re.compile("|".join(f"(?:{p})" for p in out), re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"the patterns don't combine ({e}); leave out inline flags such as (?i): case is already ignored") from None
    return out


class LevelRules(BaseModel):
    """How `auto` gives a request its level (classifier.py). The level comes from the work: the request's wording, its
    tools, and the client's own floor or hint. How long the context is never raises it: a long conversation only rules
    out models whose context window can't hold it (router.fits). Patterns are matched against the last two messages a
    person wrote, without what the harness adds to them (Claude Code's system reminders, Codex's AGENTS.md and
    environment context).

    Tools say a request is coding; the task's wording sets its level (decision 61). Coding agents send dozens of tools
    on every request, so a tool count says which client it is, not how hard the task is. ``agent_by_tools`` puts a
    request with at least ``agent_tools`` tools at ``agent`` whatever its wording, as before decision 61."""

    model_config = ConfigDict(extra="forbid")  # a misspelt rule would otherwise be ignored without a word

    general: int = Field(default=2, ge=1, le=5)  # plain text that isn't coding
    short: int = Field(default=1, ge=1, le=5)  # ...when the whole request is under short_tokens: a question on its own
    short_tokens: int = Field(default=400, ge=0)  # 0: no short-question level
    analysis: int = Field(default=3, ge=1, le=5)  # ...with analysis wording
    analysis_words: list[str] = ANALYSIS_WORDS
    coding: int = Field(default=3, ge=1, le=5)  # any tools, or code-like text
    coding_words: list[str] = CODING_WORDS
    simple: int = Field(default=2, ge=1, le=5)  # ...with simple wording (tests, renames, typos, docstrings) and nothing harder
    simple_words: list[str] = SIMPLE_WORDS
    hard_raise: int = Field(default=1, ge=0, le=4)  # coding with hard wording goes this many levels up (to 5 at most)...
    hard_signals: int = Field(default=2, ge=1, le=4)  # ...for each distinct hard pattern it matches, counting up to this many
    hard_words: list[str] = HARD_WORDS
    hardest: int = Field(default=5, ge=1, le=5)  # coding with the hardest wording (architecture, algorithms): at least this
    hardest_words: list[str] = HARDEST_WORDS
    correction_raise: int = Field(default=1, ge=0, le=4)  # a new prompt saying the last answer was wrong: this many up
    agent_by_tools: bool = False  # True: at least agent_tools tools puts coding at `agent`, whatever the wording
    agent: int = Field(default=4, ge=1, le=5)
    agent_tools: int = Field(default=5, ge=1)  # also a tag for the learned router (`rule:agent`) either way

    @field_validator("analysis_words", "coding_words", "simple_words", "hard_words", "hardest_words")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        return check_patterns(v)


class LearnedRouterConfig(BaseModel):
    """The learned router (learned.py, decision 44): a request's level learned from this organization's own outcomes.
    Off by default. ``shadow`` records each request's features and outcomes and what the model would have chosen, and
    changes nothing; ``route`` uses its level when it's sure enough. The rules decide whenever it has no model, the model
    is stale or unsure, or it fails."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "shadow", "route"] = "off"
    # Below this chance that its level is right, the rules decide.
    min_confidence: float = Field(default=0.8, gt=0, lt=1)
    # A model trained longer ago than this is stale and the rules decide. 0 (or None): never stale by age.
    max_age_days: float | None = Field(default=30.0, ge=0)
    # The worker trains a new model this often (it's used only if it predicts held-out outcomes better than the rules).
    # None: only `meritorder router train` trains.
    retrain_hours: float | None = Field(default=None, gt=0)
    train_days: int = Field(default=30, ge=1)  # how far back training reads
    # ...and at most this many of those requests, the newest: training is pure Python, and memory and time grow with it.
    train_requests: int = Field(default=20_000, ge=100)
    keep_days: int = Field(default=90, ge=1)  # request features older than this are deleted

    @field_validator("max_age_days", "retrain_hours", mode="before")
    @classmethod
    def _unset_when_blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class BenchmarkConfig(BaseModel):
    """The organization's own traffic as the routing benchmark (decision 59): when the outcome report recommends a level
    change, when a canary has been tried enough, and whether recommendations are applied without an admin."""

    model_config = ConfigDict(extra="forbid")

    # A recommendation needs at least this many recorded task starts on each side compared (the level and the one below
    # or above it), and the difference has to be clear of chance (docs/03-routing.md#the-outcome-report).
    min_outcomes: int = Field(default=200, ge=20)
    # "Does as well": the explored level's success rate is at least this share of the served level's, at the lower end
    # of its 90% interval.
    as_well: float = Field(default=0.95, gt=0, le=1)
    # A canary serves only canary sessions until it has this many recorded task starts at its level, or for this long.
    canary_outcomes: int = Field(default=30, ge=1)
    canary_days: float = Field(default=14.0, gt=0)
    # Apply a recommendation to routing.level_map without an admin, when the learned router's model in use passed its
    # held-out check (it predicts outcomes better than the rules) and agrees with the direction. Off by default.
    auto_apply: bool = False


class SuccessConfig(BaseModel):
    """The success predictor's fitted layer (success.py, decision 62): a model of each answer's chance of success, fitted to
    this organization's outcomes on top of the benchmark prior. Off by default; ``shadow`` predicts and records next to the
    prior and changes nothing; ``route`` uses it once it has predicted held-out outcomes better than the prior. Needs the
    learned router on (shadow or route): that's what records the outcomes it learns from."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "shadow", "route"] = "off"


class CascadeConfig(BaseModel):
    """Escalation inside a task (expected_cost only, decision 62): when the in-task evidence (failing tests, fix loops, error
    results, a correction) makes continuing on the task's model cost more than moving up, the task moves to a stronger model
    at the next point where that's allowed."""

    model_config = ConfigDict(extra="forbid")

    # At most this many escalations per task. 0 (the default): never; the simulator found escalating cost more than it saved.
    max_escalations: int = Field(default=0, ge=0, le=2)
    # Escalate before the next human message only after this many failures in a row (failed test or build runs, error tool
    # results), and only when the expected costs say so. Otherwise the next human message is the earliest point.
    failures_now: int = Field(default=3, ge=1)


class RoutingConfig(BaseModel):
    auto_model: str = "auto"
    switch_margin_pct: float = 10.0
    switch_margin_usd: float = 0.01
    est_output_tokens: int = 600
    est_new_tokens_per_turn: int = 2000
    min_expected_turns: int = 3
    max_expected_turns: int = 30
    shadow_knee: float = 0.6  # self-hosted utilization where the shadow price starts rising
    shadow_floor: float = 0.02  # shadow price below the knee, as a fraction of API price
    max_attempts: int = Field(default=3, ge=1)  # backends tried per request; 0 would try none
    # Reasoning effort for `auto` requests by quality-floor level (levels not listed keep the model default).
    # Set for every level so a model runs the same way whichever it is (Opus 5.5 and Haiku 5.5 default to medium,
    # most others to high), and evals measure each level at the effort it's served with.
    effort_by_floor: dict[int, str] = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}

    @field_validator("effort_by_floor")
    @classmethod
    def _known_efforts(cls, v: dict[int, str]) -> dict[int, str]:
        """Levels 1 to 5, efforts the gateway knows: a typo would otherwise be dropped without a word (fit_effort)."""
        bad_levels = sorted(k for k in v if not 1 <= k <= 5)
        if bad_levels:
            raise ValueError(f"effort_by_floor levels are 1 to 5, not {', '.join(map(str, bad_levels))}")
        bad = sorted({e for e in v.values() if e not in EFFORT_ORDER})
        if bad:
            raise ValueError(f"effort_by_floor: unknown effort {', '.join(bad)}; use one of {', '.join(EFFORT_ORDER)}")
        return v
    # The highest level `auto` gives a Claude Code request of each class (the x-claude-code-request-class header).
    # Side requests (session titles, summaries, permission checks) are small jobs; a compaction summary needs a
    # capable model, not the conversation's top level. Classes not listed (main, subagent, workflow) aren't capped.
    request_class_levels: dict[str, int] = {"auxiliary": 1, "compaction": 3}
    # How a request gets its level from its wording and tools (never from its length).
    levels: LevelRules = LevelRules()
    # What decides among routes that cost about the same. Each priority is how much more (%) a route may cost
    # than the cheapest and still win by being faster: `cost` only breaks ties, `speed` pays up to double.
    # Teams (`priority`) and requests (X-MeritOrder-Priority) can choose another one.
    priority: Priority = "cost"
    priority_premium_pct: dict[str, float] = {"cost": 2.0, "balanced": 20.0, "speed": 100.0}
    # Every Nth new session can go to a route within that range whose speed is unknown or out of date, so
    # the router keeps measuring the alternatives. 0 never does.
    speed_check_every: int = Field(default=10, ge=0)
    # How `auto` chooses among models. threshold: the cheapest whose score meets the request's level. cost_per_success:
    # the lowest expected cost per solved task, from each model's chance of solving work at that level, so a cheaper
    # model a little below the level can win; a model with no chance estimate is judged by the threshold. expected_cost:
    # the lowest expected total cost to finish the task, a failure's escalation and the person's time included
    # (success.py), the default since the routing simulator scored it (decision 62); escalation inside a task (cascade) is
    # opt-in. Teams can choose their own.
    model_choice: ModelChoice = "expected_cost"
    # cost_per_success never picks a model whose chance at the level is below this. Without it, a nearly free model
    # that usually fails would look cheap per success. A model exactly at the level has the chance the best model has
    # on the reference work (70% on FrontierSWE in November 2026).
    min_success_chance: float = Field(default=0.6, gt=0, lt=1)
    # expected_cost: what a failed attempt at the top of the scale (a level-5 agent task) costs the person who asked, in
    # USD, on top of the tokens (noticing it, saying so, waiting for the redo). A request puts a share of it at stake
    # (success.stakes, decision 67). Default $1: one minute of a developer's time at $60 an hour. Teams can set their own.
    human_cost_usd: float = Field(default=1.0, ge=0)
    # The success predictor's fitted layer, and escalation inside a task (expected_cost).
    success: SuccessConfig = SuccessConfig()
    cascade: CascadeConfig = CascadeConfig()
    # The learned router: off, shadow (compare and record, change nothing) or route.
    learned: LearnedRouterConfig = LearnedRouterConfig()
    # Levels moved by the outcome report's recommendations (applied by an admin, or automatically): the work the rules
    # (or the learned router) put at "coding:3" is routed at the level given, e.g. {"coding:3": 2}. Applied once, after
    # Claude Code's caps; never to a client's X-Route-Floor.
    level_map: dict[str, int] = {}
    benchmark: BenchmarkConfig = BenchmarkConfig()

    @field_validator("level_map")
    @classmethod
    def _level_map(cls, v: dict[str, int]) -> dict[str, int]:
        bad = [k for k, n in v.items() if not re.fullmatch(r"(coding|general):[1-5]", k) or not 1 <= int(n) <= 5]
        if bad:
            raise ValueError(f"routing.level_map maps a task type and level (coding:3) to a level from 1 to 5: {bad}")
        return {k: int(n) for k, n in v.items()}

    @field_validator("request_class_levels")
    @classmethod
    def _class_levels(cls, v: dict[str, int]) -> dict[str, int]:
        bad = {k: n for k, n in v.items() if not 1 <= int(n) <= 5}
        if bad:
            raise ValueError(f"request class levels must be 1-5: {bad}")
        return {k.lower(): int(n) for k, n in v.items()}

    @field_validator("priority_premium_pct", mode="before")
    @classmethod
    def _all_priorities(cls, v):
        """A partial map (just `balanced: 30`) keeps the other defaults."""
        merged = {"cost": 2.0, "balanced": 20.0, "speed": 100.0, **(v or {})}
        unknown = set(merged) - set(PRIORITIES)
        if unknown:
            raise ValueError(f"unknown priorities {sorted(unknown)}; use {', '.join(PRIORITIES)}")
        if any(float(x) < 0 for x in merged.values()):
            raise ValueError("a priority's premium can't be negative")
        return merged


class Safeguards(BaseModel):
    """Organization-wide hard limits. They apply to everyone, including admin pins on GPUs."""

    org_budget_usd_month: float | None = Field(default=None, ge=0)  # ledger + unpaid GPU time (spend.py); refused once reached
    gpu_budget_usd_month: float | None = Field(default=None, ge=0)  # GPU time; GPUs drain and none start once reached
    max_gpu_usd_per_hour: float | None = Field(default=None, ge=0)  # whole fleet burn rate
    max_gpu_replicas: int | None = Field(default=None, ge=0)  # across all deployments
    max_request_usd: float | None = Field(default=None, gt=0)  # refuse a request whose worst case would cost more
    alert_at: list[float] = [0.8, 1.0]  # alert when org or GPU spend crosses these fractions of its budget

    @field_validator("org_budget_usd_month", "gpu_budget_usd_month", "max_gpu_usd_per_hour", "max_gpu_replicas", "max_request_usd",
                     mode="before")
    @classmethod
    def _unset_when_blank(cls, v):
        """``${MERITORDER_ORG_BUDGET_USD:-}`` with the variable unset or empty means no limit, not an error."""
        return None if isinstance(v, str) and not v.strip() else v


BillType = Literal["anthropic", "openai", "aws", "aws_cur", "gcp", "azure", "azure_export"]


class BillSource(BaseModel):
    """Where one provider's own cost data comes from, to check the ledger against (docs/08-reporting.md, "Checking
    costs against the bills"). The fields after ``ignore`` apply to some types only."""

    name: str = ""
    # aws (Cost Explorer) and aws_cur (Data Exports' CUR 2.0 files in S3) read the same AWS bill; azure (the Cost
    # Management Query API) and azure_export (scheduled exports in Blob Storage) the same Azure bill. Pick one of each
    # pair for a given account: the config refuses both reading the same backends or GPU tags, so no day counts twice.
    type: BillType
    enabled: bool = True
    # Ledger backends this bill covers. None: every API backend of the type (api.anthropic.com, api.openai.com, Bedrock,
    # Vertex AI and the Gemini API, *.azure.com), switched on or not, since the ledger can hold their earlier requests.
    backends: list[str] | None = None
    # None: the type's default credentials. anthropic: an Admin API key in ANTHROPIC_ADMIN_KEY (x-api-key);
    # openai: an Admin key in OPENAI_ADMIN_KEY; aws, aws_cur: the AWS credential chain (profile, role, IRSA; aws_cur signs
    # for S3 in auth.region, default us-east-1); gcp: Application Default Credentials; azure, azure_export:
    # DefaultAzureCredential; azure_export also takes a SAS token as an api_key (value_env or value_file).
    # value_env and value_file work as they do for backends.
    auth: BackendAuth | None = None
    # How many days the provider takes to finish a day's costs. Younger days are shown as partial and never alerted.
    # None: anthropic 1 (and its own data_refreshed_at), openai 1, aws and aws_cur 2, gcp 2, azure and azure_export 3.
    # The exports are also partial from the last day their files have data for.
    settle_days: int | None = Field(default=None, ge=0)
    # Bill lines (glob on the line's text, case-insensitive) -> Merit Order model name, for names the matcher misses.
    models: dict[str, str] = {}
    # Bill lines (globs) left out of the comparison: other workloads in the same account.
    ignore: list[str] = []
    workspace_ids: list[str] = []  # anthropic: only these workspaces
    api_key_ids: list[str] = []  # anthropic (apikey_...) and openai (key_...): only these keys
    project_ids: list[str] = []  # openai projects; gcp projects (default: the Vertex backends' projects)
    accounts: list[str] = []  # aws, aws_cur: only these linked (usage) accounts
    table: str | None = None  # gcp: the billing export table, project.dataset.gcp_billing_export_v1_XXXXXX
    query_project: str | None = None  # gcp: project that runs (and pays for) the query; default: the table's project
    location: str | None = None  # gcp: the dataset's location, e.g. US or EU
    partitioned: bool = True  # gcp: filter on _PARTITIONTIME to scan less (turn off for a view)
    # gcp: regular expression on service.description. aws_cur: on the product code, service code and product name
    # (default "(?i)bedrock", which takes Amazon Bedrock and the models sold through it on the Marketplace).
    services: str = "(?i)vertex ai|claude|anthropic|gemini|generative language"
    # azure: /subscriptions/<id>, or /subscriptions/<id>/resourceGroups/<name>. azure_export: only rows under it (default:
    # everything the export holds).
    scope: str | None = None
    resource_types: list[str] = ["microsoft.cognitiveservices/accounts"]  # azure, azure_export: Foundry and Azure OpenAI resources
    # azure: Cost (billing currency), CostUSD, or PreTaxCost (older account types). aws_cur: line_item_unblended_cost
    # (default) or line_item_net_unblended_cost. azure_export: default x_BilledCostInUsd (FOCUS) or costInUsd (actual cost),
    # else the billing-currency column.
    cost_column: str = "Cost"
    # aws_cur: the Data Export's S3 folder, s3://<bucket>/<prefix>/<export name> (it holds data/ and metadata/).
    # azure_export: the export's folder, https://<account>.blob.core.windows.net/<container>/<directory>/<export name>.
    export: str | None = None
    # aws, aws_cur, gcp, azure, azure_export: the tag (AWS, Azure) or label (GCP) SkyPilot puts on GPU instances, whose
    # value is the deployment's backend name. None: don't fetch GPU costs.
    gpu_tag: str | None = "meritorder-deployment"
    base_url: str | None = None  # anthropic, openai: the API's address, if not the public one


class BillingConfig(BaseModel):
    """Checking the ledger against providers' own cost data. The worker fetches each source every ``refresh_hours``."""

    sources: list[BillSource] = []
    refresh_hours: float = Field(default=6.0, gt=0)
    # How far back each fetch reads. Providers restate recent days (Anthropic until about the 7th of the next month).
    lookback_days: int = Field(default=40, ge=1, le=360)
    # Alert when a settled day's bill and ledger differ by both this much (percent of the ledger) and this many dollars.
    alert_pct: float = Field(default=5.0, ge=0)
    alert_min_usd: float = Field(default=1.0, ge=0)

    def source(self, name: str) -> BillSource | None:
        return next((s for s in self.sources if s.name == name), None)


class ClickHouseConfig(BaseModel):
    """A ClickHouse copy of the ledger for very large deployments (docs/08-reporting.md, "Storage and scale"). Postgres
    stays the record: the worker copies new ledger rows here, and long-range reports and tag breakdowns read them."""

    url: str  # ClickHouse's HTTP interface, e.g. http://clickhouse:8123 or https://<host>:8443 (ClickHouse Cloud)
    database: str = "meritorder"  # created if missing
    user: str = "default"
    # The password, as for backends: value_env (default MERITORDER_CLICKHOUSE_PASSWORD) or value_file (re-read when it
    # changes, so a rotated secret works without a restart). Neither set, or both empty: no password.
    auth: BackendAuth = BackendAuth(value_env="MERITORDER_CLICKHOUSE_PASSWORD")
    # Which reports read ClickHouse once it has their range: `large` (ranges over 7 days and tag breakdowns), `all` (every
    # summary, export, GPU what-if and bill comparison; budgets and the request list always read Postgres), or `none`
    # (copy only, e.g. while a backfill runs or for your own tools).
    reads: Literal["large", "all", "none"] = "large"
    ttl_days: int | None = Field(default=None, ge=1)  # drop ledger rows in ClickHouse after this many days; unset: keep them
    rollup_ttl_days: int | None = Field(default=None, ge=1)  # the same for the hourly rollup (it can outlive the rows)
    copy_seconds: float = Field(default=30.0, gt=0)  # how often the worker copies new rows
    batch_rows: int = Field(default=50_000, ge=1)  # rows per INSERT
    timeout_seconds: float = Field(default=60.0, gt=0)  # per query or insert

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"reporting.clickhouse.url must be ClickHouse's HTTP address (http:// or https://), got {v!r}")
        return v.rstrip("/")

    @field_validator("database")
    @classmethod
    def _database(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", v):
            raise ValueError(f"reporting.clickhouse.database must be a plain name (letters, digits, _), got {v!r}")
        return v


class ReportingConfig(BaseModel):
    clickhouse: ClickHouseConfig | None = None  # None (or an empty url): Postgres only


class BatchStorage(BaseModel):
    """Where the gateway holds what it must for batches (batches/content.py): uploads until their batch is handed on, and
    batch work for self-hosted GPUs. Sealed with the batch key wherever it is."""

    # None: Postgres. Else the operator's bucket: s3://bucket/prefix, gs://bucket/prefix, or Azure Blob Storage as
    # https://<account>.blob.core.windows.net/<container>/<prefix> (or az://<account>/<container>/<prefix>).
    bucket: str | None = None
    # Whose credentials sign the bucket's requests. None: the first enabled backend that signs in to that cloud (aws_sigv4,
    # google, azure_ad), else that cloud's default credentials (the AWS chain, Application Default Credentials, Entra ID's).
    backend: str | None = None
    region: str | None = None  # s3: the bucket's region (default: the backend's)

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not (v.startswith(("s3://", "gs://", "az://")) or re.match(r"^https://[a-z0-9]+\.blob\.core\.windows\.net/[^/]+", v)):
            raise ValueError("batches.storage.bucket must be s3://bucket/prefix, gs://bucket/prefix, or "
                             "https://<account>.blob.core.windows.net/<container>/<prefix> (az://<account>/<container>/<prefix>)")
        return v.rstrip("/")


class GpuBatchSettings(BaseModel):
    """Batch work on self-hosted GPUs (batches/gpu.py): requests that can wait run on a GPU deployment that's already
    running, in its headroom, at low priority, and go to a provider's batch API before the client's deadline if they haven't."""

    enabled: bool = True
    # Requests still waiting for a GPU this long after their batch was made go to their provider batch route (most
    # providers finish a batch in minutes to a few hours). The provider's own window starts then and would outlast the
    # client's 24 hours, so the worker cancels a handed-on job still running at the batch's deadline, and what it hadn't
    # run expires (batches/poller.py). A later hand-off leaves the provider less time.
    handoff_hours: float = Field(default=12.0, gt=0, lt=24)
    # vLLM priority for batch requests (lower runs first): after free upgrades (10), well after live traffic (0).
    priority: int = 20


class BatchesConfig(BaseModel):
    """Batches (docs/02-request-flow.md#batches-work-that-can-wait): what the gateway holds for them, for how long, and how often the worker
    asks the providers how they're doing."""

    enabled: bool = True
    # Days after a batch ends before the gateway deletes what it and the providers keep for it: results at the providers,
    # the objects in Bedrock's and Vertex AI's buckets, what it holds for GPU work. Also how long an uploaded input file no
    # batch used is kept.
    retention_days: float = Field(default=7.0, gt=0)
    # The key that seals what the gateway holds for batches (uploads until their batch is handed on, GPU work): any
    # string, the same on every gateway. Without one, only in-memory development storage takes files.
    key_env: str = "MERITORDER_BATCH_KEY"
    key_file: str | None = None
    # After rotating the key, the old one, so what it sealed stays readable for key_grace_days from when a gateway first
    # used the new one. None: the retention period plus two days (a batch's window and a provider's, then retention).
    previous_key_env: str = "MERITORDER_BATCH_KEY_PREVIOUS"
    previous_key_file: str | None = None
    key_grace_days: float | None = Field(default=None, gt=0)
    storage: BatchStorage = BatchStorage()
    gpu: GpuBatchSettings = GpuBatchSettings()
    # The first check on a provider job comes this long after it's submitted, and the wait doubles while nothing changes.
    poll_seconds: float = Field(default=60.0, gt=0)
    max_poll_seconds: float = Field(default=1800.0, gt=0)

    def key(self) -> str | None:
        if self.key_file:
            value = read_secret_file(self.key_file)
            if value:
                return value
        return os.environ.get(self.key_env) or None

    def previous_key(self) -> str | None:
        if self.previous_key_file:
            value = read_secret_file(self.previous_key_file)
            if value:
                return value
        return os.environ.get(self.previous_key_env) or None

    def grace_days(self) -> float:
        return self.key_grace_days if self.key_grace_days is not None else self.retention_days + 2


def batch_discount(b: BackendConfig | None) -> float:
    """What a backend's batch API takes off its standard price when the price list has no batch price for the route."""
    if b is None:
        return BATCH_DISCOUNT
    if b.batch.discount is not None:
        return b.batch.discount
    return BATCH_DISCOUNTS.get(batch_api(b)[0] or "", BATCH_DISCOUNT)


class ResponseStoreConfig(BaseModel):
    """The keys that encrypt what teams in stored mode keep (StoredResponsesConfig): any string, the same on every gateway
    and the worker. To rotate, put the new key in ``key_env`` and the old one in ``old_keys_env`` (comma-separated); the
    worker seals everything again with the new key, and the console says when nothing needs an old key any more."""

    key_env: str = "MERITORDER_RESPONSES_KEY"
    key_file: str | None = None  # re-read when it changes (a mounted Secret); wins over key_env
    old_keys_env: str = "MERITORDER_RESPONSES_OLD_KEYS"
    old_keys_file: str | None = None  # one key a line, or comma-separated
    # Background responses (`background: true`) left `queued` or `in_progress` this long by a gateway that stopped are
    # marked failed, so clients polling them get an answer.
    background_timeout_seconds: float = Field(default=3600.0, gt=0)
    # A background response sent with `store: false` is kept only this long after it finishes, so it can be polled (OpenAI
    # holds such responses for about ten minutes), and can't be chained from.
    background_unstored_seconds: float = Field(default=600.0, gt=0)

    def key(self) -> str | None:
        if self.key_file:
            value = read_secret_file(self.key_file)
            if value:
                return value
        return os.environ.get(self.key_env) or None

    def old_keys(self) -> list[str]:
        raw = (read_secret_file(self.old_keys_file) if self.old_keys_file else None) or os.environ.get(self.old_keys_env) or ""
        return [k.strip() for k in re.split(r"[,\n]", raw) if k.strip()]


def batch_api(b: BackendConfig) -> tuple[str | None, str]:
    """Which batch API a backend has (anthropic, openai, azure, moonshot, mistral, openrouter, xai, gemini, bedrock, vertex,
    gpu, mock), or None and why not."""
    s = b.batch
    if s.enabled is False:
        return None, "batch.enabled is false"
    if b.kind == "self_hosted":
        # Batch work runs in a GPU deployment's headroom (batches/gpu.py), counted in its slots.
        if b.format != "openai":
            return None, "self-hosted: batch work on GPUs is built for OpenAI-format servers (vLLM)"
        if not b.max_concurrency:
            return None, "self-hosted: set max_concurrency so batch work can be kept within the deployment's headroom"
        return "gpu", ""
    host = re.sub(r"^[a-z]+://", "", b.base_url.lower()).split("/", 1)[0].split(":", 1)[0]
    api = s.api if s.api != "auto" else None
    if api is None:
        if b.kind == "mock":
            api = "mock"
        elif b.format == "anthropic":
            api = ("vertex" if b.flavor == "vertex" else "anthropic" if host == "api.anthropic.com"
                   else "bedrock" if b.auth.type == "aws_sigv4" or host.endswith(".api.aws") or "bedrock" in host else None)
        elif b.api == "gemini":
            api = "vertex" if b.flavor == "vertex" else "gemini"
        elif host == "api.openai.com":
            api = "openai"
        elif host.endswith((".openai.azure.com", ".cognitiveservices.azure.com")):
            api = "azure"
        else:
            api = {"api.mistral.ai": "mistral", "api.moonshot.ai": "moonshot", "api.moonshot.cn": "moonshot", "api.x.ai": "xai",
                   "openrouter.ai": "openrouter"}.get(host)
    if api is None:
        return None, "no batch API the gateway speaks at this address"
    if api == "bedrock":
        if not (s.bucket or "").startswith("s3://") or not s.role_arn:
            return None, "Bedrock batch inference needs batch.bucket (s3://...) and batch.role_arn"
        if b.auth.type != "aws_sigv4":
            return None, "Bedrock batch inference needs AWS credentials (auth type aws_sigv4)"
    if api == "vertex":
        if not (s.bucket or "").startswith("gs://"):
            return None, "Vertex AI batch prediction needs batch.bucket (gs://...)"
        if b.format == "openai" and b.api != "gemini":
            return None, "Vertex AI batch prediction is built for Claude and the native Gemini API (api: gemini)"
    return api, ""


SOURCE_URL = "https://github.com/therick/meritorder"


class ConsoleConfig(BaseModel):
    """What the console shows about the software itself (GET /about, the console's footer)."""

    # Where people using this gateway can get its source. The AGPL (section 13) asks whoever runs a changed version for
    # others over a network to offer them that version's source: point this at your own repository then.
    source_url: str = SOURCE_URL

    @field_validator("source_url", mode="before")
    @classmethod
    def _url(cls, v):
        v = (v or "").strip() or SOURCE_URL
        if not re.match(r"https?://\S+$", v):
            raise ValueError(f"console.source_url must be an http(s) URL, not {v!r}")
        return v


class McpAuth(BaseModel):
    """How the gateway signs in to an MCP server. The caller's Merit Order key is never sent on.

    * none: no credential (an internal server on a private network)
    * api_key: the gateway's own credential for the server, from value_env or value_file (as a backend's)
    * forward: each caller's own credential for the server, which they send in ``forward_header`` (for servers that
      act as the person, e.g. a personal access token); the gateway sends it on in ``header``
    """

    type: Literal["none", "api_key", "forward"] = "none"
    header: str = "authorization"
    prefix: str | None = None  # defaults to "Bearer " for the authorization header
    value_env: str | None = None
    value_file: str | None = None
    value: str | None = None  # a literal (prefer value_env)
    forward_header: str = "x-meritorder-mcp-authorization"

    def key(self) -> str | None:
        if self.value:
            return self.value
        if self.value_file:
            key = read_secret_file(self.value_file)
            if key:
                return key
        if self.value_env:
            return os.environ.get(self.value_env) or None
        return None

    def with_prefix(self, value: str) -> str:
        prefix = self.prefix if self.prefix is not None else ("Bearer " if self.header.lower() == "authorization" else "")
        return value if prefix and value.lower().startswith(prefix.lower()) else prefix + value


class McpServerConfig(BaseModel):
    """An MCP server reached through the gateway at /mcp/<name> (Streamable HTTP)."""

    name: str
    url: str
    description: str = ""
    enabled: bool = True
    auth: McpAuth = McpAuth()
    headers: dict[str, str] = {}  # more fixed headers to send (not secrets: they're shown on the console)
    timeout_seconds: float = Field(default=300.0, gt=0)  # longest wait for a response, or between events of a stream
    # Tool definitions are pinned when first seen (a hash of each one's name, description and schemas). shadow records a
    # change; enforce also hides a changed tool, and refuses calls to it, until an admin approves the new definition.
    pinning: Literal["off", "shadow", "enforce"] = "shadow"
    # allow: a tool first seen is pinned as it is; hold: it's hidden until an admin approves it (with pinning enforce).
    new_tools: Literal["allow", "hold"] = "allow"
    # Its results are other people's words (web pages, tickets, email): request screening then marks an agent's session
    # as having read untrusted content when a result of one of its tools (mcp__<name>__*) comes back.
    untrusted_content: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", v):
            raise ValueError(f"MCP server name {v!r}: use letters, digits, '_', '-' or '.' (it's part of the URL)")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://[^/\s]+", v):
            raise ValueError(f"MCP server url {v!r} must be an http(s) URL")
        if re.match(r"^https?://[^/@]*:[^/@]*@", v):
            raise ValueError("an MCP server url can't hold a password; use auth")
        return v


class McpConfig(BaseModel):
    """The MCP gateway (mcp.py, decision 75): registered servers, and how long the tool-call log is kept."""

    servers: list[McpServerConfig] = []
    log_days: int = Field(default=90, ge=1)

    def server(self, name: str) -> McpServerConfig | None:
        return next((s for s in self.servers if s.name == name and s.enabled), None)


class Config(BaseModel):
    backends: list[BackendConfig]
    models: list[ModelConfig]
    teams: list[TeamConfig] = Field(default_factory=lambda: [TeamConfig(name="default")])
    tiers: dict[str, TierConfig] = Field(default_factory=lambda: dict(DEFAULT_TIERS))  # named routes (decision 72)
    baseline_model: str | None = None
    auth: AuthConfig = AuthConfig()
    routing: RoutingConfig = RoutingConfig()
    safeguards: Safeguards = Safeguards()
    quality: QualityConfig = QualityConfig()
    billing: BillingConfig = BillingConfig()
    reporting: ReportingConfig = ReportingConfig()
    kubernetes: KubernetesConfig = KubernetesConfig()  # the cluster provisioner=kubernetes deployments run on
    # The LLMLingua-2 service teams' model-based compression calls (compression.model); off while url is empty.
    compressor: CompressorConfig = CompressorConfig()
    batches: BatchesConfig = BatchesConfig()  # the providers' batch APIs: work that can wait, at about half price
    # The keys for what teams in the Responses API's stored mode keep (teams' stored_responses turns it on).
    response_store: ResponseStoreConfig = ResponseStoreConfig()
    mcp: McpConfig = McpConfig()  # the MCP gateway: tool servers agents reach through Merit Order
    console: ConsoleConfig = ConsoleConfig()  # the source link the console and GET /about show
    # Only for models whose output limit isn't known (max_output_tokens unset and not in prices.json): what to
    # send to Anthropic-format backends, which require a limit, when the client doesn't set one.
    default_max_tokens: int = Field(default=16000, ge=1)
    prices_file: str = "prices.json"
    # How often the worker refreshes prices from the live lists. Routing compares prices on every request, so
    # they must be current; a refresh that changes nothing changes nothing.
    prices_refresh_minutes: float = Field(default=15.0, gt=0)
    config_dir: str = "."
    # Set when loaded: per task type, the chance that a model scoring exactly a level has of solving work at that level
    # (``apply_chances``). Empty for a task type without chance estimates.
    threshold_chance: dict[str, float] = {}
    # Set when loaded, from the live catalog's exploration section (benchmark.py): models newly cheapest or newly eligible at
    # a level, each tried on a share of exploring teams' task starts there before it takes the rest.
    canaries: list[dict] = []

    @model_validator(mode="after")
    def _gpus_have_a_price(self) -> "Config":
        """A Kubernetes deployment needs a price: a node type with one, or gpu_cost_per_hour. Without it the controller
        would never start it (gpu.plan_fleet), since no GPU budget or fleet cap could hold it. SkyPilot's come from its
        catalog when the controller runs (gpu_cost_per_hour is only the fallback), so those are checked there."""
        for b in self.backends:
            s = b.serve
            if not b.enabled or b.kind != "self_hosted" or s is None or s.provisioner != "kubernetes" or b.gpu_cost_per_hour > 0:
                continue
            node = (s.kubernetes or KubernetesServe()).node_type
            t = self.kubernetes.node_types.get(node) if node else None
            if t is None or not (t.spot_usd_per_hour if s.use_spot and t.spot_usd_per_hour is not None else t.usd_per_hour) > 0:
                raise ValueError(f"backend {b.name} runs GPUs on Kubernetes with no price: set gpu_cost_per_hour (per replica), or "
                                 "serve.kubernetes.node_type to a kubernetes.node_types entry with usd_per_hour. A GPU with no price "
                                 "can't be held by the GPU budget or the fleet caps, so it would never start")
        return self

    def team(self, name: str) -> TeamConfig:
        for t in self.teams:
            if t.name == name:
                return t
        for t in self.teams:
            if t.name == "default":
                return t
        return TeamConfig(name=name)

    def tier(self, name: str | None) -> TierConfig | None:
        """A named route (decision 72), unless a model has that name (a model's own name always wins)."""
        if not name or name not in self.tiers or any(m.name == name or m.matches(name) for m in self.models):
            return None
        return self.tiers[name]

    def tier_model(self, name: str | None) -> ModelConfig | None:
        """The model a fixed tier (one with a model, or an embedding tier) sends to; None for a level tier."""
        t = self.tier(name)
        if t is None or t.level is not None:
            return None
        if t.model:
            return next((m for m in self.models if m.name == t.model or m.matches(t.model)), None)
        embeds = [m for m in self.models if m.kind == "embedding" and any(r.priced for r in m.routes)]
        return min(embeds, key=lambda m: min(r.cost_price().input for r in m.routes if r.priced), default=None)

    def model(self, name: str | None) -> ModelConfig | None:
        if not name:
            return None
        if (fixed := self.tier_model(name)) is not None:
            return fixed
        for m in self.models:
            if m.name == name:
                return m
        for m in self.models:
            if m.matches(name):
                return m
        return None

    def backend(self, name: str) -> BackendConfig | None:
        for b in self.backends:
            if b.name == name:
                return b
        return None


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def _named(section: Any) -> list[dict]:
    """Accept either a mapping of name -> settings or a list of settings with names."""
    if section is None:
        return []
    if isinstance(section, dict):
        return [{**(v or {}), "name": k} for k, v in section.items()]
    return list(section)


def default_config_path() -> Path:
    env = os.environ.get("MERITORDER_CONFIG")
    if env:
        return Path(env)
    for candidate in (Path("meritorder.yaml"), Path("config/meritorder.yaml")):
        if candidate.exists():
            return candidate
    return Path("config/meritorder.yaml")


def _load_raw(path: Path, seen: set[Path]) -> dict:
    """Read one YAML file plus anything it lists under ``include:`` (merged in order)."""
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)
    raw = yaml.safe_load(path.read_text()) or {}
    merged: dict = {}
    for inc in raw.pop("include", None) or []:
        sub = _load_raw((path.parent / inc), seen)
        for section in ("backends", "models", "teams"):
            if section in sub:
                merged.setdefault(section, {}).update(_as_mapping(sub[section]))
        for k, v in sub.items():
            if k not in ("backends", "models", "teams"):
                merged.setdefault(k, v)
    for section in ("backends", "models", "teams"):
        if section in raw:
            merged.setdefault(section, {}).update(_as_mapping(raw[section]))
    for k, v in raw.items():
        if k not in ("backends", "models", "teams"):
            merged[k] = v
    return merged


def _as_mapping(section: Any) -> dict:
    if isinstance(section, dict):
        return section
    return {item["name"]: item for item in section or []}


def parse_prices(data: dict) -> tuple[dict[str, Price], dict[str, int], set[str], dict[str, int]]:
    """prices.json: route prices, context windows, models that take a reasoning effort, and output limits."""
    prices = {k: Price.model_validate(v) for k, v in (data.get("prices") or {}).items()}
    return (prices, {k: int(v) for k, v in (data.get("context") or {}).items()}, set(data.get("reasoning") or []),
            {k: int(v) for k, v in (data.get("max_output") or {}).items()})


def load_catalog(path: Path) -> dict:
    """prices.json: the live catalog (prices, limits, and the `quality` section), as last refreshed."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_prices(path: Path) -> tuple[dict[str, Price], dict[str, int], set[str], dict[str, int]]:
    return parse_prices(load_catalog(path))


def apply_quality(cfg: "Config", catalog: dict) -> None:
    """Each model's quality: the live source's scores where it has them (if it's the configured source and scale);
    quality_override always wins. A task type the live source doesn't cover gets 0 ("unmeasured"): `auto` won't pick
    the model for it on a score nobody measured, though requests that name it still work. Without live data at all
    (never fetched), the configured scores stand. Records where each came from in ``quality_sources``."""
    section = live_section(cfg.quality, catalog.get("quality") or {})
    same = section.get("source") == cfg.quality.source and section.get("scale") == cfg.quality.scale
    live = (section.get("scores") or {}) if cfg.quality.source != "config" and same else {}
    mock = {b.name for b in cfg.backends if b.kind == "mock"}
    for m in cfg.models:
        m.quality_sources = {t: "config" for t in m.quality}
        if live and m.kind == "chat" and not all(r.backend in mock for r in m.routes):
            for task in list(m.quality):
                if task not in (live.get(m.name) or {}):
                    m.quality[task] = 0.0
                    m.quality_sources[task] = "unmeasured"
        for task, v in (live.get(m.name) or {}).items():
            m.quality[task] = float(v)
            m.quality_sources[task] = cfg.quality.source
        for task, v in m.quality_override.items():
            m.quality[task] = float(v)
            m.quality_sources[task] = "override"
    apply_chances(cfg, {**catalog, "quality": section})


def live_section(q: "QualityConfig", section: dict) -> dict:
    """The live quality section on the configured scale. Scores stored on another scale (or another anchor) are worked
    out again from the chances stored with them, so a change of scale takes effect at once, not at the next refresh."""
    if not section.get("measures") or section.get("source") != q.source or q.source == "config":
        return section
    anchor = dict(q.anchor) if q.scale == "absolute" else None
    if section.get("scale") == q.scale and (anchor is None or section.get("anchor") == anchor):
        return section
    from .quality_data import scores  # here: quality_data imports this module

    return {**section, "scale": q.scale, "anchor": anchor, "scores": scores(section["measures"], q.scale, q.anchor)}


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def apply_chances(cfg: "Config", catalog: dict) -> None:
    """Each model's chance of solving work at each level, for routing by cost per success (router.py).

    The quality scale turns a model's chance on the reference work into a score (`absolute`: 5 x its chance / the
    anchor; `best`: / the best model's), so a score read back gives the chance it stands for, and a level the chance of a
    model scoring exactly that level. Level-L work is taken to be as much easier than the reference as a model scoring L
    is weaker than a 5: a model exactly at a level solves work at that level as often as a 5 solves the reference work,
    and any other model is placed by its distance from the level on Epoch's logistic ability scale,

        chance = sigmoid(logit(chance of its score) - logit(chance of the level as a score) + logit(chance of a 5))

    so level 5 is the reference work itself. Reading the chance back from the score, not from the published chance,
    keeps the threshold's own rules: overrides count, and the floor of 1 still lets any model take level-1 work.

    What a 5 solves comes from the published chances the scores were worked out from (source epoch), or from
    ``quality.measured_best_rate``, the best measured model's pass rate on hard eval tasks (source config). The Artificial
    Analysis index isn't a chance, so it gives none. Only scores on that scale get chances: the source's own and
    overrides. A score written in the config while a live source is in use (a mock model, or nothing fetched yet) gets
    none, and routing judges that model by the threshold."""
    cfg.threshold_chance = {}
    for m in cfg.models:
        m.chances = {}
    q = cfg.quality
    section = catalog.get("quality") or {}
    scales: dict[str, Any] = {}
    if q.source == "epoch" and section.get("source") == "epoch" and section.get("scale") == q.scale:
        measures = section.get("measures") or {}
        for task in {t for per in measures.values() for t in per}:
            values = [per[task]["chance"] for per in measures.values() if task in per and per[task].get("chance") is not None]
            top, low = max(values, default=0.0), min(values, default=0.0)
            if top <= 0:
                continue
            if q.scale == "range" and top > low:
                scales[task] = lambda s, top=top, low=low: low + (s - 1) / 4 * (top - low)
            else:  # best, or absolute: there a 5 is the anchor, so what a score stands for doesn't move
                scales[task] = lambda s, five=q.five(task, top): s / 5 * five
        own = {"epoch", "override"}
    elif q.source == "config":
        scales = {task: (lambda s, best=best: s / 5 * best) for task, best in q.measured_best_rate.items()}
        own = {"config", "override"}
    else:
        return
    for task, chance_of in scales.items():
        top = _logit(chance_of(5))
        cfg.threshold_chance[task] = round(1 / (1 + math.exp(-top)), 4)
        for m in cfg.models:
            score = m.q(task)
            source = m.quality_sources.get(task, m.quality_sources.get("general"))
            if m.kind != "chat" or score <= 0 or source not in own:
                continue
            own_logit = _logit(chance_of(score))
            m.chances[task] = {level: round(1 / (1 + math.exp(-(own_logit - _logit(chance_of(level)) + top))), 4) for level in range(1, 6)}


def effort_format(b: "BackendConfig") -> str:
    if b.effort_format != "auto":
        return b.effort_format
    if b.format == "anthropic":
        return "anthropic"
    if b.api == "gemini":
        return "openai"  # reasoning_effort, which the Gemini converter turns into a thinking level
    host = b.base_url.lower()
    if "openrouter.ai" in host:
        return "openrouter"
    if "api.openai.com" in host or ".openai.azure.com" in host:
        return "openai"
    return "none"


def uses_reported_cost(b: "BackendConfig") -> bool:
    if b.billed_cost != "auto":
        return b.billed_cost == "reported"
    return b.kind == "api" and "openrouter.ai" in b.base_url.lower()


def load_raw_config(path: str | Path | None = None) -> dict:
    """The merged config document (includes resolved, ${ENV} references left as written)."""
    path = Path(path) if path else default_config_path()
    return _load_raw(path, set())


def build_config(raw: dict, config_dir: str | Path = ".", prices_data: dict | None = None, quiet: bool = False) -> Config:
    """Validate a merged config document and resolve environment references, prices, and capabilities.

    ``prices_data`` (the prices.json structure) overrides the file in ``config_dir``; the
    database-backed config passes the prices it stores.
    """
    import copy

    raw = _interpolate(copy.deepcopy(raw))
    oidc = (raw.get("auth") or {}).get("oidc")
    if isinstance(oidc, dict) and not (oidc.get("issuer") and oidc.get("client_id")):
        raw["auth"]["oidc"] = None
    ch = (raw.get("reporting") or {}).get("clickhouse")
    if isinstance(ch, dict) and not str(ch.get("url") or "").strip():
        raw["reporting"]["clickhouse"] = None  # url: ${MERITORDER_CLICKHOUSE_URL:-} with the variable unset: off
    elif isinstance(ch, dict) and re.match(r"^\s*https?://[^/@]*:[^/@]*@", str(ch.get("url"))):
        # As configstore.secret_problems refuses a literal one, for a URL from an environment variable too: the URL is shown
        # on the console and in errors. Refused here, before validation, so the error doesn't repeat the URL.
        raise ValueError("reporting.clickhouse.url holds a password; set user, and the password with auth.value_env or value_file")
    raw["backends"] = _named(raw.get("backends"))
    raw["models"] = _named(raw.get("models"))
    if "teams" in raw:
        raw["teams"] = _named(raw.get("teams"))
    if isinstance(raw.get("billing"), dict):
        raw["billing"] = {**raw["billing"], "sources": _named(raw["billing"].get("sources"))}
    if isinstance(raw.get("mcp"), dict):
        raw["mcp"] = {**raw["mcp"], "servers": _named(raw["mcp"].get("servers"))}
    cfg = Config.model_validate(raw)
    _disable_unusable_backends(cfg, quiet)
    cfg.config_dir = str(Path(config_dir).resolve())
    catalog = prices_data if prices_data is not None else load_catalog(Path(config_dir) / cfg.prices_file)
    prices, contexts, reasoning, outputs = parse_prices(catalog)
    apply_quality(cfg, catalog)
    cfg.canaries = [c for c in (catalog.get("exploration") or {}).get("canaries") or [] if isinstance(c, dict)]
    unpriced = []
    for m in cfg.models:
        if m.context_window is None:
            m.context_window = contexts.get(m.name)
        if m.max_output_tokens is None:
            m.max_output_tokens = outputs.get(m.name)
        if not m.effort_levels and m.name in reasoning and m.kind == "chat":
            m.effort_levels = ["low", "medium", "high"]
        for r in m.routes:
            if r.price is None:
                r.price = prices.get(f"{r.backend}:{r.model}")
                if r.price is None:
                    unpriced.append(f"{m.name}@{r.backend}")
            elif r.price.source is None:
                r.price.source = "config"
    unknown = sorted({r.backend for m in cfg.models for r in m.routes} - {b.name for b in cfg.backends})
    if unknown:
        raise ValueError(f"routes refer to backends that aren't defined: {', '.join(unknown)}")
    for s in cfg.billing.sources:
        missing = sorted(set(s.backends or []) - {b.name for b in cfg.backends})
        if missing:
            raise ValueError(f"billing.sources.{s.name}.backends names backends that aren't defined: {', '.join(missing)}")
        bad = sorted({m for m in s.models.values() if cfg.model(m) is None})
        if bad:
            raise ValueError(f"billing.sources.{s.name}.models maps bill lines to models that aren't configured: {', '.join(bad)}")
    if len(cfg.billing.sources) > 1:
        from .billing.sources import double_counted  # here, not at the top: billing imports this module

        twice = double_counted(cfg)  # an API and its export both reading one bill: every day would count twice
        if twice:
            raise ValueError(twice[0])
    # Savings are measured against the baseline model: a typo there would silently zero them.
    chat = {m.name for m in cfg.models if m.kind == "chat"}
    for where, name in [("baseline_model", cfg.baseline_model)] + [(f"teams.{t.name}.baseline_model", t.baseline_model) for t in cfg.teams]:
        if name and name not in chat:
            raise ValueError(f"{where} is '{name}', which isn't a configured chat model")
    wants = [t.name for t in cfg.teams if t.compression.model.enabled]
    if wants and not cfg.compressor.url:
        raise ValueError(f"teams {', '.join(wants)} turn on model-based compression (compression.model), but compressor.url "
                         "isn't set: run `meritorder compressor` and point compressor.url at it")
    for t in cfg.teams:
        _check_judged_labels(cfg, t)
    if unpriced and not quiet:
        log.info("routes without a known price (not used until they have one, GPU routes excepted): %s", ", ".join(unpriced))
    if not quiet:
        _warn_skypilot_clouds(cfg)
    return cfg


def _warn_skypilot_clouds(cfg: Config) -> None:
    """Once per load, where the credential files are (the controller): the long-lived keys SkyPilot copies to the
    replicas it launches, and spot machines that stop instead of being deleted (sky_clouds.warnings)."""
    said: set[str] = set()
    for b in cfg.backends:
        s = b.serve
        if not b.enabled or s is None or s.provisioner != "skypilot":
            continue
        for w in sky_clouds.warnings(s.clouds, s.use_spot):
            if w not in said:
                said.add(w)
                log.warning("SkyPilot (%s): %s", b.name, w)
        if s.weights_cache == "volume":
            monthly = sky_clouds.volume_usd_per_month(s.weights_cache_infra, s.weights_cache_gb, s.weights_cache_usd_per_gb_month)
            log.info("SkyPilot (%s): the weights cache is a %s GB volume on %s, %s, from its first start until `meritorder gpu "
                     "volumes delete` removes it", b.name, s.weights_cache_gb, s.weights_cache_infra,
                     f"about ${monthly:,.2f} a month" if monthly is not None else
                     "at your storage class's price (set weights_cache_usd_per_gb_month to count it in GPU spend)")


def _check_judged_labels(cfg: Config, team: TeamConfig) -> None:
    """A team's judged labels must name models it can use: they're sent its requests as the team (judged.py)."""
    s = team.judged_labels
    if not s.enabled:
        return
    where = f"teams.{team.name}.judged_labels"
    if s.stronger != cfg.routing.auto_model and s.stronger != "auto":
        m = cfg.model(s.stronger)
        if m is None or m.kind != "chat":
            raise ValueError(f"{where}.stronger is '{s.stronger}', which isn't a configured chat model (or auto)")
        if not team.allows_model(m.name):
            raise ValueError(f"{where}.stronger is '{m.name}', which team '{team.name}' isn't allowed to use")
    unknown = [j for j in s.judges if (m := cfg.model(j)) is None or m.kind != "chat"]
    if unknown:
        raise ValueError(f"{where}.judges names models that aren't configured chat models: {', '.join(unknown)}")
    if not any(team.allows_model(cfg.model(j).name) for j in s.judges):  # type: ignore[union-attr]
        raise ValueError(f"{where}.judges: team '{team.name}' isn't allowed to use any of {', '.join(s.judges)}")


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else default_config_path()
    return build_config(load_raw_config(path), path.parent)


_AZURE_PROJECT = re.compile(r"(\.services\.ai\.azure\.com)/api/projects/[^/]+", re.IGNORECASE)


def normalize_base_url(url: str) -> str:
    """Accept the endpoint forms people actually have in their environment.

    * Azure AI Foundry project URLs (``.../api/projects/<name>``): Claude and the
      OpenAI v1 API live at the resource root, so the project path is dropped.
    * An endpoint that already ends in the API path the config appends
      (``.../openai/v1/openai/v1``) is collapsed.
    """
    url = _AZURE_PROJECT.sub(r"\1", url.rstrip("/"))
    for suffix in ("/openai/v1", "/anthropic", "/v1"):
        while url.endswith(suffix + suffix):
            url = url[: -len(suffix)]
    return url


def _disable_unusable_backends(cfg: Config, quiet: bool = False) -> None:
    for b in cfg.backends:
        b.base_url = normalize_base_url(b.base_url)
        if not b.enabled:
            continue
        reason = None
        if b.kind == "self_hosted" and b.serve is not None:
            pass  # endpoint is published by the capacity controller at runtime
        elif b.kind != "mock":
            if b.flavor == "vertex":
                if not b.project:
                    reason = "no GCP project configured"
            elif not b.base_url.startswith(("http://", "https://")):
                reason = f"base_url {b.base_url!r} is not a URL"
            if b.auth.type == "api_key" and not b.auth.key():
                reason = f"{b.auth.describe()} is not set"
        if reason:
            b.enabled = False
            b.disabled_reason = reason
            if not quiet:
                log.info("backend %s disabled: %s", b.name, reason)
