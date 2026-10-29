"""对话记忆：事实提炼 + 滑动窗口 + 滚动摘要 + 向量长期记忆 + 遗忘。

整个模块最核心的一个选择：长期记忆里存的不是对话原文，而是从原文里
提炼出来的「事实短句」。

    存原文 →  召回回来一大坨，token 贵；有用信息被寒暄稀释；
              "我叫小林"这种事实必须跟当时那整段对话绑在一起才找得到。
    存短句 →  一条记忆一句话，召回准、注入便宜、换任何问法都能命中。

代价是每轮多一次提炼调用，用 cfg["extract_every"] 节流，
或者 auto_extract=False 直接退回存原文的旧行为。

四层分工（**都落在 SQLite `memory.db` 里**，不再是几百 MB 的 JSON）：
    1. 会话原文 —— `messages` 表全量存，一条不丢；只在滑动窗口内进模型
    2. 滚动摘要 —— 滑出窗口的部分压成一小段（`summaries` + 游标），长会话不断片
    3. 长期记忆 —— 提炼出的短句做 embedding（`memories.vec`），按语义召回
    4. 遗忘机制 —— 去重、按半衰期衰减、命中加权、超容量淘汰最冷门的

另外三张非叙事表：`slots`（覆盖式状态）、`events`（机制埋点）、`opinions`（反馈学来的说话倾向）。

依赖：numpy（向量计算，向量以 float32 BLOB 存在 `memories.vec`）；
embedding 走千问兼容端点，提炼/摘要/画像走小米 mimo。

（这段说明以前写着"会话原文全量存 JSON""旧格式迁移"等已不成立的形态，`#94` 的 T69-② 修正。）
"""

from __future__ import annotations

import datetime as dt
import json
import re
import os
import threading
import time
from typing import Any, Callable

import numpy as np

from app.models import db as memdb
from app.utils import clock
from app.utils.llmjson import chat_json, strings

from app.log import get

log = get("memory")

# （曾有一个 SCHEMA_VERSION = 2，用来标记「存整轮原文 v1 → 存提炼短句 v2」。**已删除**：
#   全仓没有任何读取点，是纯标记；v1 数据的处理实际在 db.migrate_from_json 与 KIND_TURN。）

# 常量、提示词与默认配置搬到了 memory_defs（#072）——它们纯文本、零依赖、零行为，
# 却占了原文件开头约 300 行。这里**重新导出**，所以
# `from app.models.memory import KIND_WORLD` 这类既有写法完全不受影响。
from app.models.memory_defs import (          # noqa: F401
    EMBED_MAX_CHARS, KIND_FACT, KIND_TURN, KIND_WORLD,
    KIND_CONSTRAINT, PROTECTED_KINDS, EXPLICIT_MARKERS, EXPLICIT_EMPTY,
    RECONCILE_PROMPT, EXTRACT_PROMPT, WORLD_FULL_REVIEW, WORLD_CHECK_PROMPT,
    WORLD_CHECK_BATCH_PROMPT, STATE_CHANGE, GONE_WORDS, SELF_WORDS,
    CONSTRAINT_UPDATE_HINT,
    SUPERSEDE_PROMPT, COMPENSATE_PROMPT, SUMMARIZE_PROMPT, DEFAULT_CFG,
)

# 方法按职责拆在同级几个文件里（#073 拆的四个 + #89 加的 opinions）：本文件只留
# `__init__`（共享状态的契约）和类声明。于是「改召回」读 memory_read.py、
# 「改提示词」读 memory_defs.py，不用再在两千行里翻。各文件里的 Mixin 都吃同一个
# self，契约见下面的类文档。
from app.models.memory_storage import StorageMixin
from app.models.memory_write import WriteMixin, WorldMixin
from app.models.memory_read import StateMixin, RecallMixin
from app.models.memory_lifecycle import ForgetMixin, EventsMixin
from app.models.memory_opinions import OpinionMixin


