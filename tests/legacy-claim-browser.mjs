import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.argv[3] || "playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const views = path.join(root, "app", "views");

async function fixture(context, { claimStatus = 200, delayOldLegacy = false, delayClaimState = false,
  delayGreeting = false, delayClaimPost = false, newAccountClaim = false } = {}) {
  const tokens = new Map();
  const claimed = new Set();
  const counts = { legacyGets: 0, oldLegacyResponses: 0, claimPosts: 0, pending: 0 };
  let sequence = 0;
  let releaseOldLegacy;
  let oldLegacyStarted;
  let releaseClaimState;
  let claimStateStarted;
  let releaseGreeting;
  let greetingStarted;
  let oldClaimStarted;
  let newClaimStarted;
  let releaseOldClaim;
  let releaseNewClaim;
  const oldLegacyReady = new Promise(resolve => { oldLegacyStarted = resolve; });
  const delayedOldLegacy = new Promise(resolve => { releaseOldLegacy = resolve; });
  const claimStateReady = new Promise(resolve => { claimStateStarted = resolve; });
  const delayedClaimState = new Promise(resolve => { releaseClaimState = resolve; });
  const greetingReady = new Promise(resolve => { greetingStarted = resolve; });
  const delayedGreeting = new Promise(resolve => { releaseGreeting = resolve; });
  const oldClaimReady = new Promise(resolve => { oldClaimStarted = resolve; });
  const newClaimReady = new Promise(resolve => { newClaimStarted = resolve; });
  const delayedOldClaim = new Promise(resolve => { releaseOldClaim = resolve; });
  const delayedNewClaim = new Promise(resolve => { releaseNewClaim = resolve; });
  let delayNextClaimState = false;

  const respond = (route, data, status = 200) => route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(data),
  });

  await context.route("**/api/**", async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const headers = request.headers();
    const token = headers.authorization?.replace(/^Bearer /, "");
    const username = token && tokens.get(token);

    if (pathname === "/api/healthz") {
      return respond(route, { ok: true, auth_required: true, password_login: true,
        account_setup_required: false, legacy_data_available: true });
    }
    if (pathname === "/api/auth/login") {
      const body = JSON.parse(request.postData() || "{}");
      assert.equal(headers.authorization, undefined);
      assert.ok(["旧账号", "新账号"].includes(body.username));
      assert.equal(body.password, "a-good-password");
      const issued = `token-${body.username === "旧账号" ? "old" : "new"}-${++sequence}`;
      tokens.set(issued, body.username);
      return respond(route, { access_token: issued, username: body.username,
        token_type: "bearer", expires_in: 604800 });
    }
    if (pathname === "/api/auth/me") return respond(route, { username });
    if (pathname === "/api/auth/logout") {
      tokens.delete(token);
      return respond(route, { ok: true });
    }
    assert.ok(username, `missing authenticated fixture user for ${pathname}`);

    if (pathname === "/api/state") {
      if (delayNextClaimState) {
        delayNextClaimState = false;
        claimStateStarted();
        await delayedClaimState;
      }
      const entries = claimed.has(username) ? [{ text: "旧世界书已接回", source: "conversation" }] : [];
      return respond(route, { name: "沈知夏", model: "preview-model", chat_provider: "fixture",
        bg_model: "preview-background", personality: "温柔、坦率。", personality_text: "邻家的她。",
        profile: "还没有记录。", profile_text: "还没有记录。", memory: entries.length ? "长期记忆 1 条" : "长期记忆 0 条",
        memory_issues: [], world: entries, world_count: entries.length });
    }
    if (pathname === "/api/greeting") {
      greetingStarted();
      if (delayGreeting) await delayedGreeting;
      return respond(route, { greeting: `${username} 的开场白` });
    }
    if (pathname === "/api/auth/legacy" && request.method() === "GET") {
      counts.legacyGets++;
      if (username === "旧账号" && delayOldLegacy) {
        oldLegacyStarted();
        await delayedOldLegacy;
      }
      if (username === "旧账号") counts.oldLegacyResponses++;
      return respond(route, { available: (username === "旧账号" || newAccountClaim) && !claimed.has(username),
        memories: 1, messages: 2, world: 1 });
    }
    if (pathname === "/api/auth/legacy" && request.method() === "POST") {
      counts.claimPosts++;
      if (delayClaimPost) {
        if (username === "旧账号") { oldClaimStarted(); await delayedOldClaim; }
        else { newClaimStarted(); await delayedNewClaim; }
      }
      if (claimStatus !== 200) return respond(route, { detail: "旧记忆已经被其他账号认领。" }, claimStatus);
      claimed.add(username);
      if (delayClaimState) delayNextClaimState = true;
      return respond(route, { claimed: true });
    }
    if (pathname === "/api/pending") {
      counts.pending++;
      const text = username === "旧账号"
        ? (claimed.has(username) ? "认领后的主动消息" : "认领前的主动消息")
        : "新账号的主动消息";
      const id = username === "旧账号" ? "shared-pending" : "new-pending";
      return respond(route, { items: [{ id, text }] });
    }
    if (pathname === "/api/pending/ack") return respond(route, { acked: 1 });
    return respond(route, { detail: `unhandled fixture route: ${pathname}` }, 404);
  });

  return { counts, oldLegacyReady, releaseOldLegacy, claimStateReady, releaseClaimState,
    greetingReady, releaseGreeting, oldClaimReady, newClaimReady, releaseOldClaim, releaseNewClaim };
}

