# -*- coding: utf-8 -*-
"""Web settings: model credentials entered before/after login.

The page never receives a full secret back.  Values are stored in
``DATA_DIR/settings.json``; real environment variables always win.  Saving tries a
controlled rebuild of cached services and falls back to "restart required" when a
conversation or background memory write is still in flight.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field

from app import config
from app.controllers.chat_controller import _admin_ok
from app.services import registry, worker
from app.utils import settings_store

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


class TestIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() != str(request.base_url).rstrip("/").lower():
        raise HTTPException(status_code=403, detail="请从本服务的设置页面提交。")


def _validate(values: dict[str, str], clear: list[str]) -> None:
    for key in list(values) + list(clear):
        if key not in settings_store.KEYS:
            raise HTTPException(status_code=400, detail=f"不认识的配置项：{key}")
    for key, value in values.items():
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{key} 必须是字符串")
        if len(value) > 4000:
            raise HTTPException(status_code=400, detail=f"{key} 太长")
        if key.endswith("_BASE_URL") and value.strip() and not value.strip().startswith(
                ("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"{key} 必须是以 http:// 或 https:// 开头的地址")
        if key == "WB_EMBED_DIMENSIONS" and value.strip():
            try:
                dimensions = int(value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Embedding 维度必须是整数") from exc
            if not 0 <= dimensions <= 8192:
                raise HTTPException(status_code=400, detail="Embedding 维度需在 0 到 8192 之间")
        if key in settings_store.SECRET_KEYS and any(ch in value for ch in "\r\n\t"):
            raise HTTPException(status_code=400, detail=f"{key} 不能包含换行或制表符")


def _managed_conflict(keys) -> None:
    managed = set(config.keys_status()["managed_by_env"])
    bad = sorted(set(keys) & managed)
    if bad:
        raise HTTPException(
            status_code=409,
            detail="这些配置由部署环境变量提供，不能在页面修改：" + "、".join(bad))


def _pick(values: dict[str, str], key: str, fallback: str) -> str:
    return (values.get(key) or os.environ.get(key) or fallback or "").strip()


def _test_connection(values: dict[str, str]) -> dict:
    chat_key = _pick(values, "WB_CHAT_API_KEY", "") or _pick(values, "WB_API_KEY", "") \
        or config.CHAT_API_KEY or config.API_KEY
    chat_base = _pick(values, "WB_CHAT_BASE_URL", "") or _pick(values, "WB_BASE_URL", "") \
        or config.CHAT_BASE_URL
    chat_model = _pick(values, "WB_MODEL", config.MODEL)
    embed_key = _pick(values, "WB_EMBED_API_KEY", config.EMBED_API_KEY)
    embed_base = _pick(values, "WB_EMBED_BASE_URL", config.EMBED_BASE_URL)
    embed_model = _pick(values, "WB_EMBED_MODEL", config.EMBED_MODEL)

    def safe(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        if "<html" in text.lower() or "<!doctype" in text.lower():
            return "服务返回了 HTML 页面，请检查 API 地址是否指向 OpenAI 兼容端点"
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        for secret in (chat_key, embed_key):
            if secret:
                text = text.replace(secret, "***")
        return text[:240]

    if not chat_key:
        chat = {"ok": False, "message": "缺少主对话 API Key（WB_CHAT_API_KEY 或 WB_API_KEY）"}
    else:
        try:
            client = OpenAI(api_key=chat_key, base_url=chat_base,
                            timeout=10.0, max_retries=0)
            client.chat.completions.create(
                model=chat_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1, temperature=0,
            )
            chat = {"ok": True, "message": f"主对话可用（{chat_model}）"}
        except Exception as exc:
            chat = {"ok": False, "message": "主对话连接失败：" + safe(exc)}

    if not embed_key:
        embedding = {"ok": False, "message": "缺少 embedding API Key（WB_EMBED_API_KEY）"}
    else:
        try:
            client = OpenAI(api_key=embed_key, base_url=embed_base,
                            timeout=10.0, max_retries=0)
            client.embeddings.create(model=embed_model, input=["ping"])
            embedding = {"ok": True, "message": f"Embedding 可用（{embed_model}）"}
        except Exception as exc:
            embedding = {"ok": False, "message": "Embedding 连接失败：" + safe(exc)}
    return {"chat": chat, "embedding": embedding}


@router.get("")
def get_settings(request: Request) -> dict:
    _admin_ok(request)
    return config.keys_status()


@router.post("/test")
def test_settings(body: TestIn, request: Request) -> dict:
    _admin_ok(request)
    _same_origin(request)
    _validate(body.values, [])
    return _test_connection(body.values)


@router.post("")
def save_settings(body: SettingsIn, request: Request) -> dict:
    _admin_ok(request)
    _same_origin(request)
    _validate(body.values, body.clear)
    _managed_conflict(set(body.values) | set(body.clear))

    updates: dict[str, str] = {}
    clear = list(dict.fromkeys(body.clear))
    for key, value in body.values.items():
        value = value.strip()
        if key in settings_store.SECRET_KEYS:
            if value:
                updates[key] = value
            continue
        if value:
            updates[key] = value
        else:
            clear.append(key)

    settings_store.save(config.settings_path(), updates, clear)
    config.reload()

    applied = False
    reason = ""
    if worker.pending() == 0 and registry.reload_all():
        try:
            request.app.state.service = registry.get("")
            applied = True
        except Exception as exc:
            reason = f"配置已保存，但重建服务失败：{type(exc).__name__}；请重启服务。"
    else:
        reason = "配置已保存；有对话或后台写入正在进行，请重启服务后完全生效。"

    if applied and not config.keys_ready():
        reason = "已保存，但主对话或 embedding 的 Key 仍不完整。"
    elif applied:
        reason = "配置已生效。"

    return {
        "ok": True,
        "saved": True,
        "applied": applied,
        "restart_required": not applied,
        "message": reason,
        "keys": config.keys_status(),
    }
