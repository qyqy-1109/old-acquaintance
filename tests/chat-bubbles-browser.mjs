/**
 * `T90` 浏览器级验收：**流式回复真的会分成两个气泡**，且标记不外泄、头像昵称不重复。
 * 顺带把 `#145` 的看板口径（「她最近报备的」打头、「此刻」标成自动推演）在真浏览器里过一遍。
 *
 * 为什么需要它：`T86`／`#114` 的分气泡、`#123` 的"跨分片切分"一直是**人工看**的结论；
 * `T90` 在池子里挂着的就是"缺一个浏览器级、可重复的验收"。这里用 playwright（已装）
 * 起一个**假后端**（只拦 `/api/**`）＋ 真前端静态文件，把两个场景跑成断言。
 *
 * 跑法：node tests/chat-bubbles-browser.mjs
 *      （可选第二个参数＝ playwright 的 require 路径，与 legacy-claim-browser.mjs 一致）
 * 浏览器：用**系统 Edge**（`channel: "msedge"`）——playwright 只装了包、没下浏览器二进制，
 *        这条和 `legacy-claim-browser.mjs` 是同一个做法，换机器时记得确认有 Edge。
 * 纪律：**不连真后端、不连真模型**；只读 `app/views`，不写任何文件。
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.argv[2] || "playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const views = path.join(root, "app", "views");

// 她这一轮要说的话：两段，中间用分条标记隔开（与真机日志里的形状一致）
const BUBBLE_A = "在收球衣，有件掉在地上了";
const BUBBLE_B = "刚下楼捡回来";
const streamFrames = [
  { type: "delta", text: BUBBLE_A },
  { type: "delta", text: "\n@@@\n" },
  { type: "delta", text: BUBBLE_B },
  { type: "done" },
].map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("");

// 她主动说的话（`/api/pending`）：场景 3 会把它填成**内联标记**的形状——真机库里就是这样存的
let pendingItems = [];
const PROACTIVE_TEXT = "翻出来发现第二页也缺@@@整章例题得重过一遍，明天交作业别再漏了";

const board = {
  now: "2026-10-07 09:20",
  state: { her_name: "沈知夏", place: "楼下", activity: "在石桌边看大爷下棋", mood: "挺乐",
           on_mind: "高数笔记还剩半页", slot: "forenoon", slot_label: "上午",
           slot_date: "2026-10-07", updated_when: "今天 09:15",
           advisory: true, advisory_label: "自动推演 · 只当氛围；跟她说过的话对不上时，以「她最近报备的」为准" },
  script: { duty: "大二在读", tastes: "喜欢打球", cares: "团子" },
  favorite_places: ["学校球场"], cares: ["团子"],
  events: [{ ts: 1, when: "今天 09:15", rid: 1, slot: "forenoon", slot_label: "上午",
             text: "抄笔记抄到一半笔没水了", source: "model", source_label: "她自己选的" }],
  reports: [{ ts: 2, when: "今天 09:18", text: "糖吃完了，甜得齁", status: "已读到", her_name: "沈知夏" }],
  experiences: [], stats: { events_total: 1, said_today: 1, daily_max: 6, next_slot_when: "今天 12:00" },
};

async function serveStatic() {
  const server = createServer(async (request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://fixture").pathname);
    const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, "");
    const file = path.resolve(views, relative);
    if (!file.startsWith(views + path.sep)) { response.writeHead(403); response.end(); return; }
    try {
      const body = await readFile(file);
      const type = file.endsWith(".html") ? "text/html; charset=utf-8"
        : file.endsWith(".js") ? "text/javascript; charset=utf-8"
        : file.endsWith(".css") ? "text/css; charset=utf-8" : "text/plain";
      response.writeHead(200, { "Content-Type": type });
      response.end(body);
    } catch {
      response.writeHead(404); response.end();
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return { server, port: server.address().port };
}

async function main() {
  const { server, port } = await serveStatic();
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext();
  const seen = { chatPosts: 0, streams: 0 };

  await context.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (data, status = 200) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(data) });
    switch (url.pathname) {
      case "/api/healthz":
        return json({ ok: true, auth_required: true, password_login: true,
                      account_setup_required: false, legacy_data_available: false });
      case "/api/auth/login": {
        const body = JSON.parse(request.postData() || "{}");
        assert.equal(body.username, "验收账号");
        return json({ access_token: "fixture-token", username: body.username,
                      token_type: "bearer", expires_in: 604800 });
      }
      case "/api/auth/me": return json({ username: "验收账号" });
      case "/api/state":
        return json({ name: "沈知夏", model: "m", chat_provider: "fixture", bg_model: "b",
                      personality: "坦率", personality_text: "邻家的她", profile: "还没记录",
                      profile_text: "还没记录", memory: "长期记忆 0 条", memory_issues: [],
                      world: [], world_count: 0 });
      case "/api/greeting": return json({ greeting: "" });          // 空开场白 → 前端不挂气泡
      case "/api/pending": return json({ items: pendingItems });
      case "/api/pending/ack": return json({ acked: 0 });
      case "/api/life": return json(board);
      case "/api/relationship":
        return json({ stage: { key: "刚认识", call_him: "你" }, decide: "she",
                      promises: [], history: [], days: 2 });
      case "/api/chat/stream": {
        seen.chatPosts++; seen.streams++;
        return route.fulfill({ status: 200, contentType: "text/event-stream",
                               headers: { "Cache-Control": "no-store" }, body: streamFrames });
      }
      default:
        return json({ detail: `unhandled fixture route: ${url.pathname}` }, 404);
    }
  });

  const page = await context.newPage();
  const failures = [];
  try {
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "domcontentloaded" });
    await page.fill("#username-input", "验收账号");
    await page.fill("#password-input", "a-good-password");
    await page.click("#login-submit");
    await page.waitForSelector("#app-shell:not([hidden])", { timeout: 15000 });

    // ---- 场景 1：流式回复分成两个气泡 ----
    await page.fill("#message-input", "在干嘛呢");
    await page.click("#send");
    await page.waitForFunction(
      () => document.querySelectorAll("article.message.her").length >= 2, null, { timeout: 15000 });

    const her = await page.$$eval("article.message.her", nodes => nodes.map(node => ({
      text: node.querySelector(".message-bubble")?.textContent ?? "",
      continuation: node.classList.contains("continuation"),
      avatars: node.querySelectorAll("img.message-avatar").length,
    })));
    const texts = her.map(row => row.text);
    assert.deepEqual(texts, [BUBBLE_A, BUBBLE_B], `两条气泡的正文与顺序：${JSON.stringify(texts)}`);
    assert.equal(her[1].continuation, true, "第二条必须是 continuation 气泡（不重挂头像）");
    // ---- 场景 3：**她主动说的话**（`/api/pending`）也必须分气泡、不许泄漏 @@@ ----
    // 真机形状：库里那句是 `第一段@@@第二段`（**内联**，没有换行）——
    // 而 `bubble.js` 的规则 1 是"标记必须独占一行"，`pollPending` 更是**完全没拆分**。
    // 这条断言要的是用户看到的最终结果：两条气泡、正文里没有 `@@@`。
    pendingItems = [{ id: 9001, text: PROACTIVE_TEXT, her_name: "沈知夏" }];
    await page.waitForFunction(
      () => [...document.querySelectorAll("article.message.her .message-bubble")]
        .some(node => (node.textContent || "").includes("整章例题")),
      null, { timeout: 40000 });          // 前端 30 秒轮询一次，给它一轮的时间
    const proactive = await page.$$eval("article.message.her", nodes => nodes.map(node => ({
      text: node.querySelector(".message-bubble")?.textContent ?? "",
      proactive: node.dataset.proactive === "1",
    })));
    const proTexts = proactive.filter(row => row.proactive).map(row => row.text);
    assert.equal(proTexts.some(text => text.includes("@@@")), false,
      `主动消息不许把内部标记泄漏给用户（当前渲染成：${JSON.stringify(proTexts)}）`);
    assert.deepEqual(proTexts, ["翻出来发现第二页也缺", "整章例题得重过一遍，明天交作业别再漏了"],
      `主动消息要拆成两条气泡（当前：${JSON.stringify(proTexts)}）`);

    assert.equal(her.reduce((sum, row) => sum + row.avatars, 0), 1,
      `她这一轮只该有一个头像：${JSON.stringify(her)}`);

    const logText = await page.$eval("#log", node => node.textContent);
    assert.ok(!logText.includes("@@@"), "分条标记绝不能出现在页面上");
    assert.ok(!logText.includes("data:"), "SSE 原始帧绝不能出现在页面上");
    assert.equal(seen.streams, 1, `应当只发了一次流式请求：${JSON.stringify(seen)}`);

    // ---- 场景 2：看板口径（`#145`）----
    await page.click('[data-view="life"]');
    await page.waitForSelector("#life-said .row, #life-said *", { timeout: 10000 });
    const boardText = await page.$eval("#view-life", node => node.textContent);
    assert.ok(boardText.includes("糖吃完了，甜得齁"),
      "「她最近报备的」要把她亲口说的话显示出来");
    assert.ok(boardText.includes("自动推演") && boardText.includes("只当氛围"),
      "「此刻」必须标明是推演、只当氛围");
    const order = await page.evaluate(() => {
      const html = document.querySelector("#view-life").innerHTML;
      return { said: html.indexOf('id="life-said"'), state: html.indexOf('id="life-state"') };
    });
    assert.ok(order.said >= 0 && order.state >= 0 && order.said < order.state,
      `报备必须排在「此刻」前面：${JSON.stringify(order)}`);
  } catch (error) {
    failures.push(error);
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }

  if (failures.length) {
    for (const error of failures) console.error(error?.stack || error);
    throw failures[0];
  }
  console.log("chat bubbles browser checks passed");
}

main().catch(error => { console.error(error?.stack || error); process.exit(1); });
