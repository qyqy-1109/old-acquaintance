"""记忆的**存储基建**：落库、加载对齐、向量补算，以及 embedding 与相似度。

从 ``memory.py`` 拆出来的 Mixin（`#073`）。`memories` 与 `vectors` 是一对
**平行结构**（同一个下标指同一条记忆），`_align`／`_recover_missing_vectors`
负责维持这个不变式——只在加载期调用。
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
    EMBED_MAX_CHARS, EMBED_BATCH, KIND_FACT, KIND_TURN, KIND_WORLD,
    KIND_CONSTRAINT, PROTECTED_KINDS, EXPLICIT_MARKERS, EXPLICIT_EMPTY,
    RECONCILE_PROMPT, EXTRACT_PROMPT, WORLD_FULL_REVIEW, WORLD_CHECK_PROMPT,
    WORLD_CHECK_BATCH_PROMPT, STATE_CHANGE, GONE_WORDS, SELF_WORDS,
    SUPERSEDE_PROMPT, COMPENSATE_PROMPT, SUMMARIZE_PROMPT, DEFAULT_CFG,
)

# 「同一轮」的时间判据：与上一条相差不超过这么多秒，就算**同一次写入**产出的。
# 为什么是 0.5 秒这么小：同一次 `add_turn` 里写进去的条目共用同一个 `now`（毫秒级差），
# 而**不同轮至少差几秒**。放宽到 30 秒会把"快问快答"的两轮并成一回（实测踩到），
# 那比不合并更糟——这层只是**存量兜底**，跨轮的复述靠 `scene` 标签，不靠时间猜。
SAME_TURN_SEC = 0.5


def _guard_lock(obj):
    """`B4`（工单二）：取"读写一致性"要用的锁。

    真 `MemoryStore` 在 `__init__` 里建了 `self._lock`（RLock，契约见 db.py:186-190）；
    **轻量替身**（测试里的 `SimpleNamespace`，甚至 unbound 调用的 `StorageMixin._query_vec(store, …)`）
    可能压根没有这个属性 → 退回一把挂在它身上的锁。

    写成**模块级函数**而不是属性，是因为属性要求 `self` 真的是本类的实例；而仓库里有测试
    直接拿 `SimpleNamespace` + unbound 调用来验证降级路径。护栏不该把功能弄挂：
    拿不到共用锁时"少一层保护"，而不是抛 `AttributeError`。
    """
    lock = getattr(obj, "_lock", None)
    if lock is not None:
        return lock
    lock = getattr(obj, "_fallback_lock", None)
    if lock is None:
        lock = threading.RLock()
        try:
            obj._fallback_lock = lock
        except Exception:                     # 连属性都写不进去的对象：给它一把临时的
            pass
    return lock


class StorageMixin:

    @staticmethod
    def _to_mem(row) -> dict:
        # `row` 可能是 sqlite 的 Row，也可能是**普通 dict**（`_park_pending` 建的那条）。
        # 用 `[]` 取 `explicit` 在 dict 上会 KeyError——`#103` 实测：embedding 整体失败时
        # 「转入 pending 等补算」这条救命路径自己先炸了，文本只剩日志里一行。
        def g(key, default=None):
            try:
                return row[key]
            except (KeyError, IndexError):
                return default

        m = {"id": g("id"), "text": g("text"), "kind": g("kind"),
             "ts": g("ts"), "session": g("session"),
             "hits": g("hits"), "last_hit": g("last_hit"),
             "event_date": g("event_date"), "actor": g("actor"),
             "relaxed_at": g("relaxed_at")}
        if g("explicit"):
            m["explicit"] = True
        return m

    def _load(self) -> None:
        cols = ("id,text,kind,ts,session,hits,last_hit,explicit,event_date,actor,"
                "relaxed_at,vec")
        rows = [dict(r) for r in self.db.execute(
            f"SELECT {cols} FROM memories WHERE archived_at IS NULL ORDER BY id")]
        # 相对词条目迁移（第二批）：存量条目里的「明天/今天」是以【写入那天】为锚的，
        # 按「现在」解析必然错位（踩过：9/22 写的「年糕明天会被放出去玩」，第三天
        # 还被当成明天，用户提中秋它被召回）。用条目自己的 ts 把相对词重写为绝对日期。
        # 文本变了向量就作废（置 None），下面的补算流程会自动重新 embedding。
        re_anchored = []
        for r in rows:
            new_text = clock.reanchor_relatives(r["text"],
                                                dt.datetime.fromtimestamp(r["ts"] or 0))
            if not new_text:
                continue
            r["text"] = new_text
            r["vec"] = None                            # 文本变了，旧向量作废待补算
            d2 = clock.find_date(new_text, dt.datetime.fromtimestamp(r["ts"] or 0))
            if d2:
                r["event_date"] = d2.isoformat()
            re_anchored.append((new_text, r.get("event_date"), r["id"]))
        if re_anchored:
            with self._lock, self.db:
                self.db.executemany(
                    "UPDATE memories SET text=?, event_date=? WHERE id=?", re_anchored)
            log.info("相对词条目已按写入日重锚 %d 条（向量待补算）", len(re_anchored))
        # 有向量的一定排在前面：_recover_missing_vectors 靠这个约定找缺失的那些
        with_vec = [r for r in rows if r["vec"] is not None]
        no_vec = [r for r in rows if r["vec"] is None]
        self.memories = [self._to_mem(r) for r in with_vec]
        # `B9-①`（工单二）：重建 `pending` 时**按文本去重**（并丢掉已经有向量的那一份）。
        # 不去重的话，库里两条 `vec IS NULL` 的同一文本会各补一次向量 → 活跃集里出现两条
        # 一模一样的记忆（召回、时间轴、淘汰都被它白占名额）。
        seen_texts: set[str] = {m.get("text") for m in self.memories}
        self.pending = []
        for r in no_vec:
            mem = self._to_mem(r)
            text = mem.get("text")
            if text in seen_texts:
                continue
            seen_texts.add(text)
            self.pending.append(mem)
        # `pending` 表里还可能有"孤儿"：对应的 memories 行已经不在了（迁移遗留，或那条
        # 记忆被删过）。内存态的 pending 是从 `memories.vec IS NULL` 重建的，
        # **表只写不读**就等于把这些文本静默丢掉（T67-②）。这里重新登记成正式记忆
        # （vec 仍为空 → 走下面的补算流程补向量），让它们有机会回到活跃集。
        known = {m.get("text") for m in self.memories} | {m.get("text") for m in self.pending}
        orphans = [r for r in self.db.execute("SELECT text,kind FROM pending")
                   if r["text"] not in known]
        for r in orphans:
            entry = {"text": r["text"], "kind": r["kind"] or KIND_FACT, "ts": 0.0,
                     "session": self.session, "hits": 0, "last_hit": 0, "explicit": 0,
                     "event_date": None, "actor": "user", "relaxed_at": None}
            self._insert_memory(entry, None)          # 立刻落库，拿到 id
            self.pending.append(self._to_mem(entry))
        if orphans:
            log.warning("从 pending 表捡回 %d 条没有对应记忆行的文本（迁移遗留，待补向量）",
                        len(orphans))
        # 事件组回填（`T85` 第 2 步，零迁移）：`event_group` **不落库**（只在内存里活），
        # 所以每次加载都要重建。重建规则与写入时一致：同一个 scene 标签第 2 步才产生，
        # 存量条目没有标签，但**同一轮写入的条目天然是同一次互动**（ts 相同）——
        # 用 ts 给它们补一个组号，时间轴对老数据也能折叠。
        self._rebuild_event_groups()
        if with_vec:
            # 必须 copy：np.frombuffer 返回的是只读视图，不 copy 的话
            # 重启后任何「就地更新向量」的操作（_add_memories 的转正分支）
            # 都会报 assignment destination is read-only。这个 bug 只在重启后出现，
            # 第一次启动（走 vstack）不会触发，所以很隐蔽。
            self.vectors = np.frombuffer(
                b"".join(r["vec"] for r in with_vec), dtype=np.float32
            ).reshape(len(with_vec), -1).copy()
        else:
            self.vectors = None

        # 事件时间轴的存量迁移：event_date 是 2026-09-23 才加的字段，老条目全是 NULL。
        # find_date 是纯函数（不调模型），补跑一遍成本可忽略。
        # 只补「文本里真的能解析出日期」的；解析不出的保持 NULL（不硬造）。
        need = [m for m in list(self.memories) + list(self.pending)
                if not m.get("event_date")]
        parsed = []
        for m in need:
            d = clock.find_date(m.get("text", ""))
            if d:
                m["event_date"] = d.isoformat()
                parsed.append((d.isoformat(), m["id"]))
        if parsed:
            with self._lock, self.db:
                self.db.executemany(
                    "UPDATE memories SET event_date=? WHERE id=? AND event_date IS NULL",
                    parsed)

        keep = int(self.cfg["archive_keep"] or 0)
        self.archived = [
            {"id": r["id"], "text": r["text"], "kind": r["kind"],
             "event_date": r["event_date"], "actor": r["actor"],
             "archived_at": r["archived_at"], "reason": r["archived_reason"]}
            for r in self.db.execute(
                "SELECT id,text,kind,event_date,actor,archived_at,archived_reason "
                "FROM memories WHERE archived_at IS NOT NULL "
                "ORDER BY archived_at DESC LIMIT ?",
                (keep or 500,))
        ]

        for r in self.db.execute(
                "SELECT session,seq,role,content,ts,greeting FROM messages "
                "ORDER BY session,seq"):
            msg = {"role": r["role"], "content": r["content"], "ts": r["ts"]}
            if r["greeting"]:
                msg["greeting"] = True
            self.sessions.setdefault(r["session"], []).append(msg)
        self._synced = {s: len(v) for s, v in self.sessions.items()}

        for r in self.db.execute("SELECT session,text,cursor FROM summaries"):
            self.summaries[r["session"]] = r["text"] or ""
            self.summary_cursor[r["session"]] = int(r["cursor"] or 0)

        self._align()
        self.sessions.setdefault(self.session, [])

    def _align(self) -> None:
        """让向量和元数据严格一一对应：多出来的向量截掉，缺失的现场补算。

        **只在加载期调用**（见 `_recover_missing_vectors` 的说明）。
        """
        if self.vectors is not None and len(self.vectors) > len(self.memories):
            log.warning("向量比记忆多 %d 条，已截断", len(self.vectors) - len(self.memories))
            self.vectors = self.vectors[: len(self.memories)]
        self._recover_missing_vectors()

    def _recover_missing_vectors(self) -> None:
        """给没有向量的记忆补 embedding。补不上也不丢，挪进 pending 下次再试。

        **只在加载期调用**（`__init__` → `_load` → `_align`）：它整段重排
        `memories` / `vectors` / `pending` 三个结构，没有按写入路径那样逐段加锁，
        运行期不要调它。
        """
        n = 0 if self.vectors is None else int(len(self.vectors))
        orphans = self.memories[n:] + self.pending
        if not orphans:
            return
        # 先把没向量的挑出来，留下的这部分保持严格对齐，别让索引错位
        self.memories = self.memories[:n]
        self.pending = []

        texts = [m["text"] for m in orphans]
        vecs = self._embed(texts)
        if vecs is None:
            self.pending = orphans
            log.warning("%d 条记忆暂时没有向量（embedding 不可用），"
                        "已保留，联网后重启会自动补算", len(texts))
            return
        # 分片之后可能是"部分成功"：成功的补上向量、失败的继续留在 pending。
        # 早先这里直接 `np.asarray(vecs)`——列表里只要有一个 None 就会抛
        # "inhomogeneous shape"，**整批补算崩掉**（连已经成功的那些也白算）。
        ok = [(m, v) for m, v in zip(orphans, vecs) if v is not None]
        rest = [m for m, v in zip(orphans, vecs) if v is None]
        if rest:
            log.warning("补算时仍有 %d 条没拿到向量，留在 pending 下次再试", len(rest))
        if not ok:
            self.pending = orphans
            return
        orphans, vecs = [m for m, _ in ok], [v for _, v in ok]
        arr = np.asarray(vecs, dtype=np.float32)
        self.vectors = arr if not n else np.vstack([self.vectors, arr])
        self.memories.extend(orphans)
        self.pending = rest
        # 补齐的这些原本就在库里（只是 vec 为空），把向量补写回去
        with self._lock, self.db:
            for m, v in zip(orphans, arr):
                if m.get("id"):
                    self.db.execute("UPDATE memories SET vec=? WHERE id=?",
                                    (v.tobytes(), m["id"]))
        log.info("已为 %d 条缺失向量的记忆补算 embedding%s",
                 len(texts) - len(rest), f"（剩 {len(rest)} 条待补）" if rest else "")

    def _rebuild_event_groups(self) -> None:
        """给还没有 `event_group` 的条目按"同一轮"补组号（加载时跑一次）。

        `event_group` 只在内存里（不落库），所以每次启动都要重建；存量条目
        （写入时还没有 scene 机制）靠"**同一次写入**"归组：同一次 `add_turn` 里的条目
        共用同一个 `now`（毫秒级差），而不同轮至少差几秒，所以判据是"相差 ≤ `SAME_TURN_SEC`"。

        为什么不放宽到几十秒：那会把"快问快答"的两轮并成一回（实测踩到），比不合并更糟。
        为什么不按分钟分桶：桶有边界，10:16:40 与 10:17:10 只差 30 秒却被拆开。
        **跨轮的复述不靠时间猜，靠 `scene` 标签**——这层只是存量兜底。

        **只补空的**：带 `scene:` 前缀的组号是提炼器给的真场景、优先级更高，不许覆盖。
        """
        for m in sorted(self.memories, key=lambda x: float(x.get("ts") or 0)):
            if m.get("event_group"):
                continue
            try:
                ts = float(m.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if not ts:
                continue
            prev = self._last_group_ts
            if prev and 0 <= ts - prev[0] <= SAME_TURN_SEC:
                m["event_group"] = prev[1]          # 与上一条同一轮
            else:
                group = f"ts:{int(ts)}"
                m["event_group"] = group
                self._last_group_ts = (ts, group)

    def save(self) -> None:
        """把内存里的改动落盘。

        写是增量的：新记忆在写入那一刻就 INSERT 过了（见 _insert_memory），
        这里只补三样——命中计数的变化、会话新增的消息、摘要。
        不再有「改一个字段、全量重写整个文件」。
        """
        try:
            with self._lock, self.db:
                if self._dirty_hits:
                    self.db.executemany(
                        "UPDATE memories SET hits=?, last_hit=? WHERE id=?",
                        [(m.get("hits", 0), m.get("last_hit", 0), m["id"])
                         for m in self.memories
                         if m.get("id") in self._dirty_hits and m.get("id")])
                    self._dirty_hits.clear()

                # 遍历快照：并发的 add_turn 会 setdefault 出新的 session key，
                # 直接在 items() 视图上迭代会抛 RuntimeError（dict changed size），
                # 那是进程级异常，会打断整个请求。
                for s, msgs in list(self.sessions.items()):
                    done = self._synced.get(s, 0)
                    if len(msgs) < done:      # 会话被清空过，库里的旧消息要跟着删
                        self.db.execute("DELETE FROM messages WHERE session=?", (s,))
                        done = 0
                    for i in range(done, len(msgs)):
                        m = msgs[i]
                        self.db.execute(
                            "INSERT OR REPLACE INTO messages"
                            "(session,seq,role,content,ts,greeting) VALUES(?,?,?,?,?,?)",
                            (s, i, m.get("role", "user"), m.get("content"),
                             m.get("ts"), 1 if m.get("greeting") else 0))
                    self._synced[s] = len(msgs)

                for s, text in self.summaries.items():
                    self.db.execute(
                        "INSERT OR REPLACE INTO summaries(session,text,cursor) VALUES(?,?,?)",
                        (s, text, int(self.summary_cursor.get(s, 0))))
                # `/clear` 清掉的会话：库里的摘要行必须真的删掉，否则重启就复活，
                # 而且游标比历史还长 → 这个会话此后再也不做摘要（见 clear_session）。
                if self._summaries_dropped:
                    self.db.executemany("DELETE FROM summaries WHERE session=?",
                                        [(s,) for s in self._summaries_dropped])
                    self._summaries_dropped.clear()

                if self.pending:
                    self.db.execute("DELETE FROM pending")
                    self.db.executemany("INSERT INTO pending(text,kind) VALUES(?,?)",
                                        [(p["text"], p.get("kind", KIND_FACT))
                                         for p in self.pending])
                else:
                    self.db.execute("DELETE FROM pending")
                # 事件表修剪搭这趟车（每轮一次；不做成「每次写入都 COUNT(*)」）
                self._trim_events()
        except Exception as exc:
            log.error("保存失败：%s: %s", type(exc).__name__, exc)

    def _insert_memory(self, entry: dict, vec: np.ndarray | None) -> None:
        """新增一条记忆，顺手把主键写回 entry["id"]。"""
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO memories(text,kind,ts,session,hits,last_hit,explicit,"
                "event_date,actor,vec) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (entry["text"], entry.get("kind", KIND_FACT), float(entry.get("ts") or 0),
                 entry.get("session"), int(entry.get("hits") or 0),
                 float(entry.get("last_hit") or 0), 1 if entry.get("explicit") else 0,
                 entry.get("event_date"), entry.get("actor") or "user",
                 vec.tobytes() if vec is not None else None))
            entry["id"] = int(cur.lastrowid)

    def _update_memory(self, entry: dict, vec=None) -> None:
        """就地改动一条记忆的 kind/text/ts（可选连向量一起）**立刻落库**。

        为什么必须有这一步（T67-①）：`_add_memories` 的"就地转正"分支本来只改内存
        （`hit["kind"]/["text"]/["ts"]` + `self.vectors[j]`），而 `save()` 只写
        `hits/last_hit` → 重启后回退成旧 kind/旧文本，可 `world.json` 已经是新写法，
        下次启动 `sync_world` 就会把新文本再当一条新条目加一遍：白多一条重复。
        """
        if not entry.get("id"):
            return
        with self._lock, self.db:
            self.db.execute("UPDATE memories SET kind=?, text=?, ts=? WHERE id=?",
                            (entry.get("kind"), entry.get("text"),
                             float(entry.get("ts") or 0), entry["id"]))
            if vec is not None:
                self.db.execute("UPDATE memories SET vec=? WHERE id=?",
                                (np.asarray(vec, dtype=np.float32).tobytes(), entry["id"]))

    def _attach_vector(self, entry: dict, vec) -> None:
        """把「向量 + 元数据」**成对**追加。整个动作必须在一把锁里完成。

        为什么单独开这个口子：D7 之后 `vectors` 与 `memories` 靠下标一一对应，
        而这对关系只在"同增"时才成立。并发下如果两个写入都看到 `vectors` 为空、
        各自赋值，就会出现「vectors 1 行 / memories 2 条」——召回拿到的是别人的
        向量，而且**不报错**（这正是 D7 花大力气消灭的静默错位）。

        锁**只**包住这一段：上面的判定段里可能有模型调用（世界书三态复核），
        跨网络持锁会把整个服务拖死。
        """
        arr = np.asarray([vec], dtype=np.float32)
        with _guard_lock(self):
            if self.vectors is not None and len(self.vectors):
                self.vectors = np.vstack([self.vectors, arr])
            else:
                self.vectors = arr
            self._insert_memory(entry, arr[0])   # 落库并拿回主键
            self.memories.append(entry)

    def _embed(self, texts: list[str]) -> list[list[float] | None] | None:
        """批量算向量。**分片 + 片间隔离**：一片坏掉不连坐其它片。

        为什么不是"分批调用、任一片失败就整批 None"（老口径）：
        接口单次上限 20 条（实测 `qwen3.7-text-embedding` 超了直接 400
        `batch size is invalid, it should not be larger than 20`），所以长列表必然分片；
        而"任一片失败 → 整批丢弃"意味着**已经算成功的那些也一起扔**，
        调用方按既有口径就是"整轮不写记忆"（用户 2026-09-29 的原话："别超过 20 条就全体丢失"）。

        现在的契约：**返回与 `texts` 等长的列表**，某条没算出来就是 `None`
        （调用方 `_add_memories` 会把它放进 `pending` 表等下次补算，见那里的注释）。
        只有当**一条都没成功**时返回 `None`——保持"整个 embedding 服务不可用"这个语义，
        免得把"接口挂了"误报成"部分失败"。
        """
        if not texts:
            return []
        out: list[list[float] | None] = []
        for i in range(0, len(texts), EMBED_BATCH):
            chunk = texts[i:i + EMBED_BATCH]
            self._embed_calls += 1
            got = self._embed_chunk(chunk)
            out.extend(got if got is not None else [None] * len(chunk))
        if all(v is None for v in out):
            return None
        return out

    def _embed_chunk(self, chunk: list[str]) -> list[list[float]] | None:
        """发一片（≤ `EMBED_BATCH` 条）。失败在这一片内重试一次——瞬时的 429/抖动不值得丢记忆。"""
        started = time.perf_counter()
        kw: dict = {}
        if self.embed_dimensions:
            kw["dimensions"] = self.embed_dimensions
        payload = [t[:EMBED_MAX_CHARS] for t in chunk]
        for attempt in (1, 2):
            try:
                resp = self.embed_client.embeddings.create(
                    model=self.embed_model, input=payload, **kw)
                got = [d.embedding for d in resp.data]
                if len(got) != len(chunk):
                    log.error("embedding 返回条数不符（要 %d 条、拿到 %d 条）",
                              len(chunk), len(got))
                    return None
                return got
            except Exception as exc:
                last = exc
                if attempt == 1:
                    log.warning("phase=embedding elapsed_ms=%d error_type=%s；立即重试一次：%s",
                                round((time.perf_counter() - started) * 1000),
                                type(exc).__name__, exc)
                    time.sleep(0.5)
        log.error("phase=embedding elapsed_ms=%d error_type=%s；两次都失败"
                  "（%d 条不会进活跃集，等下次补算）：%s",
                  round((time.perf_counter() - started) * 1000),
                  type(last).__name__, len(chunk), last)
        return None

    def _sims(self, qv: np.ndarray) -> np.ndarray:
        # 快照：一次计算里**绝不重复读共享引用**——并发下两次读可能拿到不同的数组，
        # 实测表现为 ValueError: operands could not be broadcast together (14,) (13,)。
        # `B4`（工单二）：快照要**在锁内**取。判定方拿到相似度数组之后，还要用它产出
        # 的下标去索引 `self.memories`（另一次读）——两次读不在同一视界就会错位甚至越界。
        with _guard_lock(self):
            vecs = self.vectors
            if vecs is None or not len(vecs):
                return np.array([])
            denom = np.linalg.norm(vecs, axis=1) * np.linalg.norm(qv)
            return (vecs @ qv) / np.maximum(denom, 1e-9)

    def _query_vec(self, query: str) -> np.ndarray | None:
        with _guard_lock(self):                      # `B4`：这次"库空不空"的判断也要在同一视界里
            vectors = self.vectors
        if vectors is None or not len(vectors):
            return None
        started = time.perf_counter()
        got = self._embed([query])
        if not got:
            log.warning("phase=recall elapsed_ms=%d outcome=embedding_unavailable",
                        round((time.perf_counter() - started) * 1000))
            return None
        return np.asarray(got[0], dtype=np.float32)
