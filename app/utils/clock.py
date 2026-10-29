"""时间感知：让「现在几点了」和「明天早上」这两件事有确定的答案。

两个地方要用它：
- 每轮把当前时间注入上下文。不注入的话模型不知道「现在」是哪一刻，
  它说的"今天"其实是猜的，跟用户说的"今天"可能不是同一天。
- 把「明天早上」这类相对时间换算成绝对日期再存进长期记忆。
  不换算的话这条记忆过两天就废了——「明天」永远指向说话的那一天。

写入时就换算、读取时不去猜，是这里刻意选的路线：
读取时再去解析「下周三」是哪天，时间一过必然算错。
"""

from __future__ import annotations

import datetime as dt
import re

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 只认这几种写法，认不出来就不标——宁可少标，也不能把无关数字当成日期标错
_PAT_YMD = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_PAT_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_PAT_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_PAT_REL_YEAR_MD = re.compile(r"(前年|去年|今年|明年)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_PAST_EVENT = re.compile(r"曾经|以前|当时|已经|去过|去了|发生|参加过|曾在")

MAX_DAYS = 180  # 超过这个跨度就不标注时距了，多半是误识别

# 相对日期词的兜底表。**顺序有讲究**：长词在前，避免「大后天」被「后天」先匹配掉。
_REL_WORDS = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
    ("今天", 0),
    ("今日", 0),
    ("昨天", -1),
    ("昨日", -1),
    ("前天", -2),
)

# 「像约定/计划」的线索词。用于 describe_when：约定过了期要单独说白，
# 而历史事件（「上周去了医院」）只要时距就够，不用额外提醒。
PLAN_HINT = re.compile(r"约定|约好|说好|答应|计划|准备去|要去|一起去|见面|碰头")

# 「尚未兑现的承诺」线索词：比 PLAN_HINT 更窄——只认承诺/计划语义，
# **不认中性的「见面/碰头」**。真实数据里踩过：「助手在…广场见面时带了小饼干」
# 是已发生的事，用 PLAN_HINT 判会被当成"过期约定"挡掉，那等于抹掉一段共同经历。
PLAN_COMMIT = re.compile(r"约定|约好|说好|答应|计划|准备去|要去|一起去")

# ===== 时刻级解析（2026-09-23 升级）=====
# 一天里被咬了三次才升上来的：过期货标注、开场白晚上演早上的戏、
# 「九点半」被锚到错误的日期（写入时就在未来）。都源于只解析到日期级。
#
# 完成时句式。写入哨兵（fix_impossible_time）用：完成时 + 今天的未来时刻
# = 日期锚不可能（踩过：19:50 写入「晚上九点半也没有再疼」，九点半当时还没到；
# 提炼器的措辞会变——「已经」「已完全」「没有再」「病好了」都出现过，白名单要跟着喂）。
_PAST_TENSE = re.compile(r"已经|没有再|不再|好了|结束了|完成了|解决了|已(?![知而])")
# 公开别名：memory 层判断"这是不是已经发生过的事"时要用（过期约定 ≠ 已完成的事）。
PAST_TENSE = _PAST_TENSE

