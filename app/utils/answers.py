# -*- coding: utf-8 -*-
"""回答前的流式清洗（`T83`）：她**会把内部时间标记念出来**，这里只剥这一种。

问题是怎么被发现的（`#101` 真调模型验证，三轮里两轮复现）：回答会以
「（今天晚上 21:26）」开头——那是给她的**内部标记**（【对话顺序】里的时间前缀、
【当前时间】那行），不是该说给对方听的话。看图就明白：

    她答：「（今天晚上 21:33）累就赶紧歇着吧，这会儿也不早了～」

**为什么走清洗而不是继续调提示词**：提示词试了两轮都压不住（先在【对话顺序】写
"不要念出来"→ 无效；再在【当前时间】写"不要主动报时间…除非对方问现在几点"→
实测 21:27 / 22:33 照旧出现）。行为层压不住，就在输出侧兜一道。

三条约束，缺一条就不该做这件事：
1. **只剥开头**（`^`）：句中出现的时间（"明天 10 点见"）是她正常表达，绝不能碰。
2. **只剥"刚开头就报时间"**：要求标记紧跟在开头（允许前置空白），且剥完要把
   残留的标点/空白收干净。**对方问"现在几点"时她得照答**——那条回答不以
   "（今天…）"开头，所以不受影响。
3. **必须留痕**：每次剥离记一条事件/日志。悄悄改模型输出会让人以为她本来就这么说，
   下次统计又当成新问题（这正是 `T83` 的可观测性要求）。

流式实现：头几个字符先压住（`HOLD`），判定完再决定"剥掉"或"原样放行"，
所以首字延迟只多一帧、不会等全文。
"""

from __future__ import annotations

import re

from app.log import get

log = get("sanitize")

# 开头那段"内部时间标记"的形状（都是我自己生成的，所以可以写死）：
#   （今天晚上 21:26）／（今天下午 14:05）／（昨天 21:30）／（3 天前 14:00）
#   [今天晚上 21:26] ／ 今天晚上 21:26 ／ （2026年9月28日 周一 21:26）
_PERIOD = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)"
_ABS_DATE = r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
_REL_DATE = r"(?:今天|昨天|前天|明天|\d{1,2}\s*天前)"
_DATE_PART = r"(?:" + _REL_DATE + r"|" + _ABS_DATE + r")"
_TIME_PART = r"(?:\d{1,2}\s*[:：]\s*\d{2})"

# 什么才算"内部时间标记"——只有这两族，其余一律不碰：
#   A. **括号包起来的**：（今天晚上）／（今天下午 14:05）／[昨天 21:30]／（2026年9月28日 周一 21:26）
#   B. **日期词/时段 + 具体时刻**（没有括号）：今天晚上 21:26
# 为什么必须收这么窄：放宽到"只要有日期词"就会误伤正常表达——
# 实测把「明天 10 点见」「（今天）在的」都剥了（约定时间被她剥掉，比念时间更糟）。
# 括号族要求"括号 + 至少一个日期/时段/时刻"，所以「（我想了想）」不会被碰。
# 只匹配到"时刻/时段"为止：**收尾的括号与标点交给 strip_time_prefix 统一吃**
# （放在正则里那个懒惰量词会把绝对日期前半截吞掉，实测过）。
# 尾部那个 `[^\n\]\)】）]{0,12}?` 负责**把括号里剩下的东西一起吃掉**
# （「（2026年9月28日 周一 21:26）」里的" 周一 "），上限 12 字符防止跨得没边。
_MARK_BRACKETED = re.compile(
    r"^[\s\[\(【（]+\s*(?:" + _DATE_PART + r")?\s*" + _PERIOD + r"?\s*"
    r"(?:" + _TIME_PART + r")?[^\n\]\)】）]{0,12}[\s\]\)】）]+"
)
# 裸族：**日期是必需的**（否则"晚上 21:26"这种没有日期的正常表达会被误剥）。
_MARK_BARE = re.compile(
    r"^[\s]*" + _DATE_PART + r"\s*" + _PERIOD + r"?\s*" + _TIME_PART
)
_MARK_HAS = re.compile(r"(?:" + _DATE_PART + r"|" + _PERIOD + r"|" + _TIME_PART + r")")
# 括号族的匹配必须**真的以收尾括号结束**，否则说明标记还没攒全（流式分块会切开它）。
# ⚠ 不能只写 `[\s\]\)】）]+$`——末尾一个空格就能骗过它（实测：「（今天晚上 」被判成闭合）。
_CLOSED = re.compile(r"\s*[\]\)】）]+\s*$")


