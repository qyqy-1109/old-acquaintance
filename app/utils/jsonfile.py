# -*- coding: utf-8 -*-
"""JSON 文件的原子读写（面试硬伤 1：崩溃安全）。

**为什么必须原子写**：全量重写的写法（`open(path, "w")` 之后 `json.dump`）一旦中途
失败——进程被杀、断电、磁盘满——文件会被**截断成空**或半截 JSON。而
`personality.json`（手写的人设）和 `user_profile.json`（长期积累的画像）恰恰是全系统
**最难重建**的两份数据。

对比一下就更清楚：`memory.db` 早就靠 SQLite 的 WAL + 事务拿到了原子性；`world.py` 也
早就是「tmp + fsync + `os.replace`」的写法。**只有这两份漏了**——所以这里把它抽成一个
工具，让所有 JSON 走同一条路，而不是每个类各写一遍（也避免再漏）。

**做法**：先写同目录的临时文件 → `flush` + `fsync` 落盘 → `os.replace` 原子替换。
`os.replace` 在同一文件系统内是原子的：别人要么看到旧文件、要么看到新文件，**不会看到
半截**。临时文件必须与目标**同目录**，跨文件系统的 replace 不是原子的。
"""

from __future__ import annotations

import json
import os

from app.log import get

log = get("jsonfile")


def write_json_atomic(path: str, data, *, indent: int = 2) -> None:
    """把 data 原子地写成 JSON。

    失败会**抛出**（调用方自己决定记日志还是向上报）——不静默吞掉：
    目标文件此刻仍是完好的旧内容，所以这一次写入丢了，但**数据没丢**。
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            # 先真正落到盘上再替换；少了 fsync，断电时可能换来一个"空的新文件"。
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # 清掉半截临时文件，然后**如实抛出**。目标文件全程没被碰过。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default=None):
    """读 JSON；读不到就返回 default，坏了则记 ERROR 后返回 default（失败要可见）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.error("读取失败（用默认值）：%s: %s", type(exc).__name__, exc)
        return default
