import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const views = fileURLToPath(new URL("../app/views/", import.meta.url));
const browser = await chromium.launch({ channel: "msedge", headless: true });
const failures = [];
const cases = [
  { width: 1350, height: 900 },
  { width: 1280, height: 598 },
  { width: 1024, height: 480 },
  { width: 375, height: 667 },
  { width: 320, height: 480 },
];

try {
  const page = await browser.newPage();
  await page.route("**/*", async route => {
    const pathname = new URL(route.request().url()).pathname;
    const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/static\//, "");
    const file = path.resolve(views, relative);
    assert.ok(file.startsWith(views), "the layout fixture only serves frontend files");
    let body = await readFile(file);
    if (relative === "index.html") {
      body = body.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
    }
    const contentType = file.endsWith(".html") ? "text/html; charset=utf-8"
      : file.endsWith(".css") ? "text/css; charset=utf-8"
      : file.endsWith(".svg") ? "image/svg+xml" : "image/png";
    await route.fulfill({ contentType, body });
  });
  await page.goto("http://layout.test/");
  await page.evaluate(() => {
    document.getElementById("entry-screen").hidden = true;
    document.getElementById("app-shell").hidden = false;
    document.getElementById("current-user").textContent = "布局验收账号";
    document.getElementById("identity-kind").textContent = "已登录";
    const log = document.getElementById("log");
    for (let index = 0; index < 30; index++) {
      const message = document.createElement("article");
      message.className = "message her";
      const content = document.createElement("div");
      content.className = "message-content";
      const bubble = document.createElement("div");
      bubble.className = "message-bubble";
      bubble.textContent = "这是一条用来填满聊天记录的隔离消息。";
      content.append(bubble);
      message.append(content);
      log.append(message);
    }
  });

  for (const viewport of cases) {
    await page.setViewportSize(viewport);
    for (const expanded of [false, true]) {
      await page.locator("#message-input").evaluate((input, grow) => {
        input.style.height = grow ? "160px" : "";
      }, expanded);
      const dimensions = await page.evaluate(() => {
        const box = selector => {
          const bounds = document.querySelector(selector).getBoundingClientRect();
          return { top: bounds.top, bottom: bounds.bottom, height: bounds.height };
        };
        const log = document.getElementById("log");
        return {
          viewport: innerHeight,
          shell: box("#app-shell"),
          content: box("#chat-view"),
          composer: box("#composer"),
          send: box("#send"),
          sidebar: box("#sidebar"),
          sidebarClient: document.getElementById("sidebar").clientHeight,
          sidebarScroll: document.getElementById("sidebar").scrollHeight,
          sidebarOverflow: getComputedStyle(document.getElementById("sidebar")).overflowY,
          logClient: log.clientHeight,
          logScroll: log.scrollHeight,
          documentHeight: document.documentElement.scrollHeight,
        };
      });
      const label = `${viewport.width}x${viewport.height}, expanded=${expanded}`;
      console.log(`${label}: ${JSON.stringify(dimensions)}`);
      try {
        assert.ok(dimensions.composer.bottom <= viewport.height + 1,
          `${label}: composer bottom ${dimensions.composer.bottom} exceeds viewport ${viewport.height}`);
        assert.ok(dimensions.send.bottom <= viewport.height + 1 && dimensions.send.top >= 0,
          `${label}: send button is clipped`);
        assert.ok(dimensions.content.bottom <= dimensions.shell.bottom + 1,
          `${label}: chat exceeds its shell`);
        assert.ok(dimensions.logClient > 0 && dimensions.logScroll > dimensions.logClient,
          `${label}: long conversations must scroll within the log`);
        assert.ok(dimensions.documentHeight <= viewport.height + 1,
          `${label}: the document must fit the viewport`);
        if (viewport.width >= 768) {
          assert.ok(dimensions.sidebar.bottom <= viewport.height + 1,
            `${label}: sidebar exceeds viewport`);
          if (dimensions.sidebarScroll > dimensions.sidebarClient) {
            assert.ok(["auto", "scroll"].includes(dimensions.sidebarOverflow),
              `${label}: sidebar account controls must remain reachable by scrolling`);
          }
          await page.locator("#switch-identity").scrollIntoViewIfNeeded();
          const account = await page.locator("#switch-identity").boundingBox();
          assert.ok(account.y >= 0 && account.y + account.height <= viewport.height + 1,
            `${label}: account controls are clipped`);
        }
      } catch (error) {
        failures.push(error);
      }
    }
  }
  assert.deepEqual(failures.map(error => error.message), []);
  console.log(`chat layout browser checks passed (${cases.length * 2} cases)`);
} finally {
  await browser.close();
}
