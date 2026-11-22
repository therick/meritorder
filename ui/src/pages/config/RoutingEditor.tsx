import { Anchor, Paper, Select, SimpleGrid, Stack, Switch, Textarea } from "@mantine/core";
import { useEffect, useState } from "react";
import type { Api, Doc, LevelRules } from "../../api";
import { MODEL_CHOICES, Num, SectionTitle, Txt, lbl, setIn } from "./fields";
import { JudgedLabelsEditor } from "./JudgedLabelsEditor";
import { LEVELS as HL, ROUTING as H, SECTION, TEAM as HT } from "./help";

const EFFORTS = [{ value: "default", label: "model default" }, ...["low", "medium", "high", "xhigh", "max"].map((e) => ({ value: e, label: e }))];
const LEVELS: [number, string][] = [[1, "1 · trivial"], [2, "2 · simple"], [3, "3 · ordinary coding"], [4, "4 · agentic / hard"], [5, "5 · hardest"]];
const PRIORITIES = [
  { value: "cost", label: "Cost: faster only breaks ties" },
  { value: "balanced", label: "Balanced: pay up to 20% more for speed" },
  { value: "speed", label: "Speed: pay up to double for speed" },
];
const PREMIUM_DEFAULTS: Record<string, number> = { cost: 2, balanced: 20, speed: 100 };
const LEARNED_MODES = [
  { value: "off", label: "Off: the rules decide" },
  { value: "shadow", label: "Shadow: record and compare, change nothing" },
  { value: "route", label: "Route: use its level when sure" },
];
const SUCCESS_MODES = [
  { value: "off", label: "Off: the benchmark prior alone" },
  { value: "shadow", label: "Shadow: train and record, change nothing" },
  { value: "route", label: "Route: use it once it beats the prior" },
];
const CLASS_LEVELS = [{ value: "none", label: "not capped" }, ...LEVELS.map(([l, label]) => ({ value: String(l), label }))];
const REQUEST_CLASSES: [string, string][] = [["auxiliary", "Side requests (titles, summaries)"], ["compaction", "Compaction summaries"],
  ["subagent", "Subagent turns"], ["main", "Conversation turns"]];
// Hard benchmarks in Epoch's difficulty table, easiest first (a harder reference spreads models further apart).
const REFERENCES = ["SWE-bench Verified", "GPQA diamond", "FrontierCode", "FrontierSWE", "OSWorld 2.0", "Remote Labor Index"];

const LEVEL_OPTIONS = LEVELS.map(([l, label]) => ({ value: String(l), label }));
type WordList = "coding_words" | "simple_words" | "hard_words" | "hardest_words" | "analysis_words";

/** A list of patterns, one per line. Keeps its own text so blank lines can be typed; the draft gets the non-blank
 * lines, or nothing (the default) when they're the defaults. */
function Patterns({ label, help, value, defaults, onChange }: {
  label: string; help: string; value: string[] | undefined; defaults: string[] | undefined; onChange: (v: string[] | undefined) => void;
}) {
  const shown = (value ?? defaults ?? []).join("\n");
  const [text, setText] = useState(shown);
  const lines = (t: string) => t.split("\n").map((l) => l.trim()).filter(Boolean);
  useEffect(() => {
    if (lines(text).join("\n") !== shown) setText(shown);  // changed from outside: discarded, or the defaults arrived
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown]);
  const custom = value != null && value.length > 0 && JSON.stringify(value) !== JSON.stringify(defaults);
  return (
    <Stack gap={2}>
      <Textarea {...lbl(label, help)} value={text} autosize minRows={3} maxRows={10} spellCheck={false}
        description={custom ? "Changed from the default." : value?.length === 0 ? "Empty: the default applies." : "The default."}
        placeholder={defaults ? "" : "loading the defaults…"}
        styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)", fontSize: 12 } }}
        onChange={(e) => {
          const t = e.currentTarget.value;
          setText(t);
          const next = lines(t);
          onChange(defaults && JSON.stringify(next) === JSON.stringify(defaults) ? undefined : next);
        }} />
      {custom && <Anchor size="xs" onClick={() => onChange(undefined)}>Back to the default</Anchor>}
    </Stack>
  );
}

