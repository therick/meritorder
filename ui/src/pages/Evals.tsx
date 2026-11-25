import { Accordion, Alert, Badge, Button, Checkbox, Code, Drawer, Group, List, Modal, MultiSelect, NumberInput, Progress, ScrollArea, SegmentedControl,
  Select, SimpleGrid, Stack, Table, Text, TextInput, Title, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useEffect, useMemo, useState } from "react";
import type { AdminConfig, Api, EvalApplyResult, EvalApplyTarget, EvalEfficiency, EvalEstimate, EvalRequest, EvalResult, EvalRun, EvalRunner,
  EvalTaskDetail, EvalTaskScore, EvalTool, Me } from "../api";
import { pct, usd } from "../api";
import { Loading, Panel, Status, StatusBadge, type StatusKind } from "../components/common";
import { Icon } from "../components/icons";
import { useData } from "../hooks";

const TASK_TYPES = ["coding", "general"];
const STATUS_KIND: Record<string, StatusKind> = { queued: "clock", running: "clock", cancelling: "clock", done: "good", failed: "bad", cancelled: "off" };
const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "–");
const active = (r: EvalRun) => r.status === "queued" || r.status === "running" || r.status === "cancelling";
/** "42 s", "3 min 20 s". */
const secs = (s: number | null | undefined) => (s == null ? "–" : s < 60 ? `${s.toFixed(s < 10 ? 1 : 0)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const perSolve = (e?: EvalEfficiency) => (e ? (e.cost_per_pass == null ? "none solved" : usd(e.cost_per_pass)) : "–");

/** Time to completion, turns, tool calls and tokens, beside cost: what a model's passes took. */
function EfficiencyCells({ e }: { e?: EvalEfficiency }) {
  return (
    <>
      <Table.Td className="num"><Tooltip label={e ? `90% of tasks within ${secs(e.p90_s)}` : "not recorded"}><Text size="xs">{secs(e?.median_s)}</Text></Tooltip></Table.Td>
      <Table.Td className="num"><Text size="xs">{e ? `${e.turns.toFixed(1)} / ${e.tool_calls.toFixed(1)}` : "–"}</Text></Table.Td>
      <Table.Td className="num"><Text size="xs">{e ? e.output_tokens.toLocaleString() : "–"}</Text></Table.Td>
      <Table.Td className="num"><Text size="xs">{perSolve(e)}</Text></Table.Td>
    </>
  );
}
const EFFICIENCY_HEADERS = (
  <>
    <Table.Th ta="right"><Tooltip label="The model's median time to complete a task, grading not included; hover a value for the 90th percentile"><span>Median time</span></Tooltip></Table.Th>
    <Table.Th ta="right"><Tooltip label="Average turns and tool calls per task"><span>Turns / tools</span></Tooltip></Table.Th>
    <Table.Th ta="right"><Tooltip label="Average output tokens per task, thinking included"><span>Output</span></Tooltip></Table.Th>
    <Table.Th ta="right"><Tooltip label="Cost divided by tasks passed: what a solved task costs on this model"><span>Per solve</span></Tooltip></Table.Th>
  </>
);

/** A score with one decimal; "–" for none (0 is "not measured" in routing's scores). */
const one = (v: number | null | undefined) => (v == null || v === 0 ? "–" : v.toFixed(1));

/** A measured score on routing's 1–5 scale, with its 90% range. Amber when the score in use falls outside that range:
 * your own results say it's off. */
function ScoreCell({ s, inUse }: { s?: EvalTaskScore; inUse?: number }) {
  if (!s) return <Text size="xs" c="dimmed">–</Text>;
  const levels = Object.entries(s.levels).map(([l, p]) => `level ${l}: ${pct(p)}${s.counts?.[l] ? ` of ${s.counts[l]}` : ""}`).join(" · ");
  if (s.score == null) {
    return (
      <Tooltip label={`${s.why ?? "No score"}. ${levels}.`} multiline w={320}>
        <Text size="xs" c="dimmed">{s.hard ? "too few" : "–"}</Text>
      </Tooltip>
    );
  }
  const outside = !!inUse && s.low != null && s.high != null && (inUse < s.low || inUse > s.high);
  const hard = s.hard ? `Passed ${s.hard.passed} of ${s.hard.runs} hard tasks (levels 4–5). ` : "";
  return (
    <Tooltip label={`${hard}90% range ${one(s.low)}–${one(s.high)}${outside ? `: the score in use (${one(inUse)}) is outside it` : ""}. ${levels}.`}
      multiline w={320}>
      <Group gap={4} wrap="nowrap">
        {outside ? <StatusBadge size="sm" kind="warn">{one(s.score)}</StatusBadge> : <Badge size="sm" variant="default" className="mono">{one(s.score)}</Badge>}
        <Text size="xs" c="dimmed">{one(s.low)}–{one(s.high)}</Text>
      </Group>
    </Tooltip>
  );
}

function RunnerNote({ runner }: { runner: EvalRunner | null | undefined }) {
  if (!runner) return <Status kind="warn" style={{ whiteSpace: "normal", fontSize: 12 }}>The worker hasn't checked in yet: runs wait until it does (it runs them in the background).</Status>;
  return runner.sandbox
    ? <Text size="xs" c="dimmed">The worker runs evals here, with a sandbox for model-written code.</Text>
    : <Text size="xs" c="var(--mo-warn)"><Icon name="warn" size={12} /> The worker has no sandbox ({runner.why}), so code tasks are skipped when run from here. Run <code>meritorder eval</code> on a
      machine with Docker to include them; its results appear here too.</Text>;
}

function StartModal({ api, cfg, initial, onClose, onStarted }: { api: Api; cfg: AdminConfig | null; initial: EvalRequest | null; onClose: () => void;
  onStarted: (r: EvalRun) => void }) {
  const opened = !!initial;
  const suites = useData(() => api.evalSuites(), [api]);
  const live = useMemo(() => (cfg?.models ?? []).filter((m) => (m.kind ?? "chat") === "chat" && !m.name.startsWith("mock-") && m.routes.some((r) => r.live)).map((m) => m.name), [cfg]);
  const [models, setModels] = useState<string[]>([]);
  const [chosen, setChosen] = useState<string[]>(["starter", "frontier"]);
  const [repeat, setRepeat] = useState<number>(1);
  const [only, setOnly] = useState("");
  const [est, setEst] = useState<EvalEstimate | null>(null);
  const [estError, setEstError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cap, setCap] = useState<number | null>(null); // the run's spending cap; follows the estimate until edited
  const [capEdited, setCapEdited] = useState(false);
  const [agreed, setAgreed] = useState(false);
  useEffect(() => {
    if (!initial) return;
    setModels(initial.models);
    setChosen(initial.suite ? initial.suite.split(",") : ["starter", "frontier"]);
    setRepeat(initial.repeat || 1);
    setOnly((initial.only ?? []).join(", "));
    setCap(null);
    setCapEdited(false);
  }, [initial]);
  const req: EvalRequest = { models, suite: chosen.join(","), repeat, only: only.trim() ? only.split(",").map((s) => s.trim()).filter(Boolean) : null };
  const key = JSON.stringify(req);
  useEffect(() => {
    setEst(null);
    setEstError(null);
    setAgreed(false); // a new estimate needs a new "I understand"
    if (!opened || !models.length || !chosen.length) return;
    const t = setTimeout(async () => {
      try {
        const e = await api.evalEstimate(req);
        setEst(e);
        if (!capEdited) setCap(e.default_max_cost_usd);
      } catch (e) { setEstError(errorText(e)); }
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, opened]);
  const start = async () => {
    setBusy(true);
    try {
      const r = await api.startEval({ ...req, max_cost_usd: cap });
      notifications.show({ color: "green", title: "Eval queued", message: "The worker starts it within a few seconds." });
      onStarted(r);
      onClose();
    } catch (e) {
      notifications.show({ color: "red", title: "Couldn't start the eval", message: errorText(e) });
    } finally { setBusy(false); }
  };
  const claude = live.filter((m) => m.startsWith("claude-"));
  return (
    <Modal opened={opened} onClose={onClose} title="Run an eval" size="lg">
      <Stack gap="sm">
        <MultiSelect label="Models" data={live} value={models} onChange={setModels} searchable clearable placeholder="Pick models with a live route"
          description={<Group gap={6} component="span">Quick picks:
            <Button size="compact-xs" variant="subtle" onClick={() => setModels(claude)} disabled={!claude.length}>Claude models</Button>
            <Button size="compact-xs" variant="subtle" onClick={() => setModels(live)}>All {live.length}</Button></Group>} />
        <Loading loading={suites.loading && !suites.data} error={suites.error} rows={1} />
        <Checkbox.Group label="Suites" value={chosen} onChange={setChosen}>
          <Stack gap={6} mt={6}>
            {suites.data?.suites.map((s) => (
              <Checkbox key={s.name} value={s.name} label={`${s.name}: ${s.tasks} tasks`}
                description={`${Object.entries(s.kinds).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ")} · levels ${Object.entries(s.levels).filter(([, n]) => n).map(([l, n]) => `${l}×${n}`).join(" ")}`} />
            ))}
          </Stack>
        </Checkbox.Group>
        <SimpleGrid cols={2}>
          <NumberInput label="Run each task" suffix=" times" min={1} max={10} value={repeat} onChange={(v) => setRepeat(Number(v) || 1)}
            description="More runs smooth out luck, at proportional cost." />
          <TextInput label="Only tasks matching" placeholder="e.g. c5-*, a4-*" value={only} onChange={(e) => setOnly(e.currentTarget.value)}
            description="Task id patterns, comma-separated. Blank: all." />
        </SimpleGrid>
        <RunnerNote runner={suites.data?.runner} />
        {estError && <Alert color="red" icon={<Icon name="bad" size={16} />}>{estError}</Alert>}
        {est && (
          <Alert color={est.usd >= 50 ? "red" : est.usd >= est.confirm_above_usd ? "yellow" : "mo"} icon={<Icon name={est.usd >= est.confirm_above_usd ? "warn" : "info"} size={16} />}
            title={`This run costs real money: about ${usd(est.usd)} for ${est.runs.toLocaleString()} task runs`}>
            <Text size="xs">{Object.entries(est.by_model).sort(([, a], [, b]) => b - a).map(([m, v]) => `${m} ${usd(v)}`).join(" · ")}{est.judging_usd ? ` · judging ${usd(est.judging_usd)}` : ""}</Text>
            {est.judged > 0 && <Text size="xs">{est.judged} open-ended tasks graded by {est.judges.length
              ? `${est.judges.join(", ")}${est.judges.length > 2 ? ": each answer by the two from other model families" : ""}`
              : "no judges (none has a live route, so they're skipped)"}.</Text>}
            {est.unpriced.length > 0 && <Text size="xs">No price for {est.unpriced.join(", ")}: not in the estimate.</Text>}
            <Text size="xs" fw={600}>A rough guide: real runs have cost up to three times the estimate. Reasoning models can write far more than expected,
              and an agent task resends its whole conversation every turn.</Text>
            {est.budget_left_usd != null && est.budget_left_usd < Math.max(est.usd, cap ?? 0) && (
              <Text size="xs" c="var(--mo-bad)" fw={600}>The organization has {usd(Math.max(est.budget_left_usd, 0))} of its monthly budget left: the run stops when that runs out.</Text>)}
          </Alert>
        )}
        {est && (
          <NumberInput label="Spending cap" prefix="$" min={1} max={100000} decimalScale={2} value={cap ?? ""} w={220}
            onChange={(v) => { setCap(Number(v) || null); setCapEdited(true); }}
            description="No new task starts once the run has spent this much; tasks already running finish." />
        )}
        {est && est.usd >= est.confirm_above_usd && (
          <Checkbox checked={agreed} onChange={(e) => setAgreed(e.currentTarget.checked)}
            label={`I understand this run costs about ${usd(est.usd)}, maybe several times that, and stops at ${usd(cap ?? 0)}.`} />
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={start} loading={busy} color={est && est.usd >= 50 ? "red" : undefined}
            disabled={!est || !models.length || !cap || (est.usd >= est.confirm_above_usd && !agreed)}>{est ? `Start (about ${usd(est.usd)})` : "Start"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function ApplyModal({ api, run, cfg, onClose, onApplied }: { api: Api; run: EvalRun | null; cfg: AdminConfig | null; onClose: () => void; onApplied: () => void }) {
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState<EvalApplyTarget>("quality");
  const [preview, setPreview] = useState<EvalApplyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const source = cfg?.quality?.source ?? "config";
  useEffect(() => {
    setPreview(null);
    setError(null);
    if (!run) return;
    api.applyEval(run.id, { target, dry_run: true }).then(setPreview, (e) => setError(errorText(e)));
  }, [api, run, target]);
  const changes = preview?.changes ?? [];
  const apply = async () => {
    if (!run) return;
    setBusy(true);
    try {
      const r = await api.applyEval(run.id, { target });
      notifications.show({ color: "green", title: r.version ? `Saved as config version ${r.version}` : "Nothing to change",
        message: r.changed.length ? r.changed.join("; ") : "The configuration already has these scores." });
      onApplied();
      onClose();
    } catch (e) {
      notifications.show({ color: "red", title: "Not applied", message: errorText(e) });
    } finally { setBusy(false); }
  };
  const effect = target === "quality_override"
    ? "They win over every other source for these models, so routing uses them within seconds. Remove an override in Config → Models to go back."
    : source === "config"
      ? "Routing uses them within seconds (quality scores come from the configuration)."
      : `Routing keeps using the published scores (source: ${source}): a model's quality setting counts only with quality.source: config. Write them as overrides to make them count.`;
  return (
    <Modal opened={!!run} onClose={onClose} title="Apply measured quality scores" size="lg">
      <Stack gap="sm">
        <SegmentedControl size="xs" value={target} onChange={(v) => setTarget(v as EvalApplyTarget)} aria-label="Write as"
          data={[{ value: "quality", label: "Write as quality" }, { value: "quality_override", label: "Write as override" }]} />
        <Text size="sm">Scores are scaled together with every other model's latest result. They become a new configuration version (you can
          restore the previous one from Config → History). {effect}</Text>
        {error && <Alert color="red" icon={<Icon name="bad" size={16} />}>{error}</Alert>}
        {preview && !changes.length && <Text size="sm">The configuration already has these scores.</Text>}
        {changes.length > 0 && (
          <Table fz="sm"><Table.Thead><Table.Tr><Table.Th>Model</Table.Th><Table.Th>Task type</Table.Th><Table.Th>In use now</Table.Th>
            <Table.Th>Measured</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>{changes.map((c) => (
              <Table.Tr key={`${c.model}-${c.task}`}><Table.Td>{c.model}</Table.Td><Table.Td>{c.task}</Table.Td><Table.Td>{one(c.in_use)}</Table.Td>
                <Table.Td>{one(c.to)} <Text span size="xs" c="dimmed">({one(c.low)}–{one(c.high)})</Text></Table.Td></Table.Tr>
            ))}</Table.Tbody></Table>
        )}
      </Stack>
      <Group justify="flex-end" mt="md">
        <Button variant="default" onClick={onClose}>Cancel</Button>
        <Button onClick={apply} loading={busy} disabled={!changes.length}>Apply {changes.length} change{changes.length === 1 ? "" : "s"}</Button>
      </Group>
    </Modal>
  );
}

function Outcome({ r }: { r: EvalResult }) {
  const main = r.skipped ? <StatusBadge size="sm" kind="off">skipped</StatusBadge> : r.error ? <StatusBadge size="sm" kind="bad">error</StatusBadge>
    : r.passed ? <StatusBadge size="sm" kind="good">pass</StatusBadge> : <StatusBadge size="sm" kind="warn">fail</StatusBadge>;
  return <Group gap={4} wrap="nowrap">{main}{r.truncated && <Tooltip label="A reply hit its output limit before finishing"><Badge size="sm" variant="default">cut off</Badge></Tooltip>}</Group>;
}

function ResultRow({ r, onOpen }: { r: EvalResult; onOpen: () => void }) {
  const why = r.error ?? r.reasons.join("; ");
  return (
    <Table.Tr style={{ cursor: "pointer" }} onClick={onOpen} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") onOpen(); }}
      aria-label={`${r.task_id} on ${r.model}: details`}>
      <Table.Td><Text size="xs" className="mono">{r.task_id}</Text></Table.Td>
      <Table.Td><Text size="xs">{r.model}</Text></Table.Td>
      <Table.Td><Text size="xs">{r.kind} · {r.task} {r.level}</Text></Table.Td>
      <Table.Td><Outcome r={r} /></Table.Td>
      <Table.Td><Text size="xs" c="dimmed">{[r.detail, r.model_ms ? secs(r.model_ms / 1000) : "", r.output_tokens ? `${r.output_tokens.toLocaleString()} out` : ""].filter(Boolean).join(" · ")}</Text></Table.Td>
      <Table.Td className="num"><Text size="xs">{usd(r.cost)}</Text></Table.Td>
      <Table.Td><Text size="xs" c="dimmed" lineClamp={1} maw={420}>{why}</Text></Table.Td>
    </Table.Tr>
  );
}

/** One task result in full: what the model wrote, every reason it failed, and each judge's verdict on every point. */
function ResultDetail({ r, onClose }: { r: EvalResult | null; onClose: () => void }) {
  return (
    <Modal opened={!!r} onClose={onClose} size="xl" title={r ? `${r.task_id} · ${r.model}` : ""}>
      {r && (
        <Stack gap="sm">
          <Group gap="xs"><Outcome r={r} /><Text size="xs" c="dimmed">
            {[`${r.kind} · ${r.task} level ${r.level}`, r.effort ? `effort ${r.effort}` : "model's default effort", r.backend ? `on ${r.backend}` : "",
              r.output_tokens ? `${r.output_tokens.toLocaleString()} output tokens` : "", `${r.turns} turn${r.turns === 1 ? "" : "s"}`,
              r.tool_calls ? `${r.tool_calls} tool call${r.tool_calls === 1 ? "" : "s"}` : "",
              r.model_ms != null ? `${secs(r.model_ms / 1000)} model time (${secs(r.latency_ms / 1000)} with grading)` : secs(r.latency_ms / 1000),
              usd(r.cost)].filter(Boolean).join(" · ")}</Text></Group>
          {r.detail && <Text size="sm">{r.detail}</Text>}
          {(r.error || r.reasons.length > 0) && (
            <Alert color={r.skipped ? "gray" : "yellow"} icon={<Icon name={r.skipped ? "info" : "warn"} size={16} />} p="xs">
              <List size="sm" spacing={2}>{[r.error, ...r.reasons].filter(Boolean).map((w, i) => <List.Item key={i}>{w}</List.Item>)}</List>
            </Alert>
          )}
          {(r.judging ?? []).map((j) => (
            <div key={j.judge}>
              <Title order={6} mb={4}>Judge {j.judge}: {j.criteria.filter((c) => c.met).length}/{j.criteria.length} met</Title>
              <Table fz="xs" verticalSpacing={3}>
                <Table.Tbody>{j.criteria.map((c) => (
                  <Table.Tr key={c.n}><Table.Td w={28}>{c.met ? "✓" : "✗"}</Table.Td><Table.Td>{c.criterion}</Table.Td><Table.Td c="dimmed">{c.why}</Table.Td></Table.Tr>
                ))}</Table.Tbody>
              </Table>
            </div>
          ))}
          {(r.steps ?? []).length > 0 && (
            <div>
              <Title order={6} mb={4}>Tool calls ({r.steps!.length})</Title>
              <ScrollArea.Autosize mah={200}><Code block>{r.steps!.map((s, i) => `${i + 1}. ${s}`).join("\n")}</Code></ScrollArea.Autosize>
            </div>
          )}
          <div>
            <Title order={6} mb={4}>{r.kind === "agent" ? "Final answer" : "Answer"}</Title>
            {r.answer ? <ScrollArea.Autosize mah={460}><Code block style={{ whiteSpace: "pre-wrap" }}>{r.answer}</Code></ScrollArea.Autosize>
              : <Text size="sm" c="dimmed">{r.answer === undefined ? "Not recorded: this run predates saved answers." : "No answer."}</Text>}
          </div>
        </Stack>
      )}
    </Modal>
  );
}

function RunDrawer({ api, id, onClose }: { api: Api; id: string | null; onClose: () => void }) {
  const run = useData(() => (id ? api.evalRun(id) : Promise.resolve(null)), [api, id], 4000);
  const [model, setModel] = useState<string | null>(null);
  const [show, setShow] = useState("not passed");
  const [detail, setDetail] = useState<EvalResult | null>(null);
  const r = run.data;
  const results = (r?.results ?? []).filter((x) => (!model || x.model === model) && (show === "all" || (show === "not passed" ? !x.passed : x.passed)))
    .sort((a, b) => a.task_id.localeCompare(b.task_id) || a.model.localeCompare(b.model));
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="xl" title={r ? `Eval run ${r.id}` : "Eval run"}>
      <Loading loading={run.loading && !r} error={run.error} />
      {r && (
        <Stack gap="md">
          <Text size="sm">{r.request.models.join(", ")} · {r.request.suite} · ×{r.request.repeat} · started by {r.created_by} {when(r.created_at)}
            {r.executor === "cli" ? " from the command line" : ""}</Text>
          {active(r) && <Progress value={r.progress.total ? (100 * (r.progress.done ?? 0)) / r.progress.total : 0} animated />}
          {r.error && <Alert color={r.progress?.stopped ? "yellow" : "red"} icon={<Icon name={r.progress?.stopped ? "warn" : "bad"} size={16} />}>{r.error}</Alert>}
          {r.scores && (
            <Table.ScrollContainer minWidth={820}>
            <Table fz="sm">
              <Table.Thead><Table.Tr><Table.Th>Model</Table.Th>{TASK_TYPES.map((t) => <Table.Th key={t}>{t}</Table.Th>)}<Table.Th ta="right"><Tooltip label="Share of graded tasks passed. Skipped runs (a provider error, no sandbox or judges) don't count"><span>Passed</span></Tooltip></Table.Th>
                <Table.Th ta="right">Cost</Table.Th>{EFFICIENCY_HEADERS}</Table.Tr></Table.Thead>
              <Table.Tbody>{Object.entries(r.scores).map(([m, s]) => (
                <Table.Tr key={m}><Table.Td fw={600}>{m}</Table.Td>{TASK_TYPES.map((t) => <Table.Td key={t}><ScoreCell s={s.tasks[t]} /></Table.Td>)}
                  <Table.Td className="num">{pct(s.pass_rate)}{!!s.skipped && <Text size="xs" c="dimmed">{s.skipped} skipped</Text>}</Table.Td>
                  <Table.Td className="num">{usd(s.cost)}</Table.Td><EfficiencyCells e={s.efficiency} /></Table.Tr>
              ))}</Table.Tbody>
            </Table>
            </Table.ScrollContainer>
          )}
          <Group gap="sm">
            <Select size="xs" placeholder="All models" clearable data={r.request.models} value={model} onChange={setModel} aria-label="Model" />
            <SegmentedControl size="xs" value={show} onChange={setShow} data={["not passed", "passed", "all"]} />
            <Text size="xs" c="dimmed">{results.length} results</Text>
          </Group>
          <Table.ScrollContainer minWidth={760}>
            <Table>
              <Table.Thead><Table.Tr><Table.Th>Task</Table.Th><Table.Th>Model</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Result</Table.Th><Table.Th>Detail</Table.Th>
                <Table.Th ta="right">Cost</Table.Th><Table.Th>Why</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{results.map((x, i) => <ResultRow key={`${x.task_id}-${x.model}-${i}`} r={x} onOpen={() => setDetail(x)} />)}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {!r.results && !active(r) && <Text size="sm" c="dimmed">No task results recorded.</Text>}
        </Stack>
      )}
      <ResultDetail r={detail} onClose={() => setDetail(null)} />
    </Drawer>
  );
}

const KIND_HELP: Record<string, string> = {
  answer: "One prompt; the reply is checked mechanically",
  code: "Write a Python module; hidden tests run against it in a sandbox",
  agent: "A goal and tools; a scripted environment answers them, then the end state is checked",
  judged: "Open-ended work; two judge models check it against a rubric",
};

function Block({ text, mah = 360 }: { text: string; mah?: number }) {
  return <ScrollArea.Autosize mah={mah}><Code block style={{ whiteSpace: "pre-wrap" }}>{text}</Code></ScrollArea.Autosize>;
}

function Tools({ tools }: { tools: EvalTool[] }) {
  return (
    <Table fz="xs" verticalSpacing={3}>
      <Table.Tbody>{tools.map((t) => (
        <Table.Tr key={t.name}><Table.Td className="mono" w={140} style={{ verticalAlign: "top" }}>{t.name}</Table.Td>
          <Table.Td>{t.description}{t.parameters?.properties && Object.keys(t.parameters.properties).length > 0 &&
            <Text size="xs" c="dimmed">({Object.keys(t.parameters.properties).join(", ")})</Text>}</Table.Td></Table.Tr>
      ))}</Table.Tbody>
    </Table>
  );
}

/** Files an environment holds, one at a time. */
function Files({ files, label }: { files: Record<string, string>; label: string }) {
  const names = Object.keys(files);
  const [pick, setPick] = useState<string | null>(names[0] ?? null);
  if (!names.length) return null;
  return (
    <Stack gap={4}>
      <Select size="xs" data={names} value={pick} onChange={setPick} aria-label={label} allowDeselect={false} searchable maw={420} />
      {pick && <Block text={files[pick]} />}
    </Stack>
  );
}

/** One task in full: what the model gets, how it's graded, and how each model did on it. */
function TaskDrawer({ api, suite, id, onClose }: { api: Api; suite: string; id: string | null; onClose: () => void }) {
  const task = useData(() => (id ? api.evalTask(suite, id) : Promise.resolve(null)), [api, suite, id]);
  const [detail, setDetail] = useState<EvalResult | null>(null);
  const t: EvalTaskDetail | null = task.data;
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="min(920px, 100vw)" title={id ? `Task ${id}` : "Task"}>
      <Loading loading={task.loading && !t} error={task.error} />
      {t && (
        <Stack gap="md">
          <Group gap="xs"><Badge variant="light">{t.kind}</Badge><Badge variant="outline">{t.task} · level {t.level}</Badge>
            <Text size="xs" c="dimmed">{KIND_HELP[t.kind]}</Text></Group>
          {t.summary && <Text size="sm">{t.summary}</Text>}
          <Accordion multiple defaultValue={["prompt", "grading", "results"]} variant="separated">
            <Accordion.Item value="prompt">
              <Accordion.Control>What the model gets</Accordion.Control>
              <Accordion.Panel>
                <Stack gap="xs">
                  <Text size="xs" c="dimmed">{t.prompt.split(/\s+/).filter(Boolean).length.toLocaleString()} words
                    {t.generated ? ` · generated by ${t.generated.by} with ${JSON.stringify(t.generated.args)} (the same text every run)` : ""}
                    {t.effort ? ` · effort ${t.effort}` : " · effort: the level's (Routing settings)"}{t.max_tokens ? ` · at most ${t.max_tokens.toLocaleString()} output tokens` : " · no output limit"}</Text>
                  <Block text={t.prompt} mah={420} />
                  {t.agent && <Text size="xs">Up to {t.agent.max_turns} turns. Its system prompt says to use the tools, check its work, and finish with a final answer and no tool calls.</Text>}
                  {t.tools.length > 0 && <><Title order={6}>Tools</Title><Tools tools={t.tools} /></>}
                  {t.agent && Object.keys(t.agent.visible).length > 0 && (
                    <><Title order={6}>Files it can reach through the tools ({Object.keys(t.agent.visible).length})</Title><Files files={t.agent.visible} label="Visible file" /></>
                  )}
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
            <Accordion.Item value="grading">
              <Accordion.Control>How it's graded: {t.graded_by}</Accordion.Control>
              <Accordion.Panel>
                <Stack gap="xs">
                  <Text size="xs" c="dimmed">None of this is sent to the model.</Text>
                  {t.checks.length > 0 && <><Title order={6}>Checks on the reply</Title><Block text={t.checks.map((c) => JSON.stringify(c)).join("\n")} /></>}
                  {t.code && (
                    <>
                      <Text size="sm">{t.code.tests_count} hidden tests, all of which must pass. Each has {t.code.per_test_timeout} s of CPU time; the run {t.code.timeout} s
                        {t.code.forbid.length ? `; may not import ${t.code.forbid.join(", ")}` : ""}. The code runs in a container with no network, a read-only
                        filesystem and no privileges.</Text>
                      <Accordion variant="contained">
                        <Accordion.Item value="tests"><Accordion.Control>Hidden tests</Accordion.Control><Accordion.Panel><Block text={t.code.tests} /></Accordion.Panel></Accordion.Item>
                        {t.code.reference && <Accordion.Item value="ref"><Accordion.Control>Reference solution (passes every test)</Accordion.Control>
                          <Accordion.Panel><Block text={t.code.reference} /></Accordion.Panel></Accordion.Item>}
                        {t.code.wrong && <Accordion.Item value="wrong"><Accordion.Control>A plausible wrong solution (must fail)</Accordion.Control>
                          <Accordion.Panel><Block text={t.code.wrong} /></Accordion.Panel></Accordion.Item>}
                      </Accordion>
                    </>
                  )}
                  {t.agent && (
                    <>
                      <Text size="sm">{t.agent.grading}.</Text>
                      <Text size="xs" c="dimmed">Environment <span className="mono">{t.agent.env}</span> {Object.keys(t.agent.env_args).length ? `with ${JSON.stringify(t.agent.env_args)}` : ""}
                        {t.agent.about ? `: ${t.agent.about}` : ""}</Text>
                      {Object.keys(t.agent.grader).length > 0 && <><Title order={6}>The grader's data (hidden tests, expected answer)</Title><Files files={t.agent.grader} label="Grader file" /></>}
                    </>
                  )}
                  {t.judged && (
                    <>
                      <Text size="sm">Two judges read the answer without knowing which model wrote it, both from families other than its model's (from Claude Opus 5.5,
                        GPT-6.1 Sol and Gemini 4 Carbon by default). It passes only if both say every point is met.</Text>
                      <List size="sm" type="ordered" spacing={2}>{t.judged.rubric.map((r, i) => <List.Item key={i}>{r}</List.Item>)}</List>
                      {t.judged.reference_answer && <Accordion variant="contained"><Accordion.Item value="ref"><Accordion.Control>Reference answer (shown to the judges)</Accordion.Control>
                        <Accordion.Panel><Block text={t.judged.reference_answer} /></Accordion.Panel></Accordion.Item></Accordion>}
                    </>
                  )}
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
            <Accordion.Item value="results">
              <Accordion.Control>Latest results ({t.results.length})</Accordion.Control>
              <Accordion.Panel>
                {!t.results.length ? <Text size="sm" c="dimmed">No model's latest results include this task.</Text> : (
                  <Table fz="xs" verticalSpacing={3} highlightOnHover>
                    <Table.Thead><Table.Tr><Table.Th>Model</Table.Th><Table.Th>Result</Table.Th><Table.Th ta="right">Time</Table.Th><Table.Th ta="right">Output</Table.Th>
                      <Table.Th ta="right">Cost</Table.Th><Table.Th>Why</Table.Th></Table.Tr></Table.Thead>
                    <Table.Tbody>{t.results.map((r, i) => (
                      <Table.Tr key={`${r.model}-${i}`} style={{ cursor: "pointer" }} onClick={() => setDetail(r)}>
                        <Table.Td>{r.model}</Table.Td><Table.Td><Outcome r={r} /></Table.Td>
                        <Table.Td className="num">{r.model_ms != null ? secs(r.model_ms / 1000) : secs(r.latency_ms / 1000)}</Table.Td>
                        <Table.Td className="num">{r.output_tokens ? r.output_tokens.toLocaleString() : "–"}</Table.Td>
                        <Table.Td className="num">{usd(r.cost)}</Table.Td>
                        <Table.Td><Text size="xs" c="dimmed" lineClamp={1} maw={280}>{r.error ?? r.reasons.join("; ")}</Text></Table.Td>
                      </Table.Tr>
                    ))}</Table.Tbody>
                  </Table>
                )}
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
        </Stack>
      )}
      <ResultDetail r={detail} onClose={() => setDetail(null)} />
    </Drawer>
  );
}

