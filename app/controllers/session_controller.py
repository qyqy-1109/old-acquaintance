# -*- coding: utf-8 -*-
"""Chat session list and switching."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app.controllers.chat_controller import svc
from app.services import worker
from app.services.chat_service import EmptyReply

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


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


@router.patch("/{session_id}/messages/{seq}")
def edit_message(session_id: str, seq: int, body: MessageIn, request: Request) -> dict:
    _same_origin(request)
    service = svc(request)
    try:
        service.edit_message(session_id, seq, body.content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="消息不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"messages": service.session_messages(session_id)}


@router.delete("/{session_id}/messages/{seq}")
def delete_message(session_id: str, seq: int, request: Request) -> dict:
    _same_origin(request)
    service = svc(request)
    try:
        service.delete_message(session_id, seq)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="消息不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"messages": service.session_messages(session_id)}


@router.post("/{session_id}/messages/{seq}/regenerate")
def regenerate_message(session_id: str, seq: int, request: Request) -> dict:
    _same_origin(request)
    if not config.keys_ready():
        raise HTTPException(status_code=503, detail="模型尚未配置，无法重新生成。")
    service = svc(request)
    if service.store.session != session_id:
        raise HTTPException(status_code=409, detail="请先切换到这条会话。")
    messages = service.session_messages(session_id)
    if (not messages or messages[-1]["seq"] != seq
            or messages[-1]["role"] != "assistant"):
        raise HTTPException(status_code=400, detail="只能重新生成最后一条助手回复。")
    try:
        result = service.regenerate_last()
    except EmptyReply as exc:
        raise HTTPException(status_code=502,
                            detail="重新生成失败，请稍后重试；本轮未保存。") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="重新生成失败，请稍后重试。") from exc
    return {"reply": result.get("reply", ""), "messages": result.get("messages", [])}
