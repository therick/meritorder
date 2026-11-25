import { Anchor, Autocomplete, Badge, Button, Collapse, Drawer, Group, NumberInput, Select, Stack, Table, Text, TextInput } from "@mantine/core";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { Api, LedgerRow, Me, RequestQuery } from "../api";
import { CAUSES, compact, ms, num, usd } from "../api";
import { AnsweredBy, Loading, Panel, StatusBadge } from "../components/common";
import { Icon } from "../components/icons";
import { useData } from "../hooks";
import { Person, usePersonLabel } from "../people";

/** The Requests tab (docs/08-reporting.md#the-requests-view): the ledger a page at a time, filtered on the server, with the
 * filters and the cursor in the URL (#reports/requests?...), so a view can be shared and survives a refresh. */

const TIME_PRESETS = [
  { value: "1h", label: "Last hour" }, { value: "24h", label: "Last 24 hours" }, { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" }, { value: "month", label: "This month" }, { value: "all", label: "All time" },
  { value: "custom", label: "Custom" },
];
const PRESET_HOURS: Record<string, number> = { "1h": 1, "24h": 24, "7d": 168, "30d": 720 };
const PAGE_SIZES = ["25", "50", "100", "200"];
const SORTS = [{ value: "time", label: "Time" }, { value: "cost", label: "Cost" }, { value: "savings", label: "Savings" }, { value: "latency", label: "Latency" }];
const YES_NO = [{ value: "", label: "Any" }, { value: "true", label: "Yes" }, { value: "false", label: "No" }];
const STATUSES = ["success", "error", "4xx", "5xx", "400", "401", "403", "413", "429", "500", "502", "529"];
const FORMATS = ["anthropic", "openai", "responses", "gemini", "embeddings", "ocr"];
// Filter keys kept in the URL, beside preset, tags (comma-separated tag filters), limit, sort, order and cursor.
const TEXT_KEYS = ["start", "end", "team", "user", "key", "session", "requested_model", "served_model", "backend", "format", "status", "task",
  "level", "reason", "min_cost", "max_cost", "min_savings", "max_savings", "savings_cause", "min_latency", "max_latency", "stream", "batch",
  "cache_hit", "failover"] as const;
const MORE_KEYS = ["key", "session", "requested_model", "backend", "format", "task", "level", "min_cost", "max_cost", "min_savings", "max_savings",
  "savings_cause", "min_latency", "max_latency", "stream", "batch", "cache_hit", "failover"];
const DEFAULTS: Record<string, string> = { preset: "24h", limit: "50", sort: "time", order: "desc" };
type State = Record<string, string>;

function readUrl(): State {
  const query = location.hash.split("?")[1] ?? "";
  const s: State = { ...DEFAULTS };
  for (const [k, v] of new URLSearchParams(query)) s[k] = v;
  return s;
}

function writeUrl(s: State) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(s)) if (v && DEFAULTS[k] !== v) q.set(k, v);
  const str = q.toString();
  history.replaceState(null, "", `#reports/requests${str ? `?${str}` : ""}`);
}

/** The time range a state stands for: a preset counts back from when the filters were set. */
function timeRange(s: State, now = Date.now()): { start?: string; end?: string } {
  if (s.preset === "custom") return { start: s.start || undefined, end: s.end || undefined };
  if (s.preset === "all") return {};
  if (s.preset === "month") {
    const d = new Date(now);
    return { start: new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)).toISOString() };
  }
  return { start: new Date(now - (PRESET_HOURS[s.preset] ?? 24) * 3600e3).toISOString() };
}

function toQuery(s: State, range: { start?: string; end?: string }): RequestQuery {
  const q: RequestQuery = {};
  for (const k of TEXT_KEYS) if (s[k] && k !== "start" && k !== "end") (q as Record<string, string>)[k] = s[k];
  Object.assign(q, range);
  const tags = (s.tags ?? "").split(",").map((t) => t.trim()).filter(Boolean);
  if (tags.length) q.tag = tags;
  q.sort = (s.sort as RequestQuery["sort"]) ?? "time";
  q.order = (s.order as RequestQuery["order"]) ?? "desc";
  q.limit = s.limit;
  if (s.cursor) q.cursor = s.cursor;
  return q;
}

