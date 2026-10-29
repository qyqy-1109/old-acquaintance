import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.argv[3] || "playwright");
const baseURL = process.argv[2] || "http://127.0.0.1:8766";
const output = path.resolve("_tmptest/frontend-screenshots");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "msedge", headless: true });
const report = { viewports: [], checks: [], errors: [] };

async function fixture(context, { setupRequired = false, oldBackend = false } = {}) {
  let entries = [{ text: "周末一起去散步。", source: "manual" }, { text: "喜欢安静的雨天。", source: "conversation" }];
  let expired = false;
  let initialized = !setupRequired;
  let stateFailure = false;
  let sessions = 0;
  const tokens = new Set(["url-token"]);
  await context.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const headers = request.headers();
    const respond = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    if (pathname === "/api/healthz") return respond({ ok: true, auth_required: initialized,
      password_login: oldBackend ? undefined : true, account_setup_required: !initialized, legacy_data_available: true });
    if (pathname === "/api/auth/login" || pathname === "/api/auth/setup") {
      assert.equal(headers.authorization, undefined);
      const data = JSON.parse(request.postData());
      if (data.username !== "小林" || data.password !== "a-good-password") return respond({ detail: "账号或密码不正确。" }, 401);
      if (pathname.endsWith("setup")) {
        if (initialized) return respond({ detail: "already initialized" }, 409);
        assert.equal(data.inherit_default, true);
        initialized = true;
      }
      const token = "session-token-" + ++sessions;
      tokens.add(token);
      return respond({ access_token: token, username: "小林", token_type: "bearer", expires_in: 604800 }, pathname.endsWith("setup") ? 201 : 200);
    }
    assert.equal(headers["x-user-id"], undefined);
    const token = headers.authorization?.replace(/^Bearer /, "");
    if (!tokens.has(token)) return respond({ detail: "登录状态已失效，请重新登录。" }, 401);
    if (expired) { expired = false; tokens.delete(token); return respond({ detail: "expired" }, 401); }
    if (pathname === "/api/auth/me") return respond({ username: "小林" });
    if (pathname === "/api/auth/logout") { tokens.delete(token); return respond({ ok: true }); }
    if (pathname === "/api/state" && stateFailure) {
      stateFailure = false;
      return respond({ detail: "state temporarily unavailable" }, 503);
    }
    if (pathname === "/api/state") return respond({ name: "沈知夏", model: "preview-model", chat_provider: "local-preview", bg_model: "preview-background",
      personality: "温柔、坦率，认真听你说话。", personality_text: "像熟悉的邻居，亲切但不越界。",
      profile: "你喜欢散步，也喜欢雨天。", profile_text: "喜欢安静的雨天\n周末有散步的习惯",
      memory: "长期记忆 8 条", memory_issues: [], world: entries, world_count: entries.length });
    if (pathname === "/api/greeting") return respond({ greeting: "来啦。今天过得怎么样？\n慢慢说，我在听。" });
    if (pathname === "/api/chat/stream") {
      assert.equal(JSON.parse(request.postData()).message, "你好，小妹");
      const events = [{ type: "meta", recalled: [{ text: "喜欢雨天", sim: .92 }] },
        { type: "delta", text: "你好呀，\n@" }, { type: "delta", text: "@@\n今天也很高兴见到你。" },
        { type: "done", memory_note: "记住了这次聊天", memory_async: false },
        { type: "audio", wav_b64: "YXVkaW8=" }, { type: "audio_end" }];
      return route.fulfill({ contentType: "text/event-stream", body: events.map(data => `data: ${JSON.stringify(data)}\r\n\r\n`).join("") });
    }
    if (pathname === "/api/world" && request.method() === "POST") {
      entries.push({ text: JSON.parse(request.postData()).text, source: "manual" });
      return respond({ output: "已添加设定" });
    }
    if (pathname.startsWith("/api/world/") && request.method() === "DELETE") {
      entries.splice(Number(pathname.split("/").at(-1)), 1);
      return respond({ output: "已删除设定" });
    }
    if (pathname === "/api/feedback") return respond({ output: "反馈已记录" });
    if (pathname === "/api/command") {
      const command = JSON.parse(request.postData()).command;
      return respond({ output: command === "/timeline" ? "2026-09-28\n约好了周末散步。" : command === "/slots" ? "目前没有待处理的约定。" : "检查完成，各层状态一致。" });
    }
    return respond({ detail: "unhandled fixture route" }, 404);
  });
  return { expire: () => { expired = true; }, failState: () => { stateFailure = true; },
    initialize: () => { initialized = true; } };
}

