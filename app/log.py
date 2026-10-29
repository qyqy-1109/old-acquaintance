# -*- coding: utf-8 -*-
"""全局日志：统一格式、同时进控制台和日志文件，替代散落的 print。

用法（各模块固定两行）：
    from app.log import get
    log = get("memory")            # 模块名取短的：memory / chat / world / weather …
    log.info("记住 %s 条", n)      # 级别：debug < info < warning < error

格式：2026-09-23 21:04:47 WARNING [memory] 保存失败：…
文件：DATA_DIR/logs/app.log，每天零点滚动一份（app.log.2026-09-23），默认留 14 天
——按时间段筛选就是按日期文件翻；按级别/来源筛选直接 grep。

只接管 app.* 命名空间，不碰 uvicorn 自己的日志，两边不会重复。
级别用环境变量 WB_LOG_LEVEL 调（默认 INFO，排查问题时设 DEBUG，不用改代码）。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from app import config

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def force_utf8(stream) -> None:
    """把标准流**原地**切成 UTF-8（失败静默）。

    Windows 控制台默认 GBK：中文标点（…）和 ✓ 这类字符会直接抛 UnicodeEncodeError，
    表现是日志乱码、测试"假失败"。**原地**（reconfigure 而不是换对象）是关键——
    logging 的 StreamHandler 装的就是这个流对象，原地改它，已经建好的 handler 也跟着变。
    换环境（如 pytest 的 capture 对象）上可能没有 reconfigure，跳过即可。
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def force_utf8_streams(console=None) -> None:
    """把控制台相关的流**都**切到 UTF-8：日志走 stderr，print 走 stdout，一个都不能漏。

    踩过：只切了日志那条流（console.stream），于是启动横幅、`/mem` 的命令回显
    这类 print 出来的中文照样是乱码——同一个根因的另一半。
    """
    force_utf8(sys.stdout)
    force_utf8(console.stream if console is not None else sys.stderr)


def setup() -> None:
    """配置 app 命名空间的日志。幂等：反复 import / 多次建服务只配一次。"""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("app")
    root.setLevel(os.environ.get("WB_LOG_LEVEL", "INFO").upper())
    root.propagate = False                      # 不再往 root 冒泡，避免重复输出
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()           # 控制台（stderr，与 uvicorn 同一路）
    force_utf8_streams(console)                 # GBK 控制台：日志与 print 中文都会乱码
    console.setFormatter(fmt)
    root.addHandler(console)

    logdir = os.path.join(config.DATA_DIR, "logs")
    os.makedirs(logdir, exist_ok=True)
    keep = int(os.environ.get("WB_LOG_KEEP") or 14)
    filelog = TimedRotatingFileHandler(
        os.path.join(logdir, "app.log"),
        when="midnight", backupCount=keep, encoding="utf-8")
    filelog.setFormatter(fmt)
    root.addHandler(filelog)

    # uvicorn 自己的日志（访问记录、ASGI 异常栈）也接进文件——
    # 500 的 traceback 走的是 uvicorn.error，不接的话报障最需要的栈信息反而不在日志里。
    # 注意顺序：uvicorn 的 dictConfig 在加载 app 之前执行，这里加的 handler 不会被覆盖。
    for name in ("uvicorn.error", "uvicorn.access"):
        u = logging.getLogger(name)
        if filelog not in u.handlers:
            u.addHandler(filelog)


def get(name: str) -> logging.Logger:
    """取某个模块的 logger。name 用短名（"memory"），实际记录为 app.memory。"""
    setup()
    return logging.getLogger(f"app.{name}")
