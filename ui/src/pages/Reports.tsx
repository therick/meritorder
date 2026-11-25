import { Anchor, Button, Grid, Group, Select, SegmentedControl, SimpleGrid, Stack, Table, Tabs, Text, TextInput } from "@mantine/core";
import { useMemo, useState } from "react";
import type { Api, Me, Range, Row } from "../api";
import { CAUSES, GPU_IDLE_KEY, cacheHitRate, compact, num, pct, presetRange, savingsRate, usd, vsBaseline } from "../api";
import { HBars, type Series, SpendBars, StackedBars } from "../components/charts";
import { AnsweredBy, Kpi, Loading, PageTitle, Panel, SpendSplit } from "../components/common";
import { Icon } from "../components/icons";
import { useData } from "../hooks";
import { Batches } from "./Batches";
import { Bills } from "./Bills";
import { LearnedRouter } from "./LearnedRouter";
import { Levels } from "./Levels";
import { RequestsView } from "./RequestsView";
import { Person } from "../people";

const PRESETS = [{ value: "month", label: "This month" }, { value: "7d", label: "Last 7 days" }, { value: "30d", label: "Last 30 days" }, { value: "last-month", label: "Last month" }];

function days(range: Range): string[] {
  const out: string[] = [];
  for (let t = Date.parse(range.start); t < Date.parse(range.end); t += 864e5) out.push(new Date(t).toISOString().slice(0, 10));
  return out;
}

const SERIES_COLORS = ["var(--mo-s1)", "var(--mo-s2)", "var(--mo-s3)", "var(--mo-s4)"];

/** Savings by cause, a day at a time: the four largest causes in the period take the categorical colors, the rest
 * share a muted "Other". */
function causeSeries(t: Row): Series[] {
  const ranked = CAUSES.filter(([k]) => t[k] !== 0).sort((a, b) => Math.abs(t[b[0]]) - Math.abs(t[a[0]]));
  const top: Series[] = ranked.slice(0, 4).map(([k, label], i) => ({ key: k, label, color: SERIES_COLORS[i] }));
  return ranked.length > 4 ? [...top, { key: "other", label: "Other", color: "var(--mo-bar)" }] : top;
}

/** A row's name. The GPU time no request paid for (decision 64) is the organization's, not a team's or a person's: its
 * row says so. */
/** A row's key; a person (``person``) by name. */
function RowKey({ k, person = false }: { k: string; person?: boolean }) {
  if (person && k !== GPU_IDLE_KEY) return <Person id={k} />;
  if (k !== GPU_IDLE_KEY) return <>{k}</>;
  return <Text span inherit c="dimmed" data-tip="GPU time no request paid for: starting, idle, draining, and capacity requests left unused. The organization's, so no team or person carries it." tabIndex={0}>{k}</Text>;
}

/** What was saved, or how much more it cost, against the baseline: for a KPI's line under the amount. */
function savedLine(t: Row): string {
  if (t.savings_total >= 0) return `${pct(savingsRate(t))} of the baseline`;
  const more = vsBaseline(t.actual_cost, t.baseline_cost);
  return `cost ${usd(-t.savings_total)} more than without Merit Order${more ? ` (${more})` : ""}`;
}

