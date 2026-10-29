import assert from "node:assert/strict";
import test from "node:test";
import * as life from "../app/views/js/life.js";

test("a running life catchup refreshes until its final snapshot", async () => {
  assert.equal(typeof life.LifeBoard, "function", "补算看板尚未支持自动刷新");
  const callbacks = [];
  const shown = [];
  const snapshots = [{ catchup: { running: true }, events: ["一格"] },
    { catchup: { running: false }, events: ["摘要", "最近详细流水"] }];
  const board = new life.LifeBoard({ client: { epoch: 1, json: async () => snapshots.shift() },
    render: data => shown.push(data), schedule: callback => callbacks.push(callback) });
  await board.load();
  assert.equal(shown.length, 1);
  assert.equal(callbacks.length, 1);
  await callbacks.shift()();
  assert.deepEqual(shown.at(-1).events, ["摘要", "最近详细流水"]);
  assert.equal(callbacks.length, 0, "补算结束后应停止轮询");
});

test("leaving the page or switching account cancels the pending catchup refresh", async () => {
  assert.equal(typeof life.LifeBoard, "function");
  for (const change of ["account", "page"]) {
    const callbacks = [];
    let requests = 0, revision = 1;
    const client = { epoch: 1, json: async () => { requests++; return { catchup: { running: true } }; } };
    const board = new life.LifeBoard({ client, revision: () => revision,
      schedule: callback => callbacks.push(callback) });
    await board.load();
    if (change === "account") client.epoch++;
    else revision++;
    await callbacks.shift()();
    assert.equal(requests, 1, "旧页面不应继续读取生活看板");
  }
});

test("a slow earlier life response cannot overwrite a newer snapshot", async () => {
  assert.equal(typeof life.LifeBoard, "function");
  const pending = [], shown = [];
  const board = new life.LifeBoard({ client: { epoch: 1,
    json: () => new Promise(resolve => pending.push(resolve)) }, render: data => shown.push(data) });
  const first = board.load();
  const second = board.load();
  pending[1]({ events: ["新"] });
  await second;
  pending[0]({ events: ["旧"], catchup: { running: true } });
  await first;
  assert.deepEqual(shown, [{ events: ["新"] }]);
});
