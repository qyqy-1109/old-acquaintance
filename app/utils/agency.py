"""Observe everyday choice replies and ground her preferences in existing facts."""

from __future__ import annotations

import re


YIELD_CUES = ("我不挑", "都行", "随你", "听你的", "你定", "都可以", "无所谓",
              "就当我没说")

CHOICE_DIMENSIONS = {
    "drink": ("喝", "饮料", "茶", "咖啡", "果汁", "牛奶", "豆浆"),
    "food": ("吃", "食物", "饭", "面条", "面食", "粥", "菜", "饺", "饼", "馄饨"),
    "activity": ("做", "干什么", "活动", "去", "出门", "玩", "运动", "打球", "散步",
                 "逛", "看书", "阅读", "电影", "追剧"),
}


def _dimensions(text: str) -> set[str]:
    result = {name for name, words in CHOICE_DIMENSIONS.items()
              if any(word in str(text or "") for word in words)}
    specific = result & {"drink", "food"}
    return specific or result


def asks_for_her_choice(question: str) -> bool:
    text = str(question or "")
    return bool(
        re.search(r"你.{0,8}(?:更?想|更?喜欢|选|挑|定|偏好|偏向)", text)
        or re.search(r"(?:听你的|你来[选定挑]|由你[选定挑])", text)
    )


def _terms(text: str) -> set[str]:
    text = str(text or "").casefold()
    for phrase in ("你来选", "你来定", "你来挑", "听你的", "你更喜欢", "你更想", "你想", "还是",
                   "周末", "今天", "明天", "哪里", "去哪", "哪个"):
        text = text.replace(phrase, " ")
    terms = set(re.findall(r"[a-z0-9]+", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return terms - {"周末", "今天", "明天", "喜欢", "想去", "哪里", "去哪", "哪个"}


def preference_hint(question: str, favorite_places: list, life_events: list[dict],
                    *, tastes: str = "") -> str:
    if not asks_for_her_choice(question):
        return ""
    terms = _terms(question)
    dimensions = _dimensions(question) if any(word in str(question or "")
                                               for word in ("什么", "去哪", "哪里")) else set()

    def relevant(value):
        return bool(terms & _terms(value) or dimensions & _dimensions(value))

    places = []
    for item in favorite_places or []:
        value = item.get("text") if isinstance(item, dict) else item
        value = str(value or "").strip()
        if value and (not terms or relevant(value)):
            places.append(value)
    chosen = [str(event.get("text") or "").strip()
              for event in (life_events or [])
              if event.get("source") == "model" and str(event.get("text") or "").strip()
              and ((terms and relevant(event.get("text")))
                   or (not terms and any(_terms(place) & _terms(event.get("text")) for place in places)))]
    related_tastes = [part.strip() for part in re.split(r"[；;。\n，,]", str(tastes or ""))
                      if part.strip() and relevant(part)]
    if not places and not chosen and not related_tastes:
        return ""
    lines = ["【你的选择依据】"]
    if places:
        lines.append("你平时喜欢去的地方：" + "；".join(places[:5]))
    if chosen:
        lines.append("你生活记录中的原话事实：" + "；".join(chosen[:3]))
    if related_tastes:
        lines.append("生活脚本中相关的偏好：" + "；".join(related_tastes[:3]))
    lines.append(
        "这些是与当前话题有关的已有依据，你现在也可以有新的想法。偏好不证明具体往事；"
        "生活流水只证明写下的事，不要补写过去的动作、味道和次数。"
        "对方自己的重大决定仍由他拍板。"
    )
    return "\n".join(lines)


def yield_cues(question: str, answer: str) -> list[str]:
    if not asks_for_her_choice(question):
        return []
    text = str(answer or "")
    return [cue for cue in YIELD_CUES if cue in text]
