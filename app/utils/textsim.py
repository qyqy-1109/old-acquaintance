# -*- coding: utf-8 -*-
"""短句判重（`#138`）：她主动开口时，别把刚说过的话再说一遍。

**为什么不用 embedding**：这里要判的是"同一个人几分钟内说的两句话"，字符级相似度足够；
embedding 会引入网络调用与阈值漂移（`T88` 还要求测试套件零外联）。

阈值 `0.62` 是在真机样本上定的（2026-09-30）：
  · 「老板娘还多搭了两根筒骨」 vs 「排骨挑好了，老板娘还多搭两根筒骨」 → **0.8+**（该判重）
  · 「汤里扔进去，炖出来更香」 vs 上面那条 → 0.2 左右（同一次开口的第二条，**不该**判重）
  · 「汤炖好了，小青菜也放了」 vs 「旁边摊子的小青菜看着挺嫩」 → 0.3 左右
    （同话题的**新进展**，不该判重——这条比误判重复更重要：宁可让她多说一句，也别让她闭嘴）
"""

from __future__ import annotations

import difflib
import re

REPEAT_THRESHOLD = 0.62
_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:()\[\]{}「」『』\"'…~～—－-]+")


def normalize(text: str) -> str:
    """去掉标点与空白，只留字——判重不该被标点差异骗过。"""
    return _PUNCT.sub("", str(text or ""))


def similar(a: str, b: str) -> float:
    """两句的字符级相似度（0~1）。任一句为空给 0；**包含关系算 1.0**。

    包含关系单独判，是因为"短句被长句整段含住"在真机上就是重复
    （「老板娘还多搭了两根筒骨」几乎就是那条长句的尾巴），而 `SequenceMatcher`
    对小句子会给偏低的比率。
    """
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def is_repeat(text: str, others, threshold: float = REPEAT_THRESHOLD) -> bool:
    """`text` 是否与 `others` 里某一条太像（＝重复，应当丢掉）。"""
    return any(similar(text, other) >= threshold for other in (others or []))


# 「她在问他」的语气标记（`#146`）。**故意只收强标记**：
# 「吗／吧／是不是／怎么样／问号」都明确是问句；而 `没`、`呢`、`么` 单独出现太弱——
# 「我课表上明天没课」是陈述，「团子又趴我腿上呢」也不是在问他，收了就会误杀。
_ASK_MARKS = ("吗", "吧", "是不是", "怎么样", "？", "?")


def asks_about_her_own(text: str, own) -> str:
    """她在**拿只有她自己知道的东西**反问他吗？命中就返回那个词，否则空串（`#146`）。

    真机现场（2026-10-01 09:18）：她报备完"在收球衣…刚下楼捡回来"，紧跟着问了一句
    「**团子没扒窗户吧**」——团子是**她自己的猫**，他答不上来。这正是 `#130`/`#131`/
    `#143` 那一族的归属错，提示词写了三处仍然漏了一次，所以照 `D4` 补机器兜底。

    判据刻意保守：同一个**分句**里既出现她的东西、又出现问句标记才算命中——
    这样"团子又趴我腿上呢，你那边忙不忙？"这种（前者陈述、后者问他）不会被误杀。
    """
    own = [str(n or "").strip() for n in (own or [])]
    own = [n for n in own if n]
    if not own:
        return ""
    # `B2-①`（工单二）：**不把 `？` / `?` 当分隔符**。原来 `re.split` 把它们吃掉，
    # 于是 `_ASK_MARKS` 里的问号**永远不可能**出现在分句里 → 「团子在不在家？」这类
    # 纯问号形态全漏（`#158` 的 6 条用例恰好全带「吧」，所以没抓到）。
    for part in re.split(r"[。！\n，,；;]", str(text or "")):
        if not any(m in part for m in _ASK_MARKS):
            continue
        for noun in own:
            at = part.find(noun)
            if at < 0:
                continue
            # `B2-③`：`你/您的 (+那/这) + 名词` 说的是**他的**东西（"你阳台那盆花…"），
            # 不能拿通用名词（阳台／出租屋／课表）把这类话误杀。
            if re.search(r"[你您]们?(的)?[那这]?$", part[:at]):
                # `B2-③` 边角（评审复验）：复数也要认——「你们那阳台」是他的。
                continue
            return noun
    return ""
