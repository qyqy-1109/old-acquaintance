# -*- coding: utf-8 -*-
"""极简 `.env` 加载（**不引入 python-dotenv**：这个项目刻意不加依赖）。

**一条铁律：已存在的环境变量优先。** 反过来（`.env` 覆盖真实环境变量）很危险——
生产机上一个过期的 `.env` 会悄悄把密钥换回旧的，排障时根本想不到；而 env 优先最坏
也只是"文件没生效"，一眼能看出来。

只支持人能写出来的那几种形式：注释、空行、`KEY=VALUE`、成对引号、`export ` 前缀。
不支持多行值、变量插值这类花哨东西——用不上，而且写错了不如不写。

为什么要有它：密钥原来**硬编码在 `config.py` 里**（见 `#064`），那既进版本库、也没法
按环境换。现在值放 `.env`（不进库），代码里不再有任何明文。
"""

from __future__ import annotations

import os

# ⚠ 这里**不能** import app.log：app/log.py 里有 `from app import config`，而本模块是在
# `config` 初始化**期间**被调用的 —— 会形成循环导入。踩过：异常被 config 里的 try 吞掉，
# 表现成"进程正常起来、密钥却是空的"（静默失灵）。所以加载期的输出一律走 print，
# 那是启动早期最可靠的一条路。
_PREFIX = "[envfile]"


def load(path: str) -> int:
    """把 `.env` 读进 `os.environ`（**不覆盖已存在的**）。返回设了几个变量。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0                     # 没配 .env 是正常情况，算不上错误
    except Exception as exc:
        print(f"{_PREFIX} 读取 {path} 失败（忽略，继续用环境变量）：{exc}")
        return 0

    n = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            print(f"{_PREFIX} {path} 里有不认识的写法，已跳过：{raw.strip()[:60]}")
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ:
            continue                 # 铁律：已存在的环境变量优先
        os.environ[key] = value
        n += 1
    return n
