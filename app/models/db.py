"""记忆的存储层：SQLite。

**为什么要换掉 JSON + npy**

旧的落盘方式是「每次保存全量重写」：改一个字段，也要把整个 memories 列表序列化成 JSON、
把整个向量矩阵写一遍。而且有三个结构性问题：

1. 「作废」只能物理删除。一条记忆里捆着的其他信息会跟着一起没。
   实测 5 个场景里 2 次真的丢了数据——「用户住在杭州，养了只叫年糕的橘猫」
   在搬家时被整条删掉，猫也没了。
2. memories 和 vectors 是两个平行数组，靠下标对齐。改动时漏同步一个就静默错位，
   而且不会报错。这个坑已经踩过一次。
3. 归档要和活跃分离的话，得维护两套文件、两套数组同步移动。

**现在的形态**

- `memories` 表一条记忆一行，向量作为 BLOB 存在同一行里（不再有平行数组）；
- 归档 = `archived_at` 非空（软删），销掉的条目还在库里，只是不参与召回；
- 启动时把所有活跃行的 BLOB 按 id 顺序拼成一个连续 numpy 矩阵，
  之后运行时召回跟以前完全一样快（都是内存里的矩阵乘法）。

**为什么向量不另外存 npy 了**：那就又变回「两个东西要对齐」。
BLOB 全量读出来拼矩阵实测 20000 条 382ms（np.load 连续文件是 66ms），
差 315ms 而且只在启动时付一次；按实际规模（两千条以内）差距约 30ms。
换来的是「下标错位」这个风险类别彻底消失。

**写是增量的**：新增一条 INSERT 一行，作废一条 UPDATE 一行，
不再有「改一处、全量重写」。
"""

from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    ts              REAL NOT NULL,
    session         TEXT,
    hits            INTEGER NOT NULL DEFAULT 0,
    last_hit        REAL NOT NULL DEFAULT 0,
    explicit        INTEGER NOT NULL DEFAULT 0,
    event_date      TEXT,
    actor           TEXT,
    relaxed_at      REAL,          -- 约束的失效时刻：NULL = 仍然生效（见 relax_constraints）
    vec             BLOB,
    archived_at     REAL,
    archived_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_active ON memories(archived_at, id);
CREATE INDEX IF NOT EXISTS idx_kind   ON memories(kind, archived_at);

-- 状态槽位：「现在是什么样」的一等公民。覆盖式更新（主键 entity+attr），
-- 和叙事记忆（追加式、靠召回）分工：年糕在哪不该靠相似度碰运气。
-- 数量天然有限（实体数×属性数），注入时全量常驻。
CREATE TABLE IF NOT EXISTS slots (
    entity     TEXT NOT NULL,
    attr       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (entity, attr)
);

CREATE TABLE IF NOT EXISTS messages (
    session  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT,
    ts       REAL,
    greeting INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session, seq)
);

CREATE TABLE IF NOT EXISTS summaries (
    session TEXT PRIMARY KEY,
    text    TEXT,
    cursor  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- 机制埋点：append-only 的事件流水，回答「某个机制到底跑没跑、跑了几次、成功率多少」。
-- 只增不改、不参与召回也不参与注入；靠 events_keep 修剪最老的（留档必须有出口）。
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    session TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id);

-- 反馈学来的「说话方式倾向」（D31）：用户点「她说错了」之后提炼出的一句偏好。
-- 置信度**不存**——按 1-0.5^独立命中次数 现算；存了就会和 hits 打架（两条真相）。
-- sessions 存的是「**会话@日期**」标识（列名沿用，语义见 memory_opinions.learn_opinion：
-- 真机上会话永远是 "default"，只看会话 id 的话 hits 永远到不了 2、晋升门槛形成虚设）；
-- notes 数「其中几条带明确备注」；两者合起来就是晋升门槛（≥2 次独立命中 + 至少一条带备注）。
CREATE TABLE IF NOT EXISTS opinions (
    topic     TEXT PRIMARY KEY,
    text      TEXT NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 0,
    sessions  TEXT,
    notes     INTEGER NOT NULL DEFAULT 0,
    status    TEXT NOT NULL DEFAULT 'active',
    first_ts  REAL,
    last_ts   REAL,
    evidence  TEXT
);