# 时刻：时段词可选、支持阿拉伯和中文数字、分钟支持「半」。
# 要同时吃下「晚上九点半」「晚九点半」「晚上 9 点 30 分」「21:30」「早上 10 点」。
# 单字「晚/早」也要认（提炼器会写「晚九点半」这种缩写）；长词放前面保证优先匹配。
_PAT_TIME = re.compile(
    r"(凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜|晚|早)?\s*"
    r"(?<![有第])(\d{1,2}|[零一二两三四五六七八九十]+)\s*[点：:]\s*"
    r"(半|[0-9零一二三四五六七八九十]{1,3})?\s*分?"
)

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str | None) -> int | None:
    """中文/阿拉伯数字 → int。支持 十/十一/二十/九 这种口语写法。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN_NUM.get(a, 1) if a else 1) * 10 + (_CN_NUM.get(b, 0) if b else 0)
    return _CN_NUM.get(s)


def _period_hour(period: str | None, hour: int) -> int:
    """按时段词校正小时数。晚上 9 点 = 21，中午 1 点 = 13。"""
    if period in ("下午", "傍晚", "晚上", "夜里", "晚") and hour < 12:
        hour += 12
    elif period in ("半夜",) and hour >= 6:
        hour = (hour + 12) % 24
    elif period == "中午" and hour < 11:
        hour += 12
    return hour


def find_datetime(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """解析出带时刻的完整时间点（日期+几点几分）；文本里没有时刻就返回 None。

    日期部分复用 find_date（已处理绝对/相对/跨年）；没有日期词就默认今天，
    调用方（写入哨兵）自行判断今天这个时刻合不合理。
    """
    now = now or dt.datetime.now()
    m = _PAT_TIME.search(text)
    if not m:
        return None
    period = m.group(1)
    hour = _cn_to_int(m.group(2))
    if hour is None or not (0 <= hour <= 23):
        return None
    hour = _period_hour(period, hour)
    if not (0 <= hour <= 23):
        return None
    minute = 30 if m.group(3) == "半" else (_cn_to_int(m.group(3)) or 0)
    if not (0 <= minute <= 59):
        return None
    # 无时段词、无分钟、纯中文数字的多半是「早一点」这类习惯用语，不是时刻
    if period is None and not m.group(3) and not m.group(2).isdigit():
        return None
    # 单字时段（早/晚）+ 无分钟 + 中文数字同理：「早一点睡」「晚一点来」
    if period in ("早", "晚") and not m.group(3) and not m.group(2).isdigit():
        return None
    d = find_date(text, now)
    if d is None:
        d = now.date()
    try:
        return dt.datetime.combine(d, dt.time(hour, minute))
    except ValueError:
        return None


def reanchor_relatives(text: str, base: dt.datetime) -> str | None:
    """把文本里的相对日期词按【写入那天】重锚成绝对日期（存量条目迁移用）。

    踩过：「年糕今天被关在家里了，明天会被放出去玩」是 9/22 写的，没换算；
    三天后按「现在」解析，「明天」永远指向新的明天——时间线整个错位，
    用户中秋提放假她还在惦记年糕被关。这里用条目自己的写入时间（base）重写。
    文本里已有绝对日期的不动；没有相对词的不动。返回 None 表示无需改写。
    """
    if not any(word in text for word, _ in _REL_WORDS):
        return None                                   # 快路径：绝大多数条目直接过
    if _PAT_YMD.search(text) or _PAT_DATE.search(text) or _PAT_MD.search(text):
        return None                                   # 已有绝对日期，不再动
    out = text
    for word, delta in _REL_WORDS:                    # 长词在前，「大后天」不被「后天」抢先
        if word in out:
            d = (base + dt.timedelta(days=delta)).date()
            out = out.replace(word, f"{d.month}月{d.day}日")
    return out if out != text else None


def fix_impossible_time(text: str, now: dt.datetime | None = None) -> str:
    """写入哨兵：完成时句式 + 今天的未来时刻 = 日期锚不可能，回拨到昨天。

    踩过：19:50 写入「…2026年9月23日晚上九点半左右也没有再疼」——
    九点半当时还没到，用户说的只能是昨晚。回拨规则是确定的：
    该时刻最近一次过去的发生点就是昨天，不用猜。
    只动「同时满足 完成时 + 今天 + 未来时刻 + 文本里有日期词」的条目，别的一律不碰。
    """
    now = now or dt.datetime.now()
    if not _PAST_TENSE.search(text):
        return text
    ddt = find_datetime(text, now)
    if ddt is None or ddt.date() != now.date() or ddt <= now:
        return text
    m = _PAT_YMD.search(text) or _PAT_DATE.search(text) or _PAT_MD.search(text)
    if not m:
        return text                                  # 文本里没有日期词，无从回拨
    fixed = (ddt - dt.timedelta(days=1)).date()
    if m.re is _PAT_YMD:
        repl = f"{fixed.year}年{fixed.month}月{fixed.day}日"
    elif m.re is _PAT_DATE:
        repl = fixed.isoformat()
    else:
        repl = f"{fixed.month} 月 {fixed.day} 日"
    return text[:m.start()] + repl + text[m.end():]


def now_text(now: dt.datetime | None = None) -> str:
    """给模型看的当前时间。带星期是因为「下周三」要靠它推算。"""
    now = now or dt.datetime.now()
    return (f"{now.year}年{now.month}月{now.day}日 "
            f"{WEEKDAYS[now.weekday()]} {now.hour:02d}:{now.minute:02d}")


def day_part(now: dt.datetime | None = None) -> str:
    """现在是哪个时段——**给模型看的**，用来选问候语。

    为什么要有它：`now_text()` 给的「14:12」是准确的，但她仍然在下午说「早上好」
    （用户 2026-09-28 14:13 反馈，原文备注「现在是下午，为什么说早上好」）。
    那一轮 `【当前时间】` 就在 prompt 第一行——**注入正确不等于行为正确**（同 T41/S05 的
    「注入对了、回答没说」）。把时段直接写出来，比让她自己做 24 小时制的语义换算省事，
    也把"该用哪个问候"变成一条可断言的事实。
    """
    now = now or dt.datetime.now()
    h = now.hour
    if h < 5:
        return "凌晨"
    if h < 8:
        return "早上"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    return "晚上"

def find_date(text: str, now: dt.datetime | None = None) -> dt.date | None:
    """从一句话里认出绝对日期；认不出返回 None。"""
    now = now or dt.datetime.now()
    m = _PAT_YMD.search(text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _PAT_DATE.search(text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _PAT_REL_YEAR_MD.search(text)
    if m:
        offset = {"前年": -2, "去年": -1, "今年": 0, "明年": 1}[m.group(1)]
        try:
            return dt.date(now.year + offset, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _PAT_MD.search(text)
    if not m:
        # 兜底认相对日期词。理想情况下提炼器已经把「明天」换算成绝对日期了，
        # 但万一它漏了，条目里就会留着一个「明天」——而 find_date 认不出它，
        # 后果是这条约定对时间系统**完全隐形**：upcoming() 捞不到、describe_when
        # 不标注，用户永远等不到提醒。所以这里必须自己兜住。
        for word, delta in _REL_WORDS:
            if word in text:
                return (now + dt.timedelta(days=delta)).date()
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if _PAST_EVENT.search(text):
        try:
            current_year = dt.date(now.year, month, day)
            return current_year if current_year <= now.date() else dt.date(now.year - 1, month, day)
        except ValueError:
            return None
    # 没写年份的：优先当年；如果按当年算已经很早（半年以前）了，那多半是指明年
    for year in (now.year, now.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            return None
        if d >= now.date() - dt.timedelta(days=MAX_DAYS):
            return d
    return None


def describe_when(text: str, now: dt.datetime | None = None) -> str:
    """给带日期的记忆条目补一个时距，省得模型自己算错。

    例：「用户 9 月 23 日上午要去医院复查」→ 末尾补上「（还有 1 天）」
    认不出日期就原样返回，不做任何改动。
    """
    now = now or dt.datetime.now()
    d = find_date(text, now)
    if d is None:
        return text
    days = (d - now.date()).days
    if abs(days) > MAX_DAYS:
        return text
    if days == 0:
        rel = "就是今天"
    elif days == 1:
        rel = "明天"
    elif days == 2:
        rel = "后天"
    elif days > 0:
        rel = f"还有 {days} 天"
    elif days == -1:
        rel = "昨天"
    else:
        rel = f"已经过去 {-days} 天"
    # 过期约定要单独说白。光写「已经过去 3 天」对模型是**时距**信息，它仍可能
    # 把这条当「还没发生」讲出来（「我们约好明天去广场」）。约定类条目一旦过期，
    # 补一句明确的判断，比让它自己从时距推结论可靠。
    # 只对「像约定/计划」的条目加，避免给历史事件（「上周去了医院」）加没必要的噪音。
    if days < 0 and PLAN_HINT.search(text):
        rel += "，这个约定已经过期了，不要再说成还没发生"
    elif days == 0:
        # 时刻级判断（今天）：只到日期级时，「早上 10 点的约」晚上还被当成没发生，
        # 开场白就在晚上 8 点催早上的约（踩过）。
        ddt = find_datetime(text, now)
        if ddt is not None and ddt.date() == d:
            if ddt <= now and PLAN_HINT.search(text):
                rel = (f"就是今天，但约定时刻（{ddt.hour} 点）已经过了，"
                       "不要再说成还没发生，更不要发出邀约")
            elif ddt > now:
                rel += f"（约定时刻 {ddt.hour}:{ddt.minute:02d}，还没到）"
    return f"{text}（{rel}）"
