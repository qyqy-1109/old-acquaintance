"""记忆的**生命周期**：遗忘、淘汰、作废、补偿、摘要，以及埋点统计与清理。

从 ``memory.py`` 拆出来的 Mixin（`#073`）。淘汰与摘要都在写入路径的
尾部被调用，所以改动这里要连带跑 `_tmptest/probe_split_smoke*.py`。
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
    SUPERSEDE_PROMPT, COMPENSATE_PROMPT, SUMMARIZE_PROMPT, DEFAULT_CFG,
)


class ForgetMixin:

    def _weight(self, m: dict, now: float | None = None) -> float:
        """当前权重 = 命中加成 × 时间衰减。越常被想起、越近期，越难被淘汰。"""
        now = now or time.time()
        last = m.get("last_hit") or m.get("ts") or now
        age_days = max(0.0, (now - last) / 86400.0)
        half_life = float(self.cfg["half_life_days"] or 0)
        decay = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        bonus = min(float(self.cfg["hit_bonus"]) * int(m.get("hits", 0)),
                    float(self.cfg["hit_cap"]))
        return (1.0 + bonus) * decay

    def _prune(self) -> None:
        limit = int(self.cfg["max_memories"] or 0)
        if not limit:
            return
        # 候选的计算与实际删除必须在同一视界内：否则并发的写入/删除会让
        # 「算出来的下标」落到已经不是那条的记忆上。
        with self._lock:
            if len(self.memories) <= limit:
                return
            now = time.time()
            ranked = sorted(
                (self._weight(m, now), i) for i, m in enumerate(self.memories)
                # 世界书、约束不参与淘汰；对方明说「记住」的那些也不淘汰
                if m.get("kind") not in PROTECTED_KINDS and not m.get("explicit")
            )
            # 「超限多少条」不等于「能删多少条」：候选只是活跃集的一个子集，
            # 保护类（世界书/约束/明说记住的）一条都不能动。候选够时两者相等；
            # 候选不够时**上限这一轮本来就达不成**——那就必须说出来。
            # 否则 max_memories 会静默失效：每轮都删、永远删不够、一句日志都没有。
            over = len(self.memories) - limit
            victims = [i for _, i in ranked[:over]]
            if len(victims) < over:
                log.warning(
                    "记忆超限 %d 条，但可淘汰的只有 %d 条：%d 条世界书/约束/显式记忆"
                    "不参与淘汰，上限 %d 这次达不成（可调大 max_memories 或清理这几类）",
                    over, len(victims), len(self.memories) - len(ranked), limit)
                self.event("prune_blocked", over=over, victims=len(victims),
                           protected=len(self.memories) - len(ranked), limit=limit)
            # 容量淘汰是**物理删**：这里删的是"又旧又没被想起过"的冷记忆，
            # 不是"被推翻"的条目——后者才走归档（drop_memories 默认 archive=True）。
            # 显式写出来，是因为 `_drop` 的签名默认 False，读代码的人容易以为会留档（T67-④）。
            self._drop(victims, archive=False)

    def _drop(self, indices: list[int], archive: bool = False,
              reason: str = "") -> None:
        """把记忆移出活跃集。

        两条路，语义不同，别混（T67-④ 把这段说明写准）：
        · `archive=True`（**软删**，用于「被推翻 / 与世界书矛盾」——`drop_memories` 的默认）：
          库里只标记 `archived_at`，条目还在，以后还能回答「当时是怎么说的」。
          软删是作废路径的默认方向，因为物理删会连坐：一条记忆捆着好几样信息时，
          作废其中一样会把别的也一起弄丢（实测 5 个场景里 2 次真丢）。
        · `archive=False`（**物理删**）：用于「容量淘汰」（`_prune`）与
          「对方明确要求删除」（`/mem del`、`/forget`）——前者是冷数据清理，
          后者是用户的意图，都不该被我们留档。
        """
        if not indices:
            return
        drop = set(indices)
        ids = [self.memories[i].get("id") for i in drop
               if i < len(self.memories) and self.memories[i].get("id")]
        if ids:
            q = ",".join("?" * len(ids))
            with self._lock, self.db:
                if archive:
                    now = time.time()
                    self.db.execute(
                        f"UPDATE memories SET archived_at=?, archived_reason=? "
                        f"WHERE id IN ({q})", [now, reason or "superseded", *ids])
                else:
                    self.db.execute(f"DELETE FROM memories WHERE id IN ({q})", ids)

        if archive:
            with self._lock:          # 归档留档的读-改-写也要原子，否则并发作废会丢档
                self.archived = (
                    [{"id": self.memories[i].get("id"), "text": self.memories[i]["text"],
                      "kind": self.memories[i].get("kind"),
                      # event_date/actor 必须一起带过来：`timeline()` 要按日期排序、
                      # 要给她的经历标〔她〕，而内存态归档原来是这五个键之外的都丢。
                      # 库里的归档行同样要带（见 storage 的 `_load`），两处缺一处，
                      # 「归档也列在时间轴上」就永远走不到（清点时抓到）。
                      "event_date": self.memories[i].get("event_date"),
                      "actor": self.memories[i].get("actor"),
                      "archived_at": time.time(),
                      "reason": reason or "superseded"} for i in drop if i < len(self.memories)]
                    + self.archived)
                self._trim_archive()

        # 重建 memories / vectors 这对平行结构：必须整体原子，否则并发下又会错位
        with self._lock:
            keep = [i for i in range(len(self.memories)) if i not in drop]
            self.memories = [self.memories[i] for i in keep]
            if self.vectors is not None and len(self.vectors):
                mask = np.ones(len(self.vectors), dtype=bool)
                for i in drop:
                    if i < len(mask):
                        mask[i] = False
                self.vectors = self.vectors[mask]
                if not len(self.vectors):
                    self.vectors = None

    def _trim_archive(self) -> None:
        """归档留档也要有出口：超过上限就删最老的。

        没有出口的归档迟早会无限增长。上限按实测定得很宽——500 条文本只有几 MB，
        永远不会成为性能问题，它起的是保险丝作用，不是性能调优。
        """
        keep = int(self.cfg["archive_keep"] or 0)
        if not keep:
            return
        with self._lock:              # 截断内存留档与删库必须同一视界
            if len(self.archived) <= keep:
                return
            cut = self.archived[keep:]
            self.archived = self.archived[:keep]
            ids = [m["id"] for m in cut if m.get("id")]
            if ids:
                q = ",".join("?" * len(ids))
                with self.db:
                    self.db.execute(f"DELETE FROM memories WHERE id IN ({q})", ids)
            log.info("归档超过 %d 条，已清除最老的 %d 条", keep, len(ids))

    def _find_superseded(self, fresh: list[str]) -> list[str]:
        """新事实里出现状态变化词、但提炼器没作废任何旧记忆时的主动追问。

        实测（t_a04_rate.py）：「公司搬到滨江」4 次里 0 次触发对「公司在城西」的
        作废——新事实被写成「已从城西搬到滨江」，模型把它当过渡叙述而不输出 drop。
        这里单独问一次「哪些旧记忆被取代了」，只在新事实含状态变化词时才触发，
        所以频率很低。
        """
        fresh_set = set(fresh)
        related: list[str] = []

        # 一、按相似度召回（覆盖普通事实类）
        vec = self._query_vec(" ".join(fresh))
        # `B4`＋`C1`（工单三）：相似度与它索引的记忆行必须**在同一把锁里**取齐。
        # 原来 `_sims` 在锁外现算，只靠下面的边界检查做到"不崩"，仍会错配（评审复核指出的同类低危）。
        with self._lock:
            memories = list(self.memories)
            sims = self._sims(vec) if vec is not None else None
        if sims is not None:
            for i in np.argsort(-sims):
                if len(related) >= 8:
                    break
                if float(sims[i]) < float(self.cfg["threshold"]):
                    break
                k = int(i)
                if k >= len(memories):
                    continue
                m = memories[k]
                if m["text"] in fresh_set:   # 新内容自己不算旧记忆
                    continue
                related.append(m["text"])

        # 二、约束和世界书**无条件带上**。理由：约束按设计不参与召回（见 KIND_CONSTRAINT
        # 的注释），世界书也很可能因为措辞没重叠而落到底下——
        # 但它们恰恰最容易被「不在了」「不用忌了」这类变化推翻。
        # S05（年糕不在了）和 S11（约束被放宽）漏掉就是因为只看了相似度召回。
        for m in memories:
            t = m["text"]
            if m.get("kind") in (KIND_CONSTRAINT, KIND_WORLD) \
                    and not m.get("relaxed_at") \
                    and t not in fresh_set and t not in related:
                related.append(t)
        related = related[:16]
        if not related:
            return []

        self._llm_calls += 1
        data, _err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": SUPERSEDE_PROMPT.format(
                new="\n".join(f"- {t}" for t in fresh),
                old="\n".join(f"- {t}" for t in related))}],
            temperature=0.1, max_tokens=1000, retries=1,
            no_think=bool(self.cfg.get("no_think")),
        )
        if not data:
            return []
        want = set(related)
        # 只接受确实存在于库里的原文，防止模型改写出一个查无此条的句子
        return [t for t in strings(data, "drop") if t in want][:3]

    def _compensate(self, gone: list[str], fresh: list[str]) -> list[str]:
        """作废之后的补偿：把旧条目里仍然成立的部分捡回来。

        为什么需要这一步：提示词里已经写了「一条捆着好几样信息时不要整条作废」，
        但实测模型 40% 的情况不听。软删只是让它「可恢复」，信息仍然不在活跃集里，
        对话中还是用不上。这一步是那道指令的兜底——模型没做到，机器来补。
        代价是每次作废多一次模型调用，而作废本身不常发生。
        """
        if not gone:
            return []
        self._llm_calls += 1
        data, _err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": COMPENSATE_PROMPT.format(
                gone="\n".join(f"- {t}" for t in gone),
                new="\n".join(f"- {t}" for t in fresh) or "（无）")}],
            temperature=0.1, max_tokens=1000, retries=1,
            no_think=bool(self.cfg.get("no_think")),
        )
        if data is None:
            return []
        return strings(data, "keep", "parts")[:3]

    def recent_archived(self, n: int = 5) -> list[str]:
        """最近作废的几条原文。

        用途：喂给提炼器，告诉它「这些已经作废了，别再写一遍」。
        归档条目不参与召回，提炼器看不到它们，否则会把同一个已被推翻的事实重新写回来。
        """
        return [m["text"] for m in self.archived[:n]]

    def _maybe_summarize(self) -> None:
        every = int(self.cfg["summarize_every"] or 0)
        window = int(self.cfg["window"] or 0)
        if every <= 0:
            return
        # 上一批失败的退避期：不推进游标是对的（不丢摘要），但每轮都重试不行
        # ——模型长期不可用（欠费/断网/限流）时会每轮白烧一次调用。
        if time.time() < self._summarize_retry_at:
            return
        history = self.sessions.get(self.session, [])
        cursor = int(self.summary_cursor.get(self.session, 0))
        # 窗口之外的部分是已经进不了模型的；其中还没被摘要过的才是待办
        pending_end = len(history) - window * 2
        if pending_end <= 0 or pending_end - cursor < every * 2:
            return
        chunk = history[cursor:pending_end]
        text = self._summarize(chunk, self.summaries.get(self.session, ""))
        # 判成败必须用 "is not None"，不能用「返回是不是空串」：
        # 「摘要失败」返回 None，「窗口外没有可摘要的内容」返回空串，
        # 两者混在一起的下场是失败也被当成成功、游标照推，
        # 那批对话**永远不会再被摘要**，而且不报错（踩过，见 tests/test_pure.py）。
        delay = float(self.cfg["summarize_retry_delay"] or 0)
        # 摘要与游标必须**一起**更新（同一视界），否则并发下会留下
        # 「摘要换了、游标还是旧的」这种半截状态。
        with self._lock:
            if text is not None:
                self.summaries[self.session] = text
                self.summary_cursor[self.session] = pending_end
                self._summarize_retry_at = 0.0        # 成功：清掉退避
            else:
                self._summarize_retry_at = (time.time() + delay) if delay > 0 else 0.0
        if text is not None:
            self.event("summarize", ok=True, cursor=pending_end,
                       chunk=len(chunk), chars=len(text))
        else:
            self.event("summarize", ok=False, cursor=cursor,
                       chunk=len(chunk), retry_in=delay)

    def _summarize(self, chunk: list[dict], old: str) -> str | None:
        """把一段对话压成摘要。**失败返回 None**，与「内容为空」严格区分。

        返回值在这里是控制信号，不只是数据：调用方靠它决定游标推不推进。
        所以宁可多一个 None 分支，也不能让失败伪装成一个正常的字符串返回。
        """
        dialog = "\n".join(
            f"{'用户' if m.get('role') == 'user' else '助手'}："
            f"{str(m.get('content', ''))[:300]}"
            for m in chunk if m.get("content")
        )[:4000]
        if not dialog.strip():
            # 窗口外这批消息里一条有内容的都没有（比如全是空回答），
            # 没有可摘要的东西：还回原摘要（可能是空串），让游标正常推进。
            return old or ""
        prompt = SUMMARIZE_PROMPT.format(old=old or "（无）", dialog=dialog)
        kw = {"extra_body": {"thinking": {"type": "disabled"}}} if self.cfg.get("no_think") else {}
        self._llm_calls += 1
        try:
            try:
                resp = self.client.chat.completions.create(
                    model=self.extract_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=1200, **kw)
            except Exception:
                if not kw:
                    raise
                resp = self.client.chat.completions.create(  # 关不掉思考就退回
                    model=self.extract_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=1200)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            if not text.strip():
                try:
                    text = resp.model_dump()["choices"][0]["message"].get("reasoning_content") or ""
                except Exception:
                    pass
        except Exception as exc:
            log.warning("会话摘要失败（游标不推进，留到下轮重试）：%s: %s",
                        type(exc).__name__, exc)
            return None
        cap = int(self.cfg["summarize_max_chars"] or 400)
        text = text.strip()[:cap]
        if not text:
            # 模型返回空文本（典型场景：额度被思考吃光）。这不是「摘要为空」，
            # 是失败——落一个空摘要会把已有摘要抹掉，游标前移还会永久丢掉这批对话。
            log.warning("会话摘要返回空文本，按失败处理，游标不推进")
            return None
        return text


class EventsMixin:

    def event(self, kind: str, **fields) -> None:
        """记一笔机制事件。**绝不影响主流程**：任何失败都静默吞掉。

        为什么需要它：机制跑没跑、跑了几次、成功率多少，靠读 logs/app.log 的文本
        是数不出来的（日志会滚、会丢、格式会变）。有了这张表，「补偿真的有效吗」
        「兜底触发率多少」才是可回答的问题——先观测，再谈新增。
        """
        try:
            payload = json.dumps(fields, ensure_ascii=False, default=str)
            with self._lock, self.db:
                self.db.execute(
                    "INSERT INTO events(ts,kind,session,payload) VALUES(?,?,?,?)",
                    (time.time(), kind, self.session, payload))
                self._events_since_trim += 1     # 与插入同一视界
        except Exception as exc:                 # 埋点失败绝不能带崩对话
            log.debug("埋点失败（已忽略）：%s: %s", type(exc).__name__, exc)

    def _trim_events(self) -> None:
        """事件表也要有出口：超过上限删最老的（和归档留档同一条纪律）。

        挂在 save() 上（每轮一次），不做成「每次写入都 COUNT(*)」。
        id 单调递增，所以「保留 id 最大的 keep 条」一句 DELETE 就够。
        """
        keep = int(self.cfg["events_keep"] or 0)
        if not keep:
            return
        try:
            with self._lock, self.db:
                if self._events_since_trim <= 0:
                    return
                self._events_since_trim = 0
                self.db.execute(
                    "DELETE FROM events WHERE id <= "
                    "(SELECT COALESCE(MAX(id), 0) FROM events) - ?", (keep,))
        except Exception as exc:
            log.debug("事件修剪失败（已忽略）：%s: %s", type(exc).__name__, exc)

    def events_recent(self, limit: int = 10) -> list[dict]:
        """最近的事件（倒序），payload 展开成字段。"""
        out = []
        for r in self.db.execute(
                "SELECT ts,kind,session,payload FROM events ORDER BY id DESC LIMIT ?",
                (int(limit),)):
            try:
                fields = json.loads(r["payload"] or "{}")
            except Exception:
                fields = {"raw": r["payload"]}
            out.append({"ts": r["ts"], "kind": r["kind"], "session": r["session"],
                        **fields})
        return out

    def events_stats(self) -> dict[str, int]:
        """按事件类型聚合计数（降序）。"""
        return {r["kind"]: r["count"] for r in self.db.execute(
            "SELECT kind, COUNT(*) AS count FROM events GROUP BY kind "
            "ORDER BY count DESC")}

    def events_report(self, limit: int = 10) -> str:
        """给 /mem events 用的人话报告。"""
        stats = self.events_stats()
        if not stats:
            return ("事件表还是空的。机制一旦跑起来（提炼、世界书判定、补偿、摘要、"
                    "作废、槽位）就会自动记账——它是用来回答「机制到底在不在跑」的。")
        lines = [f"机制事件共 {sum(stats.values())} 条"
                 f"（保留上限 {self.cfg['events_keep']}）："]
        lines += [f"  {k}：{v} 次" for k, v in stats.items()]
        lines.append(f"\n最近 {limit} 条：")
        for e in self.events_recent(limit):
            when = dt.datetime.fromtimestamp(float(e["ts"])).strftime("%m-%d %H:%M")
            extra = "，".join(f"{k}={v}" for k, v in e.items()
                              if k not in ("ts", "kind", "session"))
            lines.append(f"  {when} [{e['kind']}] {extra[:110]}")
        return "\n".join(lines)

    def clear_session(self) -> None:
        with self._lock:              # 与并发的 add_turn / save 划清视界
            self.sessions[self.session] = []
            self.summaries.pop(self.session, None)
            self.summary_cursor.pop(self.session, None)
            # 只清内存不够：库里的摘要在 save() 里才处理，得记一笔让它被删掉
            self._summaries_dropped.add(self.session)
            # `B5`（工单二）：`_synced` 必须一起归零，**并且当场把旧消息从库里删掉**。
            # 只清内存会踩这个坑：清完再加**不少于旧条数**的新消息时，`save()` 里
            # 「`len(msgs) < done` → 会话被清空过」那个分支**不触发** → `msgs[done:]` 从旧下标切 →
            # 新消息落不了盘，而库里全是旧消息（现象就是"旧消息复活、新消息被覆盖"）。
            # 在这里当场删，是为了让"库 = 内存"立刻成立，不用等下一次 save 的偶然条件。
            self._synced[self.session] = 0
            with self.db:
                self.db.execute("DELETE FROM messages WHERE session=?", (self.session,))

    def forget_all(self, include_world: bool = False) -> str:
        """清空长期记忆。默认保留世界书——那是设定，不是记忆。"""
        if include_world:
            # 明确要求清空 → 物理删，不留档（这是用户的意图，不是我们自作主张）
            # 但**必须回报给回调**：世界书的唯一真相是 `world.json`，不通知它，
            # 文件里还是满的，下次启动 `WorldBook.sync()` 会把条目全灌回来——
            # 用户看到"已全部清空"，重启后世界书又回来了（实测过这个坑）。
            worlds = [m["text"] for m in self.memories if m.get("kind") == KIND_WORLD]
            self._drop(list(range(len(self.memories))), archive=False)
            if worlds and self.on_drop:
                self.on_drop(worlds)
            return "长期记忆、世界书和约束已全部清空"
        # 世界书是设定、约束是硬性要求，都不属于「经历」，所以默认不跟着一起忘掉
        keep = [i for i, m in enumerate(self.memories)
                if m.get("kind") in PROTECTED_KINDS or m.get("explicit")]
        n = len(self.memories) - len(keep)
        self._drop([i for i in range(len(self.memories)) if i not in keep])
        return f"已遗忘 {n} 条长期记忆，世界书和约束的 {len(keep)} 条保留"

    def stats(self) -> str:
        turns = len(self.sessions.get(self.session, [])) // 2
        counted = {KIND_FACT, KIND_TURN}
        facts = sum(1 for m in self.memories if m.get("kind") in counted)
        world = sum(1 for m in self.memories if m.get("kind") == KIND_WORLD)
        cons = sum(1 for m in self.memories
                   if m.get("kind") == KIND_CONSTRAINT and not m.get("relaxed_at"))
        relaxed = sum(1 for m in self.memories
                      if m.get("kind") == KIND_CONSTRAINT and m.get("relaxed_at"))
        tail = f"（已解除 {relaxed}）" if relaxed else ""
        s = (f"当前会话 {turns} 轮　|　长期记忆 {facts} 条　|　"
             f"世界书 {world} 条　|　约束 {cons} 条{tail}　|　会话数 {len(self.sessions)}")
        if self.summaries.get(self.session):
            s += "　|　已有早期摘要"
        limit = int(self.cfg["max_memories"] or 0)
        if limit:
            s += f"　|　上限 {limit}"
        return s
