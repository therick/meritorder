import { Alert, Badge, Button, Divider, Drawer, Group, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import type { Doc } from "../../api";
import { Icon } from "../../components/icons";
import { EnvName, KeyValue, Num, SectionTitle, Txt, lbl, setIn } from "./fields";
import { MCP as H } from "./help";

type Props = { draft: Doc; setDraft: (d: Doc) => void };

const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const AUTH = [{ value: "none", label: "None" }, { value: "api_key", label: "The gateway's key (one for everyone)" },
  { value: "forward", label: "Each person's own token" }];

/** Servers in the document as a mapping (name -> settings); a list in the YAML is read as one. */
function servers(draft: Doc): Doc {
  const s = draft.mcp?.servers;
  if (Array.isArray(s)) return Object.fromEntries(s.filter((x) => x?.name).map(({ name, ...rest }) => [name, rest]));
  return s ?? {};
}

function ServerForm({ name, server, onChange }: { name: string; server: Doc; onChange: (s: Doc) => void }) {
  const set = (path: (string | number)[], v: unknown) => onChange(setIn(server, path, v));
  const auth = server.auth ?? {};
  const type = auth.type ?? "none";
  const env = typeof auth.value_env === "string" ? auth.value_env : "";
  const suggested = `MERITORDER_MCP_${name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
  return (
    <Stack gap="md">
      <Txt label="URL" value={server.url} onChange={(v) => set(["url"], v)} placeholder="https://mcp.example.com/mcp" mono help={H.url} />
      <Txt label="Description" value={server.description} onChange={(v) => set(["description"], v)} placeholder="What it's for" help={H.description} />
      <Switch {...lbl("Enabled", H.enabled)} checked={server.enabled !== false} onChange={(e) => set(["enabled"], e.currentTarget.checked ? undefined : false)} />
      <Divider />
      <SectionTitle title="Signing in to the server" />
      <Select {...lbl("Credential", H.auth_type)} data={AUTH} value={type} allowDeselect={false}
        onChange={(v) => set(["auth"], v === "none" ? undefined : { ...auth, type: v })} />
      {type !== "none" && (
        <SimpleGrid cols={2}>
          <Txt label="Header" value={auth.header} onChange={(v) => set(["auth", "header"], v)} placeholder="authorization" mono help={H.auth_header} />
          <Txt label="Prefix" value={auth.prefix} onChange={(v) => set(["auth", "prefix"], v)} placeholder="Bearer (for authorization)" help={H.auth_prefix} />
        </SimpleGrid>
      )}
      {type === "api_key" && (
        <SimpleGrid cols={2}>
          <EnvName label="Credential (env var)" value={auth.value_env} onChange={(v) => set(["auth", "value_env"], v)} help={H.auth_value_env}
            description={`Must start with MERITORDER_MCP_, e.g. ${suggested}.`} />
          <Txt label="Credential file" value={auth.value_file} onChange={(v) => set(["auth", "value_file"], v)} placeholder={`/run/secrets/meritorder/${suggested}`} mono
            help={H.auth_value_file} />
        </SimpleGrid>
      )}
      {type === "api_key" && env && !env.startsWith("MERITORDER_MCP_") && (
        <Alert color="red" icon={<Icon name="bad" size={16} />}>The variable must start with MERITORDER_MCP_ (e.g. {suggested}), or this won't save.</Alert>
      )}
      {type === "api_key" && !auth.value_env && !auth.value_file && (
        <Alert color="yellow" icon={<Icon name="warn" size={16} />}>No credential is set: name the variable or the file that holds it.</Alert>
      )}
      {type === "forward" && (
        <Txt label="Header agents send the person's token in" value={auth.forward_header} onChange={(v) => set(["auth", "forward_header"], v)}
          placeholder="x-meritorder-mcp-authorization" mono help={H.forward_header} />
      )}
      <KeyValue label="Other headers" value={server.headers} onChange={(v) => set(["headers"], v)} keyLabel="Header" valueLabel="Value" help={H.headers} />
      <Divider />
      <SectionTitle title="Tools" />
      <SimpleGrid cols={2}>
        <Select {...lbl("Pin tool definitions", H.pinning)} allowDeselect={false} value={server.pinning ?? "shadow"}
          data={[{ value: "off", label: "Off" }, { value: "shadow", label: "Shadow: flag changes" }, { value: "enforce", label: "Enforce: hold changed tools for approval" }]}
          onChange={(v) => set(["pinning"], v === "shadow" ? undefined : v)} />
        <Select {...lbl("New tools", H.new_tools)} allowDeselect={false} value={server.new_tools ?? "allow"}
          data={[{ value: "allow", label: "Allow when first seen" }, { value: "hold", label: "Hold for approval" }]}
          onChange={(v) => set(["new_tools"], v === "allow" ? undefined : v)} />
        <Num label="Timeout (seconds)" value={server.timeout_seconds} onChange={(v) => set(["timeout_seconds"], v)} min={1} placeholder="300" help={H.timeout_seconds} />
      </SimpleGrid>
      {server.new_tools === "hold" && server.pinning !== "enforce" && (
        <Alert color="yellow" icon={<Icon name="warn" size={16} />}>Holding new tools needs pinning on Enforce; until then they're allowed.</Alert>
      )}
      <Switch {...lbl("Its results are untrusted content", H.untrusted_content)} description="Web pages, tickets, email: words from outside your organization."
        checked={!!server.untrusted_content} onChange={(e) => set(["untrusted_content"], e.currentTarget.checked || undefined)} />
    </Stack>
  );
}

export function McpEditor({ draft, setDraft }: Props) {
  const list = servers(draft);
  const [editing, setEditing] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const names = Object.keys(list);
  const write = (next: Doc) => setDraft(setIn(draft, ["mcp", "servers"], Object.keys(next).length ? next : undefined));
  const add = () => {
    const n = newName.trim();
    if (!NAME.test(n) || list[n]) return;
    write({ ...list, [n]: { url: "" } });
    setNewName("");
    setEditing(n);
  };
  const bad = newName.trim() !== "" && !NAME.test(newName.trim());
  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">{H.section}</Text>
      <Table.ScrollContainer minWidth={700}>
        <Table highlightOnHover verticalSpacing={6} fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Server</Table.Th><Table.Th>URL</Table.Th><Table.Th>Credential</Table.Th><Table.Th>Pinning</Table.Th><Table.Th /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {names.map((n) => {
              const s = list[n] ?? {};
              return (
                <Table.Tr key={n} style={{ cursor: "pointer" }} onClick={() => setEditing(n)}>
                  <Table.Td fw={600}>{n}{s.enabled === false && <Badge size="xs" color="gray" ml={6}>off</Badge>}
                    {s.untrusted_content && <Badge size="xs" color="yellow" ml={6}>untrusted</Badge>}</Table.Td>
                  <Table.Td><Text size="xs" ff="monospace" lineClamp={1}>{s.url || "–"}</Text></Table.Td>
                  <Table.Td><Text size="xs">{AUTH.find((a) => a.value === (s.auth?.type ?? "none"))?.label}</Text></Table.Td>
                  <Table.Td><Text size="xs">{s.pinning ?? "shadow"}{s.new_tools === "hold" ? ", new held" : ""}</Text></Table.Td>
                  <Table.Td><Button size="compact-xs" variant="subtle">Edit</Button></Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {!names.length && <Text size="sm" c="dimmed">No servers yet. Agents reach a server you add at /mcp/&lt;name&gt; on this gateway.</Text>}
      <Group gap="xs" align="flex-end">
        <TextInput label="New server" placeholder="e.g. linear" value={newName} onChange={(e) => setNewName(e.currentTarget.value)}
          onKeyDown={(e) => e.key === "Enter" && add()} error={bad ? "Letters, digits, . _ - (up to 64)" : undefined} />
        <Button variant="default" onClick={add} disabled={!newName.trim() || bad || !!list[newName.trim()]}>Add server</Button>
      </Group>
      <Divider />
      <Num label="Keep the call log for (days)" value={draft.mcp?.log_days} onChange={(v) => setDraft(setIn(draft, ["mcp", "log_days"], v))} min={1} placeholder="90"
        help={H.log_days} w={260} />

      <Drawer opened={!!editing} onClose={() => setEditing(null)} position="right" size="lg" title={editing ? `MCP server: ${editing}` : ""}>
        {editing && list[editing] !== undefined && (
          <Stack gap="md">
            <ServerForm name={editing} server={list[editing] ?? {}} onChange={(s) => write({ ...list, [editing]: s })} />
            {!list[editing]?.url && <Alert color="red" icon={<Icon name="bad" size={16} />}>A URL is required.</Alert>}
            <Group justify="space-between" mt="md">
              <Button color="red" variant="outline" onClick={() => { const { [editing]: _, ...rest } = list; write(rest); setEditing(null); }}>Delete server</Button>
              <Button onClick={() => setEditing(null)}>Done</Button>
            </Group>
          </Stack>
        )}
      </Drawer>
    </Stack>
  );
}
