import { Status } from "../../components/common";
import { ActionIcon, Badge, Button, Divider, Drawer, Group, MultiSelect, NumberInput, Paper, SegmentedControl, Select, SimpleGrid, Stack, Table,
  TagsInput, Text, TextInput } from "@mantine/core";
import { useMemo, useState } from "react";
import type { Doc } from "../../api";
import { Help, Num, SectionTitle, Txt, lbl, rich, setIn } from "./fields";
import { MODEL as H, SECTION } from "./help";

const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];
const PRICE_FIELDS: [keyof typeof H, string][] = [["input", "Input"], ["cache_read", "Cached read"], ["cache_write", "Cache write"],
  ["cache_write_1h", "1-hour cache write"], ["output", "Output"]];
const PAGE_FIELDS: [keyof typeof H, string][] = [["per_page", "Per page"], ["per_annotated_page", "Per annotated page"]];
const KINDS = [{ value: "chat", label: "Chat" }, { value: "embedding", label: "Embedding" }, { value: "ocr", label: "OCR" }];

function RouteCard({ route, backends, ocr, onChange, onRemove }: { route: Doc; backends: string[]; ocr: boolean; onChange: (r: Doc) => void; onRemove: () => void }) {
  const priced = route.price != null;
  return (
    <Paper p="sm">
      <Group align="flex-end" gap="xs" wrap="nowrap">
        <Select {...lbl("Backend", H.route_backend)} data={backends} value={route.backend ?? null} onChange={(v) => onChange({ ...route, backend: v ?? undefined })} searchable w={180} />
        <TextInput {...lbl("Model ID at that backend", H.route_model)} value={route.model ?? ""} style={{ flex: 1 }}
          styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)" } }} onChange={(e) => onChange({ ...route, model: e.currentTarget.value })} />
        <ActionIcon variant="subtle" color="red" aria-label="Remove route" onClick={onRemove} mb={4}>×</ActionIcon>
      </Group>
      <Group mt="xs" gap="xs" align="center">
        <SegmentedControl size="xs" value={priced ? "set" : "catalog"} onChange={(v) => onChange({ ...route, price: v === "set" ? (ocr ? { per_page: 0 } : { input: 0, output: 0 }) : undefined })}
          data={[{ value: "catalog", label: "Price from catalog" }, { value: "set", label: "Set price" }]} aria-label="Price source" />
        <Help text={H.price_mode} about="price source" />
        {!priced && <Text size="xs" c="dimmed">Uses prices.json (Models → Refresh prices). Unknown price = never chosen by auto.</Text>}
      </Group>
      {priced && (
        <SimpleGrid cols={{ base: 2, sm: ocr ? 2 : 5 }} mt="xs">
          {(ocr ? PAGE_FIELDS : PRICE_FIELDS).map(([k, label]) => (
            <NumberInput key={k} {...lbl(ocr ? `${label} $` : `${label} $/M`, H[k])} size="xs" min={0} decimalScale={6} value={route.price?.[k] ?? ""}
              placeholder={k.startsWith("cache") || k === "per_annotated_page" ? "auto" : "0"}
              onChange={(v) => onChange(setIn(route, ["price", k], v === "" || v == null ? undefined : Number(v)))} />
          ))}
        </SimpleGrid>
      )}
    </Paper>
  );
}

function ModelForm({ model, backends, onChange }: { model: Doc; backends: string[]; onChange: (m: Doc) => void }) {
  const set = (path: (string | number)[], v: unknown) => onChange(setIn(model, path, v));
  const routes: Doc[] = model.routes ?? [];
  const quality: Record<string, number> = model.quality ?? {};
  const override: Record<string, number> = model.quality_override ?? {};
  const isEmbedding = model.kind === "embedding";
  const isOcr = model.kind === "ocr";
  const notChat = isEmbedding || isOcr;
  return (
    <Stack gap="md">
      <SimpleGrid cols={2}>
        <Select {...lbl("Kind", H.kind)} data={KINDS} value={model.kind ?? "chat"}
          onChange={(v) => set(["kind"], v === "chat" ? undefined : v)} allowDeselect={false}
          description={isEmbedding ? "Never substituted: vectors from different models aren't comparable."
            : isOcr ? "Served at /v1/ocr, priced per page; never substituted for another OCR model." : undefined} />
        {!isOcr && <Num label="Context window (tokens)" value={model.context_window} onChange={(v) => set(["context_window"], v)} min={1} step={1000}
          description="Blank: from the catalog. Auto routing skips models too small for a session." help={H.context_window} />}
        {!notChat && <Num label="Max output tokens" value={model.max_output_tokens} onChange={(v) => set(["max_output_tokens"], v)} min={1} step={1000}
          description="Blank: from the catalog. What a request without max_tokens may write." help={H.max_output_tokens} />}
      </SimpleGrid>
      <TagsInput {...lbl("Other names clients may use", H.aliases)} description='Glob patterns, e.g. "claude-sonnet-*" so Claude Code model names land here.'
        value={model.aliases ?? []} onChange={(v) => set(["aliases"], v.length ? v : undefined)} clearable />
      {!notChat && (
        <>
          <Divider />
          <SectionTitle title="Quality (1–5): what auto may use it for" right={<Help text={H.quality} about="quality scores" />} />
          <Text size="xs" c="dimmed">With a live quality source (Routing tab), these are only used where the source doesn't cover the model. An override always wins.</Text>
          <SimpleGrid cols={3}>
            {["coding", "general", ...Object.keys(quality).filter((k) => k !== "coding" && k !== "general")].map((task) => (
              <NumberInput key={task} label={task} aria-label={`Quality for ${task}`} min={0} max={5} step={0.1} decimalScale={1} value={quality[task] ?? ""} placeholder="–"
                onChange={(v) => set(["quality", task], v === "" || v == null ? undefined : Number(v))} />
            ))}
          </SimpleGrid>
          <SimpleGrid cols={3}>
            {["coding", "general"].map((task) => (
              <NumberInput key={task} {...lbl(`Override: ${task}`, H.quality_override)} aria-label={`Quality override for ${task}`} min={0} max={5} step={0.1} decimalScale={1}
                value={override[task] ?? ""} placeholder="none" onChange={(v) => set(["quality_override", task], v === "" || v == null ? undefined : Number(v))} />
            ))}
            <TextInput {...lbl("Name in benchmark data", H.benchmark_name)} value={model.benchmark_name ?? ""} placeholder="from its route ids"
              onChange={(e) => set(["benchmark_name"], e.currentTarget.value || undefined)} />
          </SimpleGrid>
          <MultiSelect {...lbl("Reasoning effort levels it accepts", H.effort_levels)} description="Blank: detected from the catalog. Lets auto lower effort on simple tasks."
            data={EFFORT_LEVELS} value={model.effort_levels ?? []} onChange={(v) => set(["effort_levels"], v.length ? v : undefined)} clearable />
        </>
      )}
      {!isOcr && <Txt label="Hugging Face repository (open weights)" value={model.hf_id} onChange={(v) => set(["hf_id"], v)} placeholder="e.g. Qwen/Qwen3-Coder-Next" mono
        description="Needed to self-host it on GPUs, and to appear in the what-if calculator." help={H.hf_id} />}
      <Divider />
      <Group justify="space-between">
        <SectionTitle title="Routes: where it can run" />
        <Button size="xs" variant="default" onClick={() => set(["routes"], [...routes, { backend: backends[0], model: "" }])}>Add route</Button>
      </Group>
      <Text size="xs" c="dimmed">{H.routes}</Text>
      {routes.map((r, i) => (
        <RouteCard key={i} route={r} backends={backends} ocr={isOcr} onChange={(nr) => set(["routes", i], nr)} onRemove={() => set(["routes", i], undefined)} />
      ))}
      {!routes.length && <Status kind="bad">A model needs at least one route.</Status>}
    </Stack>
  );
}

