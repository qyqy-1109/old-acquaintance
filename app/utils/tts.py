# -*- coding: utf-8 -*-
"""语音合成：把她的回复变成声音（qwen-audio-3.1-tts-flash，DashScope WS 协议）。

v2 —— 声线档案 + 韵律脚本：
「像人」不靠全局变速，靠**局部韵律**。管线分三层：
1. clean_for_speech：剥掉给眼睛看的记号（emoji/Markdown/代码块）。
2. script_for：把句子改写成「带韵律标记的台词」——这是本模块的核心：
   - 停顿长短按声线档案变化（短停/句间停/思考停），不是每个逗号停一样长；
   - 重音与语气标签只落在少数「情绪峰」句子上（均匀分布=机械感）；
   - 局部语速用 SSML <prosody> 包裹情绪峰句（+8%/-10%），其余句子原速——
     句内有快有慢，不是整体变速；
   - 口头禅/填充词按档案概率低频出现（稳定 seed：同一轮回复永远同一版台词）。
3. _synthesize：SSML 模式合成；SSML 被拒时自动降级为纯文本重试。

回退开关：config.TTS_PROSODY=False 或 WB_TTS_PROSODY=0 → 回到 v1 纯文本管线。
声线档案：VOICE_PROFILE —— 想换「调调」改这一个字典（基线语速/停顿习惯/口头禅/情绪幅度）。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from typing import Iterator

# 同步客户端：T37 要让音频**边收边 yield**——同步 generator 直接对接 SSE 的同步
# for 循环最省事；用异步版就得在同步生成器里自己管事件循环。
from websockets.sync.client import connect as ws_connect

from app import config
from app.log import get

log = get("tts")

# ==================== 声线档案（她的「调调」） ====================
# 想微调她的说话习惯只动这里：基线、停顿、口头禅、情绪幅度。
VOICE_PROFILE = {
    "display": "安语晴 · 沈知夏调调",
    "baseline": {"rate": 1.0, "pitch": 1.0, "volume": 50},
    # 停顿习惯：逗号级短停 / 句子间中停 / 想一下的长停（毫秒，会被情绪因子缩放）
    "pause_ms": {"short": 240, "mid": 480, "think": 820},
    # 口头禅/填充词：低频出现在句首，太多就是口头禅灾难
    "fillers": ["诶，", "嗯…", "欸，"],
    "filler_prob": 0.22,
    # 句间「想一下」停顿的概率
    "think_pause_prob": 0.18,
    # 句尾偏好：欢快/软语气时偶尔挂的小尾巴
    "tail_particles": ["哦", "呀", "啦"],
    "tail_prob": 0.28,
    # 情绪表达幅度：全回复最多几处显式情绪标签（均匀分布=机械感的根源）
    "max_tags": 3,
    "min_sentence_for_tag": 8,      # 太短的句子不打标签
}

# ==================== 情绪的多维度表达 ====================
# 「欢快」不是快：音高起伏↑、重音↑、停顿↓、句尾上扬、能量↑、局部语速 +5~10%。
# 每个维度独立起作用，缺一个维度就不像那个情绪。
_MOOD_DIMS = {
    "开心": {"pause_factor": 0.75, "volume": 55, "prosody_peak": "+10%",
             "lead_tag": "[excited]", "tail_rise": True, "rate_global": 1.02,
             "think_less": True},
    "高兴": {"pause_factor": 0.75, "volume": 55, "prosody_peak": "+10%",
             "lead_tag": "[excited]", "tail_rise": True, "rate_global": 1.02,
             "think_less": True},
    "兴奋": {"pause_factor": 0.7, "volume": 58, "prosody_peak": "+15%",
             "lead_tag": "[excited]", "tail_rise": True, "rate_global": 1.05,
             "think_less": True},
    "低落": {"pause_factor": 1.4, "volume": 42, "prosody_peak": "-12%",
             "lead_tag": "[sad]", "tail_rise": False, "rate_global": 0.93,
             "think_less": False, "mid_tags": ["[sighing]", "[whispers]"]},
    "难过": {"pause_factor": 1.4, "volume": 42, "prosody_peak": "-12%",
             "lead_tag": "[sad]", "tail_rise": False, "rate_global": 0.93,
             "think_less": False, "mid_tags": ["[sighing]", "[whispers]"]},
    "伤心": {"pause_factor": 1.4, "volume": 42, "prosody_peak": "-12%",
             "lead_tag": "[sad]", "tail_rise": False, "rate_global": 0.93,
             "think_less": False, "mid_tags": ["[sighing]", "[whispers]"]},
    "疲惫": {"pause_factor": 1.25, "volume": 44, "prosody_peak": "-8%",
             "lead_tag": "[whispers]", "tail_rise": False, "rate_global": 0.95,
             "think_less": False, "mid_tags": ["[sighing]"]},
    "烦躁": {"pause_factor": 1.1, "volume": 48, "prosody_peak": "-5%",
             "lead_tag": None, "tail_rise": False, "rate_global": 0.97,
             "think_less": False, "mid_tags": ["[sighing]"]},
    "焦虑": {"pause_factor": 1.1, "volume": 46, "prosody_peak": "-5%",
             "lead_tag": None, "tail_rise": False, "rate_global": 0.97,
             "think_less": False},
}

# 官方细粒度标签白名单（3.1 支持：语气控制类 + 拟声类）
_VALID_TAGS = {"[sad]", "[angry]", "[excited]", "[whispers]", "[laughing]",
               "[gasp]", "[sighing]"}

# ==================== 文本清洗 ====================
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\u200d\u20e3]"
)
_MARKDOWN = re.compile(r"(^#+\s*)|(\*{1,3})|(`{1,3})|(^>\s*)", re.M)
_URL = re.compile(r"https?://\S+")
_BLANK = re.compile(r"\n{3,}")


def clean_for_speech(text: str) -> str:
    """把「给眼睛看」的回复清洗成「给耳朵听」的形状。纯函数，可测试。"""
    out = _CODE_BLOCK.sub("，这段我打出来给你看。", text or "")
    out = _URL.sub("", out)
    out = _EMOJI.sub("", out)
    out = _MARKDOWN.sub("", out)
    out = out.replace("——", "，").replace("……", "。")
    out = _BLANK.sub("。", out)
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out


# ==================== 韵律脚本引擎 ====================
def _rng_for(answer: str, mood: str | None) -> random.Random:
    """同一轮回复永远同一版台词（稳定、可辨识），不同轮次才有变化。"""
    seed = int(hashlib.md5(f"{answer}|{mood}".encode("utf-8")).hexdigest()[:8], 16)
    return random.Random(seed)


def _split_sentences(text: str) -> list[str]:
    """按句终符切句，保留句终符。短到没意义的碎片并回前一句。"""
    parts = re.split(r"(?<=[。！？!?…])", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and len(p) <= 2:
            out[-1] += p
        else:
            out.append(p)
    return out or ([text.strip()] if text.strip() else [])


# 情绪标签与 SSML 标记：降级纯文本时要一起剥掉
_TAG = re.compile(r"\[(?:sad|angry|excited|whispers|laughing|gasp|sighing)\]")
_SSML_TAG = re.compile(r"<[^>]+>")


def plain_text(text: str) -> str:
    """剥掉所有「给合成引擎看的标记」，只留要读的字。

    两类：SSML（`<break>` / `<prosody>`）与情绪标签（`[excited]` 等）。
    实测（#019）：PlainText 模式下 `[tag]` 会被服务端**静默忽略**（不会念出来），
    但**仍算进计费字符数**——SSML 标签同理（同一段 33 字文本，带两个 break 后
    计费 75）。所以要剥：不剥就等于白付钱。
    """
    out = _SSML_TAG.sub("", text or "")
    out = _TAG.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def trim_for_speech(text: str, limit: int) -> str:
    """按**句子**截断到 limit 字以内：宁可少念一句，也不在句中切断。

    `TTS_MAX_CHARS` 一直是死配置——注释写着"超长回复截断"，代码里却从没读过它
    （#018 查出），于是 79 字要合成 12 秒、300 字就是 40 秒。
    截断放在**脚本化之前**，这样不会把 SSML 标签切一半。
    """
    text = text or ""
    limit = int(limit or 0)
    if limit <= 0 or len(text) <= limit:
        return text
    out = ""
    for s in _split_sentences(text):
        if len(out) + len(s) > limit:
            break
        out += s
    return out or text[:limit]      # 第一句就超限时退回硬截断（总比没有声音强）


def instruction_for(mood: str | None) -> str:
    """人设语气锚 + 情绪的多维度描述（给模型的自由指令，负责「像不像」）。"""
    base = config.TTS_INSTRUCTION
    dims = _MOOD_DIMS.get((mood or "").strip()) if mood else None
    if not dims:
        return base
    bits = [base]
    if dims.get("tail_rise"):
        bits.append("句子收尾轻快、微微上扬，重点词说得更有劲，音调有起伏")
    else:
        bits.append("音调放低放缓，重音收着点，别有任何表演腔")
    if dims.get("pause_factor", 1.0) < 1.0:
        bits.append("停顿比平时短，说得更连贯")
    else:
        bits.append("停顿可以长一点，像边想边说")
    return "。".join(bits)


def script_for(answer: str, mood: str | None) -> str:
    """把清洗后的回复改写成带韵律标记的台词。

    全部随机决策都用 _rng_for 的稳定种子——同一轮回复永远同一版，
    「她的调调」才稳定可辨识。SSML 关闭（回退开关）时只做标点级停顿。
    """
    text = clean_for_speech(answer)
    if not text:
        return ""
    if not config.TTS_PROSODY:
        return text                                   # 回退：v1 纯文本管线

    dims = _MOOD_DIMS.get((mood or "").strip()) if mood else {}
    dims = dims or {"pause_factor": 1.0, "volume": 50, "prosody_peak": None,
                    "lead_tag": None, "tail_rise": False, "think_less": False}
    rng = _rng_for(answer, mood)
    sentences = _split_sentences(text)
    profile = VOICE_PROFILE
    pf = dims.get("pause_factor", 1.0)

    # 选「情绪峰」句：含 ！/？/感叹词的优先，最多 1~2 句（其余句子保持平静——
    # 所有句子同一情绪强度=机械感的根源）
    peak_idx = set()
    for i, s in enumerate(sentences):
        if ("！" in s or "？" in s or len(s) > 14) and len(s) >= profile["min_sentence_for_tag"]:
            peak_idx.add(i)
    order = sorted(peak_idx, key=lambda i: -len(sentences[i]))
    max_peaks = 2 if dims.get("lead_tag") else 0
    peak_idx = set(order[:max_peaks])

    parts: list[str] = []
    lead_used = 0
    mid_used = 0
    for i, s in enumerate(sentences):
        # 句首：低概率挂填充词（只在第一句，低频）
        if i == 0 and profile["fillers"] and rng.random() < profile["filler_prob"] \
                and not s.startswith(tuple(profile["fillers"])) \
                and not s.startswith(("诶", "嗯", "欸", "啊", "哦")):   # 原句已有语气词就不叠
            s = rng.choice(profile["fillers"]) + s
        # 情绪峰：lead 标签（首峰）+ 局部语速（prosody 包裹）
        if i in peak_idx and dims.get("lead_tag") and lead_used < 1 \
                and len(s) >= profile["min_sentence_for_tag"]:
            s = dims["lead_tag"] + s
            lead_used += 1
        if i in peak_idx and dims.get("prosody_peak"):
            s = f'<prosody rate="{dims["prosody_peak"]}">{s}</prosody>'
        # 情绪中段标签（低落系：句间偶尔叹气/轻语），总量受 max_tags 约束
        if mid_used < profile["max_tags"] and dims.get("mid_tags") \
                and i not in (0,) and len(s) >= profile["min_sentence_for_tag"] \
                and rng.random() < 0.35:
            s = rng.choice(dims["mid_tags"]) + s
            mid_used += 1
        # 句尾：欢快/软语气的陈述句偶尔挂小尾巴（疑问句原生上扬，不动）
        if (i == len(sentences) - 1 and dims.get("tail_rise")
                and profile["tail_particles"]
                and not s.rstrip().endswith(("？", "？\"", "吗", "呢"))
                and rng.random() < profile["tail_prob"]):
            if s.rstrip().endswith("。"):                 # 只挂句号结尾的陈述句，「！」/「？」本身已够表达
                s = s.rstrip("。") + rng.choice(profile["tail_particles"]) + "。"
        parts.append(s)

    # 句间停顿：短停/中停/思考停按档案权重分布，时长受情绪因子缩放——
    # 不是每个句号停一样久；偶尔（低概率）不加显式停顿，让引擎自然衔接
    out = parts[0]
    for k in range(1, len(parts)):
        roll = rng.random()
        if roll < 0.14:
            pass                                      # 自然衔接，不停
        elif roll < 0.62:
            ms = int(profile["pause_ms"]["short"] * pf)
            out += f'<break time="{ms}ms"/>' + parts[k]
        elif roll < 0.86 or not dims.get("think_less"):
            ms = int(profile["pause_ms"]["mid"] * pf)
            out += f'<break time="{ms}ms"/>' + parts[k]
        else:
            ms = int(profile["pause_ms"]["think"] * pf)
            out += f'<break time="{ms}ms"/>' + parts[k]
    return out


# ==================== 合成 ====================
def synth_stream(text: str, *, instruction: str = "", volume: int = 50,
                 rate: float = 1.0, ssml: bool = False,
                 stats: dict | None = None) -> Iterator[bytes]:
    """流式合成：**边收边 yield** 音频块。

    T37：服务端本来就流式返回（实测**首字节 0.67s**、整段 11s），原来却在
    `_synthesize` 里"收完才 return"，前端又把它塞进一条 SSE —— 用户白等一整段
    音频的时长。这里收到一块就交出一块，首声因此能到 ~1 秒级。

    `stats` 可选：调用方传 dict 进来，函数写入分段耗时与结束原因
    （conn / started / first_byte / total / chunks / bytes / timeout / error），
    这就是 T34 要的观测。**任何失败都静默结束**（调用方按"这轮没声音"处理），
    但一定会留 WARNING 日志。
    """
    st = stats if stats is not None else {}
    st.update({"conn": None, "started": None, "first_byte": None,
               "timeout": False, "chunks": 0, "bytes": 0})
    tid = uuid.uuid4().hex

    def hdr(action: str) -> dict:
        return {"action": action, "task_id": tid, "streaming": "duplex"}

    t0 = time.time()
    try:
        with ws_connect(
            config.TTS_WS_URL,
            additional_headers={"Authorization": f"Bearer {config.TTS_API_KEY}",
                                "X-DashScope-DataInspection": "enable"},
            max_size=None,
        ) as ws:
            st["conn"] = time.time() - t0
            ws.send(json.dumps({
                "header": hdr("run-task"),
                "payload": {"task_group": "audio", "task": "tts",
                            "function": "SpeechSynthesizer", "model": config.TTS_MODEL,
                            "parameters": {"text_type": "PlainText",
                                           "voice": config.TTS_VOICE,
                                           "format": "mp3", "sample_rate": 22050,
                                           "volume": volume, "rate": rate, "pitch": 1,
                                           "enable_ssml": ssml,
                                           # 语气指令。参数名是实测探出来的：`instruction`
                                           # （单数）生效，`instructions` / `prompt` 都会被
                                           # 静默忽略且音频逐字节相同（#019），而且它**不计费**。
                                           "instruction": instruction},
                            "input": {}},
            }, ensure_ascii=False))
            sent = False
            deadline = time.time() + config.TTS_TIMEOUT
            while time.time() < deadline:
                remain = max(0.1, deadline - time.time())
                try:
                    raw = ws.recv(timeout=remain)
                except TimeoutError:
                    # T33：超时原来是完全静默的（返回半截音频、连日志都没有）
                    st["timeout"] = True
                    log.warning("TTS 合成超时（%.0fs 未收完），已产出的音频照常使用",
                                config.TTS_TIMEOUT)
                    break
                if isinstance(raw, bytes):
                    if st["first_byte"] is None:
                        st["first_byte"] = time.time() - t0
                    st["chunks"] += 1
                    st["bytes"] += len(raw)
                    yield raw                    # ← 关键：立刻交出去，不等收完
                    continue
                msg = json.loads(raw)
                ev = (msg.get("header") or {}).get("event")
                if ev == "task-started" and not sent:
                    sent = True
                    st["started"] = time.time() - t0
                    ws.send(json.dumps({"header": hdr("continue-task"),
                                        "payload": {"input": {"text": text}}},
                                       ensure_ascii=False))
                    ws.send(json.dumps({"header": hdr("finish-task"),
                                        "payload": {"input": {}}}, ensure_ascii=False))
                elif ev == "task-failed":
                    raise RuntimeError((msg.get("header") or {}).get("error_message",
                                                                      "task failed"))
                elif ev == "task-finished":
                    # T38：把服务端**计费**的字符数记下来——SSML 标签也算钱
                    # （实测：同一段 33 字，带两个 <break> 后计费 75）。不记下来
                    # 就永远不知道韵律花了多少，也就没法判断值不值。
                    st["billed"] = (((msg.get("payload") or {}).get("usage") or {})
                                    .get("characters"))
                    break
            else:
                st["timeout"] = True
                log.warning("TTS 合成超过 %.0fs，已产出的音频照常使用",
                            config.TTS_TIMEOUT)
    except Exception as exc:
        st["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        log.warning("TTS 失败，本轮仅文字：%s: %s", type(exc).__name__, str(exc)[:120])
    finally:
        st["total"] = time.time() - t0


def prepare(answer: str, mood: str | None = None) -> dict | None:
    """把一轮回复准备成「要发的台词 + 合成参数」。TTS 关闭/空文本 → None。

    `speak`（非流式）与 `speak_stream`（流式）共用这一份，免得两处各算一遍参数
    慢慢走偏（当前这类"算了没用"的毛病就是这么来的 —— T27 / T28）。
    """
    if not config.TTS_ENABLED or not answer:
        return None
    mood = (mood or "").strip()
    dims = _MOOD_DIMS.get(mood) or {}
    # 先按句子截断再脚本化：`TTS_MAX_CHARS` 原来是死配置，长回复不截断（T32）
    text = script_for(trim_for_speech(answer, config.TTS_MAX_CHARS), mood)
    if not text:
        return None
    return {
        "text": text,
        "instruction": instruction_for(mood),
        "volume": int(dims.get("volume", VOICE_PROFILE["baseline"]["volume"])),
        # 语速按情绪走（原来算出来从不使用，服务端写死 1 —— T28）
        "rate": float(dims.get("rate_global", VOICE_PROFILE["baseline"]["rate"])),
        "ssml": config.TTS_PROSODY and ("<break" in text or "<prosody" in text),
        "mood": mood,
    }


def speak_stream(answer: str, mood: str | None = None,
                 stats: dict | None = None) -> Iterator[bytes]:
    """流式合成：逐块 yield 音频。**失败静默**（yield 完就结束）。

    T37 的落地：服务端本来就流式返回（实测首字节 0.67s），端到端能不能
    「边收边播」全看这里别再等收完。
    """
    p = prepare(answer, mood)
    if not p:
        return
    st = stats if stats is not None else {}
    got = 0
    for chunk in synth_stream(p["text"], instruction=p["instruction"],
                              volume=p["volume"], rate=p["rate"], ssml=p["ssml"],
                              stats=st):
        got += 1
        yield chunk
    # SSML 被拒时的兜底：**只在"一块都没拿到"时**降级（拿到一部分说明 SSML 是通的，
    # 再重试就等于把同一句话合成两遍）
    if got == 0 and p["ssml"]:
        log.warning("SSML 合成没出音频（%s），降级纯文本重试", str(st.get("error"))[:80])
        st2: dict = {}
        for chunk in synth_stream(plain_text(p["text"]), instruction=p["instruction"],
                                  volume=p["volume"], rate=p["rate"], ssml=False,
                                  stats=st2):
            got += 1
            yield chunk
        st["fallback"] = True
    if got == 0:
        log.warning("TTS 没有产出音频（error=%s，timeout=%s）",
                    st.get("error"), st.get("timeout"))
        return
    # T34：分段耗时都记下来——不然"为什么慢"只能猜（实测：握手 0.3s / 首字节 0.7s）
    log.info("TTS 流式：%d 字 → %d 块 / %d 字节（mood=%s，rate=%.2f，prosody=%s；"
             "连接 %.2fs／首字节 %s）",
             len(p["text"]), got, st.get("bytes") or 0, p["mood"] or "默认",
             p["rate"], p["ssml"], st.get("conn") or 0.0,
             "-" if st.get("first_byte") is None else f"{st['first_byte']:.2f}s")


def speak(answer: str, mood: str | None = None) -> bytes | None:
    """非流式版本：把整段收完再返回（CLI / 兼容用）。失败返回 None。"""
    p = prepare(answer, mood)
    if not p:
        return None
    st: dict = {}
    audio = b"".join(speak_stream(answer, mood, stats=st))
    if not audio:
        return None
    return audio
