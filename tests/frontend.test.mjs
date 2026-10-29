import assert from "node:assert/strict";
import test from "node:test";
import { ApiClient, ApiError } from "../app/views/js/api.js";
import { readEvents } from "../app/views/js/sse.js";
import { AudioPlayer } from "../app/views/js/audio.js";
import { validateCareText } from "../app/views/js/life.js";

async function relationshipModule() {
  const module = await import("../app/views/js/relationship.js").catch(() => null);
  assert.ok(module, "relationship board is not implemented");
  return module;
}

test("relationship form preserves an explicit local time without guessing one", async () => {
  const { promisePayload } = await relationshipModule();
  assert.deepEqual(promisePayload("  给你带点心  ", ""), { text: "给你带点心", due_at: null });
  const selected = "2026-10-07T20:30";
  assert.equal(promisePayload("给你带点心", selected).due_at, new Date(selected).getTime() / 1000);
  assert.throws(() => promisePayload("   ", ""), /写下/);
  assert.throws(() => promisePayload("给你带点心", "invalid"), /时间/);
});

test("relationship actions send the stable promise id and keep miss reasons", async () => {
  const { RelationshipBoard } = await relationshipModule();
  const requests = [];
  const client = new ApiClient({ storage: storage(), fetchImpl: async (path, options) => {
    requests.push({ path, body: JSON.parse(options.body), method: options.method });
    return Response.json({ promise: { id: "p2", status: "done" } });
  } });
  const board = new RelationshipBoard({ client });
  await board.complete("p2");
  await board.miss("p9", "  临时有事  ");
  assert.deepEqual(requests, [
    { path: "/api/relationship/promises/p2/complete", body: { note: "" }, method: "POST" },
    { path: "/api/relationship/promises/p9/miss", body: { reason: "临时有事" }, method: "POST" },
  ]);
  await assert.rejects(board.miss("p9", "   "), /原因/);
  assert.equal(requests.length, 2);
});

test("relationship responses cannot replace a later account or page", async () => {
  const { RelationshipBoard } = await relationshipModule();
  const waiting = [];
  const shown = [];
  let revision = 1;
  const client = new ApiClient({ storage: storage(), fetchImpl: () =>
    new Promise(resolve => waiting.push(resolve)) });
  client.setIdentity("token", "old-token");
  const board = new RelationshipBoard({ client, revision: () => revision, render: data => shown.push(data) });
  const oldAccount = board.load();
  client.setIdentity("token", "new-token");
  waiting.shift()(Response.json({ stage: { key: "旧账号" } }));
  await oldAccount;
  const oldPage = board.load();
  revision++;
  waiting.shift()(Response.json({ stage: { key: "旧页面" } }));
  await oldPage;
  assert.deepEqual(shown, []);
  const fresh = board.load();
  waiting.shift()(Response.json({ stage: { key: "当前" } }));
  await fresh;
  assert.deepEqual(shown, [{ stage: { key: "当前" } }]);
});

test("a slower relationship refresh cannot replace its newer snapshot", async () => {
  const { RelationshipBoard } = await relationshipModule();
  const waiting = [];
  const shown = [];
  const client = new ApiClient({ storage: storage(), fetchImpl: () =>
    new Promise(resolve => waiting.push(resolve)) });
  const board = new RelationshipBoard({ client, render: data => shown.push(data) });
  const first = board.load();
  const second = board.load();
  waiting[1](Response.json({ promises: [{ id: "p2", status: "done" }] }));
  await second;
  waiting[0](Response.json({ promises: [{ id: "p2", status: "open" }] }));
  await first;
  assert.deepEqual(shown, [{ promises: [{ id: "p2", status: "done" }] }]);
});

function storage(values = {}) {
  const items = new Map(Object.entries(values));
  return {
    getItem: key => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: key => items.delete(key),
  };
}

test("care text trims and validates before pinning", () => {
  assert.equal(validateCareText("  理完报销单，记得休息  "), "理完报销单，记得休息");
  assert.throws(() => validateCareText("   "), /不能为空/);
  assert.throws(() => validateCareText("字".repeat(61)), /60/);
});

