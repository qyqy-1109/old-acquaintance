# -*- coding: utf-8 -*-
"""Parse persona cards without letting a third-party card dictate app structure.

Supported inputs:

* native project JSON: ``{"current": {...}}`` or a flat persona object;
* SillyTavern / character-card v2 JSON: ``{"spec": "chara_card_v2", "data": {...}}``.

The parser only produces plain strings/lists.  ``Personality.import_persona`` does
the final validation and snapshots the previous persona before writing.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import zlib
from typing import Any

MAX_CARD_BYTES = 1_500_000
MAX_PNG_BYTES = 4_000_000
MAX_AVATAR_BYTES = 2_000_000
MAX_TEXT = 4000
MAX_ITEM = 300
MAX_ITEMS = 12
MAX_GREETINGS = 7
MAX_WORLD = 50
MAX_WORLD_ITEM = 2000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class CardError(ValueError):
    pass


def parse_bytes(raw: bytes, filename: str = "") -> dict:
    """Parse JSON cards and PNG cards with embedded chara/ccv3 metadata."""
    if not isinstance(raw, (bytes, bytearray)):
        raise CardError("人格卡片内容无效")
    raw = bytes(raw)
    if raw.startswith(_PNG_SIGNATURE):
        if len(raw) > MAX_PNG_BYTES:
            raise CardError("PNG 人格卡片太大（上限 4 MB）")
        text = _png_card_text(raw)
        result = parse(text, filename)
        if len(raw) <= MAX_AVATAR_BYTES:
            result["avatar"] = raw
        return result
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CardError("人格卡片既不是 PNG，也不是 UTF-8 JSON") from exc
    return parse(text, filename)


def parse(content: str, filename: str = "") -> dict:
    if not isinstance(content, str):
        raise CardError("人格卡片必须是文本")
    if len(content.encode("utf-8")) > MAX_CARD_BYTES:
        raise CardError("人格卡片太大（上限 1.5 MB）")
    try:
        raw = json.loads(content.lstrip("\ufeff"))
    except ValueError as exc:
        raise CardError(f"不是合法的 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise CardError("人格卡片的顶层必须是对象")

    warnings: list[str] = []
    avatar = b""
    if isinstance(raw.get("current"), dict):
        persona = _native(raw["current"], warnings)
        world = _world(raw.get("world") or raw.get("entries"))
        fmt = "jiushi"
        avatar = _avatar(raw["current"]) or _avatar(raw)
    elif _is_v2(raw):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        persona = _v2(data, warnings)
        world = _world((data.get("character_book") or {}).get("entries")
                       if isinstance(data.get("character_book"), dict)
                       else data.get("character_book"))
        fmt = "chara_card_v2"
        avatar = _avatar(data) or _avatar(raw)
    elif any(key in raw for key in ("name", "identity", "traits", "tone")):
        persona = _native(raw, warnings)
        world = _world(raw.get("world") or raw.get("entries"))
        fmt = "jiushi-flat"
        avatar = _avatar(raw)
    else:
        raise CardError("认不出卡片格式：既不是旧识人格 JSON，也不是角色卡 v2")
    if not persona.get("name"):
        raise CardError("人格卡片缺少名字（name）")
    if not any(persona.get(key) for key in
               ("identity", "traits", "tone", "values", "behavior", "speech_habits")):
        raise CardError("人格卡片没有可用的人设内容")
    result = {"format": fmt, "personality": persona, "world": world,
              "warnings": warnings, "filename": filename[:120]}
    if avatar:
        result["avatar"] = avatar
    return result


def image_media_type(data: bytes) -> str:
    for signature, media in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def is_image(data: bytes) -> bool:
    return image_media_type(data) != "application/octet-stream"


def _png_card_text(raw: bytes) -> str:
    offset = 8
    while offset + 8 <= len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        chunk_type = raw[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(raw):
            break
        chunk = raw[start:end]
        if chunk_type == b"tEXt":
            keyword, _, payload = chunk.partition(b"\0")
            if keyword.lower() in (b"chara", b"ccv3"):
                text = _decode_card_payload(payload)
                if text:
                    return text
        elif chunk_type == b"iTXt":
            parts = chunk.split(b"\0", 5)
            if len(parts) == 6 and parts[0].lower() in (b"chara", b"ccv3"):
                payload = parts[5]
                if parts[1] == b"\x01":
                    try:
                        payload = zlib.decompressobj().decompress(payload, MAX_CARD_BYTES)
                    except zlib.error:
                        payload = b""
                text = _decode_card_payload(payload)
                if text:
                    return text
        if chunk_type == b"IEND":
            break
        offset = end + 4
    raise CardError("PNG 里没有 chara/ccv3 角色卡元数据")


def _decode_card_payload(payload: bytes) -> str:
    candidates = []
    stripped = payload.strip()
    if stripped:
        candidates.append(stripped)
        try:
            candidates.append(base64.b64decode(stripped, validate=False))
        except Exception:
            pass
    for candidate in candidates:
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                text = candidate.decode(encoding).lstrip("\ufeff")
                data = json.loads(text)
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(data, dict):
                return text
    return ""


def _avatar(value: Any) -> bytes:
    if not isinstance(value, dict):
        return b""
    for key in ("avatar", "image", "portrait"):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.startswith("data:image/"):
            continue
        header, _, encoded = raw.partition(",")
        if ";base64" not in header:
            continue
        try:
            data = base64.b64decode(encoded, validate=False)
        except Exception:
            continue
        if len(data) <= MAX_AVATAR_BYTES and is_image(data):
            return data
    return b""


def _is_v2(raw: dict) -> bool:
    spec = str(raw.get("spec") or "")
    data = raw.get("data")
    return (spec.startswith("chara_card_v2")
            or (isinstance(data, dict)
                and bool(data.get("name"))
                and any(key in data for key in ("description", "personality", "first_mes"))))


def _native(src: dict, warnings: list[str]) -> dict:
    if "personality" in src and "traits" not in src:
        src = {**src, "traits": src.get("personality")}
    out = {
        "name": _text(src.get("name"), 80),
        "identity": _text(src.get("identity") or src.get("description") or src.get("role"), MAX_TEXT),
        "traits": _items(src.get("traits")),
        "tone": _text(src.get("tone"), MAX_ITEM),
        "values": _items(src.get("values")),
        "behavior": _items(src.get("behavior")),
        "speech_habits": _items(src.get("speech_habits")),
        "greeting": _items(src.get("greeting"), MAX_GREETINGS),
        "origin": _text(src.get("origin"), MAX_TEXT),
        "family": _text(src.get("family"), MAX_TEXT),
        "daily_life": _text(src.get("daily_life"), MAX_TEXT),
        "cares": _items(src.get("cares")),
        "favorite_places": _items(src.get("favorite_places")),
        "self_attitude": src.get("self_attitude") if isinstance(src.get("self_attitude"), dict) else {},
        "scope": src.get("scope") if isinstance(src.get("scope"), dict) else {},
    }
    if src.get("mes_example") or src.get("system_prompt"):
        warnings.append("旧识以外格式的 system/mes_example 字段不会被直接注入。")
    return out


def _v2(data: dict, warnings: list[str]) -> dict:
    description = _text(data.get("description"), MAX_TEXT)
    scenario = _text(data.get("scenario"), MAX_TEXT)
    personality = data.get("personality")
    traits = _split_free(personality, warnings)
    behavior = []
    for label, value in (("system_prompt", data.get("system_prompt")),
                         ("post_history_instructions", data.get("post_history_instructions"))):
        text = _text(value, MAX_ITEM)
        if text:
            behavior.append(text)
    if behavior:
        warnings.append("系统提示词已压缩为行为规则，最多保留 2 条。")
    greetings = _items([data.get("first_mes")] + _alternate_greetings(data),
                       MAX_GREETINGS)
    if data.get("mes_example"):
        warnings.append("示例对话（mes_example）未导入，避免把第三方示例当成当前对话。")
    if data.get("extensions"):
        warnings.append("卡片扩展字段（extensions）已忽略。")
    book = data.get("character_book")
    if isinstance(book, dict):
        world_count = len(book.get("entries") or [])
    elif isinstance(book, list):
        world_count = len(book)
    else:
        world_count = 0
    if world_count:
        warnings.append(f"角色卡带有 {world_count} 条世界书，将一并导入。")
    return {
        "name": _text(data.get("name"), 80),
        "identity": description,
        "traits": traits,
        "tone": _text(data.get("tone"), MAX_ITEM),
        "values": _items(data.get("values")),
        "behavior": behavior,
        "speech_habits": _items(data.get("speech_habits")),
        "greeting": greetings,
        "origin": "",
        "family": "",
        "daily_life": scenario,
        "cares": [],
        "favorite_places": [],
        "self_attitude": {},
        "scope": {},
    }


def _alternate_greetings(data: dict) -> list:
    value = data.get("alternate_greetings") or []
    if isinstance(value, str):
        return [value]
    return value if isinstance(value, list) else []


def _world(value: Any) -> list[str]:
    entries = value
    if isinstance(value, dict):
        entries = value.get("entries") or value.get("items") or []
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in entries:
        if isinstance(item, dict):
            if item.get("enabled") is False:
                continue
            text = item.get("content") or item.get("text") or item.get("name") or ""
        else:
            text = item
        text = _text(text, MAX_WORLD_ITEM)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_WORLD:
            break
    return out


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = _CONTROL.sub("", str(value)).strip()
    return text[:limit]


def _items(value: Any, limit: int = MAX_ITEMS) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = _split_free(value, [])
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []
    out: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("text") or item.get("content") or ""
        text = _text(item, MAX_ITEM)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _split_free(value: Any, warnings: list[str]) -> list[str]:
    if isinstance(value, (list, tuple)):
        return _items(value, MAX_ITEMS)
    text = _text(value, MAX_TEXT)
    if not text:
        return []
    # 角色卡的 personality 常是一段话；按行/分号拆，保留每条可读性。
    parts = [part.strip() for part in re.split(r"[\r\n;；]+", text) if part.strip()]
    if len(parts) > MAX_ITEMS:
        warnings.append(f"人设条目较多，只保留前 {MAX_ITEMS} 条。")
    return parts[:MAX_ITEMS]