/** A datetime-local input's value (local time, minutes) for an ISO time, and back. */
const toLocal = (iso?: string) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : new Date(d.getTime() - d.getTimezoneOffset() * 60e3).toISOString().slice(0, 16);
};
const fromLocal = (v: string) => (v ? new Date(v).toISOString() : "");

const statusOf = (r: LedgerRow) => r.status >= 400
  ? <StatusBadge kind="bad" size="sm" title={r.error ?? ""}>{r.status}</StatusBadge>
  : <Text span className="mono" size="sm" c="dimmed">{r.status}</Text>;

/** One request: its ledger fields, why it was routed where it went, its cost and savings. Never its content. */
function Detail({ row, onSession }: { row: LedgerRow; onSession: (id: string) => void }) {
  const label = usePersonLabel();
  const kv = (k: string, v: ReactNode) => (
    <Table.Tr key={k}><Table.Td c="dimmed" w={170}>{k}</Table.Td><Table.Td>{v ?? "–"}</Table.Td></Table.Tr>);
  const [task, level] = (row.floor ?? "").split(":");
  const causes = CAUSES.filter(([k]) => row[k]);
  return (
    <Stack gap="md">
      <Panel title="Why it went there" flush>
        <Table><Table.Tbody>
          {kv("Asked for", <span className="mono">{row.requested_model || "–"}</span>)}
          {kv("Served by", <><b>{row.served_model}</b>{row.upgraded && <Badge ml={6} size="sm" color="mo">free upgrade</Badge>}</>)}
          {kv("Upstream model", <span className="mono">{row.upstream_model}</span>)}
          {kv("Backend", <>{row.backend} <Text span c="dimmed" size="xs">{row.backend_kind}</Text></>)}
          {kv("Level", row.floor ? <><Badge size="sm" color="gray" className="mono">{row.floor}</Badge> <Text span size="xs" c="dimmed">task {task}, level {level}</Text></> : "none (a named model)")}
          {kv("Reason", <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{row.route_reason ?? "–"}</Text>)}
          {kv("Failed over from", row.failed_over ? <span className="mono">{row.failed_over}</span> : "nothing: the first route served it")}
        </Table.Tbody></Table>
      </Panel>
      <Panel title="Cost" flush>
        <Table><Table.Tbody>
          {kv("Spent", <span className="num">{usd(row.actual_cost)}</span>)}
          {kv("Served model at API price", <span className="num">{usd(row.served_api_cost)}</span>)}
          {kv("Without Merit Order", <span className="num">{usd(row.baseline_cost)}</span>)}
          {kv("List price", <span className="num">{usd(row.list_cost)}</span>)}
          {row.client_format === "ocr"
            ? kv("Pages read", <span className="num">{num(row.pages ?? 0)}</span>)
            : kv("Tokens in / cached / written / out", <span className="num">{num(row.input_tokens)} / {num(row.cache_read_tokens)} / {num(row.cache_write_tokens)} / {num(row.output_tokens)}</span>)}
        </Table.Tbody></Table>
      </Panel>
      <Panel title="Savings by cause" hint={usd(row.savings_total)} flush>
        {causes.length ? <Table><Table.Tbody>{causes.map(([k, label]) => kv(label, <span className="num">{usd(row[k])}</span>))}</Table.Tbody></Table>
          : <Text size="sm" c="dimmed" p="md">Nothing saved on this request.</Text>}
      </Panel>
      <Panel title="Request" flush>
        <Table><Table.Tbody>
          {kv("Time", <span className="mono">{new Date(row.ts).toLocaleString()}</span>)}
          {kv("Id", <span className="mono">{row.id}</span>)}
          {kv("Person, team", `${label(row.user_id)} · ${row.team}`)}
          {kv("Key", <span className="mono">{row.key_id ?? "–"}</span>)}
          {kv("Session", row.session_id ? <Anchor size="sm" className="mono" onClick={() => onSession(row.session_id!)}>{row.session_id}</Anchor> : "–")}
          {kv("Status", <>{statusOf(row)} {row.error && <Text size="xs" c="dimmed" mt={4}>{row.error}</Text>}</>)}
          {kv("API format", row.client_format)}
          {kv("Streamed", row.stream ? "yes" : "no")}
          {kv("Batch", row.service_tier === "batch" ? <span className="mono">{row.batch_id}</span> : "no, live")}
          {kv("Latency, first token", `${ms(row.latency_ms)}, ${ms(row.ttft_ms)}`)}
          {kv("Tags", Object.keys(row.tags ?? {}).length ? <Group gap={4}>{Object.entries(row.tags ?? {}).map(([k, v]) =>
            <Badge key={k} size="sm" color="gray" className="mono" tt="none">{k}={String(v)}</Badge>)}</Group> : "–")}
        </Table.Tbody></Table>
      </Panel>
      <Text size="xs" c="dimmed">Metadata only: prompts and responses are never stored in the ledger.</Text>
    </Stack>
  );
}

