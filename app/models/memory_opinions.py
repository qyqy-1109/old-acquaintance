# -*- coding: utf-8 -*-
"""反馈学来的「说话方式倾向」（`D31` / 实测依据见台账 `#88`）。

用户点「她说错了」时，`ChatService.mark_wrong()` 把现场写进 `feedback.jsonl`——
那是**原始证据**，永远保留。这个模块负责把那条反馈**提炼成一句她能照着做的话**，
按「独立命中次数」攒置信度，够强了再送进人格的「待确认」队列，由人拍板。

三条实测结论直接写死在实现里：
  1. **倾向文本必须三件套**：正面做法 ＋ 点名那个失败动作 ＋ 篇幅约束。
     只说「给出明确态度」时，她会在该表态时反问/写长/加铺垫来逃避承诺（实测 0/3）；
     写成「两三句说清你的判断和理由；别反问、别让他先补充背景、别两头都说」才是 3/3。
  2. **注入块的框法不能省**（「不是硬性约束…相关的时候照做…不要点破来源」）：
     去掉它，误用（把无关倾向硬套到别的话题）从 0 涨到 4~6。
  3. **模型自报的 confidence 只当闸门**（≥0.6）——实测它虚高（单条备注就给 0.95）；
     对外展示的置信度用 `1-0.5^独立命中次数`，少样本下才不会显得很有把握。

分层：这里是 Model 层，只认识 `self.db`／`self.client`／`self.cfg`；
**不碰人格文件**——晋升要写的那条 patch 由调用方（Service 层）拿去找
`Personality.record_pending()`，因为"要不要动人设"是人的判断。
"""

from __future__ import annotations

import json
import time

from app.log import get
from app.models.memory_defs import (OPINION_BLOCK, OPINION_MIN_CONF, OPINION_MIN_HITS,
                                    OPINION_PROMPT, OPINION_TOP_N)
from app.utils.llmjson import chat_json

log = get("memory")


def confidence_for(hits: int) -> float:
    """`1 - 0.5^独立命中次数`：每多一次独立命中，剩下的怀疑减半。

    1 次 → 0.5、2 次 → 0.75、3 次 → 0.875。为什么不把这个数存进库：存了就会和
    `hits` 不一致（两条真相）；为什么不用模型自报的 confidence：实测虚高，
    它只配当闸门（见 `learn_opinion`）。
    """
    return round(1 - 0.5 ** max(0, int(hits)), 3)


def opinion_block(items: list[dict]) -> str:
    """注入块。措辞分档：≥0.75 用确定说法，只有 1 次命中时说"好像"。

    没有倾向时返回空串——**不能留一个空气块**（空块会让模型以为"有偏好但要保密"）。
    """
    if not items:
        return ""
    lines = []
    for o in items[:OPINION_TOP_N]:
        prefix = "他不喜欢" if float(o.get("confidence") or 0) >= 0.75 else "他好像更希望"
        lines.append(f"- {prefix}：{o['text']}")
    return OPINION_BLOCK.format(items="\n".join(lines))


def promotion_patch(candidate: dict, current: list) -> dict:
    """把一条够格的倾向转成人格 patch。

    ⚠ 必须是「**原有条目 + 新条目**」：`Personality.evolve()` 对数组是**替换**语义，
    只发新条目等于把 `behavior` 里别的话删掉（`#046` 那次连坐丢失是同类坑）。
    """
    items = list(current or [])
    text = (candidate.get("text") or "").strip()
    if text and text not in items:
        items.append(text)
    return {"behavior": items}