class MemoryStore(
    StorageMixin,
    WriteMixin,
    WorldMixin,
    StateMixin,
    RecallMixin,
    ForgetMixin,
    EventsMixin,
    OpinionMixin,
):
    """四层记忆的中枢：事实短句 + 滚动摘要 + 向量召回 + 遗忘。

    **本类的方法按职责拆在同级的五个文件里**（`#073` 拆的四个 + `#89`）：
      - `memory_storage.py`  ：StorageMixin（落库/加载/向量基建）
      - `memory_write.py`    ：WriteMixin（提炼入库）、WorldMixin（世界书）
      - `memory_read.py`     ：StateMixin（槽位/分寸/时间轴）、RecallMixin（召回/组装/对账）
      - `memory_lifecycle.py`：ForgetMixin（遗忘/淘汰/作废/摘要）、EventsMixin（埋点/统计/清理）
      - `memory_opinions.py` ：OpinionMixin（用户点「她说错了」学来的说话倾向，`D31`）

    **它们共享同一个 `self`，契约全在 `__init__` 里**——这是拆分后唯一要记的事：
      - `cfg`（配置，24 处读）、`memories`/`vectors`（平行结构，27/9 处读）、`_lock`（18 处读）
      - `db`（SQLite 连接）、`session`/`sessions`/`summaries`/`summary_cursor`/`archived`
      - 回调用 `on_drop`/`on_world`；成本计数 `_llm_calls`/`_embed_calls`
    改这些属性等于改全类的隐式契约：**先搜一遍谁在读**，再动。
    """

    def __init__(
        self,
        client,
        path: str = "memory.db",
        session: str = "default",
        embed_model: str = "",
        extract_model: str = "glm-5.3-flash",
        cfg: dict | None = None,
        legacy_json: str = "",
        legacy_vec: str = "",
        embed_client=None,
        embed_dimensions: int = 0,
    ):
        # 文字类的活（提炼/对账/补偿/摘要）用 client；embedding 用 embed_client。
        # 分开是因为 embedding 常常得留在另一个供应商——比如小米那个端点根本没有
        # embedding 接口，换过去召回直接瘫痪。
        self.client = client
        self.embed_client = embed_client or client
        self.path = path
        self.session = session
        self.embed_model = embed_model
        if not self.embed_model:
            # 故意不给默认值。以前这里默认 "embedding-3"（智谱时代的遗迹），
            # 谁忘了传就会静默用错模型——而智谱那个账户已经没余额了，
            # 表现是「召回永远为空」，极难查。宁可启动就炸。
            raise ValueError("MemoryStore 必须显式传 embed_model（从 config.EMBED_MODEL 取）")
        self.embed_dimensions = int(embed_dimensions or 0)
        self.extract_model = extract_model
        self.cfg: dict[str, Any] = dict(DEFAULT_CFG)
        if cfg:
            self.cfg.update(cfg)

        self.sessions: dict[str, list[dict]] = {}
        self.memories: list[dict] = []      # 活跃记忆，每条的 "id" 就是库里的主键
        self.archived: list[dict] = []      # 已作废的条目，只有文本，没有向量
        self.summaries: dict[str, str] = {}     # session -> 窗口之前的滚动摘要
        self.summary_cursor: dict[str, int] = {}  # session -> 已摘要到第几条消息
        self.vectors: np.ndarray | None = None  # (N, dim) float32，与 memories 一一对应
        self.pending: list[dict] = []           # 还没算出向量的记忆，下次启动接着补
        self.on_world: Callable[[list[str]], None] | None = None  # 由 WorldBook 注册
        self.on_drop: Callable[[list[str]], None] | None = None   # 作废条目时回调

        # 网页版接口跑在线程池里，可能有两个请求同时碰库（比如前端同时拉
        # /api/state 和 /api/chat）。单条语句 SQLite 自己能保证原子，
        # 但多语句事务（归档=改标记+清归档、save=批量写）需要互斥，
        # 否则可能交叉执行出半截状态。
        self._lock = threading.RLock()
        self.db = memdb.connect(path)
        note = memdb.migrate_from_json(self.db, legacy_json, legacy_vec)
        if note:
            log.info("%s", note)
        self._synced: dict[str, int] = {}    # session -> 已落盘到第几条消息
        self._dirty_hits: set[int] = set()   # 命中计数变过的记忆 id，save() 时统一落盘
        # `clear_session()`（/clear）清掉的会话：摘要只在内存里 pop 是不够的——
        # `save()` 对 summaries 只有 INSERT OR REPLACE，库里那行永远不删，重启就复活，
        # 而且游标停在旧位置（比历史还长）会让这个会话**此后再也不做摘要**。
        # 所以这里记一笔，save() 时把对应的库行删掉。
        self._summaries_dropped: set[str] = set()
        # 摘要失败的退避截止时刻。失败**不推进游标**（否则永久丢摘要），
        # 但也不能每轮都重试——模型长期不可用时会变成每轮白烧一次调用。
        self._summarize_retry_at: float = 0.0
        self._events_since_trim: int = 0     # 距上次修剪事件表写了几条
        # 本轮的成本计数（add_turn 开头清零、turn 事件带出去）：回答「一轮到底
        # 几次模型调用、几次 embedding」。它也是 T3 第 4 步（要不要上作业队列）的
        # 判据来源——用数据决定，不靠感觉。
        # 注意：llm 按**步骤**计，不含 llmjson 内部 retries 的重试。
        self._llm_calls: int = 0
        self._embed_calls: int = 0
        # 本轮召回挡掉的过期待办（进 turn 事件）。**是集合不是计数**：一轮里
        # 世界书召回和事实召回是两次独立 `recall`，同一条过期待办会被跳过两次——
        # 计数口径下 `stale` 会翻倍。按条目 id 去重才是「本轮挡掉了几条」。
        self._stale_skipped: set = set()
        # 场景 → 事件组的映射（`T85` 第 2 步）：提炼器给每条 fact 一个 **scene 标签**，
        # 同一个场景的条目（跨轮复述也算）归到同一个 `event_group`，时间轴/统计就能折叠。
        # **只放内存**：它是"这一场戏"的运行时归属，不是记忆内容本身；落库要么加列做迁移，
        # 要么等下次启动由 event_group 自己重新长出来——现在选后者（零迁移、零风险）。
        self._scene_groups: dict = {}
        self._last_group_ts: tuple | None = None   # 事件组回填时的"上一条"(ts, 组号)

        self._load()