-- ===== 她自己的生活（2026-09-29 新增）=====
-- 需求原话：「我想让她有自己的生活，但是她终归是一个虚拟人，她没有时间观念、没有想法，
-- 我只能让她尽可能的像人」。诊断：她有「现在几点」，但没有**属于她自己的现在进行时**——
-- 所以每次用户不在，她的世界就暂停；用户回来，她的"今天"从零重编。
--
-- `life_state`：**单行**（id=1）的"她此刻"。按自然时间段推进（早上/上午/中午/下午/傍晚/
-- 晚上/深夜），推进时让她自己从候选里挑一个活动——习惯由她自己的选择累积出来，不是脚本。
-- `life_events`：她的生活流水（只增）。**隔离**：它不进 `memories`、不参与召回与作废、
-- 也**绝不**作为画像分析的输入——否则她编的"自己的生活"会污染"用户是谁"（实测风险）。
CREATE TABLE IF NOT EXISTS life_state (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    slot      TEXT,                      -- 当前时间段（morning/noon/...）
    slot_date TEXT,                      -- 该时间段所属日期 YYYY-MM-DD（防跨天不推进）
    place     TEXT,                      -- 她在哪
    activity  TEXT,                      -- 她在做什么
    mood      TEXT,                      -- 心情（一句话）
    on_mind   TEXT,                      -- 牵挂什么
    updated_at REAL,
    advanced_at REAL,
    next_advance_at REAL,
    declined_at REAL
);
CREATE TABLE IF NOT EXISTS life_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    slot   TEXT,
    text   TEXT NOT NULL,                -- 流水一句话（"今天下午在公司赶个表"）
    source TEXT,                          -- model / fallback（兜底要能看出来）
    hidden_at REAL
);

-- 她的**待发消息**（`#108` 主动说话）：她说了一句，但用户可能不在页面上。
-- 前端打开页面时轮询 `/api/pending` 取走；`consumed_at` 标记"已投递"，
-- 保证同一条不会被投两次（取走即标记，原子）。
CREATE TABLE IF NOT EXISTS pending_say (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    text        TEXT NOT NULL,
    her_name    TEXT,
    consumed_at REAL
);

-- 「她说错了」里属于**事实类**的那一半（`#98` 的 T81）。
-- 为什么单独一张表：`opinions` 只收「说话方式」的偏好（D31），而用户点「她说错了」
-- 时绝大多数是"她记错了事实"——那条路原来只回一句话就结束了，**没有任何出口**
-- （用户 2026-09-28 21:32 的反馈正是这种：她编了一个从没出现过的碰头地点，
-- 系统回"这条属于「她记错了事实」，不进说话方式倾向"，然后就没了）。
-- 这里把事实类反馈**存下来并且只做一件事：提议作废那条被推翻的记忆**——
-- 采纳与否由人拍板（`/fix accept|drop`），因为改记忆是不可逆的（D12）。
CREATE TABLE IF NOT EXISTS feedback_corrections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    question  TEXT,
    answer    TEXT,
    topic     TEXT,
    note      TEXT,
    status    TEXT NOT NULL DEFAULT 'pending',
    resolved_at REAL,
    result    TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）记忆库。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread=False 是必须的：FastAPI 的同步接口跑在线程池里，
    # 而 ChatService 是在 lifespan（主线程）里建的，连接会跨线程用。
    # 跨线程本身由 SQLite 自己的锁保证安全，多语句事务的互斥靠 MemoryStore 里的
    # 一把 RLock 兜（见 _lock）。
    con = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    con.row_factory = sqlite3.Row
    # WAL 让读写不必互相等；这里只有单进程，但事务更稳、崩溃时不易损坏
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    # 旧库迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列。
    # event_date 是 2026-09-23 加的（事件时间轴：把日期从文本提成结构化字段），
    # 在此之前建的库需要手工补一列。
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
    if "event_date" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN event_date TEXT")
    if "actor" not in cols:
        # actor 标记这条记忆是谁的：user（默认）= 用户侧事实，assistant = 她自己的经历。
        # 她的经历入时间轴和召回池，是「她觉得自己是个有历史的人」的地基。
        con.execute("ALTER TABLE memories ADD COLUMN actor TEXT")
    if "relaxed_at" not in cols:
        # 约束的失效通道：不是「删掉」，而是「标成已解除」（NULL = 仍然生效）。
        # 约束是全系统唯一只增不减的一层，被放宽后既不能继续当禁令，
        # 也不该整条消失——见 memory.relax_constraints()。
        con.execute("ALTER TABLE memories ADD COLUMN relaxed_at REAL")
    life_event_cols = {row[1] for row in con.execute("PRAGMA table_info(life_events)")}
    if "hidden_at" not in life_event_cols:
        con.execute("ALTER TABLE life_events ADD COLUMN hidden_at REAL")
    # 主动说话（`#108`）要记"她上次主动开口是什么时候""用户上次说话是什么时候"——
    # 这两个时间戳是"该不该开口"的闸门依据。放在她的生活状态行上（她自己的生活域）。
    life_cols = {r[1] for r in con.execute("PRAGMA table_info(life_state)")}
    if life_cols:
        if "her_last_ts" not in life_cols:
            con.execute("ALTER TABLE life_state ADD COLUMN her_last_ts REAL")
        if "user_last_ts" not in life_cols:
            con.execute("ALTER TABLE life_state ADD COLUMN user_last_ts REAL")
        if "roll_ts" not in life_cols:
            # 上次**掷骰**的时间（`#108` 的闸门⑥，2026-09-30 改）：
            # 掷骰要按时间窗来，否则"页面开着每 30 秒轮询一次"会比
            # "后台 20 分钟心跳"更容易触发——触发率就取决于前端轮询频率了。
            con.execute("ALTER TABLE life_state ADD COLUMN roll_ts REAL")
        if "advanced_at" not in life_cols:
            con.execute("ALTER TABLE life_state ADD COLUMN advanced_at REAL")
        if "next_advance_at" not in life_cols:
            con.execute("ALTER TABLE life_state ADD COLUMN next_advance_at REAL")
        if "declined_at" not in life_cols:
            con.execute("ALTER TABLE life_state ADD COLUMN declined_at REAL")
        con.execute(
            "UPDATE life_state SET advanced_at=COALESCE("
            "(SELECT max(ts) FROM life_events WHERE source IN ('model','fallback','dedup')),"
            "updated_at) WHERE id=1 AND slot IS NOT NULL AND advanced_at IS NULL")
    con.commit()
    return con


