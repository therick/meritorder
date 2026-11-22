import { createHmac } from "node:crypto";
import { allow, expect, expectLoaded, KEYS, open, signInAs, signInWithKey, test } from "./fixtures";

/** The authenticator app's code for a base32 secret (RFC 6238: SHA-1, 6 digits, 30 seconds). */
function totp(secret: string, at = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.replace(/\s/g, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from((bits.match(/.{8}/g) ?? []).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30000)));
  const h = createHmac("sha1", key).update(counter).digest();
  const o = h[h.length - 1] & 15;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

// Every page an admin can open renders its heading, loads its data, and logs no errors.
const ADMIN_PAGES: [string, string, RegExp | string][] = [
  ["dispatch", "Dispatch", "Live dispatch"],
  ["reports", "Reports", "Reports"],
  ["gpus", "GPUs", "GPUs"],
  ["controls", "Controls", "Controls"],
  ["backends", "Backends", "Backends"],
  ["models", "Models", "Models"],
  ["teams", "Teams", "Teams & budgets"],
  ["people", "People", "People"],
  ["keys", "Keys", "API keys"],
  ["tools", "Tools", "Tools"],
  ["config", "Config", "Configuration"],
  ["me", "My usage", "My usage"],
];

test("the source and the third-party notices are a click away, signed in or not", async ({ page }) => {
  await page.goto("/ui/");
  const source = page.getByRole("link", { name: "Source" });
  await expect(source).toHaveAttribute("href", "https://github.com/therick/meritorder"); // console.source_url
  await expect(page.getByText(/Merit Order \d+\.\d+\.\d+ · AGPL-3\.0-only/)).toBeVisible(); // from GET /about
  await expect(page.getByRole("link", { name: "Third-party notices" })).toHaveAttribute("href", "/ui/third-party-notices.txt");
  const notices = await page.request.get("/ui/third-party-notices.txt");
  expect(notices.ok()).toBeTruthy();
  expect(await notices.text()).toContain("Name: react");
  await signInWithKey(page, KEYS.admin);
  await expect(page.getByRole("button", { name: "root" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Source" })).toHaveAttribute("href", "https://github.com/therick/meritorder");
});

test("sign in with a key, see who you are, sign out", async ({ page, problems }) => {
  await page.goto("/ui/");
  await expect(page.getByLabel("Email address")).toBeVisible(); // people sign in with an account; keys are one click away
  await signInWithKey(page, "mo-not-a-real-key");
  await expect(page.getByText(/invalid API key/i)).toBeVisible();
  allow(problems, /status of 401 .*\/auth\/login/); // the browser logs the refused request

  await page.getByLabel("API key").fill(KEYS.admin);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "root" })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(KEYS.admin); // the browser keeps no key
  const cookie = (await page.context().cookies()).find((c) => c.name === "mo_session");
  expect(cookie?.httpOnly && cookie.sameSite === "Strict").toBeTruthy();
  await page.reload(); // the session is remembered
  await page.getByRole("button", { name: "root" }).click();
  await expect(page.getByText("default · admin")).toBeVisible();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByLabel("Email address")).toBeVisible();
  await page.reload(); // and ended on the gateway, not only in this tab
  await expect(page.getByLabel("Email address")).toBeVisible();
});

test("a session that ends while the page is open goes back to sign-in, and signing in returns to the page", async ({ page, problems }) => {
  await signInAs(page, "admin");
  await open(page, "keys");
  await expect(page.getByText("API keys", { exact: true }).first()).toBeVisible();
  // End the session on the gateway (as an expiry would) but keep the browser's cookie.
  const cookie = (await page.context().cookies()).find((c) => c.name === "mo_session")!;
  await page.request.post("/auth/logout", { headers: { "x-meritorder-console": "1" } });
  await page.context().addCookies([cookie]);
  await page.getByRole("button", { name: "People" }).first().click(); // the next call finds the session gone
  await expect(page.getByText("your console session has ended; sign in again")).toBeVisible();
  await expect(page.getByText("Couldn't load this")).toHaveCount(0);
  allow(problems, /status of 401/);
  await signInWithKey(page, KEYS.admin);
  await expect(page.getByText("People", { exact: true }).first()).toBeVisible();
  expect(page.url()).toContain("#people");
});

