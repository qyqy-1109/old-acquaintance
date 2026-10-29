import assert from "node:assert/strict";
import test from "node:test";
import { BubbleSplitter, MARK } from "../app/views/js/bubble.js";

/** 模拟前端：按 index 落气泡，每条写的是**完整文本快照**（不是追加）。
 *  这才是真实契约——事件发了几次是实现细节，最终几条气泡长什么样才是行为。 */
function bubbles(chunks, max = 3) {
  const items = [];
  const splitter = new BubbleSplitter({ max, onMessage: (text, index) => { items[index] = text; } });
  for (const chunk of chunks) splitter.push(chunk);
  splitter.end();
  return items;
}

test("a reply without any marker stays a single message", () => {
  assert.deepEqual(bubbles(["睡不着呀？行，我陪着你。"]), ["睡不着呀？行，我陪着你。"]);
});

test("marker chunks arriving split are still recognised", () => {
  assert.deepEqual(bubbles(["辛苦啦\n@@@\n晚饭吃了没"]), ["辛苦啦", "晚饭吃了没"]);
  assert.deepEqual(bubbles(["辛苦啦\n@", "@", "@\n晚饭吃了没"]), ["辛苦啦", "晚饭吃了没"]);
});

test("the marker splits messages even when it appears inline", () => {
  assert.deepEqual(bubbles(["第一句@@@第二句"]), ["第一句", "第二句"]);
  assert.deepEqual(bubbles(["邮箱是 a@@@b 你记一下"]), ["邮箱是 a", "b 你记一下"]);
  assert.deepEqual(bubbles(["第一句" + MARK + "第二句"]), ["第一句", "第二句"]);
});

test("the screenshot reply splits for every possible chunk boundary", () => {
  const text = "下午好呀，正抄笔记抄到头大@@@你吃饭没";
  const expected = ["下午好呀，正抄笔记抄到头大", "你吃饭没"];
  assert.deepEqual(bubbles([text]), expected);
  for (let boundary = 0; boundary <= text.length; boundary++) {
    assert.deepEqual(bubbles([text.slice(0, boundary), text.slice(boundary)]), expected);
  }
  assert.deepEqual(bubbles(Array.from(text)), expected);
});

test("inline marker prefixes never flash during streaming", () => {
  const snapshots = [];
  const splitter = new BubbleSplitter({ onMessage: text => snapshots.push(text) });
  splitter.push("抄到头大@");
  assert.deepEqual(snapshots, ["抄到头大"]);
  splitter.push("@");
  assert.deepEqual(snapshots, ["抄到头大"]);
  splitter.push("@你吃饭没");
  splitter.end();
  assert.deepEqual(splitter.segments, ["抄到头大", "你吃饭没"]);
  assert.ok(snapshots.every(text => !text.includes("@")));
});

test("ordinary at signs survive when they do not form the marker", () => {
  assert.deepEqual(bubbles(["联系 a@", "example.com，@", "@两个人"]),
    ["联系 a@example.com，@@两个人"]);
  assert.deepEqual(bubbles(["结尾@"]), ["结尾@"]);
  assert.deepEqual(bubbles(["结尾@@"]), ["结尾@@"]);
});

test("inline separators ignore empty parts and respect the message cap", () => {
  assert.deepEqual(bubbles(["@@@一@@@@@@二@@@三@@@四@@@"]), ["一", "二", "三\n四"]);
  assert.deepEqual(bubbles(["一@@@二"], 1), ["一\n二"]);
});

test("leading and trailing blank lines never produce empty bubbles", () => {
  assert.deepEqual(bubbles(["\n\n@@@\n先喘口气\n@@@\n\n"]), ["先喘口气"]);
});

test("messages beyond the cap merge into the last bubble instead of being dropped", () => {
  assert.deepEqual(bubbles(["一\n@@@\n二\n@@@\n三\n@@@\n四"]),
    ["一", "二", "三\n四"]);
  assert.deepEqual(bubbles(["一\n@@@\n二"], 1), ["一\n二"]);
});

