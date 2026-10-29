import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const views = fileURLToPath(new URL("../app/views/", import.meta.url));
const first = "下午好呀，正抄笔记抄到头大";
const second = "你吃饭没";
const expected = [first, second];
const raw = first + "@@@" + second;
const memoryText = first + "\n" + second;
const ordinaryAt = "联系 a@example.com，@@两个人";
const server = createServer(async (request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, "http://fixture").pathname);
  const filename = path.resolve(views, pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, ""));
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
  browser = await chromium.launch({ channel: "msedge", headless: true });
  for (const scenario of ["greeting", "pending", "stream", "memory", "recall"]) {
    const context = await browser.newContext();
    const acknowledgements = [];
    let polls = 0;
    await context.addInitScript(() => {
      localStorage.setItem("tts", "off");
      const nativeInterval = window.setInterval.bind(window);
      const nativeTimeout = window.setTimeout.bind(window);
      window.setInterval = (callback, delay, ...arguments_) =>
        nativeInterval(callback, delay === 30000 ? 100 : delay, ...arguments_);
      window.setTimeout = (callback, delay, ...arguments_) =>
        nativeTimeout(callback, delay === 5000 ? 100 : delay, ...arguments_);
    });
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
      if (pathname === "/api/state") return respond(scenario === "memory" ? {
        name: "沈知夏", world: [{ text: raw, source: "manual" }], world_count: 1,
        profile: raw, profile_text: raw, personality: raw, personality_text: raw,
        memory: "长期记忆 1 条", memory_issues: [],
      } : { name: "沈知夏", world: [], memory_issues: [] });
      if (pathname === "/api/greeting") return respond({ greeting: scenario === "greeting" ? raw : "" });
      if (pathname === "/api/pending") {
        polls++;
        return respond({ items: scenario === "pending" ? [{ id: 9001, text: raw }] : [] });
      }
      if (pathname === "/api/pending/ack") {
        acknowledgements.push(JSON.parse(request.postData()).ids);
        return respond({ acked: 1 });
      }
      if (pathname === "/api/chat/stream") {
        const frames = [
          { type: "meta", recalled: scenario === "recall" ? [{ text: raw, sim: 0.9 }] : [] },
          { type: "delta", text: first + "@" },
          { type: "delta", text: "@" },
          { type: "delta", text: "@" + second },
          { type: "done" },
        ].map(event => "data: " + JSON.stringify(event) + "\n\n").join("");
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: frames });
      }
      if (pathname === "/api/command" && scenario === "memory") {
        const command = JSON.parse(request.postData()).command;
        return respond({ output: command === "/slots" ? ordinaryAt : raw });
      }
      if (pathname === "/api/life" && scenario === "memory") {
        return respond({
          state: { activity: raw }, script: { duty: raw }, favorite_places: [], cares: [],
          events: [{ when: "今天 09:15", slot_label: "上午", text: raw, source_label: "她自己选的", rid: 1 }],
          reports: [{ when: "今天 09:18", text: raw, status: "已读到" }],
          experiences: [{ when: "今天 09:19", text: raw }], stats: { events_total: 1 },
        });
      }
      if (pathname === "/api/relationship" && scenario === "memory") {
        return respond({ stage: { key: "刚认识", call_him: "你" },
          history: [{ why: raw }], promises: [{ id: "p1", text: raw, status: "open", reason: raw, note: raw }],
          events: [{ kind: "promise_created", ts: 1, promise: raw, text: raw }], days: 1 });
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
    if (scenario === "stream" || scenario === "recall") {
      await page.fill("#message-input", "在干嘛呢");
      await page.click("#send");
      await page.waitForFunction(() => document.querySelector("#composer").getAttribute("aria-busy") === "false");
    }
    if (scenario === "memory") {
      const readText = selector => page.$eval(selector, node => node.textContent);
      assert.equal(await readText("#world-list p"), memoryText, "世界书保留两段原文");
      for (const selector of ["#profile-summary", "#profile-text", "#personality-summary", "#personality-text"]) {
        assert.equal(await readText(selector), memoryText, selector + " 不显示内部标记");
      }
      await page.click('[data-view="timeline"]');
      await page.waitForFunction(text => document.querySelector("#timeline-output")?.textContent.includes(text), second);
      const output = await readText("#timeline-output");
      assert.equal(output, memoryText, "记忆页应保留顺序、把内部标记显示为分隔");
      assert.ok(!output.includes("@@@"), "记忆页不许泄漏内部标记");
      await page.click('[data-view="slots"]');
      await page.waitForFunction(text => document.querySelector("#slots-output")?.textContent === text, ordinaryAt);
      assert.equal(await readText("#slots-output"), ordinaryAt, "普通 @ 和 @@ 不能被删");
      await page.click('[data-view="life"]');
      await page.waitForSelector("#life-said article");
      for (const selector of ["#life-said p", "#life-events p", "#life-exp p"]) {
        assert.equal(await readText(selector), memoryText, selector + " 保留两段原文");
      }
      assert.ok(!(await readText("#view-life")).includes("@@@"), "生活记忆页不显示标记");
      await page.click("#life-said button");
      assert.equal(await page.inputValue("#life-pin-input"), memoryText, "钉牵挂的草稿也应是干净文字");
      await page.click('[data-close="life-pin-dialog"]');
      await page.click('[data-view="relationship"]');
      await page.waitForSelector("#relationship-promises article");
      assert.ok(!(await readText("#view-relationship")).includes("@@@"), "关系记忆页不显示标记");
      await page.locator("#relationship-promises button").filter({ hasText: "这次没做到" }).click();
      assert.equal(await readText("#relationship-miss-promise"), memoryText, "承诺详情对话框不显示标记");
      await page.click('[data-close="relationship-miss-dialog"]');
      await page.click('[data-view="care"]');
      await page.waitForFunction(text => document.querySelector("#care-output")?.textContent.includes(text), second);
      assert.equal(await readText("#care-output"), memoryText, "初次记忆体检不显示标记");
      const check = page.waitForResponse(response => new URL(response.url()).pathname === "/api/command");
      await page.click("#memory-check");
      await check;
      await page.waitForFunction(() => !document.querySelector("#memory-check").disabled);
      assert.equal(await readText("#care-output"), memoryText, "再次记忆体检不显示标记");
      await page.click('[data-view="chat"]');
      await page.fill("#message-input", "/mem");
      await page.click("#send");
      await page.waitForFunction(() => document.querySelector("#composer").getAttribute("aria-busy") === "false");
      assert.equal(await readText("#log .system-message"), memoryText, "查看记忆的命令回显不显示标记");
      assert.deepEqual(pageErrors, []);
      console.log("memory: panels, dialogs and command output hide markers; text order and ordinary @ remain");
      await context.close();
      continue;
    }
    await page.waitForFunction(text => [...document.querySelectorAll("article.message.her .message-bubble")]
      .some(node => node.textContent.includes(text)), second);
    const readMessages = () => page.$$eval("article.message.her", nodes => nodes.map(node => ({
      text: node.querySelector(".message-bubble").textContent,
      continuation: node.classList.contains("continuation"),
      avatars: node.querySelectorAll(".message-avatar").length,
      proactive: node.dataset.proactive === "1",
    })));
    const messages = await readMessages();
    assert.deepEqual(messages.map(message => message.text), expected, scenario + " must split the screenshot reply");
    assert.equal(messages[1].continuation, true);
    assert.equal(messages.reduce((sum, message) => sum + message.avatars, 0), 1);
    assert.ok(messages.every(message => !message.text.includes("@@@")));
    if (scenario === "recall") {
      await page.locator(".recall summary").click();
      assert.equal(await page.$eval(".recall-item span", node => node.textContent), memoryText,
        "召回的旧记忆不显示内部标记且保留顺序");
      console.log("recall: old memory hides marker and preserves order");
    }
    if (scenario === "pending") {
      assert.ok(messages.every(message => message.proactive));
      await page.waitForResponse(response => new URL(response.url()).pathname === "/api/pending/ack");
      assert.ok(polls >= 2);
      assert.ok(acknowledgements.length >= 2);
      assert.ok(acknowledgements.every(ids => ids.length === 1 && ids[0] === 9001));
      assert.deepEqual((await readMessages()).map(message => message.text), expected);
    }
    assert.deepEqual(pageErrors, []);
    if (scenario === "pending") await page.screenshot({ path: "_tmptest/inline-marker-fixed.png" });
    console.log(scenario + ": two bubbles, one avatar, no marker" + (scenario === "pending" ? ", retry without duplicates" : ""));
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
console.log("message marker browser checks passed");
