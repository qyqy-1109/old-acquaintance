from __future__ import annotations

import re


LOCATION_ATTRS = {"位置", "所在地", "所在位置", "地点"}
_NON_CURRENT = re.compile(
    r"明天|后天|以后|将要|准备|打算|如果|要是|想去|昨天|前天|之前|还没|没有|没到|未到|不在|吗|么|？|\?"
)
_LOCATION = re.compile(
    r"^(?:我)?(?:现在|目前|此刻)?(?:已经|刚刚|刚|终于|还|仍然|正)?"
    r"(?P<verb>回到|到达|到了|到|在|回家|离开)(?P<place>.+)?$"
)
_PLACE_END = re.compile(
    r"^(?P<place>.*?(?:医院|诊所|学校|教室|图书馆|食堂|宿舍|出租屋|家里|家中|公司|办公室|"
    r"车上|车里|球场|公园|书店|超市|餐厅|咖啡馆|地铁站|门口|楼下|楼上))"
    r"(?=$|了|啦|呢|看|等|陪|给|休息|吃|上课|量)"
)


def _normalize_place(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"(?:了|啦|呀|呢|哦)+$", "", value)
    if value in {"家", "家中", "在家"}:
        return "家里"
    return value.removeprefix("在")


def confirmed_place(text: str, *, her_name: str = "", value: str | None = None) -> str | None:
    text = str(text or "").strip()
    if not text or re.search(r"如果|要是|假如", text):
        return None
    place = None
    for clause_match in re.finditer(r"([^，。！？；\n,!;?@]+)([，。！？；\n,!;?]|$)", text):
        clause = clause_match[1].strip()
        if clause_match[2] in {"？", "?"}:
            continue
        if not clause or _NON_CURRENT.search(clause):
            continue
        if re.search(r"吧|给我", clause):
            continue
        if not clause.startswith("我") and not re.fullmatch(
                r"(?:已经|刚刚|刚)?(?:到达|到了|到|回到|回家|离开).+(?:了|啦)", clause):
            continue
        match = _LOCATION.fullmatch(clause)
        if not match:
            continue
        verb = match["verb"]
        tail = str(match["place"] or "")
        if verb == "在" and re.match(r"想|做|说|听|看(?!台)|等", tail):
            continue
        if verb == "回家":
            if not tail or re.fullmatch(r"(?:了|啦|呀|呢|哦)+", tail):
                place = "家里"
            continue
        if verb == "离开":
            if tail:
                place = "当前位置待确认"
            continue
        bounded = _PLACE_END.match(tail)
        if bounded:
            place = _normalize_place(bounded["place"])
        elif verb in {"到", "到达", "到了", "回到"} and tail.endswith(("了", "啦")):
            candidate = _normalize_place(tail)
            if 1 < len(candidate) <= 30 and not re.search(r"想|做|说|听|问", candidate):
                place = candidate
    if value is not None and _normalize_place(value) != place:
        return None
    return place


def slot_has_current_evidence(assistant: str, *, value: str,
                              quote: str = "", source: str = "") -> bool:
    if source and source != "assistant":
        return False
    if quote and (quote not in str(assistant or "") or confirmed_place(quote, value=value) is None):
        return False
    return confirmed_place(assistant, value=value) is not None
