import { Anchor, Badge, Button, Grid, Paper, SegmentedControl, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { useState } from "react";
import type { Api, AttentionItem, BackendStatus, Deployment, FeedEvent, Me } from "../api";
import { ms, pct, usd, vsBaseline } from "../api";
import { MeritChart, MeritTable } from "../components/charts";
import { Kpi, Loading, Panel, SpendSplit, StateBadge, Status, StatusBadge, type StatusKind } from "../components/common";
import { Icon } from "../components/icons";
import { useData } from "../hooks";
import { Person } from "../people";

const LEVEL_KIND: Record<string, StatusKind> = { critical: "bad", warning: "warn", info: "info" };
const LEVEL_WORD: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

function time(ts: string) {
  return new Date(ts).toLocaleTimeString(undefined, { hour12: false });
}

/** A backend's last five minutes: its name, its state as an icon and a word, latency and error rate in mono. */
export function BackendTile({ b }: { b: BackendStatus }) {
  const skipped = Object.keys(b.skipped_models ?? {});
  const failing = b.circuit_open || (b.requests >= 5 && b.error_rate >= 0.2);
  const state = !b.enabled ? "off" : failing ? "bad" : b.error_rate > 0.02 || b.draining || skipped.length ? "warn" : "good";
  const tip = !b.enabled ? (b.reason ?? "Switched off in the configuration")
    : b.circuit_open ? (b.skipped?.why ?? "failing")
    : skipped.length ? Object.entries(b.skipped_models ?? {}).map(([m, s]) => `${m}: ${s.why}`).join("\n")
    : `${b.name}: ${b.requests ? `${b.requests.toLocaleString()} requests, ${pct(b.error_rate)} errors` : "no requests"} in the last 5 minutes`;
  const word = !b.enabled ? "Off" : b.circuit_open ? "Failing" : skipped.length ? `${skipped.length} model${skipped.length > 1 ? "s" : ""} skipped`
    : b.draining ? "Draining" : state === "bad" || state === "warn" ? `${pct(b.error_rate)} errors` : "Healthy";
  return (
    <div className={`tile ${state === "good" ? "" : state}`} data-tip={tip} tabIndex={0}>
      <div className="tile-s">
        <span className="tile-n">{b.name}</span>
        {b.enabled && <span className="mono">{b.avg_latency_ms != null ? `${ms(b.avg_latency_ms)} avg` : "idle"}</span>}
      </div>
      <div className="tile-s">
        <Status kind={state}>{word}</Status>
        <span className="mono">{!b.enabled ? (b.reason ? "no credentials" : "") : b.requests ? `${pct(b.error_rate)} err` : ""}</span>
      </div>
    </div>
  );
}

/** One request in the live feed: when, who, what was asked for and what served it, where, at which level, what it cost,
 * what it saved against the team's default model, and how long it took. */
export function FeedRow({ e }: { e: FeedEvent }) {
  const failed = e.status >= 400;
  return (
    <Table.Tr>
      <Table.Td c="dimmed">{time(e.ts)}</Table.Td>
      <Table.Td className="sans">
        <div style={{ lineHeight: 1.25 }}><Person id={e.user} /></div>
        <Text size="xs" c="dimmed">{e.team}</Text>
      </Table.Td>
      <Table.Td className="sans" title={e.error ?? ""}>
        <Text span inherit c="dimmed">{e.kind === "embeddings" ? "embed" : e.kind === "ocr" ? "ocr" : e.requested || "?"}</Text> <Text span c="dimmed"><Icon name="arrow" size={12} /></Text> {e.model}
        {!!e.pages && <Text span size="xs" c="dimmed"> · {e.pages} {e.pages === 1 ? "page" : "pages"}</Text>}
        {e.upgraded && <Badge ml={6} size="sm" color="mo" data-tip="Served by a better model at no extra cost">free upgrade</Badge>}
        {failed && <StatusBadge ml={6} size="sm" kind="bad" data-tip={e.error ?? `HTTP ${e.status}`}>{e.status}</StatusBadge>}
      </Table.Td>
      <Table.Td className="sans" c="dimmed">{e.backend}</Table.Td>
      <Table.Td title={e.reason ?? ""}>{e.floor ?? <Text span inherit c="dimmed">none</Text>}</Table.Td>
      <Table.Td ta="right">{usd(e.cost)}</Table.Td>
      <Table.Td ta="right" c={e.saved ? (e.saved < 0 ? "dimmed" : "var(--mo-fg-2)") : "dimmed"}>{usd(e.saved ?? 0)}</Table.Td>
      <Table.Td ta="right" c="dimmed">{ms(e.latency_ms)}</Table.Td>
    </Table.Tr>
  );
}

function Attention({ items, onNavigate }: { items: AttentionItem[]; onNavigate: (page: string) => void }) {
  if (!items.length) return <Status kind="good">Nothing needs attention.</Status>;
  return (
    <ul className="att-list">
      {items.slice(0, 6).map((a, i) => (
        <li key={i} className="att">
          <Status kind={LEVEL_KIND[a.level] ?? "info"} className="att-i" aria-label={LEVEL_WORD[a.level] ?? a.level} />
          <div style={{ minWidth: 0 }}>
            <div className="att-t">{a.title}</div>
            <div className="att-d">{a.detail}</div>
          </div>
          {a.page ? <Button size="compact-xs" variant="subtle" color="gray" onClick={() => onNavigate(a.page!)}>Open</Button> : <span />}
        </li>
      ))}
    </ul>
  );
}

function GpuFleet({ deps }: { deps: Deployment[] }) {
  if (!deps.length) return <Text size="sm" c="dimmed">No self-hosted backends configured. Use the what-if calculator on the GPUs page to see if one would pay off.</Text>;
  return (
    <Stack gap={14}>
      {deps.map((d) => {
        const fleet = (d.gpu_usd_per_hour ?? 0) * Math.max(d.replicas ?? 0, 1);
        const cover = fleet ? Math.min(100, ((d.demand_usd_per_hour ?? 0) / fleet) * 100) : 0;
        return (
          <div key={d.backend} style={{ minWidth: 0 }}>
            <div className="tile-s">
              <Text span className="mono" size="sm" truncate="end">{d.backend} <Text span inherit c="dimmed">· {d.model}</Text></Text>
              <StateBadge state={d.state} />
            </div>
            <div className={d.state === "warm" ? "mini acc" : "mini"}><i style={{ width: `${d.utilization != null ? d.utilization * 100 : cover}%` }} /></div>
            <Text className="mono" size="xs" c="dimmed" mt={5}>
              {d.utilization != null ? `util ${pct(d.utilization)} · ` : ""}demand {usd(d.demand_usd_per_hour ?? 0)}/h vs GPU {usd(d.gpu_usd_per_hour ?? 0)}/h × {d.replicas ?? 0}
            </Text>
          </div>
        );
      })}
    </Stack>
  );
}

export function Dispatch({ api, me, onNavigate }: { api: Api; me: Me; onNavigate: (page: string) => void }) {
  // Finance sees aggregates only (docs/06): the request feed is per-request rows, so the gateway refuses it to them.
  const aggregatesOnly = me.role === "finance";
  const [window, setWindow] = useState("15");
  const ov = useData(() => api.overview(), [api], 10_000);
  const merit = useData(() => api.merit(Number(window)), [api, window], 15_000);
  const feed = useData(() => (aggregatesOnly ? Promise.resolve({ events: [] }) : api.feed(40)), [api, aggregatesOnly], 3_000);
  const backends = useData(() => api.backends(), [api], 10_000);
  const att = useData(() => api.attention(), [api], 30_000);
  const gpu = useData(() => api.gpu(), [api], 15_000);
  const o = ov.data;
  const causes = o?.month.savings_by_cause ?? {};
  // The causes in dollars, signed: self-hosting is after what the GPUs cost, so it can be below zero (decision 64).
  const causeLine = ["routing", "caching", "self_hosting", "batch"]
    .filter((c) => Math.abs(causes[c] ?? 0) >= 0.005)
    .map((c) => `${c.replace("_", "-")} ${usd(causes[c] ?? 0)}`)
    .join(" · ");
  const delta = o ? vsBaseline(o.today.spend, o.today.baseline) : null;
  const deps = gpu.data?.deployments.filter((d) => d.enabled || d.state !== "disabled") ?? [];
  const running = deps.reduce((s, d) => s + (d.replicas ?? 0), 0);
  const burn = deps.reduce((s, d) => s + (d.burn_usd_per_hour ?? (d.gpu_usd_per_hour ?? 0) * (d.replicas ?? 0)), 0);

  return (
    <Stack gap="md">
      <Text component="h1" className="sr-only">Dispatch</Text>
      <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
        <Kpi label="Spend today" value={o ? usd(o.today.spend) : "…"} sub={o && <>
          <SpendSplit api={o.today.api} gpu={o.today.gpu} idle={o.today.gpu_idle} />
          <div>without Merit Order {usd(o.today.baseline)}{delta && <> · <span className={o.today.saved >= 0 ? "delta" : "delta neg"}>{delta}</span></>}</div>
        </>} />
        <Kpi label={o?.scope === "organization" ? "Spend this month" : `Spend this month · ${o?.scope ?? ""}`} value={o ? usd(o.month.spend) : "…"}
          sub={o && <>
            <SpendSplit api={o.month.api} gpu={o.month.gpu} idle={o.month.gpu_idle} />
            <div>{o.forecast ? `pace ${usd(o.forecast)}${o.budget ? ` / budget ${usd(o.budget)}` : ""}` : `${o.month.requests.toLocaleString()} requests`}</div>
            {!!o.reserved && <div>{usd(o.reserved)} reserved by running batches</div>}
          </>} />
        <Kpi label="Saved this month" value={o ? usd(o.month.saved) : "…"} accent={!o || o.month.saved >= 0}
          sub={causeLine || "routing, caching, self-hosting after GPU cost"} />
        <Kpi label="Prompt cache hit rate" value={o ? pct(o.month.cache_hit_rate) : "…"}
          sub={o && `${o.requests_per_minute.toLocaleString()} req/min · ${o.gateways} gateway${o.gateways === 1 ? "" : "s"}`} />
      </SimpleGrid>

      <Grid gap="md">
        <Grid.Col span={{ base: 12, lg: 8 }}>
          <Panel title="Merit order" hint="what a token cost, cheapest first"
            right={<SegmentedControl value={window} onChange={setWindow} data={[{ label: "15 min", value: "15" }, { label: "1 h", value: "60" }, { label: "4 h", value: "240" }]} />}>
            <Loading loading={merit.loading && !merit.data} error={merit.error} />
            {merit.data && <MeritChart tiers={merit.data.tiers} demand={merit.data.demand_tokens_per_second} clearing={merit.data.clearing_price_per_m} />}
            {merit.data?.blended_price_per_m != null && (
              <Text className="mono" size="xs" c="dimmed" mt={8}>
                blended {usd(merit.data.blended_price_per_m)}/M · each bar is a model: width = tokens it served, height = what its tokens actually cost after caching
              </Text>
            )}
            {merit.data && <MeritTable tiers={merit.data.tiers} />}
          </Panel>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 4 }}>
          <Stack gap="md">
            <Panel title="Needs attention" right={att.data && <Badge variant="default" size="sm">{att.data.items.length}</Badge>}>
              <Loading loading={att.loading && !att.data} error={att.error} />
              {att.data && <Attention items={att.data.items} onNavigate={onNavigate} />}
            </Panel>
            <Panel title="GPU fleet" footer={gpu.data && deps.length > 0 && <>
              <span>{running} replica{running === 1 ? "" : "s"} · {usd(burn)}/h</span>
              <Anchor size="xs" onClick={() => onNavigate("gpus")}>GPUs</Anchor>
            </>}>
              <Loading loading={gpu.loading && !gpu.data} error={gpu.error} />
              {gpu.data && <GpuFleet deps={deps} />}
            </Panel>
          </Stack>
        </Grid.Col>

        <Grid.Col span={{ base: 12, lg: 8 }}>
          <Panel title="Live dispatch" flush right={<span className="live"><span className="live-dot" aria-hidden="true" />refreshes every 3 s</span>}>
            {(feed.error || (feed.loading && !feed.data)) && <div style={{ padding: 16 }}><Loading loading={feed.loading && !feed.data} error={feed.error} /></div>}
            {aggregatesOnly && <Text size="sm" c="dimmed" p="md">Finance sees spend in aggregate, so the request feed isn't shown. Reports has the totals.</Text>}
            {!aggregatesOnly && feed.data && !feed.data.events.length && <Text size="sm" c="dimmed" p="md">No requests yet.</Text>}
            {feed.data && feed.data.events.length > 0 && (
              <Table.ScrollContainer minWidth={860} maxHeight={640} type="native">
                <Table className="feed-table">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Time</Table.Th><Table.Th>User / team</Table.Th><Table.Th>Asked for, served</Table.Th><Table.Th>Backend</Table.Th>
                      <Table.Th>Level</Table.Th><Table.Th ta="right">Cost</Table.Th><Table.Th ta="right">Saved</Table.Th><Table.Th ta="right">Latency</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>{feed.data.events.map((e, i) => <FeedRow key={`${e.ts}-${i}`} e={e} />)}</Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            )}
          </Panel>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 4 }}>
          <Panel title="Backends" hint="last 5 min" right={backends.data && <Text className="mono" size="xs" c="var(--mo-fg-2)">{backends.data.backends.filter((b) => b.live).length}/{backends.data.backends.length} live</Text>}
            footer={<><span>Hover a backend for details</span><Anchor size="xs" onClick={() => onNavigate("backends")}>All backend details</Anchor></>}>
            <Loading loading={backends.loading && !backends.data} error={backends.error} />
            <div className="tiles">
              {backends.data?.backends
                .slice()
                .sort((a, b) => Number(b.enabled) - Number(a.enabled) || b.requests - a.requests)
                .map((b) => <BackendTile key={b.name} b={b} />)}
            </div>
          </Panel>
        </Grid.Col>
      </Grid>
      {ov.error && <Paper><Status kind="bad">{ov.error}</Status></Paper>}
    </Stack>
  );
}