/** Every built-in task: what it asks and how it's graded. */
function TasksView({ api }: { api: Api }) {
  const [suite, setSuite] = useState("frontier");
  const suites = useData(() => api.evalSuites(), [api]);
  const names = suites.data?.suites.map((s) => s.name) ?? ["starter", "frontier"];
  const tasks = useData(() => api.evalTasks(suite), [api, suite]);
  const [kind, setKind] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const rows = (tasks.data?.tasks ?? []).filter((t) => (!kind || t.kind === kind)
    && (!q || `${t.id} ${t.summary} ${t.graded_by}`.toLowerCase().includes(q.toLowerCase())));
  return (
    <Panel title="Tasks" right={<SegmentedControl size="xs" value={suite} onChange={setSuite} data={names} aria-label="Suite" />}>
      <Text size="sm" c="dimmed" mb="xs">
        What each task asks and how it's graded. A model sees only the prompt, plus the tools for agent tasks; the checks, hidden tests, reference
        solutions and rubrics stay with the grader. Open a task to read all of it and see how each model did.
      </Text>
      <Group gap="sm" mb="xs">
        <Select size="xs" placeholder="All kinds" clearable data={Object.keys(KIND_HELP)} value={kind} onChange={setKind} aria-label="Kind" w={140} />
        <TextInput size="xs" placeholder="Search" value={q} onChange={(e) => setQ(e.currentTarget.value)} aria-label="Search tasks" w={220} />
        <Text size="xs" c="dimmed">{rows.length} tasks</Text>
      </Group>
      <Loading loading={tasks.loading && !tasks.data} error={tasks.error} />
      <Table.ScrollContainer minWidth={820}>
        <Table>
          <Table.Thead><Table.Tr><Table.Th>Task</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Level</Table.Th><Table.Th>What it tests</Table.Th><Table.Th>Graded by</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>{rows.map((t) => (
            <Table.Tr key={t.id} style={{ cursor: "pointer" }} onClick={() => setOpen(t.id)} tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter") setOpen(t.id); }} aria-label={`Task ${t.id}`}>
              <Table.Td><Text size="xs" className="mono">{t.id}</Text></Table.Td>
              <Table.Td><Tooltip label={KIND_HELP[t.kind]}><Badge size="xs" variant="light">{t.kind}</Badge></Tooltip></Table.Td>
              <Table.Td><Text size="xs">{t.task} {t.level}</Text></Table.Td>
              <Table.Td><Text size="xs" lineClamp={2} maw={480}>{t.summary}</Text></Table.Td>
              <Table.Td><Text size="xs" c="dimmed" maw={300}>{t.graded_by}</Text></Table.Td>
            </Table.Tr>
          ))}</Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      <TaskDrawer api={api} suite={suite} id={open} onClose={() => setOpen(null)} />
    </Panel>
  );
}

