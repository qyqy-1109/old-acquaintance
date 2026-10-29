# -*- coding: utf-8 -*-
"""Parse persona cards without letting a third-party card dictate app structure.

Supported inputs:

* native project JSON: ``{"current": {...}}`` or a flat persona object;
* SillyTavern / character-card v2 JSON: ``{"spec": "chara_card_v2", "data": {...}}``.

The parser only produces plain strings/lists.  ``Personality.import_persona`` does
the final validation and snapshots the previous persona before writing.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_CARD_BYTES = 1_500_000
MAX_TEXT = 4000
MAX_ITEM = 300
MAX_ITEMS = 12
MAX_GREETINGS = 7
MAX_WORLD = 50
MAX_WORLD_ITEM = 2000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class CardError(ValueError):
    pass


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
    if isinstance(raw.get("current"), dict):
        persona = _native(raw["current"], warnings)
        world = _world(raw.get("world") or raw.get("entries"))
        fmt = "jiushi"
    elif _is_v2(raw):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        persona = _v2(data, warnings)
        world = _world((data.get("character_book") or {}).get("entries")
                       if isinstance(data.get("character_book"), dict)
                       else data.get("character_book"))
        fmt = "chara_card_v2"
    elif any(key in raw for key in ("name", "identity", "traits", "tone")):
        persona = _native(raw, warnings)
        world = _world(raw.get("world") or raw.get("entries"))
        fmt = "jiushi-flat"
    else:
        raise CardError("认不出卡片格式：既不是旧识人格 JSON，也不是角色卡 v2")
    if not persona.get("name"):
        raise CardError("人格卡片缺少名字（name）")
    if not any(persona.get(key) for key in
               ("identity", "traits", "tone", "values", "behavior", "speech_habits")):
        raise CardError("人格卡片没有可用的人设内容")
    return {"format": fmt, "personality": persona, "world": world,
            "warnings": warnings, "filename": filename[:120]}


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
    world_count = len((book or {}).get("entries") or []) if isinstance(book, dict) else 0
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
