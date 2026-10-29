# -*- coding: utf-8 -*-
"""附近场所（`#101`）：把「你和她的偏好」翻译成查询词，再去高德拿**真实**地点。

设计立场（沿用 `D13`）：**工具给事实，人格给话术。**
这里只做三件事——偏好→查询词、查询词→真实 POI、距离→人话；
"推荐得像不像她"一律交给模型和人设，所以这个模块里**没有一句 if-then 话术**。

三条纪律：

1. **不许编**。拿不到数据就返回空，让注入块整个消失——她一旦说出一个不存在的店，
   下一轮就会照着它继续编（用户 2026-09-23 的反馈"太过于官方"是同一条线的另一端：
   宁可真话少说，也不要说假话凑数）。
2. **必须缓存**。用户问"附近有什么好玩的"这类话题**每轮都可能触发**，
   没有缓存就是每轮一次高德调用；类别级缓存（同城同类 30 分钟）足够。
3. **查询词来自偏好，不是硬编码"好玩的地方"**。偏好词表要窄：
   映射表里没有的偏好（"喜欢打游戏"）**不硬凑**，宁可少查一条也不要查错方向。

高德接口：`/v3/place/around`（周边搜索，按坐标 + 关键词/类型，支持 `radius`）。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from app.log import get

log = get("places")

_URL = "https://restapi.amap.com/v3/place/around"
_TEXT_URL = "https://restapi.amap.com/v3/place/text"
MAX_KEYWORDS = 3          # 每次最多查几类（每次都是一次真实 API 调用）
MAX_RESULTS = 6           # 每类最多留几条
DEFAULT_RADIUS = 3000     # 3 公里内（走路可达的量级）
DEFAULT_TTL = 1800        # 类别级缓存 30 分钟

# 偏好/人设里的说法 → 高德认识的查询词。
# 窄表是刻意的：映射不上就不查（见模块头第 3 条）。
PREF_TO_KEYWORDS = [
    # 书店与图书馆**分开**：想查"书店"时不该多带一次"图书馆"的 API 调用，
    # 而且提到图书馆时未必想找书店（反向同理）。
    (("书店", "看书", "买书"), ["书店"]),
    (("图书馆",), ["图书馆"]),
    (("阅读",), ["书店", "图书馆"]),
    (("晒太阳", "散步", "公园", "绿地", "遛"), ["公园", "广场"]),
    (("电影", "看电影", "影院"), ["电影院"]),
    (("商场", "逛街", "购物"), ["购物中心"]),
    (("咖啡", "咖啡馆", "奶茶"), ["咖啡厅"]),
    (("小吃", "美食", "吃", "餐厅", "吃饭"), ["美食"]),
    (("健身", "运动", "跑步", "游泳"), ["运动健身"]),
    (("博物馆", "展览", "美术馆", "文化"), ["博物馆", "展览馆"]),
    (("超市", "菜市场", "买菜"), ["超市", "农贸市场"]),
    (("唱", "KTV", "桌游", "台球"), ["娱乐场所"]),
]

# 城市名那一类偏好多半不是"去哪玩"，但城市本身对搜索有用
_CITY_HINT_KEYS = ("住在", "所在", "常驻")


# ===== 该不该去查：**双条件** =====
# 只认位置词太容易误伤（"今天累死了"里有"今天"）；只认地点词也不行
# （"我想去书店"没问附近，按已有记忆聊就够了）。两个都命中才查——
# 宁可漏一次（她老实说不确定），也不要每轮多一次 API 调用与一次无关注入。
_ASK_WORDS = ("附近", "周边", "周围", "这边", "这一带", "哪儿", "哪里", "哪家",
              "有什么好玩", "有什么好吃", "去哪", "推荐")
_PLACE_WORDS = ("地方", "好玩", "好吃", "逛", "玩", "吃", "晒", "书店", "书", "公园",
                "广场", "商场", "电影院", "电影", "咖啡", "超市", "博物馆", "展览",
                "餐厅", "饭店", "小吃", "娱乐", "运动", "健身", "图书馆")


def looks_like_nearby_question(text: str) -> bool:
    """这句话是不是在问「我这附近有什么可去的」。"""
    s = str(text or "")
    if not s.strip():
        return False
    return any(w in s for w in _ASK_WORDS) and any(w in s for w in _PLACE_WORDS)


def keywords_for(preferences: list[str]) -> list[str]:
    """偏好短句 → 查询词（去重、限量）。映射不上就不产出。"""
    out: list[str] = []
    for pref in preferences or []:
        text = str(pref or "")
        if not text:
            continue
        for triggers, words in PREF_TO_KEYWORDS:
            if any(t in text for t in triggers):
                for w in words:
                    if w not in out:
                        out.append(w)
    return out[:MAX_KEYWORDS]


def how_far(meters) -> str:
    """距离说人话。她要是念「916 米」就成导航了；没有距离就什么都不说。"""
    try:
        d = float(meters)
    except (TypeError, ValueError):
        return ""
    if d <= 0:
        return ""
    if d < 100:
        return "就在楼下"
    if d < 800:
        return "走路两三分钟"
    if d < 1500:
        return "走路十来分钟"
    if d < 3000:
        return "走路半小时出头"
    return "得坐车去"


def _label(raw: str) -> str:
    """高德的类型串（`购物服务;专卖店;书店`）→ 给人看的短标签。"""
    parts = [p for p in str(raw or "").split(";") if p]
    return "·".join(parts[:3])


# 关键字搜索是**模糊匹配**，会顺着"书苑"这种字面把公司、培训班一起捞回来。
# 实机证据（2026-09-28 成都搜索「书店」）：前 6 条里混进了
# 「成都市东方书苑文化发展有限公司（公司企业·公司）」「东方书苑（培训机构）」——
# 这种条目她一推荐就像在念工商名录。按**类型**挡掉，而不是按名字瞎猜。
_NON_LEISURE = ("公司企业", "培训机构", "政府机构", "工厂", "产业园区",
                "商务住宅", "地名地址")


def _is_leisure(poi: dict) -> bool:
    """这条 POI 像不像"人会去的地方"。"""
    t = str(poi.get("type") or "")
    return not any(bad in t for bad in _NON_LEISURE)


def _parse(data: dict, want: int = MAX_RESULTS) -> list[dict]:
    """高德响应 → 结构化条目（按距离升序）。`status != 1` 一律当失败。"""
    if not isinstance(data, dict) or str(data.get("status")) != "1":
        return []
    out: list[dict] = []
    for poi in (data.get("pois") or []):
        name = str(poi.get("name") or "").strip()
        if not name or not _is_leisure(poi):
            continue
        try:
            dist = int(float(poi.get("distance") or 0))
        except (TypeError, ValueError):
            dist = 0
        out.append({"name": name, "label": _label(poi.get("type")),
                    "distance": dist or None,
                    "address": str(poi.get("address") or "").strip()[:60],
                    "how_far": how_far(dist)})
    out.sort(key=lambda x: x["distance"] if x["distance"] is not None else 10 ** 9)
    return out[:want]


def _fetch(keyword: str, lat: float, lon: float, key: str, radius: int,
           timeout: int, opener) -> list[dict]:
    q = {"key": key, "location": f"{round(float(lon), 4)},{round(float(lat), 4)}",
         "keywords": keyword, "radius": str(int(radius)), "offset": str(MAX_RESULTS),
         "page": "1", "extensions": "base", "output": "JSON"}
    url = _URL + "?" + urllib.parse.urlencode(q)
    try:
        with opener(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("周边搜索失败（%s）：%s: %s", keyword, type(exc).__name__, exc)
        return []
    if str(data.get("status")) != "1":
        log.warning("周边搜索被拒（%s）：info=%s", keyword, data.get("info"))
        return []
    hits = _parse(data)
    log.info("周边搜索「%s」→ %d 条", keyword, len(hits))
    return hits


def search(lat: float, lon: float, keywords: list[str], key: str,
           radius: int = DEFAULT_RADIUS, ttl: int = DEFAULT_TTL,
           cache: dict | None = None, opener=urllib.request.urlopen) -> list[dict]:
    """按类别查附近场所。返回 `[{name,label,distance,address,how_far}]`（按距离升序）。

    缓存键是「坐标 + 关键词」：同一片区域同类地点在 `ttl` 秒内不重复打 API。
    任何一步失败都返回 `[]`——**绝不编地点**（模块头第 1 条）。
    """
    if not key or not keywords:
        return []
    store = cache if cache is not None else _CACHE
    now = time.time()
    out: list[dict] = []
    for kw in keywords:
        ck = (round(float(lat), 3), round(float(lon), 3), kw)
        hit = store.get(ck)
        if hit is None or (ttl and now - hit[0] > ttl):
            hits = _fetch(kw, lat, lon, key, radius, 8, opener)
            store[ck] = (now, hits)
            hit = store[ck]
        out.extend(hit[1])
    # 跨类别去重（同名店可能同时命中两类）
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in out:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        uniq.append(item)
    uniq.sort(key=lambda x: x["distance"] if x["distance"] is not None else 10 ** 9)
    return uniq[:MAX_RESULTS]


_CACHE: dict = {}


def _fetch_city(keyword: str, city: str, key: str, timeout: int, opener) -> list[dict]:
    """城市级关键字搜索（`place/text`）：拿不到坐标时的退路，拿到的仍是真数据。"""
    q = {"key": key, "keywords": keyword, "city": city, "offset": str(MAX_RESULTS),
         "page": "1", "extensions": "base", "output": "JSON"}
    url = _TEXT_URL + "?" + urllib.parse.urlencode(q)
    try:
        with opener(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("城市搜索失败（%s）：%s: %s", keyword, type(exc).__name__, exc)
        return []
    if str(data.get("status")) != "1":
        log.warning("城市搜索被拒（%s）：info=%s", keyword, data.get("info"))
        return []
    hits = _parse(data)          # 城市搜索的结果没有 distance，_parse 里会置 None
    log.info("城市搜索「%s」→ %d 条", keyword, len(hits))
    return hits


def search_city(city: str, keywords: list[str], key: str,
                ttl: int = DEFAULT_TTL, cache: dict | None = None,
                opener=urllib.request.urlopen) -> list[dict]:
    """没有坐标时的退路：按**城市**做关键字搜索。返回结构同 `search()`。"""
    if not key or not keywords or not city:
        return []
    store = cache if cache is not None else _CACHE
    now = time.time()
    out: list[dict] = []
    for kw in keywords:
        ck = ("city", str(city), kw)
        hit = store.get(ck)
        if hit is None or (ttl and now - hit[0] > ttl):
            hits = _fetch_city(kw, str(city), key, 8, opener)
            store[ck] = (now, hits)
            hit = store[ck]
        out.extend(hit[1])
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in out:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        uniq.append(item)
    return uniq[:MAX_RESULTS]


def render_block(hits: list[dict], purposes: dict | None = None) -> str:
    """把结果渲染成注入块。空结果返回空串——**不能留一个空块**（同 opinion_block）。"""
    if not hits:
        return ""
    lines = []
    for h in hits:
        bits = [h["name"]]
        if h.get("how_far"):
            bits.append(h["how_far"])
        if h.get("address"):
            bits.append(h["address"])
        if h.get("label"):
            bits.append(f"（{h['label']}）")
        lines.append("- " + "，".join(bits))
    return ("【附近真实存在的地方】下面这些是**刚刚查到的真实条目**（不是你想的、"
            "也不是以前记的）：\n" + "\n".join(lines) + "\n"
            "（怎么用：可以自然地说出店名、大概多远、在哪儿；**顺便说说你自己的看法**——"
            "你会怎么选、为什么。但别把没查到的说成有，也别念清单；"
            "这些是给别人参考的，不是让你背的功课。）")
