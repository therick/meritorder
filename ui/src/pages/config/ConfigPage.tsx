import { Alert, Anchor, Badge, Button, Drawer, Group, Paper, Stack, Tabs, Text, TextInput, Textarea } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useEffect, useMemo, useState } from "react";
import { ApiError, type Api, type ConfigVersion, type Doc, type Validation } from "../../api";
import { Loading, PageTitle, Status } from "../../components/common";
import { Icon } from "../../components/icons";
import { useData } from "../../hooks";
import { AccessEditor } from "./AccessEditor";
import { BackendsEditor } from "./BackendsEditor";
import { McpEditor } from "./McpEditor";
import { ModelsEditor } from "./ModelsEditor";
import { ReportingEditor } from "./ReportingEditor";
import { RoutingEditor } from "./RoutingEditor";
import { TeamsEditor } from "./TeamsEditor";

const SECTIONS: { id: string; label: string; keys: string[] }[] = [
  { id: "teams", label: "Teams", keys: ["teams", "compressor", "response_store"] },
  { id: "models", label: "Models", keys: ["models"] },
  { id: "backends", label: "Backends", keys: ["backends"] },
  { id: "routing", label: "Routing", keys: ["routing", "baseline_model", "default_max_tokens"] },
  { id: "mcp", label: "MCP", keys: ["mcp"] },
  { id: "access", label: "Access", keys: ["auth"] },
  { id: "reporting", label: "Reporting", keys: ["reporting"] },
];
const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));
// Someone else saved since this page loaded the configuration: saving would have undone their change (a 409).
const CONFLICT = "Someone else saved the configuration since you opened it. Reload to see their change, then make yours again.";
const failure = (title: string, e: unknown) => (e instanceof ApiError && e.status === 409 && /someone else saved/.test(e.message)
  ? { color: "red", title: "Saved by someone else meanwhile", message: CONFLICT, autoClose: false as const }
  : { color: "red", title, message: errorText(e) });
const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

