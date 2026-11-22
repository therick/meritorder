import { Icon } from "../../components/icons";
import { Status, StatusBadge } from "../../components/common";
import { Alert, Badge, Button, Divider, Drawer, Group, MultiSelect, Select, SimpleGrid, Stack, Switch, Table, TagsInput, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import type { AgentToolSeen, CompressorStatus, Doc, StoredKey, StoredSize } from "../../api";
import { bytes, num, storedLine, usd } from "../../api";
import { EnvName, MODEL_CHOICES, Num, SectionTitle, Txt, isEnvRef, lbl, setIn } from "./fields";
import { AGENT as AH, ROUTING as RH, SECTION, TEAM as H } from "./help";

/** What each team keeps in the Responses API's stored mode (GET /v1/console/teams): sizes, never content. */
export type StoredStatus = { teams: Record<string, StoredSize>; key: StoredKey | null };
type Props = { draft: Doc; setDraft: (d: Doc) => void; modelNames: string[]; backendNames: string[]; compressor?: CompressorStatus | null;
  stored?: StoredStatus | null; agentSeen?: Record<string, AgentToolSeen[]> };

const globs = (v: string[]) => (v.length ? v : undefined);
const ALL = (v: unknown) => !Array.isArray(v) || (v.length === 1 && v[0] === "*");

/** What a team may use through the MCP gateway (mcp), and of what its agents bring (agent_tools). ``seen``: what this
 * team's agents have offered (Tools page inventory), as suggestions. */
function ToolsSection({ team, set, mcpServers, seen }: { team: Doc; set: (path: (string | number)[], v: unknown) => void;
  mcpServers: string[]; seen: AgentToolSeen[] }) {
  const m = team.mcp ?? {};
  const a = team.agent_tools ?? {};
  const mode = a.mode ?? "off";
  const of = (kind: string) => [...new Set(seen.filter((s) => s.kind === kind).map((s) => s.name))];
  const suggest = (names: string[]) => ["*", ...names, ...names.map((n) => `!${n}`)];
  const serverTools = mcpServers.map((s) => `${s}/*`);
  return (
    <Stack gap="sm">
      <SectionTitle title="MCP servers through the gateway" intro={AH.section} />
      {mcpServers.length ? (
        <>
          <SimpleGrid cols={2}>
            <TagsInput {...lbl("Servers", AH.servers)} description='e.g. "*", "!github". Empty: all.' data={suggest(mcpServers)}
              value={m.servers ?? []} onChange={(v) => set(["mcp", "servers"], globs(v))} clearable />
            <TagsInput {...lbl("Tools (server/tool)", AH.tools)} description='e.g. "*", "!github/delete_*". Empty: all.' data={["*", ...serverTools, ...serverTools.map((t) => `!${t}`)]}
              value={m.tools ?? []} onChange={(v) => set(["mcp", "tools"], globs(v))} clearable />
            <Num label="Tool calls per person per minute" value={m.calls_per_minute} onChange={(v) => set(["mcp", "calls_per_minute"], v)} min={1} placeholder="no limit" help={AH.calls_per_minute} />
            <Select {...lbl("Keys in tool calls and results", AH.secrets)} allowDeselect={false} value={m.secrets ?? "shadow"}
              data={[{ value: "off", label: "Off" }, { value: "shadow", label: "Shadow: count them" }, { value: "block", label: "Block: refuse calls, remove from results" }]}
              onChange={(v) => set(["mcp", "secrets"], v === "shadow" ? undefined : v)} />
          </SimpleGrid>
          {onlyExclusions(m.servers) && <Alert color="red" icon={<Icon name="bad" size={16} />}>Servers has only exclusions, so this team can't use any. Add "*".</Alert>}
          {onlyExclusions(m.tools) && <Alert color="red" icon={<Icon name="bad" size={16} />}>Tools has only exclusions, so this team can't call any. Add "*".</Alert>}
        </>
      ) : <Text size="xs" c="dimmed">No MCP servers are registered (MCP tab).</Text>}
      <SectionTitle title="What agents bring (Claude Code, Codex, pi)" intro="Their own MCP servers, built-in tools and skills, read from every model request." />
      <Select {...lbl("Agent tools policy", AH.mode)} allowDeselect={false} value={mode}
        data={[{ value: "off", label: "Off: don't look" }, { value: "shadow", label: "Shadow: list what agents offer, change nothing" },
          { value: "enforce", label: "Enforce: leave out what isn't allowed" }]}
        onChange={(v) => set(["agent_tools", "mode"], v === "off" ? undefined : v)} />
      {mode !== "off" && (
        <>
          <SimpleGrid cols={2}>
            <TagsInput {...lbl("Their MCP servers", AH.mcp_servers)} description='e.g. "*", "!github". Empty: all.' data={suggest([...new Set([...of("mcp"), ...mcpServers])])}
              value={a.mcp_servers ?? []} onChange={(v) => set(["agent_tools", "mcp_servers"], globs(v))} clearable />
            <TagsInput {...lbl("Their other tools", AH.agent_tools)} description='e.g. "*", "!WebFetch". Empty: all.' data={suggest(of("tool"))}
              value={a.tools ?? []} onChange={(v) => set(["agent_tools", "tools"], globs(v))} clearable />
            <TagsInput {...lbl("Skills", AH.skills)} description='e.g. "*", "!deploy". Empty: all. "!*": none.' data={suggest(of("skill"))}
              value={a.skills ?? []} onChange={(v) => set(["agent_tools", "skills"], globs(v))} clearable />
          </SimpleGrid>
          <Switch {...lbl("Only MCP servers through this gateway", AH.gateway_mcp_only)} description="Servers an agent connects to directly are left out."
            checked={!!a.gateway_mcp_only} onChange={(e) => set(["agent_tools", "gateway_mcp_only"], e.currentTarget.checked || undefined)} />
          {mode === "shadow" && (!ALL(a.mcp_servers) || !ALL(a.tools) || !ALL(a.skills) || a.gateway_mcp_only) && (
            <Text size="xs" c="dimmed">Shadow: requests are tagged with what would be left out (Reports → Requests), and nothing changes until Enforce.</Text>
          )}
          {seen.length > 0 && <Text size="xs" c="dimmed">Seen from this team's agents: {of("mcp").length} MCP servers, {of("tool").length} tools, {of("skill").length} skills (Tools page).</Text>}
        </>
      )}
    </Stack>
  );
}

/** The stored mode's settings for one team, with what it holds now and whether the gateway has a key to seal it with. */
function StoredResponses({ team, set, size, storedKey }: { team: Doc; set: (path: (string | number)[], v: unknown) => void;
  size?: StoredSize; storedKey?: StoredKey | null }) {
  const s = team.stored_responses ?? {};
  const held = !!size && (size.responses > 0 || size.conversations > 0);
  return (
    <Stack gap="sm">
      <Switch {...lbl("Stored responses (OpenAI Responses API)", H.stored_responses)}
        description="Clients can chain turns with previous_response_id or conversations. The gateway then holds conversation content, sealed, for the retention below."
        checked={!!s.enabled} onChange={(e) => set(["stored_responses", "enabled"], e.currentTarget.checked || undefined)} />
      {!s.enabled && held && (
        <Alert color="red" icon={<Icon name="bad" size={16} />}>This team still holds {storedLine(size)}. With stored responses off, the worker deletes it at its next pass
          (within a minute of saving).</Alert>
      )}
      {s.enabled && (
        <>
          <SimpleGrid cols={2}>
            <Num label="Keep for (days after the last turn)" value={s.retention_days} onChange={(v) => set(["stored_responses", "retention_days"], v)}
              min={0.01} step={1} placeholder="required" help={H.stored_retention_days}
              error={s.retention_days == null ? "Required: how long the gateway keeps what it stores. There's no default." : undefined} />
            <MultiSelect {...lbl("Others who may read and continue them", H.stored_readers)} placeholder="only the person who made them"
              data={[{ value: "team_lead", label: "Team leads (this team)" }, { value: "finance", label: "Finance (this team)" },
                { value: "admin", label: "Admins" }, { value: "user", label: "Everyone on this team" }]}
              value={s.readers ?? []} onChange={(v) => set(["stored_responses", "readers"], v.length ? v : undefined)} clearable />
          </SimpleGrid>
          <Text size="xs" c="dimmed">Now: {storedLine(size)}{size?.running ? `, ${num(size.running)} background responses running` : ""}.
            People's responses are deleted when they're disabled on the People page or removed through SCIM.</Text>
          {storedKey && !storedKey.configured && !storedKey.memory && (
            <Alert color="red" icon={<Icon name="bad" size={16} />}>No key is set ({storedKey.env}{storedKey.file ? ` or ${storedKey.file}` : ""}): requests that would be stored are
              refused with 503 until every gateway and the worker have the same key.</Alert>
          )}
          {storedKey && storedKey.memory && !storedKey.configured && (
            <Alert color="yellow" icon={<Icon name="warn" size={16} />}>In-memory storage: what's stored is sealed with a key made for this process and lost when it stops.</Alert>
          )}
        </>
      )}
    </Stack>
  );
}

/** What the team's model-based compression can count on: a service set in the draft, and whether the running one answers. */
function ServiceNote({ url, status }: { url: unknown; status?: CompressorStatus | null }) {
  if (!url) return <Alert color="red" icon={<Icon name="bad" size={16} />}>No compressor service is set (Compressor service, below the team list), so this won't validate.</Alert>;
  if (isEnvRef(url) && status && !status.configured) {
    return <Alert color="yellow" icon={<Icon name="warn" size={16} />}>The service's URL comes from {String(url)}, which is empty on this gateway: set it on every process,
      or this won't validate.</Alert>;
  }
  if (status && status.configured && !status.reachable) {
    return <Alert color="yellow" icon={<Icon name="warn" size={16} />}>The compressor service isn't answering ({status.error ?? "no reply"}). Until it does, tool results are sent as they are.</Alert>;
  }
  if (status?.reachable && status.matches === false) {
    return <Alert color="yellow" icon={<Icon name="warn" size={16} />}>The service runs {status.loaded}, not {status.model}: it refuses these requests until they match.</Alert>;
  }
  return null;
}

function TeamForm({ name, team, models, backends, onChange, service, status, stored, mcpServers, seen }: {
  name: string; team: Doc; models: string[]; backends: string[]; onChange: (t: Doc) => void; service: unknown; status?: CompressorStatus | null;
  stored?: StoredStatus | null; mcpServers: string[]; seen: AgentToolSeen[];
}) {
  const set = (path: (string | number)[], v: unknown) => onChange(setIn(team, path, v));
  return (
    <Stack gap="md">
      <SectionTitle title="Money" />
      <SimpleGrid cols={2}>
        <Num label="Team budget per month" value={team.budget_usd_month} onChange={(v) => set(["budget_usd_month"], v)} min={0} prefix="$" placeholder="no limit"
          description="Requests are refused at 100%; alerts at 80%." help={H.budget_usd_month} />
        <Num label="Per person per month" value={team.user_budget_usd_month} onChange={(v) => set(["user_budget_usd_month"], v)} min={0} prefix="$" placeholder="no limit"
          description="People can have their own on the People page." help={H.user_budget_usd_month} />
        <Num label="Per session" value={team.max_session_usd} onChange={(v) => set(["max_session_usd"], v)} min={0} prefix="$" placeholder="no limit"
          description="Stops one runaway agent without blocking the person." help={H.max_session_usd} />
        <Select {...lbl("Baseline model", H.baseline_model)} description="'What you'd use without Merit Order' for savings reports." data={models} clearable searchable
          value={team.baseline_model ?? null} onChange={(v) => set(["baseline_model"], v ?? undefined)} placeholder="organization default" />
        <Select {...lbl("Cost or speed", H.priority)} clearable placeholder="organization default" value={team.priority ?? null}
          data={[{ value: "cost", label: "Cost: faster only breaks ties" }, { value: "balanced", label: "Balanced: up to 20% more for speed" },
            { value: "speed", label: "Speed: up to double for speed" }]}
          onChange={(v) => set(["priority"], v ?? undefined)} />
        <Select {...lbl("Choose models by", H.model_choice)} clearable placeholder="organization default" value={team.model_choice ?? null}
          data={MODEL_CHOICES} onChange={(v) => set(["model_choice"], v ?? undefined)} />
        <Num label="Never below this chance of success (0–1)" value={team.min_success_chance} onChange={(v) => set(["min_success_chance"], v)}
          min={0.01} max={0.99} step={0.05} placeholder="organization default" help={H.min_success_chance} />
      </SimpleGrid>
      <Divider />
      <SectionTitle title="Rate limits (per minute)" intro="Over a limit, requests get HTTP 429 with a Retry-After. Blank: no limit." />
      <SimpleGrid cols={2}>
        <Num label="Requests per person" value={team.rate_limits?.user_rpm} onChange={(v) => set(["rate_limits", "user_rpm"], v)} placeholder="no limit" min={1} help={H.user_rpm} />
        <Num label="Tokens per person" value={team.rate_limits?.user_tpm} onChange={(v) => set(["rate_limits", "user_tpm"], v)} placeholder="no limit" min={1} step={1000} help={H.user_tpm} />
        <Num label="Requests for the team" value={team.rate_limits?.team_rpm} onChange={(v) => set(["rate_limits", "team_rpm"], v)} placeholder="no limit" min={1} help={H.team_rpm} />
        <Num label="Tokens for the team" value={team.rate_limits?.team_tpm} onChange={(v) => set(["rate_limits", "team_tpm"], v)} placeholder="no limit" min={1} step={1000} help={H.team_tpm} />
      </SimpleGrid>
      <Divider />
      <SectionTitle title="What they can use" />
      <TagsInput {...lbl("Models", H.allowed_models)} description='Glob patterns; "*" is everything, "!pattern" excludes. Leave empty for everything.'
        data={["*", ...models]} value={team.allowed_models ?? []} onChange={(v) => set(["allowed_models"], v.length ? v : undefined)} clearable />
      <TagsInput {...lbl("Backends", H.allowed_backends)} description='e.g. "*", "!openrouter-free" to keep this team off free tiers that may log prompts.'
        data={["*", ...backends, ...backends.map((b) => `!${b}`)]} value={team.allowed_backends ?? []} onChange={(v) => set(["allowed_backends"], v.length ? v : undefined)} clearable />
      {onlyExclusions(team.allowed_models) && <Alert color="red" icon={<Icon name="bad" size={16} />}>Models has only exclusions, so this team can't use any model. Add "*".</Alert>}
      {onlyExclusions(team.allowed_backends) && <Alert color="red" icon={<Icon name="bad" size={16} />}>Backends has only exclusions, so this team's requests can't go anywhere. Add "*".</Alert>}
      <Switch {...lbl("Retry on another model when a model refuses", H.refusal_fallback)} description="Only for auto requests; named models always get their own answer."
        checked={team.refusal_fallback !== false} onChange={(e) => set(["refusal_fallback"], e.currentTarget.checked ? undefined : false)} />
      <Switch {...lbl("Substitute a model that can't run", H.substitute_unavailable)} description="When a requested model has nowhere to run (for example its GPUs are stopped), use one at least as good for the task instead of failing. Responses say which model was replaced."
        checked={!!team.substitute_unavailable} onChange={(e) => set(["substitute_unavailable"], e.currentTarget.checked || undefined)} />
      <Divider />
      <ToolsSection team={team} set={set} mcpServers={mcpServers} seen={seen} />
      <Divider />
      <SectionTitle title="Exploration" intro={SECTION.exploration} />
      <Switch {...lbl("Explore a level down or up", RH.explore_enabled)} description="A few task starts at random, so Reports → Levels can say if a level is too high or too low. Needs the learned router in shadow or route mode."
        checked={!!team.exploration?.enabled} onChange={(e) => set(["exploration", "enabled"], e.currentTarget.checked || undefined)} />
      {team.exploration?.enabled && (
        <SimpleGrid cols={2}>
          <Num label="Share of task starts (0–0.5)" value={team.exploration?.share} onChange={(v) => set(["exploration", "share"], v)} min={0} max={0.5} step={0.01} placeholder="0.04" help={RH.explore_share} />
          <Num label="A canary's share (0–0.5)" value={team.exploration?.canary_share} onChange={(v) => set(["exploration", "canary_share"], v)} min={0} max={0.5} step={0.05} placeholder="0.2" help={RH.explore_canary} />
        </SimpleGrid>
      )}
      <Divider />
      <SectionTitle title="Token savings" />
      <Switch {...lbl("Compress huge tool outputs", H.compression)} description="Deterministic, so provider caches keep hitting." checked={!!team.compression?.enabled}
        onChange={(e) => set(["compression", "enabled"], e.currentTarget.checked || undefined)} />
      {team.compression?.enabled && (
        <SimpleGrid cols={2}>
          <Num label="Leave outputs shorter than (chars)" value={team.compression?.min_chars} onChange={(v) => set(["compression", "min_chars"], v)} min={0} placeholder="4,000" help={H.min_chars} />
          <Num label="Keep head + tail beyond (chars)" value={team.compression?.max_chars} onChange={(v) => set(["compression", "max_chars"], v)} min={100} placeholder="24,000" help={H.max_chars} />
        </SimpleGrid>
      )}
      <Switch {...lbl("Model-based compression (LLMLingua-2)", H.compression_model)}
        description="Shortens prose in tool results with a small model. Each result is decided once and stored, so caches keep hitting."
        checked={!!team.compression?.model?.enabled} onChange={(e) => set(["compression", "model", "enabled"], e.currentTarget.checked || undefined)} />
      {team.compression?.model?.enabled && (
        <Stack gap="sm">
          <ServiceNote url={service} status={status} />
          <SimpleGrid cols={2}>
            <Num label="Keep this share of the prose (0–1)" value={team.compression?.model?.rate} onChange={(v) => set(["compression", "model", "rate"], v)}
              min={0.05} max={0.95} step={0.05} placeholder="0.5" help={H.model_rate} />
            <Num label="Leave results shorter than (chars)" value={team.compression?.model?.min_chars} onChange={(v) => set(["compression", "model", "min_chars"], v)}
              min={0} placeholder="4,000" help={H.model_min_chars} />
            <Num label="Wait for the model at most (ms)" value={team.compression?.model?.deadline_ms} onChange={(v) => set(["compression", "model", "deadline_ms"], v)}
              min={1} step={100} placeholder="1,000" help={H.deadline_ms} />
          </SimpleGrid>
          <TagsInput {...lbl("Never for these tools", H.skip_tools)} description="Blank: read, read_file, view and other file readers."
            value={team.compression?.model?.skip_tools ?? []} onChange={(v) => set(["compression", "model", "skip_tools"], v.length ? v : undefined)} clearable />
        </Stack>
      )}
      <Switch {...lbl("Response cache", H.response_cache)} description="Stores answers in Redis for exact repeats (FAQ bots, evals). Off for coding agents." checked={!!team.response_cache?.enabled}
        onChange={(e) => set(["response_cache", "enabled"], e.currentTarget.checked || undefined)} />
      {team.response_cache?.enabled && (
        <SimpleGrid cols={2}>
          <Num label="Keep for (seconds)" value={team.response_cache?.ttl_seconds} onChange={(v) => set(["response_cache", "ttl_seconds"], v)} min={60} placeholder="3,600" help={H.ttl_seconds} />
          <Select {...lbl("Share answers within", H.scope)} data={[{ value: "team", label: "the team" }, { value: "user", label: "each person only" }]}
            value={team.response_cache?.scope ?? "team"} onChange={(v) => set(["response_cache", "scope"], v === "team" ? undefined : v)} allowDeselect={false} />
        </SimpleGrid>
      )}
      <Divider />
      <SectionTitle title="Content the gateway holds" intro="Off unless you turn it on. Everything else here keeps no conversation content." />
      <StoredResponses team={team} set={set} size={stored?.teams[name]} storedKey={stored?.key} />
      <Text size="xs" c="dimmed">Team “{name}”. Blank fields use the defaults.</Text>
    </Stack>
  );
}

/** A pattern list with no including pattern allows nothing (config.py `_allowed`). */
function onlyExclusions(patterns: unknown): boolean {
  return Array.isArray(patterns) && patterns.length > 0 && patterns.every((p) => typeof p === "string" && p.startsWith("!"));
}

/** The top-level `compressor` section: the service model-based compression calls. */
function CompressorService({ value, onChange, status }: { value: Doc | undefined; onChange: (v: Doc | undefined) => void; status?: CompressorStatus | null }) {
  const set = (k: string, v: unknown) => {
    const next = setIn(value ?? {}, [k], v);
    onChange(Object.keys(next).length ? next : undefined);
  };
  const users = status?.teams ?? [];
  return (
    <Stack gap="sm">
      <SectionTitle title="Compressor service" intro="For model-based compression. Run `meritorder compressor` (Docker Compose: `--profile compressor`; Helm: `compressor.enabled`)."
        right={status?.configured ? <StatusBadge size="sm" kind={status.reachable ? (status.matches === false ? "warn" : "good") : "bad"}>
          {status.reachable ? (status.matches === false ? `runs ${status.loaded}` : `answering on ${status.device ?? "?"}`) : "not answering"}</StatusBadge> : undefined} />
      <SimpleGrid cols={2}>
        <Txt label="URL" value={value?.url} onChange={(v) => set("url", v)} placeholder="http://compressor:8090" mono help={H.compressor_url}
          description={isEnvRef(value?.url) ? "from an environment variable" : undefined} />
        <Txt label="Model" value={value?.model} onChange={(v) => set("model", v)} placeholder="microsoft/llmlingua-2-xlm-roberta-large-meetingbank" mono help={H.compressor_model} />
        <Num label="Keep decisions for (seconds)" value={value?.ttl_seconds} onChange={(v) => set("ttl_seconds", v)} min={21600} step={3600} placeholder="21,600"
          help={H.compressor_ttl} />
        <EnvName label="Shared secret (env var)" value={value?.api_key_env} onChange={(v) => set("api_key_env", v)}
          description="The gateways send it; the service checks MERITORDER_COMPRESSOR_KEY. Blank: none." />
      </SimpleGrid>
      {users.length > 0 && <Text size="xs" c="dimmed">Used by: {users.join(", ")}.</Text>}
    </Stack>
  );
}

/** The top-level `response_store` section: which key seals stored responses, and the old ones still read while rotating. */
function StoredKeys({ value, onChange, stored }: { value: Doc | undefined; onChange: (v: Doc | undefined) => void; stored?: StoredStatus | null }) {
  const set = (k: string, v: unknown) => {
    const next = setIn(value ?? {}, [k], v);
    onChange(Object.keys(next).length ? next : undefined);
  };
  const key = stored?.key;
  const oldValues = Object.values(stored?.teams ?? {}).reduce((n, s) => n + (s?.old_key ?? 0), 0);
  const users = Object.entries(stored?.teams ?? {}).filter(([, s]) => s?.enabled).map(([n]) => n);
  return (
    <Stack gap="sm">
      <SectionTitle title="Stored responses key" intro="Seals what teams with stored responses keep. The same key on every gateway and the worker. To rotate it, put the new key in the key variable and the old one in the old-keys variable; the worker seals everything again with the new key."
        right={key ? <StatusBadge size="sm" kind={key.configured ? "good" : key.memory ? "off" : users.length ? "bad" : "off"}>
          {key.configured ? `key ${key.id}` : key.memory ? "process key (in memory)" : "no key"}</StatusBadge> : undefined} />
      <SimpleGrid cols={2}>
        <EnvName label="Key (env var)" value={value?.key_env} onChange={(v) => set("key_env", v)} description="Default MERITORDER_RESPONSES_KEY." />
        <Txt label="Key file" value={value?.key_file} onChange={(v) => set("key_file", v)} placeholder="/run/secrets/meritorder/MERITORDER_RESPONSES_KEY" mono
          description="A mounted secret; wins over the variable and is re-read when it changes." />
        <EnvName label="Old keys still read (env var)" value={value?.old_keys_env} onChange={(v) => set("old_keys_env", v)}
          description="Default MERITORDER_RESPONSES_OLD_KEYS, comma-separated." />
      </SimpleGrid>
      {key && key.old_keys > 0 && (
        <Status kind={oldValues ? "warn" : "good"} style={{ whiteSpace: "normal", fontSize: 12 }}>{oldValues ? `${num(oldValues)} stored values still need an old key; the worker seals them again with the current one, ${num(500)} a minute.`
          : "Nothing needs the old keys any more: they can be removed."}</Status>
      )}
      {users.length > 0 && <Text size="xs" c="dimmed">Teams with stored responses on: {users.join(", ")}.</Text>}
    </Stack>
  );
}

export function TeamsEditor({ draft, setDraft, modelNames, backendNames, compressor, stored, agentSeen }: Props) {
  const rawServers = draft.mcp?.servers;
  const mcpServers: string[] = Array.isArray(rawServers) ? rawServers.map((x: Doc) => x?.name).filter(Boolean) : Object.keys(rawServers ?? {});
  const teams: Doc = draft.teams ?? {};
  const [editing, setEditing] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [rename, setRename] = useState("");
  const names = Object.keys(teams);

  const save = (name: string, team: Doc) => setDraft(setIn(draft, ["teams", name], team));
  const add = () => {
    const n = newName.trim().toLowerCase().replace(/[^a-z0-9_.-]/g, "-");
    if (!n || teams[n]) return;
    setDraft(setIn(draft, ["teams", n], {}));
    setNewName("");
    setEditing(n);
    setRename(n);
  };
  const remove = (n: string) => {
    if (n === "default") return;
    setDraft(setIn(draft, ["teams", n], undefined));
    setEditing(null);
  };
  const applyRename = () => {
    if (!editing || !rename || rename === editing || teams[rename]) return;
    const entries = Object.entries(teams).map(([k, v]) => (k === editing ? [rename, v] : [k, v]));
    setDraft({ ...draft, teams: Object.fromEntries(entries) });
    setEditing(rename);
  };

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        {SECTION.teams} People get their team from the sign-in mappings (Entra or Google groups, email addresses and domains: Access tab) or on the People page.
        Anyone in a team that isn't listed here uses the <b>default</b> team's settings.
      </Text>
      <Table.ScrollContainer minWidth={760}>
        <Table highlightOnHover verticalSpacing={6} fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Team</Table.Th><Table.Th ta="right">Budget / month</Table.Th><Table.Th ta="right">Per person</Table.Th>
            <Table.Th ta="right">Per session</Table.Th><Table.Th>Models</Table.Th><Table.Th>Savings</Table.Th><Table.Th>Stored responses</Table.Th>
            <Table.Th /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {names.map((n) => {
              const t = teams[n] ?? {};
              return (
                <Table.Tr key={n} style={{ cursor: "pointer" }} onClick={() => { setEditing(n); setRename(n); }}>
                  <Table.Td fw={600}>{n}{t.judged_labels?.enabled && <Badge size="sm" color="mo" ml={6}
                    title="Judged labels (Config → Routing): a sample of this team's prompts goes to a stronger model and a judge">judged labels</Badge>}</Table.Td>
                  <Table.Td className="num">{t.budget_usd_month != null ? usd(Number(t.budget_usd_month)) : "–"}</Table.Td>
                  <Table.Td className="num">{t.user_budget_usd_month != null ? usd(Number(t.user_budget_usd_month)) : "–"}</Table.Td>
                  <Table.Td className="num">{t.max_session_usd != null ? usd(Number(t.max_session_usd)) : "–"}</Table.Td>
                  <Table.Td><Text size="xs" c="dimmed" lineClamp={1}>{(t.allowed_models ?? ["*"]).join(", ")}</Text></Table.Td>
                  <Table.Td>{t.compression?.enabled && <Badge size="xs">compression</Badge>} {t.compression?.model?.enabled && <Badge size="xs">model compression</Badge>}{" "}
                    {t.response_cache?.enabled && <Badge size="xs">response cache</Badge>}{" "}
                    {t.agent_tools?.mode && t.agent_tools.mode !== "off" && <Badge size="xs" color={t.agent_tools.mode === "enforce" ? "mo" : "gray"}
                      title="Agent tools policy">tools: {t.agent_tools.mode}</Badge>}</Table.Td>
                  <Table.Td>{t.stored_responses?.enabled ? <Badge size="sm" color="mo">{t.stored_responses.retention_days ?? "?"} days</Badge>
                    : <Text span size="xs" c="dimmed">off</Text>}{" "}
                    {stored?.teams[n] && (stored.teams[n].responses > 0 || stored.teams[n].conversations > 0) &&
                      <Text span size="xs" c="dimmed">{bytes(stored.teams[n].bytes)}</Text>}</Table.Td>
                  <Table.Td><Button size="compact-xs" variant="subtle">Edit</Button></Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      <Group gap="xs" align="flex-end">
        <TextInput label="New team" placeholder="e.g. data-science" value={newName} onChange={(e) => setNewName(e.currentTarget.value)}
          onKeyDown={(e) => e.key === "Enter" && add()} />
        <Button variant="default" onClick={add} disabled={!newName.trim()}>Add team</Button>
      </Group>
      <Divider />
      <CompressorService value={draft.compressor} onChange={(v) => setDraft(setIn(draft, ["compressor"], v))} status={compressor} />
      <Divider />
      <StoredKeys value={draft.response_store} onChange={(v) => setDraft(setIn(draft, ["response_store"], v))} stored={stored} />

      <Drawer opened={!!editing} onClose={() => setEditing(null)} position="right" size="lg" title={editing ? `Team: ${editing}` : ""}>
        {editing && teams[editing] !== undefined && (
          <Stack gap="md">
            {editing !== "default" && (
              <Group align="flex-end" gap="xs">
                <TextInput label="Name" value={rename} onChange={(e) => setRename(e.currentTarget.value.toLowerCase().replace(/[^a-z0-9_.-]/g, "-"))} />
                <Button variant="default" onClick={applyRename} disabled={!rename || rename === editing || !!teams[rename]}>Rename</Button>
              </Group>
            )}
            {editing !== "default" && rename !== editing && <Alert color="yellow" icon={<Icon name="warn" size={16} />}>People assigned to “{editing}” keep that team name until you move them on the People page or update the Entra mapping.</Alert>}
            <TeamForm name={editing} team={teams[editing] ?? {}} models={modelNames} backends={backendNames} onChange={(t) => save(editing, t)}
              service={draft.compressor?.url} status={compressor} stored={stored} mcpServers={mcpServers}
              seen={agentSeen?.[editing] ?? agentSeen?.default ?? []} />
            <Group justify="space-between" mt="md">
              {editing !== "default" ? <Button color="red" variant="outline" onClick={() => remove(editing)}>Delete team</Button> : <span />}
              <Button onClick={() => setEditing(null)}>Done</Button>
            </Group>
          </Stack>
        )}
      </Drawer>
    </Stack>
  );
}
