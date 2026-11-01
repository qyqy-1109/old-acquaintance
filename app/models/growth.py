# -*- coding: utf-8 -*-
"""人格成长层：证据、候选特质、叙事快照和漂移护栏。

这一层不回答「她是谁」（`personality`），也不回答「你们是什么关系」
（`relationship`）。它只回答三件事：

1. **这条改动凭什么发生？** 每条候选特质都带证据：哪一轮、谁说的、原话是什么。
   单次情绪、玩笑、反话只算一条证据，不够转正。
2. **它现在该不该进人设？** 先入候选，攒够独立证据（跨会话）才自动转正；
   不够的留在候选区，下一轮继续攒，不丢。
3. **她有没有在慢慢变成另一个人？** 每次应用前做静态漂移检查：
   底座逐字不变、单次新增有上限、与基线/上一版的相似度不低于阈值。
   固定场景的前后对照仍由 `benchmark/run_persona.py --evolve` 负责。

数据是 `growth.json`，和 `personality.json` 分开放：候选和叙事是「还没进人设」
或「已经发生过的变化」，混进人设文件会污染提示词与人工编辑。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from typing import Any


DEFAULT_GROWTH: dict[str, Any] = {
    "version": 1,
    "candidates": [],
    "narrative": [],
    "drift": {"baseline": None, "last": None},
    "stats": {"observed": 0, "promoted": 0, "held": 0, "blocked": 0},
}

# 只有这些字段会走「候选 → 转正」。cares / favorite_places 属于底座偏好，
# 仍走 personality.pending 的人审通道，不在这里自动转正。
LIST_FIELDS = {"traits", "values", "behavior", "speech_habits", "greeting"}
SCALAR_FIELDS = {"identity", "tone"}
HIGH_RISK_FIELDS = {"identity", "values"}

# 漂移检查只看这些字段；version/updated_at 不算人格内容。
DRIFT_FIELDS = ("name", "identity", "traits", "tone", "values", "behavior",
                "speech_habits", "greeting", "origin", "family", "daily_life",
                "cares", "favorite_places", "self_attitude")
PROTECTED_FIELDS = ("name", "scope", "origin", "family", "daily_life", "self_attitude")

MAX_CANDIDATES = 200
MAX_NARRATIVE = 40
MAX_EVIDENCE_PER_CANDIDATE = 8


def texts_of(value) -> list[str]:
    """把字符串 / 字符串数组 / `{text, _pinned}` 数组拉平成文本。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _candidate_id(field: str, text: str) -> str:
    digest = hashlib.sha1(f"{field}\x1f{text}".encode("utf-8")).hexdigest()[:12]
    return f"cand-{digest}"


