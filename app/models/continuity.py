"""Lightweight, source-backed conversation continuity topics."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.log import get

log = get("continuity")


class ContinuityBook:
    """Project open topics over the existing per-user messages table.

    The book deliberately stores a digest of the referenced message.  A reused
    ``(session, seq)`` therefore cannot silently change the meaning of a topic.
    """

    VALID_STATUS = {"open", "resolved", "suppressed"}
    VALID_STATE = {"intention", "decision", "experience"}

    def __init__(self, store, ttl_days: float = 7, context_limit: int = 3):
        self.store = store
        self.db = store.db
        self.lock = store._lock
        self.ttl_days = max(1.0, float(ttl_days))
        self.context_limit = max(1, int(context_limit))
        with self.lock, self.db:
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS continuity_topics (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    text TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'user',
                    state TEXT NOT NULL DEFAULT 'experience',
                    status TEXT NOT NULL DEFAULT 'open',
                    session TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    assistant_seq INTEGER,
                    source_digest TEXT NOT NULL,
                    source_ts REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL,
                    eligible_at REAL,
                    revision INTEGER NOT NULL DEFAULT 1
                )""")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS continuity_revisions (
                    topic_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY(topic_id, revision)
                )""")
            for row in self.db.execute("SELECT * FROM continuity_topics").fetchall():
                self._archive(row)

    @staticmethod
    def _digest(session: str, seq: int, role: str, content: str, source_ts: float) -> str:
        body = json.dumps([session, int(seq), role, content, float(source_ts)], ensure_ascii=False,
                          separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _id(topic: str, actor: str = "user") -> str:
        value = topic.strip().casefold()
        if actor != "user":
            value = actor + ":" + value
        return "topic:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _archive(self, row):
        self.db.execute(
            "INSERT OR IGNORE INTO continuity_revisions(topic_id,revision,data) VALUES(?,?,?)",
            (row["id"], row["revision"], json.dumps(dict(row), ensure_ascii=False)))

    @staticmethod
    def _row(row, source_valid: bool | None = None) -> dict[str, Any]:
        result = dict(row)
        if source_valid is not None:
            result["source_valid"] = source_valid
        return result

    def _message(self, session: str, seq: int):
        return self.db.execute(
            "SELECT role,content,ts FROM messages WHERE session=? AND seq=?",
            (session, int(seq))).fetchone()

    def _valid(self, row) -> bool:
        message = self._message(row["session"], row["seq"])
        if not message:
            return False
        return self._digest(row["session"], row["seq"], message["role"],
                            str(message["content"] or ""),
                            float(message["ts"] or 0)) == row["source_digest"]

    def _all(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,topic,text,quote,actor,state,status,session,seq,assistant_seq,"
            "source_digest,source_ts,updated_at,expires_at,eligible_at,revision "
            "FROM continuity_topics ORDER BY updated_at DESC, id").fetchall()
        return [self._row(row, self._valid(row)) for row in rows]

    def items(self) -> list[dict]:
        with self.lock:
            return self._all()

    def get(self, topic_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT id,topic,text,quote,actor,state,status,session,seq,assistant_seq,"
                "source_digest,source_ts,updated_at,expires_at,eligible_at,revision "
                "FROM continuity_topics WHERE id=?", (str(topic_id),)).fetchone()
            return self._row(row, self._valid(row)) if row else None

    def history(self, topic_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM continuity_revisions WHERE topic_id=? ORDER BY revision",
                (str(topic_id),)).fetchall()
            revisions = [json.loads(row["data"]) for row in rows]
            return [self._row(row, self._valid(row)) for row in revisions]

    def _candidate_rows(self, question: str, now: float, limit: int,
                        include_closed: bool = False) -> list[dict]:
        rows = self._all()
        query = str(question or "").strip().casefold()
        result = []
        for row in rows:
            if not row["source_valid"]:
                continue
            if not include_closed and row["status"] != "open":
                continue
            if row["status"] != "open" and not query:
                continue
            if row["status"] == "open" and row.get("expires_at") is not None \
                    and float(row["expires_at"]) <= now:
                continue
            topic_text = str(row["topic"]).casefold()
            quote_text = str(row["quote"]).casefold()
            if row["status"] != "open" and query \
                    and topic_text not in query and query not in quote_text:
                continue
            if query and row["status"] == "open":
                haystack = (str(row["topic"]) + " " + str(row["quote"])).casefold()
                if topic_text not in query and query not in haystack:
                    continue
            result.append(row)
            if len(result) >= limit:
                break
        return result

    def snapshot(self, question: str = "", now: float | None = None,
                 limit: int | None = None, include_closed: bool = False) -> list[dict]:
        now = time.time() if now is None else float(now)
        limit = self.context_limit if limit is None else max(1, int(limit))
        with self.lock:
            return self._candidate_rows(question, now, limit, include_closed)

    def render(self, question: str = "", now: float | None = None,
               limit: int | None = None) -> str:
        now = time.time() if now is None else float(now)
        active = self.snapshot(question, now=now, limit=limit)
        closed = self.snapshot(question, now=now, limit=limit, include_closed=True)
        seen = {row["id"] for row in active}
        closed = [row for row in closed if row["id"] not in seen and row["status"] != "open"]
        lines = []
        for row in active:
            actor = "她" if row["actor"] == "assistant" else "用户"
            state = {"intention": "意向", "decision": "决定", "experience": "经历"}.get(row["state"], "原话")
            lines.append(f"- [{row['id']}] {row['topic']}：{row['quote']}（{actor}的{state}，待关心）")
        for row in closed[:max(0, int(limit or self.context_limit))]:
            label = "已解决" if row["status"] == "resolved" else "不再提"
            actor = "她" if row["actor"] == "assistant" else "用户"
            state = {"intention": "意向", "decision": "决定", "experience": "经历"}.get(row["state"], "原话")
            lines.append(f"- [{row['id']}] {row['topic']}：{row['quote']}（{actor}的{state}，{label}）")
        if not lines:
            return ""
        return "【近期话题】\n" + "\n".join(lines) + (
            "\n意向不等于决定，决定不等于已经发生；原话的归属以所标人物为准。"
            "只把开放话题当作未完事项；已解决或不再提的只在当前话题相关时作历史说明。")

    def apply(self, updates: list[dict] | None, session: str, user_seq: int,
              assistant_seq: int | None = None, now: float | None = None,
              user_text: str | None = None, assistant_text: str | None = None,
              source_ts: float | None = None) -> list[dict]:
        """Apply model-structured updates after their source messages are saved."""
        now = time.time() if now is None else float(now)
        updates = updates if isinstance(updates, list) else []
        accepted = []
        with self.lock, self.db:
            user_source = self._message(session, user_seq)
            if not user_source or user_source["role"] != "user":
                return []
            persisted = str(user_source["content"] or "")
            persisted_ts = float(user_source["ts"] or 0)
            if source_ts is not None and float(source_ts) != persisted_ts:
                return []
            extracted = persisted if user_text is None else str(user_text)
            if extracted != persisted and not persisted.startswith(extracted):
                return []
            for raw in updates:
                if not isinstance(raw, dict):
                    continue
                actor = str(raw.get("actor") or "user").strip().lower()
                topic = str(raw.get("topic") or "").strip()
                quote = str(raw.get("quote") or "").strip()
                status = str(raw.get("status") or "open").strip().lower()
                state = str(raw.get("state") or ("experience" if actor == "user" else "")).strip().lower()
                topic_id = str(raw.get("id") or raw.get("topic_id") or "").strip()
                if actor not in {"user", "assistant"} or not topic or not quote \
                        or status not in self.VALID_STATUS or state not in self.VALID_STATE:
                    continue
                source = user_source
                source_seq = int(user_seq)
                source_text = persisted
                source_window = extracted[:1000]
                if actor == "assistant":
                    if assistant_seq is None or int(assistant_seq) != int(user_seq) + 1:
                        continue
                    source = self._message(session, assistant_seq)
                    if not source or source["role"] != "assistant" \
                            or float(source["ts"] or 0) != persisted_ts:
                        continue
                    source_seq = int(assistant_seq)
                    source_text = str(source["content"] or "")
                    source_window = source_text if assistant_text is None else str(assistant_text)
                    if not source_text.startswith(source_window):
                        continue
                    source_window = source_window[:1500]
                if quote not in source_text or quote not in source_window:
                    continue
                message_ts = float(source["ts"] or 0)
                digest = self._digest(session, source_seq, source["role"], source_text, message_ts)
                existing = None
                if topic_id:
                    existing = self.db.execute(
                        "SELECT * FROM continuity_topics WHERE id=?", (topic_id,)).fetchone()
                    if not existing or existing["topic"] != topic or existing["actor"] != actor:
                        continue
                else:
                    topic_id = self._id(topic, actor)
                    existing = self.db.execute(
                        "SELECT * FROM continuity_topics WHERE id=?", (topic_id,)).fetchone()
                    if existing and existing["source_digest"] != digest and self._valid(existing):
                        continue
                if topic not in quote and (actor != "assistant" or existing is None
                                           or not (raw.get("id") or raw.get("topic_id"))):
                    continue
                if existing:
                    if existing["source_digest"] == digest:
                        continue
                    previous_ts = float(existing["source_ts"] or 0)
                    same_session = existing["session"] == session
                    rebuilding = not self._valid(existing) and message_ts > previous_ts
                    if message_ts < previous_ts or (
                            same_session and not rebuilding
                            and source_seq <= int(existing["seq"])) or (
                            not same_session and message_ts == previous_ts):
                        continue
                expires = raw.get("expires_at")
                if expires is None and status == "open":
                    expires = now + self.ttl_days * 86400
                try:
                    expires = float(expires) if expires is not None else None
                except (TypeError, ValueError):
                    expires = now + self.ttl_days * 86400 if status == "open" else None
                eligible = raw.get("eligible_at")
                try:
                    eligible = float(eligible) if eligible is not None else None
                except (TypeError, ValueError):
                    eligible = None
                revision = int(existing["revision"]) + 1 if existing else 1
                text = str(raw.get("text") or quote).strip()[:500]
                if existing:
                    self.db.execute(
                        "UPDATE continuity_topics SET text=?,quote=?,actor=?,state=?,status=?,"
                        "session=?,seq=?,assistant_seq=?,source_digest=?,source_ts=?,updated_at=?,expires_at=?,"
                        "eligible_at=?,revision=? WHERE id=?",
                        (text, quote[:500], actor, state, status, session, source_seq,
                         assistant_seq, digest, message_ts, now, expires, eligible, revision, topic_id))
                else:
                    self.db.execute(
                        "INSERT INTO continuity_topics(id,topic,text,quote,actor,state,status,session,seq,"
                        "assistant_seq,source_digest,source_ts,updated_at,expires_at,eligible_at,revision) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (topic_id, topic[:120], text, quote[:500], actor, state, status,
                         session, source_seq, assistant_seq, digest, message_ts, now, expires,
                         eligible, revision))
                row = self.db.execute("SELECT * FROM continuity_topics WHERE id=?", (topic_id,)).fetchone()
                self._archive(row)
                accepted.append(self._row(row, True))
        return accepted
