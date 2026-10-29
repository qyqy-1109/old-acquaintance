# -*- coding: utf-8 -*-
"""你们的关系层（`#146`）：她怎么称呼你、你们到哪一步了、她答应过什么。

**为什么单开一层**（用户定的"第 4 条"，方向是「虚拟 AI 女友」）：
人设回答"她是谁"，编剧层回答"她的一天"，画像回答"你是谁"——而
「**你们是什么关系**」原先没有任何地方存：称呼从哪来、说过的话算不算数、
熟到哪一步该换个叫法，全靠模型每轮即兴。女友向的体验恰恰全在这里。

**设计取向**（沿用项目一贯的反幻觉取向 ＋ 手写慢变层的规矩）：
· 慢变、手写（和 `life.json`、人设的自我底座同类），**不进 LLM 的自动演化通道**；
· 阶段**按可数的事实取得资格**（认识天数／你说过的话条数），是否推进由她自己决定，
  阈值写在文件里、你随时能改，
  也可以用 `/relationship stage <key>` 手动定；
· 它只提供**事实与口吻**，不替模型写台词。
"""

from __future__ import annotations

import datetime as dt
import copy
import json
import math
import os
import threading
import time
from functools import wraps
from typing import Any

from app.utils.jsonfile import write_json_atomic

DEFAULT_RELATIONSHIP: dict[str, Any] = {
    "hint": "你们的关系。默认由她自己决定是否推进；stages 的阈值只作资格线（天数或消息数，满足其一）；"
            "her_own 是**只有她自己知道**的东西——她不许拿这些反问他，机器也会拦。",
    "since": "",                  # 认识于（YYYY-MM-DD）；留空 = 建这个文件那天
    "decide": "she",              # `#148`：she = **她自己决定**推进（默认）｜auto = 按阈值自动
    "stage": "",                  # 当前阶段（她自己推进到这里；留空 = 第一档）
    "changed_at": 0,              # 上次改口的时间（冷却用）
    "min_days_per_step": 3,       # 两次改口至少隔几天（护栏：推进要慢）
    "stages": [
        {"key": "刚认识", "call_him": "你", "min_days": 0, "min_msgs": 0},
        {"key": "熟络", "call_him": "阿哲", "min_days": 3, "min_msgs": 60},
        {"key": "亲近", "call_him": "阿哲", "min_days": 10, "min_msgs": 200},
        {"key": "亲密", "call_him": "亲爱的", "min_days": 30, "min_msgs": 500},
    ],
    "call_note": "",              # 称呼上的补充（例如"闹着玩的时候会叫全名"）
    "promises": [],               # 她答应过他的事（完成/失约都保留历史）
    "rituals": [],                # 你们的固定节目（"睡前说一句晚安"）
    "care_style": "关心他的作息和吃饭；他诉苦时先陪着，明确想要办法时再一起想；"
                  "偶尔撒娇、小吃醋，但不查岗、不追问到底。",
    "her_own": ["团子", "球衣", "课表", "出租屋", "阳台"],
}


