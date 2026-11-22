import { Badge, Button, Code, Group, Modal, SegmentedControl, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useState } from "react";
import type { AgentToolsOverview, Api, McpCall, McpOverview, McpServer, McpTool, McpToolDetail, Me } from "../api";
import { num } from "../api";
import { Kpi, Loading, PageTitle, Panel, StatusBadge, type StatusKind } from "../components/common";
import { useData } from "../hooks";
import { Person } from "../people";

const PIN: Record<McpTool["status"], { label: string; kind: StatusKind; help: string }> = {
  approved: { label: "pinned", kind: "good", help: "Its definition is the one approved (or first seen)" },
  changed: { label: "changed", kind: "warn", help: "Its name, description or schemas changed since they were approved: review what changed" },
  new: { label: "waiting", kind: "clock", help: "New, and this server holds new tools until an admin approves them" },
};

const CALL: Record<string, { label: string; kind: StatusKind; help: string }> = {
  ok: { label: "ok", kind: "good", help: "The tool answered" },
  error: { label: "tool error", kind: "warn", help: "The tool answered that it failed (isError)" },
  failed: { label: "failed", kind: "bad", help: "No usable answer: the server was unreachable, refused the gateway, or the stream broke" },
  refused: { label: "refused", kind: "bad", help: "The gateway refused the call; the agent was told why" },
  no_answer: { label: "no answer", kind: "off", help: "The server's reply didn't answer the call" },
};

const FINDING: Record<string, string> = {
  steering_description: "addresses the model",
  unicode_tags: "hidden characters",
  unicode_bidi: "direction overrides",
  unicode_invisible: "invisible characters",
};

const AUTH: Record<McpServer["auth"], string> = {
  none: "no credential",
  api_key: "the gateway's credential",
  forward: "each person's own credential",
};

const REASON: Record<string, string> = {
  not_allowed: "the team may not use it",
  tool_changed: "its definition changed",
  tool_new: "waiting for approval",
  rate_limited: "over the per-minute limit",
  secret_in_arguments: "a key in its arguments",
  batch: "another call in its batch was refused",
  unreachable: "server unreachable",
  upstream_auth: "the server refused the credential",
  stream_broken: "the stream broke",
  too_large: "the answer was too large",
};

function Connect({ s }: { s: McpServer }) {
  const url = `${window.location.origin}${s.path}`;
  const extra = s.auth === "forward" ? ` --header "${s.forward_header}: Bearer $YOUR_${s.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_TOKEN"` : "";
  return (
    <Stack gap={4}>
      <Text size="xs" c="dimmed">Claude Code</Text>
      <Code block>{`claude mcp add --transport http ${s.name} ${url} --header "Authorization: Bearer $MERITORDER_API_KEY"${extra}`}</Code>
      <Text size="xs" c="dimmed">Codex (~/.codex/config.toml)</Text>
      <Code block>{`[mcp_servers.${s.name}]\nurl = "${url}"\nbearer_token_env_var = "MERITORDER_API_KEY"`}</Code>
    </Stack>
  );
}