async function serveStatic() {
  const server = createServer(async (request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://fixture").pathname);
    const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, "");
    const file = path.resolve(views, relative);
    if (!file.startsWith(views + path.sep)) { response.writeHead(403); response.end(); return; }
    try {
      const body = await readFile(file);
      const type = file.endsWith(".html") ? "text/html; charset=utf-8" :
        file.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/plain";
      response.writeHead(200, { "Content-Type": type });
      response.end(body);
    } catch {
      response.writeHead(404);
      response.end("not found");
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}/` };
}

async function login(page, username = "旧账号") {
  await page.locator("#username-input").fill(username);
  await page.locator("#password-input").fill("a-good-password");
  await page.locator("#login-submit").click();
  await page.locator("#app-shell").waitFor();
}

async function waitForClaimDialog(page) {
  await page.locator("#legacy-claim-dialog[open]").waitFor();
  assert.match(await page.locator("#legacy-claim-message").textContent(), /旧世界书|旧记忆/);
}

async function closeEntry(page) {
  await page.locator("#switch-identity").click();
  await page.locator("#entry-screen").waitFor();
}

async function acceleratePending(page) {
  await page.addInitScript(() => {
    const nativeTimeout = window.setTimeout.bind(window);
    const nativeInterval = window.setInterval.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeTimeout(callback, delay === 5000 ? 50 : delay, ...args);
    window.setInterval = (callback, delay, ...args) =>
      nativeInterval(callback, delay === 30000 ? 100 : delay, ...args);
  });
}

async function run() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const staticServer = await serveStatic();
  try {
    {
      const context = await browser.newContext();
      const control = await fixture(context, { delayClaimPost: true, newAccountClaim: true });
      const page = await context.newPage();
      await page.addInitScript(() => {
        const nativeFetch = window.fetch.bind(window);
        window.fetch = (input, options = {}) => {
          if (String(input) === "/api/auth/legacy" && options.method === "POST") {
            const detached = { ...options };
            delete detached.signal;
            return nativeFetch(input, detached);
          }
          return nativeFetch(input, options);
        };
      });
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await waitForClaimDialog(page);
      await page.locator("#legacy-claim-confirm").click();
      await control.oldClaimReady;
      await page.locator("#legacy-claim-dismiss").click();
      await closeEntry(page);
      await login(page, "新账号");
      await waitForClaimDialog(page);
      assert.equal(await page.locator("#legacy-claim-confirm").isEnabled(), true,
        "the new account claim must be usable after leaving an old pending claim");
      await page.locator("#legacy-claim-confirm").click();
      await control.newClaimReady;
      control.releaseOldClaim();
      await page.waitForTimeout(200);
      assert.equal(await page.locator("#legacy-claim-confirm").isDisabled(), true,
        "a stale claim completion must not unlock the new account claim");
      assert.match(await page.locator("#log").textContent(), /新账号\s*的开场白/);
      assert.equal(await page.locator("#legacy-claim-dialog").getAttribute("open"), "");
      control.releaseNewClaim();
      await page.locator("#legacy-claim-dialog").waitFor({ state: "hidden" });
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context, { delayGreeting: true });
      const page = await context.newPage();
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await control.greetingReady;
      await page.waitForTimeout(100);
      assert.equal(control.counts.legacyGets, 0, "legacy checks must wait until the greeting request completes");
      assert.equal(await page.locator("#legacy-claim-dialog").getAttribute("open"), null);
      control.releaseGreeting();
      await waitForClaimDialog(page);
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context);
      const page = await context.newPage();
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await waitForClaimDialog(page);
      await page.locator("#legacy-claim-dismiss").click();
      assert.equal(control.counts.claimPosts, 0, "dismiss must not claim data");
      await closeEntry(page);
      await login(page);
      await waitForClaimDialog(page);
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context);
      const page = await context.newPage();
      await acceleratePending(page);
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await waitForClaimDialog(page);
      await page.locator("#log").getByText(/旧账号\s*的开场白/).waitFor();
      await page.locator("#log").getByText("认领前的主动消息").waitFor();
      await page.evaluate(() => {
        document.getElementById("legacy-claim-confirm").click();
        document.getElementById("legacy-claim-confirm").click();
      });
      await page.locator("#world-summary").filter({ hasText: "1 条设定" }).waitFor({ state: "attached" });
      assert.equal(control.counts.claimPosts, 1, "double click must send one claim request");
      assert.equal(await page.locator("#world-list").textContent(), "旧世界书已接回来自对话");
      await page.waitForTimeout(300);
      assert.doesNotMatch(await page.locator("#log").textContent(), /旧账号\s*的开场白|认领前的主动消息/,
        "claiming must isolate the prior empty-account conversation log");
      await page.locator("#log").getByText("认领后的主动消息").waitFor();
      await closeEntry(page);
      await login(page, "新账号");
      assert.doesNotMatch(await page.locator("#log").textContent(), /旧账号\s*的开场白|认领前的主动消息|认领后的主动消息/);
      await page.waitForTimeout(300);
      await page.locator("#log").getByText("新账号的主动消息").waitFor();
      assert.doesNotMatch(await page.locator("#log").textContent(), /认领前的主动消息|认领后的主动消息/);
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context, { claimStatus: 409 });
      const page = await context.newPage();
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await waitForClaimDialog(page);
      await page.locator("#legacy-claim-confirm").click();
      await page.locator("#legacy-claim-error:not([hidden])").waitFor();
      assert.match(await page.locator("#legacy-claim-error").textContent(), /其他账号认领/);
      assert.equal(control.counts.claimPosts, 1);
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context, { delayOldLegacy: true });
      const page = await context.newPage();
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await control.oldLegacyReady;
      await closeEntry(page);
      await login(page, "新账号");
      await page.waitForTimeout(300);
      assert.equal(await page.locator("#legacy-claim-dialog").getAttribute("open"), null,
        "a late old-account legacy response must not open the new account dialog");
      control.releaseOldLegacy();
      await page.waitForTimeout(300);
      assert.equal(control.counts.oldLegacyResponses, 1, "the stale legacy response must reach the browser fixture");
      assert.equal(await page.locator("#legacy-claim-dialog").getAttribute("open"), null,
        "releasing the stale response must remain isolated");
      await context.close();
    }

    {
      const context = await browser.newContext();
      const control = await fixture(context, { delayClaimState: true });
      const page = await context.newPage();
      await page.goto(staticServer.url);
      await page.locator("#login-submit:not(:disabled)").waitFor();
      await login(page);
      await waitForClaimDialog(page);
      await page.locator("#legacy-claim-confirm").click();
      await control.claimStateReady;
      await closeEntry(page);
      control.releaseClaimState();
      await login(page, "新账号");
      await page.waitForTimeout(300);
      assert.equal(await page.locator("#toast").getAttribute("hidden"), "",
        "a stale claim refresh must not show success in the new account");
      await context.close();
    }
    console.log("legacy claim browser checks passed");
  } finally {
    await browser.close();
    await new Promise(resolve => staticServer.server.close(resolve));
  }
}

run().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