def _today(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(float(now if now is not None else time.time())).strftime("%Y-%m-%d")


def _locked(method):
    @wraps(method)
    def synchronized(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return synchronized


def _timestamp(value) -> float:
    try:
        stamp = float(value or 0)
        return stamp if math.isfinite(stamp) and stamp > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


class Relationship:
    """`relationship.json` 的读写：阶段、称呼、承诺、关心方式、她自己的东西。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT_RELATIONSHIP)
        raw: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError(f"关系文件必须是 JSON 对象：{path}")
        else:
            self.data["since"] = _today()
            self._write()
        for k, v in raw.items():
            self.data[k] = v
        if not self.data.get("since"):
            # 老文件没写 since：以文件创建时间为准，别让"认识第 1 天"永远重置
            self.data["since"] = _today(os.path.getctime(path) if os.path.exists(path) else None)
            self._write()
        if self._normalise_promises():
            self._write()

    # ---------- 读写 ----------
    def _write(self) -> None:
        write_json_atomic(self.path, self.data)

    @_locked
    def save(self) -> None:
        self._write()

    # ---------- 承诺与关系事件 ----------
    def _normalise_promises(self) -> bool:
        """把旧版字符串承诺升级成带状态的记录，保留原文。"""
        raw = self.data.get("promises") or []
        rows: list[dict[str, Any]] = []
        changed = not isinstance(raw, list)
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, str):
                text = item.strip()[:60]
                if not text:
                    changed = True
                    continue
                rows.append({"text": text, "status": "open", "created_at": 0})
                changed = True
                continue
            if not isinstance(item, dict):
                changed = True
                continue
            text = str(item.get("text") or "").strip()[:60]
            if not text:
                changed = True
                continue
            status = str(item.get("status") or "open").strip().lower()
            if status not in ("open", "done", "missed"):
                status = "open"
                changed = True
            row = dict(item)
            row["text"] = text
            row["status"] = status
            row.setdefault("created_at", 0)
            if row != item:
                changed = True
            rows.append(row)
        if raw != rows:
            changed = True
        reserved = {str(row.get("id")) for row in rows
                    if str(row.get("id") or "").startswith("p")
                    and str(row.get("id"))[1:].isdigit()}
        next_id = max([int(handle[1:]) for handle in reserved] or [0]) + 1
        used = set()
        for row in rows:
            handle = str(row.get("id") or "")
            if handle not in reserved or handle in used:
                handle = f"p{next_id}"
                next_id += 1
                row["id"] = handle
                changed = True
            used.add(handle)
        self.data["promises"] = rows
        if not isinstance(self.data.get("events"), list):
            self.data["events"] = []
            changed = True
        return changed

    @_locked
    def promise_items(self, include_closed: bool = False) -> list[dict[str, Any]]:
        """返回承诺快照；默认只返回仍待履约的承诺。"""
        self._normalise_promises()
        rows = self.data.get("promises") or []
        if not include_closed:
            rows = [row for row in rows if row.get("status") == "open"]
        return [dict(row) for row in rows]

    @_locked
    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近的关系事件，顺序从旧到新，便于看完整闭环。"""
        rows = self.data.get("events") or []
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows[-max(1, int(limit)):]
                if isinstance(row, dict)]

    @_locked
    def record_event(self, kind: str, text: str, source: str = "system",
                     promise: str = "", now: float | None = None) -> dict[str, Any]:
        event = {"ts": float(now if now is not None else time.time()),
                 "kind": str(kind), "text": str(text)[:120], "source": str(source)}
        if str(promise or "").strip():
            event["promise"] = str(promise)[:60]
        self.data.setdefault("events", []).append(event)
        self.data["events"] = self.data["events"][-100:]
        return event

    def _open_promise(self, handle: str) -> tuple[dict[str, Any] | None, str]:
        handle = str(handle).strip()
        if handle.startswith("p"):
            row = next((row for row in self.data.get("promises", [])
                        if row.get("id") == handle), None)
            if row is None:
                return None, "没有这条承诺"
            if row.get("status") != "open":
                return None, "这条承诺已有结果，不能重复修改"
            return row, ""
        try:
            wanted = int(str(handle).strip())
        except (TypeError, ValueError):
            return None, "用法：/relationship done <序号>"
        if wanted < 1:
            return None, "序号必须从 1 开始"
        opened = [row for row in self.data.get("promises", [])
                  if row.get("status") == "open"]
        if wanted > len(opened):
            return None, f"序号超出范围（现在有 {len(opened)} 条待履约承诺）"
        return opened[wanted - 1], ""

    @_locked
    def complete_promise(self, handle: str, note: str = "",
                         source: str = "human", now: float | None = None) -> str:
        self._normalise_promises()
        row, error = self._open_promise(handle)
        if row is None:
            return error
        stamp = float(now if now is not None else time.time())
        before = copy.deepcopy(self.data)
        try:
            row["status"] = "done"
            row["completed_at"] = stamp
            if str(note or "").strip():
                row["note"] = str(note).strip()[:120]
            self.record_event("promise_completed", "承诺已完成", source,
                              promise=row["text"], now=stamp)
            self.save()
        except Exception:
            self.data = before
            raise
        return f"已完成：{row['text']}"

    @_locked
    def miss_promise(self, handle: str, reason: str, source: str = "human",
                     now: float | None = None) -> str:
        self._normalise_promises()
        reason = str(reason or "").strip()[:120]
        if not reason:
            return "失约需要写原因，不能无声无息地划掉。用法：/relationship miss <序号> <原因>"
        row, error = self._open_promise(handle)
        if row is None:
            return error.replace("done", "miss")
        stamp = float(now if now is not None else time.time())
        before = copy.deepcopy(self.data)
        try:
            row["status"] = "missed"
            row["missed_at"] = stamp
            row["reason"] = reason
            self.record_event("promise_missed", reason, source,
                              promise=row["text"], now=stamp)
            self.save()
        except Exception:
            self.data = before
            raise
        return f"记下了这次没做到：{row['text']}（原因：{reason}）"

    @_locked
    def create_promise(self, text: str, due_at: float | None = None) -> dict[str, Any]:
        clean = str(text or "").strip()
        if not clean or len(clean) > 60:
            raise ValueError("承诺需要 1–60 个字")
        if due_at is not None and not _timestamp(due_at):
            raise ValueError("到期时间必须是有效的正时间戳")
        self._normalise_promises()
        next_id = max([int(row["id"][1:]) for row in self.data["promises"]] or [0]) + 1
        row = {"id": f"p{next_id}", "text": clean, "status": "open", "created_at": time.time()}
        if due_at is not None:
            row["due_at"] = float(due_at)
        before = copy.deepcopy(self.data)
        try:
            self.data["promises"].append(row)
            self.record_event("promise_created", "记下了承诺", "human", promise=clean)
            self.save()
        except Exception:
            self.data = before
            raise
        return dict(row)

    @_locked
    def update_promise(self, promise_id: str, status: str, note: str = "", reason: str = "") -> dict[str, Any]:
        self._normalise_promises()
        row = next((row for row in self.data["promises"] if row["id"] == promise_id), None)
        if row is None:
            raise KeyError("没有这条承诺")
        if row["status"] != "open":
            raise ValueError("这条承诺已有结果，不能重复修改")
        if status not in ("done", "missed") or (status == "missed" and not reason.strip()):
            raise ValueError("失约需要填写原因")
        if status == "done":
            self.complete_promise(promise_id, note=note)
        else:
            self.miss_promise(promise_id, reason=reason)
        return next(item for item in self.promise_items(include_closed=True)
                    if item["id"] == promise_id)

    @_locked
    def due_promises(self, now: float | None = None) -> list[dict[str, Any]]:
        stamp = float(now if now is not None else time.time())
        return [row for row in self.promise_items()
                if 0 < _timestamp(row.get("due_at")) <= stamp
                and not _timestamp(row.get("reminded_at"))]

    @_locked
    def mark_promise_reminded(self, promise_id: str, now: float | None = None) -> bool:
        stamp = float(now if now is not None else time.time())
        if promise_id not in {row["id"] for row in self.due_promises(stamp)}:
            return False
        before = copy.deepcopy(self.data)
        try:
            row, _error = self._open_promise(promise_id)
            row["reminded_at"] = stamp
            self.record_event("promise_reminded", "到期承诺已提醒，结果待确认", "she",
                              promise=row["text"], now=stamp)
            self.save()
        except Exception:
            self.data = before
            raise
        return True

    @_locked
    def board(self, now: float | None = None, user_msgs: int = 0) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        promises = self.promise_items(include_closed=True)
        for row in promises:
            row["overdue"] = row["status"] == "open" and 0 < _timestamp(row.get("due_at")) <= stamp
        return {"stage": dict(self.stage(stamp, user_msgs)), "since": self.data.get("since"),
                "days": self.days(stamp), "call_him": self.call_him(stamp, user_msgs),
                "decide": self.data.get("decide") or "she", "user_msgs": int(user_msgs),
                "promises": promises, "events": self.events(30),
                "history": copy.deepcopy(self.data.get("history") or [])}

    # ---------- 事实 ----------
    def days(self, now: float | None = None) -> int:
        """认识第几天（今天算第 1 天）。"""
        try:
            start = dt.datetime.strptime(str(self.data.get("since")), "%Y-%m-%d").date()
        except ValueError:
            return 1
        today = dt.datetime.fromtimestamp(float(now if now is not None else time.time())).date()
        return max(1, (today - start).days + 1)

    def stages(self) -> list[dict[str, Any]]:
        rows = [s for s in (self.data.get("stages") or []) if isinstance(s, dict) and s.get("key")]
        return rows or list(DEFAULT_RELATIONSHIP["stages"])

    def stage(self, now: float | None = None, user_msgs: int = 0) -> dict[str, Any]:
        """当前阶段。

        **默认口径（`#148`）：她自己决定**——`stage` 就是她推进到的那一档，
        阈值（天数／消息数）只当"她**够不够**进下一档"的**资格线**，不替她做主。
        `decide=auto` 时才回到旧口径（按阈值自动跳），留作对照与测试。
        """
        rows = self.stages()
        if str(self.data.get("decide") or "she") == "auto":
            days = self.days(now)
            met = [s for s in rows
                   if days >= int(s.get("min_days") or 0) or int(user_msgs) >= int(s.get("min_msgs") or 0)]
            return met[-1] if met else rows[0]
        key = str(self.data.get("stage") or "").strip() or str(rows[0].get("key"))
        for s in rows:
            if str(s.get("key")) == key:
                return s
        return rows[0]

    def eligible(self, now: float | None = None, user_msgs: int = 0) -> dict[str, Any]:
        """按阈值她**最多够得到**哪一档（是资格线，不是决定）。"""
        rows = self.stages()
        days = self.days(now)
        met = [s for s in rows
               if days >= int(s.get("min_days") or 0) or int(user_msgs) >= int(s.get("min_msgs") or 0)]
        return met[-1] if met else rows[0]

    def next_stage(self, now: float | None = None, user_msgs: int = 0) -> dict[str, Any] | None:
        rows = self.stages()
        keys = [str(s.get("key")) for s in rows]
        cur = str(self.stage(now, user_msgs).get("key"))
        if cur not in keys or keys.index(cur) + 1 >= len(rows):
            return None
        return rows[keys.index(cur) + 1]

    def can_advance(self, now: float | None = None, user_msgs: int = 0) -> tuple[bool, str]:
        """她现在**能不能**往前挪一档：(行不行, 说明)。不行时说明要能看懂。"""
        if str(self.data.get("decide") or "she") == "auto":
            return False, "decide=auto：阶段按阈值自动走，不用她决定"
        nxt = self.next_stage(now, user_msgs)
        if nxt is None:
            return False, "已经在最后一档了（没有更亲的叫法）"
        # ⚠ 这里**必须比大小、不能比相等**（`#148` 真机探针抓到的 bug）：
        # 她够得到的最高档（eligible）可以**高于**下一档——比如认识 11 天时她够到「亲近」，
        # 而下一档只是「熟络」。一档一档往前走本来就该允许这种情况；
        # 写成"下一档必须正好等于 eligible"，她就永远挪不动（探针里她就这么被误拦的）。
        keys = [str(s.get("key")) for s in self.stages()]
        if keys.index(str(nxt.get("key"))) > keys.index(str(self.eligible(now, user_msgs).get("key"))):
            return False, (f"还够不到「{nxt.get('key')}」：需要认识 {nxt.get('min_days')} 天 "
                           f"或你说过 {nxt.get('min_msgs')} 条话"
                           f"（现在第 {self.days(now)} 天／{int(user_msgs)} 条）")
        cooldown = int(self.data.get("min_days_per_step") or 0)
        last = float(self.data.get("changed_at") or 0)
        if not last and str(self.data.get("since") or ""):
            try:
                last = dt.datetime.strptime(str(self.data["since"]), "%Y-%m-%d").timestamp()
            except ValueError:
                last = 0
        if cooldown and last:
            waited = (float(now if now is not None else time.time()) - last) / 86400
            if waited < cooldown:
                return False, (f"刚改口不久（{waited:.1f} 天），至少隔 {cooldown} 天再挪一档"
                               f"——推进要慢，快了不像处出来的")
        return True, f"可以进「{nxt.get('key')}」（以后叫他「{nxt.get('call_him')}」）"

    @_locked
    def advance(self, to_key: str = "", why: str = "", source: str = "she",
                now: float | None = None, user_msgs: int = 0) -> tuple[bool, str]:
        """把关系往前挪一档。**决定权归她**（她想挪才挪）；护栏负责"落地要确定"（`D4`）。

        四条护栏：① 一次只挪**一档**（不许跳）；② 必须够资格（阈值＝资格线）；
        ③ 两次改口至少隔 `min_days_per_step` 天；④ **必须给理由**——没理由不落盘。
        每次改动都进 `history` 并埋点，所以"她什么时候改的口、为什么"查得到。
        """
        to_key = str(to_key or "").strip()
        if source == "she":
            ok, why_not = self.can_advance(now, user_msgs)
            if not ok:
                return False, why_not
        nxt = self.next_stage(now, user_msgs)
        if nxt is None:
            return False, "已经在最后一档了"
        if to_key and to_key != str(nxt.get("key")):
            return False, (f"只能一档一档往前：现在是「{self.stage(now, user_msgs).get('key')}」，"
                           f"下一档是「{nxt.get('key')}」，说的「{to_key}」跳档了")
        if not str(why or "").strip():
            return False, "没给理由——她为什么想改口，得说一句，不然不落盘"
        before = str(self.stage(now, user_msgs).get("key"))
        self.data["stage"] = str(nxt.get("key"))
        self.data["changed_at"] = float(now if now is not None else time.time())
        self.data.setdefault("history", []).append({
            "ts": self.data["changed_at"], "from": before, "to": str(nxt.get("key")),
            "why": str(why)[:120], "source": source})
        self.record_event("stage_changed", f"关系从「{before}」到「{nxt.get('key')}」",
                          source, now=self.data["changed_at"])
        self.save()
        return True, (f"关系从「{before}」挪到「{nxt.get('key')}」——"
                      f"她以后叫他「{nxt.get('call_him')}」（理由：{str(why)[:60]}）")

    @_locked
    def render_for_decision(self, now: float | None = None, user_msgs: int = 0) -> str:
        """给她自己看的一小段：现在到哪一档、下一档是什么、够不够格、多久没改口了。"""
        cur = self.stage(now, user_msgs)
        nxt = self.next_stage(now, user_msgs)
        lines = [f"你对他的称呼现在是「{cur.get('call_him')}」，关系是「{cur.get('key')}」"
                 f"（认识第 {self.days(now)} 天）。"]
        if nxt is None:
            lines.append("已经是最亲近的一档了。")
            return "\n".join(lines)
        ok, why = self.can_advance(now, user_msgs)
        lines.append(f"再往前一档是「{nxt.get('key')}」（那时你叫他「{nxt.get('call_him')}」）：{why}。")
        last = float(self.data.get("changed_at") or 0)
        if last:
            days_since = (float(now if now is not None else time.time()) - last) / 86400
            lines.append(f"上次改口是 {days_since:.1f} 天前。")
        return "\n".join(lines)

    def call_him(self, now: float | None = None, user_msgs: int = 0) -> str:
        return str(self.stage(now, user_msgs).get("call_him") or "你")

    def her_own(self) -> list[str]:
        return [str(x) for x in (self.data.get("her_own") or []) if str(x).strip()]

    # ---------- 给提示词的那一段（保持短：注入块越多越容易互相打架，`#131` 的教训） ----------
    @_locked
    def render(self, now: float | None = None, user_msgs: int = 0,
               scene_text: str = "") -> str:
        st = self.stage(now, user_msgs)
        days = self.days(now)
        lines = [f"【你和他的关系】认识第 {days} 天，现在算是「{st.get('key')}」。"
                 f"你平常叫他「{self.call_him(now, user_msgs)}」。"]
        note = str(self.data.get("call_note") or "").strip()
        if note:
            lines.append(f"称呼上的讲究：{note}")
        promises = self.promise_items()
        lines.append("你答应过他的事（待履约承诺）：" +
                     ("；".join(p["text"] for p in promises)
                      if promises else "（暂时没有）"))
        recent = self.events(3)
        if recent:
            labels = {"promise_created": "记下了承诺", "promise_completed": "承诺已完成",
                      "promise_missed": "承诺这次没做到", "stage_changed": "关系阶段变了"}
            lines.append("最近关系记录：" + "；".join(labels.get(e.get("kind"), "关系有记录")
                                              for e in recent))
        rituals = [str(x) for x in (self.data.get("rituals") or []) if str(x).strip()]
        lines.append("你们的固定节目：" + ("；".join(rituals) if rituals else "（还没形成）"))
        style = str(self.data.get("care_style") or "").strip()
        if style:
            lines.append(f"你怎么关心他：{style}")
        familiar = st.get("key") in ("熟络", "亲近", "亲密")
        playful = familiar and "偶尔撒娇" in style
        if playful:
            lines.append("【你愿意怎么靠近他】熟悉以后，可以直接表达惦记、想念和想跟他待一会儿，"
                         "也可以顺着气氛逗他一句、小撒娇；用自己的话，按当前称呼，不必每次都做。"
                         "他忙时给他空间，不查岗、不拿冷淡惩罚他，也不编共同过去。")
        return "\n".join(lines)

    # ---------- 给人用的操作 ----------
    @_locked
    def set_stage(self, key: str) -> str:
        """人手动操作：切"谁来决定"，或直接把阶段钉到某一档。"""
        key = str(key or "").strip()
        known = [str(s.get("key")) for s in self.stages()]
        if key in ("she", "她自己", "她自己决定"):
            self.data["decide"] = "she"
            self.save()
            return ("已改回**她自己决定**：要不要往前挪一档由她在复盘时提，"
                    "系统只做护栏（一档一档、够资格、有理由才落盘）。")
        if key in ("auto", "自动", "按阈值"):
            self.data["decide"] = "auto"
            self.save()
            return f"已改成按阈值自动推进。现在是「{self.stage().get('key')}」。"
        if key not in known:
            return (f"没有这个阶段：{key}。可选：{'、'.join(known)}"
                    f"（或 she = 她自己决定 / auto = 按阈值自动）")
        before = str(self.stage().get("key"))
        self.data["stage"] = key
        self.data["changed_at"] = time.time()
        self.data.setdefault("history", []).append(
            {"ts": time.time(), "from": before, "to": key, "why": "人手动改的", "source": "human"})
        self.record_event("stage_changed", f"关系从「{before}」到「{key}」", "human")
        self.save()
        return f"已把阶段定为「{key}」，她以后叫你「{self.stage().get('call_him')}」。"

    @_locked
    def add_promise(self, text: str, due_at: float | None = None) -> str:
        text = str(text or "").strip()
        if not text:
            return "要写什么？用法：/relationship promise 下次给你带后街的烧烤"
        row = self.create_promise(text[:60], due_at)
        opened = len(self.promise_items())
        return f"记下了，她答应过：{row['text']}（{row['id']}；第 {opened} 条待履约）"

    def drop_promise(self, handle: str) -> str:
        return self.complete_promise(handle)

    @_locked
    def report(self, now: float | None = None, user_msgs: int = 0) -> str:
        st = self.stage(now, user_msgs)
        she = str(self.data.get("decide") or "she") == "she"
        rows = [f"认识第 {self.days(now)} 天（{self.data.get('since')} 起）",
                f"当前阶段：{st.get('key')}（"
                + ("**她自己决定**——她在复盘时觉得是时候了才挪" if she
                   else "按阈值自动推进（decide=auto）") + "）",
                f"她叫你：{self.call_him(now, user_msgs)}",
                f"你说过的话：{int(user_msgs)} 条｜按阈值她最多够得到：「{self.eligible(now, user_msgs).get('key')}」",
                "阶段表（改 relationship.json 就能调）："]
        for s in self.stages():
            rows.append(f"  · {s.get('key')}：叫「{s.get('call_him')}」"
                        f"（≥{s.get('min_days')} 天 或 ≥{s.get('min_msgs')} 条）")
        promises = self.promise_items(include_closed=True)
        status_label = {"open": "待履约", "done": "已完成", "missed": "失约"}
        rows.append("她答应过的：" + ("；".join(
            f"{p['id']}.{status_label.get(p.get('status'), '未知')}：{p.get('text')}"
            for i, p in enumerate(promises, 1)) if promises else "（无）"))
        recent = self.events(5)
        if recent:
            rows.append("最近关系事件：" + "；".join(
                f"{e.get('kind')}（{e.get('text')}）" for e in recent))
        rituals = [str(x) for x in (self.data.get("rituals") or []) if str(x).strip()]
        rows.append("固定节目：" + ("；".join(rituals) if rituals else "（无）"))
        rows.append(f"她自己的东西（不许拿这些反问他）：{'、'.join(self.her_own()) or '（无）'}")
        rows.append("用法：/relationship stage 熟络｜/relationship promise <一句话>｜"
                    "/relationship done <ID>｜/relationship miss <ID> <原因>（旧待履约序号仍兼容）")
        return "\n".join(rows)
