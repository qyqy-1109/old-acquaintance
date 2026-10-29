# -*- coding: utf-8 -*-
"""Chat session list and switching."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.controllers.chat_controller import svc
from app.services import worker

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _ensure_idle() -> None:
    if worker.pending():
        raise HTTPException(
            status_code=409,
            detail="还有上一轮的长期记忆正在后台写入，请稍后再切换会话。",
        )


class SessionIn(BaseModel):
    title: str = Field(default="", max_length=60)


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() != str(request.base_url).rstrip("/").lower():
        raise HTTPException(status_code=403, detail="请从本服务的页面提交。")


@router.get("")
def list_sessions(request: Request) -> dict:
    return svc(request).sessions()


@router.post("")
def create_session(body: SessionIn, request: Request) -> dict:
    _same_origin(request)
    _ensure_idle()
    return svc(request).new_session(body.title)


@router.post("/{session_id}/select")
def select_session(session_id: str, request: Request) -> dict:
    _same_origin(request)
    _ensure_idle()
    try:
        return svc(request).select_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{session_id}")
def rename_session(session_id: str, body: RenameIn, request: Request) -> dict:
    _same_origin(request)
    try:
        return svc(request).rename_session(session_id, body.title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{session_id}")
def delete_session(session_id: str, request: Request) -> dict:
    _same_origin(request)
    _ensure_idle()
    return svc(request).delete_session(session_id)


@router.get("/{session_id}/messages")
def session_messages(session_id: str, request: Request) -> dict:
    return {"messages": svc(request).session_messages(session_id)}
