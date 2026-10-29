# -*- coding: utf-8 -*-
"""用户情绪轨迹：jev（TypeSafe System One 决策模型）每轮异步判一次。

形态是「跨天的情绪记忆」，不是每轮给主对话塞标签——单轮的情绪聊天模型
自己懂，她做不到的是「他昨天烦躁、今天没提，该不该主动问一句」。

jev 不是聊天模型，协议是它自己的 System One：
    POST {base}/systemone
    {"state": {...要判断的内容...}, "questions": {...类型化问题...}}
返回每个问题的**概率 + 置信度**，而不是一段文本。这个形态天然支持
「低置信度不记录」——误判放大比沉默更伤：把调侃识别成生气然后道歉，非常出戏。

已知限制（官方文档明确说的）：对中文和间接推理偏弱、算数和日期不行——
所以这里只让它做情绪分类这一件事，时刻和日期一律不问它。
"""

from __future__ import annotations

import json
import time
import urllib.request

from app.log import get

log = get("emotion")

NEGATIVE = {"低落", "烦躁", "焦虑", "生气", "委屈"}
CONF_MIN = 0.6          # 置信度门槛：低于这个不记录（宁沉默勿误判）
KEEP_DAYS = 14          # 轨迹只留两周，更老的没有注入价值

MOOD_CRITERIA = {
    "开心": "明显的愉快、满足、兴奋",
    "平静": "没有明显情绪波动，日常陈述",
    "低落": "难过、沮丧、失落、提不起劲",
    "烦躁": "不耐烦、被惹毛了、嫌麻烦",
    "焦虑": "担心、紧张、有压力",
    "生气": "愤怒、被冒犯",
    "委屈": "被误解、被冤枉、付出没被看见",
}
INTENSITY_LEVELS = [
    "几乎没有情绪波动，随口一说",
    "有感觉，但还能正常聊",
    "情绪很明显，话里已经带出来了",
    "情绪很强，快绷不住了",
]


class EmotionTracker:
    """轨迹存 emotion.json（用户的话原文也留一句，总结时引用更自然）。"""

    def __init__(self, path: str, base_url: str, key: str, model: str):
        self.path = path
        self.base_url = (base_url or "").rstrip("/")
        self.key = key or ""
        self.model = model or "jev-1.13"
        self.entries: list[dict] = self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.key)

    # -------------------- 记录 --------------------

    def record(self, user_text: str, context: str = "") -> dict | None:
        """判这一轮的用户情绪并入轨迹。低置信度 / 失败 → None（不记录）。"""
        if not self.enabled or not user_text.strip():
            return None
        state: dict = {"now": user_text[:300]}
        if context:
            state["context"] = context[:300]
        body = json.dumps({
            "model": self.model,
            "state": state,
            "questions": {
                "mood": {
                    "type": "choice",
                    "instructions": ("判断 state.now 里【用户】此刻的真实情绪，不是字面意思——"
                                     "反讽、口是心非、强颜欢笑都要看穿。"
                                     "state.context 只是用来理解处境，不参与情绪判断。"),
                    "criteria": MOOD_CRITERIA,
                },
                "intensity": {
                    "type": "score",
                    "instructions": "state.now 里用户的情绪强度",
                    "criteria": INTENSITY_LEVELS,
                },
            },
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{self.base_url}/systemone", data=body,
                headers={"Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            log.warning("情绪判断失败，本条不入轨迹：%s: %s", type(exc).__name__, exc)
            return None                                    # 静默：轨迹断了不能挂对话
        ans = (d.get("answers") or {})
        mood = (ans.get("mood") or {})
        choice = mood.get("choice")
        conf = float(mood.get("confidence") or 0)
        if choice not in MOOD_CRITERIA or conf < CONF_MIN:
            return None                                    # 低置信度不行动
        score = (ans.get("intensity") or {}).get("score")
        rec = {
            "ts": time.time(),
            "mood": choice,
            "confidence": round(conf, 2),
            "intensity": min(5, max(1, int(round(float(score or 0)) + 1))),
            "quote": user_text.strip()[:40],               # 用户原话，总结时引用更自然
        }
        self.entries.append(rec)
        log.debug("情绪记录：%s 强度%s conf=%.2f", rec["mood"], rec["intensity"], rec["confidence"])
        self.entries = [e for e in self.entries
                        if e["ts"] >= time.time() - KEEP_DAYS * 86400]
        self._save()
        return rec

    def _save(self) -> None:
        """落盘（**原子写**）。

        原来这里是裸 `open(path,"w")` + `except: pass`（T69-④）：① 崩溃时可能留下半截
        JSON；② 写失败**完全静默**——`emotion.json` 会一直停在旧内容而没人知道。
        失败仍不该打断对话（情绪只是加分项），所以只 WARNING、不抛。
        """
        from app.utils.jsonfile import write_json_atomic
        try:
            write_json_atomic(self.path, self.entries)
        except Exception as exc:
            log.warning("情绪轨迹写入失败（这次只在内存里，重启就没了）：%s", exc)

    # -------------------- 注入 --------------------

    def summary(self, now: float | None = None) -> str | None:
        """给注入用的一行总结。没有值得说的就返回 None——沉默优于噪音。"""
        now = now or time.time()
        day = 86400
        recent = [e for e in self.entries if e["ts"] >= now - 2 * day]
        older = [e for e in self.entries if now - 7 * day <= e["ts"] < now - 2 * day]
        if not recent and not older:
            return None

        def neg(es):
            return [e for e in es if e["mood"] in NEGATIVE and e["intensity"] >= 3]

        rn, on = neg(recent), neg(older)
        if len(rn) >= 2:
            last = rn[-1]
            return (f"用户最近情绪偏低：近两天有 {len(rn)} 次负面"
                    f"（最近一次：「{last.get('quote', '')}」），"
                    f"说话轻一点，先接住情绪再谈别的，别急着给建议")
        if rn and not on:
            e = rn[-1]
            return f"用户刚经历了不愉快（「{e.get('quote', '')}」），这轮语气轻一点"
        if on and not rn and recent:
            last = recent[-1]
            if last["mood"] in ("开心", "平静"):
                return "用户之前情绪偏低，这两天明显好转，可以顺着轻松一点的氛围聊"
        return None

    # -------------------- 存取 --------------------

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or not all(
                isinstance(entry, dict)
                and isinstance(entry.get("ts"), (int, float))
                and isinstance(entry.get("mood"), str)
                and isinstance(entry.get("intensity"), (int, float))
                for entry in data
            ):
                log.warning("情绪轨迹格式无效，本次按空轨迹运行；原文件保留：%s",
                            self.path)
                return []
            return data
        except Exception:
            return []
