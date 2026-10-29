# -*- coding: utf-8 -*-
"""Chat session list/switch/rename/delete.

Messages already live in ``messages.session``; this mixin adds the small amount of
metadata needed by the UI and keeps ``self.session`` in sync.  The database is the
source of truth for titles/timestamps, while ``self.sessions`` remains the live
message cache used by the prompt builder.
"""

from __future__ import annotations

import time
import uuid


class SessionMixin:
    def _ensure_session_meta(self, session_id: str, title: str = "") -> None:
        now = time.time()
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO chat_sessions(id,title,created_at,updated_at) "
                "VALUES(?,?,?,?)", (session_id, title, now, now))

    def list_sessions(self) -> list[dict]:
        with self._lock:
            self._ensure_session_meta(self.session)
            metas = {
                row["id"]: dict(row)
                for row in self.db.execute(
                    "SELECT id,title,created_at,updated_at FROM chat_sessions")
            }
            counts = {
                row["session"]: {"count": int(row["n"] or 0), "last": row["last_ts"] or 0}
                for row in self.db.execute(
                    "SELECT session,COUNT(*) AS n,MAX(ts) AS last_ts "
                    "FROM messages GROUP BY session")
            }
            ids = set(metas) | set(counts) | set(self.sessions) | {self.session}
            items = []
            for sid in ids:
                msgs = self.sessions.get(sid, [])
                title = str((metas.get(sid) or {}).get("title") or "").strip()
                if not title:
                    for msg in msgs:
                        if msg.get("role") == "user" and str(msg.get("content") or "").strip():
                            title = str(msg["content"]).strip().replace("\n", " ")[:24]
                            break
                meta = metas.get(sid) or {}
                count = max(len(msgs), int((counts.get(sid) or {}).get("count") or 0))
                updated = max(float(meta.get("updated_at") or 0),
                              float((counts.get(sid) or {}).get("last") or 0),
                              float(meta.get("created_at") or 0))
                if not title and not count and sid != self.session:
                    continue
                items.append({
                    "id": sid,
                    "title": title or "新对话",
                    "message_count": count,
                    "updated_at": updated,
                    "active": sid == self.session,
                })
            items.sort(key=lambda item: item["updated_at"], reverse=True)
            return items

    def new_session(self, title: str = "") -> str:
        session_id = "chat-" + uuid.uuid4().hex[:12]
        now = time.time()
        title = str(title or "").strip()[:60]
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO chat_sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (session_id, title, now, now))
            self.sessions[session_id] = []
            self._synced[session_id] = 0
        self.session = session_id
        return session_id

    def select_session(self, session_id: str) -> str:
        session_id = str(session_id or "").strip()
        if not session_id or len(session_id) > 120:
            raise ValueError("会话 id 无效")
        with self._lock:
            known = self.db.execute(
                "SELECT 1 FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
            if not known:
                known = self.db.execute(
                    "SELECT 1 FROM messages WHERE session=? LIMIT 1", (session_id,)).fetchone()
            if not known:
                raise KeyError(session_id)
            self._ensure_session_meta(session_id)
            self.sessions.setdefault(session_id, [])
            self.session = session_id
        return session_id

    def rename_session(self, session_id: str, title: str) -> str:
        session_id = str(session_id or "").strip()
        title = str(title or "").strip()[:60]
        if not session_id or not title:
            raise ValueError("会话名称不能为空")
        with self._lock, self.db:
            result = self.db.execute(
                "UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
                (title, time.time(), session_id))
            if not result.rowcount:
                raise KeyError(session_id)
        return title

    def delete_session(self, session_id: str) -> str:
        session_id = str(session_id or "").strip()
        with self._lock, self.db:
            self.db.execute("DELETE FROM messages WHERE session=?", (session_id,))
            self.db.execute("DELETE FROM summaries WHERE session=?", (session_id,))
            self.db.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
            self.sessions.pop(session_id, None)
            self.summaries.pop(session_id, None)
            self.summary_cursor.pop(session_id, None)
            self._synced.pop(session_id, None)
            if self.session == session_id:
                remaining = [sid for sid, msgs in self.sessions.items() if msgs]
                self.session = remaining[0] if remaining else "default"
                self.sessions.setdefault(self.session, [])
                now = time.time()
                self.db.execute(
                    "INSERT OR IGNORE INTO chat_sessions(id,title,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (self.session, "", now, now))
        return self.session

    def session_messages(self, session_id: str) -> list[dict]:
        session_id = str(session_id or "").strip()
        with self._lock:
            rows = self.db.execute(
                "SELECT seq,role,content,ts,greeting FROM messages "
                "WHERE session=? ORDER BY seq", (session_id,)).fetchall()
        return [{"seq": int(row["seq"]), "role": row["role"],
                 "content": row["content"] or "", "ts": row["ts"],
                 "greeting": bool(row["greeting"])} for row in rows]