class OpinionMixin:
    """`MemoryStore` 的反馈倾向部分（MRO 见 `memory.py` 的类文档）。"""

    # ---------- 写入：把一次「她说错了」变成倾向 --------------
    def learn_opinion(self, question: str, answer: str, note: str = "",
                      now: float | None = None) -> dict:
        """提炼并 upsert 一条倾向。返回 `{stored, topic, text, hits, confidence, note}`。

        `note` 字段是**给人看的结果说明**：模型不可用、证据太弱、属于事实类，
        都要说清楚——这个按钮以前是"点了没有任何反馈"，那正是 `#84` 的病根（D10）。
        """
        empty = {"stored": False, "topic": "", "text": "", "hits": 0, "confidence": 0.0}
        prompt = OPINION_PROMPT.format(
            question=(question or "（空）")[:500],
            answer=(answer or "（空）")[:800],
            note=(note or "").strip() or "（用户没写备注）")
        self._llm_calls += 1
        data, err = chat_json(
            self.client, self.extract_model,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=300, retries=1,
            no_think=bool(self.cfg.get("no_think")),
        )
        if not isinstance(data, dict):
            log.warning("倾向提炼失败：%s", err)
            self.event("opinion_fail", error=str(err)[:120])
            return {**empty, "note": f"这条这次没能变成倾向（提炼失败：{err}）——"
                                    f"原始证据已经留在 feedback.jsonl 里"}

        topic = str(data.get("topic") or "").strip()
        text = str(data.get("text") or "").strip()
        # 三分类（`#135`）：`kind` 是新字段，`is_style_issue` 仍兼容（旧输出没有 kind 时按它推）。
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in ("style", "fact", "other"):
            kind = "style" if data.get("is_style_issue") else "fact"
        if kind == "other":
            # **不许硬塞成说话偏好**（`#135`）：内容不对／前后矛盾／关系分寸不合适——
            # 既不是"说话方式"、也不是"记错事实"，旧代码里两条路都不对，于是它只能被
            # 硬整理成一条说话偏好（真机实例：「和之前的画像有差距」→「别突然生硬」）。
            # 现在第三条出口：**什么都不记**，但要说人话 + 留痕。
            self.event("opinion_skipped", reason="other", topic=topic[:40])
            return {**empty, "topic": topic, "text": text,
                    "note": "这条不是说话方式、也不是记错事实（属于内容／前后矛盾／分寸那类）——"
                            "**没有硬塞成说话偏好**；原始现场留在 feedback.jsonl 里。"
                            "这类问题得改提示词或改代码，记一条倾向治不了"}
        if kind == "fact":
            # 「她记错了事实」不是说话方式问题：不进 opinions（那是 D31 的边界）。
            # 但它**必须有出口**——以前到这里就结束了，用户点了「她说错了」只收到
            # 一句"不进说话方式倾向"，那条错的事实照样留在库里（T81，`#98`）。
            # 现在登记成一条**待确认的修正**，由人在 `/fix` 里决定要不要作废那条记忆。
            self.event("opinion_skipped", reason="fact", topic=topic[:40])
            rec = self.record_correction(question, answer, topic=topic, note=note)
            return {**empty, "topic": topic, "text": text, "correction": rec.get("id"),
                    "note": "这条属于「她记错了事实」，不进说话方式倾向——"
                            "已登记成**待确认的修正**：`/fix` 看列表，"
                            "`/fix accept N` 把那条被推翻的记忆作废（也可用 /fix drop N 忽略）"}
        if not topic or not text:
            self.event("opinion_skipped", reason="empty")
            return {**empty, "note": "这条没提炼出可用的倾向（备注为空、或对话里看不出你不满什么）"}
        try:
            self_conf = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            self_conf = 0.0
        if self_conf < OPINION_MIN_CONF:
            self.event("opinion_skipped", reason="weak", topic=topic[:40])
            return {**empty, "topic": topic, "text": text,
                    "note": f"这条证据太弱（模型自报 {self_conf:.2f} < {OPINION_MIN_CONF}），先不收"}

        ts = float(now if now is not None else time.time())
        # 「独立命中」的判据是**会话@日期都不同**。为什么不能只看会话 id：真机上会话
        # 永远是 "default"（`MemoryStore(session=...)` 用的是默认值，`/clear` 也不换），
        # 只看会话 id 的话 `hits` 永远到不了 2，晋升门槛形同虚设（这个缺口是落地时
        # 想"生产上可达吗"才发现的，测试抓不到——测试是自己指定 session 的）。
        # 隔天再犯同一类毛病 = 新的一次独立证据，这与用户定的"不同会话/天"一致。
        key = f"{self.session or '-'}@{time.strftime('%Y-%m-%d', time.localtime(ts))}"
        with self._lock:
            row = self.db.execute("SELECT * FROM opinions WHERE topic=?", (topic,)).fetchone()
            if row is None:
                keys, notes, evidence = [], 0, []
                hits = 0
                first_ts = ts
            else:
                keys = json.loads(row["sessions"] or "[]")
                notes = int(row["notes"] or 0)
                evidence = json.loads(row["evidence"] or "[]")
                hits = int(row["hits"] or 0)
                first_ts = row["first_ts"] or ts
            independent = key not in keys
            if independent:
                keys.append(key)
                hits += 1
            if (note or "").strip():
                notes += 1
            evidence.append(ts)
            evidence = evidence[-20:]          # 证据指针只留最近 20 条
            self.db.execute(
                "INSERT INTO opinions(topic,text,hits,sessions,notes,status,first_ts,last_ts,evidence) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(topic) DO UPDATE SET text=excluded.text, hits=excluded.hits, "
                "sessions=excluded.sessions, notes=excluded.notes, last_ts=excluded.last_ts, "
                "evidence=excluded.evidence",
                (topic, text, hits, json.dumps(keys, ensure_ascii=False), notes,
                 "active", first_ts, ts, json.dumps(evidence)))
            self.db.commit()
        conf = confidence_for(hits)
        how = f"第 {hits} 次独立命中" if independent else "同一会话里又提到一次（不算新的独立命中）"
        self.event("opinion_learned", topic=topic[:40], hits=hits, confidence=conf)
        return {"stored": True, "topic": topic, "text": text, "hits": hits,
                "confidence": conf, "independent": independent,
                "note": f"已记下：{topic}（{how}，置信度 {conf}）"}

    # ---------- 读取：注入 / 晋升 / 自查 --------------
    def opinions(self, limit: int = 0, active_only: bool = True) -> list[dict]:
        """按置信度（即独立命中次数）降序取倾向；`limit=0` 表示不限。

        返回值带 `rid`（`rowid`）——它是**停用时的句柄**（`/mem opinion drop N`，`T94`）：
        表的主键是六字中文 `topic`，抄错一个字就失败，`rowid` 短且可复制。
        """
        sql = "SELECT rowid AS rid,topic,text,hits,notes,status,first_ts,last_ts FROM opinions"
        if active_only:
            # promoted 仍然注入：倾向本身照样成立，只是那条人格 patch 还在等人拍板。
            sql += " WHERE status <> 'dropped'"
        sql += " ORDER BY hits DESC, last_ts DESC"
        rows = [dict(r) for r in self.db.execute(sql).fetchall()]
        for r in rows:
            r["confidence"] = confidence_for(r["hits"])
        return rows[:limit] if limit else rows

    def opinion_candidates(self) -> list[dict]:
        """够格晋升的倾向：**≥2 次独立命中 且 至少一条带明确备注**（门槛由用户定，`#88`）。

        返回值带 `patch`（给 `Personality.record_pending`）与 `why`（给人看的理由）。
        """
        out = []
        for o in self.opinions():
            if o["status"] == "promoted" or o["hits"] < OPINION_MIN_HITS or o["notes"] < 1:
                continue
            why = (f"来自 {o['hits']} 次独立命中（其中 {o['notes']} 次带备注），"
                   f"置信度 {o['confidence']}；最近一次 {time.strftime('%m-%d %H:%M', time.localtime(o['last_ts'] or 0))}")
            # ⚠ 这里**不造 patch**：patch 必须是「原有条目 + 新条目」，而 Model 层看不到
            # 人设的 behavior（那是 Service 层的事）。早先这里放过一个
            # `promotion_patch(o, [])` 的"死字段"，正好踩在自己 docstring 警告的那个坑上
            # （只发新条目 = 把 behavior 里别的话替换掉）。调用方用
            # `promotion_patch(cand, current)` 自己拼。
            out.append({**o, "why": why})
        return out

    def mark_opinion_promoted(self, topic: str) -> None:
        with self._lock:
            self.db.execute("UPDATE opinions SET status='promoted' WHERE topic=?", (topic,))
            self.db.commit()

    def opinions_report(self, limit: int = 10) -> str:
        """`/mem opinions` —— 让"她到底从反馈里学到了什么"随时可查。

        每条都印出 `#<rowid>` 句柄：学歪了要能用 `/mem opinion drop N` 停用（`T94`）。
        """
        rows = self.opinions(limit=limit)
        head = "她从反馈里学到的说话偏好"
        if not rows:
            return (f"{head}：还没有。你点「她说错了」并写一句备注，"
                    f"就会开始攒（同一类不满在不同会话里被提到 2 次才够格改人设）。")
        lines = [f"{head}（{len(rows)} 条，按置信度排序）："]
        for r in rows:
            tag = {"promoted": "已进待确认", "active": "生效中"}.get(r["status"], r["status"])
            lines.append(f"- #{r.get('rid', '?')}（{tag}）{r['topic']}：{r['text']}\n"
                         f"    独立命中 {r['hits']} 次 / 带备注 {r['notes']} 次 / 置信度 {r['confidence']}")
        lines.append("（哪条学歪了：`/mem opinion drop <编号>` 停用它——倾向是**每轮注入**的。）")
        return "\n".join(lines)

    def drop_opinion(self, handle) -> str:
        """`/mem opinion drop N` —— **停用**一条学歪的倾向（`T94`）。

        为什么必须有一条正规出口：`opinions()` 只过滤 `status='dropped'`，而在 `#135` 之前
        **代码里没有任何路径把它置成 dropped**（只有 `feedback_corrections` 有 `/fix drop N`）。
        倾向又是**每轮注入**的——于是"提炼器写歪一条"的代价是"她此后每一轮都被这条歪约束压着，
        除非有人去改数据库"。停用**只标状态、不删行**（证据留着），再点一次「她说错了」随时能学回来。
        """
        try:
            rid = int(str(handle).strip().lstrip("#"))
        except (TypeError, ValueError):
            return (f"「{handle}」不是编号。先 `/mem opinions` 看列表，再 "
                    f"`/mem opinion drop <编号>`（编号就是行首那个 #N）。")
        row = self.db.execute(
            "SELECT rowid AS rid,topic,status FROM opinions WHERE rowid=?", (rid,)).fetchone()
        if row is None:
            return f"没有 #{rid} 这条倾向（可能已经被停用了）。`/mem opinions` 看还有哪些。"
        if row["status"] == "dropped":
            return f"#{rid}「{row['topic']}」**已经停用过了**，不用再停。"
        with self._lock, self.db:
            self.db.execute("UPDATE opinions SET status='dropped' WHERE rowid=?", (rid,))
        self.event("opinion_dropped", id=rid, topic=str(row["topic"])[:40])
        return (f"已停用 #{rid}「{row['topic']}」——它不会再被注入（行没删，证据还在）。"
                f"还剩 {len(self.opinions())} 条生效：`/mem opinions` 看列表。")

    # ---------- 事实类反馈：提议作废那条被推翻的记忆（T81） ----------

    def record_correction(self, question: str, answer: str, topic: str = "",
                          note: str = "", now: float | None = None) -> dict:
        """记一条「她说错了」里的事实类反馈（等人在 `/fix` 里拍板）。

        为什么落库而不是只回一句话：事实类反馈原来是死路——`learn_opinion` 判出
        `is_style_issue=false` 就 return，`opinions` 不落任何东西，用户点了之后
        除了"已记下"什么都得不到（而他的期望是"点了就该有用"）。
        这里把它变成一条**可执行的提议**：采纳 = 作废那条被推翻的记忆，忽略 = 结案。
        采纳/忽略都留 `status` 与 `result`，所以"这条反馈后来怎么样了"随时可查。
        """
        ts = float(now if now is not None else time.time())
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO feedback_corrections(ts,question,answer,topic,note,status)"
                " VALUES(?,?,?,?,?,'pending')",
                (ts, (question or "")[:500], (answer or "")[:1500],
                 (topic or "")[:60], (note or "")[:300]))
            cid = int(cur.lastrowid or 0)
        self.event("correction_pending", id=cid, topic=(topic or "")[:40])
        return {"id": cid, "stored": True}

    def pending_corrections(self, status: str = "pending") -> list[dict]:
        """待确认的修正（默认只看还没处理的）。"""
        rows = self.db.execute(
            "SELECT id,ts,question,answer,topic,note,status,result FROM feedback_corrections "
            "WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_correction(self, index: int, accept: bool) -> str:
        """`/fix accept N` / `/fix drop N`。

        **采纳 = 作废那条被推翻的记忆**（软删归档，`D4` 的默认方向：留档可查
        "当时是怎么说的"），并且**只依据问题里出现的原文**——不猜、不靠相似度
        去挑一条"最像的"删掉。猜错的代价是删掉一条真记忆，那比不做更糟（D12）。
        因此：能从库里找到原文才可采纳；找不到就明确说"我没找到那条记忆，
        可能已经被作废了，或它不在库里"，并把这条件记进 `result`。
        """
        pend = self.pending_corrections()
        if index < 1 or index > len(pend):
            return f"序号要在 1 到 {len(pend)} 之间（用 /fix 看列表）"
        row = pend[index - 1]
        if not accept:
            with self._lock, self.db:
                self.db.execute("UPDATE feedback_corrections SET status='dropped',"
                                " resolved_at=?, result=? WHERE id=?",
                                (time.time(), "人工忽略", row["id"]))
            self.event("correction_dropped", id=row["id"])
            return f"已忽略第 {index} 条（她只是记错了事实，不动记忆）"

        # 找那条被推翻的记忆：优先"问答里原样出现"的活跃条目
        texts = [m for m in self.memories if m.get("text")]
        blob = f"{row.get('question') or ''}\n{row.get('answer') or ''}"
        hit = next((m for m in texts if m["text"] and m["text"] in blob), None)
        if hit is None:
            with self._lock, self.db:
                self.db.execute("UPDATE feedback_corrections SET status='unmatched',"
                                " resolved_at=?, result=? WHERE id=?",
                                (time.time(), "没在库里找到可作废的原文", row["id"]))
            self.event("correction_unmatched", id=row["id"])
            return ("这条我**没找到**可作废的记忆（可能已经不在库里，或原文对不上）——"
                    "没有动任何数据。要按内容删，用 `/mem del <序号>` 自己挑。")

        n = self.drop_memories([hit["text"]], reason="user-feedback")
        with self._lock, self.db:
            self.db.execute("UPDATE feedback_corrections SET status='accepted',"
                            " resolved_at=?, result=? WHERE id=?",
                            (time.time(), f"已作废：{hit['text'][:60]}", row["id"]))
        self.event("correction_accepted", id=row["id"], text=hit["text"][:60])
        return (f"已采纳第 {index} 条：作废了「{hit['text']}」"
                f"（{n} 条，软删归档，时间轴上仍能看到它曾经存在）")

    def corrections_report(self, limit: int = 10) -> str:
        """`/fix` —— 事实类反馈的出口。"""
        pend = self.pending_corrections()[:limit]
        others = self.db.execute(
            "SELECT count(*) c FROM feedback_corrections WHERE status<>'pending'").fetchone()["c"]
        if not pend and not others:
            return ("还没有「事实类」的待确认修正。你点「她说错了」时，"
                    "属于**她记错事实**的那类会被登记在这里；属于**说话方式**的那类走 /mem opinions。")
        lines = []
        if pend:
            lines.append(f"待确认的修正（{len(pend)} 条）——采纳会用 `/fix accept N`，"
                         f"忽略用 `/fix drop N`：")
            for i, r in enumerate(pend, 1):
                lines.append(f"{i}. 你说：{r['question']}｜她答：{(r['answer'] or '')[:60]}"
                             f"\n   备注：{r['note'] or '（无）'}"
                             f"\n   → 采纳 = 把那条被她推翻的记忆作废（软删，可查）")
        if others:
            lines.append(f"（另有 {others} 条已处理：/fix all 看全部）")
        return "\n".join(lines)

    def corrections_all(self, limit: int = 20) -> str:
        """`/fix all` —— 含已处理的那部分，方便回看"我点过的反馈后来怎么了"。"""
        rows = self.db.execute(
            "SELECT id,ts,topic,status,result FROM feedback_corrections ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        if not rows:
            return "还没有事实类反馈的记录"
        tag = {"pending": "待确认", "accepted": "已采纳", "dropped": "已忽略",
               "unmatched": "没找到对应记忆"}
        return "\n".join(f"- #{r['id']}（{tag.get(r['status'], r['status'])}）"
                         f"{r['topic'] or ''} {r['result'] or ''}" for r in rows)
