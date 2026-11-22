import { AppShell, Burger, Button, Group, Menu, NavLink, ScrollArea, Text, UnstyledButton, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Api, ApiError, auth, SESSION_ENDED } from "./api";
import { useData } from "./hooks";
import { Keys, MyUsage, Teams } from "./pages/Admin";
import { AcceptInvite, ChangePassword, SignIn, TwoFactor } from "./pages/SignIn";
import { ConfigPage } from "./pages/config/ConfigPage";
import { People } from "./pages/People";
import { Dispatch } from "./pages/Dispatch";
import { Backends, Models } from "./pages/Infra";
import { Gpus } from "./pages/Gpus";
import { Controls } from "./pages/Controls";
import { Reports } from "./pages/Reports";
import { Tools } from "./pages/Tools";
import { SourceFooter } from "./components/SourceFooter";
import { PeopleContext } from "./people";
import { BrandMark, Icon } from "./components/icons";
import { DEMO_KEY, DemoBanner } from "./demo/Banner";

/** Where consoles before decision 69 kept the API key they signed in with. Read once, traded for a session, removed. */
const OLD_KEY_STORAGE = "meritorder.key";

function takeOldKey(): string | null {
  try {
    const k = localStorage.getItem(OLD_KEY_STORAGE);
    localStorage.removeItem(OLD_KEY_STORAGE);
    return k;
  } catch {
    return null;
  }
}

const inviteToken = () => (location.hash.startsWith("#invite=") ? decodeURIComponent(location.hash.slice(8)) : null);

/** Why an SSO sign-in failed: the gateway sends the browser back to /ui/#sso-error=<message>. Read once, then cleared. */
function takeSsoError(): string | null {
  if (!location.hash.startsWith("#sso-error=")) return null;
  const message = decodeURIComponent(location.hash.slice(11));
  history.replaceState(null, "", location.pathname);
  return `Single sign-on didn't work: ${message}`;
}

type Page = { id: string; label: string; roles: string[] };
const PAGES: Page[] = [
  { id: "dispatch", label: "Dispatch", roles: ["admin", "finance", "team_lead"] },
  { id: "reports", label: "Reports", roles: ["admin", "finance", "team_lead", "user"] },
  { id: "gpus", label: "GPUs", roles: ["admin", "finance", "team_lead"] },
  { id: "controls", label: "Controls", roles: ["admin", "finance"] },
  { id: "backends", label: "Backends", roles: ["admin", "team_lead"] },
  { id: "models", label: "Models", roles: ["admin"] },
  { id: "teams", label: "Teams", roles: ["admin", "finance", "team_lead"] },
  { id: "people", label: "People", roles: ["admin", "team_lead"] },
  { id: "keys", label: "Keys", roles: ["admin", "team_lead"] },
  { id: "tools", label: "Tools", roles: ["admin", "team_lead"] },
  { id: "config", label: "Config", roles: ["admin"] },
  { id: "me", label: "My usage", roles: ["admin", "finance", "team_lead", "user"] },
];

/** A text toggle ("Light" / "Dark": the scheme it switches to) rather than a glyph. */
function SchemeToggle() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const dark = colorScheme !== "light";
  return (
    <Button variant="default" size="compact-sm" h={32} px={10} leftSection={<Icon name="theme" />}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"} onClick={() => setColorScheme(dark ? "light" : "dark")}>
      <Text span inherit visibleFrom="xs">{dark ? "Light" : "Dark"}</Text>
    </Button>
  );
}

