"""记忆的**写入路径**：提炼入库（WriteMixin）与世界书（WorldMixin）。

从 ``memory.py`` 拆出来的 Mixin（`#073`）。`add_turn` 是每轮对话的写入入口，
世界书也走同一个 `_add_memories`，所以两者放在同一个文件里。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import os
import threading
import time
from typing import Any, Callable

import numpy as np

from app.models import db as memdb
from app.utils import clock
from app.utils.llmjson import chat_json, strings

from app.utils import answers
from app.utils.location import LOCATION_ATTRS, slot_has_current_evidence
from app.log import get

log = get("memory")

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
    CONSTRAINT_UPDATE_HINT,
    SUPERSEDE_PROMPT, COMPENSATE_PROMPT, SUMMARIZE_PROMPT, DEFAULT_CFG,
)

# 「这条 facts 的主语其实是她」——只认句子开头的主语，不做语义猜测。
# 为什么要这道守卫：提炼器的双方视角只靠**输出字段**区分（`self` 字段 = 她的经历，
# 且要求以「我」开头；`facts` 字段 = 用户侧）。模型一旦把「她的事」写进 `facts`，
# 入库时就被无条件标成 actor="user"——真实库里已经有这样的行（清点时在
# memory.db 读到 `actor='user'` 而文本是「助手喜欢看散文和生活随笔…」）。
# 而 `facts` 正是喂给画像提炼的原料，标错就是往「他是谁」里塞她的属性。
# 守得窄是刻意的：只认句子**以「助手」开头**的那一类。为什么不连「她」一起收——
# 「她」在这个系统里还指用户提到的第三方（「她是我妹妹」），一个字就把用户的事
# 翻成她的经历，比漏判更糟（漏判只是维持现状，还留着 actor_reassigned 事件可回溯）。
# 已知边界（按字面判主语，不做语义猜测）：「沈知夏今天提醒了用户」这类同义写法不会改写；
# 反过来「助手感受…」这种把"感受"当词首的怪句会被算进来。两者都只是标错视角，
# 不改文本、也不影响除时间轴与画像原料之外的通道，所以按"宁可漏、别乱猜"取舍。
_HER_SUBJECT = re.compile(r"^\s*助手(?:\s*(?:今天|昨天|明天|也|又|会|就|已|还|不|没|很))?\s*\S")
# 同一个 scene 标签多久没出现就重新开一组（防止"早饭"把几个月串成一件事）
SCENE_TTL_DAYS = 7


def _norm_scene(scene: str) -> str:
    """场景标签的**确定性**归一：去空白/引号/句末标点/括号补充。

    只做"同一场戏被写成不同格式"这类纯格式归一（`广场晒太阳（今天）` → `广场晒太阳`）。
    **刻意不做词根合并**：`晒太阳` 与 `广场晒太阳` 是不同的字面，谁也不是谁的前缀规则能覆盖的——
    那要靠"让她复用同一个词"（见提示词里的已知场景表），**不靠猜**。
    相似度那条路已被实测否掉（该合 ≥0.709 / 不该合 ≤0.737 重叠）。
    """
    s = str(scene or "").strip()
    s = re.sub(r"[「」『』\"'《》]", "", s)          # 引号
    s = re.sub(r"[（(][^）)]*[）)]", "", s)         # 括号补充
    s = re.sub(r"[。，,、.！!？?；;：:\s]+$", "", s)  # 句末标点
    return s.strip()[:24]


def _split_scene_items(items: list) -> tuple[list[str], dict[str, str]]:
    """`[{"text","event_group"}]` → (文本列表, {文本: 组号})。

    兼容两种输入（`_extract` 现在总是给 dict，但直接调用方可能给字符串）。
    """
    texts: list[str] = []
    mapping: dict[str, str] = {}
    for it in items or []:
        if isinstance(it, dict):
            text = str(it.get("text") or "").strip()
            grp = str(it.get("event_group") or "").strip()
        else:
            text, grp = str(it or "").strip(), ""
        if not text:
            continue
        texts.append(text)
        if grp:
            mapping[text] = grp
    return texts, mapping


def _looks_like_her_own(text: str) -> bool:
    """以「助手…」开头的条目算她的视角（见上面那段理由）。"""
    return bool(_HER_SUBJECT.match(text or ""))


def _resolve_actor(text: str, actor: str) -> str:
    """入库前定这条记忆的视角：默认用调用方给的，主语是她的就翻成 assistant。"""
    if actor == "user" and _looks_like_her_own(text):
        return "assistant"
    return actor


class WriteMixin:

    @property
    def _continuity_payload(self) -> dict:
        with self._lock:
            local = getattr(self, "_continuity_thread", None)
            if local is None:
                local = threading.local()
                self._continuity_thread = local
        if not hasattr(local, "payload"):
            local.payload = {"updates": [], "source": None, "input_user": ""}
        return local.payload

    @property
    def _continuity_updates(self) -> list:
        return self._continuity_payload["updates"]

    @_continuity_updates.setter
    def _continuity_updates(self, value):
        self._continuity_payload["updates"] = value

    @property
    def _continuity_source(self):
        return self._continuity_payload["source"]

    @_continuity_source.setter
    def _continuity_source(self, value):
        self._continuity_payload["source"] = value

    @property
    def _continuity_input_user(self) -> str:
        return self._continuity_payload["input_user"]

    @_continuity_input_user.setter
    def _continuity_input_user(self, value):
        self._continuity_payload["input_user"] = value

    def _group_by_original_kind(self, texts: list[str],
                                kinds: dict[str, str]) -> dict[str, list[str]]:
        """按**条目原来的 kind** 把要写回的文本分组（`B9-④`，工单二）。

        补偿捡回的条目，原来只要"这批里有一条是世界书"，就**全部**按世界书写回（混批被贴错
        标签）→ 普通事实永久变成世界书：常驻注入、不再参与冷却淘汰。
        """
        out: dict[str, list[str]] = {}
        for text in texts:
            kind = kinds.get(text) or KIND_FACT
            out.setdefault(kind, []).append(text)
        return out

    def add_turn(self, user: str, assistant: str) -> str:
        """记一轮对话，返回一句给用户的备忘（没有新东西就返回空串）。"""
        self._llm_calls = 0           # 本轮成本计数：给 turn 事件用（见 __init__）
        self._embed_calls = 0
        self._continuity_updates = []
        self._continuity_input_user = str(user or "")[:1000]
        self._continuity_source = None
        # `_stale_skipped`（本轮召回挡掉了几条过期待办）**有意不在这里清零**：
        # 累加发生在同一轮更早的 prepare 阶段（`build_messages → recall`），清零放在那里；
        # 这里若清零，turn 事件报出来的 `stale` 就恒为 0 —— 而台账 T13/T26 正要靠
        # 这个字段在真实语料里量识别率（清点时发现该字段从来没进过库）。
        now = time.time()
        # `C2`（工单三）：**存储层不变量——空回复不构成一轮**。
        # 先剥掉开头的内部时间标记（`T83` 的口径：只剥开头、必须留痕），再判空：
        # `finish_turn`（流式那条）是直接把原文交给这里的，所以"整条只有「（今天下午 13:54）」"
        # 的回复曾把标记原文写进记忆、还留一条空 assistant 轮。守在这里，所有调用点自动正确。
        raw_assistant = str(assistant or "")
        cleaned, did_strip = answers.strip_time_prefix(raw_assistant)
        if did_strip:
            self.event("answer_prefix_stripped", where="add_turn", raw=raw_assistant[:40])
        empty = not cleaned.strip()
        with self._lock:              # 与并发的 save() 遍历划清视界
            history = self.sessions.setdefault(self.session, [])
            # 用户那句照记（他确实说了）；**空的 assistant 轮一条都不留**
            history.append({"role": "user", "content": user, "ts": now})
            if not empty:
                history.append({"role": "assistant", "content": cleaned, "ts": now})
                self._continuity_source = {
                    "session": self.session,
                    "user_seq": len(history) - 2,
                    "assistant_seq": len(history) - 1,
                    "user_text": str(user or ""),
                    "assistant_text": cleaned,
                    "source_ts": now,
                }
        if empty:
            log.info("add_turn：assistant 为空（剥掉时间标记后仍为空）→ 不追加这一轮：%r",
                     raw_assistant[:40])
            self.event("turn_empty_skipped", question=str(user or "")[:60])

        assistant = cleaned
        if self.cfg["summarize_every"]:
            self._maybe_summarize()
        if empty:
            return ""

        turn = len(history) // 2

        if not self.cfg["auto_extract"]:
            added, dup = self._add_memories(
                [f"Q: {user}\nA: {assistant[:800]}"], KIND_TURN)
            return self._note(len(added), dup, 0)

        every = max(1, int(self.cfg["extract_every"] or 1))
        if turn % every:
            return ""

        # 显式通道：对方明说「记住这个」就按原话存，不经过提炼器的取舍。
        # 提炼器会判断「值不值得记」，而明说的意思是「必须记」，两者不是一个决定。
        explicit_note = ""
        forced = self._explicit_text(user)
        if forced:
            got, _ = self._add_memories([forced], KIND_FACT, explicit=True)
            if got:
                explicit_note = "按你要求记住 1 条"

        facts, worlds, cons, drops, selfs, slots = self._extract(user, assistant)
        if self._continuity_source:
            self._continuity_source["user_text"] = self._continuity_input_user
        if facts is None:  # 提炼失败
            return explicit_note

        # 作废前先记下它们原本是什么类型，补偿回来的条目要沿用同样的类型
        drop_kinds = {m["text"]: m.get("kind", KIND_FACT)
                      for m in self.memories if m.get("text") in set(drops)}
        dropped = self.drop_memories(drops) if drops else 0
        # 四类文本**一次** embedding 拿回来再分发：原来按 kind 分别调 _add_memories，
        # 每个非空 kind 就是一次独立的网络往返（embedding 天生支持批量，纯浪费）。
        # `facts` 现在是 `[{"text","event_group"}]`（见 `_extract`）：这里拆成
        # 文本列表（给 embedding/写入用）与"文本→组号"（给折叠用）。
        facts, fact_groups = _split_scene_items(facts)
        groups = [(facts, KIND_FACT, "user"), (worlds, KIND_WORLD, "user"),
                  (cons, KIND_CONSTRAINT, "user"), (selfs, KIND_FACT, "assistant")]
        flat = [t.strip() for texts_, _, _ in groups for t in texts_ if t and t.strip()]
        all_vecs = self._embed(flat) if flat else []
        if flat and (all_vecs is None or len(all_vecs) != len(flat)):
            # **整体**失败（`_embed` 一条都没成功）→ 整轮不写：要么都写、要么都不写。
            # "逐类各自失败各自跳过"会留下半截状态（facts 进去了、world 没进去），
            # 那正是本项目最忌讳的那种不一致；而且失败必须可见（事件 + ERROR 日志）。
            # ⚠ 但**不能丢**：这些是对方真实说过的话，全部转入 pending 等下次补算
            # （用户 2026-09-29："别超过 20 条就全体丢失"）。
            log.error("批量 embedding 全部失败，本轮不写入记忆；%d 条转入 pending 等补算",
                      len(flat))
            for texts_, kind_, actor_ in groups:
                missing = [t.strip() for t in texts_ if t and t.strip()]
                if missing:
                    self._park_pending(missing, kind_, actor=actor_)
            self.event("embed_fail", n=len(flat), source="turn")
            return explicit_note
        # **部分**失败不用在这里特判：`_embed` 分片后会把失败的条目返回成 `None`，
        # 由 `_add_memories` 按位置过滤（成功的写入、失败的进 pending）。
        # 早先我在这里塞过 `np.zeros` 占位——**那是错的**：零向量会让"最相似的记忆"
        # 变成一条垃圾，比不写还糟。
        pos = 0

        def _take(texts_: list[str]) -> list:
            """按 groups 的顺序切片——顺序必须与上面拼接 flat 的顺序一致。"""
            nonlocal pos
            n = len([t for t in texts_ if t and t.strip()])
            got = all_vecs[pos:pos + n]
            pos += n
            return got

        added, dup = self._add_memories(facts, KIND_FACT, vecs=_take(facts),
                                       event_groups=fact_groups)
        w_added, _ = self._add_memories(worlds, KIND_WORLD, vecs=_take(worlds))
        c_added, _ = self._add_memories(cons, KIND_CONSTRAINT, vecs=_take(cons))
        # 她自己的经历：同样进时间轴和召回池，标 actor=assistant。
        # 这是「她是个有历史的人」的地基——没有自己的过去，人设就只是一份说话规范。
        s_added, _ = self._add_memories(selfs, KIND_FACT, actor="assistant",
                                        vecs=_take(selfs))
        # 状态槽位：覆盖式更新「现在是什么样」。和事件记忆分工——
        # 事件进时间轴，状态进槽位常驻，她不再靠召回碰运气知道年糕在哪。
        slot_n = self.upsert_slots(slots)

        kept = []
        if dropped:
            # 兜底：模型经常整条作废，把捆在同一条里的其他信息一起抹掉。
            # 这里问一次「旧条目里还有哪些仍然成立」，捡回来。
            back = self._compensate(drops, facts + worlds + cons)
            if back:
                # `B9-④`（工单二）：按**原来的 kind** 分组写回，而不是"有一条世界书就全按
                # 世界书"——混批被贴错标签会让普通事实永久变成世界书。
                kept = []
                for kkind, group in self._group_by_original_kind(back, drop_kinds).items():
                    got, _dup = self._add_memories(group, kkind)
                    kept.extend(got)
                    if kkind == KIND_WORLD and got and self.on_world:
                        self.on_world(got)
                self.event("compensate", asked=len(back), kept=len(kept),
                           source="extract")

        # 第二道兜底：出现状态变化词，或本轮新增约束时，主动追问一次
        # 「哪些旧记忆过期了」。约束改写不一定会说出固定的放宽词。
        # 实测（t_a04_rate.py）：「公司搬到滨江」4 次里 0 次触发对「公司在城西」的作废——
        # 新事实被写成「已从城西搬到滨江」，模型把它当过渡叙述而不输出 drop。
        #
        # 三个设计要点，都是踩出来的：
        # 1. 条件不是「提炼器没作废过」：它可能作废了别的条目、唯独漏掉真该作废的那条。
        # 2. 三类新内容都要看：约束类更新（S11 放宽约束）走的是 cons 不是 facts；
        #    世界书更新（S05 年糕不在了）走的是 worlds。
        # 3. **用户原话也要看**：有时提炼器一条都没输出（只是句陈述），
        #    这时只有原话里的「年糕不在了」能当触发信号，否则旧世界书永远清不掉。
        #
        # _find_superseded 只返回活跃集里确实存在的原文，所以已经作废过的不会重复触发。
        new_bits = facts + worlds + cons
        # 约束更新本身就是低频且语义明确的状态变化：即使用户没有说出
        # 「可以吃了／不用忌了」这类词，新约束也可能只是把旧限制改写得更宽。
        # 只有本轮确实新增了约束、且库里还有别的现役约束时才探查，避免首条约束
        # 和重复约束白白增加一次模型调用。
        added_constraints = set(c_added)
        has_prior_active_constraint = any(
            m.get("kind") == KIND_CONSTRAINT
            and not m.get("relaxed_at")
            and m.get("text") not in added_constraints
            for m in self.memories
        )
        probe_texts = []
        for text in [user, *new_bits]:
            if text and text not in probe_texts:
                probe_texts.append(text)
        state_change = any(STATE_CHANGE.search(t) for t in probe_texts)
        constraint_update = has_prior_active_constraint and any(
            CONSTRAINT_UPDATE_HINT.search(t) for t in probe_texts)
        active_constraint_update = bool(c_added and has_prior_active_constraint)
        if state_change or active_constraint_update or constraint_update:
            stale = self._find_superseded(probe_texts or [user])
            if stale:
                self.event("superseded", hit=len(stale))
                kinds2 = {m["text"]: m.get("kind", KIND_FACT)
                          for m in self.memories if m.get("text") in set(stale)}
                n2 = self.drop_memories(stale)
                if n2:
                    dropped += n2
                    back2 = self._compensate(stale, facts + worlds + cons)
                    if back2:
                        kkind2 = (KIND_WORLD if KIND_WORLD in kinds2.values() else KIND_FACT)
                        kept2, _ = self._add_memories(back2, kkind2)
                        self.event("compensate", asked=len(back2), kept=len(kept2),
                                   source="superseded")
                        if kkind2 == KIND_WORLD and kept2 and self.on_world:
                            self.on_world(kept2)
                        kept = kept + kept2

        if w_added and self.on_world:
            self.on_world(w_added)  # 同步回 world.json，保证它是手工可编辑的唯一来源

        parts = [explicit_note, self._note(len(added), dup, len(w_added))]
        if c_added:
            parts.append(f"约束 +{len(c_added)} 条")
        if slot_n:
            parts.append(f"状态更新 {slot_n} 条")
        if dropped:
            parts.append(f"作废旧记忆 {dropped} 条")
        if kept:
            parts.append(f"捡回被连坐的 {len(kept)} 条")
        # 一轮的完整账：观测「提炼产出多少、作废多少、补偿捡回多少」全靠它
        self.event("turn", facts=len(added), world=len(w_added), cons=len(c_added),
                   selfs=len(s_added), slots=slot_n, dup=dup,
                   dropped=dropped, kept=len(kept),
                   # 本轮成本：llm = 模型步骤数，embed = embedding 往返次数。
                   # 这两个数就是「要不要上作业队列」的判据（T3 第 4 步）。
                   llm=self._llm_calls, embed=self._embed_calls,
                   # 本轮召回里挡掉了几条过期待办（见 _is_stale_plan，按条目去重）
                   stale=len(self._stale_skipped))
        return "、".join(p for p in parts if p)

    @staticmethod
    def _note(added: int, dup: int, worlds: int) -> str:
        parts = []
        if added:
            parts.append(f"记住 {added} 条")
        if worlds:
            parts.append(f"世界书 +{worlds} 条")
        if dup:
            parts.append(f"重复 {dup} 条已跳过")
        return "、".join(parts)

    def _extract(self, user: str, assistant: str) -> tuple[
            list[str] | None, list[str], list[str], list[str],
            list[str], list[dict]]:
        """提炼一轮对话，返回 (facts, world, constraints, drop, selfs, slots)。

        **失败分支必须返回同样 6 个位置**：调用方（add_turn）是按 6 元组解包的，
        少一个就是 ValueError。踩过：失败分支只写了 4 个值，于是「提炼失败」
        这条本来就该被容忍的路径，变成整轮对话崩掉、SSE 收尾断掉。失败是常态
        （网络抖动、超时、限流、模型输出不是合法 JSON），所以这里必须对齐。

        喂进去两样东西：
        - 已有世界书条目，让它自己避开重复（写完再去重永远慢一步，
          「换个说法说同一件事」也很难用相似度卡死）；
        - 跟这一轮最相关的旧记忆，这样它才有机会发现「这条已经被推翻了」。
          只给相关的几条，不是全量，省 token 也少干扰。
        """
        self._continuity_updates = []
        self._continuity_input_user = str(user or "")[:1000]
        # 世界书和约束一起给模型看：两者都是「长期设定」，模型判断是否重复时要看到全部
        existing = self.world_texts() + self.constraints()
        world_block = "\n".join(f"- {t}" for t in existing) if existing else "（还是空的）"
        try:
            related_hits = self.recall(user, top_k=5,
                                       kinds={KIND_FACT, KIND_TURN}, mark_hit=False)
        except Exception as exc:
            # `B9-③`（工单二）：**降级必须可见**。原来 `except Exception: []` 静默吞掉——
            # 提炼照跑，但"给模型看的旧记忆"悄悄变成空，日志里一个字都没有。
            related_hits = []
            log.warning("提炼前召回旧记忆失败（这次不带旧记忆给模型）：%s: %s",
                        type(exc).__name__, exc)
            self.event("extract_recall_failed", error=type(exc).__name__)
        related_block = ("\n".join(f"- {h['text']}" for h in related_hits)
                         if related_hits else "（没有相关的旧记忆）")
        try:
            all_slots = self.slots_all(limit=-1)
            source_text = f"{user[:1000]}\n{assistant[:1500]}"
            participants = {"用户", str(self.cfg.get("her_name") or "她")}
            relevant = [s for s in all_slots if s["entity"] in participants
                        or (s["entity"] and s["entity"] in source_text)]
            remaining = [s for s in all_slots if s not in relevant]
            slot_rows = relevant + remaining[:max(0, 12 - len(relevant))]
            slots_block = self.render_slots(slot_rows) if slot_rows else "（还没有）"
        except Exception as exc:
            slots_block = "（读取失败，本轮不带状态槽位）"
            log.warning("提炼前读取状态槽位失败（本轮不带状态）：%s: %s",
                        type(exc).__name__, exc)
        # 归档的条目不参与召回，所以模型看不到它们。不告诉它一声，
        # 它会把同一个已经被推翻的事实重新写回来——归档反而制造了「复活式增长」。
        gone = self.recent_archived(5)
        gone_block = "\n".join(f"- {t}" for t in gone) if gone else "（没有）"

        # 「她已经用过的场景词表」（`T85` 标签归一化）：把已知场景喂回去，**明令优先复用**。
        # 为什么不是事后归一：相似度这条路已被实测否掉（该合 ≥0.709 / 不该合 ≤0.737 重叠），
        # `广场晒太阳` 与 `广场` 事后没法安全地认成一场戏——所以要让它**在产生时就统一**。
        # 只喂最近 7 天用过的（与 `_group_for` 的 TTL 一致），免得提示词被历史场景淹没。
        now_ts = time.time()
        live_scenes = [s for s, v in self._scene_groups.items()
                       if now_ts - float(v.get("ts") or 0) <= SCENE_TTL_DAYS * 86400]
        scene_block = ("、".join(live_scenes[-12:]) if live_scenes else "（还没有）")

        # 「她用名字」：槽位的 entity 曾经写成「助手」，而她已经叫沈知夏了
        # （用户 2026-09-29 的反馈直接点了这条）。名字由上层经 cfg 传进来，
        # Model 层不自己去读人设文件——它不该知道自己被谁调用。
        prompt = EXTRACT_PROMPT.format(world=world_block, related=related_block,
                                        slots=slots_block,
                                       archived=gone_block, now=clock.now_text(),
                                       her=str(self.cfg.get("her_name") or "她"),
                                       scenes=scene_block,
                                       user=user[:1000], assistant=assistant[:1500])
        try:
            continuity = getattr(self, "continuity", None)
            rows = continuity.items() if continuity is not None else []
            current_text = (self._continuity_input_user + "\n" + str(assistant or "")[:1500]).casefold()
            related_topics = [
                row for row in rows if row.get("source_valid") and str(row.get("topic") or "")
                and (str(row["topic"]).casefold() in current_text or (
                    row.get("actor") == "assistant" and row.get("status") == "open"
                    and row.get("state") in {"intention", "decision"}
                    and (row.get("expires_at") is None or float(row["expires_at"]) > now_ts)))
            ]
            related_topics.sort(key=lambda row: (
                str(row["topic"]).casefold() in current_text,
                float(row.get("updated_at") or 0)), reverse=True)
            known_topics = [{key: row.get(key) for key in (
                "id", "topic", "status", "actor", "state", "quote")}
                for row in related_topics[:continuity.context_limit]] if continuity else []
            prompt += "\n【已知未完话题（只能使用这些 id 更新）】\n" + json.dumps(
                known_topics, ensure_ascii=False)
        except Exception as exc:
            log.warning("提炼前读取话题连续性失败（本轮不带话题表）：%s: %s",
                        type(exc).__name__, exc)
            self.event("continuity_context_failed", error=type(exc).__name__)
        self._llm_calls += 1
        data, err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=1500,
            retries=1,  # 提炼失败等于这轮白聊，值得再试一次
            no_think=bool(self.cfg.get("no_think")),
        )
        if data is None:
            log.warning("提炼失败：%s", err)
            self.event("extract_fail", error=str(err)[:120])
            # 六个位置一个都不能少：调用方按 6 元组解包，缺一个就 ValueError。
            return None, [], [], [], [], []
        raw_continuity = data.get("continuity")
        if isinstance(raw_continuity, list):
            self._continuity_updates = [item for item in raw_continuity[:3]
                                        if isinstance(item, dict)]
        cap = int(self.cfg["max_items_per_turn"] or 3)
        # facts 现在可以是 `[{"text":..., "scene":...}]`，也可以是老写法 `["..."]`。
        # 两种都收（旧数据与旧提示词不能破），统一整理成 (文本, 事件组号) 的列表。
        facts: list[dict] = []
        for item in (data.get("facts") or [])[:cap]:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                scene = str(item.get("scene") or "").strip()
            else:
                text, scene = str(item or "").strip(), ""
            if not text:
                continue
            facts.append({"text": text, "event_group": self._group_for(scene)})
        worlds = strings(data, "world", "lore", "background")[:cap]
        cons = strings(data, "constraints", "constraint", "rules")[:cap]
        # 她自己的经历：只认第一人称（「我…」）的条目，防模型把用户的事混进来
        selfs = [t for t in strings(data, "self", "assistant")[:cap]
                 if t.startswith("我")]
        # 状态槽位：三键齐全才收，防残缺对象污染槽位表
        slots = []
        her_name = str(self.cfg.get("her_name") or "她")
        for s in (data.get("slots") or [])[:cap]:
            if not isinstance(s, dict):
                continue
            ent, attr, val = (str(s.get("entity", "")).strip(),
                              str(s.get("attr", "")).strip(),
                              str(s.get("value", "")).strip())
            if ent and attr and val:
                if (ent == her_name and attr in LOCATION_ATTRS and not slot_has_current_evidence(
                        assistant, value=val,
                        quote=str(s.get("quote") or ""),
                        source=str(s.get("source") or ""))):
                    continue
                if ent == her_name and attr in LOCATION_ATTRS:
                    attr = "位置"
                slots.append({"entity": ent, "attr": attr, "value": val})
        # 只认确实存在的原文，防止模型编一条不存在的记录来"删"
        known = {m.get("text", "") for m in self.memories}
        drops = [t for t in strings(data, "drop", "world_drop", "fact_drop")[:cap]
                 if t in known]
        return facts, worlds, cons, drops, selfs, slots

    def _group_for(self, scene: str) -> str:
        """scene 标签 → 事件组号（`T85` 第 2 步）。空标签返回空串（调用方退回"同一轮"）。

        三条规矩：
        · 同一个场景 → 同一个组号（**这就是跨轮复述能归组的原因**）；
        · 场景之间绝不共号；
        · **超过 `SCENE_TTL_DAYS` 天没再出现的场景，重新开一组**——否则"早饭"这个标签
          会把三个月里每天的早饭串成一件事，那比不合并更糟。
        """
        s = _norm_scene(scene)
        if not s:
            return ""
        now = time.time()
        got = self._scene_groups.get(s)
        if got and now - float(got.get("ts") or 0) <= SCENE_TTL_DAYS * 86400:
            got["ts"] = now
            return got["group"]
        group = f"scene:{s}:{int(now)}"
        self._scene_groups[s] = {"group": group, "ts": now}
        return group

    def _park_pending(self, texts: list[str], kind: str,
                      explicit: bool = False, actor: str = "user") -> None:
        """把"暂时算不出向量"的条目登记进 `pending`（`memories` 行 + 内存 + **pending 表**），
        等下次补算。

        为什么不是直接丢：这些是**用户真实说过的事**，因为一次接口抖动丢掉它们，
        等于"她忘了你刚说的话"。`pending` 本来就有出口（启动时 `_load` 会读它、
        `_recover_missing_vectors` 补算向量），所以这里是它们该待的地方。

        **pending 表在这里就写**，不等 `save()`：`save()` 只在 `finish_turn` / 命令里调，
        而这条路随时可能因为进程退出而没走到那一步——只写内存等于"抖动一次就永久丢"。
        表是 `save()` 全量重写的那份（DELETE + INSERT），这里先落一条不影响它。
        """
        now = time.time()
        for text in texts:
            entry = {"text": text, "kind": kind, "ts": now, "session": self.session,
                     "hits": 0, "last_hit": 0, "actor": actor,
                     "event_date": None, "relaxed_at": None}
            if explicit:
                entry["explicit"] = True
            try:
                self._insert_memory(entry, None)      # memories 行（vec 为空）+ 内存
                self.pending.append(self._to_mem(entry))
                with self._lock, self.db:
                    self.db.execute("INSERT INTO pending(text,kind) VALUES(?,?)", (text, kind))
            except Exception as exc:
                log.exception("pending 登记失败（这条文本只剩日志了）：%s: %s",
                              type(exc).__name__, exc)

    def _add_memories(self, texts: list[str], kind: str,
                      explicit: bool = False, actor: str = "user",
                      vecs: list | None = None,
                      event_groups: dict | None = None) -> tuple[list[str], int]:
        """批量写入一批记忆。返回 (真正新增的文本, 判定重复的数量)。

        explicit=True 表示这是对方明说「记住」的内容：不参与容量淘汰。
        actor 标这条记忆是谁的：user（默认）= 用户侧事实，
        assistant = 她自己的经历（时间轴和「她是有历史的人」的地基）。
        vecs 可选：调用方已经算好向量时传进来——一轮里多个 kind 可以**一次**
        批量 embedding 再分发（见 add_turn），不传就自己算。
        """
        texts = [t.strip() for t in texts if t and t.strip()]
        if not texts:
            return [], 0
        if vecs is None:
            vecs = self._embed(texts)
        if vecs is None or len(vecs) != len(texts):
            # 走不到这里的兜底：现在是"整体失败"（`_embed` 一条都没成功）或长度错乱。
            # **不能丢**：这些是对方真实说过的话，全部登记进 `pending`，
            # 下次启动由 `_recover_missing_vectors` 补算向量救回来
            # （用户 2026-09-29："别超过 20 条就全体丢失"）。
            log.error("embedding 整体失败，%d 条转入 pending 等补算（不丢）", len(texts))
            self._park_pending(texts, kind, explicit, actor)
            self.event("embed_fail", n=len(texts), source="all")
            return [], 0
        # 分片之后"部分失败"是可能的（某片两次都没成）：
        # 成功的照写、失败的进 pending——**片与片之间不许连坐**。
        if any(v is None for v in vecs):
            bad = [t for t, v in zip(texts, vecs) if v is None]
            log.warning("embedding 有 %d 条没成功，转入 pending 等补算：%s",
                        len(bad), "；".join(t[:20] for t in bad[:3]))
            self._park_pending(bad, kind, explicit, actor)
            self.event("embed_fail", n=len(bad), source="partial")
            keep = [(t, v) for t, v in zip(texts, vecs) if v is not None]
            texts = [t for t, _ in keep]
            vecs = [v for _, v in keep]

        now = time.time()
        added: list[str] = []
        dup = 0
        world_conflicts: list[str] = []   # 被新条目推翻的旧世界书条目，等新条目落库后再作废
        # 世界书：一轮多条新设定**一次**问完（T3 第 2 步）。
        # 依据：实测逐条 vs 批量，verdict/stale 8/8 一致（#023）；原来每条一次往返。
        # 只有一条时不走批量——单条 prompt 更省 token，也没必要换形状。
        batch_verdicts: dict[str, tuple[str, list[str]]] = {}
        if kind == KIND_WORLD and len(texts) > 1:
            batch_verdicts = self._world_check_batch(texts, vecs)
        for text, vec in zip(texts, vecs):
            # 时刻级写入哨兵：完成时句式 + 今天的未来时刻 = 日期锚不可能。
            # 踩过：19:50 写入「…晚上九点半也没有再疼」，九点半当时还没到——
            # 用户说的只能是昨晚，回拨到昨天是唯一合理解读（规则确定，不用猜）。
            fixed = clock.fix_impossible_time(text, dt.datetime.fromtimestamp(now))
            if fixed != text:
                log.warning("[时刻哨兵] 完成时句式指向未来时刻，日期已回拨：%s", text[:36])
                text = fixed
            arr = np.asarray([vec], dtype=np.float32)
            # `B4`（工单二）：判定用的**下标**和它索引的 `self.memories` 必须同一视界——
            # 只锁 `_sims` 不够：它返回后我们还要再读一次 `self.memories`，那一次读同样会
            # 与并发的 attach/drop 错位。所以在锁内一次取齐（快照），模型调用仍在锁外。
            with self._lock:
                vectors = self.vectors
                sims = self._sims(arr[0]) if vectors is not None and len(vectors) else None
                memories = list(self.memories)
            # `C1`（工单三）：`_sims` 回来时向量库可能已经变短——**空数组不是 None**，
            # 直接 `np.argmax` 会抛 "attempt to get argmax of an empty sequence"。
            if sims is not None and len(sims):
                j = int(np.argmax(sims))
                if j < len(memories) and sims[j] >= float(self.cfg["dedup_sim"]):
                    hit = memories[j]
                    # 世界书和约束是更强的身份：普通记忆被它们覆盖时就地转正。
                    # 少了这一步，以前记成事实的过敏禁忌就永远升不上来——
                    # 用户原样再说一遍也只会被判成重复丢掉，常驻注入永远不生效。
                    if kind in PROTECTED_KINDS and hit.get("kind") not in PROTECTED_KINDS:
                        hit["kind"] = kind
                        # 文本也一起换成新的。只改 kind 不改文本的话，
                        # world.json 存的是新写法、向量库里还是旧写法，下次启动 sync
                        # 会把旧文本当成新条目再加一遍，白白多出一条重复。
                        hit["text"] = text
                        hit["ts"] = now
                        with self._lock:              # 就地改向量同样要受锁
                            self.vectors[j] = arr[0]
                        # 光改内存不够：必须立刻落库（T67-①），否则上面那条注释描述的
                        # 重复问题只是被推迟到"重启之后"发生。
                        self._update_memory(hit, arr[0])
                        added.append(text)
                    else:
                        hit["ts"] = now
                        # `B9-②`（工单二）：普通重复也要**落库**——只改内存的话重启后 ts 回退，
                        # 时间轴与遗忘权重都按旧值算（受保护的那条分支早就落了库）。
                        self._update_memory(hit)
                    dup += 1
                    continue
                # 世界书另查一道：0.92 那道太松，同义改写会漏进来；
                # 而且矛盾（同一主题的互斥结论）在字数上可能毫无重叠，
                # 纯相似度永远拦不住，只能靠这一步语义判断 + 作废旧条目。
                if kind == KIND_WORLD:
                    if text in batch_verdicts:
                        # 批量判过的直接用（一轮多条时只调过一次模型）
                        verdict, conflicts = batch_verdicts[text]
                    else:
                        # 单条，或批量里漏答了这条 → 回落到单条判定（多花一次但正确）
                        verdict, conflicts = self._world_check(text, arr[0])
                    self.event("world_verdict", verdict=verdict,
                               conflicts=len(conflicts), text=text[:60])
                    if verdict == "duplicate":
                        dup += 1
                        continue
                    if verdict == "contradict":
                        world_conflicts.extend(conflicts)
                # 注意：这里**不再**直接改 self.vectors —— 上面可能刚调过模型，
                # 而锁不能跨越模型调用。成对写入统一走 _attach_vector()。
            # 事件时间轴：入库时把日期解析成结构化字段。
            # 提炼器已经把「明天」换算成绝对日期，这里只是把结果钉住，
            # 不增加任何模型调用。解析不出（吃了一碗饭）就是 None，不硬造。
            _ed = clock.find_date(text, dt.datetime.fromtimestamp(now))
            _actor = _resolve_actor(text, actor)
            if _actor != actor:
                # 改写要留痕：静默改写会让人以为"模型标对了"，下次清点又当成新发现。
                log.warning("[视角守卫] facts 里出现了她的经历，已改标 actor=%s：%s",
                            _actor, text[:36])
                self.event("actor_reassigned", text=text[:60], was=actor, now=_actor)
            entry = {"text": text, "kind": kind, "ts": now,
                     "session": self.session, "hits": 0, "last_hit": 0,
                     "actor": _actor,
                     # 事件组（`T85` 第 2 步）：同一次互动的条目共享一个组号，
                     # 时间轴/统计按它折叠。没给 scene 的条目退回"同一轮"（用本轮 ts）。
                     "event_group": str((event_groups or {}).get(text)
                                        or f"turn:{int(now)}"),
                     "event_date": _ed.isoformat() if _ed else None}
            if explicit:
                entry["explicit"] = True
            self._attach_vector(entry, arr[0])   # 成对写入：向量与元数据一起进
            added.append(text)

        self._prune()
        # 新条目已经落库了，才去作废被它推翻的旧条目——顺序反了会出现
        # 「旧条目没了、新条目也没写进去」的空窗。
        if world_conflicts:
            n = self.drop_memories(world_conflicts, reason="world-conflict")
            if n:
                # 世界书是「可手工编辑的唯一真相」，自动作废必须让人知道发生了什么
                log.info("新设定与 %d 条旧设定矛盾，已作废旧条目：%s",
                      n, '；'.join(t[:24] for t in world_conflicts))
        return added, dup

    def drop_memories(self, texts: list[str], archive: bool = True,
                      reason: str = "superseded") -> int:
        """按原文把记忆移出活跃集（事实和世界书都行），并回调同步删掉 world.json 里的。

        默认是**软删（归档）**：对话里判定被推翻的条目要留档，
        否则捆在同一条里的其他信息会跟着消失。用户明确要求删除时传 archive=False。
        """
        want = set(texts)
        # 约束走「解除」而不是「作废归档」——在这里分流，所有调用方自动获得正确
        # 行为（提炼器的 drop、兜底追问、世界书矛盾、手动删除，全都一样）。
        relaxed = self.relax_constraints(sorted(want))
        idx = [i for i, m in enumerate(self.memories)
               if m.get("text") in want and m.get("kind") != KIND_CONSTRAINT]
        if idx:
            self._drop(idx, archive=archive, reason=reason)
            self.event("drop", n=len(idx), reason=reason, archived=bool(archive))
            # `B9-⑤`（工单二）：**只有真的作废了才回调**。原来无匹配也回调，而 `on_drop`
            # 被接去同步 world.json → 会去删"根本没被作废"的条目（回调语义是「这些没了」，
            # 不是「请把这些删掉」）。约束走 `relax_constraints`，不在 world.json 里，无需回调。
            if self.on_drop:
                self.on_drop(list(want))
        return len(idx) + relaxed


class WorldMixin:

    def add_world(self, text: str) -> bool:
        """单独往世界书里塞一条，供手工录入使用。"""
        added, _ = self._add_memories([text], KIND_WORLD)
        return bool(added)

    def _world_check(self, text: str, vec: np.ndarray) -> tuple[str, list[str]]:
        """世界书入库检查：返回 (判定, 被推翻的旧条目原文)。

        判定三态："duplicate"（丢了）/ "contradict"（写新的 + 作废旧的）/ "new"。
        两道关卡的原因见 DEFAULT_CFG 里的注释——0.92 拦不住同义改写，
        而一味压低阈值又会误杀「年糕爱晒太阳」和「年糕爱去邻家躺着」这种
        各自独立、都该留下的条目。

        为什么这里必须处理「矛盾」而不是只处理「重复」：矛盾不在字数上，
        两条可能一个词都不重合（「养了只橘猫」vs「没养宠物」），
        纯相似度永远拦不住，只能靠语义判断 + 作废。
        """
        # `B4`：`idx`／`existing`／`sims` 必须来自**同一视界**（下面还要拿它们查矛盾）。
        with self._lock:
            memories = list(self.memories)
            idx = [i for i, m in enumerate(memories) if m.get("kind") == KIND_WORLD]
            vectors = self.vectors
            sims = (self._sims(vec)[idx]
                    if (idx and vectors is not None and len(vectors)) else None)
        if sims is None or not len(sims):
            # `C1`：空数组同样按"新增"放行（下面的 `sims.max()` 会炸在空序列上）
            return "new", []
        top = float(sims.max())
        if top >= float(self.cfg["world_dedup_sim"]):
            return "duplicate", []
        # 关键：**不能靠相似度筛候选来查矛盾**。
        # 实测「养了只叫年糕的橘猫」vs「家里没有养任何宠物」只有 0.50 相似度——
        # 语义互斥的两句，字面几乎不重叠，任何合理的相似度阈值都拦不住。
        # 世界书条目本来就少（十几条量级），所以直接全量复核：
        # 一次调用换「不可能有矛盾并存」，而且世界书只在提炼出新设定时才写，频率很低。
        if len(idx) <= WORLD_FULL_REVIEW:
            return self._world_review(text, [memories[i]["text"] for i in idx][:WORLD_FULL_REVIEW])
        # 条目多到不适合全量时，退回相似度近邻（至少比不查强）
        near = [memories[idx[k]]["text"]
                for k in np.argsort(-sims)[:6]
                if sims[k] >= float(self.cfg["world_review_sim"])]
        return self._world_review(text, near) if near else ("new", [])

    def _world_review(self, new_text: str, existing: list[str]) -> tuple[str, list[str]]:
        """灰区判定：重复 / 矛盾 / 新增。判不了就按新增放行，宁可多一条也不丢信息。"""
        self._llm_calls += 1
        data, _err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": WORLD_CHECK_PROMPT.format(
                existing="\n".join(f"- {t}" for t in existing), new=new_text)}],
            temperature=0.1, max_tokens=600, retries=1,
            no_think=bool(self.cfg.get("no_think")),
        )
        if data is None:
            return "new", []
        verdict = str(data.get("verdict", "new")).lower()
        if verdict not in ("duplicate", "contradict", "new"):
            return "new", []
        if verdict != "contradict":
            return verdict, []
        # 只接受确实存在于库里的原文，防止模型改写出一个查无此条的句子
        want = set(existing)
        stale = [t for t in strings(data, "stale") if t in want]
        # 说了矛盾却没给 stale，退化成新增（不敢凭一句结论就删旧条目）
        return ("contradict", stale) if stale else ("new", [])

    def _world_review_many(self, new_texts: list[str],
                           existing: list[str]) -> dict[str, tuple[str, list[str]]]:
        """一次问一批新设定的三态（T3 第 2 步的合并调用）。

        返回 {原文: (verdict, stale)}；**没答到的条目不出现在结果里**，调用方会回落到
        单条判定——宁可多花一次调用，也不要因为模型漏答就把新设定当脏数据丢掉。
        """
        self._llm_calls += 1
        data, _err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": WORLD_CHECK_BATCH_PROMPT.format(
                existing="\n".join(f"- {t}" for t in existing),
                new_items="\n".join(f"{i}. {t}" for i, t in enumerate(new_texts)))}],
            temperature=0.1, max_tokens=1200, retries=1,
            no_think=bool(self.cfg.get("no_think")),
        )
        out: dict[str, tuple[str, list[str]]] = {}
        if data is None:
            return out                                  # 调用失败 → 调用方按新增放行
        want = set(existing)
        for v in (data.get("verdicts") or []):
            if not isinstance(v, dict):
                continue
            try:
                i = int(v.get("index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(new_texts)):
                continue
            verdict = str(v.get("verdict", "new")).lower()
            if verdict not in ("duplicate", "contradict", "new"):
                verdict = "new"
            # 与单条判定同一条纪律：stale 只认库里真实存在的原文，
            # 说了矛盾却没给 stale 就退化成新增。
            stale = [t for t in strings(v, "stale") if t in want]
            out[new_texts[i]] = (("contradict", stale)
                                 if verdict == "contradict" and stale
                                 else (verdict, []))
        return out

    def _world_check_batch(self, texts: list[str],
                           vecs: list) -> dict[str, tuple[str, list[str]]]:
        """一次判定一批新世界书条目的三态。

        快路径仍留在本地：相似度 ≥ `world_dedup_sim` 直接判重，不花模型调用；
        只有落进灰区的才交给模型，而且**一次问完**。库里还没有世界书时全部按新增。
        """
        out: dict[str, tuple[str, list[str]]] = {}
        # `B4`：`idx`／`existing`／`sims` 同一视界（模型调用仍在锁外）。
        with self._lock:
            memories = list(self.memories)
            idx = [i for i, m in enumerate(memories) if m.get("kind") == KIND_WORLD]
            existing = [memories[i].get("text", "") for i in idx]
            vectors = self.vectors
        pending: list[str] = []
        for text, vec in zip(texts, vecs):
            if not idx or vectors is None or not len(vectors):
                out[text] = ("new", [])                 # 世界书还是空的 → 全是新增
                continue
            arr = np.asarray([vec], dtype=np.float32)
            # `C1`（工单三）：`sims` **必须和 `idx` 在同一把锁里算**（照 `_world_check` 的写法）。
            # B4 只把单条路径收进了锁内，这里曾留在锁外：并发 `_drop`/attach 时用旧 `idx` 索引
            # 新向量会错位、甚至越界（真机复现 IndexError: index 0 is out of bounds for axis 0 with size 0）。
            # ⚠ 只"移进 with"不够——`_sims` 内部也会读共享引用，所以**算完再校一次边界**：
            # 视界变了就按新增放行（宁可多一条，也不为此崩掉整轮对话）。
            with self._lock:
                sims_all = self._sims(arr[0])
                if not idx or len(sims_all) <= idx[-1]:
                    out[text] = ("new", [])
                    continue
                sims = sims_all[idx]
            if float(sims.max()) >= float(self.cfg["world_dedup_sim"]):
                out[text] = ("duplicate", [])
                continue
            pending.append(text)
        if pending:
            out.update(self._world_review_many(pending, existing))
        return out

    def sync_world(self, entries: list[str]) -> tuple[int, int]:
        """按外部清单校准世界书：清单里有就补，清单里没有就删。

        world.json 是唯一真相，模型自动提炼到的条目会回调写进那里，
        所以这里以清单为准做增删是安全的。
        """
        want = {e.strip() for e in entries if e and e.strip()}
        have = {m.get("text", ""): m for m in self.memories
                if m.get("kind") == KIND_WORLD}

        # `B1`（工单二）：**按身份删、不按下标**。原来先把"文本 → 下标"算好，再调
        # `_add_memories`——而它内部会 `_prune()`（物理删冷记忆）→ 下标整体漂移 →
        # 下面的 `_drop(stale)` 会删到**别的条目**上。真机复现：初值
        # `[事实F, 世界书A, 世界书B]`、`sync_world(["世界书B","世界书C"])` →
        # 得到 `[世界书A, 世界书C]`（B 被误删、A 反而留下）→ **世界书丢数据**。
        # 现在：先记下要删的**那些行**，增删之后再按身份重新定位下标。
        missing = [t for t in (want - set(have)) if t]
        self._add_memories(missing, KIND_WORLD)
        added = len(missing)

        stale_rows = [m for text, m in have.items() if text not in want]
        if stale_rows:
            with self._lock:
                victims = [i for i, m in enumerate(self.memories)
                           if any(m is row for row in stale_rows)]
                if victims:
                    self._drop(victims)
        return added, len(stale_rows)

    def world_texts(self) -> list[str]:
        return [m["text"] for m in self.memories if m.get("kind") == KIND_WORLD]
