# -*- coding: utf-8 -*-
"""她主动开口（`#108`）：从"只能你先说"变成"她也会来找你"。

原理（用户问的就是这个）：现在是纯请求/响应——只有用户发消息才跑一轮。
要让她主动，得补三件事：

1. **有人替她判断"现在想不想说"** → 后台一个低频线程（`main.py` 的 lifespan 起），
   空闲时也过一遍闸门；
2. **她说了、但你可能不在** → 存进 `pending_say` 待发表，前端打开页面时轮询取走；
3. **不能一直说** → 一串**可数**的闸门（下面 `should_speak()`），
   全是纯本地判断，**只有全过了才调模型**（省钱也是纪律）。

三条内容纪律（沿用项目一贯的反幻觉取向）：
· 生活事实来自她的状态与流水；关心对方只依据他明确说过的话；
· **不编"你们共同的经历"**，也不编没记录的生活事件；
· 她可以决定暂不说；开口时也允许**没有问句**。

**另外：说几条也由她自己定**（`T86`）。像正常人发消息那样——一句话就说完的发一条，
确实有两三句才分（`@@@` 分隔，见 `app/utils/bubbles.py`）。这里**只负责把标记切干净**，
不替她决定说几条；上限与主对话是同一条契约（3 条）。这与"她的生活每一格她自己选"
是同一个取向：**习惯由她自己的选择累积出来，不是脚本**。

和记忆层的边界（设计时定的）：她说的话**进 `messages`**（下轮上下文才连贯），
但**不进 `memories`**——避免"她自己说过的话"被当成事实反复强化。
"""

from __future__ import annotations

import re
import json
import hashlib
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from app import config
import threading
import time

from app.utils import textsim
from app.utils.bubbles import split_messages

from app.log import get

log = get("speak")

SPEAK_PROMPT = """你是「{her}」。现在是{now}（{slot_label}）。

{personality}
【本次接话的起点】
{focus}
【你自己此刻的状态】{state}
【你最近的生活】（越靠上越新）
{recent}
【你一贯喜欢和在意的事】（背景偏好，不是已发生的记录）{tastes}；{cares}
【你和他现在的关系】（`#146` 关系层，慢变，手写——称呼照它来）
{relation}
【到了约定时间、还待确认的承诺】
{due_promises}
【你和他最近说过的话】
{chat}
【你最近主动说过的话】（**别重复这些**）
{said}

现在有一次主动联系他的机会，可以说想说的话，也可以暂不说。

他正在讲自己的事时，优先接他在意的地方。已好转、了结或不想再聊的事就翻篇。

报备、分享已确认近况或源于偏好的想法都可以；想跟他说话本身也是理由。

承诺到了时间，只代表这件事还待确认；可以自然地提起，也可以暂不说。
不要因为到点就编成已经做到了，也不要自动认定失约。
若提起某条承诺，在一句话中完整保留上面的承诺原文，再用你自己的语气接话；
JSON 的 reminder_ids 只列这次确实提到的承诺编号，没提就给空数组。

不重复最近主动说过的话，也不发没有内容的「在吗」。
说几条由你自己定：一句话能说完就发一条，确实需要时分成 **1~3 条**，
每条之间**单独占一行**写三个 `@`（`@@@`），
必须是三个 `@`、必须在**行首**；写成别的符号会被**原样发到对方眼前**。
**别为了分条而分条**，也别把一句话硬掰成两条。

要求：
- 生活事实只能来自明确的状态和生活记录；系统兜底候选、含「或者」的活动、偏好和计划
  都不证明某件事已发生，不能据此说「刚打完球」，更不能添「两场」这类结果细节。
  未确认时可以表达想法，或只接他的话。提到他的情况，只能依据"他："明确说过的话，
  不要猜他后来怎样了，也不要编没发生过的经历。
- 共同经历只回扣当前提供的明确记录：近期用户原话、生活或关系记录写明发生过才提，
  没有记录就不补细节；可以说现在想一起做什么，不把想法说成做过。
- **你自己家里的事有个分寸**：你**人在家**时，屋里什么样、团子在不在家、有没人敲门
  你自己看得见——直接说就行，**别反问他**；你**不在家**时你确实不知道，那就
  **如实说不知道**（「我还在学校呢，没回去看」），**别问他**「团子是不是又跑我院里
  去了」——他不在你家，答不上来；**你自己干的事也一样**（你刚煮面把蛋壳掉进锅里，
  就别问他「蛋壳挑出来没」）。
  **点名说清**：**团子是你的猫、球衣是你的球衣、课表是你的课表、出租屋是你住的地方**——
  这些**只有你知道**，永远不要拿它们问他（`#146`：提示词写了三处仍漏过一次，机器也会拦）。
  要问就问**他那边**的事（今天怎么样、吃没吃饭）。
- **你自己刚说过的话要算数**：别翻案。刚说过「炖好给你端过去」，下一条就不能改成
  「你过来拿」——二选一，挑定了就别翻；真改主意也要明说（「我刚想了一下，还是你过来吧」）。
- 语气跟平时一样：口语、放松，让他知道你为什么想跟他说这句话；短句为主，
  需要时把在意的话说完整，不用为了凑字数截断语气，也不用长篇总结。
- **可以没有问句**（自言自语也行）；也可以顺口问一句，但别每次都用问句结尾。
- 别提"我主动找你""打扰你"这类meta的话。
- 不要复述天气、不要播报时间。

只输出 JSON。想说：{{"speak": true, "text": "你要说的话；要分条就在两条之间单独占一行写 @@@", "reminder_ids": []}}
暂不说：{{"speak": false, "text": "", "reminder_ids": []}}
"""


_GAME_COMPLETION = re.compile(
    r"打完(?:了)?(?P<finished_count>[0-9一二两三四五六七八九十]+)场(?:篮球|球)?"
    r"|打完(?:了)?(?:篮球|球)"
    r"|打(?:了|过)(?P<played_count>[0-9一二两三四五六七八九十]+)场(?:篮球|球)?")