test("password login receives a token and never persists the password", async () => {
  const saved = storage();
  const requests = [];
  const client = new ApiClient({ storage: saved, fetchImpl: async (path, options) => {
    requests.push({ path, ...options, headers: new Headers(options.headers) });
    return Response.json(path === "/api/auth/login" ? { access_token: "session-token", username: "小林" } : { ok: true });
  } });
  await client.login("小林", "a-good-password");
  await client.json("/api/state");
  assert.deepEqual(JSON.parse(requests[0].body), { username: "小林", password: "a-good-password" });
  assert.equal(requests[0].headers.has("Authorization"), false);
  assert.equal(requests[1].headers.get("Authorization"), "Bearer session-token");
  assert.equal(saved.getItem("auth_token"), "session-token");
  assert.equal(saved.getItem("auth_username"), "小林");
  assert.equal(saved.getItem("password"), null);
  assert.equal(saved.getItem("user_id"), null);
});

test("legacy claim sends the authenticated request and returns the server decision", async () => {
  const requests = [];
  const client = new ApiClient({ storage: storage(), fetchImpl: async (path, options) => {
    requests.push({ path, method: options.method, headers: new Headers(options.headers), body: options.body });
    return Response.json({ claimed: true, storage_uid: "" });
  } });
  client.setIdentity("token", "session-token");

  const result = await client.claimLegacy();

  assert.deepEqual(result, { claimed: true, storage_uid: "" });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/auth/legacy");
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].headers.get("Authorization"), "Bearer session-token");
  assert.equal(requests[0].body, undefined);
});

test("anonymous login failure preserves the server credential error", async () => {
  const client = new ApiClient({ storage: storage(), fetchImpl: async () =>
    Response.json({ detail: "账号或密码不正确。" }, { status: 401 }) });
  await assert.rejects(client.login("小林", "wrong"), error => error.status === 401 && error.message === "账号或密码不正确。");
});

test("logout calls the server with its token and clears saved identity", async () => {
  const saved = storage({ auth_token: "session-token", auth_username: "小林" });
  let request;
  const client = new ApiClient({ storage: saved, fetchImpl: async (path, options) => {
    request = { path, ...options, headers: new Headers(options.headers) };
    return Response.json({ ok: true });
  } });
  client.restoreIdentity();
  await client.logout();
  assert.equal(request.path, "/api/auth/logout");
  assert.equal(request.headers.get("Authorization"), "Bearer session-token");
  assert.equal(saved.getItem("auth_token"), null);
  assert.equal(saved.getItem("auth_username"), null);
});

test("Chinese and literal percent nicknames remain valid HTTP identities", async () => {
  const saved = storage();
  let headers;
  const client = new ApiClient({ storage: saved, fetchImpl: async (_path, options) => {
    headers = new Headers(options.headers);
    return Response.json({ ok: true });
  } });
  for (const name of ["\u5976\u9f99", "A+B 100%", "%E5%A5%B6", "M\u00e9lanie"]) {
    client.setIdentity("nickname", name);
    await client.json("/api/state");
    assert.equal(decodeURIComponent(headers.get("X-User-Id")), name);
    assert.equal(headers.get("X-User-Id-Encoding"), "percent-encoded");
    client.persistIdentity();
    assert.equal(saved.getItem("user_id"), name);
  }
});

test("saved tokens take precedence and anonymous health requests have no identity", async () => {
  const saved = storage({ auth_token: "issued-token", user_id: "old-name" });
  let headers;
  const client = new ApiClient({ storage: saved, fetchImpl: async (_path, options) => {
    headers = new Headers(options.headers);
    return Response.json({ ok: true });
  } });
  assert.equal(client.restoreIdentity(), true);
  await client.json("/api/state");
  assert.equal(headers.get("Authorization"), "Bearer issued-token");
  assert.equal(headers.has("X-User-Id"), false);
  await client.json("/api/healthz", { anonymous: true });
  assert.equal(headers.has("Authorization"), false);
});

test("401 clears both credentials and invokes recovery once", async () => {
  const saved = storage({ auth_token: "expired", user_id: "old-name" });
  let recovered = 0;
  const client = new ApiClient({ storage: saved, onUnauthorized: () => recovered++,
    fetchImpl: async () => Response.json({ detail: "expired" }, { status: 401 }) });
  client.restoreIdentity();
  await assert.rejects(client.json("/api/state"), error => error instanceof ApiError && error.status === 401);
  assert.equal(saved.getItem("auth_token"), null);
  assert.equal(saved.getItem("user_id"), null);
  assert.equal(client.identity.value, "");
  assert.equal(recovered, 1);
});