function Review({ api, tool, canApprove, onClose }: { api: Api; tool: McpTool; canApprove: boolean; onClose: (changed: boolean) => void }) {
  const res = useData<McpToolDetail>(() => api.mcpTool(tool.server, tool.name), [api, tool.server, tool.name]);
  const [busy, setBusy] = useState(false);
  const d = res.data;
  const approve = async () => {
    if (!d) return;
    setBusy(true);
    try {
      await api.approveMcpTool(d.server, d.name, d.hash);
      notifications.show({ color: "green", title: "Tool approved", message: `${d.server}/${d.name}: agents see this definition now.` });
      onClose(true);
    } catch (e) {
      notifications.show({ color: "red", title: "Couldn't approve the tool", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened onClose={() => onClose(false)} title={`${tool.server}/${tool.name}`} size="xl">
      {res.loading || res.error || !d ? <Loading loading={res.loading} error={res.error} /> : (
        <Stack gap="sm">
          <Text size="sm">Read what the server tells the model about this tool. A description that tells the model to read files, send data, or
            keep something from the person isn't describing a tool.</Text>
          <SimpleGrid cols={{ base: 1, md: d.approved_definition ? 2 : 1 }}>
            {d.approved_definition && <Stack gap={4}>
              <Text size="xs" c="dimmed">Approved{d.approved_by ? ` by ${d.approved_by}` : " (as first seen)"}</Text>
              <Code block style={{ maxHeight: 420, overflow: "auto" }}>{JSON.stringify(d.approved_definition, null, 2)}</Code>
            </Stack>}
            <Stack gap={4}>
              <Text size="xs" c="dimmed">Now{d.changed_at ? `, since ${new Date(d.changed_at).toLocaleString()}` : ""}</Text>
              <Code block style={{ maxHeight: 420, overflow: "auto" }}>{JSON.stringify(d.definition, null, 2)}</Code>
            </Stack>
          </SimpleGrid>
          {!!d.findings.length && <Text size="sm" c="var(--mo-warn)">Noticed: {d.findings.map((f) => FINDING[f] ?? f).join(", ")}</Text>}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => onClose(false)}>Close</Button>
            {canApprove && d.status !== "approved" && <Button loading={busy} onClick={approve}>Approve this definition</Button>}
          </Group>
        </Stack>
      )}
    </Modal>
  );
}

function Calls({ calls }: { calls: McpCall[] }) {
  return (
    <Table.ScrollContainer minWidth={860}>
      <Table>
        <Table.Thead>
          <Table.Tr><Table.Th>When</Table.Th><Table.Th>Who</Table.Th><Table.Th>Tool</Table.Th><Table.Th>Status</Table.Th>
            <Table.Th ta="right">Time</Table.Th><Table.Th>Noticed</Table.Th></Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {calls.map((c) => {
            const st = CALL[c.status] ?? { label: c.status, kind: "off" as StatusKind, help: "" };
            return (
              <Table.Tr key={c.id}>
                <Table.Td className="mono" style={{ whiteSpace: "nowrap" }}>{new Date(c.ts).toLocaleString()}</Table.Td>
                <Table.Td><Person id={c.user_id} /> <Text span size="xs" c="dimmed">{c.team}</Text></Table.Td>
                <Table.Td className="mono">{c.server}/{c.tool}</Table.Td>
                <Table.Td><StatusBadge size="sm" kind={st.kind} title={st.help}>{st.label}</StatusBadge>
                  {c.reason && <Text size="xs" c="dimmed">{REASON[c.reason] ?? c.reason}</Text>}</Table.Td>
                <Table.Td className="num">{c.duration_ms == null ? "–" : `${num(c.duration_ms)} ms`}</Table.Td>
                <Table.Td><Text size="xs">{Object.keys(c.findings).filter((k) => k !== c.reason).map((k) => k.replace(/_/g, " ")).join(", ")}</Text></Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

const KIND = { mcp: "MCP server", tool: "Tool", skill: "Skill" } as const;
const MODE = { off: "off", shadow: "shadow: listed, nothing changes", enforce: "enforced: what isn't allowed is left out" } as const;

/** What each team's agents offer the model, from their requests (decision 76), and what the team's policy leaves out. */
function AgentTools({ data }: { data: AgentToolsOverview }) {
  const teams = data.teams.filter((t) => t.policy.mode !== "off");
  const [team, setTeam] = useState<string | null>(null);
  const [kind, setKind] = useState<string>("all");
  if (!teams.length) {
    return <Text c="dimmed" size="sm">No team looks at what its agents bring. Turn on Agent tools for a team (Config → Teams → the team → What agents bring) to
      list the MCP servers, tools and skills Claude Code, Codex and pi offer, and to leave out what the team doesn't allow.</Text>;
  }
  const t = teams.find((x) => x.team === team) ?? teams[0];
  const rows = t.seen.filter((s) => kind === "all" || s.kind === kind);
  const ago = (at: number) => new Date(at * 1000).toLocaleString();
  return (
    <Stack gap="sm">
      <Group gap="sm" wrap="wrap">
        {teams.length > 1 && <SegmentedControl size="xs" value={t.team} onChange={setTeam} data={teams.map((x) => x.team)} />}
        <SegmentedControl size="xs" value={kind} onChange={setKind}
          data={[{ value: "all", label: "All" }, { value: "mcp", label: "MCP servers" }, { value: "tool", label: "Tools" }, { value: "skill", label: "Skills" }]} />
        <Text size="xs" c="dimmed">Team {t.team}: {MODE[t.policy.mode]}{t.policy.gateway_mcp_only ? "; only MCP servers through this gateway" : ""}</Text>
      </Group>
      {rows.length ? (
        <Table.ScrollContainer minWidth={640}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Kind</Table.Th><Table.Th>Name</Table.Th><Table.Th>Policy</Table.Th><Table.Th>Last seen</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {rows.map((s) => (
                <Table.Tr key={`${s.kind}:${s.name}`}>
                  <Table.Td><Text size="sm">{KIND[s.kind] ?? s.kind}</Text></Table.Td>
                  <Table.Td className="mono">{s.name}{s.kind === "mcp" && data.gateway_servers.includes(s.name) && <Badge size="xs" ml={6} color="mo">gateway</Badge>}</Table.Td>
                  <Table.Td>{s.left_out
                    ? <StatusBadge size="sm" kind={t.policy.mode === "enforce" ? "bad" : "warn"}>{t.policy.mode === "enforce" ? "left out" : "would be left out"}</StatusBadge>
                    : <StatusBadge size="sm" kind="good">allowed</StatusBadge>}</Table.Td>
                  <Table.Td className="mono" style={{ whiteSpace: "nowrap" }}>{ago(s.at)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      ) : <Text c="dimmed" size="sm">Nothing seen yet from this team's agents. Each name is noted at most every ten minutes.</Text>}
    </Stack>
  );
}

/** The MCP gateway (decision 75): the tool servers agents reach through Merit Order, their tools as pinned, and the calls. */
export function Tools({ api, me }: { api: Api; me: Me }) {
  const res = useData<McpOverview>(() => api.mcp(7), [api], 30_000);
  const log = useData(() => api.mcpCalls(100), [api], 30_000);
  const agents = useData(() => api.agentTools(), [api], 60_000);
  const [review, setReview] = useState<McpTool | null>(null);
  const [connect, setConnect] = useState<string | null>(null);
  const d = res.data;
  const title = <PageTitle title="Tools" sub="MCP servers your agents reach through Merit Order, and what agents bring themselves: who may use which tools, pinned tool definitions, and every call" />;
  if (res.loading || res.error || !d) return <>{title}<Loading loading={res.loading} error={res.error} /></>;

  const total = d.counts.reduce((a, c) => a + c.calls, 0);
  const refused = d.counts.filter((c) => c.status === "refused").reduce((a, c) => a + c.calls, 0);
  const failed = d.counts.filter((c) => c.status === "failed").reduce((a, c) => a + c.calls, 0);
  const review_ = d.tools.filter((t) => t.status !== "approved");
  return (
    <Stack gap="md">
      {title}
      <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
        <Kpi label="Servers" value={num(d.servers.filter((s) => s.enabled !== false).length)} sub={`${num(d.tools.length)} tools pinned`} />
        <Kpi label="Tool calls" value={num(total)} sub={`last ${d.days} days${me.role === "team_lead" ? `, team ${me.team}` : ""}`} />
        <Kpi label="Refused" value={num(refused)} sub={`by the gateway; ${num(failed)} more failed at the server`} />
        <Kpi label="To review" value={num(review_.length)} accent={review_.length > 0} sub="tools whose definition changed, or that wait for approval" />
      </SimpleGrid>

      <Panel title="Servers" right={<Text size="xs" c="dimmed">Registered on Config → MCP; agents sign in with their Merit Order key</Text>}>
        {d.servers.length ? (
          <Table.ScrollContainer minWidth={900}>
            <Table>
              <Table.Thead>
                <Table.Tr><Table.Th>Server</Table.Th><Table.Th>Sends on to</Table.Th><Table.Th>Signs in with</Table.Th><Table.Th>Pinning</Table.Th>
                  <Table.Th ta="right">Tools</Table.Th><Table.Th /></Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {d.servers.map((s) => (
                  <Table.Tr key={s.name}>
                    <Table.Td><Text className="mono" size="sm">{s.name}</Text>{s.description && <Text size="xs" c="dimmed">{s.description}</Text>}
                      {s.enabled === false && <Text size="xs" c="dimmed">disabled</Text>}</Table.Td>
                    <Table.Td className="mono"><Text size="xs">{s.url}</Text></Table.Td>
                    <Table.Td><Text size="sm">{AUTH[s.auth]}</Text>{s.untrusted_content && <Text size="xs" c="dimmed">results count as untrusted content</Text>}</Table.Td>
                    <Table.Td><Text size="sm">{s.pinning === "enforce" ? "enforced" : s.pinning === "shadow" ? "recorded" : "off"}</Text>
                      {s.pinning === "enforce" && s.new_tools === "hold" && <Text size="xs" c="dimmed">new tools wait for approval</Text>}</Table.Td>
                    <Table.Td className="num">{num(s.tools ?? 0)}</Table.Td>
                    <Table.Td><Button size="compact-xs" variant="default" onClick={() => setConnect(connect === s.name ? null : s.name)}>Connect</Button></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        ) : (
          <Text c="dimmed" size="sm">No MCP servers are registered. {me.role === "admin" ? "Add one on Config → MCP" : "An admin adds them on Config → MCP"}, and
            agents reach it at <code>/mcp/&lt;name&gt;</code> with their Merit Order key.</Text>
        )}
        {connect && d.servers.find((s) => s.name === connect) && <div style={{ marginTop: 12 }}><Connect s={d.servers.find((s) => s.name === connect)!} /></div>}
      </Panel>

      <Panel title="Tools" right={<Text size="xs" c="dimmed">Each tool's definition is pinned when first seen; a change is flagged, and with pinning enforced held until approved</Text>}>
        {d.tools.length ? (
          <Table.ScrollContainer minWidth={900}>
            <Table>
              <Table.Thead>
                <Table.Tr><Table.Th>Tool</Table.Th><Table.Th>Definition</Table.Th><Table.Th>Noticed</Table.Th><Table.Th>First seen</Table.Th>
                  <Table.Th ta="right">Calls</Table.Th><Table.Th /></Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {d.tools.map((t) => {
                  const calls = d.counts.filter((c) => c.server === t.server && c.tool === t.name).reduce((a, c) => a + c.calls, 0);
                  return (
                    <Table.Tr key={`${t.server}/${t.name}`}>
                      <Table.Td className="mono">{t.server}/{t.name}</Table.Td>
                      <Table.Td><StatusBadge size="sm" kind={PIN[t.status].kind} title={PIN[t.status].help}>{PIN[t.status].label}</StatusBadge>
                        {t.changed_at && <Text size="xs" c="dimmed">changed {new Date(t.changed_at).toLocaleString()}</Text>}</Table.Td>
                      <Table.Td><Text size="xs" c={t.findings.length ? "var(--mo-warn)" : "dimmed"}>{t.findings.map((f) => FINDING[f] ?? f).join(", ") || "–"}</Text></Table.Td>
                      <Table.Td className="mono" style={{ whiteSpace: "nowrap" }}>{new Date(t.first_seen).toLocaleDateString()}</Table.Td>
                      <Table.Td className="num">{num(calls)}</Table.Td>
                      <Table.Td><Button size="compact-xs" variant={t.status === "approved" ? "default" : "filled"} onClick={() => setReview(t)}>
                        {t.status !== "approved" && d.can_approve ? "Review" : "View"}</Button></Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        ) : <Text c="dimmed" size="sm">No tools seen yet: they're pinned the first time an agent lists a server's tools.</Text>}
      </Panel>

      <Panel title="What agents bring" right={<Text size="xs" c="dimmed">Their own MCP servers, tools and skills, read from model requests (Claude Code, Codex, pi)</Text>}>
        {agents.loading || agents.error || !agents.data ? <Loading loading={agents.loading} error={agents.error} /> : <AgentTools data={agents.data} />}
      </Panel>

      <Panel title="Recent calls" right={<Text size="xs" c="dimmed">Who called which tool and how it went; the arguments and results aren't kept</Text>}>
        {log.loading || log.error || !log.data ? <Loading loading={log.loading} error={log.error} /> :
          log.data.calls.length ? <Calls calls={log.data.calls} /> : <Text c="dimmed" size="sm">No tool calls yet.</Text>}
      </Panel>
      {review && <Review api={api} tool={review} canApprove={d.can_approve} onClose={(changed) => { setReview(null); if (changed) res.reload(); }} />}
    </Stack>
  );
}