def _completed_games(text: str) -> list[int | str | None]:
    """识别自己已打完球及场数；否定、计划、条件与他人归属不作完成证据。"""
    result = []
    for clause in re.split(r"[，,。！!？?；;\n]", str(text or "")):
        if "球" not in clause:
            continue
        for match in _GAME_COMPLETION.finditer(clause):
            prefix, suffix = clause[:match.start()], clause[match.end():]
            owners = re.findall(r"[我你您他她]", prefix)
            if owners and owners[-1] != "我":
                continue
            if not owners and re.search(r"同学|室友|朋友|老师", prefix):
                continue
            if re.match(r"的[你您他她]", suffix):
                continue
            if re.search(r"没(?:有)?|未|不|想|准备|打算|计划|等|要|会|如果|假如|待会|明天|下次", prefix):
                continue
            if re.match(r"(?:了)?(?:就|再|后|以后|之后)", suffix) and not re.search(
                    r"刚|已经|方才|总算|终于", prefix):
                continue
            count = match.group("finished_count") or match.group("played_count")
            if count is not None:
                numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                count = int(count) if count.isdigit() else numerals.get(count, count)
            result.append(count)
    return result


class Speaker:
    """判断"该不该开口" + 生成那句话 + 待发表读写。"""

    def __init__(self, db, life, session: str = "default", lock_source=None,
                 personality_source=None, motive_mode: bool = False,
                 motive_limit: int = 4, motive_ttl_days: float = 7):
        self.db = db
        # `B3`（工单二）：同 `life.py` —— 待发表/时间戳的写入要和 `MemoryStore` 共用同一把
        # RLock（db.py:186-190 的契约），否则共享连接上会与对话路径事务交错。
        # 同样**现取不缓存**：`lock_source` 是 `MemoryStore`（替换它要能被看见，见 life.py）。
        self._lock_source = lock_source
        self._own_lock = threading.RLock()
        self.life = life
        self._personality_source = personality_source
        self.session = session or "default"
        self.motive_mode = bool(motive_mode)
        self.motive_limit = max(1, int(motive_limit))
        self.motive_ttl_days = max(1.0, float(motive_ttl_days))
        with self._write_lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS proactive_motive_claims (
                material_key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                kind TEXT NOT NULL, source_revision TEXT, source_digest TEXT,
                claimed_at REAL NOT NULL, batch_id TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS proactive_batches (
                id TEXT PRIMARY KEY, ts REAL NOT NULL, n INTEGER NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS pending_motive_sources (
                pending_id INTEGER NOT NULL, material_key TEXT NOT NULL,
                candidate_id TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL,
                source_revision TEXT, source_digest TEXT, expires_at REAL,
                status TEXT NOT NULL DEFAULT 'active', invalidated_at REAL,
                PRIMARY KEY(pending_id,material_key), FOREIGN KEY(pending_id) REFERENCES pending_say(id))""")
        self.last_decision: bool | None = None
        self.last_reminders: dict[str, list[str]] = {}
        self.last_promise_mentions: dict[str, list[str]] = {}
        self.last_grounding: list[str] = []
        self.last_motives: list[dict] = []
        self.last_motive_ids: list[str] = []

    @property
    def _write_lock(self):
        """`B3`：写库要用的锁——**动态取**（理由见 `__init__` 里的注释）。"""
        source = getattr(self, "_lock_source", None)
        lock = getattr(source, "_lock", None) if source is not None else None
        return lock if lock is not None else self._own_lock

    @contextmanager
    def _motive_transaction(self):
        outer = self.db.in_transaction
        name = "motive_" + uuid.uuid4().hex
        if outer:
            self.db.execute(f"SAVEPOINT {name}")
            self.db.execute("UPDATE life_state SET id=id WHERE id=1")
        else:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if outer:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
            else:
                self.db.rollback()
            raise
        else:
            if outer:
                self.db.execute(f"RELEASE {name}")
            else:
                self.db.commit()

    @staticmethod
    def _material_digest(*values) -> str:
        return hashlib.sha256(json.dumps(values, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()

    def _fresh_promises(self, now: float, due_only: bool = True) -> list[dict]:
        relation = getattr(self, "relationship", None)
        if relation is None:
            return []
        try:
            with relation._lock:
                data = json.loads(Path(relation.path).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("promises", []), list):
                raise ValueError("承诺文件格式无效")
            result = []
            for row in data.get("promises", []):
                if not isinstance(row, dict) or row.get("status") != "open":
                    continue
                if due_only and (not 0 < float(row.get("due_at") or 0) <= now
                                 or float(row.get("reminded_at") or 0) > 0):
                    continue
                result.append(dict(row))
            return result
        except Exception as exc:
            log.warning("读取当前承诺来源失败（这次不提醒）：%s: %s", type(exc).__name__, exc)
            return []

    def mark_motive_reminded(self, promise_id: str, now: float | None = None) -> bool:
        now = float(now if now is not None else time.time())
        relation = getattr(self, "relationship", None)
        if relation is None:
            return False
        with relation._lock:
            fresh = {str(row["id"]): row for row in self._fresh_promises(now)}
            if str(promise_id) not in fresh:
                log.warning("承诺提醒已入队但当前承诺已变化，本次不覆盖：%s", promise_id)
                return False
            try:
                current = json.loads(Path(relation.path).read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    raise ValueError("关系文件必须是对象")
                relation.data = current
                return relation.mark_promise_reminded(promise_id, now=now)
            except Exception as exc:
                log.warning("承诺提醒记账失败，待发保留：%s: %s", type(exc).__name__, exc)
                return False

    def motive_candidates(self, now: float | None = None, limit: int | None = None) -> list[dict]:
        """Return bounded, source-backed materials eligible for one proactive turn."""
        now = float(now if now is not None else time.time())
        limit = self.motive_limit if limit is None else max(1, int(limit))
        rows: list[dict] = []
        book = getattr(self._lock_source, "continuity", None)
        if book is not None:
            for item in book.items():
                if item.get("status") != "open" or not item.get("source_valid"):
                    continue
                if item.get("expires_at") is not None and float(item["expires_at"]) <= now:
                    continue
                if item.get("eligible_at") is not None and float(item["eligible_at"]) > now:
                    continue
                kind = "choice" if item.get("actor") == "assistant" else "topic"
                material_key = f"continuity:{item['id']}:r{item.get('revision', 1)}"
                rows.append({"id": material_key, "kind": kind, "text": str(item.get("quote") or item.get("text") or ""),
                             "topic": item.get("topic", ""), "state": item.get("state", "experience"),
                             "material_key": material_key, "source_id": item["id"],
                             "source_revision": str(item.get("revision", 1)),
                             "source_digest": item.get("source_digest", ""), "updated_at": float(item.get("updated_at") or 0),
                             "expires_at": item.get("expires_at"), "eligible_at": item.get("eligible_at")})
        try:
            with self._write_lock:
                events = self.db.execute(
                    "SELECT id,ts,text,source,hidden_at FROM life_events WHERE hidden_at IS NULL "
                    "AND source IN ('model','conversation','dedup') ORDER BY id DESC LIMIT ?",
                    (max(limit * 2, 8),)).fetchall()
        except Exception as exc:
            log.warning("读取主动生活素材失败：%s: %s", type(exc).__name__, exc)
            events = []
        for event in events:
            stamp = float(event["ts"] or 0)
            if stamp <= 0 or stamp > now or now - stamp > self.motive_ttl_days * 86400:
                continue
            digest = self._material_digest(event["id"], stamp, event["text"], event["source"])
            material_key = f"life:{int(event['id'])}:{digest[:16]}"
            rows.append({"id": material_key, "kind": "life", "text": str(event["text"]),
                         "material_key": material_key, "source_id": int(event["id"]),
                         "source_revision": str(event["ts"]), "source_digest": digest,
                         "updated_at": stamp, "expires_at": stamp + self.motive_ttl_days * 86400,
                         "eligible_at": stamp})
        relation = getattr(self, "relationship", None)
        if relation is not None:
            try:
                for promise in self._fresh_promises(now):
                    text = str(promise.get("text") or "").strip()
                    if not text:
                        continue
                    material_key = f"promise:{promise['id']}"
                    rows.append({"id": material_key, "kind": "promise", "text": text,
                                 "material_key": material_key, "source_id": promise["id"],
                                 "source_revision": str(promise.get("status", "open")),
                                 "source_digest": self._material_digest(promise["id"], text,
                                     promise.get("created_at"), promise.get("due_at")),
                                 "updated_at": float(promise.get("due_at") or 0),
                                 "expires_at": None, "eligible_at": promise.get("due_at")})
            except Exception as exc:
                log.warning("读取主动承诺素材失败：%s: %s", exc)
        rows.sort(key=lambda item: (float(item.get("updated_at") or 0), item["id"]), reverse=True)
        with self._write_lock:
            claimed = {row[0] for row in self.db.execute(
                "SELECT material_key FROM proactive_motive_claims").fetchall()}
        return [item for item in rows if item["material_key"] not in claimed][:limit]

    def _motive_current(self, item: dict, now: float, delivery: bool = False) -> bool:
        """Revalidate a selected snapshot immediately before claiming it."""
        if item.get("expires_at") is not None and float(item["expires_at"]) <= now:
            return False
        kind = item.get("kind")
        if kind in {"topic", "choice"}:
            book = getattr(self._lock_source, "continuity", None)
            source = book.get(item.get("source_id")) if book is not None else None
            return bool(source and source.get("status") == "open" and source.get("source_valid")
                        and str(source.get("revision")) == str(item.get("source_revision"))
                        and source.get("source_digest") == item.get("source_digest")
                        and (source.get("expires_at") is None or float(source["expires_at"]) > now)
                        and (source.get("eligible_at") is None or float(source["eligible_at"]) <= now))
        if kind == "life":
            row = self.db.execute("SELECT ts,text,source,hidden_at FROM life_events WHERE id=?",
                                  (int(item.get("source_id")),)).fetchone()
            return bool(row and not row["hidden_at"] and row["source"] in ("model", "conversation", "dedup")
                        and 0 < float(row["ts"] or 0) <= now
                        and now - float(row["ts"]) <= self.motive_ttl_days * 86400
                        and self._material_digest(int(item["source_id"]), float(row["ts"]), row["text"], row["source"])
                        == item.get("source_digest"))
        if kind == "promise":
            relation = getattr(self, "relationship", None)
            if relation is None:
                return False
            sources = self._fresh_promises(now, due_only=not delivery)
            return any(str(row.get("id")) == str(item.get("source_id"))
                       and row.get("status") == "open"
                       and self._material_digest(row["id"], str(row.get("text") or ""),
                                                row.get("created_at"), row.get("due_at")) == item.get("source_digest")
                       for row in sources)
        return False

    def enqueue_motivated(self, texts: list[str], candidates: list[dict],
                          now: float | None = None, quiet_min: float = 5,
                          cfg: dict | None = None) -> list[int]:
        """Atomically claim selected materials and insert their pending bubbles."""
        now = float(now if now is not None else time.time())
        selected = {str(item.get("id")): item for item in (candidates or [])
                    if isinstance(item, dict) and item.get("id")}
        if not selected or not texts:
            return []
        lock = self._write_lock
        relation_lock = getattr(getattr(self, "relationship", None), "_lock", None)
        with (relation_lock or nullcontext()), lock, self._motive_transaction():
            cap = int((cfg or {}).get("daily_max", config.PROACTIVE_DAILY_MAX))
            if self.motive_mode and cap > 0 and self._said_today(now) >= cap:
                return []
            last_user = self._ts("user_last_ts")
            if last_user and quiet_min > 0 and now - last_user < quiet_min * 60:
                return []
            if cfg is not None:
                last_her = self._ts("her_last_ts")
                if last_her and quiet_min > 0 and now - last_her < quiet_min * 60:
                    return []
                gap_h = float(cfg.get("min_gap_hours", 0) or 0)
                if last_her and gap_h > 0 and now - last_her < gap_h * 3600:
                    return []
                if not cfg.get("enabled", True):
                    return []
                lo, hi = cfg.get("quiet_hours", (0, 8))
                hour = time.localtime(now).tm_hour
                if (lo <= hour or hour < hi) if lo > hi else (lo <= hour < hi):
                    return []
            valid = [item for item in selected.values() if self._motive_current(item, now)]
            if len(valid) != len(selected):
                return []
            material_keys = [str(item["material_key"]) for item in valid]
            marks = ",".join("?" for _ in material_keys)
            if self.db.execute("SELECT 1 FROM proactive_motive_claims WHERE material_key IN (" + marks + ") LIMIT 1",
                               material_keys).fetchone():
                return []
            batch_id = uuid.uuid4().hex
            self.db.execute("SAVEPOINT proactive_enqueue")
            for item in valid:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO proactive_motive_claims(material_key,candidate_id,kind,source_revision,source_digest,claimed_at,batch_id) VALUES(?,?,?,?,?,?,?)",
                    (item["material_key"], item["id"], item["kind"], str(item.get("source_revision") or ""),
                     str(item.get("source_digest") or ""), now, batch_id))
                if cur.rowcount != 1:
                    self.db.execute("ROLLBACK TO proactive_enqueue")
                    self.db.execute("RELEASE proactive_enqueue")
                    return []
            ids = []
            for text in texts:
                cur = self.db.execute("INSERT INTO pending_say(ts,text,her_name) VALUES(?,?,?)",
                                      (now, str(text), getattr(self.life, "her_name", "") or ""))
                pending_id = int(cur.lastrowid or 0)
                ids.append(pending_id)
                for item in valid:
                    self.db.execute("INSERT INTO pending_motive_sources(pending_id,material_key,candidate_id,source_id,kind,source_revision,source_digest,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (pending_id, item["material_key"], item["id"], str(item.get("source_id") or ""), item["kind"],
                                     str(item.get("source_revision") or ""), str(item.get("source_digest") or ""), item.get("expires_at")))
            self.db.execute("INSERT INTO proactive_batches(id,ts,n) VALUES(?,?,?)", (batch_id, now, len(ids)))
            self.db.execute("INSERT INTO events(ts,kind,session,payload) VALUES(?,?,?,?)",
                            (now, "proactive", self.session, json.dumps({"n": len(ids), "batch_id": batch_id}, ensure_ascii=False)))
            self.db.execute(
                "INSERT INTO life_state(id,updated_at,her_last_ts) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET her_last_ts=excluded.her_last_ts",
                (now, now))
            self.db.execute("RELEASE proactive_enqueue")
            return ids

    def drop_invalid_motives(self, items: list[dict]) -> list[dict]:
        """Invalidate stale source-linked pending rows before client delivery."""
        kept = []
        for row in list(items or []):
            links = self.db.execute("SELECT * FROM pending_motive_sources WHERE pending_id=?",
                                    (int(row["id"]),)).fetchall()
            if not links:
                kept.append(row)
                continue
            if any(link["status"] != "active" for link in links):
                continue
            invalid = []
            for link in links:
                candidate = {"id": link["candidate_id"], "kind": link["kind"],
                             "material_key": link["material_key"], "source_id": link["source_id"],
                             "source_revision": link["source_revision"], "source_digest": link["source_digest"],
                             "expires_at": link["expires_at"]}
                if not self._motive_current(candidate, time.time(), delivery=True):
                    invalid.append(link)
            if not invalid:
                kept.append(row)
                continue
            with self._write_lock, self.db:
                changed = self.db.execute(
                    "UPDATE pending_motive_sources SET status='invalidated',invalidated_at=? "
                    "WHERE pending_id=? AND status='active'", (time.time(), int(row["id"]))).rowcount
            recorder = getattr(self._lock_source, "event", None)
            if changed and callable(recorder):
                recorder("pending_motive_invalid", pending_id=int(row["id"]),
                         candidate_ids=[link["candidate_id"] for link in invalid])
        return kept

    def _has_fresh_motive(self, since_ts: float, now: float | None = None) -> bool:
        now = float(now if now is not None else time.time())
        return any(float(row.get("updated_at") or 0) > float(since_ts or 0)
                   for row in self.motive_candidates(now=now))

    # ---------- 闸门（全是纯本地判断，过了才调模型）----------
    def should_speak(self, now: float | None = None,
                     cfg: dict | None = None) -> tuple[bool, str, dict]:
        """返回 `(该不该说, 原因, 关键数字)`。原因**无论成败都要给**——失败要可见。

        第三个返回值是"为什么"的可量化版本（`gate` = 闸门身份、`since_min` = 距上次有人
        说话几分钟、`said`/`cap` = 今天开口几次、`chance` = 这一轮算出来的概率），
        给 `proactive_skip` 埋点与 `/mem events` 用。**`gate` 是去重键**：原因文本里带
        数字（"已过 0.16 → 0.17 小时"），拿文本去重等于每条都算"变了"——实测 30 秒一次
        的轮询会把 `events`（上限 5000 条）冲掉。

        概率曲线由用户 2026-09-30 定稿；抽中表示获得判断机会，见 `app/config.py`。
        """
        cfg = cfg or {}
        now = float(now if now is not None else time.time())
        cap = int(cfg.get("daily_max", 6))
        said = self._said_today(now)
        last_her = self._ts("her_last_ts")
        last_user = self._ts("user_last_ts")
        # 起算点 = **最近一次有人说话**（他说的 / 她上次主动说的）。
        # 她实际开口后从头开始；只检查或选择沉默不重置。
        last_any = max([t for t in (last_user, last_her) if t] or [0]) or None
        since_min = (now - last_any) / 60.0 if last_any else None
        detail = {"gate": "", "said": said, "cap": cap, "chance": 0.0,
                  "since_min": None if since_min is None else round(since_min, 1)}

        def no(gate, why):
            detail["gate"] = gate
            # 去重用的**粗分类**：`dice`（这轮没抽中）与 `roll_window`（同窗不重复抽）
            # 都属于"在等下一轮"，会交替出现；按精确 gate 去重等于每轮都算"变了"——
            # 实测每分钟 2 条 ≈ 一天 2880 条，`events`（上限 5000）两天就被它们冲掉。
            detail["gate_class"] = "waiting" if gate in ("dice", "roll_window") else gate
            return False, why, detail

        # ① 开关
        if not cfg.get("enabled", True):
            return no("disabled", "主动说话关着（WB_PROACTIVE=0）")

        # 静默时段（夜里不吵）
        quiet = cfg.get("quiet_hours", (0, 8))
        hour = time.localtime(now).tm_hour
        lo, hi = quiet
        if (lo <= hour or hour < hi) if lo > hi else (lo <= hour < hi):
            return no("quiet_hours", f"静默时段（{lo}:00–{hi}:00）")

        # ② 日上限：按**开口次数**（说满即停）
        if cap > 0 and said >= cap:
            return no("daily_cap", f"今天已经主动开口 {said}/{cap} 次（上限）")

        # ③ 可选：距上次开口至少隔 N 小时（默认 0 = 关）。防连发主要靠"抽中一次从头开始"。
        gap_h = float(cfg.get("min_gap_hours", 0) or 0)
        if gap_h > 0 and last_her and (now - last_her) < gap_h * 3600:
            return no("min_gap", f"距上次主动说话不足 {gap_h:g} 小时"
                                 f"（已过 {(now - last_her) / 3600:.2f} 小时）")

        # ⑤ 对话下限：刚聊完这几分钟**根本不抽**（用户："对话的时候不进行这个线程"）
        quiet_min = float(cfg.get("quiet_min", 5) or 0)
        if since_min is not None and since_min < quiet_min:
            return no("in_conversation",
                      f"刚聊过（{since_min:.1f} 分钟前；对话中不抽，下限 {quiet_min:g} 分钟）")

        # ④ **这一格报备过没有**（`#144`，女友向）。
        # 用户要的是「报备 + 有可以和我分享的事情」——报备的天然单位就是**时段**：
        # 这一格的事跟他说过就不再重复，换一格（或期间真有新流水）才允许再开口。
        # 一天 7 格、日上限 6 次，两者正好对得上。
        # 旧的判据是"有没有新流水"——配合旧提示词里那句"例行报备就别说了"，
        # 结果就是她**憋着不报**；而一旦放开，又必须防她在一格里反复说同一件事。
        import datetime as _dt

        from app.models.life import SLOT_LABEL as _SLOT_LABEL
        from app.models.life import slot_of as _slot_of
        cur_slot = _slot_of(_dt.datetime.fromtimestamp(now))
        if not self.motive_mode and last_her and _slot_of(_dt.datetime.fromtimestamp(last_her)) == cur_slot \
                and not self._has_fresh_life(last_her) and not self._due_promises(now) \
                and not self._has_fresh_motive(last_her, now):
            return no("reported_this_slot",
                      f"这一格（{_SLOT_LABEL.get(cur_slot, cur_slot)}）已经报备过了")

        declined_at = self._ts("declined_at")
        decision_gap = float(cfg.get("decision_gap_min", 30) or 0)
        guaranteed_at = (last_any + float(cfg.get("high_min", 60) or 60) * 60
                         if last_any else 0)
        if (decision_gap > 0 and declined_at and last_her <= declined_at
                and now - declined_at < decision_gap * 60
                and last_user <= declined_at
                and not (declined_at < guaranteed_at <= now)
                and not self._has_fresh_life(declined_at)
                and not self._has_fresh_motive(declined_at, now)):
            return no("decision_wait", "她刚决定暂不说，稍后再看有没有想聊的")

        # ⑥ 概率曲线（用户定稿的形状）
        low_min = float(cfg.get("low_min", 30) or 30)
        low_p = float(cfg.get("low_chance", 0.05))
        high_min = float(cfg.get("high_min", 60) or 60)
        high_p = float(cfg.get("high_chance", 0.80))
        ceil_p = float(cfg.get("chance_ceil", 1.0))
        s = since_min if since_min is not None else high_min     # 没有记录 → 当作已过上限
        if s < low_min:
            p = low_p * (s / low_min) if low_min > 0 else low_p
        elif s < high_min:
            p = (high_p if high_min <= low_min else
                 high_p + (ceil_p - high_p) * ((s - low_min) / (high_min - low_min)))
        else:
            p = ceil_p
        p = max(0.0, min(1.0, p))
        detail["chance"] = round(p, 3)

        # ⑦ 抽取：**按时间窗**（用户："每分钟抽取"）。
        # 为什么不"每次 tick 抽一次"：`/api/pending` 被前端每 30 秒轮询一次，
        # 而后台心跳是每分钟——同一套概率下触发率会取决于前端轮询频率，那就不叫可数的闸门。
        every = float(cfg.get("roll_every_min", 1) or 0)
        last_roll = self._ts("roll_ts")
        if every > 0 and last_roll and (now - last_roll) < every * 60:
            return no("roll_window", f"这一轮时间窗内已经抽过了（每 {every:g} 分钟一次）")

        import random
        self._set_ts("roll_ts", now)      # 抽了就记下（不管中不中，避免同一窗反复抽）
        if random.random() > p:
            return no("dice", f"这次没抽中（概率 {int(round(p * 100))}%）")
        return True, "可以说", detail

    # ---------- 生成 ----------
    def speak(self, client, model: str, chat_lines: list[str] | None = None,
              now: float | None = None) -> list[str]:
        """让她决定是否说话；选择开口时生成 **1~3 条**，沉默或失败返回空列表。

        说几条是**她自己选的**（提示词里给了她这个选择），这里只做一件事：
        把 `@@@` 标记切干净、限长。切出来的文本**不含标记**——`pending_say` 里的
        每一行都是能直接显示给她看的干净气泡（前端轮询到就渲染，不做二次切分）。
        """
        from app.models.life import SLOT_LABEL, slot_of
        from app.utils.llmjson import chat_json
        import datetime as dt

        self.last_decision = None
        self.last_reminders = {}
        self.last_promise_mentions = {}
        self.last_grounding = []
        self.last_motives = []
        self.last_motive_ids = []
        self.last_duplicates: list[str] = []      # 判重丢掉的那些（给埋点用，`#138`）
        self.last_ownership: list[str] = []       # 归属兜底丢掉的那些（`#146`）
        now = float(now if now is not None else time.time())
        when = dt.datetime.fromtimestamp(now)
        st = self.life.state() or {}
        script = self.life.script()
        state = "、".join(x for x in (
            f"在{st.get('place')}" if st.get("place") else "",
            str(st.get("activity") or ""),
            f"心情{st.get('mood')}" if st.get("mood") else "",
            f"心里挂着{st.get('on_mind')}" if st.get("on_mind") else "") if x) or "（未记录）"
        events = self.life.recent_events(6)
        fallback_state = any(event.get("source") == "fallback"
                             and float(event.get("ts") or 0) == float(st.get("advanced_at") or 0)
                             and event["text"] == f"{SLOT_LABEL.get(st.get('slot'), '')}：{st.get('activity')}"
                             for event in events)
        if fallback_state:
            state = "（活动未确认：系统兜底候选，不证明已经发生）" + state
        elif re.search(r"或者|可能", str(st.get("activity") or "")):
            state = "（活动未确认：包含候选，不证明已经发生）" + state
        labels = {"fallback": "系统兜底候选，未确认发生", "conversation": "对话中确认",
                  "catchup_summary": "分段补记摘要，只支持概况不支持某格具体完成",
                  "model": "生活记录", "dedup": "生活记录"}
        recent = "\n".join(f"- （{labels.get(e.get('source'), '来源未确认')}）{e['text']}"
                           for e in events) or "（还没有）"
        said = "\n".join(f"- {t}" for t in self._recent_said(5)) or "（还没主动说过）"
        chat = "\n".join(chat_lines or []) or "（最近没聊过）"
        focus = next((str(line).strip() for line in reversed(chat_lines or [])
                      if str(line).strip().startswith("他：")),
                     "（没有近期用户原话，可以分享自己的想法或已确认近况）")
        due_promises = self._due_promises(now)
        due_text = "\n".join(f"[{row['id']}] {row['text']}" for row in due_promises) or "（没有）"
        motives = self.motive_candidates(now=now) if self.motive_mode else []
        self.last_motives = motives
        if self.motive_mode and not motives:
            log.info("主动说话跳过：当前没有可核验的主动缘由")
            return []
        motive_text = "\n".join(
            json.dumps({"id": row["id"], "kind": row["kind"], "text": row["text"],
                        "state": row.get("state", ""), "topic": row.get("topic", "")}, ensure_ascii=False)
            for row in motives) or "（没有可核验的主动缘由；本轮不生成）"

        personality = self._personality_source() if self._personality_source is not None else None
        prompt = SPEAK_PROMPT.format(
            her=personality.display_name() if personality is not None else self.life.her_name,
            personality=personality.render(proactive=True) if personality is not None else "",
            now=f"{when.year}年{when.month}月{when.day}日 {when.hour:02d}:{when.minute:02d}",
            slot_label=SLOT_LABEL.get(slot_of(when), ""), focus=focus, state=state,
            recent=recent, tastes=script.get("tastes") or "（未记录）",
            cares=script.get("cares") or "（未记录）", chat=chat, said=said,
            relation=self._relation_text(now, scene_text=focus), due_promises=due_text)
        if self.motive_mode:
            prompt += ("\n【本轮可选择的主动缘由（只能从这里选）】\n" + motive_text +
                       "\n若开口，JSON 必须给 motive_ids，且只能填上面出现的 id；沉默给空数组。")
        try:
            data, err = chat_json(client, model,
                                  [{"role": "user", "content": prompt}],
                                  temperature=0.9, max_tokens=200, retries=1, no_think=True)
        except Exception as exc:
            log.warning("主动说话生成异常（这次不说）：%s: %s", type(exc).__name__, exc)
            return []
        choice = (data or {}).get("speak")
        if choice is False or (isinstance(choice, str) and choice.lower() == "false"):
            self._set_ts("declined_at", now)
            self.last_decision = False
            log.info("她选择暂不主动开口")
            return []
        if self.motive_mode:
            wanted = (data or {}).get("motive_ids")
            if not isinstance(wanted, list) or not wanted:
                log.warning("主动说话生成缺少有效 motive_ids（这次不说）")
                return []
            allowed = {str(row["id"]): row for row in motives}
            if any(str(value) not in allowed for value in wanted):
                log.warning("主动说话生成包含未提供的 motive_ids（这次不说）")
                return []
            self.last_motive_ids = [str(value) for value in wanted if str(value) in allowed]
            if not self.last_motive_ids:
                log.warning("主动说话生成选择了未知 motive_ids（这次不说）")
                return []
        text = str((data or {}).get("text") or "").strip()
        if not text:
            log.warning("主动说话生成失败（这次不说）：%s", err)
            return []
        # 切分：上限与主对话同一条契约（3 条），多的并进最后一条，不丢字。
        parts = [p[:120] for p in split_messages(text) if p.strip()]
        if not parts:
            # 只生成了分隔符（或全是空行）——那不是"她说了话"，是不说
            log.warning("主动说话只生成了分隔符、没有正文（这次不说）：%r", text[:40])
            return []
        evidence = [event for event in events
                    if event.get("source") in ("model", "conversation", "dedup")
                    and event.get("slot") == slot_of(when)
                    and float(event.get("ts") or 0) <= now
                    and dt.datetime.fromtimestamp(float(event.get("ts") or 0)).date() == when.date()]
        completed = [count for event in evidence for count in _completed_games(event["text"])]
        grounded = []
        for part in parts:
            claims = _completed_games(part)
            if claims and any(not completed or (count is not None and count not in completed)
                              for count in claims):
                self.last_grounding.append(part)
            else:
                grounded.append(part)
        if self.last_grounding:
            log.info("主动说话丢掉无记录支持的打球完成说法：%s", " / ".join(self.last_grounding))
            recorder = getattr(self._lock_source, "event", None)
            if callable(recorder):
                recorder("proactive_grounding_drop", reason="unsupported_completed_game",
                         dropped=list(self.last_grounding), evidence_ids=[event["rid"] for event in evidence])
        parts = grounded
        if not parts:
            return []
        # 判重（`#138`）：她 2026-09-30 真机重复过一句——19:21「排骨挑好了，老板娘还多搭两根筒骨」
        # → 21:38 又说「老板娘还多搭了两根筒骨」。而当时那份"【你最近主动说过的话】（**别重复这些**）"
        # **确实带着**那一条，她还是说了：提示词压不住，照 `D4` 补机器兜底。
        # 判据 = 与最近说过的话、以及最近的生活流水比（她"没新事可说"时最爱复述自己刚做的）。
        seen = list(self._recent_said(6)) + [str(e.get("text") or "") for e in self.life.recent_events(6)]
        kept: list[str] = []
        for part in parts:
            if textsim.is_repeat(part, seen) or textsim.is_repeat(part, kept):
                self.last_duplicates.append(part)
                continue
            kept.append(part)
        if self.last_duplicates:
            log.info("主动说话判重丢掉 %d 条（与最近说过/刚做的太像）：%s",
                     len(self.last_duplicates),
                     " / ".join(x[:24] for x in self.last_duplicates))
        if not kept:
            # 整批都是重复 → 等于这次没开口。**decision 保持 None**，让调用方按
            # `last_duplicates` 分辨"她选择不说"和"被机器判重挡下"（两种原因的埋点不同）。
            return []
        # 归属兜底（`#146`）：**只有她自己知道的东西**（团子／球衣／课表／出租屋…）
        # 不许反过来问他。真机现场：她报备完"在收球衣…刚下楼捡回来"，紧跟着一句
        # 「团子没扒窗户吧」——团子是她自己的猫，他答不上来。提示词三处都写了，
        # 仍然漏了一次，所以照 `D4` 补机器兜底（只丢**那一条**，报备那条留着）。
        rel = getattr(self, "relationship", None)
        own = rel.her_own() if rel is not None else []
        if own:
            survivors: list[str] = []
            for part in kept:
                hit = textsim.asks_about_her_own(part, own)
                if hit:
                    self.last_ownership.append(part)
                    log.info("主动说话丢掉一条（拿自己的东西「%s」问他了）：%s", hit, part[:40])
                    continue
                survivors.append(part)
            kept = survivors
            if not kept:
                return []                          # 整批都犯了 → 这次不开口
        if self.motive_mode and (self.last_duplicates or self.last_grounding or self.last_ownership):
            selected = [row for row in motives if row["id"] in self.last_motive_ids]
            nonpromises = [row for row in selected if row.get("kind") != "promise"]
            removed_promises = [row["text"] for row in selected
                                if row.get("kind") == "promise"
                                and not any(row["text"] in part for part in kept)]
            filtered = self.last_duplicates + self.last_grounding + self.last_ownership
            if len(nonpromises) > 1 or (nonpromises and len(selected) > 1
                    and any(not any(text in part for text in removed_promises) for part in filtered)):
                log.warning("主动说话部分气泡被过滤，多素材无法对应存活气泡（这次不消费）")
                return []
        self.last_decision = True
        for part in kept:
            mentions = [row["id"] for row in due_promises if row["text"] in part]
            if mentions:
                self.last_promise_mentions[part] = mentions
        reminder_ids = (data or {}).get("reminder_ids")
        if isinstance(reminder_ids, list):
            wanted = {value for value in reminder_ids if isinstance(value, str)}
            for part in kept:
                matches = [promise_id for promise_id in self.last_promise_mentions.get(part, [])
                           if promise_id in wanted]
                if matches:
                    self.last_reminders[part] = matches
        return kept

    # ---------- 待发表 ----------
    def enqueue(self, text: str) -> int:
        with self._write_lock, self.db:
            cur = self.db.execute(
                "INSERT INTO pending_say(ts,text,her_name) VALUES(?,?,?)",
                (time.time(), text, getattr(self.life, "her_name", "") or ""))
        self._set_ts("her_last_ts", time.time())
        return int(cur.lastrowid or 0)

    def pending_items(self) -> list[dict]:
        """**还没被确认收下**的待发表（只读，不改状态）。投递协议见 `ack_pending`。"""
        rows = list(self.db.execute(
            "SELECT id,ts,text,her_name FROM pending_say WHERE consumed_at IS NULL "
            "ORDER BY id"))
        return [dict(r) for r in rows]

    def ack_pending(self, ids) -> int:
        """客户端确认"这几条已经显示给用户了"→ **这时才标记已读**。

        ⚠ 为什么把"取走即标记"改成"确认才标记"（`#119`）：原协议是 `GET /api/pending`
        顺手标记已读，于是**任何一次渲染失败、页面刚好刷新、响应在网络里丢了**——
        那条话就永远消失了，库里只剩一个 `consumed_at`。用户的原话是
        「**主动说的话我一点都不知道**」，而查下来前台确实取走了（13:51:04 标了已读），
        但页面上什么都没有：**取走 ≠ 看到**。
        现在改成**至少一次投递**：没确认的下轮还会给，前端按 id 去重（`seenSay`）。
        """
        wanted = [int(i) for i in (ids or [])]
        if not wanted:
            return 0
        marks = ",".join("?" for _ in wanted)
        with self._write_lock, self.db:
            cur = self.db.execute(
                f"UPDATE pending_say SET consumed_at=? "
                f"WHERE consumed_at IS NULL AND id IN ({marks})",
                [time.time(), *wanted])
        return int(cur.rowcount or 0)

    def take_pending(self) -> list[dict]:
        """取走全部未读（= 读一次 + 全部确认）。留给不需要 ack 的调用方与测试。

        `#156`：**投递前必须过归属兜底**——生成口那道护栏管不到队列里早先攒下的话
        （真机库里就躺着 `#146` 之前生成的「团子没扒窗户吧」）。
        """
        items = self.drop_own_questions(self.drop_invalid_motives(self.pending_items()))
        self.ack_pending([i["id"] for i in items])
        return items

    def drop_own_questions(self, items=None) -> list[dict]:
        """投递口兜底（`#156`）：**只有她自己知道的东西**不许在投递时才漏出去。

        与 `speak()` 里那道同类护栏是一对：那道管**生成**，这道管**投递**。为什么两道都要：
        · 队列里可能躺着护栏上线**之前**生成的话（真机现场：「团子没扒窗户吧」，
          团子是她自己的猫，他答不上来）；
        · 照 `D4`「可靠性不能只靠提示词」，护栏要装在**真正生效**的那一点上。

        参数 `items` 是 `pending_items()` 那样的行（`{id, ts, text, her_name}`）；
        返回**仍该投递的**（保持原顺序、行原样，投递方要拿 `id` 去 ack）。
        命中的那些**当场标记已投递**（`ack_pending`），所以之后 `pending_items()` 不再返回它们；
        丢掉的原文记进 `self.last_delivery_drops`（给埋点用，每次调用重置）。
        没挂关系层（拿不到"她自己的东西"清单）时**原样返回**，绝不因为护栏本身出错而少发一条话。
        """
        rows = list(items if items is not None else self.pending_items())
        self.last_delivery_drops = []
        # `B2-②`：被挡下的那些**行 id**（行没被标记已读，所以随时能人工放行/复查）
        self.last_delivery_drop_ids: list[int] = []
        rel = getattr(self, "relationship", None)
        own = rel.her_own() if rel is not None else []
        if not own:
            return rows
        kept: list[dict] = []
        dropped: list[tuple[str, dict]] = []
        for row in rows:
            text = str(row.get("text") or "")
            hit = textsim.asks_about_her_own(text, own)
            (dropped.append((hit, row)) if hit else kept.append(row))
        if dropped:
            # `B2-②`（工单二）：**不当场 ack**。原来命中即 `ack_pending` ＝不可恢复的丢弃
            # （护栏判错一次，那句话就永远没了、也没痕迹）。现在只"不投递"：
            # 行**留在 `pending_say`**（看板可见、可人工恢复），并在日志/埋点里留痕。
            for hit, row in dropped:
                text = str(row.get("text") or "")
                self.last_delivery_drops.append(text)
                try:
                    self.last_delivery_drop_ids.append(int(row.get("id") or 0))
                except (TypeError, ValueError):
                    pass
                log.info("投递前挡下一条（拿自己的东西「%s」问他了，未标记已读）：%s",
                         hit, text[:40])
        return kept

    def said_recent(self, limit: int = 20) -> list[dict]:
        """她主动说过的话（**只读**）。

        ⚠ 别拿 `take_pending()` 看历史：那个是"取走并标记已读"（语义是投递），
        用来看一眼会把待投递的话标记成已读——看板要的是只读视图。
        """
        rows = self.db.execute(
            "SELECT ts,text,her_name,consumed_at FROM pending_say ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [{"ts": r["ts"], "text": r["text"], "her_name": r["her_name"],
                 "consumed": bool(r["consumed_at"])} for r in rows]

    def said_today(self, now: float | None = None) -> int:
        """今天主动开口**几次**（看板与闸门共用同一口径，见 `_said_today`）。"""
        return self._said_today(float(now if now is not None else time.time()))

    def touch_user(self) -> None:
        """记下"用户刚说过话"（闸门 ④ 的依据）。"""
        self._set_ts("user_last_ts", time.time())

    # ---------- 内部 ----------
    def _ts(self, col: str) -> float:
        row = self.db.execute(f"SELECT {col} FROM life_state WHERE id=1").fetchone()
        try:
            return float(row[col]) if row and row[col] else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _set_ts(self, col: str, value: float) -> None:
        if col not in ("her_last_ts", "user_last_ts", "roll_ts", "declined_at"):
            return
        with self._write_lock, self.db:
            self.db.execute(
                "INSERT INTO life_state(id,updated_at,%s) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET %s=excluded.%s" % (col, col, col),
                (time.time(), float(value)))

    def _said_today(self, now: float) -> int:
        """今天主动开口**几次**（不是几条气泡）。

        ⚠ 为什么按"次"不按"条"（2026-09-30 修，真机实测踩到）：`T86` 之后她一次开口
        可能发 1~3 条，而 `pending_say` 是**一行一条气泡**。原来数行数，于是真机上
        "一次三条"直接把一天的额度吃光（13:19 那次 `said` 当场跳到 2，再来一次就满），
        日上限实际变成"一天只准开口一次"。现在数 `events` 表里的 `proactive` 事件：
        **一次开口一条**，与配置名的语义一致。
        """
        import datetime as dt
        day = dt.datetime.fromtimestamp(now).date()
        start = dt.datetime.combine(day, dt.time(0, 0)).timestamp()
        try:
            if self.motive_mode:
                durable = self.db.execute(
                    "SELECT count(*) FROM proactive_batches WHERE ts>=?", (start,)).fetchone()[0]
                legacy = self.db.execute(
                    "SELECT count(*) FROM events WHERE kind='proactive' AND ts>=? "
                    "AND payload NOT LIKE '%\"batch_id\"%'", (start,)).fetchone()[0]
                return int(durable) + int(legacy)
            row = self.db.execute(
                "SELECT count(*) FROM events WHERE kind='proactive' AND ts>=?",
                (start,)).fetchone()
            return int(row[0] if row else 0)
        except Exception:                     # 旧库没有 events 表 → 当作没说过
            return 0

    def _relation_text(self, now: float, scene_text: str = "") -> str:
        """关系层那一段（`#146`）。没挂关系层时给中性占位，别让提示词里出现空块。"""
        rel = getattr(self, "relationship", None)
        if rel is None:
            return "（还没记录）"
        try:
            return rel.render(now, int(getattr(self, "user_msgs", 0) or 0), scene_text=scene_text)
        except Exception as exc:
            log.warning("读关系层失败（这次按中性关系说）：%s", exc)
            return "（还没记录）"

    def _due_promises(self, now: float) -> list[dict]:
        relation = getattr(self, "relationship", None)
        if relation is None:
            return []
        try:
            return relation.due_promises(now)
        except Exception as exc:
            log.warning("读到期承诺失败（这次不提醒）：%s", exc)
            return []

    def _recent_said(self, limit: int) -> list[str]:
        rows = self.db.execute("SELECT text FROM pending_say ORDER BY id DESC LIMIT ?",
                               (max(1, int(limit)),)).fetchall()
        return [r["text"] for r in rows]

    def _has_fresh_life(self, since_ts: float) -> bool:
        """某次决定暂不说之后，生活里有没有新的开口线索。"""
        if not since_ts:
            return bool(self.life.recent_events(1))
        row = self.db.execute("SELECT count(*) FROM life_events WHERE ts > ? AND hidden_at IS NULL",
                              (float(since_ts),)).fetchone()
        return bool(row and row[0])
