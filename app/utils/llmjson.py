"""跟大模型要 JSON 时的两个通用兜底动作。

在智谱 glm 的推理模型上踩过两次：thinking token 会先吃掉 max_tokens 额度，
于是正文 content 变成空串（内容其实在 reasoning_content 里），或者 JSON 被
截在半截、括号没闭合。user 画像分析、记忆提炼、性格迭代三处都要处理同样
的问题，所以抽到这里共用。
"""

from __future__ import annotations

import json
import re

from openai import BadRequestError

from app.log import get

log = get("llm")

# 被截断的 JSON 常见的几种残缺尾巴，逐个试着补回来
_TAILS = ("", "}", "]}", '"}', '"}}', "]}}")


def parse_json(text: str) -> dict | None:
    """从模型回复里抠出第一个 JSON 对象；缺尾巴的试着补回去。"""
    if not text:
        return None
    stripped = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", stripped, flags=re.S)
    if m:
        candidates = [m.group(0)]
    else:
        # `B7`（工单二）：括号没闭合的**截断回复**走不到上面——`\{.*\}` 本身就要求有闭括号，
        # 于是下面那圈 `_TAILS` 补尾成了死代码（实测 `parse_json('{"facts": ["a","b"]')` → None）。
        # 这里从**第一个 `{`** 起截一段当候选，交给同一个补尾循环去试。
        start = stripped.find("{")
        candidates = [stripped[start:]] if start >= 0 else []
    for raw in candidates:
        for tail in _TAILS:
            try:
                data = json.loads(raw + tail)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    return None


def strings(data: dict | None, *keys: str) -> list[str]:
    """把 JSON 里的某个数组字段安全地取成字符串列表，去重去空。"""
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            s = str(item).strip()
            if s and s not in out:
                out.append(s)
    return out


def _unsupported_thinking_error(exc: Exception) -> bool:
    if not isinstance(exc, BadRequestError) or getattr(exc, "status_code", None) != 400:
        return False
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict) and str(detail.get("code")) == "1210":
            return True
    text = f"{body} {exc}".lower()
    return ("thinking" in text
            and any(marker in text for marker in ("not supported", "does not support",
                                                  "unsupported", "不支持")))


def _create(client, model: str, messages: list[dict], temperature: float,
            max_tokens: int, no_think: bool):
    kw = {"extra_body": {"thinking": {"type": "disabled"}}} if no_think else {}
    try:
        return client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, **kw)
    except Exception as exc:
        if not no_think or not _unsupported_thinking_error(exc):
            raise
        # glm-5.3 系列「始终思考，不支持关闭」（错误码 1210），退回默认行为重试一次
        return client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens)


def chat_text(client, model: str, messages: list[dict], temperature: float = 0.7,
              max_tokens: int = 800, no_think: bool = False) -> str:
    """要一段普通文本（不是 JSON）时用这个，比如开场白。失败返回空串，由调用方兜底。"""
    try:
        resp = _create(client, model, messages, temperature, max_tokens, no_think)
    except Exception as exc:
        log.warning("调用失败：%s: %s", type(exc).__name__, exc)
        return ""
    text = (resp.choices[0].message.content or "") if resp.choices else ""
    if not text.strip():
        try:
            text = resp.model_dump()["choices"][0]["message"].get("reasoning_content") or ""
        except Exception:
            pass
    return text.strip()


def chat_json(client, model: str, messages: list[dict], temperature: float = 0.2,
              max_tokens: int = 2000, retries: int = 0,
              no_think: bool = False) -> tuple[dict | None, str]:
    """调一次模型并要求它返回 JSON，返回 (解析结果, 错误说明)。

    结果不为 None 时错误说明是空串；为 None 时错误说明可以直接打给用户看。
    retries：解析失败再试几次。模型偶尔会在 JSON 前后加一段闲话、或者干脆不按格式来，
    重试一次通常就正常了，代价远小于为此丢掉一整轮的记忆。
    no_think：关掉思考链路。后台那些活（提炼、查重、摘要）要的都是「按格式吐一小段 JSON」，
    不需要深度推理，关掉之后快 5～10 倍，也不会再把 max_tokens 吃光导致空回复。
    """
    err = "未知错误"
    for _ in range(retries + 1):
        try:
            resp = _create(client, model, messages, temperature, max_tokens, no_think)
        except Exception as exc:
            err = f"调用失败：{type(exc).__name__}: {exc}"
            continue

        text = (resp.choices[0].message.content or "") if resp.choices else ""
        if not text.strip():
            # 推理模型可能把额度都花在思考上了，正文空但有思考内容
            try:
                text = resp.model_dump()["choices"][0]["message"].get("reasoning_content") or ""
            except Exception:
                pass

        data = parse_json(text)
        if data is not None:
            return data, ""
        err = "模型返回的不是合法 JSON"
    return None, err
