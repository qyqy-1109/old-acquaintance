# -*- coding: utf-8 -*-
"""Persona card preview/import/export for the current user."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.controllers.chat_controller import svc
from app.utils import persona_card

router = APIRouter(prefix="/api/persona", tags=["persona"])


class CardIn(BaseModel):
    filename: str = Field(default="persona.json", max_length=120)
    content: str = Field(min_length=2, max_length=1_600_000)


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() != str(request.base_url).rstrip("/").lower():
        raise HTTPException(status_code=403, detail="请从本服务的页面提交。")


def _preview(parsed: dict) -> dict:
    persona = parsed["personality"]
    return {
        "format": parsed["format"],
        "name": persona.get("name", ""),
        "identity": persona.get("identity", "")[:500],
        "traits": len(persona.get("traits") or []),
        "greeting": len(persona.get("greeting") or []),
        "world_count": len(parsed.get("world") or []),
        "warnings": parsed.get("warnings") or [],
    }


@router.get("")
def current(request: Request) -> dict:
    service = svc(request)
    return {
        "name": service.personality.display_name(),
        "version": service.personality.data.get("version"),
        "world_count": service.world.count(),
    }


@router.post("/preview")
def preview(body: CardIn, request: Request) -> dict:
    _same_origin(request)
    try:
        parsed = persona_card.parse(body.content, body.filename)
    except persona_card.CardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _preview(parsed)


@router.post("/import")
def import_card(body: CardIn, request: Request) -> dict:
    _same_origin(request)
    try:
        parsed = persona_card.parse(body.content, body.filename)
    except persona_card.CardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    service = svc(request)
    try:
        summary = service.personality.import_persona(
            parsed["personality"], reason=f"导入人格卡片：{body.filename}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    world_note = ""
    try:
        if parsed.get("world"):
            world_note = service.world.import_entries(
                parsed["world"], sync=service.embed_ready)
    except Exception as exc:
        return {"ok": True, "summary": summary, "world": "",
                "warnings": parsed["warnings"] + [f"世界书导入失败：{type(exc).__name__}"],
                "name": service.personality.display_name()}
    return {"ok": True, "summary": summary, "world": world_note,
            "warnings": parsed["warnings"],
            "name": service.personality.display_name()}


@router.get("/export")
def export_card(request: Request) -> Response:
    service = svc(request)
    payload = {
        "format": "jiushi-persona-v1",
        "current": service.personality.export_current(),
        "world": [entry["text"] for entry in service.world.entries],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        text, media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="personality.json"'},
    )
