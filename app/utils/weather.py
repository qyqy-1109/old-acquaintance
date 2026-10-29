# -*- coding: utf-8 -*-
"""高德天气：一小时缓存 + last-known-good，任何失败都静默。

设计原则：天气是「环境常量」，跟【当前时间】同等待遇——注入一行事实，
话术交给人格模板（工具给事实，人格给话术；不写「下雨就说带伞」的 if-then）。
所以这个模块的铁律是：**任何失败都不能挂对话**。高德挂了就退回上一次的
好数据，连上一次都没有就不注入，仅此而已。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from app.log import get

log = get("weather")

_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
# 刷新失败后多久重试。老代码无论成败都把时间戳推到现在，于是**一次网络抖动
# 会让天气冻结整整一个 TTL**（默认一小时）——2026-09-29 用户反馈里的
# "28 号的天气还挂着"就是这条叠着实况为空那个 bug 一起造成的。
FAIL_RETRY = 120


# ===== 城市 → adcode（高德天气接口要 adcode，不是城市名）=====
# 为什么要有这张表：用户 2026-09-28 的反馈直接问的是「能不能加个检测用户位置的功能」。
# 但**不做定位猜测**——猜错城市会让【天气】变成假信息，而天气是每轮都注入的事实。
# 这一版是"问一句就记住"：`/city 上海` 即可，世界书里也写一条（重启不丢）。
# 表只覆盖常用城市；不在表里就让人直接给 adcode（高德文档可查），不许瞎猜。
CITY_ADCODE = {
    "北京": "110000", "上海": "310000", "天津": "120000", "重庆": "500000",
    "杭州": "330100", "宁波": "330200", "温州": "330300", "嘉兴": "330400",
    "南京": "320100", "苏州": "320500", "无锡": "320200", "徐州": "320300",
    "常州": "320400", "南通": "320600", "扬州": "321000",
    "广州": "440100", "深圳": "440300", "珠海": "440400", "佛山": "440600",
    "东莞": "441900", "中山": "442000", "惠州": "441300",
    "成都": "510100", "绵阳": "510700", "武汉": "420100", "宜昌": "420500",
    "长沙": "430100", "株洲": "430200", "西安": "610100", "咸阳": "610400",
    "郑州": "410100", "洛阳": "410300", "济南": "370100", "青岛": "370200",
    "烟台": "370600", "合肥": "340100", "福州": "350100", "厦门": "350200",
    "泉州": "350500", "南昌": "360100", "沈阳": "210100", "大连": "210200",
    "长春": "220100", "哈尔滨": "230100", "石家庄": "130100", "太原": "140100",
    "呼和浩特": "150100", "兰州": "620100", "西宁": "630100", "银川": "640100",
    "乌鲁木齐": "650100", "拉萨": "540100", "贵阳": "520100", "昆明": "530100",
    "南宁": "450100", "桂林": "450300", "海口": "460100", "三亚": "460200",
    "香港": "810000", "澳门": "820000", "台北": "710000",
}

# 用户可能只说省份，按省会处理（天气只需要一个城市）
PROVINCE_TO_CITY = {
    "浙江": "杭州", "江苏": "南京", "广东": "广州", "四川": "成都", "湖北": "武汉",
    "湖南": "长沙", "陕西": "西安", "河南": "郑州", "山东": "济南", "安徽": "合肥",
    "福建": "福州", "江西": "南昌", "辽宁": "沈阳", "吉林": "长春", "黑龙江": "哈尔滨",
    "河北": "石家庄", "山西": "太原", "内蒙古": "呼和浩特", "甘肃": "兰州", "青海": "西宁",
    "宁夏": "银川", "新疆": "乌鲁木齐", "西藏": "拉萨", "贵州": "贵阳", "云南": "昆明",
    "广西": "南宁", "海南": "海口",
}

_CITY_SUFFIX = ("特别行政区", "自治区", "自治州", "地区", "城市", "市", "省", "县")


def adcode_for(name: str) -> str | None:
    """城市名 / 省份名 / adcode → adcode；认不出返回 None（**不猜**）。"""
    s = (name or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 6:
        return s                              # 直接给 adcode（高德文档可查）
    for suffix in _CITY_SUFFIX:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    if s in CITY_ADCODE:
        return CITY_ADCODE[s]
    if s in PROVINCE_TO_CITY:
        return CITY_ADCODE[PROVINCE_TO_CITY[s]]
    return None


def city_name_for(adcode: str) -> str:
    """adcode → 城市名（用于给用户看与写世界书）。查不到就原样返回 adcode。"""
    for city, code in CITY_ADCODE.items():
        if code == str(adcode or "").strip():
            return city
    return str(adcode or "").strip()


# 英文城市名 → 表里的键。公网 IP 反查（ip-api）返回的是英文名，必须能对上，
# 对不上就宁可不用它——**不许拿一个"最像的"顶上**（猜错城市会让每轮注入的天气变成假信息）。
CITY_EN = {
    "beijing": "北京", "shanghai": "上海", "tianjin": "天津", "chongqing": "重庆",
    "hangzhou": "杭州", "ningbo": "宁波", "wenzhou": "温州", "jiaxing": "嘉兴",
    "nanjing": "南京", "suzhou": "苏州", "wuxi": "无锡", "xuzhou": "徐州",
    "changzhou": "常州", "nantong": "南通", "yangzhou": "扬州",
    "guangzhou": "广州", "shenzhen": "深圳", "zhuhai": "珠海", "foshan": "佛山",
    "dongguan": "东莞", "zhongshan": "中山", "huizhou": "惠州",
    "chengdu": "成都", "mianyang": "绵阳", "wuhan": "武汉", "yichang": "宜昌",
    "changsha": "长沙", "zhuzhou": "株洲", "xian": "西安", "xianyang": "咸阳",
    "zhengzhou": "郑州", "luoyang": "洛阳", "jinan": "济南", "qingdao": "青岛",
    "yantai": "烟台", "hefei": "合肥", "fuzhou": "福州", "xiamen": "厦门",
    "quanzhou": "泉州", "nanchang": "南昌", "shenyang": "沈阳", "dalian": "大连",
    "changchun": "长春", "harbin": "哈尔滨", "shijiazhuang": "石家庄", "taiyuan": "太原",
    "hohhot": "呼和浩特", "lanzhou": "兰州", "xining": "西宁", "yinchuan": "银川",
    "urumqi": "乌鲁木齐", "lhasa": "拉萨", "guiyang": "贵阳", "kunming": "昆明",
    "nanning": "南宁", "guilin": "桂林", "haikou": "海口", "sanya": "三亚",
    "hong kong": "香港", "macau": "澳门", "taipei": "台北",
}


def _match_city(name: str) -> str:
    """名字（中/英/带后缀）→ 表里的城市键；对不上返回空串。"""
    s = (name or "").strip()
    if not s:
        return ""
    if s in CITY_ADCODE:                       # 已经是中文键（含「深圳」这种）
        return s
    if adcode_for(s):                          # 带「市/省」后缀或 adcode
        for city in CITY_ADCODE:
            if city in s or s.startswith(city):
                return city
        for prov, city in PROVINCE_TO_CITY.items():
            if prov in s:
                return city
    low = s.lower().replace("city", "").replace("shi", "").strip(" ,")
    if low in CITY_EN:
        return CITY_EN[low]
    for en, city in CITY_EN.items():
        if low == en or low.startswith(en + " "):
            return city
    return ""


def _city_from_amap(data: dict) -> str:
    """高德逆地理响应 → 城市键。只在 `status == "1"` 时认。"""
    if not isinstance(data, dict) or str(data.get("status")) != "1":
        return ""
    comp = ((data.get("regeocode") or {}).get("addressComponent") or {})
    for key in ("city", "province", "district"):
        got = _match_city(str(comp.get(key) or ""))
        if got:
            return got
    return ""


def _city_from_ipapi(data: dict) -> str:
    """ip-api 响应 → 城市键。只在 `status == "success"` 时认（它的失败响应也是 200）。"""
    if not isinstance(data, dict) or data.get("status") != "success":
        return ""
    return _match_city(str(data.get("city") or ""))


_IPAPI_FIELDS = "status,message,city,regionName,country"


def reverse_geocode(lat: float, lon: float, key: str, timeout: int = 8) -> dict:
    """坐标 → 城市。**只把打码到 0.1 度的坐标发出去**（≈10km，判城市够、定不到住址）。

    返回 `{city, adcode, detail, source}`；失败就是空 city（调用方据此回退到 IP）。
    """
    out = {"city": "", "adcode": "", "detail": "", "source": "高德逆地理"}
    if not key:
        out["detail"] = "没配 WB_AMAP_KEY（逆地理用不了）"
        return out
    loc = f"{round(float(lon), 1)},{round(float(lat), 1)}"
    url = "https://restapi.amap.com/v3/geocode/regeo?" + urllib.parse.urlencode(
        {"key": key, "location": loc, "extensions": "base", "output": "JSON"})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"[:120]
        log.warning("逆地理失败：%s", out["detail"])
        return out
    city = _city_from_amap(data)
    if not city:
        out["detail"] = f"高德没有给出可识别的城市（status={data.get('status')}）"
        return out
    comp = ((data.get("regeocode") or {}).get("addressComponent") or {})
    out.update(city=city, adcode=adcode_for(city) or "",
               detail=str(comp.get("district") or comp.get("city") or ""))
    return out


def city_by_ip(timeout: int = 8) -> dict:
    """公网 IP → 城市（兜底路）。返回 `{city, adcode, detail, source}`。

    为什么只当兜底：IP 是宽带出口，换城市也会骗人（实测同一台机器 IP 给出台中、
    系统定位给出成都）。而且这是**唯一一条会把请求发给第三方的路**，
    失败/认不出城市时宁可返回空，也不让一个"最像的"顶上。
    """
    out = {"city": "", "adcode": "", "detail": "", "source": "公网 IP"}
    url = "http://ip-api.com/json/?" + urllib.parse.urlencode({"fields": _IPAPI_FIELDS})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"[:120]
        log.warning("IP 反查失败：%s", out["detail"])
        return out
    city = _city_from_ipapi(data)
    if not city:
        out["detail"] = (f"IP 反查给的是「{data.get('city') or data.get('message') or '未知'}」，"
                         f"不在我认识的城市表里")
        return out
    out.update(city=city, adcode=adcode_for(city) or "",
               detail=f"{data.get('regionName') or ''} {data.get('country') or ''}".strip())
    return out


class WeatherCache:
    """天气缓存。refresh() 可能联网（只在后台调），line() 永远只读缓存。"""

    def __init__(self, key: str, city: str, ttl: int = 3600):
        self.key = key or ""
        self.city = city or ""
        self.ttl = max(60, int(ttl or 3600))
        self._data: dict | None = None   # last-known-good
        self._ts = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.city)

    @property
    def city_name(self) -> str:
        """当前城市的名字（给用户看 / 写世界书用）。"""
        return city_name_for(self.city)

    def set_city(self, city: str) -> None:
        """换城市：清掉旧缓存（否则会把上一个城市的天气当新的注入进去）。"""
        self.city = city or ""
        self._data = None
        self._ts = 0.0

    def line(self) -> str | None:
        """给注入用的一句话。只读缓存，永不阻塞、永不抛异常。

        **必须把"这是哪天的天气"写出来**：跨天而缓存没刷新时（下面 refresh 有失败
        重试与 TTL 两道门），一行不带日期的天气会被当成"今天"讲出去——用户
        2026-09-29 的反馈正是"28 号的天气状态还挂着"。
        """
        d = self._data
        if not d:
            return None
        parts = []
        today_desc = d.get("day") or d.get("weather")
        if d.get("date"):
            parts.append(f"{d['date']}：{today_desc}")
        else:
            parts.append(str(today_desc))
        if d.get("weather") and d.get("weather") != d.get("day"):
            parts.append(f"实况{d['weather']}")
        if d.get("temperature"):
            parts.append(f"{d['temperature']}°C")
        if d.get("wind"):
            parts.append(d["wind"])
        s = f"{d['city']}，" + "，".join(p for p in parts if p)
        if d.get("tomorrow"):
            s += f"；明天{d['tomorrow']}"
            if d.get("tomorrow_high"):
                s += f" {d['tomorrow_low']}~{d['tomorrow_high']}°C"
        return s

    def refresh(self) -> dict | None:
        """给后台用：缓存过期了就拉一次新的。

        两条纪律（都是 2026-09-29 那轮反馈逼出来的）：
        · **失败要可见**：老代码在失败路径上一行日志都没有，天塌了都不知道；
        · **失败要能重试**：老代码无论成败都把 `_ts` 推到现在，于是一次网络抖动
          会让天气冻结整整一个 TTL（默认一小时）——现在失败时把 `_ts` 设成
          `now - ttl + FAIL_RETRY`，也就是**只等一小会儿就再试**。
        """
        if not self.enabled:
            return None
        now = time.time()
        if now - self._ts < self.ttl:
            return self._data
        try:
            fresh = self._fetch()
        except Exception as exc:
            log.warning("天气接口请求失败：%s: %s", type(exc).__name__, exc)
            fresh = None
        if fresh:
            self._data = fresh
            log.info("天气已刷新：%s", self.line())
            self._ts = now
        else:
            log.warning("天气刷新失败（结构见上一条日志）；%d 秒后再试，"
                        "当前仍用上一次的好数据", FAIL_RETRY)
            self._ts = now - self.ttl + FAIL_RETRY
        return self._data

    def _fetch(self) -> dict | None:
        # 实况（`extensions=base`）**可能返回空数组**：高德对某些 adcode / 某些时段
        # 只给预报不给实况。老代码在这里 `return None`，于是连预报一起丢、
        # 缓存永不更新、**一行日志都没有**——用户 2026-09-29 的反馈
        # （slots 里挂着 28 号的天气、她还在说"今天晴 30 度"）就是这么来的。
        # 现在的口径：**实况缺了也要把预报用起来**，缺什么就在行里少说什么。
        base = self._call({"extensions": "base"}) or {}
        lives = base.get("lives") or []
        live = lives[0] if lives else {}
        fc = self._call({"extensions": "all"}) or {}
        casts = ((fc.get("forecasts") or [{}])[0].get("casts") or [])
        today = casts[0] if casts else {}
        tomorrow = casts[1] if len(casts) > 1 else {}
        if not live.get("weather") and not today.get("dayweather"):
            # 两边都没有才算失败（这时才返回 None，让调用方保留 last-known-good）
            log.warning("天气接口没给出可用数据：实况=%s 预报=%s（city=%s）",
                        bool(lives), bool(casts), self.city)
            return None
        if not live.get("weather"):
            log.info("天气实况为空、只有预报（city=%s，预报日=%s）——按预报注入",
                     self.city, today.get("date"))
        return {
            # 城市名优先取实况里的；实况为空时退回**从 adcode 反查**
            # （预报响应里没有城市名，直接用 adcode 注入会写成"440300，小雨"）
            "city": ((live.get("city") or "").replace("市", "")
                     or city_name_for(self.city) or self.city),
            "weather": live.get("weather") or today.get("dayweather"),
            "temperature": live.get("temperature") or today.get("daytemp"),
            "wind": (f"{live.get('winddirection', '')}风 {live.get('windpower', '')}".strip()
                     if live.get("winddirection") or live.get("windpower") else ""),
            "day": today.get("dayweather"),
            "night": today.get("nightweather"),
            "date": today.get("date"),
            # 明天预报必须有：被问「明天要不要带伞」是最常见的天气问题，
            # 只注今天她只能答「我不知道明天的」——留证 #3 抓到的缺口。
            "tomorrow": tomorrow.get("dayweather"),
            "tomorrow_low": tomorrow.get("nighttemp"),
            "tomorrow_high": tomorrow.get("daytemp"),
            "reporttime": live.get("reporttime") or fc.get("reporttime"),
        }

    def _call(self, extra: dict) -> dict | None:
        qs = urllib.parse.urlencode({"key": self.key, "city": self.city, **extra})
        with urllib.request.urlopen(f"{_URL}?{qs}", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if str(data.get("status")) == "1" else None
