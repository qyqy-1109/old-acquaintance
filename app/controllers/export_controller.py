# -*- coding: utf-8 -*-
"""Download a consistent backup of the current user's data."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import time
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.controllers.chat_controller import svc

router = APIRouter(prefix="/api/export", tags=["export"])

_FILES = (
    "personality.json",
    "personality.json.pending.json",
    "world.json",
    "user_profile.json",
    "relationship.json",
    "life.json",
    "emotion.json",
    "avatar.bin",
    "feedback.jsonl",
)


@router.get("")
def export_all(request: Request) -> Response:
    service = svc(request)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            with service.store._lock:
                dest = sqlite3.connect(tmp.name)
                try:
                    service.store.db.backup(dest)
                finally:
                    dest.close()
            archive.write(tmp.name, "memory.db")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        for name in _FILES:
            path = os.path.join(service.data_dir, name)
            if os.path.isfile(path):
                archive.write(path, name)

        archive.writestr("README.txt",
                         "旧识用户数据导出\n"
                         f"用户：{service.user_id or '(默认用户)'}\n"
                         f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                         "包含：memory.db、人格、世界书、画像、关系、生活、情绪、头像、反馈。\n"
                         "不包含：账号密码库、API Key 设置。\n")

    data = buffer.getvalue()
    return Response(
        data, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="old-acquaintance-export.zip"',
                 "Cache-Control": "no-store"},
    )