export default function App() {
  const api = useMemo(() => new Api(__DEMO__ ? DEMO_KEY : null), []);  // the demo signs in as its admin; the console, by cookie
  const [epoch, setEpoch] = useState(0);  // bumped on sign-in and sign-out, to ask the gateway who this is again
  const [invite, setInvite] = useState(inviteToken);
  const [migrating, setMigrating] = useState(() => !__DEMO__ && !inviteToken());
  const [ssoError] = useState(takeSsoError);
  const [ended, setEnded] = useState<string | null>(null);  // why a session stopped working, for the sign-in page
  // Signed out is an answer, not an error: null. /auth/session answers 200 either way, so a signed-out console logs no error.
  const me = useData(async () => {
    if (migrating) return new Promise<never>(() => {});
    if (!api.key) {
      const s = await auth.session();
      setEnded(s.signed_in ? null : s.reason ?? null);
      if (!s.signed_in) return null;
    }
    return api.me().catch((e) => {
      if (e instanceof ApiError && e.status === 401) return null;
      throw e;
    });
  }, [api, epoch, migrating]);
  const people = useData(() => (me.data ? api.people() : Promise.resolve({ people: {} })), [api, me.data?.user], 60_000);
  const [page, setPage] = useState(() => location.hash.slice(1));
  const [opened, { toggle, close }] = useDisclosure(false);
  const [changing, password] = useDisclosure(false);
  const [twoFactor, twoFactorPanel] = useDisclosure(false);

  useEffect(() => {
    if (!migrating) return;
    const old = takeOldKey();
    if (!old) { setMigrating(false); return; }
    auth.login({ api_key: old }).catch(() => undefined).finally(() => setMigrating(false));
  }, [migrating]);

  useEffect(() => {
    const onHash = () => { setPage(location.hash.slice(1)); setInvite(inviteToken()); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((id: string) => {
    location.hash = id;
    close();
  }, [close]);
  const signedIn = () => setEpoch((n) => n + 1);
  // A call that finds the session ended asks the gateway again (GET /auth/session), which shows the sign-in page with the
  // reason. The page in the address bar is kept, so signing in again returns to it.
  useEffect(() => {
    const ended = () => setEpoch((n) => n + 1);
    window.addEventListener(SESSION_ENDED, ended);
    return () => window.removeEventListener(SESSION_ENDED, ended);
  }, []);
  const signOut = async () => {
    try { await auth.logout(); } catch { /* the cookie is cleared or already gone; show sign-in either way */ }
    signedIn();
  };

  if (invite) return <AcceptInvite token={invite} onDone={() => { setInvite(null); signedIn(); }} />;
  if (me.loading && !me.data) return null;
  if (!me.data) return <SignIn onSignedIn={signedIn} notice={me.error ?? ssoError ?? ended} />;
  const role = me.data.role;
  const shown = me.data.name || me.data.email || me.data.user;  // an SSO id is a GUID: show the person's name instead
  const pages = PAGES.filter((p) => p.roles.includes(role));
  const active = pages.find((p) => p.id === page.split("/")[0])?.id ?? pages[0].id;  // "models/evals": a tab within a page
  // The current page in the accent; the rest as quiet text buttons. Below 1240px (eleven admin pages) they fold into
  // the burger's side panel.
  const nav = (vertical: boolean) => pages.map((p) => vertical
    ? <NavLink key={p.id} label={p.label} active={active === p.id} onClick={() => go(p.id)} />
    : <Button key={p.id} size="compact-sm" h={30} px={10} fw={active === p.id ? 500 : 400} variant={active === p.id ? "light" : "subtle"}
        color={active === p.id ? "mo" : "gray"} c={active === p.id ? undefined : "var(--mo-fg-2)"} aria-current={active === p.id ? "page" : undefined}
        onClick={() => go(p.id)}>{p.label}</Button>);

  return (
    <PeopleContext.Provider value={people.data?.people ?? {}}>
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: 1240, collapsed: { desktop: true, mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" gap={20} wrap="nowrap" maw={1480} mx="auto">
          <Burger opened={opened} onClick={toggle} className="nav-narrow" size="sm" aria-label="Menu" />
          <UnstyledButton onClick={() => go(pages[0].id)} className="brand" aria-label="Merit Order console home">
            <BrandMark /><span>Merit Order</span>
          </UnstyledButton>
          <Group gap={2} className="nav-wide" wrap="nowrap">{nav(false)}</Group>
          <Group gap={10} ml="auto" wrap="nowrap">
            <span className="live hide-sm" title="Data refreshes on its own"><span className="live-dot" aria-hidden="true" />live</span>
            <SchemeToggle />
            <Menu position="bottom-end" withinPortal>
              <Menu.Target>
                <Button variant="default" size="compact-sm" h={32} px={10} leftSection={<span className="avatar" aria-hidden="true">{shown.slice(0, 1).toUpperCase()}</span>}
                  rightSection={<Icon name="chev" size={12} />} title={me.data.email ?? me.data.user}>{shown}</Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>{me.data.email && me.data.email !== shown ? `${me.data.email} · ` : ""}{me.data.team} · {role.replace("_", " ")}</Menu.Label>
                <Menu.Item onClick={() => go("me")}>My usage</Menu.Item>
                {me.data.sign_in?.has_password && me.data.sign_in.method === "password" && <Menu.Item onClick={password.open}>Change password</Menu.Item>}
                {me.data.sign_in?.has_password && me.data.sign_in.method === "password" &&
                  <Menu.Item onClick={twoFactorPanel.open}>Two-factor sign-in{me.data.sign_in.two_factor ? " (on)" : ""}</Menu.Item>}
                {!__DEMO__ && <Menu.Item color="red" onClick={() => void signOut()}>Sign out</Menu.Item>}
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs"><ScrollArea>{nav(true)}</ScrollArea></AppShell.Navbar>
      <AppShell.Main>
        {__DEMO__ && <DemoBanner />}
        <div style={{ maxWidth: 1480, margin: "0 auto", paddingTop: 6 }}>
          {active === "dispatch" && <Dispatch api={api} me={me.data} onNavigate={go} />}
          {active === "reports" && <Reports api={api} me={me.data} />}
          {active === "gpus" && <Gpus api={api} me={me.data} />}
          {active === "controls" && <Controls api={api} me={me.data} />}
          {active === "backends" && <Backends api={api} />}
          {active === "models" && <Models api={api} me={me.data} />}
          {active === "teams" && <Teams api={api} me={me.data} onNavigate={go} />}
          {active === "keys" && <Keys api={api} me={me.data} />}
          {active === "tools" && <Tools api={api} me={me.data} />}
          {active === "config" && <ConfigPage api={api} />}
          {active === "people" && <People api={api} me={me.data} />}
          {active === "me" && <MyUsage me={me.data} />}
        </div>
        <SourceFooter />
      </AppShell.Main>
      <ChangePassword opened={changing} onClose={password.close} />
      <TwoFactor opened={twoFactor} on={!!me.data.sign_in?.two_factor} onClose={(changed) => { twoFactorPanel.close(); if (changed) me.reload(); }} />
    </AppShell>
    </PeopleContext.Provider>
  );
}
