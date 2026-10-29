import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const views = fileURLToPath(new URL("../app/views/", import.meta.url));
const server = createServer(async (request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, "http://fixture").pathname);
  const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, "");
  const filename = path.resolve(views, relative);
  if (!filename.startsWith(views)) {
    response.writeHead(403).end();
    return;
  }
  try {
    const body = await readFile(filename);
    const contentType = filename.endsWith(".js") ? "text/javascript"
      : filename.endsWith(".css") ? "text/css" : "text/html";
    response.writeHead(200, { "Content-Type": contentType }).end(body);
  } catch {
    response.writeHead(404).end();
  }
});

await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  try {
    browser = await chromium.launch({ channel: "msedge", headless: true });
  } catch {
    browser = await chromium.launch({ headless: true });
  }
  const context = await browser.newContext();
  await context.addInitScript(() => {
    localStorage.setItem("tts", "off");
    const nativeTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...arguments_) =>
      nativeTimeout(callback, delay === 2000 ? 150 : delay, ...arguments_);
  });
  let lifeCalls = 0;
  let leaveWhileRunning = false;
  await context.route("**/api/**", async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const respond = (body, status = 200) => route.fulfill({ status,
      contentType: "application/json", body: JSON.stringify(body) });
    if (pathname === "/api/healthz") return respond({ ok: true, auth_required: true,
      password_login: true, account_setup_required: false, legacy_data_available: false });
    if (pathname === "/api/auth/login") return respond({ username: "验收账号",
      access_token: "fixture-token", token_type: "bearer", expires_in: 604800 });
    if (pathname === "/api/auth/me") return respond({ username: "验收账号" });
    if (pathname === "/api/auth/legacy") return respond({ available: false });
    if (pathname === "/api/state") return respond({ name: "沈知夏", world: [], memory_issues: [] });
    if (pathname === "/api/greeting") return respond({ greeting: "" });
    if (pathname === "/api/pending") return respond({ items: [] });
    if (pathname === "/api/life") {
      lifeCalls += 1;
      if (lifeCalls === 1 || leaveWhileRunning) return respond({
        catchup: { running: true, remaining_slots: 64 },
        state: { her_name: "她", place: "家", activity: "整理房间", slot_date: "今天", slot_label: "上午" },
        script: {}, favorite_places: [], cares: [], reports: [], experiences: [],
        events: [{ when: "今天 09:00", slot_label: "上午", text: "补记中的一格", source_label: "她自己选的", rid: 1 }],
        stats: { events_total: 1, said_today: 0, daily_max: 3 },
      });
      return respond({
        catchup: { running: false, remaining_slots: 0 },
        state: { her_name: "她", place: "家", activity: "整理房间", slot_date: "今天", slot_label: "上午" },
        script: {}, favorite_places: [], cares: [], reports: [], experiences: [],
        events: [
          { when: "过去几天", slot_label: "分段摘要", text: "过去 60 格：她按自己的节奏生活", source_label: "补记摘要", rid: 2 },
          { when: "今天 09:00", slot_label: "最近一格", text: "她给薄荷浇了水", source_label: "她自己选的", rid: 3 },
        ],
        stats: { events_total: 2, said_today: 0, daily_max: 3 },
      });
    }
    return respond({ detail: "Unhandled fixture route " + pathname }, 404);
  });

  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.goto("http://127.0.0.1:" + server.address().port + "/");
  await page.fill("#username-input", "验收账号");
  await page.fill("#password-input", "a-good-password");
  await page.click("#login-submit");
  await page.waitForSelector("#message-input:not([disabled])");

  await page.click('[data-view="life"]');
  await page.waitForFunction(() => document.querySelector("#life-state")?.textContent.includes("正在补记"));
  assert.equal(lifeCalls, 1, "首次看板请求应显示补记中的中间状态");
  assert.equal(await page.locator("#life-events article").count(), 1);
  await page.waitForFunction(() => !document.querySelector("#life-state")?.textContent.includes("正在补记"));
  assert.equal(lifeCalls, 2, "补记完成后看板应自动刷新一次");
  assert.equal(await page.locator("#life-events article").count(), 2);
  const events = await page.$$eval("#life-events article p", nodes => nodes.map(node => node.textContent));
  assert.deepEqual(events, ["过去 60 格：她按自己的节奏生活", "她给薄荷浇了水"]);

  await page.click('[data-view="chat"]');
  leaveWhileRunning = true;
  await page.click('[data-view="life"]');
  await page.waitForFunction(() => document.querySelector("#life-state")?.textContent.includes("正在补记"));
  assert.equal(lifeCalls, 3, "再次进入生活页应请求当前状态");
  await page.click('[data-view="chat"]');
  const callsAfterSwitch = lifeCalls;
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(lifeCalls, callsAfterSwitch, "切页后不得继续轮询生活看板");
  assert.deepEqual(pageErrors, []);
  console.log("life catch-up browser checks passed: progress, auto-refresh, summary/detail, stop on tab switch");
  await context.close();
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
