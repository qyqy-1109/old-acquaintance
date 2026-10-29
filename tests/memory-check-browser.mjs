import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const views = path.resolve(fileURLToPath(new URL("../app/views/", import.meta.url)));
const browser = await chromium.launch({ channel: "msedge", headless: true });
const failures = [];
const scenarios = ["repeat-navigation", "leave-and-return", "shared-request", "timeout-retry", "failure-retry", "identity-change", "startup-error", "state-timeout", "incomplete-retry"];

try {
  for (const scenario of scenarios) {
    const context = await browser.newContext();
    const held = [];
    const pageErrors = [];
    let commandCount = 0;
    let mode = scenario === "failure-retry" ? "error" : scenario === "incomplete-retry" ? "incomplete" : "hold";
    let stateCount = 0;
    await context.addInitScript(() => {
      localStorage.setItem("tts", "off");
      const nativeTimeout = window.setTimeout.bind(window);
      window.setTimeout = (callback, delay, ...arguments_) =>
        nativeTimeout(callback, delay === 45000 ? 250 : delay, ...arguments_);
    });
    await context.route("**/*", async route => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      const respond = (body, status = 200) => route.fulfill({ status,
        contentType: "application/json", body: JSON.stringify(body) });
      if (pathname.startsWith("/api/")) {
        if (pathname === "/api/healthz") return respond({ ok: true, auth_required: true,
          password_login: true, account_setup_required: false, legacy_data_available: false });
        if (pathname === "/api/auth/login") return respond({ username: "隔离体检账号",
          access_token: "fixture-token", token_type: "bearer", expires_in: 604800 });
        if (pathname === "/api/auth/me") return respond({ username: "隔离体检账号" });
        if (pathname === "/api/auth/legacy") return respond({ available: false });
        if (pathname === "/api/auth/logout") return respond({ ok: true });
        if (pathname === "/api/state") {
          stateCount++;
          if (scenario === "state-timeout" && stateCount > 1) return;
          return respond({ name: "隔离角色", world: [], memory_issues: [],
            memory_check_error: scenario === "startup-error" ? "记忆体检未完成，请稍后重试。" : "" });
        }
        if (pathname === "/api/greeting") return respond({ greeting: "" });
        if (pathname === "/api/pending") return respond({ items: [] });
        if (pathname === "/api/command") {
          assert.equal(JSON.parse(request.postData()).command, "/mem check");
          commandCount++;
          if (mode === "hold") { held.push(route); return; }
          if (mode === "error") return respond({ detail: "记忆体检未完成，请稍后重试。" }, 504);
          if (mode === "incomplete") return respond({ output: "记忆体检未完成，请稍后重试。",
            memory_check_error: "记忆体检未完成，请稍后重试。", memory_issues: [] });
          return respond({ output: "本次隔离体检已完成。", memory_check_error: "", memory_issues: [] });
        }
        throw new Error("Unexpected fixture endpoint " + pathname);
      }
      const filename = path.resolve(views, pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, ""));
      assert.ok(filename.startsWith(views + path.sep), "only serve repository frontend assets");
      const contentType = filename.endsWith(".js") ? "text/javascript"
        : filename.endsWith(".css") ? "text/css" : filename.endsWith(".html") ? "text/html" : "image/svg+xml";
      return route.fulfill({ contentType, body: await readFile(filename) });
    });
    const page = await context.newPage();
    page.on("pageerror", error => pageErrors.push(error.message));
    const login = async () => {
      await page.fill("#username-input", "隔离体检账号");
      await page.fill("#password-input", "fixture-password");
      await page.click("#login-submit");
      await page.waitForSelector("#message-input:not([disabled])");
    };
    const navigateCare = () => page.click('[data-view="care"]');
    const completed = () => page.waitForFunction(() =>
      document.querySelector("#care-output").textContent === "本次隔离体检已完成。", null, { timeout: 1800 });
    const release = async (text, extra = {}) => {
      const route = held.shift();
      assert.ok(route, "a command must actually be waiting");
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ output: text, ...extra }) });
    };
    try {
      await page.goto("http://memory-check.test/");
      await login();
      await navigateCare();
      if (scenario === "repeat-navigation") {
        await navigateCare();
        await release("本次隔离体检已完成。", { memory_issues: [], memory_check_error: "" });
        await completed();
        assert.equal(commandCount, 1, "same navigation must reuse the pending check");
      } else if (scenario === "leave-and-return") {
        await page.click('[data-view="chat"]');
        const response = page.waitForResponse("**/api/command");
        await release("本次隔离体检已完成。");
        await response;
        await navigateCare();
        await completed();
        assert.equal(commandCount, 1, "navigation must not lose a completed response");
      } else if (scenario === "shared-request") {
        assert.equal(await page.locator("#memory-check").isDisabled(), true,
          "automatic checks must mark the same button busy");
        await page.locator("#memory-check").dispatchEvent("click");
        await release("本次隔离体检已完成。", { memory_issues: [], memory_check_error: "" });
        await completed();
        assert.equal(commandCount, 1, "manual and automatic entry must share one request");
      } else if (scenario === "timeout-retry") {
        await page.waitForFunction(() => document.querySelector("#care-output").textContent.includes("超时"),
          null, { timeout: 1800 });
        assert.equal(await page.locator("#memory-check").isDisabled(), false, "timeout restores the check button");
        mode = "success";
        await page.click("#memory-check");
        await completed();
        assert.equal(commandCount, 2, "a timeout can be retried");
      } else if (scenario === "failure-retry") {
        await page.waitForFunction(() => document.querySelector("#care-output").textContent.includes("未完成"));
        mode = "success";
        await page.click("#memory-check");
        await completed();
      } else if (scenario === "identity-change") {
        await page.click("#switch-identity");
        mode = "success";
        await login();
        await navigateCare();
        await completed();
        await release("旧账号的检查结果。");
        assert.equal(await page.locator("#care-output").textContent(), "本次隔离体检已完成。",
          "the old identity cannot overwrite the new identity");
      } else if (scenario === "startup-error") {
        assert.ok((await page.locator("#care-summary").textContent()).includes("未完成"),
          "a failed startup check cannot be displayed as no pending issues");
        assert.equal(await page.locator("#issue-badge").isHidden(), false,
          "a failed startup check must remain visible in the issue badge");
        await release("本次隔离体检已完成。", { memory_issues: [], memory_check_error: "" });
        await completed();
        assert.equal(await page.locator("#issue-badge").isHidden(), true,
          "a successful retry clears the startup error badge");
        assert.ok(!(await page.locator("#care-summary").textContent()).includes("未完成"));
      } else if (scenario === "state-timeout") {
        await release("本次隔离体检已完成。");
        await page.waitForFunction(() => !document.querySelector("#memory-check").disabled,
          null, { timeout: 1800 });
        assert.equal(await page.locator("#care-output").textContent(), "本次隔离体检已完成。",
          "a state refresh timeout cannot overwrite a completed check");
      } else if (scenario === "incomplete-retry") {
        await page.waitForFunction(() => document.querySelector("#care-output").textContent.includes("未完成"));
        mode = "success";
        await page.click('[data-view="chat"]');
        await navigateCare();
        await completed();
        assert.equal(commandCount, 2, "an incomplete HTTP 200 report remains retryable on reentry");
      }
      await page.waitForFunction(() => !document.querySelector("#memory-check").disabled);
      assert.ok((await page.locator("#memory-check").textContent()).includes("检查"),
        "the restored button label must allow another check");
      assert.deepEqual(pageErrors, [], "no unhandled browser errors");
      console.log("PASS " + scenario);
    } catch (error) {
      failures.push(scenario + ": " + error.message);
      console.error("FAIL " + scenario + ": " + error.message);
    } finally {
      await context.close();
    }
  }
} finally {
  await browser.close();
}

assert.deepEqual(failures, []);
console.log(`memory check browser checks passed (${scenarios.length} cases)`);
