# -*- coding: utf-8 -*-
"""按用户取 `ChatService`（产品形态：**每人一个她**）。

为什么需要这一层：每个用户是一整套数据（人设、画像、世界书、记忆库、情绪），
所以每个用户一个 `ChatService` 实例。实例**按需创建 + 缓存**，而且**有上限**——
否则用户一多，内存会被几十上百个实例拖垮（每个实例都持有自己的向量矩阵，
这正是规模化时第一个撞到的墙）。

超上限时回收**最久没用过**的那个（`close()` 会先落盘，数据不丢），下次用到再按需重建
—— 用「冷用户按需加载」换内存。

**但正在被使用的实例不能回收**（`#066`）：请求跑到一半实例被收走，它下一个请求会
新建实例，`_evolved_pos` / `_turns_since_profile` 这些状态归零（重复演化、重复画像），
还得重新读文件、重算向量。所以这里记引用计数，回收时跳过借出中的。
**请求路径必须走 `borrow()` / `acquire()`**，否则计数不起作用。

`user_id` 为空 = 默认用户，沿用根数据目录：CLI、benchmark、单用户部署的行为一概不变。
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict

from app.log import get

log = get("registry")

MAX_SERVICES = int(os.environ.get("WB_MAX_SERVICES") or 50)

_lock = threading.RLock()
_services: "OrderedDict[str, object]" = OrderedDict()
# 每个 uid 正在被借出的次数。>0 的实例**不参与回收**。
_refs: dict[str, int] = {}


class ServiceBusy(Exception):
    """The cached data set still has active request or background owners."""


def get(user_id: str = ""):
    """取（必要时新建）某个用户的 ChatService。

    注意：**它不计引用**。请求路径请用 `borrow()`，否则实例可能在使用中被回收。
    """
    from app.services.chat_service import ChatService   # 延迟导入，避免循环依赖

    from app.services import auth
    with _lock:
        uid = auth.storage_uid((user_id or "").strip())
        svc = _services.get(uid)
        if svc is not None:
            _services.move_to_end(uid)          # LRU：标成最近使用
            return svc
        svc = ChatService(user_id=uid)
        _services[uid] = svc
        _recycle(protect=uid)       # 别把刚建好的这个收走
        return svc


def _recycle(protect: str = "") -> None:
    """把实例数压回上限内，**跳过借出中的**。调用方须持有 `_lock`。

    `protect` = "这次调用要返回的那个用户"：刚建好的实例必须被保护住，否则会出现
    "刚建出来立刻被回收、然后返回一个已经 close 过的实例"——这是测试抓出来的。
    """
    failed: set[str] = set()
    while len(_services) > MAX_SERVICES:
        victim = next((u for u in list(_services)
                       if u not in (protect, "") and u not in failed
                       and _refs.get(u, 0) <= 0), None)
        if victim is None:
            # 全都在用：宁可不回收，也不能把别人正在用的实例收走。上限是软约束，
            # 但"使用中被回收"会静默地丢状态，那是硬伤。
            log.warning("实例数已达上限（%d）但全都在使用中，本次不回收；"
                        "需要更多常驻实例请调大 WB_MAX_SERVICES", MAX_SERVICES)
            return
        old = _services.pop(victim)
        try:
            old.close()                         # close() 里会 save()，数据不丢
            log.info("实例数超上限（%d），已回收最久未用的用户：%s",
                     MAX_SERVICES, victim or "（默认）")
        except Exception as exc:
            _services[victim] = old
            _services.move_to_end(victim, last=False)
            failed.add(victim)
            log.error("回收用户 %s 的实例失败：%s", victim, exc)


def acquire(user_id: str = "") -> str:
    """标记"这个用户正在被使用"，返回 uid（交给 `release` 用）。"""
    from app.services import auth
    with _lock:
        uid = auth.storage_uid((user_id or "").strip())
        get(user_id)
        _refs[uid] = _refs.get(uid, 0) + 1
    return uid


def release(uid: str) -> None:
    """与 `acquire` 配对；归零后其实例才重新参与回收。"""
    with _lock:
        left = _refs.get((uid or "").strip(), 1) - 1
        if left <= 0:
            _refs.pop((uid or "").strip(), None)
        else:
            _refs[(uid or "").strip()] = left


@contextlib.contextmanager
def borrow(user_id: str = ""):
    """借出一个实例：进入时计数 +1、退出 -1。请求路径推荐用这个。"""
    uid = acquire(user_id)
    try:
        yield _services.get(uid)
    finally:
        release(uid)


def in_use(user_id: str = "") -> bool:
    from app.services import auth
    with _lock:
        uid = auth.storage_uid((user_id or "").strip())
        return _refs.get(uid, 0) > 0


@contextlib.contextmanager
def exclusive(user_id: str = ""):
    """Hold the registry while validating and rebinding an idle account."""
    from app.services import auth
    with _lock:
        uid = auth.storage_uid((user_id or "").strip())
        if _refs.get(uid, 0) > 0:
            raise ServiceBusy("账号正在使用，请稍后重试。")
        yield uid


def retain(service) -> str | None:
    """Retain an existing cached instance without creating or remapping it."""
    if service is None:
        return None
    uid = getattr(service, "user_id", None)
    with _lock:
        if _services.get(uid) is not service:
            return None
        _refs[uid] = _refs.get(uid, 0) + 1
        return uid


@contextlib.contextmanager
def borrow_cached(service):
    """Borrow a cached snapshot, skipping instances removed since the snapshot."""
    uid = retain(service)
    try:
        yield service if uid is not None else None
    finally:
        if uid is not None:
            release(uid)


def drop(user_id: str = "") -> bool:
    """把某个用户的实例移出缓存并关闭（删号时用）。

    **只负责内存与关闭**：数据文件（用户目录）由调用方删除 —— 数据归属是 service 层
    的语义，registry 不该越界去动文件。
    """
    from app.services import auth
    with _lock:
        uid = auth.storage_uid((user_id or "").strip())
        if _refs.get(uid, 0) > 0:
            log.warning("拒绝移除使用中的实例：%s", uid or "（默认）")
            return False
        svc = _services.get(uid)
        if svc is not None:
            try:
                svc.close()
            except Exception as exc:
                log.error("关闭 %s 的实例失败：%s", uid, exc)
                return False
        _services.pop(uid, None)
        _refs.pop(uid, None)
    return True


def all_services() -> list:
    with _lock:
        return list(_services.values())


def close_all() -> None:
    with _lock:
        for svc in list(_services.values()):
            try:
                svc.close()
            except Exception as exc:
                log.error("关闭实例失败：%s", exc)
        _services.clear()
        _refs.clear()


def reload_all() -> bool:
    """Close every cached instance so the next request sees new settings.

    Returns False when a request or background owner still holds an instance; the
    caller should then leave the old services alone and tell the user to restart.
    """
    with _lock:
        if any(count > 0 for count in _refs.values()):
            return False
        for svc in list(_services.values()):
            try:
                svc.close()
            except Exception as exc:
                log.error("重载时关闭实例失败：%s", exc)
                return False
        _services.clear()
        _refs.clear()
    return True


def active_refs() -> int:
    with _lock:
        return sum(_refs.values())


def count() -> int:
    with _lock:
        return len(_services)