test("POST JSON and SSE requests use the same encoded identity", async () => {
  const requests = [];
  const client = new ApiClient({ storage: storage(), fetchImpl: async (path, options) => {
    requests.push({ path, ...options, headers: new Headers(options.headers) });
    return Response.json({ ok: true });
  } });
  client.setIdentity("nickname", "\u5976\u9f99");
  await client.json("/api/command", { method: "POST", body: { command: "/slots" } });
  await client.request("/api/chat/stream", { method: "POST", body: JSON.stringify({ message: "hello" }) });
  assert.equal(requests[0].body, '{"command":"/slots"}');
  assert.equal(requests[0].headers.get("Content-Type"), "application/json");
  assert.equal(requests[0].headers.get("X-User-Id"), requests[1].headers.get("X-User-Id"));
});

test("SSE preserves UTF-8 split packets, CRLF and all five event types", async () => {
  const events = [
    { type: "meta", recalled: [{ text: "memory" }] },
    { type: "delta", text: "\u4f60\u597d" },
    { type: "done", memory_note: "saved" },
    { type: "audio", wav_b64: "YXVkaW8=" },
    { type: "audio_end" },
  ];
  const bytes = new TextEncoder().encode(events.map(value => `data: ${JSON.stringify(value)}\r\n\r\n`).join(""));
  const body = new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += 2) controller.enqueue(bytes.slice(i, i + 2));
    controller.close();
  } });
  const received = [];
  await readEvents(new Response(body), value => received.push(value));
  assert.deepEqual(received, events);
});

test("SSE rejects incomplete responses rather than claiming success", async () => {
  const body = new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('data: {"type":"delta","text":"partial"}\n\n'));
    controller.close();
  } });
  await assert.rejects(readEvents(new Response(body), () => {}), /中断/);
});

test("SSE shows a model connection error after preserving partial text", async () => {
  const events = [];
  const body = new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode(
      'data: {"type":"delta","text":"部分回答"}\n\n' +
      'data: {"type":"error","message":"模型连接失败，请稍后重试。"}\n\n'));
    controller.close();
  } });
  await assert.rejects(readEvents(new Response(body), value => events.push(value)),
    /模型连接失败/);
  assert.equal(events[0].text, "部分回答");
});

test("cancelling after response headers still aborts the streaming body", async () => {
  let transportSignal;
  const client = new ApiClient({ storage: storage(), fetchImpl: async (_path, options) => {
    transportSignal = options.signal;
    return new Response("data: streaming");
  } });
  const controller = new AbortController();
  await client.request("/api/chat/stream", { signal: controller.signal });
  controller.abort();
  assert.equal(transportSignal.aborted, true);
});

test("a late 401 from the previous identity cannot sign out the new identity", async () => {
  let resolve;
  let recovered = 0;
  const client = new ApiClient({ storage: storage(), onUnauthorized: () => recovered++,
    fetchImpl: () => new Promise(done => { resolve = done; }) });
  client.setIdentity("token", "old-token");
  const old = client.json("/api/state");
  client.setIdentity("token", "new-token");
  resolve(Response.json({ detail: "expired" }, { status: 401 }));
  await assert.rejects(old, error => error.status === 401);
  assert.equal(client.identity.value, "new-token");
  assert.equal(recovered, 0);
});

test("an MSE failure retains every audio chunk for the Blob fallback", async () => {
  const saved = { Audio: globalThis.Audio, MediaSource: globalThis.MediaSource,
    create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  const blobs = [];
  let source;
  class BrokenSource extends EventTarget {
    static isTypeSupported() { return true; }
    constructor() { super(); source = this; }
    addSourceBuffer() { throw new Error("MSE unavailable"); }
  }
  try {
    globalThis.MediaSource = BrokenSource;
    globalThis.Audio = class { pause() {} play() { return Promise.resolve(); } };
    URL.createObjectURL = value => { if (value instanceof Blob) blobs.push(value); return "blob:test"; };
    URL.revokeObjectURL = () => {};
    const player = new AudioPlayer();
    player.start();
    player.push("YWJj");
    source.dispatchEvent(new Event("sourceopen"));
    player.push("ZGVm");
    player.end();
    assert.equal(blobs.length, 1);
    assert.equal(await blobs[0].text(), "abcdef");
    player.stop();
  } finally {
    globalThis.Audio = saved.Audio;
    globalThis.MediaSource = saved.MediaSource;
    URL.createObjectURL = saved.create;
    URL.revokeObjectURL = saved.revoke;
  }
});
