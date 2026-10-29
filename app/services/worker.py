# -*- coding: utf-8 -*-
"""后台任务队列（`T61`）：把写记忆挪出请求路径。

**为什么需要**：`#067` 首次量到一轮对话的耗时分解 —— `prepare 1ms ／ 首字 3026ms ／
正文 5712ms ／ **写记忆 4519ms**`。也就是说用户看完回答后，HTTP 请求还要挂 4.5 秒。
单用户无感（正文早已上屏），但并发时每个请求都白占 4.5s 的 worker。
（顺带纠正一个旧判据：`T3 第 4 步`当初按"单轮 LLM 调用 ≥4 次的占比 = 0%"关掉了作业队列，
那个判据**只看次数、没看耗时**，所以低估了这件事。）

**两个硬约束，都是这个项目自己的规矩**：

1. **同一用户必须串行** —— 记忆写入、摘要游标、演化游标都假设"上一轮先落库"；
   乱序会让记忆里出现时间倒置的内容。做法：**按 uid 分片**固定的一批 worker，
   同一个人永远落在同一个队列里，天然串行；不同用户互不阻塞。
2. **退出必须 flush** —— 入队之后就多了一个"丢失窗口"。不清空队列就退出，最后几轮
   她说的话不会被记住。`flush()` 就是给退出钩子用的，`main.py` 里已经接上。

**不引入 Redis/Kafka**：单机不需要外部队列，而且这个项目刻意不加依赖。
参数 `WB_WORKERS`（默认 2）；设 0 则退化成"提交即同步执行"（排障与测试用）。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import zlib

from app.log import get

log = get("worker")

WORKERS = int(os.environ.get("WB_WORKERS") or 2)

_lock = threading.Lock()
_queues: list[queue.Queue] = []
_threads: list[threading.Thread] = []
_started = False


def _shard(user_id: str) -> int:
    """按 uid 选队列。用 crc32 而不是内置 hash：后者每进程加盐，跨进程不一致。"""
    return zlib.crc32((user_id or "").encode("utf-8")) % max(1, WORKERS)


def _ensure_started() -> None:
    global _started
    if _started or WORKERS <= 0:
        return
    with _lock:
        if _started:
            return
        for i in range(WORKERS):
            _queues.append(queue.Queue())
            # daemon：解释器退出时不会因为我们挂住；但**别指望它替你收尾**——
            # 收尾靠 main.py 的 lifespan 调 flush()，那才是数据不丢的保证。
            t = threading.Thread(target=_run, args=(i,), daemon=True,
                                 name=f"mem-worker-{i}")
            t.start()
            _threads.append(t)
        _started = True
        log.info("后台队列已启动：%d 个 worker（同用户串行，按 uid 分片）", WORKERS)


def _run(shard: int) -> None:
    q = _queues[shard]
    while True:
        fn, args, kwargs, held_uid = q.get()
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            # 失败要可见：一轮写记忆失败不该让 worker 停摆，但绝不能悄悄没了。
            log.error("后台任务失败（worker %d）：%s: %s", shard, type(exc).__name__, exc)
        finally:
            if held_uid is not None:
                from app.services import registry
                registry.release(held_uid)
            q.task_done()


def submit(user_id: str, fn, *args, **kwargs) -> None:
    """把一件后台活排进该用户的队列（同用户按提交顺序执行）。"""
    from app.services import auth, registry
    shard_uid = auth.storage_uid((user_id or "").strip())
    held_uid = registry.retain(getattr(fn, "__self__", None))
    try:
        if WORKERS <= 0:
            try:
                fn(*args, **kwargs)
            finally:
                if held_uid is not None:
                    registry.release(held_uid)
            return
        _ensure_started()
        _queues[_shard(shard_uid)].put((fn, args, kwargs, held_uid))
    except Exception:
        if WORKERS > 0 and held_uid is not None:
            registry.release(held_uid)
        raise


def flush(timeout: float = 30.0) -> bool:
    """等所有队列清空。返回是否在超时内完成（退出钩子必须查这个返回值）。"""
    if WORKERS <= 0 or not _started:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(q.unfinished_tasks == 0 for q in _queues):
            return True
        time.sleep(0.01)
    return False


def pending(user_id: str = "") -> int:
    """积压量。带 uid 看某一个队列，不带则总计（观测用）。"""
    if WORKERS <= 0 or not _started:
        return 0
    if user_id:
        return _queues[_shard(user_id)].unfinished_tasks
    return sum(q.unfinished_tasks for q in _queues)


def queued(user_id: str) -> bool:
    return pending(user_id) > 0