export function ConfigPage({ api }: { api: Api }) {
  const src = useData(() => api.configSource(), [api]);
  const status = useData(() => api.backends(), [api]);
  const levels = useData(() => api.levels(), [api]);  // the level rules' defaults, for the Routing tab
  const compressor = useData(() => api.compressor(), [api]);  // whether model-based compression's service answers
  const teamStatus = useData(() => api.teams(), [api]);  // what each team keeps in stored mode (sizes, never content)
  const stored = useMemo(() => teamStatus.data ? {
    teams: Object.fromEntries(teamStatus.data.teams.map((t) => [t.name, t.stored_responses])), key: teamStatus.data.stored_key,
  } : null, [teamStatus.data]);
  const agentTools = useData(() => api.agentTools(), [api]);  // what each team's agents offer, as suggestions
  const agentSeen = useMemo(() => agentTools.data ? Object.fromEntries(agentTools.data.teams.map((t) => [t.team, t.seen])) : undefined, [agentTools.data]);
  const [tick, setTick] = useState(0);
  const versions = useData(() => api.configVersions(), [api, tick]);
  const [original, setOriginal] = useState<Doc | null>(null);
  const [draft, setDraft] = useState<Doc | null>(null);
  const [tab, setTab] = useState<string | null>("teams");
  const [yamlText, setYamlText] = useState("");
  const [check, setCheck] = useState<Validation | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const [viewing, setViewing] = useState<(ConfigVersion & { yaml: string }) | null>(null);

  useEffect(() => {
    if (src.data) {
      setOriginal(src.data.document);
      setDraft(src.data.document);
      setYamlText(src.data.yaml);
    }
  }, [src.data]);

  const changed = useMemo(() => {
    if (!draft || !original) return [];
    const known = new Set(SECTIONS.flatMap((s) => s.keys));
    const out = SECTIONS.filter((s) => s.keys.some((k) => !same(draft[k], original[k]))).map((s) => s.label);
    const other = [...new Set([...Object.keys(draft), ...Object.keys(original)])].filter((k) => !known.has(k) && !same(draft[k], original[k]));
    return other.length ? [...out, "Other"] : out;
  }, [draft, original]);

  const edit = (d: Doc) => { setDraft(d); setCheck(null); };
  const names = (k: string) => Object.keys(draft?.[k] ?? {});

  const onTab = async (t: string | null) => {
    if (t === "yaml" && draft) {
      try {
        const r = await api.validateConfig({ document: draft });
        setYamlText(r.yaml ?? yamlText);
      } catch { /* keep the last text */ }
    }
    setTab(t);
  };
  const applyYaml = async () => {
    setBusy("yaml");
    try {
      const r = await api.validateConfig({ yaml: yamlText });
      if (r.document) setDraft(r.document);
      setCheck(r);
      if (r.document) notifications.show({ color: r.ok ? "green" : "amber", title: "Forms updated from YAML", message: r.ok ? "It validates." : r.error ?? "" });
    } catch (e) {
      notifications.show({ color: "red", title: "Couldn't read the YAML", message: errorText(e) });
    } finally { setBusy(null); }
  };
  const validate = async () => {
    if (!draft) return;
    setBusy("validate");
    try { setCheck(await api.validateConfig({ document: draft })); } catch (e) { setCheck({ ok: false, error: errorText(e) }); } finally { setBusy(null); }
  };
  const save = async () => {
    if (!draft) return;
    setBusy("save");
    try {
      const v = await api.validateConfig({ document: draft });
      if (!v.ok) { setCheck(v); return; }
      const r = await api.saveConfig({ document: draft }, comment || `Edited ${changed.join(", ")}`, src.data?.version ?? null);
      notifications.show({ color: "green", title: `Saved as version ${r.version}`, message: "Every gateway applies it within a few seconds." });
      setComment("");
      setCheck(null);
      setTick((n) => n + 1);
      await src.reload();
    } catch (e) {
      notifications.show(failure("Not saved", e));
    } finally { setBusy(null); }
  };
  const restore = async (id: number) => {
    try {
      const r = await api.restoreConfig(id, src.data?.version ?? null);
      notifications.show({ color: "green", title: `Version ${id} restored`, message: `Saved as version ${r.version}.` });
      setViewing(null);
      setTick((n) => n + 1);
      await src.reload();
    } catch (e) {
      notifications.show(failure("Couldn't restore", e));
    }
  };

  return (
    <>
      <PageTitle title="Configuration">
        <Group gap="sm">
          {src.data && <Badge color={src.data.source === "database" ? "mo" : "gray"}>
            {src.data.source === "database" ? `version ${src.data.version}` : "from the YAML file"}</Badge>}
          <Button size="xs" variant="default" onClick={() => setHistory(true)}>History</Button>
        </Group>
      </PageTitle>
      {src.data?.storage === "memory" && <Alert color="yellow" icon={<Icon name="warn" size={16} />} mb="md" title="No database">Saving needs Postgres (MERITORDER_DATABASE_URL). You can still edit and validate.</Alert>}
      <Loading loading={src.loading && !draft} error={src.error} rows={6} />
      {draft && (
        <>
          <Tabs value={tab} onChange={onTab} keepMounted={false}>
            <Tabs.List mb="md">
              {SECTIONS.map((s) => (
                <Tabs.Tab key={s.id} value={s.id} rightSection={changed.includes(s.label) ? <Badge size="xs" circle color="mo" variant="filled" aria-label="changed"> </Badge> : null}>{s.label}</Tabs.Tab>
              ))}
              <Tabs.Tab value="yaml" ml="auto">YAML</Tabs.Tab>
            </Tabs.List>
            <Tabs.Panel value="teams"><TeamsEditor draft={draft} setDraft={edit} modelNames={names("models")} backendNames={names("backends")}
              compressor={compressor.data ?? null} stored={stored} agentSeen={agentSeen} /></Tabs.Panel>
            <Tabs.Panel value="mcp"><McpEditor draft={draft} setDraft={edit} /></Tabs.Panel>
            <Tabs.Panel value="models"><ModelsEditor draft={draft} setDraft={edit} backendNames={names("backends")} /></Tabs.Panel>
            <Tabs.Panel value="backends"><BackendsEditor draft={draft} setDraft={edit} modelNames={names("models")} status={status.data?.backends ?? []} /></Tabs.Panel>
            <Tabs.Panel value="routing"><RoutingEditor api={api} draft={draft} setDraft={edit} modelNames={names("models")} levelDefaults={levels.data?.defaults ?? null} /></Tabs.Panel>
            <Tabs.Panel value="access"><AccessEditor draft={draft} setDraft={edit} teamNames={names("teams").length ? names("teams") : ["default"]} /></Tabs.Panel>
            <Tabs.Panel value="reporting"><ReportingEditor draft={draft} setDraft={edit} api={api} /></Tabs.Panel>
            <Tabs.Panel value="yaml">
              <Paper>
                <Text size="sm" c="dimmed" mb="xs">The whole configuration as YAML, for anything the forms don't cover. Edits here update the forms when you apply them.</Text>
                <Textarea className="yaml-editor" value={yamlText} onChange={(e) => setYamlText(e.currentTarget.value)} autosize minRows={20} maxRows={40} spellCheck={false} aria-label="Configuration YAML" />
                <Group mt="sm" justify="flex-end"><Button variant="default" loading={busy === "yaml"} onClick={applyYaml}>Apply YAML to the forms</Button></Group>
              </Paper>
            </Tabs.Panel>
          </Tabs>
          <div style={{ height: 96 }} />
          {(changed.length > 0 || check) && (
            <Paper shadow="md" style={{ position: "fixed", left: 16, right: 16, bottom: 16, zIndex: 150, maxWidth: 1180, margin: "0 auto" }}>
              <Stack gap="xs">
                {check && !check.ok && <Alert color="red" icon={<Icon name="bad" size={16} />} title="Not valid yet">{check.error}</Alert>}
                {check?.ok && check.summary && (
                  <Status kind="good" style={{ whiteSpace: "normal" }}>Valid: {check.summary.models} chat models, {check.summary.backends_enabled.length} live backends, teams {check.summary.teams.join(", ")}.
                    {check.summary.unpriced_routes.length ? ` ${check.summary.unpriced_routes.length} routes have no price.` : ""}</Status>
                )}
                <Group gap="sm" wrap="nowrap">
                  <Text size="sm" fw={600} style={{ whiteSpace: "nowrap" }}>{changed.length ? `Unsaved: ${changed.join(", ")}` : "No changes"}</Text>
                  <TextInput placeholder="What changed? (shown in history)" value={comment} onChange={(e) => setComment(e.currentTarget.value)} style={{ flex: 1 }} aria-label="Change description" />
                  <Button variant="subtle" color="gray" onClick={() => { setDraft(original); setCheck(null); }} disabled={!changed.length}>Discard</Button>
                  <Button variant="default" onClick={validate} loading={busy === "validate"}>Validate</Button>
                  <Button onClick={save} loading={busy === "save"} disabled={!changed.length || src.data?.storage === "memory"}>Save and apply</Button>
                </Group>
              </Stack>
            </Paper>
          )}
        </>
      )}
      <Drawer opened={history} onClose={() => setHistory(false)} position="right" title="Configuration history">
        <Loading loading={versions.loading} error={versions.error} />
        {versions.data && !versions.data.versions.length && <Text size="sm" c="dimmed">No saved versions yet. The YAML file is in effect until you save here.</Text>}
        <Stack gap="sm">
          {versions.data?.versions.map((v) => (
            <Group key={v.id} justify="space-between" wrap="nowrap">
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={600}>v{v.id}{v.id === versions.data!.active && <Badge ml={6} size="sm" color="mo">active</Badge>}</Text>
                <Text size="xs" c="dimmed">{new Date(v.created_at).toLocaleString()} · {v.author}</Text>
                {v.comment && <Text size="xs">{v.comment}</Text>}
              </div>
              <Anchor size="xs" onClick={async () => setViewing(await api.configVersion(v.id))}>View</Anchor>
            </Group>
          ))}
        </Stack>
        {viewing && (
          <Stack mt="lg" gap="xs">
            <Text size="sm" fw={600}>Version {viewing.id}</Text>
            <Textarea className="yaml-editor" value={viewing.yaml} readOnly autosize minRows={10} maxRows={22} />
            <Group justify="flex-end">
              <Button variant="default" size="xs" onClick={() => { setYamlText(viewing.yaml); setTab("yaml"); setHistory(false); }}>Open in YAML tab</Button>
              <Button size="xs" onClick={() => restore(viewing.id)}>Restore this version</Button>
            </Group>
          </Stack>
        )}
      </Drawer>
    </>
  );
}
