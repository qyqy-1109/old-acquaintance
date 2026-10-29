# -*- coding: utf-8 -*-
"""Runtime settings written from the web UI.

Secrets entered in the browser are stored in ``DATA_DIR/settings.json`` rather than
``.env`` so they survive Docker volume restores and read-only source mounts.  Real
environment variables still win: a value injected by the deployment is never
overwritten by this file.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from app.utils.jsonfile import write_json_atomic

# Keys the settings page may edit.  Everything else stays deployment-owned.
KEYS: tuple[str, ...] = (
    "WB_API_KEY",
    "WB_BASE_URL",
    "WB_CHAT_API_KEY",
    "WB_CHAT_BASE_URL",
    "WB_EMBED_API_KEY",
    "WB_EMBED_BASE_URL",
    "WB_MODEL",
    "WB_EMBED_MODEL",
    "WB_EMBED_DIMENSIONS",
    "WB_EXTRACT_MODEL",
    "WB_TTS_API_KEY",
    "WB_AMAP_KEY",
    "WB_EMOTION_API_KEY",
)

SECRET_KEYS = frozenset({
    "WB_API_KEY", "WB_CHAT_API_KEY", "WB_EMBED_API_KEY",
    "WB_TTS_API_KEY", "WB_AMAP_KEY", "WB_EMOTION_API_KEY",
})

DEFAULTS: dict[str, str] = {
    "WB_BASE_URL": "https://api.xiaomimimo.com/v1",
    "WB_CHAT_BASE_URL": "https://api.xiaomimimo.com/v1",
    "WB_EMBED_BASE_URL": "https://maas.qianwenaiapi.com/compatible-mode/v1",
    "WB_MODEL": "mimo-v2.6-flash",
    "WB_EMBED_MODEL": "qwen3.7-text-embedding",
    "WB_EMBED_DIMENSIONS": "2048",
    "WB_EXTRACT_MODEL": "mimo-v2.6-flash",
}


def load(path: str) -> dict[str, str]:
    """Read known string values; corrupt or missing files mean "no overrides"."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: str(raw[key]) for key in KEYS if isinstance(raw.get(key), str)}


def save(path: str, updates: dict[str, str], clear: Iterable[str] = ()) -> dict[str, str]:
    """Merge updates into the settings file and keep it private to the OS user."""
    current = load(path)
    for key in clear:
        if key in KEYS:
            current.pop(key, None)
    for key, value in updates.items():
        if key not in KEYS or not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    write_json_atomic(path, current)
    _restrict_permissions(path)
    return current


def _restrict_permissions(path: str) -> None:
    """Best-effort 0600 on POSIX; Windows ACLs are left to the user profile."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "…" + value[-4:]