test("nothing is emitted for an empty reply", () => {
  assert.deepEqual(bubbles([""]), []);
  assert.deepEqual(bubbles(["\n\n"]), []);
});

test("every line boundary closes a message, the tail streams in live", () => {
  // 逐字上屏靠"快照"：前端拿到的一直是**这条的完整文本**，不是增量，
  // 所以重复的一次通知也不会把字写重（app.js 里按文本比对再落 DOM）。
  const parts = [];
  const splitter = new BubbleSplitter({ onMessage: text => parts.push(text) });
  splitter.push("刚刚");
  assert.deepEqual(parts, ["刚刚"], "残段也要立刻可见，不能等整行");
  splitter.push("浇完花");
  assert.deepEqual(parts, ["刚刚", "刚刚浇完花"]);
  const settled = parts.length;
  splitter.push("\n");
  assert.equal(parts.at(-1), "刚刚浇完花", "换行只是把这条落定，内容不变");
  assert.ok(parts.length >= settled);
  splitter.push("你呢");
  splitter.end();
  assert.equal(parts.at(-1), "刚刚浇完花\n你呢");
});

test("a lone @ at line start is held back so the marker never flashes", () => {
  const parts = [];
  const splitter = new BubbleSplitter({ onMessage: text => parts.push(text) });
  splitter.push("辛苦啦\n@");
  assert.deepEqual(parts, ["辛苦啦"], "可能是标记开头，先按住不上屏");
  splitter.push("@");
  assert.deepEqual(parts, ["辛苦啦"]);
  splitter.push("@\n");
  splitter.push("晚饭");
  splitter.end();
  assert.deepEqual(parts.at(-1), "晚饭");
});

test("non-streaming: one whole push equals the same text fed in pieces", () => {
  // T86 先做**非流式**那一半：`/api/chat` 与主动说话拿到的都是一整段文本，
  // 一次 push 就完事。它必须与流式碎片喂法得到**同一条分段结果**——
  // 否则"先用非流式看效果"看到的和线上流式会是两回事，等于验了个假。
  const whole = "一\n@@@\n二\n@@@\n三\n@@@\n四";
  const pieces = ["一", "\n@", "@@", "\n二\n", "@@@\n三", "\n@@@", "\n四"];
  assert.deepEqual(bubbles([whole]), ["一", "二", "三\n四"]);
  assert.deepEqual(bubbles(pieces), bubbles([whole]));
});

test("a complete marker at a chunk boundary never reaches the screen", () => {
  const parts = [];
  const splitter = new BubbleSplitter({ onMessage: text => parts.push(text) });
  splitter.push("甲\n");
  assert.deepEqual(parts, ["甲"]);
  splitter.push("@@@");
  assert.deepEqual(parts, ["甲"], "完整标记也不许上屏");
  splitter.push("\n乙");
  splitter.end();
  assert.deepEqual(parts.at(-1), "乙");
  assert.deepEqual(splitter.segments, ["甲", "乙"]);
});

test("a marker at the start of a line splits, and the rest of that line starts the next message", () => {
  // 真机 8 次样本里出现过这种写法（`@@@ 要是碰上下雨…`）：要求标记**独占一行**太严，
  // 它会直接漏成正文发给用户看。放宽成"**行首**是标记就分条"，该行剩下的内容算下一段。
  // 句中出现的 @@@ 也必须切开，不能让内部标记进入气泡。
  assert.deepEqual(bubbles(["甲\n@@@ 乙"]), ["甲", "乙"]);
  assert.deepEqual(bubbles(["甲\n@@@乙"]), ["甲", "乙"]);
  assert.deepEqual(bubbles(["甲\n@@@ 乙\n@@@ 丙"]), ["甲", "乙", "丙"]);
  assert.deepEqual(bubbles(["甲\n  @@@  乙"]), ["甲", "乙"], "标记前后的空白不算");
  assert.deepEqual(bubbles(["甲@@@乙"]), ["甲", "乙"], "句中标记也要切开");
});
