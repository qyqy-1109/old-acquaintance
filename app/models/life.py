# -*- coding: utf-8 -*-
"""她自己的生活（`#107`）：一个会**随时间自己往前走**的状态 + 流水。

需求原话（用户 2026-09-29）：「我想让她有自己的生活，但是她终归是一个虚拟人，
她没有时间观念、没有想法，我只能让她尽可能的像人。」

诊断：她有「现在几点」（`clock` 注入），也有"事件日期"，但**没有属于她自己的
现在进行时**——用户不在时她的世界就暂停，用户回来她的"今天"从零重编。这一层补的就是它。

三条设计决定（都对应用户的原话）：

1. **按自然时间段推进，不按对话轮次**：一天切 7 段（早上/上午/中午/下午/傍晚/晚上/深夜），
   新时段开始后在一个短窗口内推进一次。用户三天不理她，她的生活照走——回来时她能说"这两天忙死了"。
2. **每一格她自己选**（用户明确要的："就是想让她自己选"）：给几个由**她的职责与偏好**
   推出的候选，让她挑一个。**习惯由她自己的选择累积出来**——不是我写死日程，
   也不是让她凭空复读底色（实测两头都不像人）。选完写进流水，下一格她看得到。
3. **隔离**：她的生活流水**不进 `memories`**、不参与召回/作废，也**绝不**进画像分析的
   输入。否则她编的"自己的生活"会污染"用户是谁"（那条风险实测过：她的发言会进对话历史，
   而画像分析器是照对话历史提炼的）。

编剧层（她的一天有哪些时段、她什么职责、她的偏好倾向）放在 `life.json`：**手写、慢变**。
它不是日程表——里面没有任何一条规定"她周二晚上 8 点做什么"，只给维度，选择在她。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time

from app.log import get
from app.utils.location import confirmed_place

log = get("life")

# 一天切 7 段。判据用小时，见 `slot_of()`。
SLOTS = ("morning", "forenoon", "noon", "afternoon", "evening", "night", "late")
SLOT_LABEL = {
    "morning": "清晨", "forenoon": "上午", "noon": "中午", "afternoon": "下午",
    "evening": "傍晚", "night": "晚上", "late": "深夜",
}

# 每一格的兜底候选（模型不可用 / 返回不合法时用）。
# 注意：这是**兜底**，不是日程——正常路径是她自己选。
# ⚠ 2026-09-30 换人（`#140`）：这张表也得跟着换。**换人时最容易漏的就是它**——
#   库里清空了，但程序立刻用这里的兜底把"她此刻"重新生成出来，于是新她第一句话
#   变成「在给花浇水呢」（旧她的兜底）——真机实测踩到过。
FALLBACK = {
    "morning": ("在家", "刚起，洗漱完准备去学校"),
    "forenoon": ("学校", "上课，或者没课就在图书馆待着"),
    "noon": ("学校食堂", "吃饭，顺便刷会儿手机"),
    "afternoon": ("学校", "下午没课，打会儿球或者回屋待着"),
    "evening": ("出租屋", "刚回来，随便弄点吃的"),
    "night": ("在家", "追剧、听歌，或者逗团子"),
    "late": ("在家", "准备睡了"),
}

LIFE_PROMPT = """你是「{her}」这个角色的生活推进器。现在是{now}（{slot_label}）。

【她是谁】{identity}
【她的职责与作息】{duty}
【她的偏好倾向】{tastes}
【她牵挂的】{cares}

【她最近的生活流水】（越靠上越新）
{recent}

【她和对方最近的未完话题】（只把有来源、仍在有效期内的事项当作可接素材）
{context_source}

【这一格要做什么】
她现在进入「{slot_label}」这一段。请先想出 3 个这一段里都合理、但活动主题不同的候选，再替她挑一个。
每个候选都要给出她此刻在哪、在做什么、心情和牵挂。

要求：
- 挑的事要**符合她的作息与偏好**，并且**跟她刚做的事接得上**（别上午在公司、中午忽然在家）。
- 3 个候选都必须符合她的背景；不要为了凑数硬塞她不会做的事。
- 3 个候选的活动主题要有区别（例如上课、打球、去后街吃点什么），不能只是同一句话换几个字。
- **`event` 必须写"这一格新发生的事"**：不要和上面【她最近的生活】里的任何一条重复
  （**哪怕意思相近也不行**）。上一格在球场上，这一格就得是别的进展——
  打完回屋了、或者被别的事打断了。**照抄上一格的写法是最糟的**（她的一天会看起来没在走）。
- 允许她**有自己的想法**：可以选"今天想换换"，也可以照着最近的习惯来。**不必每次都不一样**，
  人本来就会重复。但如果最近几格都在做同一件事，可以偶尔换一件。
