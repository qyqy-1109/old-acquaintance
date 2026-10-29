# -*- coding: utf-8 -*-
"""多消息气泡 · **非流式**那一半（`T86`）：把已经拿全的一段回复按 `@@@` 切成 1~3 条。

## 为什么服务端也要有一份

前端那份（`app/views/js/bubble.js`）是**增量**切分器：文本一块一块到，它得按住
可能的标记前缀、边收边判断。而 `/api/chat`（非流式）拿到的**整段已经齐全**，
不需要那套状态机——这里就是一个纯函数：进一段文本，出一串消息。

两条路径必须给出**同一条结果**。否则"先用非流式看效果"看到的和线上流式是两回事，
等于验了个假。所以规则是逐条对齐的，两侧的单测也逐条对应：

    app/utils/bubbles.py      ←→  app/views/js/bubble.js
    tests/test_pure.py:test_split_messages_matches_the_frontend_contract
    tests/bubble.test.mjs

## 三条硬规则（和前端一样，一条都不能松）

1. **标记必须独占一行**：`第一句@@@第二句` 里的 `@@@` 不算——宁可少切一条，
   也绝不能切坏她的原话（邮箱、`a@@@b` 这种正文里真的可能出现三个 @）。
2. **空行不参与判断**：空行在纯文本里是"分段"，跟"分条"混在一起会分不清，
   所以分条只认标记；空行直接丢掉，不产生空气泡。
3. **超过条数上限时并进最后一条**，不丢字、不静默截断。

## 与提示词的分工

"要分几条、每条多短"是**说话方式**，由提示词管（见 `chat_service` 的
`MULTI_MESSAGE_HINT`，且**只注入非流式那一轮**）；这里只管"标记怎么切"。
分工的理由和别处一样：工具给事实，人格给话术。

标记 `@@@` 的选型依据（`_tmptest` 三轮探针、共 26 次真实调用）记在前端那份的注释里；
两边的 `MARK` 常量是各写一遍的（跨语言没法共享），靠上面那两组单测钉住不许漂。
"""

from __future__ import annotations

MARK = "@@@"
MAX_MESSAGES = 3


def split_messages(text: str, max_messages: int = MAX_MESSAGES,
                   mark: str = MARK) -> list[str]:
    """把一段完整回复切成 1~`max_messages` 条消息。

    没有标记就返回一条（**不硬分**）；空文本返回 `[]`（不是 `[""]`）。
    `slot` 的用法与前端 `BubbleSplitter` 完全一致：`-1` = 上一格已被标记收尾，
    下一条内容要新开；已经写满 `max_messages - 1` 格之后，后面的都并进最后一格。

    参数名不叫 `max`：那会把内置函数遮住（写这一版时就踩了，`max(1, int(max))`
    直接 `TypeError: 'int' object is not callable`）。
    """
    limit = max(1, int(max_messages or 1))
    segments: list[str] = []
    slot = -1

    def target() -> int:
        if slot >= 0:
            return slot
        return min(len(segments), limit - 1)

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:                # 规则 2：空行不是分条，直接丢
            continue
        if line.startswith(mark):   # 规则 1：**行首**是标记就算分条
            slot = -1
            # 真机 8 个样本里出现过「`@@@ ` 后面直接跟内容」的写法。那一行的剩余部分
            # **是下一段的开头**，不是正文——按"整行正好是标记"处理的话，它会连同
            # 标记一起原样发给用户看。所以把剩余部分接过来、继续走下面的写入逻辑。
            # ⚠ 放宽的只是"标记后面还有没有内容"：`第一句@@@第二句` 这类**句中**出现的
            # 标记仍然一个字都不动（`startswith` 天然满足这条）。
            line = line[len(mark):].strip()
            if not line:
                continue
        at = target()
        # 满了就并进最后一格（`at` 落在已有元素上）；否则新开一格。
        # ⚠ 必须区分这两种写法：Python 的 list **不会**像 JS 数组那样"往越界下标赋值
        # 就自动扩容"，`segments[at] = …` 在 `at == len(segments)` 时直接 IndexError
        # （写这一版时踩了）。js 那份对应的是 `this.segments[at] = …`。
        if at < len(segments):
            segments[at] = f"{segments[at]}\n{line}"
        else:
            segments.append(line)
        slot = at
    return segments
