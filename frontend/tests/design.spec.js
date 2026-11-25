import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const workspace = "/dev/design";
const auth = "/dev/design/auth";
const axeTags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

test("shared layout serves compiled CSS, fonts and artwork on the current origin", async ({ page, baseURL }) => {
  for (const path of [workspace, auth]) {
    const host = new URL(baseURL).host;
    const failed = [];
    const observe = (response) => {
      if ((!response.ok() && response.status() !== 304) || new URL(response.url()).host !== host) failed.push(response.url());
    };
    page.on("response", observe);
    const response = await page.goto(path);
    expect(response.headers()["cache-control"]).toBe("no-store");
    await expect(page.locator(".skip-link")).toHaveAttribute("href", "#main-content");
    await expect(page.locator("main h1:visible")).toHaveCount(1);
    await expect(page.locator('link[href="/static/css/clubos.css"]')).toHaveCount(1);
    const fontFamilies = await page.evaluate(async () => {
      await document.fonts.ready;
      return [...document.fonts].filter(({ status }) => status === "loaded").map(({ family }) => family);
    });
    expect(fontFamilies.join(" ")).toContain("Quicksand");
    expect(fontFamilies.join(" ")).toContain("Nunito Sans");
    expect(failed).toEqual([]);
    page.off("response", observe);
  }
  await expect(page.locator(".auth-art img")).toHaveJSProperty("complete", true);
  await expect(page.locator(".auth-art img")).toHaveAttribute("alt", "");
  await page.goto(workspace);
  await expect(page.locator(".auth-art")).toHaveCount(0);
});

test("sign-in explains invalid input, keeps entered values and submits no credentials", async ({ page }) => {
  await page.goto(auth);
  const posts = [];
  page.on("request", (request) => {
    if (request.method() === "POST") posts.push(request.url());
  });
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const username = page.getByLabel("Username");
  const password = page.getByLabel("Password");
  await expect(username).toBeFocused();
  await expect(username).toHaveAccessibleDescription("Enter your username.");
  await expect(password).toHaveAccessibleDescription("Enter your password.");
  await username.fill("fixture.user");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(password).toBeFocused();
  await expect(username).toHaveValue("fixture.user");
  await password.fill("synthetic");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#login-feedback")).toHaveText("Your username or password is incorrect. Please check your details and try again.");
  expect(posts).toEqual([]);
  await page.getByRole("link", { name: "View registration layout" }).click();
  await expect(page.getByRole("heading", { name: "Join your school" })).toBeFocused();
  await page.getByRole("button", { name: "Preview registration" }).click();
  await expect(page.getByLabel("School code")).toBeFocused();
  await page.getByRole("link", { name: "Back to sign in" }).click();
  await expect(username).toHaveValue("fixture.user");
});

