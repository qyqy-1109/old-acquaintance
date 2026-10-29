# -*- coding: utf-8 -*-
"""Search active memories, world book and message history."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.controllers.chat_controller import svc

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def search(request: Request, q: str = Query(..., min_length=1, max_length=80)) -> dict:
    query = q.strip()
    if not query:
        return {"query": "", "memories": [], "world": [], "messages": []}
    service = svc(request)
    store = service.store
    with store._lock:
        memory_rows = store.db.execute(
            "SELECT text,kind,event_date,actor FROM memories "
            "WHERE archived_at IS NULL AND instr(text, ?) > 0 "
            "ORDER BY id DESC LIMIT 10", (query,)).fetchall()
        message_rows = store.db.execute(
            "SELECT session,seq,role,content,ts FROM messages "
            "WHERE instr(COALESCE(content,''), ?) > 0 "
            "ORDER BY ts DESC LIMIT 20", (query,)).fetchall()
    titles = {item["id"]: item["title"] for item in service.sessions()["items"]}
    return {
        "query": query,
        "memories": [{"text": row["text"], "kind": row["kind"],
                      "event_date": row["event_date"], "actor": row["actor"]}
                     for row in memory_rows],
        "world": [{"text": entry["text"]} for entry in service.world.entries
                  if query in entry["text"]][:10],
        "messages": [{"session": row["session"], "session_title": titles.get(
            row["session"], row["session"]), "seq": int(row["seq"]),
            "role": row["role"], "content": str(row["content"] or "")[:300],
            "ts": row["ts"]} for row in message_rows],
    }
