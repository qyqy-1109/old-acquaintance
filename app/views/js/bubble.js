// 多消息气泡的切分器（T86）：把一轮回复切成 1~3 条短消息。
//
// 为什么切成"多条"要在**前端**做，而不是让模型输出 JSON 数组：
// 那样就得等整段生成完才上屏，首字延迟直接从 ~1s 变成整段（和 T61 的方向相反）。
// 这里的做法是**保持流式**：模型在两条之间单独写一行 `@@@`，边收边按行判断，
// 标记是内部约定，即使模型把它写在句中也分条，用户永远看不到它。
//
// 三条硬规则（每条都有单测守着）：
//   1. **完整标记表示分条**：`第一句@@@第二句` 也拆成两条，普通 @ 和 @@ 保留；
//   2. **空行不参与判断**：空行在纯文本里是"分段"，与"分条"混在一起会分不清，
//      所以分条只认标记（提示词也明写了不许出现空行）；
//   3. **超过条数上限时并入最后一条**，不丢字、不静默截断。
//
// 标记选 `@@@` 是实测结论（_tmptest 三轮探针，共 26 次真实调用）：`---` 只在 1/6
// 里被用、空行方案 4/6 但有歧义、把 `\n` 当字面标记 0/4；而 `@@@` 在 10 个真实提问里
// 命中 9/10、零空行、最长一段 30 字、没有一段超过 40 字。
// 原因是 `---` 会被模型当成普通排版符号；中文语料里不会自然出现的符号才稳。
//
// ===== 非流式（`/api/chat`、主动说话）与流式必须**同一条结果** =====
// 前者的文本是**一整段**拿到的（一次 `push`），后者是碎片喂进来的。两种喂法
// 必须产出同样的分段——否则"先用非流式看效果"看到的和线上流式就是两回事。
// 这条有单测钉着（`non-streaming: one whole push equals the same text fed in pieces`）。
//
// 内部状态只有三样（`segments` 是对外的真相，其余都是它的推导）：
//   · `segments` —— 每一格的**完整文本**（`onMessage(text, index)` 发的就是它）；
//   · `slot`     —— 正在写的那一格；`-1` = 上一格已被标记收尾，下一条内容要新开；
//   · `buffer`   —— 还没收到换行的残段（可能正好是标记的一半，甚至正好是标记）。
//
// 踩过的坑（写错过三版，全是同一类）：**别用"现在是不是在写"这类时序标志去推断
// 该接哪一格**。第二版拿"刚写完那条"（`done`）跟新文本做 `startsWith` 比较，
// 而 `done` 只在遇到标记行时才有值 → 第一段永远是空串比较，于是 `end()` 里的
// 落定与残段上屏各新开一条，**单条回复会变成三条重复气泡**（`['X','X','X']`，
// 由 `a reply without any marker stays a single message` 抓到）。
// 现在改成显式记 `slot`（该写哪一格由它直接决定），并且通知按 (下标, 文本) 去重。

export const MARK = "@@@";

const MAX_MESSAGES = 3;

export class BubbleSplitter {
  /** onMessage(text, index)：某条消息的文本变了。
   *  `text` 是那条消息的**完整文本**（不是增量），`index` 是它在这一轮里的序号。
   *  前端按序号定位气泡、直接把这份文本写进去——所以重复通知是安全的，
   *  "新建还是追加"的判断不在回调方。 */
  constructor({ onMessage, max = MAX_MESSAGES, mark = MARK } = {}) {
    this.onMessage = onMessage || (() => {});
    this.max = Math.max(1, Number(max) || MAX_MESSAGES);
    this.mark = mark;
    this.segments = [];             // 每一格的完整文本（对外唯一真相）
    this.slot = -1;                 // 正在写的那一格；-1 = 下一条内容要新开
    this.buffer = "";               // 还没收到换行的残段
    this._sent = { index: -1, text: "" };   // 上次通知过的内容，用来去重
  }

  push(text) {
    if (text) this.buffer += text;
    while (this.buffer) {
      const newline = this.buffer.indexOf("\n");
      const marker = this.buffer.indexOf(this.mark);
      if (marker >= 0 && (newline < 0 || marker < newline)) {
        this._commit(this.buffer.slice(0, marker));
        this.buffer = this.buffer.slice(marker + this.mark.length);
        this.slot = -1;
      } else if (newline >= 0) {
        const line = this.buffer.slice(0, newline);
        this.buffer = this.buffer.slice(newline + 1).replace(/^\s+/, "");
        this._commit(line);
      } else break;
    }
    this._tail();               // 残段：只上屏，不落定（它可能还要长）
  }

  end() {
    if (this.buffer) this._commit(this.buffer);
    this.buffer = "";
    this._tail();
  }

  /** 下一条内容该写进哪一格。
   *  没开着槽位就新开；已经到上限（或超了）就并进最后一格。 */
  _target() {
    if (this.slot >= 0) return this.slot;
    return Math.min(this.segments.length, this.max - 1);
  }

  /** 一个**完整行**到了。
   *  · 普通行 → 拼进当前格并上屏；
   *  · **空行直接丢掉**：它在纯文本里是"分段"，和"分条"混在一起会分不清。 */
  _commit(raw) {
    const line = String(raw).trim();
    if (!line) return;
    const at = this._target();
    const body = this.segments[at] || "";
    this.segments[at] = body ? body + "\n" + line : line;
    this.slot = at;
    this._emit(this.segments[at], at);
  }

  /** 还没换行的残段：只上屏、**不落定**（它还会长），逐字手感就靠这一步。
   *
   *  残段末尾若是标记前缀（`@` / `@@`）先按住，避免跨分片标记闪屏；
   *  完整标记在 push 中处理，结束时未凑成标记的前缀原样保留。 */
  _tail() {
    let tail = this.buffer.trim();
    for (let length = Math.min(this.mark.length - 1, tail.length); length > 0; length--) {
      if (tail.endsWith(this.mark.slice(0, length))) {
        tail = tail.slice(0, -length).trimEnd();
        break;
      }
    }
    const at = this._target();
    const body = this.segments[at] || "";
    const live = tail ? (body ? body + "\n" + tail : tail) : body;
    if (live) this._emit(live, at);
  }

  /** 把"这一格此刻的样子"通知出去。同一格同一文本只发一次（去重）。 */
  _emit(text, index) {
    const clean = String(text).trim();
    if (!clean) return;
    if (this._sent.index === index && this._sent.text === clean) return;
    this._sent = { index, text: clean };
    this.onMessage(clean, index);
  }
}