test("keyboard navigation skips content and restores focus from a drawer and a dialog", async ({ page, browserName }) => {
  const tab = browserName === "webkit" && process.platform === "darwin" ? "Alt+Tab" : "Tab";
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(workspace);
  await page.keyboard.press(tab);
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  const menu = page.getByRole("button", { name: "Menu" });
  await menu.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer.getByRole("button", { name: "Close navigation" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toBeFocused();
  await page.keyboard.press("Enter");
  await drawer.getByRole("link", { name: "Form controls" }).click();
  await expect(page.getByRole("heading", { name: "Form controls" })).toBeFocused();

  const open = page.getByRole("button", { name: "Preview confirmation" });
  await open.focus();
  await page.keyboard.press("Enter");
  const remove = page.getByRole("dialog", { name: "Remove draft?" });
  await expect(remove.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press(tab);
  await expect(remove.getByRole("button", { name: "Remove draft" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
});

test("review preserves explanations, guards repeated clicks and checks an uncertain result", async ({ page }) => {
  await page.goto(workspace);
  const decision = page.getByLabel("Decision");
  const comments = page.getByLabel("Explanation");
  const open = page.getByRole("button", { name: "Review decision" });
  await decision.selectOption("CHANGES_REQUESTED");
  await open.click();
  await expect(comments).toBeFocused();
  await expect(comments).toHaveAccessibleDescription(/Explain what the student needs to change/);
  await decision.selectOption("REJECTED");
  await open.click();
  await expect(comments).toHaveAccessibleDescription(/Explain why the activity is being rejected/);
  const explanation = "<img src=x onerror=alert(1)> Please include the count.";
  await comments.fill(explanation);
  await page.getByLabel("Simulate a lost connection").check();
  await open.click();
  const dialog = page.getByRole("dialog", { name: "Confirm activity review" });
  await expect(dialog.getByText(explanation, { exact: true })).toBeVisible();
  await expect(dialog.locator("img")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Back to decision" }).click();
  await expect(open).toBeFocused();
  await expect(comments).toHaveValue(explanation);
  await open.click();
  const confirm = dialog.getByRole("button", { name: "Reject activity" });
  await confirm.evaluate((button) => { button.click(); button.click(); });
  await expect(dialog.getByText("Submitting your review…")).toBeVisible();
  await expect(confirm).toHaveAttribute("aria-busy", "true");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/couldn’t confirm the result/)).toBeVisible();
  await dialog.getByRole("button", { name: "Check result" }).click();
  await expect(dialog).toBeHidden();
  await expect(open).toBeFocused();
  await expect(page.locator("#review-result")).toContainText("Sample review saved");
  await expect(page.locator("#review-steps [aria-current]")).toHaveText("Result");
});

test("impersonation distinguishes accounts, validates reason and uses the same expiry after reload", async ({ page }) => {
  await page.goto(workspace);
  const search = page.getByLabel("Search by name");
  await search.fill("Alex");
  const options = page.locator(".account-option");
  await expect(options).toHaveCount(2);
  await expect(options.first()).toContainText("alex.m.teacher");
  await expect(options.last()).toContainText("alex.m.student");
  await page.getByRole("navigation", { name: "Eligible account pages" }).getByRole("button", { name: "Next" }).click();
  await expect(options).toHaveCount(1);
  await expect(options.first()).toContainText("Alex Rivera");
  await page.getByRole("navigation", { name: "Eligible account pages" }).getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: /alex.m.student/ }).click();
  const reason = page.getByLabel("Reason");
  await expect(reason).toBeFocused();
  await page.getByRole("button", { name: "Review impersonation" }).click();
  await expect(reason).toHaveAccessibleDescription(/Explain why you need to impersonate/);
  await reason.fill("Review a student view for a sample support request.");
  await page.getByRole("button", { name: "Review impersonation" }).click();
  const dialog = page.getByRole("dialog", { name: "Confirm impersonation" });
  await expect(dialog.getByText("15 minutes")).toBeVisible();
  await expect(dialog.getByText(/attributed to you/)).toBeVisible();
  await expect(dialog.getByText(/Read-only student access/)).toBeVisible();
  await dialog.getByRole("button", { name: "Start impersonation" }).click();
  const banner = page.getByRole("region", { name: "Impersonation status" });
  const end = banner.getByRole("button", { name: "End impersonation" });
  await expect(end).toBeFocused();
  await expect(search).toBeHidden();
  const saved = await page.evaluate(() => sessionStorage.getItem("clubos_design_impersonation"));
  const state = JSON.parse(saved);
  expect(Date.parse(state.expires_at) - Date.parse(state.started_at)).toBe(15 * 60 * 1000);
  await page.reload();
  await expect(banner).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("clubos_design_impersonation"))).toBe(saved);
  await end.click();
  await expect(banner).toBeHidden();
  await expect(search).toBeFocused();
});

test("impersonation ends when the authoritative expiry has passed", async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem("clubos_design_impersonation", JSON.stringify({
      represented_name: "Alex Morgan", represented_role: "Teacher", expires_at: new Date(Date.now() - 1000).toISOString(),
    }));
  });
  await page.goto(workspace);
  await expect(page.locator("#impersonation-result")).toHaveText("Impersonation has expired.");
  await expect(page.getByRole("region", { name: "Impersonation status" })).toBeHidden();
});

