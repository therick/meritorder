import { Alert, Anchor, Badge, Button, Group, MultiSelect, NumberInput, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Tabs, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useEffect, useState } from "react";
import type { AdminConfig, Api, BackendStatus, Breakeven, BreakevenGpu, BreakevenModel, BreakevenWeights, Me, Price, PriceStatus, QualityStatus, TokenCalibration, WhatIf } from "../api";
import { ms, num, pct, usd } from "../api";
import { BreakevenChart, compact, replicaLabel } from "../components/charts";
import { AnsweredBy, Kpi, Loading, PageTitle, Panel, Status, StatusBadge } from "../components/common";
import { Icon } from "../components/icons";
import { useData } from "../hooks";
import { EvalsPanel } from "./Evals";

// ------------------------------------------------------------------ GPUs

export function WhatIfPanel({ api }: { api: Api }) {
  const models = useData(() => api.gpuCandidates(), [api]);
  const [model, setModel] = useState<string | null>(null);
  const [gpu, setGpu] = useState<number | string>(16);
  const [cap, setCap] = useState<number | string>(4000);
  const [days, setDays] = useState<number | string>(30);
  const [result, setResult] = useState<WhatIf | null>(null);
  const [busy, setBusy] = useState(false);
  const choices = (models.data?.models ?? []).map((m) => m.id);
  const chosen = model ?? choices[0] ?? null;
  const run = async () => {
    if (!chosen) return;
    setBusy(true);
    try {
      setResult(await api.whatIf(chosen, Number(gpu), Number(cap), Number(days)));
    } catch (e) {
      notifications.show({ color: "red", title: "What-if failed", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="What if we self-hosted a model?">
      <Text size="sm" c="dimmed" mb="sm">Replays your recent traffic hour by hour: GPUs run in the hours where the traffic they could serve costs more at API prices than the GPUs do.</Text>
      <Group align="flex-end" gap="sm">
        <Select label="Open-weight model" data={choices} value={chosen} onChange={setModel} searchable w={220} />
        <NumberInput label="GPU $/hour per replica" value={gpu} onChange={setGpu} min={0.01} step={0.5} decimalScale={2} w={170} />
        <NumberInput label="Tokens/s per replica" value={cap} onChange={setCap} min={1} step={100} w={150} />
        <NumberInput label="Days of history" value={days} onChange={setDays} min={1} max={90} w={130} />
        <Button onClick={run} loading={busy} disabled={!chosen}>Calculate</Button>
      </Group>
      {result && (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} mt="md">
          <Kpi label="Projected savings / month" value={usd(result.projected_monthly_savings)} accent={result.projected_monthly_savings > 0}
            sub={result.projected_monthly_savings > 0 ? "after GPU costs" : "self-hosting wouldn't pay for itself"} />
          <Kpi label="Traffic it could serve" value={usd(result.addressable_api_cost)} sub={`at API prices, last ${result.days} days`} />
          <Kpi label="GPU cost" value={usd(result.gpu_cost)} sub={`${num(result.hours_gpus_would_run)} of ${num(result.hours_with_traffic)} busy hours, up to ${result.peak_replicas} replica(s)`} />
          <Kpi label="Tokens on GPUs" value={pct(result.share_of_tokens_on_gpu)} sub="of what it could serve" />
        </SimpleGrid>
      )}
      {result && <Group justify="space-between" mt="xs"><Text size="xs" c="dimmed">{result.note}</Text><AnsweredBy by={result.answered_by} /></Group>}
    </Panel>
  );
}

export function Backends({ api }: { api: Api }) {
  const res = useData(() => api.backends(), [api], 10_000);
  const [showOff, setShowOff] = useState(false);
  const rows = (res.data?.backends ?? []).filter((b) => showOff || b.enabled);
  return (
    <>
      <PageTitle title="Backends"><Switch label="Show switched-off backends" checked={showOff} onChange={(e) => setShowOff(e.currentTarget.checked)} /></PageTitle>
      <Panel title={`Health · last ${res.data?.minutes ?? 5} minutes`}>
        <Loading loading={res.loading && !res.data} error={res.error} />
        <Table.ScrollContainer minWidth={900}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Backend</Table.Th><Table.Th>Status</Table.Th><Table.Th>Kind</Table.Th><Table.Th ta="right">Requests</Table.Th>
              <Table.Th ta="right">Errors</Table.Th><Table.Th ta="right">Avg latency</Table.Th><Table.Th ta="right">Time to first token</Table.Th>
              <Table.Th>Effort</Table.Th><Table.Th>Models</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {rows.map((b: BackendStatus) => (
                <Table.Tr key={b.name}>
                  <Table.Td><Text fw={600} size="sm">{b.name}</Text><Text size="xs" c="dimmed">{b.host ?? ""}</Text></Table.Td>
                  <Table.Td>{!b.enabled ? <StatusBadge kind="off" title={b.reason ?? ""}>off</StatusBadge> : b.circuit_open ? <StatusBadge kind="bad">failing</StatusBadge>
                    : b.draining ? <StatusBadge kind="drain">draining</StatusBadge> : b.live ? <StatusBadge kind="good">live</StatusBadge> : <StatusBadge kind="clock">waiting</StatusBadge>}
                    {!b.enabled && b.reason && <Text size="xs" c="dimmed">{b.reason}</Text>}
                    {b.skipped && <Status kind="bad" style={{ whiteSpace: "normal", fontSize: 12, display: "flex" }}>{b.skipped.why}; retrying in {b.skipped.seconds_left}s</Status>}
                    {Object.entries(b.skipped_models ?? {}).map(([m, s]) => (
                      <Status key={m} kind={s.capacity ? "warn" : "bad"} title={s.why} style={{ whiteSpace: "normal", fontSize: 12, display: "flex" }}>{m}: {s.capacity ? "busy" : "skipped"}, retrying in {s.seconds_left}s</Status>
                    ))}</Table.Td>
                  <Table.Td>{b.kind === "api" ? `${b.api === "gemini" ? "gemini" : b.format} API` : b.kind.replace("_", "-")}
                    {b.batch && <Text size="xs" c="dimmed" title={b.batch.api === "gpu" ? "Batch work runs in this deployment's headroom while it's running, at low priority" : b.batch.why}>
                      {b.batch.api === "gpu" ? "batches: in its headroom" : b.batch.api ? `batches: ${b.batch.api}` : "no batches"}</Text>}</Table.Td>
                  <Table.Td className="num">{num(b.requests)}</Table.Td>
                  <Table.Td className="num" c={b.error_rate > 0.02 ? "var(--mo-bad)" : undefined}>{b.requests ? `${b.errors} (${pct(b.error_rate)})` : "–"}</Table.Td>
                  <Table.Td className="num">{ms(b.avg_latency_ms)}</Table.Td>
                  <Table.Td className="num">{ms(b.avg_ttft_ms)}</Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{b.effort_format}</Text></Table.Td>
                  <Table.Td><Text size="xs" c="dimmed" lineClamp={2} maw={260}>{b.models.join(", ")}</Text>
                    {Object.entries(b.speeds ?? {}).map(([m, sp]) => (
                      <Text key={m} size="xs" title={`${sp.samples} replies timed, last ${sp.seconds_ago}s ago`}>
                        {m}: first token {ms(sp.first_token_ms)}{sp.tokens_per_second ? ` · ${num(Math.round(sp.tokens_per_second))} tok/s` : ""}</Text>
                    ))}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ Models

const SOURCE_LABEL: Record<string, [string, string]> = {
  openrouter: ["OpenRouter", "OpenRouter's catalog: what OpenRouter charges. Refreshed by the worker."],
  litellm: ["provider list", "The provider's list price, from the price list LiteLLM maintains. Refreshed by the worker."],
  config: ["config", "Set in the configuration: an override such as a negotiated discount. Not refreshed."],
};
const ago = (iso?: string | null) => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};

/** Where a route's price comes from, and its long-prompt prices. */
function PriceSource({ p }: { p: Price }) {
  const [label, help] = SOURCE_LABEL[p.source ?? "config"] ?? [p.source ?? "config", ""];
  const tiers = (p.tiers ?? []).map((t) => `above ${Math.round(t.above / 1000)}K prompt tokens: $${t.input} in, $${t.output} out`).join("; ");
  return (
    <Group gap={4} wrap="nowrap">
      <Tooltip label={help} multiline w={280}><Badge size="sm" color={p.source === "config" || !p.source ? "mo" : "gray"}>{label}</Badge></Tooltip>
      {tiers && <Tooltip label={tiers} multiline w={300}><Badge size="xs" variant="outline">long-prompt tier</Badge></Tooltip>}
    </Group>
  );
}

/** How this model's prompt tokens compare with the gateway's estimate, learned from what its replies report. */
function TokenRatio({ t }: { t?: TokenCalibration }) {
  if (!t) return null;
  const ratio = `×${t.ratio.toFixed(2)}`;
  const help = t.source === "model"
    ? `This model's replies report ${t.ratio.toFixed(2)} prompt tokens for every token the gateway estimated (${num(t.samples)} ${t.samples === 1 ? "reply" : "replies"}`
      + `${t.spread != null ? `, varying by about ${pct(t.spread)}` : ""}). Context-window checks, long-prompt prices and cost estimates use the corrected count.`
    : t.source === "family"
      ? `No replies from this model yet: using the ratio measured on other ${t.family} models.`
      : "Not calibrated yet: the plain estimate is used until this model's replies report their prompt tokens.";
  return (
    <Tooltip label={help} multiline w={300} withArrow>
      <Text size="xs" c={t.source === "model" ? undefined : "dimmed"}>{t.source === "none" ? "uncalibrated" : t.source === "family" ? `${ratio} (${t.family})` : ratio}</Text>
    </Tooltip>
  );
}

/** When prices were last checked, and anything wrong with the live lists. */
function PriceFreshness({ s }: { s: PriceStatus }) {
  const failed = Object.entries(s.sources ?? {}).filter(([, v]) => !v.ok);
  const stale = !s.checked_at || Date.now() - new Date(s.checked_at).getTime() > 3 * s.refresh_minutes * 60000;
  const problems = failed.length > 0 || stale || (s.disagreements?.length ?? 0) > 0;
  return (
    <Alert color={problems ? "yellow" : "gray"} icon={<Icon name={problems ? "warn" : "info"} size={16} />} mb="md" title={problems ? "Prices need attention" : undefined}>
      Prices are live: checked {ago(s.checked_at)} against OpenRouter's catalog and the providers' list prices, every {s.refresh_minutes} minutes by the worker
      {s.changed?.length ? `; the last check changed ${s.changed.length}` : ""}.
      {stale && " That's longer ago than it should be: is the worker running?"}
      {failed.map(([name, v]) => <Text key={name} size="sm">Couldn't fetch {name}: {v.error}. Routes priced from it keep their last prices.</Text>)}
      {(s.disagreements ?? []).map((d) => (
        <Text key={`${d.model}-${d.backend}-${d.field}`} size="sm">{d.model} on {d.backend}: list price ${d.listed} for {d.field}, OpenRouter ${d.openrouter}.</Text>
      ))}
    </Alert>
  );
}

const QUALITY_SOURCE: Record<string, [string, string]> = {
  epoch: ["Epoch AI", "gray"],
  artificial_analysis: ["Artificial Analysis", "gray"],
  config: ["config", "gray"],
  override: ["override", "mo"],
  unmeasured: ["not measured", "yellow"],
};
const pctOf = (x: number) => `${Math.round(x * 100)}%`;

/** A model's quality scores, each with where it came from and (on hover) how it was worked out. */
function QualityBadges({ m, q }: { m: AdminConfig["models"][number]; q?: QualityStatus }) {
  return (
    <Group gap={4}>
      {Object.entries(m.quality).map(([task, v]) => {
        const src = m.quality_sources?.[task] ?? "config";
        const [label, color] = QUALITY_SOURCE[src] ?? [src, "gray"];
        const d = m.quality_detail?.[task];
        const ref = q?.reference?.[task];
        // Estimated: Epoch refines this task type with the model's own results (e.g. FrontierSWE for coding), and it
        // has none. A task type with no such benchmarks (general) is the model's ability itself, not an estimate.
        const estimated = src === "epoch" && d && (q?.evidence?.[task]?.length ?? 0) > 0 && !Object.keys(d.evidence ?? {}).length;
        const levels = m.chances?.[task];
        // Used when routing by cost per success: this score's chance of solving work at each level.
        const chances = levels && <Text size="xs">Chance of solving work at levels 1–5: {[1, 2, 3, 4, 5].map((l) => pctOf(levels[l] ?? 0)).join(", ")}.</Text>;
        const help = d && src !== "config" && src !== "override"
          ? <Stack gap={2}>
              <Text size="xs">{label}: {ref ? `a ${pctOf(d.chance)} chance of solving a task as hard as ${ref}` : `index ${Math.round(d.chance * 100)}`}, {d.basis}.</Text>
              {Object.entries(d.evidence ?? {}).map(([b, e]) => <Text key={b} size="xs">{b}: {pctOf(e.score)}{e.variant ? ` (at ${e.variant} effort)` : ""}</Text>)}
              {estimated && <Text size="xs">Estimated from the model's general ability: no {task} results published yet.</Text>}
              {chances}
            </Stack>
          : src === "override" ? <Stack gap={2}><Text size="xs">Set by hand (quality_override): the live source doesn't change it.</Text>{chances}</Stack>
          : src === "unmeasured" ? "The live source doesn't cover this model, so auto won't pick it for this (requests that name it still work). Set a quality_override to use it."
          : levels ? <Stack gap={2}><Text size="xs">From the configuration.</Text>{chances}</Stack>
          : "From the configuration.";
        return (
          <Tooltip key={task} label={help} multiline w={320} withArrow>
            <Badge size="xs" variant="light" color={color}>{task} {src === "unmeasured" ? "–" : v.toFixed(1)}{estimated ? "*" : ""}</Badge>
          </Tooltip>
        );
      })}
    </Group>
  );
}

/** Where quality scores come from, when they were worked out, and the credit the source asks for. */
const SCALE_NAME: Record<string, string> = { absolute: "absolute", best: "best-anchored", range: "worst-to-best" };

function QualityFreshness({ q }: { q: QualityStatus }) {
  const name = QUALITY_SOURCE[q.source]?.[0] ?? q.source;
  if (q.source === "config") return null;
  const failed = q.status && !q.status.ok;
  return (
    <Alert color={failed || q.stale ? "yellow" : "gray"} icon={<Icon name={failed || q.stale ? "warn" : "info"} size={16} />} mb="md">
      Quality scores come from {name}{q.reference && q.source === "epoch" ? `: each model's chance of solving a task as hard as ${[...new Set(Object.values(q.reference))].join(" / ")}` : ""},
      {" "}on the <b>{SCALE_NAME[q.scale] ?? q.scale}</b> scale ({q.scale === "absolute" ? `a 5 solves the reference ${Object.entries(q.anchor ?? {}).map(([t, a]) => `${Math.round(a * 100)}% of the time for ${t}`).join(" and ")}, a fixed bar; others in proportion` : q.scale === "best" ? "the best model is 5, others in proportion" : "the weakest is 1, the best 5"}),
      {" "}worked out {ago(q.updated ?? undefined)} and every {q.refresh_hours} hours by the worker. A * marks a score estimated from general ability.
      {q.missing.length > 0 && ` Not covered, so auto doesn't pick them (requests that name them still work; a quality_override changes that): ${q.missing.join(", ")}.`}
      {failed && <Text size="sm">Couldn't refresh: {q.status?.error}. The last scores stay in use.</Text>}
      {q.stale && <Text size="sm">The quality settings changed since these were worked out; the worker updates them on its next cycle.</Text>}
      {q.attribution && <Text size="xs" c="dimmed" mt={4}>Data: {q.attribution}.</Text>}
    </Alert>
  );
}

const perM = (v: number | null | undefined) => (v == null ? "–" : `$${v < 0.1 ? v.toFixed(3) : v.toFixed(2)}`);
const gb = (bytes: number) => `${compact(bytes / 1e9)} GB`;

/** A model's verdict: whether a replica pays now, from how much traffic, or never (the GPU costs more per token). */
function Verdict({ m }: { m: BreakevenModel }) {
  if (!m.gpu) return <Text size="xs" c="dimmed">{m.why}</Text>;
  if (m.tokens_per_minute == null) return <Text size="xs" c="dimmed">{m.basis}</Text>;
  if (!m.pays) return <Text size="xs">✕ never: {perM(m.gpu_usd_per_million)}/M on the GPU vs {perM(m.api_usd_per_million)}/M on APIs</Text>;
  return <Text size="xs">{m.traffic.peak_tokens_per_minute >= m.tokens_per_minute ? "✓ pays now" : `✓ from ${compact(m.tokens_per_minute)}/min`}</Text>;
}

/** The replica in a table cell: GPUs, where, spot or on-demand, and its price (the other price on hover). */
function ReplicaCell({ g }: { g: BreakevenGpu | null }) {
  if (!g) return <Text size="xs" c="dimmed">–</Text>;
  const other = g.spot ? g.on_demand_usd_per_hour : g.spot_usd_per_hour;
  const where = [g.region, g.instance_type].filter(Boolean).join(" · ");
  const label = `${where}${other != null ? `. ${g.spot ? "On-demand" : "Spot"}: ${usd(other)}/h` : ""}${g.source === "controller" ? ". The controller's current price" : g.source === "node_type" ? ". Its Kubernetes node type's configured price" : g.source === "configured" ? ". Configured price" : ""}`
    + (g.weights ? `. Weights in ${g.weights}${g.published_weights && g.published_weights !== g.weights ? ` (published in ${g.published_weights})` : ""}` : "")
    + (g.computes_in ? `; the GPUs compute in ${g.computes_in}` : "");
  return (
    <Tooltip label={label || "configured"} disabled={!label}>
      <div><Text size="xs">{replicaLabel(g)}{g.weights ? ` · ${g.weights}` : ""}</Text>
        <Text size="xs" c="dimmed">{usd(g.usd_per_hour)}/h{g.deployment ? ` · ${g.deployment}` : ""}</Text></div>
    </Tooltip>
  );
}

/** Per task type, the highest quality level an open model you can run could serve: the best open model that fits on
 * a machine on offer, its replica, and a better one that doesn't fit. */
function HostableKpis({ d }: { d: Breakeven }) {
  const tasks = Object.entries(d.hostable);
  if (!tasks.length) return null;
  return (
    <SimpleGrid cols={{ base: 1, sm: Math.min(tasks.length, 3) }}>
      {tasks.map(([task, h]) => (
        <Kpi key={task} label={`Best ${task} level you can host`} value={h.level != null ? `${task}:${h.level}` : "none"}
          sub={h.model
            ? `${h.model} (${task} ${h.quality?.toFixed(1)}) on ${replicaLabel(h.gpu)}${h.gpu?.weights ? ` · ${h.gpu.weights}` : ""}, ${usd(h.gpu?.usd_per_hour ?? 0)}/h`
              + (h.gpu?.quality_note ? `; quality unmeasured at ${h.gpu.weights}` : "")
              + (h.better_unrunnable ? `. ${h.better_unrunnable.model} (${h.better_unrunnable.quality.toFixed(1)}) can't run: ${h.better_unrunnable.why}` : "")
            : "no open model with a score for it fits on a machine on offer"} />
      ))}
    </SimpleGrid>
  );
}

/** How much traffic one GPU replica needs to pay for itself: by quality level (the price it has to beat is the cheapest
 * API model good enough for that level) and by open-weight model (what the traffic it could serve costs today). Each
 * model's replica is sized to it and priced from SkyPilot's catalog. */
function BreakevenPanel({ api, admin }: { api: Api; admin: boolean }) {
  const [spot, setSpot] = useState(true);
  const [weights, setWeights] = useState<BreakevenWeights>("published");
  const [clouds, setClouds] = useState<string[]>([]);
  const [eff, setEff] = useState<number | string>(10);
  const [util, setUtil] = useState<number | string>(70);
  const [days, setDays] = useState<number | string>(7);
  const [shown, setShown] = useState({ eff: 10, util: 70, days: 7 });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const next = { eff: Number(eff), util: Number(util), days: Number(days) };
    if (!(next.eff > 0 && next.eff <= 100 && next.util > 0 && next.util <= 100 && next.days >= 1 && next.days <= 90)) return;
    const t = setTimeout(() => setShown(next), 400);  // recalculate once typing pauses
    return () => clearTimeout(t);
  }, [eff, util, days]);
  const res = useData(() => api.gpuBreakeven({ days: shown.days, utilization: shown.util / 100, efficiency: shown.eff / 100, spot, weights, clouds }),
    [api, shown, spot, weights, clouds]);
  const d: Breakeven | null = res.data;
  // Stale or missing prices are fetched in the background (about a minute): ask again until they're in.
  useEffect(() => {
    if (!d?.prices.refreshing) return;
    const t = setTimeout(() => void res.reload(), 10_000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d]);
  const refresh = async () => {
    setBusy(true);
    try {
      const r = await api.refreshGpuPrices();
      if (r.requested) {
        notifications.show(r.controller
          ? { color: "mo", title: "Asked the GPU controller", message: "It gets the latest prices from SkyPilot; they show here within a minute." }
          : { color: "yellow", title: "No GPU controller is running", message: "SkyPilot isn't installed where the gateway runs, so prices come from the GPU controller. Start it, or install SkyPilot in the gateway's image." });
        if (r.controller) for (const wait of [10_000, 30_000, 60_000]) setTimeout(() => void res.reload(), wait);
        return;
      }
      notifications.show({ color: "green", title: "Latest GPU prices", message: `${r.offers} offers from SkyPilot's catalog`
        + (r.unsized?.length ? `; no size published for ${r.unsized.join(", ")}` : "") });
      await res.reload();
    } catch (e) {
      notifications.show({ color: "red", title: "Couldn't get prices", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  const reachable = d?.groups.filter((g) => g.gpu && g.pays) ?? [];
  const paying = d?.models.filter((m) => m.gpu && m.pays) ?? [];
  return (
    <Stack gap="md">
      <Alert variant="light" color="gray">
        A GPU replica earns its keep by taking traffic you'd otherwise pay an API for, so the controller starts one only when the traffic it could serve
        costs at least its price plus {d ? Math.round(d.assumptions.margin * 100) : 20}% at API prices. Each open-weight model gets the machine that pays from the
        least traffic (or, if none can, comes closest) among those whose GPUs hold its weights in {d ? Math.round(d.assumptions.fit * 100) : 70}% of their
        memory (the rest is for the KV cache), priced from SkyPilot's catalog.
        Because <code>auto</code> sends each request to the cheapest model good enough for its level, the price a GPU has to beat at a level is that model's.
      </Alert>
      {d?.prices.refreshing && <Alert color="mo" icon={<Icon name="clock" size={16} />} title="Getting the latest GPU prices and model sizes">
        Reading SkyPilot's catalogs and the models' sizes on Hugging Face takes a minute or two. This page updates when
        they're in{d.prices.fetched_at ? `; until then, the prices shown are from ${ago(d.prices.fetched_at)}.` : "."}</Alert>}
      {d?.prices.problem && <Alert color="yellow" icon={<Icon name="warn" size={16} />} title="Couldn't get the latest GPU prices">{d.prices.problem}
        {d.prices.fetched_at ? ` The prices shown are from ${ago(d.prices.fetched_at)}.` : ""}</Alert>}
      <Panel title="Assumptions" right={<Group gap="sm" wrap="nowrap"><AnsweredBy by={d?.answered_by} />
        {admin && <Button size="xs" variant="default" loading={busy} onClick={refresh}>Get latest prices</Button>}</Group>}>
        <Group align="flex-end" gap="md">
          <MultiSelect label="Clouds" data={d?.prices.clouds ?? []} value={clouds} onChange={setClouds} placeholder={clouds.length ? undefined : "all"}
            w={260} clearable aria-label="Clouds" />
          <NumberInput label="Share of peak compute %" value={eff} onChange={setEff} min={1} max={100} step={1} w={170} />
          <NumberInput label="Target utilization %" value={util} onChange={setUtil} min={1} max={100} step={5} w={150} />
          <NumberInput label="Days of traffic" value={days} onChange={setDays} min={1} max={90} w={120} />
          <Stack gap={4}>
            <Text size="sm" fw={500}>Weights</Text>
            <SegmentedControl size="xs" value={weights} onChange={(v) => setWeights(v as BreakevenWeights)} aria-label="Weights"
              data={[{ label: "As published", value: "published" }, { label: "FP8", value: "fp8" }, { label: "4-bit", value: "4bit" }]} />
          </Stack>
          <Switch label="Spot prices" checked={spot} onChange={(e) => setSpot(e.currentTarget.checked)} pb={6} />
        </Group>
        <Text size="xs" c="dimmed" mt="xs">
          {d && `GPU prices: SkyPilot${d.prices.skypilot ? ` ${d.prices.skypilot}` : ""}'s catalog, ${d.prices.offers} offers, checked ${ago(d.prices.fetched_at)}`
            + (d.prices.catalog_updated && Object.keys(d.prices.catalog_updated).length
              ? ` (catalog downloaded ${ago(Object.values(d.prices.catalog_updated).sort()[0])})` : "")
            + `. Model sizes from Hugging Face, ${ago(d.sizes.fetched_at)}. Throughput is an estimate: ${shown.eff}% of the GPUs' peak compute at two operations per active
            parameter per token, with cached input not computed again; a deployment's measured capacity replaces it. Prices are blended for
            ${d.mix.source === "traffic" ? "your traffic's" : "an assumed (no traffic yet)"} mix: ${Math.round(d.mix.input * 100)}% input, ${Math.round(d.mix.cache_read * 100)}% cached
            input, ${Math.round(d.mix.cache_write * 100)}% cache writes, ${Math.round(d.mix.output * 100)}% output`
            + (d.mix.tokens_per_request ? `, about ${compact(d.mix.tokens_per_request)} tokens a request.` : ".")}
        </Text>
      </Panel>
      {weights !== "published" && <Alert color="yellow" icon={<Icon name="warn" size={16} />} title={weights === "fp8" ? "Serving in FP8" : "Serving in 4 bits"}>
        {weights === "fp8"
          ? "16-bit checkpoints served in FP8 (vLLM's --quantization fp8), half the memory. Checkpoints published already quantized are served as published. "
          : "Every weight above 4 bits served in 4 (a 4-bit checkpoint made from the published one: AWQ, GPTQ or NVFP4), about a quarter of 16-bit memory. "
            + "Models published in 4 bits (gpt-oss, Kimi, DeepSeek's experts) barely change. "}
        Quality scores are for the weights as published, so a quantized model may score lower than shown: run the evals against it before routing on it.
        Coding is usually the first to suffer.</Alert>}
      <Loading loading={res.loading && !d} error={res.error} />
      {d && <HostableKpis d={d} />}
      {d && (
        <SimpleGrid cols={{ base: 1, sm: 3 }}>
          <Kpi label="Levels where a GPU can pay" value={`${reachable.length} of ${d.groups.length}`} accent={reachable.length > 0}
            sub={reachable.length ? reachable.map((g) => `${g.floor} (${g.gpu?.model})`).join(", ") : "none at these prices"} />
          <Kpi label="Open models that can pay" value={`${paying.length} of ${d.models.length}`}
            sub={paying.length ? `cheapest to start: ${paying[0].model} from ${compact(paying[0].tokens_per_minute)}/min` : "none at these prices"} />
          <Kpi label="GPU prices" value={d.assumptions.spot ? "spot" : "on-demand"} sub={`${d.assumptions.clouds.join(", ") || "no clouds"} · checked ${ago(d.prices.fetched_at)}`} />
        </SimpleGrid>
      )}
      {d && (
        <Panel title={`By quality level · last ${d.days} days`}>
          <BreakevenChart groups={d.groups} />
          <Table.ScrollContainer minWidth={1000} mt="sm">
            <Table fz="sm" verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th>Level</Table.Th><Table.Th>Cheapest API model good enough</Table.Th><Table.Th ta="right">API $/M</Table.Th>
                <Table.Th>Open model and GPU</Table.Th><Table.Th ta="right">GPU $/M full</Table.Th><Table.Th ta="right">Needed tok/min</Table.Th>
                <Table.Th ta="right">≈ requests/h</Table.Th><Table.Th ta="right">Your tok/min (avg · busiest)</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{d.groups.map((g) => (
                <Table.Tr key={g.floor}>
                  <Table.Td>{g.floor}</Table.Td>
                  <Table.Td>{g.cheapest ? <>{g.cheapest.model} <Text span size="xs" c="dimmed">{g.cheapest.backend}</Text></> : "–"}</Table.Td>
                  <Table.Td className="num">{perM(g.api_usd_per_million)}</Table.Td>
                  <Table.Td>{g.gpu ? <><Text size="xs" fw={600}>{g.gpu.model}</Text><ReplicaCell g={g.gpu} /></>
                    : <Text size="xs" c="dimmed" lineClamp={2} title={g.why ?? ""}>{g.why}</Text>}</Table.Td>
                  <Table.Td className="num">{perM(g.gpu_usd_per_million)}</Table.Td>
                  <Table.Td className="num">{compact(g.tokens_per_minute)}{g.tokens_per_minute != null && !g.pays && <Text span size="xs" c="dimmed"> (over capacity)</Text>}</Table.Td>
                  <Table.Td className="num">{compact(g.requests_per_hour)}</Table.Td>
                  <Table.Td className="num">{compact(g.traffic.tokens_per_minute)} · {compact(g.traffic.peak_tokens_per_minute)}</Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Panel>
      )}
      {d && (
        <Panel title="By open-weight model">
          <Text size="xs" c="dimmed" mb="xs">What the traffic each one could serve (its quality meets the level) cost over the last {d.days} days, against one replica
            sized to it. Models <code>auto</code> doesn't pick (no quality score) have no traffic to take.</Text>
          <Table.ScrollContainer minWidth={1250}>
            <Table fz="sm" verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th>Model</Table.Th><Table.Th>Size</Table.Th><Table.Th>GPU replica</Table.Th><Table.Th ta="right">Tok/s</Table.Th>
                <Table.Th ta="right">GPU $/M full</Table.Th><Table.Th ta="right">Its API $/M</Table.Th><Table.Th ta="right">Its traffic worth $/M</Table.Th>
                <Table.Th ta="right">Needed tok/min</Table.Th><Table.Th ta="right">≈ requests/h</Table.Th><Table.Th ta="right">Your tok/min (avg · busiest)</Table.Th>
                <Table.Th>Verdict</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{d.models.map((m) => (
                <Table.Tr key={m.model}>
                  <Table.Td><Text size="sm" fw={600}>{m.model}</Text><Text size="xs" c="dimmed">{Object.entries(m.quality).map(([t, q]) => `${t} ${q ? q.toFixed(1) : "–"}`).join(" · ")}</Text>
                    {m.size?.quality_note && <Tooltip label={m.size.quality_note} multiline w={280}><Status kind="warn" style={{ fontSize: 12 }}>quality unmeasured at {m.size.weights}</Status></Tooltip>}</Table.Td>
                  <Table.Td>{m.size ? <Tooltip label={m.size.basis} multiline w={280}><div><Text size="xs">{gb(m.size.weight_bytes)}{m.size.weights ? ` · ${m.size.weights}` : ""}</Text>
                    <Text size="xs" c="dimmed">{compact(m.size.total_params / 1e9)}B · {compact(m.size.active_params / 1e9)}B active
                      {m.size.published_weights && m.size.published_weights !== m.size.weights ? ` · published ${m.size.published_weights}` : ""}</Text></div></Tooltip>
                    : <Text size="xs" c="dimmed">unknown</Text>}</Table.Td>
                  <Table.Td><ReplicaCell g={m.gpu} /></Table.Td>
                  <Table.Td className="num">{m.gpu ? <Tooltip label={m.gpu.capacity_basis === "configured"
                    ? `Configured (measured) capacity${m.gpu.estimated_capacity_tokens_per_second ? `; the estimate would be ${compact(m.gpu.estimated_capacity_tokens_per_second)}` : ""}`
                    : "Estimated from the GPUs' compute and the model's active parameters"}><span>{compact(m.gpu.capacity_tokens_per_second)}{m.gpu.capacity_basis === "estimated" ? "*" : ""}</span></Tooltip> : "–"}</Table.Td>
                  <Table.Td className="num">{perM(m.gpu_usd_per_million)}</Table.Td>
                  <Table.Td className="num">{perM(m.own_api_usd_per_million)}</Table.Td>
                  <Table.Td className="num"><Tooltip label={m.basis}><span>{perM(m.api_usd_per_million)}</span></Tooltip></Table.Td>
                  <Table.Td className="num">{compact(m.tokens_per_minute)}</Table.Td>
                  <Table.Td className="num">{compact(m.requests_per_hour)}</Table.Td>
                  <Table.Td className="num">{compact(m.traffic.tokens_per_minute)} · {compact(m.traffic.peak_tokens_per_minute)}</Table.Td>
                  <Table.Td><Verdict m={m} /></Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          <Text size="xs" c="dimmed" mt="xs">* estimated throughput.</Text>
        </Panel>
      )}
    </Stack>
  );
}

export function Models({ api, me }: { api: Api; me: Me }) {
  const res = useData(() => api.adminConfig(), [api]);
  const evals = useData(() => api.evals(), [api]);
  const [onlyLive, setOnlyLive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<string | null>(location.hash.endsWith("/evals") ? "evals" : location.hash.endsWith("/breakeven") ? "breakeven" : "models");
  const cfg: AdminConfig | null = res.data;
  const models = (cfg?.models ?? []).filter((m) => !onlyLive || m.routes.some((r) => r.live));
  const refresh = async () => {
    setBusy(true);
    try {
      const r = await api.refreshPrices();
      const q = await api.refreshQuality();
      notifications.show({ color: "green", title: "Live data checked", message: `${r.priced_routes} routes priced; ${r.changed.length ? `${r.changed.length} prices changed` : "no prices changed"}`
        + `${q.ok ? `; ${q.changed?.length ? `${q.changed.length} quality scores moved` : "quality scores unchanged"}` : `; quality scores not refreshed (${q.error ?? q.why})`}.` });
      await res.reload();
    } catch (e) {
      notifications.show({ color: "red", title: "Couldn't refresh live data", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageTitle title="Models">
        {tab === "models" && <Group>
          <Switch label="Only models with a live route" checked={onlyLive} onChange={(e) => setOnlyLive(e.currentTarget.checked)} />
          {me.role === "admin" && <Button size="xs" variant="default" loading={busy} onClick={refresh}>Check prices and scores now</Button>}
        </Group>}
      </PageTitle>
      <Tabs value={tab} onChange={(t) => { setTab(t); history.replaceState(null, "", t === "models" || !t ? "#models" : `#models/${t}`); }} mb="md" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="models">Models and routes</Tabs.Tab>
          <Tabs.Tab value="evals">Evals</Tabs.Tab>
          <Tabs.Tab value="breakeven">GPU break-even</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      {tab === "breakeven" ? <BreakevenPanel api={api} admin={me.role === "admin"} /> : tab === "evals" ? <EvalsPanel api={api} me={me} cfg={cfg} /> : <>
      {cfg?.prices && <PriceFreshness s={cfg.prices} />}
      {cfg?.quality && <QualityFreshness q={cfg.quality} />}
      {cfg && <Alert variant="light" color="gray" mb="md">Baseline for savings: <b>{cfg.baseline_model ?? "none"}</b>. Prices are dollars per million tokens (OCR models: per page). Routes without a known price are never chosen by <code>auto</code>.
        {" "}Under each context window: how the model's prompt tokens compare with the gateway's estimate (×1.00: the estimate was right), learned from its replies.
        {" "}Quality scores (1–5) drive <code>auto</code> routing: a task at level 3 goes only to models scoring at least 3. To check them on your own tasks, use
        the <Anchor component="button" onClick={() => { setTab("evals"); history.replaceState(null, "", "#models/evals"); }}>Evals</Anchor> tab.</Alert>}
      <Panel title="Models and routes">
        <Loading loading={res.loading} error={res.error} />
        <Table.ScrollContainer minWidth={900}>
          <Table verticalSpacing={5} fz="sm">
            <Table.Thead><Table.Tr><Table.Th>Model</Table.Th><Table.Th>Quality</Table.Th><Table.Th>Measured</Table.Th><Table.Th ta="right">Context</Table.Th><Table.Th>Route</Table.Th>
              <Table.Th ta="right">Input</Table.Th><Table.Th ta="right">Cached</Table.Th><Table.Th ta="right">Output</Table.Th><Table.Th>Price from</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {models.flatMap((m) => m.routes.map((r, i) => (
                <Table.Tr key={`${m.name}-${r.backend}`} style={{ opacity: r.live ? 1 : 0.5 }}>
                  <Table.Td>{i === 0 && <Text fw={600} size="sm">{m.name}{m.kind === "ocr" && <Badge ml={6} size="xs" variant="default">OCR</Badge>}</Text>}</Table.Td>
                  <Table.Td>{i === 0 && <QualityBadges m={m} q={cfg?.quality} />}</Table.Td>
                  <Table.Td>{i === 0 && evals.data?.models[m.name] && (() => {
                    const e = evals.data.models[m.name];
                    // Amber: the score in use is outside the 90% range your own eval measured.
                    const outside = (t: string) => {
                      const v = e.tasks[t];
                      const q = m.quality[t];
                      return !!q && v?.low != null && v.high != null && (q < v.low || q > v.high);
                    };
                    const scored = Object.entries(e.tasks).filter(([, v]) => v.score != null);
                    return (
                      <Group gap={4} title={`${e.suite} suite, ${new Date(e.ran_at).toLocaleString()}, ${pct(e.pass_rate)} passed; 90% ranges: `
                        + scored.map(([t, v]) => `${t} ${v.low?.toFixed(1)}–${v.high?.toFixed(1)}`).join(", ")}>
                        {scored.map(([t, v]) => <Badge key={t} size="sm" color={outside(t) ? "yellow" : "gray"}>{t} {v.score?.toFixed(1)}</Badge>)}
                        {!scored.length && <Text size="xs" c="dimmed">too few hard tasks</Text>}
                        {scored.some(([t]) => outside(t)) && <Status kind="warn" style={{ fontSize: 12 }}>differs</Status>}
                      </Group>
                    );
                  })()}</Table.Td>
                  <Table.Td className="num">{i === 0 && <>{m.context_window ? `${Math.round(m.context_window / 1000)}k` : ""}
                    {(m.kind ?? "chat") === "chat" && <TokenRatio t={m.tokens} />}</>}</Table.Td>
                  <Table.Td>{r.backend} <Text span size="xs" c="dimmed">{r.model}</Text></Table.Td>
                  {r.price?.per_page != null ? <>
                    <Table.Td className="num" title="Dollars a page">{r.price.per_page.toFixed(4)}/page</Table.Td>
                    <Table.Td className="num">–</Table.Td>
                    <Table.Td className="num" title="Dollars a page with annotations (structured output)">{r.price.per_annotated_page != null ? `${r.price.per_annotated_page.toFixed(4)} annotated` : ""}</Table.Td>
                  </> : <>
                    <Table.Td className="num">{r.price ? r.price.input.toFixed(3) : <StatusBadge size="sm" kind="warn">unpriced</StatusBadge>}</Table.Td>
                    <Table.Td className="num">{r.price?.cache_read != null ? r.price.cache_read.toFixed(3) : "–"}</Table.Td>
                    <Table.Td className="num">{r.price ? r.price.output.toFixed(3) : ""}</Table.Td>
                  </>}
                  <Table.Td>{r.price && <PriceSource p={r.price} />}</Table.Td>
                </Table.Tr>
              )))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Panel>
      </>}
    </>
  );
}
