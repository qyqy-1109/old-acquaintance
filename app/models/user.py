"""User：agent 对用户的理解，由分析模型从对话中提炼，可持续迭代。

思路：不让聊天模型兼职记人，而是另起一次调用，把"最近对话 + 已有画像"
交给分析模型，让它输出更新后的结构化画像。普通更新合并保留旧信息，明确纠正
移出当前画像并保存历史，避免因为上下文窗口被挤掉而丢失。
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from typing import Any

from app.utils.llmjson import chat_json, parse_json

from app.log import get

log = get("user")

_PROFILE_LIST_FIELDS = {"facts", "preferences", "topics"}
_PROFILE_MAP_FIELDS = {"expertise"}
_PROFILE_SCALAR_FIELDS = {"summary", "communication"}
_PROFILE_FIELDS = _PROFILE_LIST_FIELDS | _PROFILE_MAP_FIELDS | _PROFILE_SCALAR_FIELDS


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value
            if isinstance(item, str) and item.strip()]


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key.strip(): item.strip() for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
            and key.strip() and item.strip()}

EMPTY_PROFILE: dict[str, Any] = {
    "summary": "",
    "facts": [],       # 用户明确说过的事实：名字、职业、生日、偏好
    "preferences": [],  # 表达方式、回答风格上的偏好
    "expertise": {},    # 主题 -> 水平，如 {"Python": "熟练"}
    "communication": "",
    "topics": [],       # 关心的话题
    "history": [],
    "version": 0,
    "updated_at": 0,
}

ANALYZE_PROMPT = """你是用户画像分析师。请根据【已有画像】和【最近用户原话】，输出更新后的完整用户画像。

【已有画像】
{old}

【最近用户原话】
{dialog}

要求：
1. 保留已有画像里的信息，普通更新只补充，不要因为本轮没提到就丢弃。
2. 只把用户原话中关于用户自己的内容作为新依据；用户转述她或他人的经历不能写成用户事实，不要臆测、不要脑补。
3. 用户明确纠正旧信息时，在 corrections 列出修正：field 仅限 facts/preferences/topics，old 必须逐字复制已有条目，new 是非空的新条目，quote 必须逐字引用本批用户原话中的依据。没有明确纠正就返回空列表。
4. 每次明确修正后同步更新 summary；修正偏好时也同步更新 communication。
5. 只输出一个 JSON 对象，不要任何解释文字，字段如下：
   {{"summary": "一句话概括这位用户",
    "facts": ["事实1", "事实2"],
    "preferences": ["偏好1"],
    "expertise": {{"主题": "水平"}},
    "communication": "沟通风格描述",
    "topics": ["话题1"],
    "corrections": [{{"field": "facts", "old": "已有事实原文", "new": "修正后的事实", "quote": "用户纠正原话"}}]}}