test("account search debounces, cancels stale results and recovers from failure", async ({ page }) => {
  let releaseOld;
  const oldResponse = new Promise((resolve) => { releaseOld = resolve; });
  let firstFailure = true;
  await page.route("**/dev/design/accounts?*", async (route) => {
    const query = new URL(route.request().url()).searchParams.get("q");
    if (query === "Al") {
      await oldResponse;
      await route.fulfill({ json: { items: [{ name: "Stale result", username: "stale", role: "Student" }], next_cursor: null } }).catch(() => {});
    } else if (query === "Casey" && firstFailure) {
      firstFailure = false;
      await route.fulfill({ status: 503 });
    } else {
      await route.continue();
    }
  });
  await page.goto(workspace);
  const search = page.getByLabel("Search by name");
  const firstRequest = page.waitForRequest((request) => request.url().includes("accounts?q=Al"));
  await search.pressSequentially("Al");
  expect(new URL((await firstRequest).url()).searchParams.get("q")).toBe("Al");
  await expect(page.locator("#account-search-results [data-skeleton]")).toBeVisible();
  await search.fill("Casey");
  await expect(page.getByText(/Couldn’t load eligible accounts/)).toBeVisible();
  await page.getByRole("button", { name: "Retry search" }).click();
  await expect(page.getByRole("button", { name: /casey.s.student/ })).toBeVisible();
  releaseOld();
  await search.fill("No matching account");
  await expect(page.getByText(/No eligible accounts match that name/)).toBeVisible();
  await expect(page.getByText("Stale result")).toHaveCount(0);
});

test("table pagination and retry loading expose clear states", async ({ page }) => {
  await page.goto(workspace);
  const table = page.getByRole("table", { name: "Recent activity submissions" });
  await expect(table.getByRole("row")).toHaveCount(4);
  const pager = page.getByRole("navigation", { name: "Activity table pages" });
  await pager.getByRole("button", { name: "Next" }).click();
  await expect(pager.getByRole("status")).toHaveText("Page 2");
  await expect(table.getByText("Spring chess meet")).toBeVisible();
  await expect(table.getByText("Campus bird count")).toBeHidden();
  await expect(page.getByText("Couldn’t load the club list")).toBeVisible();
  await page.getByRole("button", { name: "Retry loading clubs" }).click();
  await expect(page.locator("#retry-preview [data-skeleton]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Club list loaded" })).toBeVisible();
});

for (const width of [320, 390, 768, 1024, 1440]) {
  test(`both layouts reflow at ${width}px without page overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    for (const path of [workspace, auth]) {
      await page.goto(path);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      const main = page.getByRole("main");
      await expect(main).toBeVisible();
      if (path === workspace) {
        const menu = page.getByRole("button", { name: "Menu" });
        await (width < 1024 ? expect(menu).toBeVisible() : expect(menu).toBeHidden());
        const widthOfButton = await page.getByRole("button", { name: "Review decision" }).boundingBox();
        expect(widthOfButton.height).toBeGreaterThanOrEqual(44);
        await page.getByRole("button", { name: "Preview confirmation" }).click();
        const dialog = page.getByRole("dialog", { name: "Remove draft?" });
        await expect(dialog.getByRole("button", { name: "Remove draft" })).toBeInViewport();
      }
    }
  });
}

test("axe WCAG 2.2 AA checks cover layouts and an open confirmation dialog", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const scan = async () => (await new AxeBuilder({ page }).withTags(axeTags).analyze()).violations;
  await page.goto(workspace);
  expect(await scan()).toEqual([]);
  await page.getByRole("button", { name: "Preview confirmation" }).click();
  expect(await scan()).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(auth);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  expect(await scan()).toEqual([]);
});

test("200% text enlargement keeps main controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 800 });
  for (const path of [workspace, auth]) {
    await page.goto(path);
    await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
    if (path === workspace) {
      await page.getByRole("button", { name: "Preview confirmation" }).click();
      const dialog = page.getByRole("dialog", { name: "Remove draft?" });
      await expect(dialog.getByRole("button", { name: "Remove draft" })).toBeInViewport();
    } else {
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await expect(page.getByLabel("Username")).toBeFocused();
      await expect(page.getByText("Enter your username.")).toBeVisible();
    }
  }
});

test("authenticated transport declines cross-origin requests before sending a tab key", async ({ page }) => {
  await page.goto(workspace);
  const requests = [];
  page.on("request", (request) => {
    if (new URL(request.url()).origin !== new URL(page.url()).origin) requests.push(request.url());
  });
  const outcome = await page.evaluate(async () => {
    const { authFetch } = await import("/static/js/session.js");
    sessionStorage.setItem("clubos_tab_key", "test-fixture");
    try {
      await authFetch("https://example.invalid");
      return "request allowed";
    } catch (error) {
      return error.message;
    } finally {
      sessionStorage.removeItem("clubos_tab_key");
    }
  });
  expect(outcome).toBe("ClubOS requests must stay on the current origin.");
  expect(requests).toEqual([]);
});