/** The biggest spenders by team or user over the period: spend with a bar against the largest, and what was saved. */
function TopTable({ rows, what, onMore }: { rows: Row[]; what: "team" | "user"; onMore: () => void }) {
  const top = [...rows].sort((a, b) => b.actual_cost - a.actual_cost).slice(0, 8);
  const max = top[0]?.actual_cost || 1;
  return (
    <Panel title={what === "team" ? "Top teams" : "Top users"} flush
      footer={<><span>{rows.length > top.length ? `top ${top.length} of ${rows.length}` : `${rows.length} ${what}${rows.length === 1 ? "" : "s"}`}</span>
        <Anchor size="xs" onClick={onMore}>Breakdown by {what}</Anchor></>}>
      {!rows.length ? <Text size="sm" c="dimmed" p="md">No requests in this period.</Text> : (
        <Table.ScrollContainer minWidth={560}>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{what === "team" ? "Team" : "User"}</Table.Th><Table.Th ta="right">Requests</Table.Th><Table.Th ta="right" w={120}>Spent</Table.Th>
                <Table.Th ta="right">Without Merit Order</Table.Th><Table.Th ta="right">Saved</Table.Th><Table.Th ta="right">Saved %</Table.Th>
                {what === "team" && <Table.Th ta="right">Cache hits</Table.Th>}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {top.map((r) => (
                <Table.Tr key={String(r.key)}>
                  <Table.Td><RowKey k={String(r.key)} person={what === "user"} /></Table.Td>
                  <Table.Td className="num">{num(r.requests)}</Table.Td>
                  <Table.Td className="num">{usd(r.actual_cost)}<div className="mini acc"><i style={{ width: `${(r.actual_cost / max) * 100}%` }} /></div></Table.Td>
                  <Table.Td className="num" c="dimmed">{usd(r.baseline_cost)}</Table.Td>
                  <Table.Td className="num">{usd(r.savings_total)}</Table.Td>
                  <Table.Td className="num">{pct(savingsRate(r))}</Table.Td>
                  {what === "team" && <Table.Td className="num">{pct(cacheHitRate(r))}</Table.Td>}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Panel>
  );
}

function Overview({ api, range, me, onBreakdown }: { api: Api; range: Range; me: Me; onBreakdown: (dim: string) => void }) {
  const daily = useData(() => api.summary(range, "day"), [api, range.start, range.end]);
  const byModel = useData(() => api.summary(range, "model"), [api, range.start, range.end]);
  // Top teams and users: what Breakdown groups by, for everyone who can see more than their own requests.
  const wide = me.role !== "user";
  const byTeam = useData(() => (wide ? api.summary(range, "team") : Promise.resolve(null)), [api, range.start, range.end, wide]);
  const byUser = useData(() => (wide ? api.summary(range, "user") : Promise.resolve(null)), [api, range.start, range.end, wide]);
  const t = daily.data?.totals;
  const series = useMemo(() => {
    const m = new Map((daily.data?.rows ?? []).map((r) => [String(r.key).slice(0, 10), r]));
    return days(range).map((d) => ({ label: d, actual: m.get(d)?.actual_cost ?? 0, gpu: m.get(d)?.gpu_cost ?? 0, baseline: m.get(d)?.baseline_cost ?? 0 }));
  }, [daily.data, range]);
  const causes = useMemo(() => (t ? causeSeries(t) : []), [t]);
  const causeDays = useMemo(() => {
    const m = new Map((daily.data?.rows ?? []).map((r) => [String(r.key).slice(0, 10), r]));
    const named = new Set(causes.map((c) => c.key));
    return days(range).map((d) => {
      const r = m.get(d);
      const values: Record<string, number> = {};
      for (const [k] of CAUSES) {
        const v = r ? r[k] : 0;
        if (named.has(k)) values[k] = v; else values.other = (values.other ?? 0) + v;
      }
      return { label: d, values };
    });
  }, [daily.data, range, causes]);
  return (
    <Stack gap="md">
      <Loading loading={daily.loading} error={daily.error} />
      {t && (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
          <Kpi label="Spent" value={usd(t.actual_cost)} sub={<><div>{num(t.requests)} requests</div><SpendSplit api={t.api_cost} gpu={t.gpu_cost} idle={t.gpu_idle_cost} /></>} />
          <Kpi label="Without Merit Order" value={usd(t.baseline_cost)} sub="same work on the team's default model" />
          <Kpi label="Saved" value={usd(t.savings_total)} accent={t.savings_total >= 0} sub={savedLine(t)} />
          <Kpi label="Cache hit rate" value={pct(cacheHitRate(t))} sub={`${compact(t.cache_read_tokens)} cached tokens read`} />
        </SimpleGrid>
      )}
      <Panel title="Daily spend vs. cost without Merit Order"
        right={<div className="legend"><span><i style={{ background: "var(--mo-accent)" }} />Spent{t?.gpu_cost ? " on APIs" : ""}</span>
          {!!t?.gpu_cost && <span><i style={{ background: "var(--mo-accent)", opacity: 0.45 }} />on GPUs</span>}<span><i style={{ background: "var(--mo-bar-2)", border: "1px solid var(--mo-line-strong)" }} />Without Merit Order</span></div>}
        footer={<><span>{new Date(range.start).toLocaleDateString(undefined, { month: "short", day: "numeric" })} to {new Date(Date.parse(range.end) - 864e5).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</span><AnsweredBy by={daily.data?.answered_by} /></>}>
        <SpendBars days={series} />
      </Panel>
      <Grid gap="md">
        <Grid.Col span={{ base: 12, md: 7 }}>
          <Panel title="Savings by cause" footer={<span>Only caching Merit Order added counts; caching the client set up itself would have happened anyway.
            Self-hosting is the API cost of the requests GPUs served, less what the GPUs cost{me.role === "admin" || me.role === "finance" ? ", idle and starting time included" : " (your requests' shares)"}: below zero when they cost more.</span>}>
            {t && (causes.length ? <StackedBars days={causeDays} series={causes} /> : <Text c="dimmed" size="sm">Nothing saved in this period yet.</Text>)}
          </Panel>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 5 }}>
          <Panel title="Spend by model" right={<Text size="xs" c="dimmed">requests after the amount</Text>}>
            <Loading loading={byModel.loading} error={byModel.error} />
            {byModel.data && <HBars items={[...byModel.data.rows].sort((a, b) => b.actual_cost - a.actual_cost).slice(0, 10)
              .map((r) => ({ label: String(r.key), value: r.actual_cost, note: `${num(r.requests)}` }))} />}
          </Panel>
        </Grid.Col>
        {wide && (
          <>
            <Grid.Col span={{ base: 12, lg: 6 }}>
              <Loading loading={byTeam.loading && !byTeam.data} error={byTeam.error} />
              {byTeam.data && <TopTable rows={byTeam.data.rows} what="team" onMore={() => onBreakdown("team")} />}
            </Grid.Col>
            <Grid.Col span={{ base: 12, lg: 6 }}>
              <Loading loading={byUser.loading && !byUser.data} error={byUser.error} />
              {byUser.data && <TopTable rows={byUser.data.rows} what="user" onMore={() => onBreakdown("user")} />}
            </Grid.Col>
          </>
        )}
      </Grid>
    </Stack>
  );
}

function Breakdown({ api, range, me, initial }: { api: Api; range: Range; me: Me; initial?: string | null }) {
  const dims = me.role === "user" ? ["model", "backend", "day"] : ["team", "user", "model", "backend", "day"];
  const [dim, setDim] = useState(initial && dims.includes(initial) ? initial : dims[0]);
  const [tag, setTag] = useState("project");
  const groupBy = dim === "tag" ? `tag:${tag}` : dim;
  const res = useData(() => api.summary(range, groupBy), [api, range.start, range.end, groupBy]);
  const rows = useMemo(() => [...(res.data?.rows ?? [])].sort((a, b) => b.actual_cost - a.actual_cost), [res.data]);
  const gpus = rows.some((r) => r.gpu_cost);  // the APIs and GPUs columns, when anything ran on GPUs
  const pages = rows.some((r: Row) => (r.pages ?? 0) > 0);  // OCR pages, shown once there are any
  return (
    <Panel title="Breakdown" right={<Group gap="sm" wrap="nowrap"><AnsweredBy by={res.data?.answered_by} />
      <Button size="xs" variant="default" leftSection={<Icon name="down" size={12} />} onClick={() => api.downloadCsv(range, groupBy)}>Download CSV</Button></Group>}>
      <Group mb="sm" gap="sm">
        <SegmentedControl size="xs" value={dim} onChange={setDim}
          data={[...dims, "tag"].map((d) => ({ value: d, label: d === "tag" ? "Tag" : d[0].toUpperCase() + d.slice(1) }))} />
        {dim === "tag" && <TextInput size="xs" value={tag} onChange={(e) => setTag(e.currentTarget.value.replace(/[^A-Za-z0-9_.-]/g, ""))} aria-label="Tag name" w={140} />}
      </Group>
      <Loading loading={res.loading} error={res.error} />
      <Table.ScrollContainer minWidth={760}>
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{dim === "tag" ? tag : dim}</Table.Th><Table.Th ta="right">Requests</Table.Th><Table.Th ta="right">Spent</Table.Th>
              {gpus && <><Table.Th ta="right">APIs</Table.Th><Table.Th ta="right">GPUs</Table.Th></>}
              <Table.Th ta="right">Without Merit Order</Table.Th><Table.Th ta="right">Saved</Table.Th><Table.Th ta="right">Saved %</Table.Th>
              <Table.Th ta="right">Cache hits</Table.Th><Table.Th ta="right">Tokens</Table.Th>
              {pages && <Table.Th ta="right" title="Pages read by OCR (/v1/ocr)">Pages</Table.Th>}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((r: Row) => (
              <Table.Tr key={String(r.key)}>
                <Table.Td>{dim === "day" ? String(r.key).slice(0, 10) : <RowKey k={String(r.key)} person={dim === "user"} />}</Table.Td>
                <Table.Td className="num">{num(r.requests)}</Table.Td>
                <Table.Td className="num">{usd(r.actual_cost)}</Table.Td>
                {gpus && <><Table.Td className="num" c="dimmed">{usd(r.api_cost)}</Table.Td><Table.Td className="num" c="dimmed">{usd(r.gpu_cost)}</Table.Td></>}
                <Table.Td className="num">{usd(r.baseline_cost)}</Table.Td>
                <Table.Td className="num">{usd(r.savings_total)}</Table.Td>
                <Table.Td className="num">{pct(savingsRate(r))}</Table.Td>
                <Table.Td className="num">{pct(cacheHitRate(r))}</Table.Td>
                <Table.Td className="num">{compact(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens)}</Table.Td>
                {pages && <Table.Td className="num">{r.pages ? compact(r.pages) : "–"}</Table.Td>}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {!res.loading && !rows.length && <Text c="dimmed" size="sm" mt="sm">No requests in this period.</Text>}
    </Panel>
  );
}

export function Reports({ api, me }: { api: Api; me: Me }) {
  const [preset, setPreset] = useState("month");
  const range = useMemo(() => presetRange(preset), [preset]);
  // The tab is in the URL (#reports/requests?...): the Requests tab keeps its filters there too.
  const [tab, setTabState] = useState<string | null>(() => location.hash.slice(1).split("?")[0].split("/")[1] || "overview");
  const setTab = (t: string | null) => {
    setTabState(t);
    history.replaceState(null, "", !t || t === "overview" ? "#reports" : `#reports/${t}`);
  };
  const [dim, setDim] = useState<string | null>(null);  // Overview's "Breakdown by team" opens Breakdown on that dimension
  const scope = me.role === "admin" || me.role === "finance" ? "all teams" : me.role === "team_lead" ? `team ${me.team}` : "your requests";
  return (
    <>
      <PageTitle title="Reports" sub={scope}>
        <Group gap="sm">
          {tab !== "requests" && <Select size="sm" data={PRESETS} value={preset} onChange={(v) => v && setPreset(v)} allowDeselect={false} w={150} aria-label="Period" />}
          {(me.role === "team_lead" || me.role === "admin") && <Button size="sm" variant="default" leftSection={<Icon name="down" size={13} />} onClick={() => api.downloadLedger(range)}>Export ledger (JSONL)</Button>}
        </Group>
      </PageTitle>
      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List mb="md">
          <Tabs.Tab value="overview">Overview</Tabs.Tab>
          <Tabs.Tab value="breakdown">Breakdown</Tabs.Tab>
          {me.role !== "finance" && <Tabs.Tab value="requests">Requests</Tabs.Tab>}
          <Tabs.Tab value="batches">Batches</Tabs.Tab>
          <Tabs.Tab value="levels">Levels</Tabs.Tab>
          {(me.role === "finance" || me.role === "admin") && <Tabs.Tab value="bills">Bills</Tabs.Tab>}
          {(me.role === "team_lead" || me.role === "admin") && <Tabs.Tab value="router">Learned router</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="overview"><Overview api={api} range={range} me={me} onBreakdown={(d) => { setDim(d); setTab("breakdown"); }} /></Tabs.Panel>
        <Tabs.Panel value="breakdown"><Breakdown api={api} range={range} me={me} initial={dim} /></Tabs.Panel>
        <Tabs.Panel value="requests"><RequestsView api={api} me={me} /></Tabs.Panel>
        <Tabs.Panel value="batches"><Batches api={api} me={me} range={range} /></Tabs.Panel>
        <Tabs.Panel value="levels"><Levels api={api} me={me} /></Tabs.Panel>
        <Tabs.Panel value="bills"><Bills api={api} me={me} range={range} /></Tabs.Panel>
        <Tabs.Panel value="router"><LearnedRouter api={api} me={me} /></Tabs.Panel>
      </Tabs>
    </>
  );
}