export function EvalsPanel({ api, me, cfg }: { api: Api; me: Me; cfg: AdminConfig | null }) {
  const latest = useData(() => api.evals(), [api]);
  const [poll, setPoll] = useState(false);
  const runs = useData(() => api.evalRuns(), [api, poll], poll ? 3000 : 20000);
  const [starting, setStarting] = useState<EvalRequest | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const [applying, setApplying] = useState<EvalRun | null>(null);
  const admin = me.role === "admin";
  const anyActive = !!runs.data?.runs.some(active);
  useEffect(() => {
    if (poll && !anyActive) void latest.reload();  // a run just finished: its scores are the latest now
    setPoll(anyActive);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyActive]);
  const quality = Object.fromEntries((cfg?.models ?? []).map((m) => [m.name, m.quality]));
  const scored = Object.entries(latest.data?.models ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const cancel = async (r: EvalRun) => {
    try { await api.cancelEval(r.id); await runs.reload(); } catch (e) { notifications.show({ color: "red", title: "Couldn't cancel", message: errorText(e) }); }
  };
  const [view, setView] = useState("results");
  if (admin && view === "tasks") {
    return (
      <Stack gap="md">
        <SegmentedControl value={view} onChange={setView} data={[{ value: "results", label: "Results" }, { value: "tasks", label: "Tasks" }]} w={240} aria-label="Evals view" />
        <TasksView api={api} />
      </Stack>
    );
  }
  return (
    <Stack gap="md">
      {admin && <SegmentedControl value={view} onChange={setView} data={[{ value: "results", label: "Results" }, { value: "tasks", label: "Tasks" }]} w={240} aria-label="Evals view" />}
      <Alert variant="light" color="gray">
        Evals measure each model on your own graded tasks, on the same 1–5 scale as the scores <code>auto</code> routes on: the best model measured scores 5
        and every other in proportion to how many hard tasks (levels 4–5) it passes, with the 90% range beside it. Easy tasks don't count: strong and weak
        models alike pass them. A model needs at least 10 graded hard tasks for a score (a frontier run has 10 coding and 13 general); runs a provider error stopped don't count. Amber: the score in use
        is outside the measured range. Code is tested by running it; agent tasks by the end state of a scripted environment; open-ended work by two
        judge models, both from families other than its model's, which must both agree.
      </Alert>
      <Panel title="Measured quality (each task's latest graded result per model)" right={admin && <Button size="xs" onClick={() => setStarting({ models: [], suite: "starter,frontier", repeat: 1 })}>Run an eval…</Button>}>
        <Loading loading={latest.loading && !latest.data} error={latest.error} />
        {latest.data && !scored.length && <Text size="sm" c="dimmed">No evals yet. Run one to measure the models you route to.</Text>}
        {scored.length > 0 && (
          <Table.ScrollContainer minWidth={1040}>
            <Table fz="sm" verticalSpacing={5}>
              <Table.Thead><Table.Tr><Table.Th>Model</Table.Th>{TASK_TYPES.map((t) => <Table.Th key={t}>{t}: in use → measured</Table.Th>)}
                <Table.Th ta="right"><Tooltip label="Share of graded tasks passed. Skipped runs (a provider error, no sandbox or judges) don't count"><span>Passed</span></Tooltip></Table.Th><Table.Th ta="right">Cost</Table.Th>{EFFICIENCY_HEADERS}<Table.Th>Run</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{scored.map(([m, s]) => (
                <Table.Tr key={m}>
                  <Table.Td fw={600}>{m}</Table.Td>
                  {TASK_TYPES.map((t) => <Table.Td key={t}><Group gap={6} wrap="nowrap"><Text size="xs" c="dimmed">{one(quality[m]?.[t])} →</Text>
                    <ScoreCell s={s.tasks[t]} inUse={quality[m]?.[t]} /></Group></Table.Td>)}
                  <Table.Td className="num">{pct(s.pass_rate)}{!!s.skipped && <Text size="xs" c="dimmed">{s.skipped} skipped</Text>}</Table.Td>
                  <Table.Td className="num">{usd(s.cost)}</Table.Td>
                  <EfficiencyCells e={s.efficiency} />
                  <Table.Td><Text size="xs" c="dimmed">{s.suite} · {when(s.ran_at)}</Text>
                    {(s.runs?.length ?? 0) > 1 && <Text size="xs" c="dimmed">tasks from {s.runs!.length} runs</Text>}
                    {s.run_id && <Button size="compact-xs" variant="subtle" onClick={() => setViewing(s.run_id!)}>details</Button>}</Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Panel>
      <Panel title="Runs">
        <RunnerNote runner={runs.data?.runner} />
        <Loading loading={runs.loading && !runs.data} error={runs.error} />
        {runs.data && !runs.data.runs.length && <Text size="sm" c="dimmed" mt="xs">No runs yet.</Text>}
        {!!runs.data?.runs.length && (
          <Table.ScrollContainer minWidth={820}>
            <Table fz="sm" verticalSpacing={5} mt="xs" highlightOnHover>
              <Table.Thead><Table.Tr><Table.Th>Started</Table.Th><Table.Th>Models</Table.Th><Table.Th>Suite</Table.Th><Table.Th>Status</Table.Th>
                <Table.Th ta="right">Cost</Table.Th><Table.Th /></Table.Tr></Table.Thead>
              <Table.Tbody>{runs.data.runs.map((r) => (
                <Table.Tr key={r.id} style={{ cursor: "pointer" }} onClick={() => setViewing(r.id)}>
                  <Table.Td><Text size="xs">{when(r.created_at)}</Text><Text size="xs" c="dimmed">{r.created_by}{r.executor === "cli" ? " · CLI" : ""}</Text></Table.Td>
                  <Table.Td><Text size="xs" lineClamp={2} maw={260}>{r.request.models.join(", ")}</Text></Table.Td>
                  <Table.Td><Text size="xs">{r.request.suite}{r.request.repeat > 1 ? ` ×${r.request.repeat}` : ""}{r.request.only?.length ? ` · ${r.request.only.join(", ")}` : ""}</Text></Table.Td>
                  <Table.Td>
                    <StatusBadge size="sm" kind={STATUS_KIND[r.status] ?? "off"}>{r.status}</StatusBadge>
                    {r.progress?.stopped && <Tooltip label={r.error ?? "stopped at its spending cap"}><StatusBadge size="sm" kind="warn" ml={4}>stopped at cap</StatusBadge></Tooltip>}
                    {active(r) && r.progress.total ? <Progress mt={4} size="sm" value={(100 * (r.progress.done ?? 0)) / r.progress.total} w={140} /> : null}
                    {r.applied_version && <Text size="xs" c="dimmed">applied as v{r.applied_version}</Text>}
                  </Table.Td>
                  <Table.Td className="num"><Text size="xs">{usd(r.cost)}</Text>{r.request.estimate_usd != null && <Text size="xs" c="dimmed">est. {usd(r.request.estimate_usd)}</Text>}</Table.Td>
                  <Table.Td onClick={(e) => e.stopPropagation()}>
                    <Group gap={4} wrap="nowrap">
                      {admin && active(r) && r.status !== "cancelling" && <Button size="compact-xs" variant="outline" color="red" onClick={() => cancel(r)}>Cancel</Button>}
                      {admin && r.status === "done" && r.scores && !r.applied_version && <Button size="compact-xs" variant="light" onClick={async () => setApplying(await api.evalRun(r.id))}>Apply scores…</Button>}
                      {admin && !active(r) && <Button size="compact-xs" variant="subtle"
                        onClick={() => setStarting({ models: r.request.models, suite: r.request.suite, repeat: r.request.repeat, only: r.request.only })}>Run again…</Button>}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Panel>
      <StartModal api={api} cfg={cfg} initial={starting} onClose={() => setStarting(null)} onStarted={() => { setPoll(true); void runs.reload(); }} />
      <ApplyModal api={api} run={applying} cfg={cfg} onClose={() => setApplying(null)} onApplied={() => { void runs.reload(); void latest.reload(); }} />
      <RunDrawer api={api} id={viewing} onClose={() => setViewing(null)} />
    </Stack>
  );
}