export function ModelsEditor({ draft, setDraft, backendNames }: { draft: Doc; setDraft: (d: Doc) => void; backendNames: string[] }) {
  const models: Doc = draft.models ?? {};
  const [editing, setEditing] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [newName, setNewName] = useState("");
  const names = useMemo(() => Object.keys(models).filter((n) => n.toLowerCase().includes(filter.toLowerCase())), [models, filter]);
  const add = () => {
    const n = newName.trim();
    if (!n || models[n]) return;
    setDraft(setIn(draft, ["models", n], { quality: { coding: 3, general: 3 }, routes: [{ backend: backendNames[0], model: n }] }));
    setNewName("");
    setEditing(n);
  };
  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">{rich(SECTION.models)}</Text>
      <Group justify="space-between" align="flex-end">
        <TextInput placeholder="Filter models" value={filter} onChange={(e) => setFilter(e.currentTarget.value)} w={260} aria-label="Filter models" />
        <Group gap="xs" align="flex-end">
          <TextInput placeholder="new-model-name" value={newName} onChange={(e) => setNewName(e.currentTarget.value)} aria-label="New model name"
            onKeyDown={(e) => e.key === "Enter" && add()} />
          <Button variant="default" onClick={add} disabled={!newName.trim()}>Add model</Button>
        </Group>
      </Group>
      <Table.ScrollContainer minWidth={820}>
        <Table highlightOnHover verticalSpacing={5} fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Model</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Quality</Table.Th><Table.Th>Routes</Table.Th><Table.Th>Effort</Table.Th><Table.Th /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {names.map((n) => {
              const m = models[n] ?? {};
              return (
                <Table.Tr key={n} style={{ cursor: "pointer" }} onClick={() => setEditing(n)}>
                  <Table.Td fw={600}>{n}</Table.Td>
                  <Table.Td>{m.kind === "embedding" || m.kind === "ocr" ? <Badge size="sm" variant="default">{m.kind === "ocr" ? "OCR" : m.kind}</Badge> : "chat"}</Table.Td>
                  <Table.Td><Group gap={4}>{Object.entries(m.quality ?? {}).map(([k, v]) => <Badge key={k} size="sm" variant="default" className="mono">{k} {typeof v === "number" ? v.toFixed(1) : String(v)}</Badge>)}</Group></Table.Td>
                  <Table.Td><Group gap={4}>{(m.routes ?? []).map((r: Doc, i: number) => <Badge key={i} size="sm" variant={r.price ? "light" : "default"} color={r.price ? "mo" : "gray"} title={r.price ? "Priced here, overriding the catalog" : undefined}>{r.backend}</Badge>)}</Group></Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{(m.effort_levels ?? []).join(", ") || "auto"}</Text></Table.Td>
                  <Table.Td><Button size="compact-xs" variant="subtle">Edit</Button></Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      <Drawer opened={!!editing} onClose={() => setEditing(null)} position="right" size="xl" title={editing ? `Model: ${editing}` : ""}>
        {editing && models[editing] && (
          <Stack>
            <ModelForm model={models[editing]} backends={backendNames} onChange={(m) => setDraft(setIn(draft, ["models", editing], m))} />
            <Group justify="space-between" mt="md">
              <Button color="red" variant="outline" onClick={() => { setDraft(setIn(draft, ["models", editing], undefined)); setEditing(null); }}>Delete model</Button>
              <Button onClick={() => setEditing(null)}>Done</Button>
            </Group>
          </Stack>
        )}
      </Drawer>
    </Stack>
  );
}
