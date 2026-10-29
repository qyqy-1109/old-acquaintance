# -*- coding: utf-8 -*-
"""每用户用量配额（成本护栏）。

**为什么需要**：LLM 调用是这个项目的主要成本，而每个用户每轮要跑 1~3 次后台调用。
没有配额时，一个脚本就能把额度刷光 —— 界面上"多试几句"和脚本"每秒一轮"看起来一样。

**为什么用内存计数**：配额是软护栏、不是计费依据。重启清零可以接受，而且不落盘就
不会引入"配额文件写坏 → 所有人被锁"这类新故障。真要精确计费时再换成表存储。

**为什么拒绝时必须说清恢复时间**：她"突然不说话了"和"今天说够了"是两种完全不同的
体验。只回一句"不行"，用户只会觉得她坏了 —— 与"失败要可见"是同一条道理。
"""

from __future__ import annotations

import datetime as dt
import os
import threading

from app.log import get

log = get("quota")

# 每用户每天允许的**轮数**（一轮 ≈ 1~3 次后台调用）。0 = 不限。
DAILY_TURNS = int(os.environ.get("WB_DAILY_TURNS") or 300)

_lock = threading.Lock()
_used: dict[str, tuple[str, int]] = {}          # uid -> (日期, 已用轮数)


def _today() -> str:
    return dt.date.today().isoformat()


def _key(uid: str) -> str:
    return (uid or "").strip() or "（默认）"


def check(uid: str = "") -> tuple[bool, str]:
    """能不能再来一轮。返回 (是否放行, 拒绝理由)。"""
    if DAILY_TURNS <= 0:
        return True, ""
    key = _key(uid)
    with _lock:
        day, n = _used.get(key, (_today(), 0))
        if day != _today():                     # 跨天自动重置
            n = 0
    if n >= DAILY_TURNS:
        return False, (f"今天的额度用完了（{n}/{DAILY_TURNS} 轮）。额度每天零点恢复——"
                       f"明天再来找我聊呀，我在的。")
    return True, ""


def spend(uid: str = "", n: int = 1) -> int:
    """记一笔用量，返回今日累计。"""
    key = _key(uid)
    with _lock:
        day, used = _used.get(key, (_today(), 0))
        if day != _today():
            used = 0
        used += n
        _used[key] = (_today(), used)
        return used


def used(uid: str = "") -> int:
    key = _key(uid)
    with _lock:
        day, n = _used.get(key, (_today(), 0))
        return n if day == _today() else 0


def reset(uid: str = "", limit: int | None = None) -> None:
    """清空某用户的用量（测试与排障用）；传 limit 会临时改总上限。"""
    global DAILY_TURNS
    with _lock:
        _used.pop(_key(uid), None)
        if limit is not None:
            DAILY_TURNS = int(limit)