export function RoutingEditor({ api, draft, setDraft, modelNames, levelDefaults }: {
  api: Api; draft: Doc; setDraft: (d: Doc) => void; modelNames: string[]; levelDefaults: LevelRules | null;
}) {
  const r: Doc = draft.routing ?? {};
  const set = (path: (string | number)[], v: unknown) => setDraft(setIn(draft, path, v));
  const lv: Doc = r.levels ?? {};
  const setLevel = (k: keyof LevelRules, v: unknown) => set(["routing", "levels", k], v);
  const levelSelect = (k: keyof LevelRules, label: string, help: string, fallback: number) => (
    <Select {...lbl(label, help)} data={LEVEL_OPTIONS} allowDeselect={false} value={String(lv[k] ?? levelDefaults?.[k] ?? fallback)}
      onChange={(v) => setLevel(k, v == null || Number(v) === (levelDefaults?.[k] ?? fallback) ? undefined : Number(v))} />
  );
  const byTools = Boolean(lv.agent_by_tools ?? levelDefaults?.agent_by_tools ?? false);
  const words = (k: WordList, label: string, help: string) => (
    <Patterns label={label} help={help} value={lv[k]} defaults={levelDefaults?.[k]} onChange={(v) => setLevel(k, v)} />
  );
  const effort: Record<string, string> = r.effort_by_floor ?? { 1: "low", 2: "low", 3: "medium", 4: "high", 5: "high" };
  const setEffort = (level: number, v: string | null) => {
    const next = { ...effort };
    if (!v || v === "default") delete next[level];
    else next[level] = v;
    set(["routing", "effort_by_floor"], next);
  };
  const classLevels: Record<string, number> = r.request_class_levels ?? { auxiliary: 1, compaction: 3 };
  const setClassLevel = (cls: string, v: string | null) => {
    const next = { ...classLevels };
    if (!v || v === "none") delete next[cls];
    else next[cls] = Number(v);
    set(["routing", "request_class_levels"], next);
  };
  const choice: string = r.model_choice ?? "expected_cost";
  const human: number = r.human_cost_usd ?? 1;
  const teams: Doc = draft.teams ?? {};
  const lr: Doc = r.learned ?? {};
  const setLearned = (k: string, v: unknown) => set(["routing", "learned", k], v);
  const premium: Record<string, number> = r.priority_premium_pct ?? {};
  const setPremium = (p: string, v: number | undefined) => {
    const next = { ...premium };
    if (v == null) delete next[p];
    else next[p] = v;
    set(["routing", "priority_premium_pct"], Object.keys(next).length ? next : undefined);
  };
  return (
    <Stack gap="md" maw={900}>
      <Paper>
        <SectionTitle title="Savings baseline" intro={SECTION.baseline} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          <Select {...lbl("Baseline model", H.baseline_model)} description="'What you'd use without Merit Order'. Teams can override."
            data={modelNames} searchable value={draft.baseline_model ?? null} onChange={(v) => set(["baseline_model"], v ?? undefined)} />
          <Num label="Fallback max output tokens" value={draft.default_max_tokens} onChange={(v) => set(["default_max_tokens"], v)} min={256} step={1024}
            description="Only for models whose output limit isn't known." placeholder="16,000" help={H.default_max_tokens} />
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Automatic routing" intro={SECTION.routing} />
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
          <Txt label="Model name that means 'choose for me'" value={r.auto_model} onChange={(v) => set(["routing", "auto_model"], v)} placeholder="auto" mono help={H.auto_model} />
          <Num label="Move a session only if this much cheaper" value={r.switch_margin_pct} onChange={(v) => set(["routing", "switch_margin_pct"], v)} min={0} suffix=" %"
            description="After paying to rebuild its prompt cache elsewhere." placeholder="10 %" help={H.switch_margin_pct} />
          <Num label="…plus at least" value={r.switch_margin_usd} onChange={(v) => set(["routing", "switch_margin_usd"], v)} min={0} step={0.01} prefix="$" placeholder="$0.01" help={H.switch_margin_usd} />
          <Num label="Expected output tokens per turn" value={r.est_output_tokens} onChange={(v) => set(["routing", "est_output_tokens"], v)} min={1} placeholder="600" help={H.est_output_tokens} />
          <Num label="Expected new input tokens per turn" value={r.est_new_tokens_per_turn} onChange={(v) => set(["routing", "est_new_tokens_per_turn"], v)} min={1} placeholder="2,000" help={H.est_new_tokens_per_turn} />
          <Num label="Max backends to try per request" value={r.max_attempts} onChange={(v) => set(["routing", "max_attempts"], v)} min={1} placeholder="3" help={H.max_attempts} />
          <Num label="Assume at least this many turns left" value={r.min_expected_turns} onChange={(v) => set(["routing", "min_expected_turns"], v)} min={1} placeholder="3" help={H.min_expected_turns} />
          <Num label="…and at most" value={r.max_expected_turns} onChange={(v) => set(["routing", "max_expected_turns"], v)} min={1} placeholder="30" help={H.max_expected_turns} />
        </SimpleGrid>
        <Switch mt="md" {...lbl("Send requests for older versions to the current one", H.upgrade_versions)}
          description="claude-sonnet-4-5 gets claude-sonnet-5-5; responses say so. Off: other versions get a 404 naming the current one."
          checked={r.upgrade_versions !== false} onChange={(e) => set(["routing", "upgrade_versions"], e.currentTarget.checked ? true : false)} />
      </Paper>
      <Paper>
        <SectionTitle title="Threshold or cost per success" intro={SECTION.model_choice} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          <Select {...lbl("Choose models by", H.model_choice)} data={MODEL_CHOICES} allowDeselect={false} value={r.model_choice ?? "expected_cost"}
            onChange={(v) => set(["routing", "model_choice"], v === "expected_cost" ? undefined : v)} />
          <Num label="Never below this chance of success (0–1)" value={r.min_success_chance} onChange={(v) => set(["routing", "min_success_chance"], v)}
            min={0.01} max={0.99} step={0.05} placeholder="0.6" help={H.min_success_chance}
            description={choice === "threshold" ? "Used with cost per success and expected total cost." : undefined} />
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Expected total cost" intro={SECTION.expected_cost} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          <Num label="A failed level-5 attempt costs a person" value={r.human_cost_usd} onChange={(v) => set(["routing", "human_cost_usd"], v)} min={0} step={0.25}
            prefix="$" placeholder="$1.00" help={H.human_cost_usd}
            description={`${human === 0 ? "Only tokens count." : `About ${Math.round(human / 1 * 10) / 10} minute${human === 1 ? "" : "s"} of a developer at $60 an hour; a third as much for each level below 5.`}${choice === "expected_cost" ? "" : " Used with expected total cost."}`} />
          <Select {...lbl("Fitted layer of the success predictor", H.success_mode)} data={SUCCESS_MODES} allowDeselect={false} value={r.success?.mode ?? "off"}
            onChange={(v) => set(["routing", "success", "mode"], v === "off" ? undefined : v)}
            description={(lr.mode ?? "off") === "off" && (r.success?.mode ?? "off") !== "off" ? "Needs the learned router on (below): it records the outcomes." : undefined} />
          <Num label="At most this many escalations per task" value={r.cascade?.max_escalations} onChange={(v) => set(["routing", "cascade", "max_escalations"], v)}
            min={0} max={2} step={1} placeholder="0 (off)" help={H.max_escalations} />
          <Num label="Escalate mid-task after this many failures in a row" value={r.cascade?.failures_now} onChange={(v) => set(["routing", "cascade", "failures_now"], v)}
            min={1} step={1} placeholder="3" help={H.failures_now} />
        </SimpleGrid>
        {Object.keys(teams).length > 0 && (
          <SimpleGrid cols={{ base: 1, sm: 3 }} mt="sm">
            {Object.keys(teams).map((name) => (
              <Num key={name} label={`…for team ${name}`} value={teams[name]?.human_cost_usd} onChange={(v) => set(["teams", name, "human_cost_usd"], v)}
                min={0} step={0.25} prefix="$" placeholder={`organization's ($${human.toFixed(2)})`} help={HT.human_cost_usd} />
            ))}
          </SimpleGrid>
        )}
      </Paper>
      <Paper>
        <SectionTitle title="Learned router" intro={SECTION.learned} />
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
          <Select {...lbl("Learned router mode", H.learned_mode)} data={LEARNED_MODES} allowDeselect={false} value={lr.mode ?? "off"}
            onChange={(v) => setLearned("mode", v === "off" ? undefined : v)} />
          <Num label="Use its level when at least this sure (0–1)" value={lr.min_confidence} onChange={(v) => setLearned("min_confidence", v)}
            min={0.01} max={0.99} step={0.05} placeholder="0.8" help={H.learned_confidence} />
          <Num label="Stale after (days)" value={lr.max_age_days} onChange={(v) => setLearned("max_age_days", v)} min={0} step={1} placeholder="30"
            help={H.learned_age} />
          <Num label="Retrain every (hours)" value={lr.retrain_hours} onChange={(v) => setLearned("retrain_hours", v)} min={1} step={1}
            placeholder="only when asked" help={H.learned_retrain} />
          <Num label="Train on the last (days)" value={lr.train_days} onChange={(v) => setLearned("train_days", v)} min={1} step={1} placeholder="30"
            help={H.learned_train_days} />
          <Num label="…at most this many requests" value={lr.train_requests} onChange={(v) => setLearned("train_requests", v)} min={100} step={1000}
            placeholder="20,000" help={H.learned_train_requests} />
          <Num label="Keep request features for (days)" value={lr.keep_days} onChange={(v) => setLearned("keep_days", v)} min={1} step={1}
            placeholder="90" help={H.learned_keep_days} />
        </SimpleGrid>
      </Paper>
      <JudgedLabelsEditor api={api} draft={draft} setDraft={setDraft} modelNames={modelNames} />
      <Paper>
        <SectionTitle title="Outcome report" intro={SECTION.exploration} />
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
          <Num label="Task starts needed on each side" value={r.benchmark?.min_outcomes} onChange={(v) => set(["routing", "benchmark", "min_outcomes"], v)}
            min={20} step={50} placeholder="200" help={H.bench_min} />
          <Num label="Does as well: at least this share (0–1)" value={r.benchmark?.as_well} onChange={(v) => set(["routing", "benchmark", "as_well"], v)}
            min={0.5} max={1} step={0.01} placeholder="0.95" help={H.bench_as_well} />
          <Num label="A canary is tried on (task starts)" value={r.benchmark?.canary_outcomes} onChange={(v) => set(["routing", "benchmark", "canary_outcomes"], v)}
            min={1} step={10} placeholder="30" help={H.bench_canary} />
          <Num label="…or for at most (days)" value={r.benchmark?.canary_days} onChange={(v) => set(["routing", "benchmark", "canary_days"], v)}
            min={1} step={1} placeholder="14" help={H.bench_canary} />
          <Select {...lbl("Apply recommendations", H.bench_auto)} allowDeselect={false} value={r.benchmark?.auto_apply ? "auto" : "admin"}
            data={[{ value: "admin", label: "When an admin does" }, { value: "auto", label: "Automatically, when the learned router agrees" }]}
            onChange={(v) => set(["routing", "benchmark", "auto_apply"], v === "auto" || undefined)} />
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Cost or speed" intro={SECTION.priority} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          <Select {...lbl("Priority", H.priority)} data={PRIORITIES} allowDeselect={false} value={r.priority ?? "cost"}
            onChange={(v) => set(["routing", "priority"], v === "cost" ? undefined : v)} />
          <Num label="Check another route's speed every Nth new session" value={r.speed_check_every} onChange={(v) => set(["routing", "speed_check_every"], v)}
            min={0} placeholder="10" help={H.speed_check_every} />
        </SimpleGrid>
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="sm">
          {Object.entries(PREMIUM_DEFAULTS).map(([p, d]) => (
            <Num key={p} label={`A faster route may cost more, ${p}`} value={premium[p]} onChange={(v) => setPremium(p, v)} min={0} suffix=" %"
              placeholder={`${d} %`} help={H.premium} />
          ))}
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Quality scores" intro={SECTION.quality} />
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
          <Select {...lbl("Source", H.quality_source)} data={[{ value: "epoch", label: "Epoch AI benchmarks" }, { value: "artificial_analysis", label: "Artificial Analysis index" },
            { value: "config", label: "Scores in this config" }]} value={draft.quality?.source ?? "epoch"} onChange={(v) => set(["quality", "source"], v ?? undefined)} allowDeselect={false} />
          <Select {...lbl("Scale", H.quality_scale)} data={[{ value: "absolute", label: "Absolute (a fixed chance is 5)" },
            { value: "best", label: "Anchored to the best (5)" }, { value: "range", label: "Worst (1) to best (5)" }]}
            value={draft.quality?.scale ?? "absolute"} onChange={(v) => set(["quality", "scale"], v === "absolute" ? undefined : v)} allowDeselect={false} />
          {(draft.quality?.scale ?? "absolute") === "absolute" && ["coding", "general"].map((task) => (
            <Num key={task} label={`A 5 for ${task} solves (0–1)`} value={draft.quality?.anchor?.[task]} onChange={(v) => set(["quality", "anchor", task], v)}
              min={0.01} max={0.99} step={0.01} placeholder={task === "coding" ? "0.70" : "0.77"} help={H.quality_anchor} />
          ))}
          <Num label="Refresh every (hours)" value={draft.quality?.refresh_hours} onChange={(v) => set(["quality", "refresh_hours"], v)} min={1} step={1} placeholder="24" help={H.quality_source} />
          {["coding", "general"].map((task) => (
            <Select key={task} {...lbl(`Reference for ${task}`, H.quality_reference)} data={REFERENCES} value={draft.quality?.reference?.[task] ?? "FrontierSWE"}
              onChange={(v) => set(["quality", "reference", task], v ?? undefined)} allowDeselect={false} />
          ))}
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="How requests get a level" intro={SECTION.levels} />
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
          {levelSelect("general", "Plain text (general)", HL.general, 2)}
          {levelSelect("short", "A short question on its own (general)", HL.short, 1)}
          <Num label="Short means under this many tokens" value={lv.short_tokens} onChange={(v) => setLevel("short_tokens", v)} min={0} step={100}
            placeholder={String(levelDefaults?.short_tokens ?? 400)} help={HL.short_tokens} />
          {levelSelect("analysis", "Analysis wording (general)", HL.analysis, 3)}
          {levelSelect("coding", "Tools or code-like text (coding)", HL.coding, 3)}
          {levelSelect("simple", "Simple wording (coding)", HL.simple, 2)}
          {levelSelect("hardest", "The hardest wording (coding)", HL.hardest, 5)}
          <Num label="Hard wording raises coding by" value={lv.hard_raise} onChange={(v) => setLevel("hard_raise", v)} min={0} max={4}
            placeholder={String(levelDefaults?.hard_raise ?? 1)} suffix=" levels a sign" help={HL.hard_raise} />
          <Num label="…counting this many signs at most" value={lv.hard_signals} onChange={(v) => setLevel("hard_signals", v)} min={1} max={4}
            placeholder={String(levelDefaults?.hard_signals ?? 2)} help={HL.hard_signals} />
          <Num label="A prompt correcting the last answer raises by" value={lv.correction_raise} onChange={(v) => setLevel("correction_raise", v)}
            min={0} max={4} placeholder={String(levelDefaults?.correction_raise ?? 1)} suffix=" levels" help={HL.correction_raise} />
          <Select {...lbl("Tool count sets an agent's level", HL.agent_by_tools)} allowDeselect={false} value={byTools ? "yes" : "no"}
            data={[{ value: "no", label: "No: the wording does (default)" }, { value: "yes", label: "Yes: as before decision 61" }]}
            onChange={(v) => setLevel("agent_by_tools", v === "yes" ? true : undefined)} />
          {byTools && levelSelect("agent", "An agent at work (coding)", HL.agent, 4)}
          {byTools && (
            <Num label="An agent has at least this many tools" value={lv.agent_tools} onChange={(v) => setLevel("agent_tools", v)} min={1}
              placeholder={String(levelDefaults?.agent_tools ?? 5)} help={HL.agent_tools} />
          )}
        </SimpleGrid>
        <SimpleGrid cols={{ base: 1, sm: 3 }} mt="sm">
          {words("coding_words", "Code-like text", HL.coding_words)}
          {words("simple_words", "Simple wording", HL.simple_words)}
          {words("hard_words", "Hard wording", HL.hard_words)}
          {words("hardest_words", "The hardest wording", HL.hardest_words)}
          {words("analysis_words", "Analysis wording", HL.analysis_words)}
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Reasoning effort by task difficulty" intro={SECTION.effort} />
        <SimpleGrid cols={{ base: 1, sm: 5 }} mt="xs">
          {LEVELS.map(([lvl, label]) => (
            <Select key={lvl} {...lbl(label, H.effort_level)} data={EFFORTS} value={effort[lvl] ?? "default"} onChange={(v) => setEffort(lvl, v)} allowDeselect={false} />
          ))}
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Claude Code request types" intro={SECTION.request_class} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          {REQUEST_CLASSES.map(([cls, label]) => (
            <Select key={cls} {...lbl(label, H.request_class_level)} data={CLASS_LEVELS} value={classLevels[cls] ? String(classLevels[cls]) : "none"}
              onChange={(v) => setClassLevel(cls, v)} allowDeselect={false} />
          ))}
        </SimpleGrid>
      </Paper>
      <Paper>
        <SectionTitle title="Self-hosted pricing curve" intro={SECTION.shadow} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="xs">
          <Num label="Knee (utilization 0–1)" value={r.shadow_knee} onChange={(v) => set(["routing", "shadow_knee"], v)} min={0} max={1} step={0.05} placeholder="0.6" help={H.shadow_knee} />
          <Num label="Price below the knee (fraction of API price)" value={r.shadow_floor} onChange={(v) => set(["routing", "shadow_floor"], v)} min={0} max={1} step={0.01}
            placeholder="0.02" help={H.shadow_floor} />
        </SimpleGrid>
      </Paper>
    </Stack>
  );
}
