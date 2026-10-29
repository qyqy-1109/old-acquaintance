# -*- coding: utf-8 -*-
"""Persona card preview/import/export for the current user."""

from __future__ import annotations

import base64
import json
import os
import tempfile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.controllers.chat_controller import svc
from app.utils import persona_card

router = APIRouter(prefix="/api/persona", tags=["persona"])


class CardIn(BaseModel):
    filename: str = Field(default="persona.json", max_length=120)
    content: str = Field(default="", max_length=1_600_000)
    content_base64: str | None = Field(default=None, max_length=6_000_000)
    include_world: bool = True


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() != str(request.base_url).rstrip("/").lower():
        raise HTTPException(status_code=403, detail="请从本服务的页面提交。")


def _parse(body: CardIn) -> dict:
    if body.content_base64:
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="卡片附件编码无效") from exc
    elif body.content:
        raw = body.content.encode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="缺少人格卡片内容")
    try:
        return persona_card.parse_bytes(raw, body.filename)
    except persona_card.CardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _preview(parsed: dict, service) -> dict:
    persona = parsed["personality"]
    world = parsed.get("world") or []
    behavior = list(service.personality.CORE_BEHAVIOR)
    for item in persona.get("behavior") or []:
        if item not in behavior:
            behavior.append(item)
    return {
        "format": parsed["format"],
        "name": persona.get("name", ""),
        "identity": persona.get("identity", ""),
        "traits": persona.get("traits") or [],
        "greetings": persona.get("greeting") or [],
        "behavior": behavior,
        "world": world[:20],
        "world_count": len(world),
        "warnings": parsed.get("warnings") or [],
        "avatar": bool(parsed.get("avatar")),
        "current_name": service.personality.display_name(),
        "current_version": service.personality.data.get("version"),
        "will_replace": True,
    }


@router.get("")
def current(request: Request) -> dict:
    service = svc(request)
    return {
        "name": service.personality.display_name(),
        "version": service.personality.data.get("version"),
        "world_count": service.world.count(),
        "has_avatar": os.path.exists(os.path.join(service.data_dir, "avatar.bin")),
    }


@router.post("/preview")
def preview(body: CardIn, request: Request) -> dict:
    _same_origin(request)
    parsed = _parse(body)
    return _preview(parsed, svc(request))


@router.post("/import")
def import_card(body: CardIn, request: Request) -> dict:
    _same_origin(request)
    parsed = _parse(body)
    service = svc(request)
    try:
        summary = service.personality.import_persona(
            parsed["personality"], reason=f"导入人格卡片：{body.filename}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    world_note = ""
    if body.include_world and parsed.get("world"):
        try:
            world_note = service.world.import_entries(
                parsed["world"], sync=service.embed_ready)
        except Exception as exc:
            world_note = f"世界书导入失败：{type(exc).__name__}"

    avatar_note = ""
    if parsed.get("avatar"):
        avatar_note = _store_avatar(service.data_dir, parsed["avatar"])
    return {"ok": True, "summary": summary, "world": world_note,
            "avatar": avatar_note, "warnings": parsed["warnings"],
            "name": service.personality.display_name()}


def _store_avatar(directory: str, data: bytes) -> str:
    if not data or not persona_card.is_image(data):
        return "卡片里的头像格式不受支持"
    if len(data) > persona_card.MAX_AVATAR_BYTES:
        return "头像太大，未保存"
    path = os.path.join(directory, "avatar.bin")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".avatar-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return "头像已更新"
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "头像保存失败"


@router.get("/avatar")
def avatar(request: Request) -> Response:
    service = svc(request)
    path = os.path.join(service.data_dir, "avatar.bin")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="还没有头像") from exc
    return Response(data, media_type=persona_card.image_media_type(data),
                    headers={"Cache-Control": "no-store"})


def _v2_payload(service) -> dict:
    p = service.personality.data
    greetings = [str(x) for x in (p.get("greeting") or []) if str(x).strip()]
    entries = [{"keys": [], "content": entry["text"], "enabled": True,
                "insertion_order": index}
               for index, entry in enumerate(service.world.entries)]
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": p.get("name", ""),
            "description": p.get("identity", ""),
            "personality": "\n".join(str(x) for x in (p.get("traits") or [])),
            "scenario": p.get("daily_life", ""),
            "first_mes": greetings[0] if greetings else "",
            "alternate_greetings": greetings[1:],
            "mes_example": "",
            "system_prompt": "\n".join(str(x) for x in (p.get("behavior") or [])),
            "post_history_instructions": "",
            "character_book": {"entries": entries},
            "extensions": {},
        },
    }


@router.get("/export")
def export_card(request: Request, fmt: str = Query("native", alias="format")) -> Response:
    service = svc(request)
    if fmt == "v2":
        payload = _v2_payload(service)
        filename = "persona-card-v2.json"
    else:
        payload = {
            "format": "jiushi-persona-v1",
            "current": service.personality.export_current(),
            "world": [entry["text"] for entry in service.world.entries],
        }
        filename = "personality.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        text, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