- 这一格的事要**具体、小、当下**（"在赶一份表"、"在菜市场挑茄子"），
  不要写"度过了充实的一天"这种总结。
- **不要提到对方**（用户），也不要提"你们"共同的经历——这是**她自己的生活**。
- 心情用一句短话（"有点累"、"挺松快"），牵挂没有就给空串。

只输出 JSON，不要解释。choice 从 0 开始：
{{"candidates": [
  {{"place": "在哪", "activity": "在做什么", "mood": "心情一句话", "on_mind": "牵挂，可空", "event": "流水一句话"}},
  {{"place": "在哪", "activity": "在做什么", "mood": "心情一句话", "on_mind": "牵挂，可空", "event": "流水一句话"}},
  {{"place": "在哪", "activity": "在做什么", "mood": "心情一句话", "on_mind": "牵挂，可空", "event": "流水一句话"}}
], "choice": 0}}
"""


def slot_of(now=None) -> str:
    """当前自然时间段。纯函数，方便测试（不依赖真时钟）。"""
    import datetime as dt
    now = now or dt.datetime.fromtimestamp(time.time())
    h = now.hour
    if h < 6:
        return "late"
    if h < 9:
        return "morning"
    if h < 11:
        return "forenoon"
    if h < 14:
        return "noon"
    if h < 17:
        return "afternoon"
    if h < 19:
        return "evening"
    if h < 23:
        return "night"
    return "late"


def _slot_index(t) -> int:
    """把跨午夜的深夜也算作独立一步，供补算统计。"""
    slot = slot_of(t)
    index = 0 if slot == "late" and t.hour < 6 else SLOTS.index(slot) + 1
    return t.date().toordinal() * (len(SLOTS) + 1) + index


def _next_slot_time(t):
    """返回"下一个时段的起点"（用于逐格补算）。"""
    import datetime as dt
    if slot_of(t) == "late":
        if t.hour < 6:
            return dt.datetime.combine(t.date(), dt.time(6, 30))
        return dt.datetime.combine(t.date() + dt.timedelta(days=1), dt.time(0, 30))
    order = list(SLOTS)
    cur = order.index(slot_of(t))
    nxt = (cur + 1) % len(order)
    starts = {"morning": 6, "forenoon": 9, "noon": 11, "afternoon": 14,
              "evening": 17, "night": 19, "late": 23}
    return dt.datetime.combine(t.date(), dt.time(starts[order[nxt]], 30))


def _slot_time(index):
    import datetime as dt

    day, offset = divmod(index, len(SLOTS) + 1)
    hours = (0, 6, 9, 11, 14, 17, 19, 23)
    return dt.datetime.combine(dt.date.fromordinal(day), dt.time(hours[offset], 30))


DEFAULT_LIFE = {
    "hint": "她的生活编剧层：她的时段是固定的，但**每一格做什么由她自己选**（见 app/models/life.py）。"
            "这里只给维度（职责、偏好、牵挂），不要写具体日程表。",
    # 她的日常职责/作息（慢变，手写）
    # ⚠ 2026-09-30 换人（`#140`）：从"上班的姑娘"换成"隔壁的大二女生"。这份默认只在
    # `life.json` 不存在时兜底——不跟着换的话，空库路径会让她**回去上班**。
    # `#141`：按角色卡重写了措辞（写"她是哪种人"，不写日程表——时段表由 `advance()` 管）。
    "duty": "大二在读，学校就在小区隔壁，走路十分钟。上午有课就出门，没课睡到自然醒；"
            "下午没课就跟同学打会儿球，或者回屋躺着刷手机；晚上回出租屋，"
            "赶作业能赶到半夜。周末不用上课，睡个懒觉，天气好就去球场转转。",
    # 她的偏好倾向（慢变，手写）——她"有想法"的依据就在这
    # `#142`：用户校准——「其实是喜欢打篮球，没那么热爱」，所以从"正经爱好"降成"有空就打两场"。
    "tastes": "喜欢打球，但没到痴迷的地步——有空就去打两场，天太热或者犯懒就改天；"
              "爱吃辣和烧烤，夏天离不开冰的；"
              "晚上爱放歌，音量常常忘了收（这楼隔音差，邻居听得见）；喜欢老小区里香樟和桂花的"
              "味道；运动鞋比裙子多，阳台上晾的是球衣和运动背心。",
    # 她在意/牵挂的（慢变，手写）
    "cares": "一起打球的那几个同学；隔壁的阿哲——搬来那天她抱着球上楼，差点砸到他脚；"
             "她养的那只银渐层猫团子，一出门就惦记它有没有扒窗户；"
             "楼下石桌边下棋择菜的邻居；还有期末的作业和考试。",
}


class LifeBook:
    """她的生活状态与流水。数据落在 `memory.db` 的两张表（`life_state` / `life_events`）。"""

    _DONE_MARKERS = ("浇了", "浇过", "浇好", "浇完", "理完", "处理完", "做完",
                     "弄完", "弄好", "忙完", "搞定", "完成了", "完成",
                     # 做饭/家务这一族（`#138` 按真机现场补）：她 21:38 说「汤炖好了，小青菜也
                     # 放了，端过来啦」，而当时词表里没有"炖好"→ `_is_completed` 判否 →
                     # 状态一直停在 19:23 的「回家炖汤中」，用户看到的就是这个矛盾。
                     "炖好", "煮好", "炒好", "蒸好", "做好", "盛好", "洗完", "收拾好")
    _NOT_DONE_MARKERS = ("还没", "没有", "没", "未", "尚未", "等我", "要",
                         "准备", "待会", "稍后")

    def __init__(self, db, path: str, her_name: str = "她", identity: str = "",
                 advance_delay_min: int = 10, advance_delay_max: int = 50,
                 lock_source=None, context_source=None):
        if not 0 <= advance_delay_min <= advance_delay_max <= 60:
            raise ValueError("生活推进延迟须满足 0 <= 最小分钟 <= 最大分钟 <= 60")
        self.db = db
        self.context_source = context_source
        # `B3`（工单二）：写库必须和 `MemoryStore` **共用同一把 RLock**（契约见 db.py:186-190：
        # "多语句事务的互斥靠 MemoryStore 里的那把 RLock"）。原来这里全是裸写（`with self.db:`）→
        # 在这条共享连接上会和对话路径交错（`#124` 在别处收口过，这两处没跟上）。
        # `lock_source` 就是 `MemoryStore`：锁**每次现取、不缓存对象**——缓存会让"替换
        # store._lock"这种收口手段失效（`tests/test_promise_reminders.py` 正是靠替换它验证
        # enqueue 到底会不会等锁）。单独用（脚本/测试）时退回自己那把 RLock。
        self._lock_source = lock_source
        self._own_lock = threading.RLock()
        self.path = path
        self.her_name = her_name or "她"
        self.identity = identity or ""
        self.advance_delay_min = advance_delay_min
        self.advance_delay_max = advance_delay_max
        self.cfg = self._load_cfg()

    @property
    def _write_lock(self):
        """`B3`：写库要用的锁——**动态取**（理由见 `__init__` 里的注释）。"""
        source = getattr(self, "_lock_source", None)
        lock = getattr(source, "_lock", None) if source is not None else None
        return lock if lock is not None else self._own_lock

    # ---------- 编剧层（life.json，手写） ----------
    def _load_cfg(self) -> dict:
        if not os.path.exists(self.path):
            self._save_cfg(DEFAULT_LIFE)      # 首次运行生成一份可改的模板
            return dict(DEFAULT_LIFE)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            # 与 `Personality` 同样的教训（T73）：**不许**拿默认值把它盖掉
            log.error("life.json 读不出来，暂用默认编剧层（原文件不动）：%s", exc)
            return dict(DEFAULT_LIFE)
        out = dict(DEFAULT_LIFE)
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if v})
        return out

    def _save_cfg(self, data: dict) -> None:
        from app.utils.jsonfile import write_json_atomic
        try:
            write_json_atomic(self.path, data)
        except Exception as exc:
            log.error("life.json 写入失败：%s", exc)

    # ---------- 状态 ----------
    def state(self) -> dict:
        row = self.db.execute("SELECT * FROM life_state WHERE id=1").fetchone()
        return dict(row) if row else {}

    def script(self) -> dict:
        """编剧层（`life.json`）的原文——**只有维度，没有任何具体日程**。

        看板用它回答"她说的细节是从哪来的"（用户 2026-09-30：「我不知道薄荷是哪里来的」）：
        这一层写着「喜欢侍弄花（阳台几盆）」，而她说「阳台那盆**薄荷**没浇水」——
        **花名是模型在那一格现编的**，不在这一层里，也没落进任何一张表。
        """
        cfg = self._load_cfg() or {}
        return {k: str(cfg.get(k) or "") for k in ("duty", "tastes", "cares")}

    def next_slot_at(self, now=None) -> float:
        """下次已安排的推进时间。看板用它说"下一格大约几点"。

        `now` **时间戳或 datetime 都收**：看板那条路径手上是 `time.time()`，
        而底下的 `_next_slot_time()` 要的是 datetime——不统一就会静默变成空字符串
        （写这一版时踩的：看板里那一栏一直是空的）。
        """
        import datetime as dt
        if now is None:
            t = dt.datetime.fromtimestamp(time.time())
        elif isinstance(now, (int, float)):
            t = dt.datetime.fromtimestamp(float(now))
        else:
            t = now
        scheduled = self.state().get("next_advance_at")
        if scheduled:
            return float(scheduled)
        return _next_slot_time(t).timestamp()

    def _next_advance_at(self, now) -> float:
        import datetime as dt

        boundary = _next_slot_time(now) - dt.timedelta(minutes=30)
        delay = random.randint(self.advance_delay_min, self.advance_delay_max)
        return (boundary + dt.timedelta(minutes=delay)).timestamp()

    def _save_state(self, slot: str, slot_date: str, data: dict, at: float | None = None,
                    advanced_at: float | None = None,
                    next_advance_at: float | None = None) -> None:
        """写状态。`at` 是"这次推进所代表的时间戳"。

        ⚠ 必须用调用方传进来的时间，**不能用真实时钟**：补算（`catch_up`）要一格一格地
        模拟"当时"的推进，如果每次都写 `time.time()`，`missed_slots()` 就会永远算出 0
        （测试抓到的：夹具传 9:30 推进、再问 15:00 落后几格 → 得到 0）。
        """
        with self._write_lock, self.db:
            updated_at = float(at if at is not None else time.time())
            current = self.state()
            place = str(data.get("place") or "")
            location_row = self.db.execute(
                "SELECT value,updated_at FROM slots WHERE entity=? AND attr=?",
                (self.her_name, "位置")).fetchone()
            if location_row and float(location_row["updated_at"] or 0) > updated_at:
                place = str(location_row["value"] or "")
            advanced_at = float(advanced_at if advanced_at is not None else
                                current.get("advanced_at") or current.get("updated_at") or
                                updated_at)
            if next_advance_at is None:
                next_advance_at = current.get("next_advance_at")
            self.db.execute(
                "INSERT INTO life_state(id,slot,slot_date,place,activity,mood,on_mind,"
                "updated_at,advanced_at,next_advance_at) "
                "VALUES(1,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "slot=excluded.slot, slot_date=excluded.slot_date, place=excluded.place, "
                "activity=excluded.activity, mood=excluded.mood, "
                "on_mind=excluded.on_mind, updated_at=excluded.updated_at, "
                "advanced_at=excluded.advanced_at, next_advance_at=excluded.next_advance_at",
                 (slot, slot_date, place, data.get("activity", ""),
                 data.get("mood", ""), data.get("on_mind", ""),
                 updated_at, advanced_at, next_advance_at))
            if place and (not location_row or str(location_row["value"] or "") != place):
                self.db.execute(
                    "INSERT INTO slots(entity,attr,value,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(entity,attr) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (self.her_name, "位置", place, updated_at))

    @staticmethod
    def _compact_text(text: str) -> str:
        return re.sub(r"[\s，。！？；：、,.!?;:()\[\]{}「」『』\"'…]+", "", str(text or ""))

    @classmethod
    def _is_completed(cls, text: str) -> bool:
        compact = cls._compact_text(text)
        if not compact:
            return False
        if re.search(r"(?:没(?:有)?(?:对上|理完|处理完|完成|做完|弄完)|"
                     r"还(?:有|剩).{0,8}没(?:有)?|仍(?:然)?(?:没|在)|依旧(?:没|在))",
                     compact):
            return False
        for marker in cls._NOT_DONE_MARKERS:
            if re.search(re.escape(marker) + r".{0,3}(?:浇|理|处理|做|弄|忙|完成|搞定)", compact):
                return False
        return any(marker in compact for marker in cls._DONE_MARKERS)

    @classmethod
    def _related(cls, current: str, text: str) -> bool:
        current_compact = cls._compact_text(current)
        text_compact = cls._compact_text(text)
        if not current_compact or not text_compact:
            return False
        if any(marker in current_compact and marker in text_compact
               for marker in ("浇", "花", "薄荷", "报销", "发票", "表格", "邮件", "会议",
                              # `#140` 换人后新她也用得上的相关性标记。
                              # 旧那批**故意留着**：它是"这句话跟她此刻在忙的事相不相关"的
                              # 启发式，不进提示词、不花 token，删了反而会让历史用例失真。
                              "球", "课", "图书馆", "饭", "食堂", "团子", "猫",
                              "作业", "考试", "睡", "澡")):
            return True
        grams = {current_compact[i:i + 2] for i in range(len(current_compact) - 1)}
        return any(len(gram) == 2 and gram in text_compact for gram in grams)

    def _update_observed_state(self, changes: dict) -> bool:
        with self._write_lock:
            current = self.state()
            if not current:
                return False
            data = {key: str(current.get(key) or "")
                    for key in ("place", "activity", "mood", "on_mind")}
            changed = False
            for key, value in changes.items():
                value = str(value or "")
                if data.get(key) != value:
                    data[key] = value
                    changed = True
            if not changed:
                return False
            self._save_state(current.get("slot") or "", current.get("slot_date") or "",
                             data, at=time.time())
            return True

    def observe_user_update(self, text: str) -> bool:
        """读取用户对她生活的明确完成确认，只更新当前状态。"""
        current = self.state()
        on_mind = str(current.get("on_mind") or "") if current else ""
        compact = self._compact_text(text)
        if any(owner in compact for owner in ("我家", "我自己的", "我那盆")):
            return False
        if not on_mind or not self._is_completed(text) or not self._related(on_mind, text):
            return False
        changed = self._update_observed_state({"on_mind": ""})
        if changed:
            if "浇" in on_mind:
                subject = re.sub(r"(?:该|还没|需要|要)?浇(?:水)?(?:了)?$", "", on_mind)
                event = f"{subject or '花'}浇过水了"
            else:
                event = f"{on_mind} → 已完成"
            self._add_event(current.get("slot") or "", event, "conversation", time.time())
            log.info("用户确认她已完成牵挂：%s", on_mind[:40])
        return changed

    def observe_assistant_update(self, text: str) -> bool:
        """读取她自己话里的完成语义，避免看板继续显示已结束的活动。"""
        current = self.state()
        observed_place = confirmed_place(text, her_name=self.her_name)
        place_changed = bool(observed_place and current and
                             observed_place != str(current.get("place") or ""))
        if place_changed:
            self._update_observed_state({"place": observed_place})
            current = self.state()
        activity = str(current.get("activity") or "") if current else ""
        if not activity or not self._is_completed(text):
            return place_changed
        if not self._related(activity, text):
            compact = self._compact_text(text)
            generic_completion = re.fullmatch(
                r"(?:我)?(?:已经|刚|都|总算|终于)?(?:理完|忙完|做完|弄完|完成)"
                r"(?:了|啦|呀|哟)?", compact)
            same_document = "单子" in compact and "单" in activity
            if not generic_completion and not same_document:
                return place_changed
        compact = self._compact_text(text)
        if "理完" in compact or "整理" in compact:
            verb = "理完"
        elif "忙完" in compact:
            verb = "忙完"
        # 做饭这一族（`#138`）：只说"完成"会变成"刚完成汤"这种不像人话的写法。
        elif "炖" in compact:
            verb = "炖好"
        elif "煮" in compact:
            verb = "煮好"
        elif "炒" in compact:
            verb = "炒好"
        elif "蒸" in compact:
            verb = "蒸好"
        elif "做" in compact:
            verb = "做好"
        else:
            verb = "完成"
        combined = self._compact_text(activity + text)
        labels = (("报销", "报销单"), ("发票", "发票"), ("表格", "表格"),
                  ("邮件", "邮件"), ("会议", "会议"), ("薄荷", "薄荷"),
                  ("花", "花"),
                  # 做饭这一族（`#138`）：对象要说出来，别都归到"手头的事"
                  ("汤", "汤"), ("菜", "菜"), ("饭", "饭"))
        label = next((name for marker, name in labels if marker in combined), "手头的事")
        changes = {"activity": f"刚{verb}{label}"}
        on_mind = str(current.get("on_mind") or "")
        if on_mind and self._related(on_mind, text):
            changes["on_mind"] = ""
        changed = self._update_observed_state(changes)
        if changed:
            self._add_event(current.get("slot") or "", changes["activity"],
                            "conversation", time.time())
            log.info("她在回复中确认活动已完成：%s", changes["activity"])
        return changed or place_changed

    def recent_events(self, limit: int = 6, *, include_hidden: bool = False) -> list[dict]:
        """最近的流水（越靠前越新）；默认只读可见记录，回忆过去时显式包含隐藏记录。"""
        visibility = "" if include_hidden else "WHERE hidden_at IS NULL "
        rows = self.db.execute(
            "SELECT id AS rid,ts,slot,text,source,hidden_at FROM life_events "
            + visibility + "ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in rows]

    def delete_event(self, rid) -> tuple[str, dict | None]:
        """从看板隐藏流水，保留她的历史。返回 `(给人看的话, 埋点载荷或 None)`。

        为什么需要它：她每一格做什么是**她自己选**的（`#107`），于是会现编具体细节
        （真事：**"阳台那盆薄荷"**）——而这些细节**不进任何记忆**（`#117` 查清的事），
        用户看着不认的时候，原来**只能手改数据库**。
        隐藏记录不作为当前生活素材，仍可在回忆过去时读取；不动记忆、世界书与此刻状态。
        """
        try:
            n = int(str(rid).strip())
        except (TypeError, ValueError):
            return f"「{rid}」不是编号。", None
        row = self.db.execute("SELECT text FROM life_events WHERE id=?", (n,)).fetchone()
        if row is None:
            return f"没有 #{n} 这条流水。", None
        text = str(row["text"])
        hidden_at = time.time()
        with self._write_lock, self.db:
            result = self.db.execute(
                "UPDATE life_events SET hidden_at=? WHERE id=? AND hidden_at IS NULL",
                (hidden_at, n))
        if not result.rowcount:
            return f"#{n} 这条流水已经隐藏，历史仍保留。", None
        return (f"已从看板隐藏这条流水：「{text[:24]}」，她的历史仍保留。",
                {"id": n, "text": text[:60], "hidden_at": hidden_at, "history_preserved": True})

    def pin_care(self, text: str) -> tuple[str, dict | None]:
        """把一句话钉进她的编剧层（`life.json` 的 `cares`）。返回 `(给人看的话, 埋点载荷或 None)`。

        为什么要它：她的每一格是**她自己选**的，所以会现编细节（"阳台那盆薄荷"）；
        钉进来 = 让这件事成为她的**长期背景**，从下一格起她就会当真，
        而不是只在某一句话里冒一下（`T91`；看板上那个"钉进她的牵挂"按钮）。
        `cares` 在 `life.json` 里是**一段用「；」连起来的字符串**（不是数组），所以是接一段。
        """
        clean = "；".join(x.strip() for x in str(text or "").replace("\n", "；").split("；") if x.strip())
        clean = clean.strip()[:60]
        if not clean:
            return "要钉的内容是空的。", None
        cfg = dict(self._load_cfg() or {})
        cares = str(cfg.get("cares") or "").strip()
        if clean in cares:
            return f"她的牵挂里已经有「{clean}」了。", None
        cfg["cares"] = (cares.rstrip("。；; ") + "；" + clean) if cares else clean
        self._save_cfg(cfg)
        self.cfg = cfg
        return (f"已钉进她的牵挂：「{clean}」（写在 `life.json`，随时能手改）。",
                {"text": clean})

    def history_for(self, question: str, limit: int = 8) -> str:
        about_her_past = (
            re.search(r"(?:你|她).{0,8}(?:昨天|前天|之前|过去|以前|最近|这几天|做过|经历过|去过)", question)
            or re.search(r"(?:昨天|前天|之前|过去|以前|最近|这几天).{0,8}(?:你|她)", question)
        )
        if not about_her_past:
            return ""
        from datetime import datetime

        events = self.recent_events(limit, include_hidden=True)
        if not events:
            return ""
        lines = []
        for event in events:
            when = datetime.fromtimestamp(float(event["ts"])).strftime("%Y-%m-%d %H:%M")
            source = ("分段补记摘要；只记录这段概况，不代表某一格的具体经历"
                      if event["source"] == "catchup_summary" else
                      "对话中确认的" if event["source"] == "conversation" else
                      "她自己选的" if event["source"] in ("model", "dedup") else "系统兜底")
            if event.get("hidden_at") is not None:
                source += "；已从看板隐藏，仅作历史记录，不作为当前事实"
            lines.append(f"- {when}：{event['text']}（{source}）")
        return ("【你最近的生活流水】这是你自己的过去，不是用户的经历。"
                "只按记录回答；没有记录的日子不要补编。\n" + "\n".join(lines))

    def _add_event(self, slot: str, text: str, source: str, at: float) -> None:
        with self._write_lock, self.db:
            self.db.execute("INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                            (at, slot, text, source))

    # ---------- 推进 ----------
    def needs_advance(self, now=None) -> bool:
        """跨段后等到已保存的推进时间，再让她进入下一段。"""
        import datetime as dt
        now = now or dt.datetime.fromtimestamp(time.time())
        st = self.state()
        if not st:
            return True
        if (st.get("slot") == slot_of(now)
                and st.get("slot_date") == now.date().isoformat()):
            return False
        due = st.get("next_advance_at")
        return not due or now.timestamp() >= float(due)

    MAX_CATCH_UP = 4

    def missed_slots(self, now=None) -> int:
        """程序关着的时候，她的生活落下了几格？

        为什么需要它：服务没跑 = 没有进程在推进她的生活。但**时间是照走的**——
        用户关了两天再打开，如果只补一格，那就是"她这两天只过了一个片段"，不真实。
        这里按时段算差值，交给 `catch_up()` 逐格补出来。
        """
        import datetime as dt
        now = now or dt.datetime.fromtimestamp(time.time())
        st = self.state()
        if not st or not st.get("updated_at"):
            return 0
        try:
            last = dt.datetime.fromtimestamp(float(st.get("advanced_at") or st["updated_at"]))
        except (TypeError, ValueError):
            return 0
        # 按时段的序号算差值（同一天内跨几段 / 跨几天）
        return max(0, _slot_index(now) - _slot_index(last))

    def _summarize_gap(self, start, end, slots: int, client=None, model: str = "") -> None:
        start_label = start.strftime("%Y-%m-%d %H:%M")
        end_label = end.strftime("%Y-%m-%d %H:%M")
        summary = ""
        if client is not None and model:
            recent = "\n".join(f"- {event['text']}" for event in self.recent_events(6)) or "（还没有）"
            prompt = (
                f"你是「{self.her_name}」的生活补记器。服务暂停期间有一段生活需要压缩补记。\n"
                f"【覆盖时间】{start_label} 至 {end_label}，共 {slots} 个自然时段。\n"
                f"【她是谁】{self.identity or '（没写）'}\n"
                f"【职责与作息】{self.cfg.get('duty', '')}\n"
                f"【偏好】{self.cfg.get('tastes', '')}\n"
                f"【牵挂】{self.cfg.get('cares', '')}\n"
                f"【此前的生活】\n{recent}\n"
                "请替她选择这段期间的大致生活脉络，按日期先后写成2至3个短段，"
                "时间很短时可只写一段。与此前生活衔接，符合背景和作息。"
                "这里只生成概况，不冒充逐格记录，不编具体次数、胜负、精确对话或承诺。"
                "不提用户，不创造共同经历；不要把偏好直接写成每一天都做过的事。"
                '只输出 JSON：{"summary": "按日期分段的生活概况，不超过180字"}'
            )
            try:
                from app.utils.llmjson import chat_json

                data, error = chat_json(client, model, [{"role": "user", "content": prompt}],
                                        temperature=0.7, max_tokens=400,
                                        retries=0, no_think=True)
                candidate = data.get("summary") if isinstance(data, dict) else None
                if isinstance(candidate, str):
                    summary = candidate.strip()[:600]
                if not summary:
                    log.warning("生活补记摘要不可用（%s），保留时间范围而不补编细节", error)
            except Exception as exc:
                log.warning("生活补记摘要失败，保留时间范围：%s: %s", type(exc).__name__, exc)
        text = (f"补记摘要（{start_label}—{end_label}，共{slots}格）："
                + (summary or "这段时间未逐格记录，不补编具体经历。"))
        self._add_event("summary", text, "catchup_summary", end.timestamp())
        log.info("她的生活补记摘要覆盖 %s 至 %s（%d格）", start_label, end_label, slots)

    def catch_up(self, client=None, model: str = "", now=None) -> list[dict]:
        """远期缺口分段摘要，最近 `MAX_CATCH_UP` 个历史格及当前格详补。"""
        import datetime as dt
        now = now or dt.datetime.fromtimestamp(time.time())
        st = self.state()
        if not st:
            return [self.advance(client=client, model=model, now=now)]
        missed = self.missed_slots(now)
        n = min(missed, self.MAX_CATCH_UP + 1)
        if n <= 0:
            return [st] if not self.needs_advance(now) else \
                [self.advance(client=client, model=model, now=now)]
        if not self.needs_advance(now):
            return [st]
        out = []
        try:
            last = dt.datetime.fromtimestamp(float(st.get("advanced_at") or st["updated_at"]))
        except (TypeError, ValueError):
            last = now
        step = last
        compressed = missed - n
        if compressed:
            step = _slot_time(_slot_index(last) + compressed)
            self._summarize_gap(_next_slot_time(last), step, compressed,
                                client=client, model=model)
        for _ in range(n):
            if not self.needs_advance(now):
                break
            step = _next_slot_time(step)
            if _slot_index(step) >= _slot_index(now):
                out.append(self.advance(client=client, model=model, now=now))
                break
            out.append(self.advance(client=client, model=model, now=step, force=True))
        current = self.state()
        if (current.get("slot") != slot_of(now)
                or current.get("slot_date") != now.date().isoformat()):
            out.append(self.advance(client=client, model=model, now=now, force=True))
        return out

    def advance(self, client=None, model: str = "", now=None, force: bool = False) -> dict:
        """把她往前推一格。**失败也要留状态**（兜底候选），绝不让她"停住"。

        返回推进后的状态。`client` 为空时直接用兜底候选（不联网也能跑）。
        """
        import datetime as dt
        now = now or dt.datetime.fromtimestamp(time.time())
        slot = slot_of(now)
        if not force and not self.needs_advance(now):
            return self.state()

        place, activity = FALLBACK.get(slot, ("在家", "待着"))
        mood, on_mind, source = "", "", "fallback"
        chosen = ""

        if client is not None and model:
            recent = "\n".join(f"- {e['text']}" for e in self.recent_events(6)) or "（还没有）"
            prompt = LIFE_PROMPT.format(
                her=self.her_name,
                now=f"{now.year}年{now.month}月{now.day}日 {now.hour:02d}:{now.minute:02d}",
                slot_label=SLOT_LABEL.get(slot, slot),
                identity=self.identity or "（没写）",
                duty=self.cfg.get("duty", ""),
                tastes=self.cfg.get("tastes", ""),
                cares=self.cfg.get("cares", ""),
                recent=recent,
                context_source=(self.context_source(SLOT_LABEL.get(slot, slot))
                                if self.context_source else "（没有）"))
            try:
                from app.utils.llmjson import chat_json
                data, err = chat_json(
                    client, model, [{"role": "user", "content": prompt}],
                    temperature=0.9, max_tokens=300, retries=1, no_think=True)
                if isinstance(data, dict):
                    candidates = data.get("candidates")
                    if isinstance(candidates, list):
                        valid = [item for item in candidates
                                 if isinstance(item, dict)
                                 and str(item.get("activity") or "").strip()]
                        if valid:
                            try:
                                choice = int(data.get("choice", 0))
                            except (TypeError, ValueError):
                                choice = 0
                            choice = max(0, min(choice, len(valid) - 1))
                            recent_texts = {str(e.get("text") or "").strip()
                                            for e in self.recent_events(6)}
                            ordered = valid[choice:] + valid[:choice]
                            picked = next(
                                (item for item in ordered
                                 if str(item.get("activity") or "").strip() != activity
                                 and str(item.get("event") or "").strip() not in recent_texts),
                                valid[choice])
                            data = picked
                    if str(data.get("activity") or "").strip():
                        place = str(data.get("place") or place).strip()
                        activity = str(data.get("activity")).strip()
                        mood = str(data.get("mood") or "").strip()
                        on_mind = str(data.get("on_mind") or "").strip()
                        chosen = str(data.get("event") or "").strip()
                        source = "model"
                else:
                    log.warning("生活推进的模型输出不可用（%s），用兜底候选", err)
            except Exception as exc:
                log.warning("生活推进失败（用兜底候选，不让她停住）：%s: %s",
                            type(exc).__name__, exc)

        # ⚠ **流水不许和最近几条逐字重复**（`#119`，真机抓到的）：模型在推进下一格时会把
        # 【她最近的生活】里的 `event` 原样抄回来——实测 09:39 / 13:42 / 14:00 三条
        # **一字不差**（"月初又要填一堆表，先从报销单啃起"），于是"她的一天"看起来
        # 根本没在走。提示词里已经明写不要重复，但**光靠提示词不够**（`D4` 的教训：
        # 涉及"必须不重样"的东西，要在模型之外补一道机器兜底）。
        # 兜底写法 `时段：在做什么` —— 时段标签天然不同，所以它**构造上就不会重复**。
        label = SLOT_LABEL.get(slot, slot)
        recent_texts = {str(e.get("text") or "").strip() for e in self.recent_events(6)}
        if chosen and chosen.strip() in recent_texts:
            log.warning("生活流水与最近几条重复（模型抄了上一格的 event），改用兜底写法：%r",
                        chosen[:40])
            event = f"{label}：{activity}"
            suffix = 1
            while event in recent_texts:
                suffix += 1
                event = f"{label}：{activity}（继续{suffix}）"
            source = "dedup"
        else:
            event = chosen or f"{label}：{activity}"
        self._save_state(slot, now.date().isoformat(),
                         {"place": place, "activity": activity,
                          "mood": mood, "on_mind": on_mind},
                         at=now.timestamp(), advanced_at=now.timestamp(),
                         next_advance_at=self._next_advance_at(now))
        self._add_event(slot, event, source, now.timestamp())
        log.info("她的生活推进到「%s」：%s（%s）", SLOT_LABEL.get(slot, slot), event, source)
        return self.state()

    # ---------- 注入 ----------
    def render(self) -> str:
        """给模型看的一行块。状态为空时返回空串（**不留空气块**）。"""
        st = self.state()
        if not st or not (st.get("activity") or st.get("place")):
            return ""
        bits = []
        if st.get("place"):
            bits.append(f"你在{st['place']}")
        if st.get("activity"):
            bits.append(st["activity"])
        line = "，".join(bits)
        if st.get("mood"):
            line += f"（心情：{st['mood']}）"
        if st.get("on_mind"):
            line += f"；心里挂着：{st['on_mind']}"
        return ("【你此刻】" + line + "\n"
                "（这是**你自己**此刻的状态，不是对方的记忆，也不是对方告诉你的。"
                "对方问「你在干嘛」就照着说——但**别一开口就汇报**，也别每句都提。"
                "**你自己家里的事有个分寸**：你**人在家**时，屋里有什么、谁来了、"
                "花浇没浇，你自己看得见——直接说就行，**别反问他**；你**不在家**时你确实"
                "不知道，那就**如实说不知道**（「我还在公司呢，没回去看」），"
                "**也别问他**「是不是又跑我家蹭太阳了」——他不在你家，答不上来；"
                "**你自己干的事也一样**（你刚煮面把蛋壳掉进锅里，就别问他「蛋壳挑出来没」）；"
                "要问就问**他那边**的事。这里面没有的事不要编。）")