"""


class UserProfile:
    def __init__(self, client, path: str = "user_profile.json", model: str = "glm-5.3-flash"):
        self.client = client
        self.path = path
        self.model = model
        self.data: dict[str, Any] = deepcopy(EMPTY_PROFILE)
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as exc:
                log.error("读取失败，用空画像：%s", exc)

    def _save(self) -> None:
        # 原子写（见 app/utils/jsonfile.py）：画像是长期积累、最难重建的数据之一，
        # 不能因为一次崩溃/断电就变成半截 JSON。
        from app.utils.jsonfile import write_json_atomic
        write_json_atomic(self.path, self.data)

    # ---------- 渲染 ----------
    def render(self) -> str:
        """渲染成注入 system prompt 的文本；没内容时返回空串。"""
        d = self.data
        summary = d.get("summary") if isinstance(d.get("summary"), str) else ""
        facts = _string_list(d.get("facts"))
        expertise = _string_map(d.get("expertise"))
        preferences = _string_list(d.get("preferences"))
        communication = d.get("communication") if isinstance(d.get("communication"), str) else ""
        if not any((summary, facts, expertise, preferences, communication)):
            return ""
        parts = [f"[关于这位用户] {summary}"]
        if facts:
            parts.append("已知：" + "；".join(facts))
        if expertise:
            parts.append("水平：" + "；".join(f"{k}={v}" for k, v in expertise.items()))
        if preferences:
            parts.append("偏好：" + "；".join(preferences))
        if communication:
            parts.append("沟通：" + communication)
        return "\n".join(p for p in parts if p)

    def summary(self) -> str:
        d = self.data
        n = len(_string_list(d.get("facts")))
        return f"画像 v{d.get('version', 0)}　|　事实 {n} 条　|　{str(d.get('summary', ''))[:40]}"

    # ---------- 分析迭代 ----------
    def analyze(self, turns: list[dict], max_chars: int = 3000) -> str:
        """拿最近对话更新画像，返回变更说明。"""
        user_texts = [str(turn.get("content", "")) for turn in turns
                      if turn.get("role") == "user" and str(turn.get("content", "")).strip()]
        if not user_texts:
            return ""
        dialog = "\n".join(
            f"user: {text[:400]}" for text in user_texts
        )[-max_chars:]

        prompt = ANALYZE_PROMPT.format(
            old=json.dumps({key: value for key, value in self.data.items() if key != "history"},
                           ensure_ascii=False), dialog=dialog
        )
        data, err = chat_json(
            self.client, self.model,
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            # 推理模型会先消耗大半额度思考，给小了 JSON 会被截断
            max_tokens=3000,
            retries=1,  # 画像隔几轮才跑一次，但截断一次就白跑，值得重试
        )
        if data is None:
            return err
        new = data

        before = self.data.get("summary", "")
        # **合并，而不是整体覆盖**（T70-③）：提示词里写着"只增不删"，但那只是提示——
        # 模型漏返回一个字段，旧画像就被整片抹掉（清单类字段丢一条就找不回来了）。
        # 口径：列表取并集（只增不删、去重），字符串只在非空时覆盖，version/updated_at 自己算。
        merged = deepcopy(self.data)
        for key, value in (new or {}).items():
            if key not in _PROFILE_FIELDS:
                continue
            if key in _PROFILE_LIST_FIELDS:
                old = _string_list(merged.get(key))
                merged[key] = old + [v for v in _string_list(value) if v not in old]
            elif key in _PROFILE_MAP_FIELDS:
                old = _string_map(merged.get(key))
                for item_key, item_value in _string_map(value).items():
                    old[item_key] = item_value
                merged[key] = old
            elif isinstance(value, str):
                if value.strip():
                    merged[key] = value
        updated_at = time.time()
        corrections = new.get("corrections", [])
        corrected = set()
        if not isinstance(merged.get("history"), list):
            merged["history"] = []
        for correction in corrections if isinstance(corrections, list) else []:
            if not isinstance(correction, dict):
                continue
            field, old, replacement, quote = (
                correction.get(key) for key in ("field", "old", "new", "quote")
            )
            if (field not in ("facts", "preferences", "topics")
                    or not all(isinstance(value, str) for value in (old, replacement, quote))
                    or not replacement.strip() or not quote.strip() or old == replacement
                    or old.strip() not in _string_list(self.data.get(field))
                    or quote not in dialog or old.strip() in corrected):
                log.warning("忽略画像修正：字段、旧条目或用户原话证据无效")
                continue
            for list_field in _PROFILE_LIST_FIELDS:
                merged[list_field] = [value for value in _string_list(merged.get(list_field))
                                      if value != old.strip()]
            values = merged[field]
            if replacement not in values:
                values.append(replacement)
            merged[field] = values
            merged.setdefault("history", []).append({
                "field": field, "old": old, "new": replacement,
                "quote": quote, "ts": updated_at,
            })
            corrected.add(old.strip())
        if corrected:
            merged["summary"] = "；".join(_string_list(merged.get("facts")))
            merged["communication"] = "；".join(_string_list(merged.get("preferences")))
        merged["version"] = int(self.data.get("version", 0)) + 1
        merged["updated_at"] = updated_at
        self.data = merged
        self._save()

        after = merged.get("summary", "")
        if before != after:
            return f"画像已更新（v{merged['version']}）：{after}"
        return f"画像已刷新（v{merged['version']}），共 {len(_string_list(merged.get('facts')))} 条事实"

    @staticmethod
    def _parse(text: str) -> dict | None:
        return parse_json(text)  # 兼容旧调用：解析逻辑统一放在 llmjson

    def manual(self, text: str) -> str:
        self.data.setdefault("facts", []).append(text)
        self.data["version"] = int(self.data.get("version", 0)) + 1
        self.data["updated_at"] = time.time()
        self._save()
        return f"已记下：{text}"