def _pick_match(s: str):
    """两族模式各试一次，**取匹配更长的那个**。

    为什么不是 `A or B`：括号族是可选式的（括号 + 日期? + 时段? + 时刻?），
    当它只匹配到"（"就会被短匹配赢走——实测「（今天晚上 21:26）…」被裸族截成
    「今天晚上 21:26」，剩下的"）"留在回答里。取最长匹配才是对的。
    """
    best = None
    for rx in (_MARK_BRACKETED, _MARK_BARE):
        m = rx.match(s)
        if m and (best is None or m.end() > best.end()):
            best = m
    return best

# 先压住这么多字符再判定：足够看清是不是时间标记，又不至于等太久
HOLD = 24


def strip_time_prefix(text: str) -> tuple[str, bool]:
    """剥掉开头的内部时间标记。返回 `(清洗后的文本, 是否剥掉了)`。

    只动开头；不匹配就原样返回（**绝不**改中间与结尾）。
    两族模式（见上面 `_MARK_BRACKETED` / `_MARK_BARE` 的注释）都必须**含日期词或时段词**，
    否则不认——「（我想了想）」这类普通括号因此不会被误伤。
    """
    s = str(text or "")
    m = _pick_match(s)
    if not m or not m.group(0).strip() or not _MARK_HAS.search(m.group(0)):
        return s, False
    # 1) 吃掉尾部残留：收尾括号、空白、以及紧跟的分隔标点
    rest = re.sub(r"^[\s\]\)】）]*", "", s[m.end():])
    rest = re.sub(r"^[\s，,、。：:；;\-—－]+", "", rest)
    return rest, True


def note_strip(where: str, removed: str) -> None:
    """留痕：静默改模型输出会掩盖行为问题（`T83` 的第三条约束）。"""
    log.info("[回答清洗] 剥掉开头的时间标记：%r（来源 %s）", str(removed)[:40], where)


class PrefixStripper:
    """流式清洗：前 `HOLD` 个字符压住，判定后要么剥掉、要么原样放行。

    用法：`out = PrefixStripper(); for chunk in ...: yield from out.feed(chunk)`，
    结束时 `yield from out.flush()`。
    """

    def __init__(self, hold: int = HOLD, on_strip=None):
        self._hold = max(1, int(hold))
        self._buf = ""
        self._removed = ""        # 已经剥掉的部分（留给痕用，最后一起记）
        self._done = False
        self._on_strip = on_strip

    def feed(self, chunk: str) -> list[str]:
        if self._done:
            return [chunk] if chunk else []
        self._buf += chunk or ""
        if len(self._buf) < self._hold:
            return []
        return self._decide()

    def flush(self) -> list[str]:
        if self._done:
            return []
        out = self._decide(force=True)
        self._report()
        return out

    def _decide(self, force: bool = False) -> list[str]:
        """判定。**分块会把标记切成两半**（「（今天」+「晚上 21:26）」），
        所以只有"明显不是时间标记"或"标记已经越过窗口"时才下结论，
        否则继续攒——看不到窗口边界就再等一帧，比剥错强。
        """
        m = _pick_match(self._buf)
        # 括号族还没闭上 → 标记没攒全（流式会把它切开），等一帧。
        # ⚠ 只有**括号族**才要求闭合：裸族（「今天晚上 21:26 我在的」）本来就没有括号，
        # 拿它去要求闭合会把正常情况也一直等下去。
        bracketed_open = bool(m) and m.group(0).lstrip()[:1] in "([【（"
        pending_close = bracketed_open and not _CLOSED.search(m.group(0))
        complete = bool(m) and _MARK_HAS.search(m.group(0)) and not pending_close
        if complete and m.end() < len(self._buf):
            grown = [self._buf[m.end():]]
            self._removed += self._buf[:m.end()]
            self._buf = ""
            self._done = True
            self._report()
            return grown
        # 还没闭合就**一直等**，不设上限：标记是确定的（形状就那么几种），
        # 攒着不会无限增长——比"到上限就放弃、把「（今天」当正文吐出去"强。
        # （实测：hold=3 的极端分块下，到上限强制定案会把标记的尾巴留进回答。）
        if pending_close:
            return []
        if not complete and not force and len(self._buf) < self._hold * 2:
            return []
        if len(self._buf) >= self._hold * 2 or force:
            if m:
                text, _ = strip_time_prefix(self._buf)
                self._removed += self._buf[: len(self._buf) - len(text)]
            else:
                text = self._buf
            self._buf = ""
            self._done = True
            self._report()
            return [text] if text else []
        return []          # 还没看清，继续攒

    def _report(self) -> None:
        if self._removed and self._on_strip:
            try:
                self._on_strip(self._removed)
            except Exception:      # 留痕失败不能影响回答
                pass
        self._removed = ""