test("two-factor: turn it on, sign in with a code, and with a recovery code", async ({ page, browser }) => {
  await signInAs(page, "admin");
  const inv = await (await page.request.post("/v1/admin/invites", { data: { email: "tf@example.com" }, headers: { "x-meritorder-console": "1" } })).json();
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  await p.goto(`/ui/#invite=${inv.token}`);
  await p.getByLabel(/^Password/).fill("a long enough passphrase");
  await p.getByLabel("Again").fill("a long enough passphrase");
  await p.getByRole("button", { name: "Create account and sign in" }).click();
  await p.getByRole("button", { name: "tf@example.com" }).click();
  await p.getByRole("menuitem", { name: "Two-factor sign-in" }).click();
  await p.getByRole("button", { name: "Set up" }).click();
  await expect(p.getByRole("link", { name: "Add to my authenticator app" })).toHaveAttribute("href", /^otpauth:\/\/totp\//);
  await expect(p.getByRole("img", { name: /QR code to add Merit Order/ })).toBeVisible();
  const secret = (await p.getByRole("dialog").locator("code").innerText()).replace(/\s/g, "");
  await p.getByLabel("Then enter the code it shows").fill(totp(secret));
  await p.getByRole("button", { name: "Turn on" }).click();
  const codes = (await p.getByRole("dialog").locator("pre").innerText()).trim().split("\n");
  expect(codes).toHaveLength(10);
  await p.getByRole("button", { name: "Done" }).click();
  await p.getByRole("button", { name: "tf@example.com" }).click();
  await p.getByRole("menuitem", { name: "Sign out" }).click();
  // Password first, then the code it asks for.
  await p.getByLabel("Email address").fill("tf@example.com");
  await p.locator("input[autocomplete=current-password]").fill("a long enough passphrase");
  await p.getByRole("button", { name: "Sign in", exact: true }).click();
  const code = p.getByLabel("Code from your authenticator app");
  await expect(code).toBeVisible();
  await code.fill(codes[0]);  // a recovery code works too
  await p.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(p.getByRole("button", { name: "tf@example.com" })).toBeVisible();
  await ctx.close();
});

test("a console that kept a key before sessions trades it for one and forgets it", async ({ page }) => {
  await page.addInitScript((key) => {
    if (!sessionStorage.getItem("seeded")) { localStorage.setItem("meritorder.key", key); sessionStorage.setItem("seeded", "1"); }
  }, KEYS.admin);
  await page.goto("/ui/");
  await expect(page.getByRole("button", { name: "root" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("meritorder.key"))).toBeNull();
});

test("an invite link: set a password, sign out, sign in with it", async ({ page, browser }) => {
  await signInAs(page, "admin");
  await open(page, "people");
  const invite = page.locator("form").filter({ hasText: "Make invite link" });
  await invite.getByLabel("Email address").fill("newhire@example.com");
  await invite.getByRole("button", { name: "Make invite link" }).click();
  const dialog = page.getByRole("dialog", { name: /Invite link for newhire@example.com/ });
  const link = (await dialog.locator("code").innerText()).trim();
  expect(link).toMatch(/\/ui\/#invite=/);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("cell", { name: "newhire@example.com" }).first()).toBeVisible();

  const other = await (await browser.newContext()).newPage(); // the new person, in their own browser
  await other.goto(link);
  await expect(other.getByText("newhire@example.com")).toBeVisible();
  await other.getByLabel("Your name").fill("New Hire");
  await other.getByLabel(/^Password/).fill("a long enough passphrase");
  await other.getByLabel("Again").fill("a long enough passphrase");
  await other.getByRole("button", { name: "Create account and sign in" }).click();
  await expect(other.getByRole("button", { name: "New Hire" })).toBeVisible();
  expect(other.url()).not.toContain("invite="); // the used link leaves the address bar
  await other.getByRole("button", { name: "New Hire" }).click();
  await other.getByRole("menuitem", { name: "Sign out" }).click();
  await other.getByLabel("Email address").fill("newhire@example.com");
  await other.locator("input[autocomplete=current-password]").fill("a long enough passphrase");
  await other.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(other.getByRole("button", { name: "New Hire" })).toBeVisible();
  await other.context().close();
});

test.describe("as an admin", () => {
  test.beforeEach(async ({ page }) => signInAs(page, "admin"));

  for (const [hash, nav, heading] of ADMIN_PAGES) {
    test(`${nav} page renders`, async ({ page }) => {
      await open(page, hash);
      await expect(page.getByText(heading, { exact: true }).first()).toBeVisible();
      await expectLoaded(page);
    });
  }

  test("navigation reaches every page", async ({ page }) => {
    await open(page, "dispatch");
    const header = page.locator("header");
    for (const [hash, nav, heading] of ADMIN_PAGES.filter(([h]) => h !== "me")) {
      await header.getByRole("button", { name: nav, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`#${hash}$`));
      await expect(page.getByText(heading, { exact: true }).first()).toBeVisible();
    }
  });

  test("dashboards show the seeded traffic", async ({ page }) => {
    await open(page, "dispatch");
    await expect(page.getByText("Live dispatch")).toBeVisible();
    await expect(page.getByText("small", { exact: true }).filter({ visible: true }).first()).toBeVisible(); // a served model
    await open(page, "reports");
    await expectLoaded(page);
    await expect(page.getByText(/^\d+ requests$/).first()).toBeVisible();
    await expect(page.getByText("Spend by model")).toBeVisible();
    await page.getByRole("tab", { name: "Requests" }).click();
    await expectLoaded(page);
    await expect(page.getByText("alice", { exact: true }).first()).toBeVisible();
    // Why each request got its level: "what is 2+2?" is a short question on its own.
    await expect(page.getByText(/^classified general:1 \(short question/).first()).toBeVisible();
    await expect(page.getByText("Answered by Postgres").first()).toBeVisible(); // the newest rows always come from Postgres
  });

  test("reports say which store answered; Config shows the reporting backend and its health", async ({ page }) => {
    // The e2e config points at a ClickHouse that can't be reached: every report falls back to Postgres and says so.
    await open(page, "reports");
    await expectLoaded(page);
    await expect(page.getByText("Answered by Postgres").first()).toBeVisible();
    await page.getByRole("tab", { name: "Breakdown" }).click();
    await expectLoaded(page);
    await expect(page.getByText("Answered by Postgres").first()).toBeVisible();

    await open(page, "config");
    await expectLoaded(page);
    await page.getByRole("tab", { name: "Reporting" }).click();
    await expect(page.getByText("Reports come from Postgres and ClickHouse")).toBeVisible();
    await expect(page.getByText("ClickHouse can't be reached", { exact: true })).toBeVisible();
    await expect(page.getByText("Postgres answers every report: ClickHouse can't be reached")).toBeVisible();
    await expect(page.getByText("Last copy failed")).toBeVisible(); // the worker's cycle in the seed tried and saved why
    await expect(page.getByLabel("URL", { exact: true })).toHaveValue("http://127.0.0.1:9");
    await page.getByRole("button", { name: "About Reports that read it" }).hover();
    await expect(page.getByRole("tooltip")).toContainText("Budgets and the request list always read Postgres");
    await expect(page.getByText(/^Unsaved:/)).toHaveCount(0);
  });

  test("Requests tab filters and pages on the gateway, keeps them in the URL, and opens a request's details", async ({ page }) => {
    await open(page, "reports/requests?limit=25");
    await expectLoaded(page);
    const panel = page.locator(".panel").filter({ has: page.getByRole("button", { name: "Next" }) });
    await expect(panel.getByText(/^1–\d+ of \d+$/)).toBeVisible();
    await expect(panel.getByText("alice", { exact: true }).first()).toBeVisible();

    // A filter narrows the rows on the gateway, after a pause in typing, and lands in the URL.
    const asked = page.waitForRequest((r) => r.url().includes("/v1/reports/requests?") && r.url().includes("user=lee"));
    await page.getByLabel("User", { exact: true }).fill("lee");
    await asked;
    await expect(page).toHaveURL(/user=lee/);
    await expectLoaded(page);
    await expect(panel.getByText("lee", { exact: true }).first()).toBeVisible();
    await expect(panel.getByText("alice", { exact: true })).toHaveCount(0);
    await page.reload();
    await expect(page.getByLabel("User", { exact: true })).toHaveValue("lee");
    await expect(panel.getByText("lee", { exact: true }).first()).toBeVisible();

    // Paging: one row a page is too few to pick in the console, so ask the API what the console asks.
    const headers = { authorization: `Bearer ${KEYS.admin}` };
    const first = await (await page.request.get("/v1/reports/requests?limit=2", { headers })).json();
    expect(first.rows).toHaveLength(2);
    const second = await (await page.request.get(`/v1/reports/requests?limit=2&cursor=${first.next}`, { headers })).json();
    expect(second.rows.map((r: { id: string }) => r.id)).not.toContain(first.rows[0].id);
    expect(second.total).toBe(first.total);

    // The CSV is the filtered set.
    const download = page.waitForEvent("download");
    await panel.getByRole("button", { name: "Download CSV" }).click();
    expect((await download).suggestedFilename()).toMatch(/^meritorder-requests-.*\.csv$/);

    await panel.locator("tbody tr").first().click();
    const drawer = page.getByRole("dialog");
    await expect(drawer.getByText("Why it went there")).toBeVisible();
    await expect(drawer.getByText("Savings by cause")).toBeVisible();
    await expect(drawer.getByText("lee · eng")).toBeVisible();
    await expect(drawer.getByText(/prompts and responses are never stored/)).toBeVisible();
  });

  test("Bills tab shows each bill source and the ledger it's checked against", async ({ page }) => {
    await open(page, "reports");
    await page.getByRole("tab", { name: "Bills" }).click();
    await expectLoaded(page);
    await expect(page.getByText("mock-bill", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/MERITORDER_BACKEND_E2E_BILLING_UNSET is not set/).first()).toBeVisible(); // the worker couldn't fetch it
    await expect(page.getByText("big", { exact: true }).first()).toBeVisible(); // the seeded traffic, on the ledger side
  });

  test("Learned router tab shows the shadow comparison, its model and versions", async ({ page }) => {
    await open(page, "reports");
    await page.getByRole("tab", { name: "Learned router" }).click();
    await expectLoaded(page);
    // Shadow mode (e2e/meritorder.yaml) recorded the seeded requests; no model is trained yet, so the rules decided all of them.
    await expect(page.getByText("Shadow: compares, changes nothing")).toBeVisible();
    await expect(page.getByText("No model in use: the rules decide every level.")).toBeVisible();
    await expect(page.getByText(/^no model yet: \d+$/)).toBeVisible();
    await expect(page.getByText("No model trained yet.")).toBeVisible();
    await expect(page.getByText(/^No team routes by expected total cost yet\./)).toBeVisible();
    await expect(page.getByText("Answered by Postgres")).toBeVisible(); // request features are never copied to ClickHouse
    // Training runs in the worker, never in a gateway: the button asks for it.
    await page.getByRole("button", { name: "Train now" }).click();
    await expect(page.getByText("Training requested").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Training requested" })).toBeDisabled();
  });

  test("Batches tab shows each batch, where its requests went and what it saved", async ({ page }) => {
    await open(page, "reports");
    await page.getByRole("tab", { name: "Batches" }).click();
    await expectLoaded(page);
    await expect(page.getByText("Anthropic Message Batches").first()).toBeVisible();
    await expect(page.getByText("OpenAI /v1/chat/completions").first()).toBeVisible();
    await expect(page.getByText("completed", { exact: true }).first()).toBeVisible(); // the seed's worker pass settled them
    await expect(page.getByText(/^mock-anthropic \d+$/).first()).toBeVisible(); // the provider split
    await expect(page.getByText("Batch discount", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("On your GPUs", { exact: true })).toBeVisible();
    // Admins see where batch content is held and the key's state.
    await expect(page.getByText("Held for batches")).toBeVisible();
    await expect(page.getByText("Postgres", { exact: true })).toBeVisible();
  });

  test("spend counts GPU time, idle included, and says what was APIs and what GPUs", async ({ page }) => {
    // The seed accrued an hour of gpu-static ($4.00) that served none of its requests (seed_gpu.py).
    await open(page, "dispatch");
    await expect(page.locator(".kpi").filter({ hasText: "Spend this month" }).getByText(/^APIs \$[\d.,]+ · GPUs \$4\.00$/)).toBeVisible();
    await open(page, "reports");
    await expectLoaded(page);
    const spent = page.locator(".kpi").filter({ hasText: "Spent" }).first();
    await expect(spent.getByText(/^APIs \$[\d.,]+ · GPUs \$4\.00$/)).toBeVisible();
    await expect(page.getByText("GPU idle, unallocated").first()).toBeVisible();  // Top teams: the organization's, no team's
    await page.getByRole("tab", { name: "Breakdown" }).click();
    await expectLoaded(page);
    await expect(page.getByRole("columnheader", { name: "GPUs" })).toBeVisible();
    await expect(page.getByRole("row", { name: /GPU idle, unallocated/ })).toBeVisible();
    await open(page, "controls");
    await expectLoaded(page);
    await expect(page.locator(".panel").filter({ hasText: "Organization spend this month" }).getByText(/· GPUs \$4\.00$/)).toBeVisible();
  });

  test("GPUs page explains a deployment that isn't running", async ({ page }) => {
    await open(page, "gpus");
    await expect(page.getByText("gpu-static").first()).toBeVisible();
    await expectLoaded(page);
    // Sessions go back to the replica that holds their KV cache once it runs.
    await expect(page.getByText("Cache affinity").first()).toBeVisible();
    await expect(page.getByText("Per session, once running").first()).toBeVisible();
  });

  test("Models page says how each model's token counts are calibrated", async ({ page }) => {
    await open(page, "models");
    await expectLoaded(page);
    // The seeded traffic ran on mock backends, which never calibrate anything: the plain estimate is in use.
    await expect(page.getByText("uncalibrated").first()).toBeVisible();
  });

  test("GPUs page says where a Kubernetes deployment runs", async ({ page }) => {
    await open(page, "gpus");
    await expectLoaded(page);
    await expect(page.getByText("coder-oss · kubernetes, KServe")).toBeVisible();
    await expect(page.getByText(/Runs H100:4 on your cluster through KServe, namespace gpus, on p5\.48xlarge nodes/)).toBeVisible();
  });

  test("dark and light mode", async ({ page }) => {
    await open(page, "dispatch");
    const html = page.locator("html");
    const before = await html.getAttribute("data-mantine-color-scheme");
    const toggle = page.getByRole("button", { name: /Switch to (light|dark) mode/ });
    await toggle.click();
    await expect(html).not.toHaveAttribute("data-mantine-color-scheme", before ?? "");
    await page.reload();
    await expect(html).not.toHaveAttribute("data-mantine-color-scheme", before ?? ""); // remembered
    await page.getByRole("button", { name: /Switch to (light|dark) mode/ }).click();
    await expect(html).toHaveAttribute("data-mantine-color-scheme", before ?? "");
  });
});