async function checkLayout(page, viewport, screen) {
  await page.setViewportSize(viewport);
  if (viewport.width < 768 && screen !== "login" && screen !== "navigation") {
    await page.waitForFunction(() => getComputedStyle(document.getElementById("sidebar")).visibility === "hidden");
  }
  await page.screenshot({ path: path.join(output, `${screen}-${viewport.width}.png`), fullPage: true, animations: "disabled" });
  const layout = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    brokenImages: [...document.images].filter(image => image.getClientRects().length && (!image.complete || !image.naturalWidth)).length }));
  assert.ok(layout.documentWidth <= layout.width, `${screen} overflows at ${viewport.width}`);
  assert.equal(layout.brokenImages, 0);
  report.viewports.push({ screen, ...viewport, horizontalOverflow: false, brokenImages: 0 });
}

try {
  const context = await browser.newContext({ viewport: { width: 1350, height: 900 } });
  const hooks = await fixture(context);
  const page = await context.newPage();
  page.on("pageerror", error => report.errors.push(error.message));
  await page.goto(baseURL);
  await page.locator("#login-submit:not(:disabled)").waitFor();
  for (const viewport of [{ width: 1350, height: 900 }, { width: 820, height: 1080 }, { width: 375, height: 812 }, { width: 320, height: 640 }]) await checkLayout(page, viewport, "login");
  await page.setViewportSize({ width: 1350, height: 900 });
  await page.locator("#username-input").fill("小林");
  await page.locator("#password-input").fill("a-good-password");
  await page.locator("#toggle-password").click();
  assert.equal(await page.locator("#password-input").getAttribute("type"), "text");
  await page.locator("#toggle-password").click();
  await page.locator("#login-submit").click();
  await page.locator("#message-input:not(:disabled)").waitFor();
  assert.equal(await page.evaluate(() => localStorage.getItem("auth_token")), "session-token-1");
  assert.equal(await page.evaluate(() => localStorage.getItem("auth_username")), "小林");
  assert.equal(await page.evaluate(() => localStorage.getItem("password")), null);
  assert.equal(await page.locator("#current-user").textContent(), "小林");
  assert.equal(await page.locator("#password-input").inputValue(), "");
  await page.locator("#tts-toggle").click();
  assert.equal(await page.evaluate(() => localStorage.getItem("tts")), "off");
  await page.locator("#message-input").fill("你好，小妹");
  await page.locator("#send").click();
  await page.locator("#log .message.her:not(.continuation) .message-bubble", { hasText: "你好呀，" }).waitFor();
  await page.locator("#log .message.her.continuation .message-bubble", { hasText: "今天也很高兴见到你。" }).waitFor();
  assert.equal(await page.locator("#log .message.her.continuation .message-avatar").count(), 0);
  assert.equal(await page.locator("#log .message.her.continuation .message-meta").count(), 0);
  assert.ok(!(await page.locator("#log").textContent()).includes("@@@"));
  report.checks.push("stream renders separate assistant bubbles without repeated identity or marker");
  await page.locator("#feedback-open:not(:disabled)").waitFor();
  assert.equal(await page.locator(".recall summary").textContent(), "想起了 1 条");
  for (const viewport of [{ width: 1350, height: 900 }, { width: 820, height: 1080 }, { width: 375, height: 812 }, { width: 320, height: 640 }]) await checkLayout(page, viewport, "chat");
  await page.locator("#message-input").fill("尚未发送的草稿");
  const conversationBeforeHome = await page.locator("#log").textContent();
  const tokenBeforeHome = await page.evaluate(() => localStorage.getItem("auth_token"));
  await page.setViewportSize({ width: 375, height: 812 });
  await page.locator("#nav-open").click();
  assert.equal(await page.locator("#sidebar").getAttribute("aria-modal"), "true");
  await page.locator('[data-view="world"]').click();
  assert.equal(await page.locator("#nav-open").getAttribute("aria-expanded"), "false");
  await page.locator("#world-input").fill("每周一起读一本书。");
  await page.locator("#world-add").click();
  await page.getByText("每周一起读一本书。", { exact: true }).waitFor();
  assert.equal(await page.locator("#world-list .world-row").count(), 3);
  await page.locator('#world-list button[aria-label="删除第 3 条设定"]').click();
  await page.waitForFunction(() => document.querySelectorAll("#world-list .world-row").length === 2);
  for (const viewport of [{ width: 1350, height: 900 }, { width: 375, height: 812 }]) await checkLayout(page, viewport, "world");
  await page.setViewportSize({ width: 1350, height: 900 });
  for (const view of ["slots", "timeline", "profile", "care"]) {
    await page.locator(`[data-view="${view}"]`).click();
    await page.locator(`#view-${view}`).waitFor();
    await checkLayout(page, { width: 375, height: 812 }, view);
    await page.setViewportSize({ width: 1350, height: 900 });
  }
  await page.locator("#sidebar .brand-mark").click();
  assert.ok(await page.locator("#chat-view").isVisible(), "clicking the sidebar brand should return to chat");
  assert.equal(await page.locator("#view-title").textContent(), "对话");
  assert.equal(await page.locator('[data-view="chat"]').getAttribute("aria-current"), "page");
  assert.ok(await page.locator("#message-input").evaluate(input => input === document.activeElement));
  report.checks.push("desktop brand returns to chat and focuses composer");

  const brandHome = page.getByRole("button", { name: "旧识，返回对话", exact: true });
  for (const key of ["Enter", "Space"]) {
    await page.locator('[data-view="profile"]').click();
    await brandHome.focus();
    await brandHome.press(key);
    assert.ok(await page.locator("#chat-view").isVisible(), `${key} should activate the brand button`);
    assert.ok(await page.locator("#message-input").evaluate(input => input === document.activeElement));
  }
  report.checks.push("brand button supports Enter and Space");

  await page.locator('[data-view="timeline"]').click();
  await page.setViewportSize({ width: 375, height: 812 });
  await page.locator("#nav-open").click();
  await checkLayout(page, { width: 375, height: 812 }, "navigation");
  await brandHome.click();
  assert.ok(await page.locator("#chat-view").isVisible());
  assert.equal(await page.locator("#nav-open").getAttribute("aria-expanded"), "false");
  assert.equal(await page.locator("#sidebar").getAttribute("aria-modal"), null);
  assert.ok(await page.locator("#message-input").evaluate(input => input === document.activeElement));
  assert.equal(await page.locator("#message-input").inputValue(), "尚未发送的草稿");
  assert.equal(await page.locator("#log").textContent(), conversationBeforeHome);
  assert.equal(await page.evaluate(() => localStorage.getItem("auth_token")), tokenBeforeHome);
  report.checks.push("mobile brand closes navigation and preserves conversation, draft and session");
  await page.setViewportSize({ width: 1350, height: 900 });
  await page.locator("#feedback-open").click();
  await page.locator("#feedback-note").fill("这句需要再确认。");
  await page.locator("#feedback-submit").click();
  await page.waitForFunction(() => !document.getElementById("feedback-dialog").open);
  await page.locator("#command-open").click();
  await page.getByRole("button", { name: "记忆事件 /mem events", exact: true }).click();
  assert.equal(await page.locator("#message-input").inputValue(), "/mem events");
  hooks.expire();
  await page.locator("#refresh-state").click();
  await page.locator("#entry-screen").waitFor();
  assert.equal(await page.evaluate(() => localStorage.getItem("auth_token")), null);
  assert.equal(await page.locator("#care-output").textContent(), "");
  report.checks.push("password login and automatic token persistence", "password visibility", "SSE text, recall and done", "mute persistence", "world add/delete", "all memory views", "mobile navigation", "feedback", "command menu", "401 recovery and old identity cleanup");
  await context.close();

  const tokenContext = await browser.newContext();
  await fixture(tokenContext);
  const tokenPage = await tokenContext.newPage();
  tokenPage.on("pageerror", error => report.errors.push(error.message));
  await tokenPage.goto(baseURL);
  await tokenPage.locator("#login-submit:not(:disabled)").waitFor();
  await tokenPage.locator("#username-input").fill("小林");
  await tokenPage.locator("#password-input").fill("wrong-password");
  await tokenPage.locator("#login-submit").click();
  await tokenPage.getByText("账号或密码不正确。", { exact: true }).waitFor();
  await tokenPage.locator("#password-input").fill("a-good-password");
  await tokenPage.locator("#login-submit").click();
  await tokenPage.locator("#app-shell").waitFor();
  assert.equal(await tokenPage.evaluate(() => localStorage.getItem("auth_token")), "session-token-1");
  await tokenPage.reload();
  await tokenPage.locator("#app-shell").waitFor();
  assert.equal(await tokenPage.locator("#current-user").textContent(), "小林");
  await tokenPage.locator("#switch-identity").click();
  await tokenPage.locator("#entry-screen").waitFor();
  assert.equal(await tokenPage.evaluate(() => localStorage.getItem("auth_token")), null);
  await tokenPage.goto(baseURL + "/?token=url-token&source=test");
  await tokenPage.locator("#app-shell").waitFor();
  assert.equal(new URL(tokenPage.url()).searchParams.has("token"), false);
  assert.equal(new URL(tokenPage.url()).searchParams.get("source"), "test");
  report.checks.push("wrong password feedback", "validated session restoration", "server logout and identity cleanup", "URL token migration");
  await tokenContext.close();

  const setupContext = await browser.newContext();
  const setupControl = await fixture(setupContext, { setupRequired: true });
  const setupPage = await setupContext.newPage();
  setupPage.on("pageerror", error => report.errors.push(error.message));
  await setupPage.goto(baseURL);
  await setupPage.locator("#password-confirm-wrap").waitFor();
  assert.equal(await setupPage.locator("#inherit-default").isChecked(), true);
  for (const viewport of [{ width: 1350, height: 900 }, { width: 820, height: 1080 }, { width: 375, height: 812 }, { width: 320, height: 640 }]) await checkLayout(setupPage, viewport, "setup");
  await setupPage.locator("#username-input").fill("小林");
  await setupPage.locator("#password-input").fill("a-good-password");
  await setupPage.locator("#password-confirm").fill("different-password");
  await setupPage.locator("#login-submit").click();
  await setupPage.getByText("两次密码不一致。", { exact: true }).waitFor();
  await setupPage.locator("#password-confirm").fill("a-good-password");
  setupControl.failState();
  await setupPage.locator("#login-submit").click();
  await setupPage.getByText("state temporarily unavailable", { exact: true }).waitFor();
  assert.equal(await setupPage.locator("#password-confirm-wrap").isHidden(), true);
  assert.equal(await setupPage.locator("#login-submit span").textContent(), "登录");
  await setupPage.locator("#password-input").fill("a-good-password");
  await setupPage.locator("#login-submit").click();
  await setupPage.locator("#app-shell").waitFor();
  assert.equal(await setupPage.locator("#current-user").textContent(), "小林");
  report.checks.push("first account setup and legacy data choice", "password confirmation", "setup recovery after state request failure");
  await setupContext.close();

  const staleSetupContext = await browser.newContext();
  const staleSetupControl = await fixture(staleSetupContext, { setupRequired: true });
  const staleSetupPage = await staleSetupContext.newPage();
  staleSetupPage.on("pageerror", error => report.errors.push(error.message));
  await staleSetupPage.goto(baseURL);
  await staleSetupPage.locator("#password-confirm-wrap").waitFor();
  staleSetupControl.initialize();
  await staleSetupPage.locator("#username-input").fill("小林");
  await staleSetupPage.locator("#password-input").fill("a-good-password");
  await staleSetupPage.locator("#password-confirm").fill("a-good-password");
  await staleSetupPage.locator("#login-submit").click();
  await staleSetupPage.getByText("already initialized", { exact: true }).waitFor();
  await staleSetupPage.locator("#password-confirm-wrap").waitFor({ state: "hidden", timeout: 3000 });
  await staleSetupPage.locator("#login-submit").click();
  await staleSetupPage.locator("#app-shell").waitFor();
  assert.equal(await staleSetupPage.locator("#current-user").textContent(), "小林");
  report.checks.push("setup conflict refreshes account state and allows login");
  await staleSetupContext.close();

  const oldContext = await browser.newContext();
  await fixture(oldContext, { oldBackend: true });
  const oldPage = await oldContext.newPage();
  await oldPage.goto(baseURL);
  await oldPage.getByText("后端需要加载账号密码登录，请重启服务后重新连接。", { exact: true }).waitFor();
  assert.equal(await oldPage.locator("#login-submit").isDisabled(), true);
  assert.equal(await oldPage.evaluate(() => localStorage.getItem("auth_token")), null);
  report.checks.push("old backend cannot bypass password authentication");
  await oldContext.close();
  assert.deepEqual(report.errors, []);
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.failure = error.stack;
  throw error;
} finally {
  await writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  await browser.close();
}
console.log(`browser checks passed (${report.viewports.length} viewport captures, ${report.checks.length} workflow checks)`);
