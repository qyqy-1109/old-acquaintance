"""记忆的**读路径**：状态槽位／分寸／时间轴，以及召回、组装、对账。

从 ``memory.py`` 拆出来的 Mixin（`#073`）。`build_messages` 是每轮对话
真正被调用的那个入口——想弄懂她这轮看到了什么，从这里读起。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import os
import threading
import time
from typing import Any, Callable

import numpy as np

from app.models import db as memdb
from app.utils import clock
from app.utils.llmjson import chat_json, strings

from app.log import get

log = get("memory")


class MemoryCheckError(RuntimeError):
    """对账请求失败，不能被当成「没有矛盾」。"""

# 「这条带日期，但不是约定」——感知层写进来的天气事实。
# 为什么必须挡：`upcoming()` 只按 `event_date` 筛，而天气条目也带日期（那是给时间轴用的），
# 于是 2026-09-28 21:25 那一轮的【今天说好的事】里装的**全是天气**：
# 「2026年9月28日杭州有雾、白天有小雨，气温约27度」——真正的约定一条没有。
# 块名叫"说好的事"，装天气等于告诉模型这段不重要（那一轮她就把下午说好的事讲成了明天）。
_PLAN_WEATHER = re.compile(r"气温|℃|°C|度[，,、。 ]|风力|空气质量|[小中大暴]雨|多云|阴天|晴[天到]")


def _when_label(ts, now: dt.datetime | None = None) -> str:
    """给滑窗里的历史消息做人话时间标记：今天下午 14:05 / 昨天 21:30 / 3 天前 14:00。

    为什么必须标（用户 2026-09-23 的三条反馈同一个根因）：滑窗原来只给 `role/content`，
    模型无从知道哪句是昨晚说的、哪句是上午说的，于是把历史里的话照搬成现在——
    实测 `seq 21`（昨晚）「现在快九点半了，早点休息，明天见呀」，到 `seq 36`（20:24）
    又变成「都快九点半了…该吃吃该睡睡」；上午晒的太阳，下午说「才刚晒没一会儿」。
    **不是注入错了，是注入里缺了"这话是什么时候说的"。**
    """
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if not t:
        return ""
    now = now or dt.datetime.fromtimestamp(time.time())
    when = dt.datetime.fromtimestamp(t)
    days = (now.date() - when.date()).days
    hm = f"{when.hour:02d}:{when.minute:02d}"
    if days == 0:
        return f"（今天{clock.day_part(when)} {hm}）"
    if days == 1:
        return f"（昨天 {hm}）"
    return f"（{days} 天前 {hm}）"

# （曾有一个 SCHEMA_VERSION = 2，用来标记「存整轮原文 v1 → 存提炼短句 v2」。**已删除**：
#   全仓没有任何读取点，是纯标记；v1 数据的处理实际在 db.migrate_from_json 与 KIND_TURN。）

# 常量、提示词与默认配置搬到了 memory_defs（#072）——它们纯文本、零依赖、零行为，
# 却占了原文件开头约 300 行。这里**重新导出**，所以
# `from app.models.memory import KIND_WORLD` 这类既有写法完全不受影响。
from app.models.memory_defs import (          # noqa: F401
    EMBED_MAX_CHARS, KIND_FACT, KIND_TURN, KIND_WORLD,
    KIND_CONSTRAINT, PROTECTED_KINDS, EXPLICIT_MARKERS, EXPLICIT_EMPTY,
    RECONCILE_PROMPT, EXTRACT_PROMPT, WORLD_FULL_REVIEW, WORLD_CHECK_PROMPT,
    WORLD_CHECK_BATCH_PROMPT, STATE_CHANGE, GONE_WORDS, SELF_WORDS,
    SUPERSEDE_PROMPT, COMPENSATE_PROMPT, SUMMARIZE_PROMPT, DEFAULT_CFG,
    OPINION_TOP_N,
)
from app.models.memory_opinions import opinion_block


_GONE_NEGATIONS = ("没有再", "没再", "没有", "并未", "未曾", "不曾", "从未", "不再", "不是", "没", "未", "不")


def _gone_word_is_negated(value: str, start: int) -> bool:
    prefix = value[:start]
    for marker in _GONE_NEGATIONS:
        if not prefix.endswith(marker):
            continue
        if marker != "不是" and prefix[:-len(marker)].endswith("不是"):
            return False
        return True
    return False


def _contains_active_gone_word(value: str) -> bool:
    for gone_word in sorted(GONE_WORDS, key=len, reverse=True):
        search_start = 0
        while True:
            start = value.find(gone_word, search_start)
            if start < 0:
                break
            if not _gone_word_is_negated(value, start):
                return True
            search_start = start + 1
    return False


class StateMixin:

    def upsert_slots(self, slots: list[dict]) -> int:
        """写入状态并刷新确认时间，返回值只统计实际发生变化的条数。"""
        n = 0
        now = time.time()
        with self._lock, self.db:
            for s in slots:
                old = self.db.execute(
                    "SELECT value FROM slots WHERE entity=? AND attr=?",
                    (s["entity"], s["attr"])).fetchone()
                is_her_location = (s.get("attr") == "位置" and
                                   s.get("entity") == self.cfg.get("her_name"))
                if old and old["value"] == s["value"]:
                    self.db.execute(
                        "UPDATE slots SET updated_at=? WHERE entity=? AND attr=?",
                        (now, s["entity"], s["attr"]))
                    if is_her_location:
                        self.db.execute(
                            "UPDATE life_state SET place=?,updated_at=MAX(COALESCE(updated_at,0),?) "
                            "WHERE id=1 AND COALESCE(place,'')!=?",
                            (s["value"], now, s["value"]))
                    continue
                self.db.execute(
                    "INSERT INTO slots(entity,attr,value,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(entity,attr) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (s["entity"], s["attr"], s["value"], now))
                n += 1
                if is_her_location:
                    self.db.execute(
                        "UPDATE life_state SET place=?,updated_at=MAX(COALESCE(updated_at,0),?) "
                        "WHERE id=1 AND COALESCE(place,'')!=?",
                        (s["value"], now, s["value"]))
        if n:
            self.event("slots", changed=n)
        return n

    def slots_all(self, limit: int = 12) -> list[dict]:
        """按最后确认时间倒序读取槽位；limit=-1 时读取全部。"""
        return [dict(r) for r in self.db.execute(
            "SELECT entity, attr, value, updated_at FROM slots "
            "ORDER BY updated_at DESC LIMIT ?", (limit,))]

    def render_slots(self, rows: list[dict] | None = None) -> str:
        """Render state rows with their last confirmation time."""
        if rows is None:
            rows = self.slots_all()
        lines = []
        for row in rows or []:
            entity = str(row.get("entity") or "").strip()
            attr = str(row.get("attr") or "").strip()
            value = str(row.get("value") or "").strip()
            if not entity or not attr or not value:
                continue
            try:
                confirmed = dt.datetime.fromtimestamp(float(row.get("updated_at"))).strftime(
                    "%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OverflowError, OSError):
                confirmed = "确认时间未知"
            suffix = f"（最后确认 {confirmed}"
            if attr == "位置":
                suffix += "；这是当时的位置，之后未确认"
            suffix += "）"
            lines.append(f"- {entity}·{attr}：{value}{suffix}")
        return "\n".join(lines)

    def behavior_hints(self, limit: int = 4) -> list[str]:
        """从状态槽位派生的「此刻的分寸」。**纯函数，零模型调用**。

        S05 的教训：召回里明明有「年糕跑丢至今未归」，她还是推荐猫玩具。
        根因不是记忆缺失，是**行为通道缺失**——记忆是叙事（累积、靠召回），
        行为需要的却是当前状态。这里把「某个实体已经不在了」翻译成一条分寸，
        并点明「默认假设已经不成立」。

        分寸为什么只说到「别再按旧状态发起活动」为止：「哪些活动跟它有关」
        是语义判断，交给模型——铁律是**工具给事实，人格给话术**。代码只负责
        指出「状态变了、旧假设失效了」，这正是纯函数能做到且不会说错的部分。

        状态恢复（覆盖式更新）后这条分寸自动消失：它不是手写死的规则。
        """
        out: list[str] = []
        for s in self.slots_all(limit=50):
            ent = str(s.get("entity") or "它").strip() or "它"
            if ent in SELF_WORDS:      # 用户自己不是"不在了"的实体（见 SELF_WORDS 处的实测）
                continue
            value = str(s.get("value") or "")
            if not _contains_active_gone_word(value):
                continue
            out.append(
                f"{ent}现在是「{value}」——它不在原来的位置上了，"
                f"「它还照常在场」这个默认假设已经不成立："
                f"不要提议跟{ent}有关的活动、不要替它做安排、也不要假设它还在。"
                f"他主动问起{ent}时，先把它的现状摆出来，**不要顺着旧情况去安排**"
                f"（他想聊的是这件事本身，不是照旧过日子）。")
        return out[:max(1, int(limit))]

    def timeline(self, limit: int = 40) -> list[dict]:
        """事件时间轴：所有带日期的条目按时间倒序。

        这不是新的一层，只是「event_date 非空的记忆」的投影——
        日期本来就在文本里，入库时已经解析成字段，这里只是排序给人看。
        归档的条目也列出来（带标记）：走丢→找到这种历史正是时间轴的价值所在。

        **按事件组折叠**（`T85` 第 2 步）：同一次互动常被逐轮记成好几条不同措辞
        （"我带了小饼干去赴约"／"助手在见面时带了小饼干"），全列出来就是流水账。
        折叠的规矩是"**只标关系、不合并数据**"：同组只留最新那条在前面，
        其余标 `grouped_under`（渲染时缩进跟随），**底层一条都不少**。
        """
        pools = [(m, True) for m in self.memories] + [(m, False) for m in self.archived]
        out = [{"date": m["event_date"], "text": m["text"],
                "kind": m["kind"], "actor": m.get("actor") or "user",
                "event_group": m.get("event_group") or "",
                "active": active}
               for m, active in pools if m.get("event_date")]
        out.sort(key=lambda x: (x["date"], x["text"]), reverse=True)

        # 折叠：同一组里最新（列表里最靠前）的那条当"代表"，其余标跟随
        seen: dict[str, int] = {}
        for row in out:
            g = row.get("event_group") or ""
            if not g:
                continue
            if g in seen:
                row["grouped_under"] = out[seen[g]]["text"]
            else:
                seen[g] = out.index(row)
        counts: dict[str, int] = {}
        for row in out:
            g = row.get("event_group") or ""
            if g:
                counts[g] = counts.get(g, 0) + 1
        for row in out:
            g = row.get("event_group") or ""
            if g and counts.get(g, 0) > 1:
                row["group_size"] = counts[g]
        return out[:limit]

    def constraints(self) -> list[str]:
        """**现役**硬性约束，按记录顺序。每轮无条件注入，不靠相似度召回。

        已解除的不在这里——它们走 relaxed_constraints()，注入成「已解除」而不是禁令。
        """
        cap = int(self.cfg["max_constraints"] or 0)
        items = [m["text"] for m in self.memories
                 if m.get("kind") == KIND_CONSTRAINT and not m.get("relaxed_at")]
        return items[-cap:] if cap else items

    def relaxed_constraints(self, limit: int = 5) -> list[str]:
        """最近解除的旧禁忌（按解除时间倒序，带上限，免得上下文膨胀）。

        为什么还要提它们：一句「我对花生过敏」被推翻，正确的结果不是"她忘了"，
        而是"她记得以前有这个忌口、现在已经解除"。全忘掉等于抹掉一段共同经历。
        """
        got = [m for m in self.memories
               if m.get("kind") == KIND_CONSTRAINT and m.get("relaxed_at")]
        got.sort(key=lambda m: float(m.get("relaxed_at") or 0), reverse=True)
        return [m["text"] for m in got[:max(1, int(limit))]]

    def relax_constraints(self, texts: list[str]) -> int:
        """把约束标成「已解除」（不删除），返回改动的条数。

        为什么是标记而不是删除：删掉她会完全不记得「以前有这回事」；
        留着又会一直说「你不能吃花生」（S11 的顽固失败，反复出现）。
        第三条路是：**不再是禁令，但仍是她的记忆**——注入时不说「你不能…」，
        而说「这件事已经解除了」。
        """
        want = set(texts)
        now = time.time()
        hit = [m for m in self.memories
               if m.get("text") in want and m.get("kind") == KIND_CONSTRAINT
               and not m.get("relaxed_at")]
        if not hit:
            return 0
        ids = [m["id"] for m in hit if m.get("id")]
        if ids:
            q = ",".join("?" * len(ids))
            with self._lock, self.db:
                self.db.execute(
                    f"UPDATE memories SET relaxed_at=? WHERE id IN ({q})", [now, *ids])
        with self._lock:                    # 内存态同样受锁（与 DB 更新保持同一视界）
            for m in hit:
                m["relaxed_at"] = now
        self.event("relax", n=len(hit), texts=[m["text"][:40] for m in hit])
        return len(hit)

    def upcoming(self, within_days: int = 7, only_today: bool = False,
                 now: dt.datetime | None = None) -> list[tuple[int, dict]]:
        """挑出「今天要做的」和「接下来几天约好的」事，返回 [(距今天数, 条目)]。

        为什么要单独挑而不是靠相似度召回：用户早上打开时说的是「你好」，
        跟「9 月 23 日上午约好一起溜猫」没有任何字面重叠，靠向量必然召不回来——
        跟约束失效是同一类毛病。约定这种事必须按日期主动捞。

        只认真能解析出日期的条目。作息类（每天早上七点跑步）没有具体日期，
        自然不会被当成「今天该催的事」。世界书和约束也不在其中：
        前者是长期背景，后者每一轮都已经无条件注入了。
        """
        now = now or dt.datetime.fromtimestamp(time.time())
        out: list[tuple[int, dict]] = []
        for m in self.memories:
            if m.get("kind") in PROTECTED_KINDS:
                continue
            # 优先用结构化的 event_date 字段（入库时已解析），文本重新解析只做兜底
            d = None
            ed = m.get("event_date")
            if ed:
                try:
                    d = dt.date.fromisoformat(str(ed)[:10])
                except ValueError:
                    d = None
            if d is None:
                d = clock.find_date(m.get("text", ""), now)
            if d is None:
                continue
            days = (d - now.date()).days
            if days < 0:            # 已经过去的约定不再催
                continue
            if only_today and days != 0:
                continue
            if days > within_days:
                continue
            out.append((days, m))
        return sorted(out, key=lambda x: x[0])

    def dead_memories(self, min_age_days: float = 3.0) -> list[dict]:
        """从没被想起过的记忆：占着存储、稀释排序，但一次都没参与过推理。

        只挑「没被命中」这一档。报告里还提了「命中但没影响输出」那一档，
        那需要判断模型有没有采纳，在对话场景里既贵又判不准，所以不做。
        """
        now = time.time()
        out = []
        for m in self.memories:
            if m.get("kind") in PROTECTED_KINDS or m.get("explicit"):
                continue
            if int(m.get("hits", 0)) > 0:
                continue
            if (now - float(m.get("ts") or now)) / 86400.0 >= min_age_days:
                out.append(m)
        return sorted(out, key=lambda m: float(m.get("ts") or 0))


class RecallMixin:

    def recall(self, query: str, top_k: int | None = None,
               kinds: set[str] | None = None,
               vec: np.ndarray | None = None,
               mark_hit: bool = True) -> list[dict]:
        """按语义召回相关的记忆，返回 [{"sim", "text", "kind"}]，相似度降序。"""
        if vec is None:
            vec = self._query_vec(query)          # embedding 调用：在锁外
        if vec is None:
            return []
        top_k = int(top_k if top_k is not None else self.cfg["top_k"])
        threshold = float(self.cfg["threshold"])
        now = time.time()
        out: list[dict] = []
        # 这一段必须整体在锁里：sims 的下标就是 memories 的下标，中途被
        # _drop / _attach_vector 改动就会张冠李戴，甚至直接抛异常
        # （实测并发下 _sims 两次读 self.vectors 拿到不同形状 →
        #   ValueError: operands could not be broadcast (14,) (13,)，召回直接 500）。
        with self._lock:
            sims = self._sims(vec)
            if not len(sims):
                return []
            for i in np.argsort(-sims):
                if len(out) >= top_k:
                    break
                sim = float(sims[i])
                if sim < threshold:
                    break  # 已按相似度降序，后面的只会更低
                m = self.memories[i]
                kind = m.get("kind", KIND_FACT)
                if kinds is not None and kind not in kinds:
                    continue
                if self._is_stale_plan(m):
                    # 过期的约定不再冒充待办：不参与召回抢名额（数据仍在库里，
                    # 时间轴/对账照旧看得到）。**按条目去重地**记进 turn 事件，
                    # 不刷 events 表——同一轮里世界召回与事实召回都会跳过它，
                    # 用计数会翻倍（清点时用两个 recall 的轮次实测到 1 → 2）。
                    self._stale_skipped.add(m.get("id") if m.get("id") is not None else i)
                    continue
                if mark_hit:
                    # 命中计数每轮都在变，但不影响正确性，攒到 save() 时一次落盘，
                    # 免得每条召回都写一次库。（读-改-写在 _mark_hit 里整体受锁）
                    self._mark_hit(int(i), now)
                out.append({"sim": sim, "text": m["text"], "kind": kind,
                            # 弱相似标注：贴着召回线进来的条目多半是字面重叠蹭上的，
                            # 不一定真相关——标注出来让模型自己决定要不要接这个话茬。
                            # 不直接过滤：0.45 是 46 条实测出来的线，拍脑袋收紧会伤别的召回。
                            "weak": sim < threshold + 0.05})
        return out

    def _is_stale_plan(self, m: dict, now: dt.datetime | None = None) -> bool:
        """这条是不是「已经过了期的约定」。

        过期约定最该做的不是提醒，而是**不再冒充待办**：`upcoming()` 早就把
        `days < 0` 的挡掉了（"今天说好的事"里不会出现），但召回层一直没管——
        它仍会和普通记忆抢 `top_k`，还可能被讲成「我们约好明天去广场」（踩过）。

        只判「过期 + 像约定 + 还没完成」三件事**都成立**：
        · 历史事件（「9 月 20 日去了医院」）没有承诺类词 → 不受影响；
        · 还没到的约定（明天见面）→ 照常召回；
        · 已完成的事（「已到达约定的见面地点」「见面时带了小饼干」）→ 照常召回
          ——真实库里就有这种条目，用宽判据会把它们当过期约定挡掉，
          那等于抹掉一段共同经历（这条是拿真实数据试出来的）。
        过滤**不是**让她忘掉：数据留在库里，时间轴、对账照旧看得到。
        """
        if not self.cfg.get("drop_stale_plans", True):
            return False
        if m.get("kind") not in (KIND_FACT, KIND_TURN):
            return False
        text = m.get("text", "")
        # 用 PLAN_COMMIT（承诺/计划）而不是 PLAN_HINT：后者含中性的「见面/碰头」
        if not clock.PLAN_COMMIT.search(text):
            return False
        if clock.PAST_TENSE.search(text):
            return False
        now = now or dt.datetime.fromtimestamp(time.time())
        d = None
        ed = m.get("event_date")
        if ed:
            try:
                d = dt.date.fromisoformat(str(ed)[:10])
            except ValueError:
                d = None
        if d is None:
            d = clock.find_date(text, now)      # 兜底：字段为空时从文本里解析
        return d is not None and d < now.date()

    def _mark_hit(self, i: int, now: float) -> None:
        """记一次命中。**读-改-写必须整体受锁**，否则并发召回会丢计数。

        命中计数影响遗忘权重（`_weight`：命中越多越难被淘汰）。丢一次不会崩，
        但计数错乱会让淘汰挑错人——所以和 vectors 一样收进锁里。
        """
        with self._lock:
            m = self.memories[i]
            m["hits"] = int(m.get("hits", 0)) + 1
            m["last_hit"] = now
            if m.get("id"):
                self._dirty_hits.add(m["id"])

    def build_messages(self, system_prompt: str, query: str,
                       window: int | None = None,
                       top_k: int | None = None) -> tuple[list[dict], list[dict]]:
        """组装送进模型的 messages。**顺序是刻意的，改之前先读下面每块的注释**：

        system（时间/天气/情绪/状态槽位/人设/画像）→ 硬性约束 → 已解除的旧禁忌
        → 此刻的分寸 → 反馈学来的说话偏好 → 今天说好的事（**含明天/后天的约定**）
        → 共同的世界（召回）→ 想起来的事（召回）→ 更早之前（摘要）→ 滑动窗口 → 当前问题

        前六块**不参与召回、无条件在场**，因为它们与当前这句话常常零字面重叠
        （「早上好」和「9月23日约好一起溜猫」没有任何共同词）；只有后三块是语义召回。
        （这段说明以前写的是旧顺序——漏了分寸/倾向/今日约定，还把世界书排在事实前，
         与实现不符；`#94` 的 T69-① 修正。`#98` 把该块从「只有今天」扩成「今天 + 还没到的」，
         并挡掉天气——理由是用户 2026-09-28 的反馈：明天要去书店这件事**一块都没露面**。）
        """
        window = int(window if window is not None else self.cfg["window"])
        top_k = int(top_k if top_k is not None else self.cfg["top_k"])
        # 「本轮挡掉了几条过期待办」从这里开始数：下面的两个 recall 会往里加（按条目
        # 去重），同一轮稍后的 `add_turn` 把条数写进 turn 事件。清零放在这里（而不是
        # add_turn 开头）是因为服务层的顺序是 prepare → finish_turn：在 add_turn 里清零
        # 等于写完就清，`stale` 永远报 0（清点时抓到，台账 T13/T26 正要用这个数）。
        self._stale_skipped = set()
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 约束不参与召回，无条件常驻。理由见 KIND_CONSTRAINT 的注释：
        # 靠相似度碰运气的话，「给我推荐点零食吧」是召不回「我对花生过敏」的。
        cons = self.constraints()
        if cons:
            # 「不要违反」不够（T41 实测）：模型会把它读成"默默照做就行"，于是探针问
            # 「我更喜欢你怎么跟我沟通？」时只答"文字"，把"不要长语音"漏掉
            # （B11：关思考 8/8 漏、开思考 3/8 漏）。约束同时也是**他希望你记得的事**，
            # 所以必须点名"相关时要讲出来"——不说和说错，在对方眼里都叫没记住。
            messages.append({"role": "system", "content":
                "【必须遵守的约束】以下是对方明确提过的硬性要求和禁忌，"
                "每一轮都成立，任何时候都不要违反。**这些同时也是他希望你记得的事**："
                "话题相关时（偏好、习惯、忌口、禁忌类的问题）要把它说出来，"
                "别只是默默照做——不说和说错，在他眼里都是没记住：\n"
                f"{self._bullets(cons)}"})

        # 已解除的旧禁忌：不说清楚，她要么继续顽固地禁止（「你不能吃花生」），
        # 要么显得完全不记得有这回事。明说「已解除」既保住记忆，又撤掉禁令。
        relaxed = self.relaxed_constraints()
        if relaxed:
            messages.append({"role": "system", "content":
                "【已经解除的旧禁忌】下面这些以前是禁忌，用户后来已经解除了。"
                "**不要再当成禁令**（不要说「你不能…」「记得别…」），"
                "但你要记得有过这回事，可以自然地提起来：\n"
                + self._bullets([f"{t}（已解除）" for t in relaxed])})

        # 状态 → 分寸：S05 那种「记忆里明明有、行为上照旧」的失败缺的就是这一步。
        # 它是状态槽位的投影（【当前状态】块在 system_prompt 里，同一个来源）。
        hints = self.behavior_hints()
        if hints:
            messages.append({"role": "system", "content":
                "【此刻的分寸】下面这些状态跟你可能记得的旧情况不一样，"
                "默认假设已经不成立——别按旧情况行事：\n"
                + "\n".join(f"- {h}" for h in hints)})

        # 反馈学来的说话偏好（D31）：**说话方式**层面的东西，所以放在「分寸」之后、
        # 「今天说好的事」之前。框法由 opinion_block() 统一给——实测不能省：
        # 去掉"相关的时候照做／不要点破来源"，误用（把无关倾向硬套）从 0 涨到 4~6。
        picks = self.opinions(limit=OPINION_TOP_N)
        if picks:
            messages.append({"role": "system", "content": opinion_block(picks)})

        # 今天与**明天**的约定单独拎出来常驻。理由和约束一样：「早上好」这句话跟
        # 「9月23日上午约好一起溜猫」没有任何字面重叠，靠相似度必然召不回来，
        # 但它恰恰是最该被主动想起来的东西。
        # 为什么连明天一起给（用户 2026-09-28 反馈"今天早上的事怎么变成了明天的事"）：
        # 原来只给 `only_today`，于是 9-29 的书店之约在 21:25 那轮**一块都没露面**，
        # 她只能靠滑窗编出「我到时候在书店门口等你」。明天的事必须让她看见，
        # 但要标清楚是哪天——否则她会把明天的约定当成今天已经安排好的事去说。
        # 顺手挡掉天气（见 _PLAN_WEATHER 的注释）：那不是"说好的事"。
        upcoming_all = [(days, m) for days, m in self.upcoming(within_days=2)
                        if not _PLAN_WEATHER.search(m.get("text", ""))]
        today = [(days, m) for days, m in upcoming_all if days == 0][:3]
        later = [(days, m) for days, m in upcoming_all if days > 0][:2]
        if today or later:
            lines = []
            if today:
                lines.append("今天的事（日子就在今天，该动身时提醒一下，别每句都念叨）：")
                lines += [f"- {m['text']}" for _, m in today]
            if later:
                lines.append("**还没到**的约定（不是今天，不要当成今天要说的事、"
                             "也不要当成已经安排好的事去承诺）：")
                lines += [f"- {'明天' if days == 1 else f'{days} 天后'}：{m['text']}"
                          for days, m in later]
            messages.append({"role": "system", "content":
                "【今天说好的事】下面这些是对方跟你约好、日子已经定了的事。"
                "今天的可以在该动身时提醒；**还没到的那几条只是背景**——"
                "对方没提起就别主动催，更不要把它的时间说错：\n" + "\n".join(lines)})

        vec = self._query_vec(query)
        world = self.recall(query, top_k=self.cfg["top_k_world"],
                            kinds={KIND_WORLD}, vec=vec) if vec is not None else []
        facts = self.recall(query, top_k=top_k,
                            kinds={KIND_FACT, KIND_TURN}, vec=vec) if vec is not None else []

        if world:
            messages.append({"role": "system", "content":
                "【你和用户共同生活的世界】下面这些是长期成立的背景，当成你知道的常识，"
                f"不要反问、不要重复确认：\n{self._bullets([h['text'] for h in world])}"})
        if facts:
            # 弱相似条目带标注：贴线进来的多半是字面重叠蹭上的，让模型自己权衡
            marked = [h["text"] + ("（弱相关）" if h.get("weak") else "")
                      for h in facts]
            messages.append({"role": "system", "content":
                "【想起来的事】以下是更早之前的对话里跟当前问题相关的内容，仅供参考，"
                "不要当成刚刚说过的话。括号里的时距只给你自己判断用，不要念出来；"
                "标了（弱相关）的条目跟当前话题可能只是字面相似，没头绪就别硬接：\n"
                f"{self._bullets(marked)}"})

        summary = self.summaries.get(self.session, "")
        if summary:
            messages.append({"role": "system", "content":
                f"【更早之前】滑动窗口之前的对话已经压成这段摘要，用它保持连贯：\n{summary}"})

        # 滑窗里的每条历史消息都带时间标记（见 _when_label 的注释）。
        # 不加这一行提示的话，模型会把标记当成消息正文的一部分照念。
        #
        # 末尾那条"自己说过的话要算数"来自用户 2026-09-30 的反馈现场（`feedback.jsonl`
        # 19:23:35，问题就是「好呀好呀」）：她先说"炖好给你**端过去**"，隔一条又说
        # "别提前跑**过来**蹭锅啊"——**30 秒翻案**。查下来现有的一致性机制全是**层与层**
        # 之间的（人格演化 `T47`、世界书/记忆对账），**没有一句管她自己前后是否一致**。
        # 为什么贴在这一块：这里紧接着就是她自己的原文，跟"不要念时间"那条同址；
        # 写法照 `#102` 的教训——**硬禁令 + 具体反例 + 正例**，抽象原则真机压不住。
        messages.append({"role": "system", "content":
            "【对话顺序】下面按时间先后列出来的是**已经说过的话**，每条开头的"
            "「（今天下午 14:05）」「（昨天 21:30）」是它**说出口的时刻**，"
            "只给你判断新旧用：**绝对不要在回答里复述它、也不要照这个格式写时间**"
            "（真调模型实测过：写成「不要念出来」她还是会念，所以改成硬禁令 + 正例："
            "直接说话就行，别在开头报时间）。"
            "要判断现在几点、现在该说早还是晚安，只看最上面的【当前时间】；"
            "这些话里的「明天」「晚上」「快九点半了」都是**当时**说的，不代表现在。\n"
            "**还有一条：你自己刚说过的话要算数。**同一件事前后说法必须一致——你刚说了"
            "「炖好给你端过去」，下一条就不能改成「你过来拿」；二选一，挑定了就别翻。"
            "**这种翻案最伤人**：对方会觉得你根本没在听自己说话。"
            "（真要改主意，就明说「我刚想了一下，还是你过来吧」——那叫改主意，不叫自相矛盾。）"})
        for m in self.sessions.get(self.session, [])[-window * 2:]:
            if m.get("content"):  # 跳过推理耗尽的空回答
                label = _when_label(m.get("ts"))
                messages.append({"role": m["role"],
                                 "content": f"{label}{m['content']}" if label else m["content"]})
        messages.append({"role": "user", "content": query})
        return messages, world + facts

    def check_consistency(self) -> list[dict]:
        """全量对账：找出各层之间互相矛盾的条目。

        **只报告，不改数据。** 自动裁定试过一次，代价是模型把「住在杭州，养了只叫年糕的
        橘猫」整条删了，猫也连坐弄丢——涉及删改的自动化，宁可交给人决定。
        """
        world = self.world_texts()
        # 带上**视角标记**：同一次互动会同时存在"用户侧"和"她的经历侧"两条，
        # 那是 `actor` 机制的正常产物，不是两条互相矛盾的事实。不把标记给模型，
        # 它就会把两侧报成"重复"——实测真库上 21 处里 20 处都是这种噪音，
        # 而前端"记忆体检"红点因此长期亮着。
        facts = [f"【{'她' if m.get('actor') == 'assistant' else '他'}】{m['text']}"
                 for m in self.memories
                 if m.get("kind") in (KIND_FACT, KIND_TURN)]
        cons = self.constraints()
        if not any((world, facts, cons)):
            return []
        timeout = float(self.cfg.get("check_timeout", 20))
        if not math.isfinite(timeout) or timeout <= 0:
            raise MemoryCheckError("记忆体检超时配置必须是有限正数（秒）")
        with_options = getattr(self.client, "with_options", None)
        check_client = (with_options(timeout=timeout, max_retries=0)
                        if callable(with_options) else self.client)
        data, err = chat_json(
            check_client, self.extract_model,
            [{"role": "user", "content": RECONCILE_PROMPT.format(
                world="\n".join(f"- {t}" for t in world) or "（空）",
                facts="\n".join(f"- {t}" for t in facts) or "（空）",
                constraints="\n".join(f"- {t}" for t in cons) or "（空）")}],
            temperature=0.1, max_tokens=1500, retries=0,
            no_think=bool(self.cfg.get("no_think")),
        )
        if data is None:
            log.error("对账检查失败：%s", err)
            raise MemoryCheckError(err)
        raw_issues = data.get("issues")
        if (not isinstance(raw_issues, list)
                or any(not isinstance(item, dict) or not isinstance(item.get("items"), list)
                       for item in raw_issues)):
            raise MemoryCheckError("模型未返回有效的 issues 列表，无法确认检查结果")
        # 原句必须真的存在：模型编一个不存在的句子来"发现矛盾"，是它自己的幻觉。
        # 事实记忆带 `【他】/【她】` 前缀，而模型回抄时**可能带、也可能不带**前缀——
        # 两种都认（否则一开前缀过滤，全部条目都会被当成"编造的"丢掉）。
        known = set(world) | set(cons)
        for t in facts:
            known.add(t)
            known.add(t[3:] if t.startswith(("【他】", "【她】")) else t)
        issues = []
        for it in raw_issues:
            # 原句必须真的存在：模型编一个不存在的句子来"发现矛盾"，是它自己的幻觉
            items = [t for t in (it.get("items") or []) if t in known]
            if len(items) < 2:
                continue
            issues.append({"type": str(it.get("type", "?")), "items": items,
                           "reason": str(it.get("reason", ""))})
        self.event("reconcile", issues=len(issues))
        return issues

    @staticmethod
    def _bullets(texts: list[str]) -> str:
        """每条记忆一行；能认出日期的补一个时距提示。

        时距是替模型省算术的——让它自己算「9月23日离今天几天」很容易错，
        而判断「这件事是过去了还是还没到」正是靠这一步。
        """
        return "\n".join(f"- {clock.describe_when(t)}" for t in texts)

    @staticmethod
    def _explicit_text(user: str) -> str | None:
        """对方明说「记住这个」时，返回要按原话存下来的那段，否则 None。"""
        for marker in EXPLICIT_MARKERS:
            i = user.find(marker)
            if i < 0:
                continue
            rest = user[i + len(marker):].lstrip("：:，,。.、！! ")
            if len(rest) >= 2 and rest not in EXPLICIT_EMPTY:
                return rest
        return None
