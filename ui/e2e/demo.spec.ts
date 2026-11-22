import { expect, test as base, type Page } from "@playwright/test";

// The static demo (playwright.demo.config.ts): every page and tab loads its sample data with no console error and no
// failed request, signed in as the demo's admin with no gateway anywhere.
const test = base.extend<{ problems: string[] }>({
  problems: [async ({ page }, use) => {
    const problems: string[] = [];
    page.on("pageerror", (e) => problems.push(`uncaught: ${e.message}`));
    page.on("console", (msg) => { if (msg.type() === "error") problems.push(`console: ${msg.text()}`); });
    page.on("requestfailed", (r) => problems.push(`failed: ${r.url()} (${r.failure()?.errorText})`));
    page.on("response", (r) => { if (r.status() >= 400) problems.push(`${r.status()}: ${r.url()}`); });
    await use(problems);
    expect(problems, "console errors, uncaught exceptions or failed requests").toEqual([]);
  }, { auto: true }],
});

const PAGES: [string, string][] = [
  ["dispatch", "Live dispatch"], ["reports", "Reports"], ["gpus", "GPUs"], ["controls", "Controls"], ["backends", "Backends"],
  ["models", "Models"], ["teams", "Teams & budgets"], ["people", "People"], ["keys", "API keys"], ["tools", "Tools"], ["config", "Configuration"],
  ["me", "My usage"],
];

async function loaded(page: Page) {
  await expect(page.getByText("Couldn't load this")).toHaveCount(0);
  await expect(page.getByText(/The demo has no sample answer/)).toHaveCount(0);
}

for (const [hash, heading] of PAGES) {
  test(`${hash}: the page and each of its tabs load`, async ({ page }) => {
    await page.goto(`./#${hash}`);
    await expect(page.getByRole("note", { name: "Demo" })).toBeVisible();
    await expect(page.getByText(heading, { exact: true }).first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    await loaded(page);
    const tabs = page.getByRole("tab");
    for (let i = 0; i < (await tabs.count()); i++) {
      await tabs.nth(i).click();
      await page.waitForTimeout(400);  // the demo answers in a fraction of a second, as a gateway would
      await loaded(page);
    }
  });
}

test("signed in as the demo's admin, with the banner's links", async ({ page }) => {
  await page.goto("./");
  await expect(page.getByRole("button", { name: "Alex Morgan" })).toBeVisible();
  const banner = page.getByRole("note", { name: "Demo" });
  await expect(banner.getByRole("link", { name: "meritorder.dev" })).toHaveAttribute("href", "https://meritorder.dev");
  await expect(banner.getByRole("link", { name: "GitHub" })).toHaveAttribute("href", "https://github.com/therick/meritorder");
  await expect(page.getByText(/Merit Order .*\(demo\) · AGPL-3\.0-only/)).toBeVisible();
  const notices = await page.request.get(await page.getByRole("link", { name: "Third-party notices" }).getAttribute("href") ?? "");
  expect(await notices.text()).toContain("Name: react");
});

test("spend says what was APIs and what GPUs, and the GPUs' idle time is the organization's", async ({ page }) => {
  await page.goto("./#dispatch");
  await expect(page.locator(".kpi").filter({ hasText: "Spend this month" }).getByText(/^APIs \$[\d.,]+ · GPUs \$[\d.,]+$/)).toBeVisible();
  await page.goto("./#reports/breakdown");
  await expect(page.getByRole("columnheader", { name: "GPUs" })).toBeVisible();
  await expect(page.getByRole("row", { name: /GPU idle, unallocated/ })).toBeVisible();
});

test("the live feed moves", async ({ page }) => {
  await page.goto("./#dispatch");
  await expect(page.getByText("Live dispatch").first()).toBeVisible();  // the demo answers the console's calls from here on
  const feed = async () => page.evaluate(async () => (await (await fetch("/v1/console/feed?limit=5")).json()).events[0].ts as string);
  const first = await feed();
  await expect.poll(feed, { timeout: 15_000 }).not.toBe(first);
});

test("Requests pages, filters, keeps both in the URL, and opens a request's details", async ({ page }) => {
  await page.goto("./#reports/requests?limit=25");
  const panel = page.locator(".panel").filter({ has: page.getByRole("button", { name: "Next" }) });
  await expect(panel.getByText(/^1–25 of \d+$/)).toBeVisible();
  const firstPage = await panel.locator("tbody tr").first().textContent();
  await panel.getByRole("button", { name: "Next" }).click();
  await expect(panel.getByText(/^26–50 of \d+$/)).toBeVisible();
  await expect(page).toHaveURL(/cursor=/);
  await expect(panel.locator("tbody tr").first()).not.toHaveText(firstPage ?? "");
  await panel.getByRole("button", { name: "Previous" }).click();
  await expect(panel.getByText(/^1–25 of \d+$/)).toBeVisible();

  await page.getByRole("combobox", { name: "Status" }).fill("success");
  await expect(page).toHaveURL(/status=success/);
  await expect(page).not.toHaveURL(/cursor=/);
  await expect(panel.locator("tbody tr").first()).toBeVisible();
  await page.reload();  // the filters survive a refresh
  await expect(page.getByRole("combobox", { name: "Status" })).toHaveValue("success");

  await panel.locator("tbody tr").first().click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByText("Why it went there")).toBeVisible();
  await expect(drawer.getByText("Savings by cause")).toBeVisible();
  const session = drawer.getByRole("row", { name: /^Session/ }).getByRole("button");
  if (await session.count()) {
    await session.click();
    await expect(page).toHaveURL(/session=/);
  }
});

test("a change works for the session and says it isn't saved", async ({ page }) => {
  await page.goto("./#keys");
  await page.getByLabel("User").fill("new.person@example.com");
  await page.getByRole("button", { name: "Issue key" }).click();
  await expect(page.getByText("Demo: not saved")).toBeVisible();
  await expect(page.getByText(/mo-demo-/).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "new.person@example.com" }).first()).toBeVisible();
});

test("a refresh stays on the page, and the host's 404 page sends a path to it", async ({ page, problems }) => {
  await page.goto("./#gpus");
  await page.reload();
  await expect(page.getByText("GPUs", { exact: true }).first()).toBeVisible();
  await expect(page).toHaveURL(/#gpus$/);
  // The host (CloudFront, GitHub Pages) answers /gpus with 404.html; opened there, it goes on to #gpus. vite preview
  // doesn't, so here it is played by hand; against a deployed copy (DEMO_URL) the host does it.
  if (!process.env.DEMO_URL) {
    await page.route("**/gpus", async (route) => route.fulfill({ status: 200, contentType: "text/html",
      body: await (await page.request.get("./404.html")).text() }));
  }
  await page.goto("./gpus");
  await expect(page).toHaveURL(/\/#gpus$/);
  await expect(page.getByText("GPUs", { exact: true }).first()).toBeVisible();
  // The host's answer for /gpus is a 404, as it should be, and Chrome logs it as an error: both are expected.
  for (const expected of [/^404: .*\/gpus$/, /^console: Failed to load resource: .* status of 404/]) {
    const i = problems.findIndex((p) => expected.test(p));
    if (i >= 0) problems.splice(i, 1);
  }
});