def has_data(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM memories LIMIT 1").fetchone() is not None


def migrate_from_json(con: sqlite3.Connection, json_path: str, vec_path: str) -> str:
    """把旧的 memory_data.json + _vecs.npy 搬进库里。只做一次。

    搬完把旧文件改名成 .migrated 留着，不删——万一新库有问题还能回头看。
    """
    if has_data(con) or not os.path.exists(json_path):
        return ""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return f"旧记忆文件读取失败，跳过迁移：{exc}"

    mems = data.get("memories") or []
    vecs = None
    if os.path.exists(vec_path):
        import numpy as np
        try:
            vecs = np.load(vec_path)
        except Exception:
            vecs = None
    # 向量条数对不上就不带向量搬：宁可先缺向量（启动会自动补），也不要错位
    if vecs is not None and len(vecs) != len(mems):
        vecs = None

    try:
        with con:
            for i, m in enumerate(mems):
                blob = vecs[i].tobytes() if vecs is not None else None
                con.execute(
                    "INSERT INTO memories(text,kind,ts,session,hits,last_hit,explicit,vec) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (m.get("text", ""), m.get("kind", "fact"), float(m.get("ts") or 0),
                     m.get("session"), int(m.get("hits") or 0), float(m.get("last_hit") or 0),
                     1 if m.get("explicit") else 0, blob))
            for s, msgs in (data.get("sessions") or {}).items():
                for i, msg in enumerate(msgs):
                    con.execute(
                        "INSERT OR REPLACE INTO messages(session,seq,role,content,ts,greeting) "
                        "VALUES(?,?,?,?,?,?)",
                        (s, i, msg.get("role", "user"), msg.get("content"),
                         msg.get("ts"), 1 if msg.get("greeting") else 0))
            for s, text in (data.get("summaries") or {}).items():
                con.execute("INSERT OR REPLACE INTO summaries(session,text,cursor) VALUES(?,?,?)",
                            (s, text, int((data.get("summary_cursor") or {}).get(s, 0))))
            for p in (data.get("pending") or []):
                con.execute("INSERT INTO pending(text,kind) VALUES(?,?)",
                            (p.get("text", ""), p.get("kind", "fact")))
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migrated_from',?)",
                        (json_path,))
    except Exception as exc:
        return f"迁移失败（原数据未改动）：{exc}"

    for old in (json_path, vec_path):
        if os.path.exists(old):
            try:
                os.replace(old, old + ".migrated")
            except OSError:
                pass
    return f"已从旧格式迁移 {len(mems)} 条记忆（原文件改名为 *.migrated 保留）"