export function RequestsView({ api, me }: { api: Api; me: Me }) {
  const [state, setState] = useState<State>(readUrl);
  const [draft, setDraft] = useState<State>(state);
  const [more, setMore] = useState(() => MORE_KEYS.some((k) => state[k]));
  const [page, setPage] = useState<number | null>(state.cursor ? null : 1);  // unknown after opening a link to a later page
  const [open, setOpen] = useState<LedgerRow | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const range = useMemo(() => timeRange(state), [state.preset, state.start, state.end]);
  const sortRange = 7;  // the gateway's SORT_RANGE: cost, savings and latency sort only within this many days
  const days = useMemo(() => (range.start ? ((range.end ? Date.parse(range.end) : Date.now()) - Date.parse(range.start)) / 864e5 : Infinity), [range]);
  const sortable = days <= sortRange;
  const query = useMemo(() => {
    const q = toQuery(state, range);
    if (!sortable && q.sort !== "time") { q.sort = "time"; delete q.cursor; }
    return q;
  }, [state, range, sortable]);
  const res = useData(() => api.requests(Number(state.limit) || 50, query), [api, JSON.stringify(query)]);

  // Typing waits for a pause before it asks the gateway; a changed filter starts again from the first page.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const id = setTimeout(() => {
      const next = { ...draft };
      delete next.cursor;
      setState(next);
      setPage(1);
      writeUrl(next);
    }, 350);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(draft)]);

  const set = (k: string, v: string | null) => setDraft((d) => ({ ...d, [k]: v ?? "" }));
  const go = (cursor: string | null | undefined, step: number) => {
    if (!cursor) return;
    const next = { ...state, cursor };
    setState(next);
    setDraft(next);
    first.current = true;  // the draft follows; it isn't a filter change
    setPage((p) => (p == null ? null : Math.max(1, p + step)));
    writeUrl(next);
  };
  const filterBy = (patch: State) => {
    const next: State = { ...DEFAULTS, limit: state.limit, ...patch };
    setDraft(next);
    setOpen(null);
  };

  const active = TEXT_KEYS.filter((k) => draft[k] && k !== "start" && k !== "end").length + (draft.tags ? 1 : 0);
  const d = res.data;
  const rows = d?.rows ?? [];
  const total = d?.total;
  const counted = total == null ? "" : `${d?.total_exact === false ? "about " : ""}${num(total)} matching`;
  const firstRow = page != null ? (page - 1) * (Number(state.limit) || 50) + 1 : null;
  const people = me.role !== "user";

  const text = (k: string, label: string, placeholder?: string, w = 170) => (
    <TextInput size="xs" label={label} placeholder={placeholder} value={draft[k] ?? ""} w={w} onChange={(e) => set(k, e.currentTarget.value)} />);
  const number = (k: string, label: string, prefix?: string) => (
    <NumberInput size="xs" label={label} value={draft[k] ?? ""} w={110} min={k.includes("latency") ? 0 : undefined} prefix={prefix}
      decimalScale={k.includes("latency") ? 0 : 6} onChange={(v) => set(k, v === "" ? "" : String(v))} hideControls />);
  const yesNo = (k: string, label: string) => (
    <Select size="xs" label={label} data={YES_NO} value={draft[k] ?? ""} w={100} allowDeselect={false} onChange={(v) => set(k, v)} />);

  return (
    <Stack gap="md">
      <Panel title="Filters" hint={active ? `${active} active` : "none"}
        right={<Group gap="xs" wrap="nowrap">
          <Button size="xs" variant="subtle" color="gray" onClick={() => setMore((m) => !m)} rightSection={<Icon name="chev" size={12} />}>
            {more ? "Fewer filters" : "More filters"}</Button>
          <Button size="xs" variant="default" disabled={!active && draft.preset === DEFAULTS.preset} onClick={() => filterBy({})}>Clear</Button>
        </Group>}>
        <Group gap="sm" align="flex-end" wrap="wrap">
          <Select size="xs" label="Time" data={TIME_PRESETS} value={draft.preset} w={150} allowDeselect={false}
            onChange={(v) => setDraft((s) => ({ ...s, preset: v ?? "24h", ...(v === "custom" ? { start: s.start || range.start || "", end: s.end || "" } : { start: "", end: "" }) }))} />
          {draft.preset === "custom" && (
            <>
              <TextInput size="xs" type="datetime-local" label="From" value={toLocal(draft.start)} onChange={(e) => set("start", fromLocal(e.currentTarget.value))} w={190} />
              <TextInput size="xs" type="datetime-local" label="To (blank: now)" value={toLocal(draft.end)} onChange={(e) => set("end", fromLocal(e.currentTarget.value))} w={190} />
            </>
          )}
          {people && text("team", "Team", me.role === "team_lead" ? me.team : "any")}
          {people && text("user", "User", "any")}
          {text("served_model", "Model served", "any")}
          <Autocomplete size="xs" label="Status" placeholder="any" data={STATUSES} value={draft.status ?? ""} w={120} onChange={(v) => set("status", v)} />
          {text("reason", "Reason contains", "e.g. short question", 190)}
          {text("tags", "Tags", "project=apollo, explore", 200)}
        </Group>
        <Collapse expanded={more}>
          <Group gap="sm" align="flex-end" wrap="wrap" mt="sm">
            {text("requested_model", "Model asked for", "auto, a name")}
            {text("backend", "Backend", "any", 150)}
            {text("key", "Key id", "key_...", 150)}
            {text("session", "Session id", "any", 170)}
            <Select size="xs" label="Task" data={[{ value: "", label: "Any" }, { value: "coding", label: "coding" }, { value: "general", label: "general" }]}
              value={draft.task ?? ""} w={110} allowDeselect={false} onChange={(v) => set("task", v)} />
            <Select size="xs" label="Level" data={[{ value: "", label: "Any" }, ..."12345".split("").map((n) => ({ value: n, label: n }))]}
              value={draft.level ?? ""} w={90} allowDeselect={false} onChange={(v) => set("level", v)} />
            <Autocomplete size="xs" label="API format" placeholder="any" data={FORMATS} value={draft.format ?? ""} w={130} onChange={(v) => set("format", v)} />
          </Group>
          <Group gap="sm" align="flex-end" wrap="wrap" mt="sm">
            {number("min_cost", "Cost from", "$")}{number("max_cost", "Cost to", "$")}
            {number("min_savings", "Saved from", "$")}{number("max_savings", "Saved to", "$")}
            <Select size="xs" label="Savings cause" data={[{ value: "", label: "Any" }, ...CAUSES.map(([k, label]) => ({ value: k.replace("savings_", ""), label }))]}
              value={draft.savings_cause ?? ""} w={170} allowDeselect={false} onChange={(v) => set("savings_cause", v)} />
            {number("min_latency", "Latency from (ms)")}{number("max_latency", "Latency to (ms)")}
            {yesNo("stream", "Streamed")}{yesNo("batch", "Batch")}{yesNo("cache_hit", "Cache hit")}{yesNo("failover", "Failed over")}
          </Group>
        </Collapse>
      </Panel>

      <Panel title="Requests" hint={counted || undefined} flush
        right={<Group gap="sm" wrap="nowrap">
          <Select size="xs" aria-label="Sort by" w={110} allowDeselect={false} value={sortable ? draft.sort : "time"}
            data={SORTS.map((s) => ({ ...s, disabled: s.value !== "time" && !sortable }))} onChange={(v) => set("sort", v ?? "time")} />
          <Select size="xs" aria-label="Order" w={120} allowDeselect={false} value={draft.order}
            data={[{ value: "desc", label: draft.sort === "time" ? "Newest first" : "Highest first" }, { value: "asc", label: draft.sort === "time" ? "Oldest first" : "Lowest first" }]}
            onChange={(v) => set("order", v ?? "desc")} />
          <Button size="xs" variant="default" leftSection={<Icon name="down" size={12} />}
            onClick={() => api.downloadRequestsCsv(query)}>Download CSV</Button>
        </Group>}
        footer={<>
          <Group gap="sm">
            <span>{rows.length && firstRow != null ? `${num(firstRow)}–${num(firstRow + rows.length - 1)}` : rows.length ? `${rows.length} rows` : "No rows"}{counted && ` of ${counted.replace(" matching", "")}`}</span>
            {!sortable && <span>Sorting by cost, savings or latency needs a time range of {sortRange} days or less.</span>}
            <AnsweredBy by={d?.answered_by} />
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Select size="xs" aria-label="Rows per page" data={PAGE_SIZES.map((n) => ({ value: n, label: `${n} a page` }))} value={draft.limit} w={110}
              allowDeselect={false} onChange={(v) => set("limit", v ?? "50")} />
            <Button size="xs" variant="default" disabled={!d?.prev || res.loading} onClick={() => go(d?.prev, -1)}>Previous</Button>
            <Button size="xs" variant="default" disabled={!d?.next || res.loading} onClick={() => go(d?.next, 1)}>Next</Button>
          </Group>
        </>}>
        {res.error ? <Stack p="md"><Loading loading={false} error={res.error} /></Stack> : !d ? <Stack p="md"><Loading loading error={null} rows={6} /></Stack> : (
          <div style={{ opacity: res.loading ? 0.55 : 1, transition: "opacity .15s" }} aria-busy={res.loading}>
            <Table.ScrollContainer minWidth={1240}>
              <Table highlightOnHover>
                <Table.Thead>
                  <Table.Tr><Table.Th>Time</Table.Th><Table.Th>User</Table.Th><Table.Th>Asked for → served</Table.Th><Table.Th>Why</Table.Th><Table.Th>Backend</Table.Th>
                    <Table.Th ta="right">Status</Table.Th><Table.Th ta="right">In / cached / out</Table.Th><Table.Th ta="right">Cost</Table.Th>
                    <Table.Th ta="right">Saved</Table.Th><Table.Th ta="right">Latency</Table.Th></Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {rows.map((r) => (
                    <Table.Tr key={r.id} onClick={() => setOpen(r)} style={{ cursor: "pointer" }} tabIndex={0}
                      onKeyDown={(e) => { if (e.key === "Enter") setOpen(r); }} aria-label={`Request ${r.id}`}>
                      <Table.Td className="mono" style={{ whiteSpace: "nowrap" }}>{new Date(r.ts).toLocaleString()}</Table.Td>
                      <Table.Td><div><Person id={r.user_id} /></div>{people && <Text size="xs" c="dimmed">{r.team}</Text>}</Table.Td>
                      <Table.Td>{r.requested_model || "–"} → <b>{r.served_model}</b>
                        {r.upgraded && <Badge ml={6} size="sm" color="mo">free upgrade</Badge>}
                        {r.service_tier === "batch" && <Badge ml={6} size="sm" color="gray" title={`Through the provider's batch API, in ${r.batch_id}`}>batch</Badge>}
                        {r.failed_over && <Badge ml={6} size="sm" color="gray" title={`Tried first: ${r.failed_over}`}>failover</Badge>}
                        {r.floor && <Badge ml={6} size="sm" color="gray" className="mono">{r.floor}</Badge>}</Table.Td>
                      <Table.Td maw={340}><Text size="xs" c="dimmed" lineClamp={2} title={r.route_reason ?? ""}>{r.route_reason ?? "–"}</Text></Table.Td>
                      <Table.Td>{r.backend}</Table.Td>
                      <Table.Td ta="right">{statusOf(r)}</Table.Td>
                      <Table.Td className="num">{r.client_format === "ocr" ? `${compact(r.pages ?? 0)} pages`
                        : <>{compact(r.input_tokens)} / {compact(r.cache_read_tokens)} / {compact(r.output_tokens)}</>}</Table.Td>
                      <Table.Td className="num">{usd(r.actual_cost)}</Table.Td>
                      <Table.Td className="num">{usd(r.savings_total)}</Table.Td>
                      <Table.Td className="num">{ms(r.latency_ms)}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            {!rows.length && <Text c="dimmed" size="sm" p="md">No requests match these filters.</Text>}
          </div>
        )}
      </Panel>
      <Drawer opened={open != null} onClose={() => setOpen(null)} position="right" size="lg" title={<Text fw={600}>Request</Text>}>
        {open && <Detail row={open} onSession={(id) => filterBy({ preset: "30d", session: id })} />}
      </Drawer>
      <Text size="xs" c="dimmed">Metadata only: prompts and responses are never stored. Click a row for its details.</Text>
    </Stack>
  );
}