class GrowthBook:
    """`growth.json` 的读写与规则；不调用模型，结果稳定可测。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_GROWTH)
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            # 成长层是派生数据，但叙事和候选也不能静默丢；留档后从空档案继续。
            dst = self.path + ".corrupt"
            if os.path.exists(dst):
                dst = f"{dst}.{int(time.time())}"
            try:
                os.replace(self.path, dst)
            except OSError:
                dst = ""
            from app.log import get
            get("growth").error("成长层文件解析失败，已留档为 %s：%s",
                                os.path.basename(dst) if dst else "（留档失败，原文件未动）",
                                exc)
            return
        if not isinstance(raw, dict):
            return
        for key, default in DEFAULT_GROWTH.items():
            value = raw.get(key, copy.deepcopy(default))
            if isinstance(default, list) and not isinstance(value, list):
                value = copy.deepcopy(default)
            if isinstance(default, dict) and not isinstance(value, dict):
                value = copy.deepcopy(default)
            self.data[key] = value
        for key, default in DEFAULT_GROWTH["stats"].items():
            self.data["stats"].setdefault(key, default)
        self.data["candidates"] = [c for c in self.data["candidates"]
                                   if isinstance(c, dict) and c.get("id")]
        for candidate in self.data["candidates"]:
            if not isinstance(candidate.get("evidence"), list):
                candidate["evidence"] = []
            candidate["observations"] = len(candidate["evidence"])
        self.data["narrative"] = [n for n in self.data["narrative"]
                                  if isinstance(n, dict) and n.get("text")]

    def _save(self) -> None:
        # 延迟导入：避免 `app.models` 初始化时和 log/config/jsonfile 形成循环。
        from app.utils.jsonfile import write_json_atomic
        write_json_atomic(self.path, self.data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)

    # ---------- 候选 ----------
    @staticmethod
    def _clean_evidence(evidence, now: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for item in evidence or []:
            if isinstance(item, str):
                meta: dict[str, Any] = {}
                quote = item
            elif isinstance(item, dict):
                meta = item
                quote = str(item.get("quote") or "")
            else:
                continue
            quote = " ".join(quote.split())[:200]
            if not quote:
                continue
            try:
                seq = int(meta.get("seq", -1))
            except (TypeError, ValueError):
                seq = -1
            key = (str(meta.get("session") or ""), seq, quote)
            if key in seen:
                continue
            seen.add(key)
            try:
                at = float(meta.get("at") or now)
            except (TypeError, ValueError):
                at = now
            out.append({
                "session": str(meta.get("session") or ""),
                "seq": seq,
                "at": at,
                "role": str(meta.get("role") or "user"),
                "quote": quote,
            })
            if len(out) >= MAX_EVIDENCE_PER_CANDIDATE:
                break
        return out

    def observe(self, field: str, text: str, evidence, confidence: float = 0.6,
                now: float | None = None, source: str = "auto") -> dict[str, Any] | None:
        """记一条候选；同一字段同一文本只累加证据，不重复占位。"""
        field = str(field or "").strip()
        text = " ".join(str(text or "").split())[:120]
        if field not in LIST_FIELDS | SCALAR_FIELDS or not text:
            return None
        now = float(now if now is not None else time.time())
        ev = self._clean_evidence(evidence, now)
        if not ev:
            return None
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.6
        cid = _candidate_id(field, text)
        with self._lock:
            found = next((c for c in self.data["candidates"] if c.get("id") == cid), None)
            if found is None:
                found = {
                    "id": cid,
                    "field": field,
                    "text": text,
                    "status": "candidate",
                    "confidence": conf,
                    "evidence": [],
                    "first_seen": now,
                    "last_seen": now,
                    "last_reinforced": now,
                    "observations": 0,
                    "source": source,
                    "reason": "",
                }
                self.data["candidates"].append(found)
            if found.get("status") == "active":
                return copy.deepcopy(found)
            seen = {(e.get("session"), e.get("seq"), e.get("quote"))
                    for e in found.get("evidence") or []}
            for item in ev:
                key = (item["session"], item["seq"], item["quote"])
                if key in seen or len(found["evidence"]) >= MAX_EVIDENCE_PER_CANDIDATE:
                    continue
                seen.add(key)
                found["evidence"].append(item)
            found["confidence"] = max(float(found.get("confidence") or 0), conf)
            found["last_seen"] = now
            found["last_reinforced"] = now
            found["observations"] = len(found["evidence"])
            if found.get("status") == "rejected":
                found["status"] = "candidate"
                found["reason"] = ""
            self.data["stats"]["observed"] = int(self.data["stats"].get("observed") or 0) + 1
            self._trim_candidates()
            self._save()
            return copy.deepcopy(found)

    def _trim_candidates(self) -> None:
        candidates = self.data["candidates"]
        if len(candidates) <= MAX_CANDIDATES:
            return
        # 只淘汰「候选」状态里最老、证据最少的；已转正/被拒的历史留着可追溯。
        candidates.sort(key=lambda c: (
            0 if c.get("status") == "candidate" else 1,
            float(c.get("last_seen") or 0),
            int(c.get("observations") or 0),
        ))
        keep = candidates[:MAX_CANDIDATES]
        self.data["candidates"] = keep

    @staticmethod
    def _min_evidence(field: str) -> int:
        # 身份和价值观改错代价最大：至少三条独立证据，且必须跨会话。
        return 3 if field in HIGH_RISK_FIELDS else 2

    def eligible(self, candidate: dict, *, min_confidence: float = 0.55) -> bool:
        if not isinstance(candidate, dict) or candidate.get("status") != "candidate":
            return False
        field = str(candidate.get("field") or "")
        if int(candidate.get("observations") or 0) < self._min_evidence(field):
            return False
        try:
            confidence = float(candidate.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        if confidence < min_confidence:
            return False
        if field in HIGH_RISK_FIELDS:
            sessions = {str(e.get("session") or "") for e in candidate.get("evidence") or []}
            if len(sessions) < 2:
                return False
        return True

    def ready_patch(self, personality, *, min_confidence: float = 0.55,
                    max_new_items: int = 3, touched: set[str] | None = None
                    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """把够格的候选拼成补丁；返回 (patch, promoted_ids, held_ids)。

        `touched` 是本次新观察到、但还不够格的候选：只把这些报成「暂缓」，
        不把历史候选每轮都重复念一遍。
        """
        touched = touched or set()
        patch: dict[str, Any] = {}
        promoted: list[str] = []
        remaining = max(0, int(max_new_items))
        with self._lock:
            ordered = sorted(
                self.data["candidates"],
                key=lambda c: (float(c.get("last_seen") or 0),
                               int(c.get("observations") or 0)),
                reverse=True,
            )
            for candidate in ordered:
                field = str(candidate.get("field") or "")
                if field not in LIST_FIELDS | SCALAR_FIELDS:
                    continue
                if not self.eligible(candidate, min_confidence=min_confidence):
                    continue
                text = str(candidate.get("text") or "").strip()
                if not text:
                    continue
                if field in SCALAR_FIELDS:
                    if str(personality.data.get(field) or "").strip() == text:
                        promoted.append(candidate["id"])
                        continue
                    if field in patch:
                        continue
                    patch[field] = text
                    promoted.append(candidate["id"])
                    continue
                current = texts_of(personality.data.get(field))
                if text in current:
                    # 人设里已经有了：候选可以标 active，不需要再改一次。
                    promoted.append(candidate["id"])
                    continue
                if text in patch.get(field, []):
                    continue
                if remaining <= 0:
                    continue
                if len(patch.get(field, current)) >= getattr(personality, "MAX_ITEMS", 8):
                    continue
                patch.setdefault(field, list(current)).append(text)
                promoted.append(candidate["id"])
                remaining -= 1
        promoted_set = set(promoted)
        held = [c["id"] for c in self.data["candidates"]
                if c.get("id") in touched and c.get("id") not in promoted_set
                and c.get("status") == "candidate"]
        return patch, promoted, sorted(set(held))

    def mark_applied(self, ids: list[str], personality, now: float | None = None) -> None:
        """应用成功后按人设现状标记：真的进去了才算 active，被护栏摘掉算 rejected。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            changed = False
            for cid in ids or []:
                candidate = next((c for c in self.data["candidates"]
                                  if c.get("id") == cid), None)
                if candidate is None:
                    continue
                field = str(candidate.get("field") or "")
                text = str(candidate.get("text") or "")
                value = personality.data.get(field)
                present = (str(value or "").strip() == text if field in SCALAR_FIELDS
                           else text in texts_of(value))
                if present:
                    candidate["status"] = "active"
                    candidate["promoted_at"] = now
                    candidate["reason"] = ""
                    self.data["stats"]["promoted"] = (
                        int(self.data["stats"].get("promoted") or 0) + 1)
                else:
                    candidate["status"] = "rejected"
                    candidate["reason"] = "应用时被护栏拦下"
                    self.data["stats"]["blocked"] = (
                        int(self.data["stats"].get("blocked") or 0) + 1)
                changed = True
            if changed:
                self._save()

    def reconcile(self, personality, now: float | None = None) -> int:
        """人设被人工改动后，把已经出现在人设里的候选标成 active。"""
        now = float(now if now is not None else time.time())
        changed = 0
        with self._lock:
            for candidate in self.data["candidates"]:
                if candidate.get("status") != "candidate":
                    continue
                field = str(candidate.get("field") or "")
                text = str(candidate.get("text") or "")
                value = personality.data.get(field)
                present = (str(value or "").strip() == text if field in SCALAR_FIELDS
                           else text in texts_of(value))
                if present:
                    candidate["status"] = "active"
                    candidate["promoted_at"] = now
                    candidate["reason"] = "人工采纳"
                    changed += 1
            if changed:
                self.data["stats"]["promoted"] = (
                    int(self.data["stats"].get("promoted") or 0) + changed)
                self._save()
        return changed

    def candidate_summary(self, ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(ids or [])
        return [copy.deepcopy(c) for c in self.data["candidates"]
                if c.get("id") in wanted]

    # ---------- 叙事 ----------
    def add_narrative(self, version: int, changes: list[str], stage: str = "",
                      now: float | None = None) -> dict[str, Any] | None:
        parts = [" ".join(str(x).split())[:100] for x in (changes or []) if str(x).strip()]
        if not parts:
            return None
        text = "；".join(parts[:3])
        if stage and stage not in text:
            text += f"；关系：{stage}"
        item = {"version": int(version or 0), "at": float(now or time.time()),
                "text": text[:260]}
        with self._lock:
            self.data["narrative"].append(item)
            self.data["narrative"] = self.data["narrative"][-MAX_NARRATIVE:]
            self._save()
        return copy.deepcopy(item)

    def render_prompt(self, limit: int = 2) -> str:
        items = self.data.get("narrative") or []
        if not items:
            return ""
        lines = ["【我的成长】"]
        for item in items[-max(1, int(limit)):]:
            lines.append(f"第 {item.get('version', '?')} 版：{item.get('text', '')}")
        lines.append("（这些是你已经发生的变化，不是这轮的新设定；不要主动汇报成长，"
                     "也不要因此改口或反复提它。）")
        return "\n".join(lines)

    # ---------- 漂移 ----------
    def reset_for(self, personality, now: float | None = None) -> None:
        """换人设/导入新卡后重置对照基准：旧候选和旧基线不属于新人格。"""
        with self._lock:
            self.data["candidates"] = []
            self.data["narrative"] = []
            self.data["stats"] = copy.deepcopy(DEFAULT_GROWTH["stats"])
            self.data["drift"] = {
                "baseline": {
                    "at": float(now or time.time()),
                    "data": {k: copy.deepcopy(personality.data.get(k))
                             for k in DRIFT_FIELDS},
                },
                "last": None,
            }
            self._save()

    @staticmethod
    def _tokens(data: dict) -> set[str]:
        blob_parts: list[str] = []
        for key in DRIFT_FIELDS:
            blob_parts.extend(texts_of(data.get(key)))
        blob = " ".join(blob_parts).lower()
        units = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", blob)
        tokens = set(units)
        for left, right in zip(units, units[1:]):
            if len(left) == 1 and len(right) == 1:
                tokens.add(left + right)
        return tokens

    @classmethod
    def similarity(cls, before: dict, after: dict) -> float:
        a, b = cls._tokens(before), cls._tokens(after)
        if not a and not b:
            return 1.0
        union = a | b
        return len(a & b) / len(union) if union else 1.0

    def ensure_baseline(self, data: dict, now: float | None = None) -> None:
        with self._lock:
            if self.data.get("drift", {}).get("baseline"):
                return
            self.data.setdefault("drift", {})["baseline"] = {
                "at": float(now or time.time()),
                "data": {k: copy.deepcopy(data.get(k)) for k in DRIFT_FIELDS},
            }
            self._save()

    def check_drift(self, before: dict, after: dict, *, max_new_items: int = 3,
                    min_similarity: float = 0.55,
                    min_baseline_similarity: float = 0.35) -> tuple[bool, list[str], dict]:
        issues: list[str] = []
        for field in PROTECTED_FIELDS:
            if before.get(field) != after.get(field):
                issues.append(f"{field} 被改动（底座必须逐字不变）")
        added = 0
        for field in LIST_FIELDS:
            old = set(texts_of(before.get(field)))
            new = set(texts_of(after.get(field)))
            added += len(new - old)
        if added > max_new_items:
            issues.append(f"一次新增 {added} 条，超过上限 {max_new_items}")
        changed_scalars = sum(1 for f in SCALAR_FIELDS if before.get(f) != after.get(f))
        similarity = self.similarity(before, after)
        if similarity < min_similarity:
            issues.append(f"与上一版相似度 {similarity:.2f}，低于 {min_similarity:.2f}")
        baseline = (self.data.get("drift") or {}).get("baseline") or {}
        baseline_similarity = None
        if isinstance(baseline.get("data"), dict):
            baseline_similarity = self.similarity(baseline["data"], after)
            if baseline_similarity < min_baseline_similarity:
                issues.append(
                    f"与基线相似度 {baseline_similarity:.2f}，低于 {min_baseline_similarity:.2f}")
        metrics = {
            "at": time.time(),
            "added_items": added,
            "changed_scalars": changed_scalars,
            "similarity": round(similarity, 4),
            "baseline_similarity": (round(baseline_similarity, 4)
                                    if baseline_similarity is not None else None),
            "ok": not issues,
            "issues": issues,
        }
        return not issues, issues, metrics

    def record_drift(self, metrics: dict, *, version: int = 0) -> None:
        with self._lock:
            metrics = dict(metrics or {})
            metrics["version"] = int(version or 0)
            self.data.setdefault("drift", {})["last"] = metrics
            if not metrics.get("ok"):
                self.data["stats"]["blocked"] = (
                    int(self.data["stats"].get("blocked") or 0) + 1)
            self._save()
