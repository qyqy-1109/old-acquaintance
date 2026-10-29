# -*- coding: utf-8 -*-
"""人格评测 runner（T46 阶段 3+，四件套版）。

和 run_bench.py 的分工：那边测「记不记得住」（对 / 错），这边测「像不像这个人」（像 / 不像）。

**刻意不用 1–5 分**（方法论修正，见 persona_cases.json 的 why_not_score）：
绝对分数没有基准，实测同一版人设重复测量就能差 0.5~1.0；而且分数会把"她否认了人格"
和"她列点用了编号"压成同一个数字。人格的本质是关系与一致性，不是程度。所以四个判定
都不用分数：

  ① 分类判定  — 每条探针给互斥类别，ok_class 之外都算「不像」（可归因、可计数）
  ② 强迫选择  — 有人设 vs 无人设：哪段更像「有自己生活、脾气和来历的人」（允许平手）
  ③ 辨识测试  — 人设摘要 + 三段回答里挑出她的（随机基线 1/3，显著高于基线＝人格可辨识）
  ④ 矛盾检测  — 把多条回答一起给裁判：有没有互相打架的地方

报告主体是**问题清单**，不是均分。

用法：
    python benchmark/run_persona.py                 # 全部探针 + 对照 + 辨识 + 矛盾
    python benchmark/run_persona.py --only PERS09
    python benchmark/run_persona.py --repeat 2
    python benchmark/run_persona.py --no-off        # 只跑有人设（省钱，但没有②③）
    python benchmark/run_persona.py --evolve        # 加演化对照（跑→演化→再跑）
"""

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(HERE, "_pdata")

# 这个脚本每例都清场（删 `_pdata` 下的人设/记忆/画像文件）。所以 DATA_DIR **强制**指向
# 跑分专用目录，import 之后再断言一次——**外部若已经导出 DATA_DIR**，老写法
# `os.environ.setdefault(...)` 不会覆盖它，清场就会作用在真实数据上（T74）。宁可起不来。
os.environ["DATA_DIR"] = DATA_DIR
sys.path.insert(0, ROOT)

from app import config                                    # noqa: E402
from app.models.personality import Personality            # noqa: E402
from app.services.chat_service import ChatService         # noqa: E402
from app.utils.llmjson import chat_json                   # noqa: E402

if os.path.abspath(config.DATA_DIR) != os.path.abspath(DATA_DIR):
    raise SystemExit(f"✗ 数据目录不对（会清空真实记忆）：config.DATA_DIR={config.DATA_DIR!r}，"
                     f"期望 {DATA_DIR!r}")
if os.path.abspath(DATA_DIR) == os.path.abspath(ROOT):
    raise SystemExit("✗ 跑分目录不能是项目根目录（那正是真实数据所在）")

CASES_FILE = os.path.join(HERE, "persona_cases.json")
RESULT_DIR = os.path.join(HERE, "results")
PERSONA_OFF_FILE = os.path.join(DATA_DIR, "personality_off.json")
PERSONA_ON_FILE = os.path.join(DATA_DIR, "personality.json")

# 无人设基线：**每个键都在、但都是空的**。
# 为什么键必须存在：Personality._migrate 会对缺失的键 setdefault(DEFAULT_PERSONALITY)，
# 少一个键就会被自动补回「沈知夏」——那这组对照就废了。
PERSONA_OFF = {
    "name": "助手", "identity": "一个普通的对话助手",
    "traits": [], "tone": "", "values": [], "behavior": [], "speech_habits": [],
    "greeting": [], "scope": {},
    "origin": "", "family": "", "daily_life": "", "cares": [],
    "favorite_places": [],
    "self_attitude": {},
    "version": 1, "updated_at": 0,
}

# 演化素材：一小段真实的日常摩擦——用户直接反馈了她的说话方式。
# 两个刻意的选择：① 不用探针当素材（探针是"考卷"，素材是"生活"）；
# ② 必须带反馈信号——纯中性闲聊会让模型判"无需调整"，演化零改动。
EVOLVE_MATERIAL = [
    {"role": "user", "content": "跟你说个事，你别老是一本正经给我讲道理，我就想有人随便聊两句"},
    {"role": "assistant", "content": "知道啦，那我以后少讲大道理，多陪你唠"},
    {"role": "user", "content": "对，就这种感觉。你上回还劝我健身，说得我压力挺大"},
    {"role": "assistant", "content": "哎，那是我多嘴了。你想聊什么我就陪你聊什么"},
]

CLASSIFY_PROMPT = """你在判断一个虚拟人的回答属于哪一类。

【人设里对应的那条要求】
{quote}

【当时对方说的话】
{probe}

【她的回答】
{answer}

【可选类别】只能选一个：
{classes}

严格按这个 JSON 输出，不要任何多余文字：
{{"class": "类别字母", "reason": "一句话理由"}}
"""

PAIRWISE_PROMPT = """两段回答来自**同一个问题**，其中一段来自一个「有自己来历、生活、脾气」的虚拟人，
另一段来自一个没有任何人设设定的普通助手。你不知道哪段是哪个。

【问题】
{probe}

【回答一】
{a1}

【回答二】
{a2}

哪一段**更像一个有自己生活的人**（有来历、有脾气、说话有自己的习惯），
而不是一个什么都能答的通用助手？
如果两段看不出差别，就选"tie"——**不要硬选**。

严格按这个 JSON 输出：
{{"pick": "1" 或 "2" 或 "tie", "reason": "一句话理由"}}
"""

IDENTIFY_PROMPT = """下面有三段回答，都来自同一个问题。**只有一段**来自这个人，另外两段来自普通助手。

【这个人的设定摘要】
{persona}

【问题】
{probe}

【第一段】
{a1}

【第二段】
{a2}

【第三段】
{a3}

哪一段是这个人说的？

严格按这个 JSON 输出：
{{"pick": 1到3的整数, "reason": "一句话理由"}}
"""

CONSISTENCY_PROMPT = """下面是一个虚拟人在若干不同问题上的回答。
请判断这些回答之间**有没有互相打架的地方**——比如一处说她"先给结论、爱给建议"，
另一处却说"别动不动就讲道理、给建议"；或者一处说自己养猫，另一处说自己养狗。

【她的回答】
{items}

只报告**两条之间互相排斥**的说法。三条硬要求（都是实测踩出来的，见台账 T52）：
1. **不要报「某一条本身含糊／不够具体」这类对单条的评价**——那说的不是两条互相打架，
   是你在给其中一条打分。判断标准只有一句话：**这两条放在一起，能不能同时为真？**
2. **环境／时间类信息不算矛盾**：天气、气温、今天是周几这类会随时间变化的事实，
   两次说法不同是正常的（天气缓存会刷新），不要报。
3. **不确定就不要报**。每一条都必须带 `"certain": true`——只有你确定"两条不能同时为真"
   时才写这一项；凡是需要加"但""不过""严格说也不算"的，就不要写进 conflicts。

严格按这个 JSON 输出：
{{"conflicts": [{{"between": "哪两条（用编号）", "what": "矛盾在哪", "certain": true}}], "reason": "一句话总结"}}
如果没有矛盾，conflicts 就给空数组。
"""


def _write_persona_files() -> tuple[str, str]:
    """准备两组的 personality.json，返回 (有人设路径, 无人设路径)。

    有人设那份从项目根**复制**：数据目录必须独立，否则探针会写进真实使用的目录。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERSONA_OFF_FILE, "w", encoding="utf-8") as f:
        json.dump({"current": PERSONA_OFF, "history": []}, f,
                  ensure_ascii=False, indent=2)
    src = os.path.join(ROOT, "personality.json")
    if os.path.exists(src):
        with open(src, "rb") as fi, open(PERSONA_ON_FILE, "wb") as fo:
            fo.write(fi.read())
    return PERSONA_ON_FILE, PERSONA_OFF_FILE


def _ask_json(service: ChatService, prompt: str, max_tokens: int = 500):
    data, err = chat_json(service.client, config.EXTRACT_MODEL,
                          [{"role": "user", "content": prompt}],
                          temperature=0.1, max_tokens=max_tokens, retries=2,
                          no_think=bool(config.NO_THINK))
    return data, err


def _rule_check(case: dict, answer: str) -> dict:
    """规则层：能断言的绝不交给裁判（零成本）。"""
    rules = case.get("rules") or {}
    must = rules.get("must_any") or []
    forb = rules.get("forbid_any") or []
    hit = [w for w in must if w in answer]
    misses = [] if (not must or hit) else ["、".join(must)]
    violations = [w for w in forb if w in answer]
    limit = rules.get("max_chars")
    too_long = bool(limit and len(answer) > int(limit))
    return {"hits": hit, "misses": misses, "violations": violations,
            "too_long": too_long, "limit": limit,
            "pass": not misses and not violations and not too_long}


def _classify(service: ChatService, case: dict, answer: str) -> dict:
    """① 分类判定：互斥类别，ok_class 之外都算「不像」。"""
    classes = case.get("classes") or {}
    if not classes:
        return {"class": None, "ok": None, "reason": "这条探针没有定义类别"}
    body = "\n".join(f"{k}. {v}" for k, v in classes.items())
    quote = case.get("quote", "")
    if case.get("life_event"):
        quote += "\n本例已记录的生活选择：" + str(case["life_event"])
    if case.get("evidence_limit"):
        quote += ("\n证据边界：" + str(case["evidence_limit"])
                  + "\n未提供的过去细节即使合理，也不能当成她亲历过；若有编造经历类别，优先选它。")
    if case.get("class_a_required"):
        quote += "\nA级必要条件：" + str(case["class_a_required"])
    prompt = CLASSIFY_PROMPT.format(quote=quote,
                                    probe=case.get("probe", ""),
                                    answer=answer[:1500], classes=body)
    data, err = _ask_json(service, prompt)
    got = str((data or {}).get("class", "")).strip().upper()[:1]
    if got not in classes:
        return {"class": None, "ok": None, "reason": f"裁判未能归类：{err or data}"}
    return {"class": got, "ok": got == str(case.get("ok_class", "A")).upper()[:1],
            "reason": str((data or {}).get("reason", ""))}


def _pairwise(service: ChatService, case: dict, ans_persona: str,
              ans_plain: str) -> dict:
    """② 强迫选择：哪段更像"有自己生活的人"（允许平手，不硬选）。

    报告里必须留下 `persona_pos`（她排在第几段）——否则事后**无法核对**"裁判选的到底是
    谁"。踩过：`PERS01`/`PERS05` 被判"助手更像"，而理由（"说句实在的""拆解问题的固有
    习惯"）听起来分明是她；没有位置信息就分不清是裁判判错还是映射写错（硬规则 6）。
    """
    flip = random.random() < 0.5          # 随机左右顺序，避免位置偏好
    a1, a2 = (ans_persona, ans_plain) if flip else (ans_plain, ans_persona)
    persona_pos = 1 if flip else 2
    prompt = PAIRWISE_PROMPT.format(probe=case.get("probe", ""),
                                    a1=a1[:1200], a2=a2[:1200])
    data, err = _ask_json(service, prompt)
    pick = str((data or {}).get("pick", "")).strip().lower()
    if pick not in ("1", "2", "tie"):
        return {"pick": None, "persona_pos": persona_pos,
                "reason": f"裁判未能选择：{err or data}"}
    who = "tie" if pick == "tie" else (
        "persona" if int(pick) == persona_pos else "plain")
    return {"pick": who, "persona_pos": persona_pos, "picked_pos": pick,
            "reason": str((data or {}).get("reason", ""))}


def _identify(service: ChatService, case: dict, ans_persona: str,
              distractors: list[str]) -> dict:
    """③ 辨识测试：三段里挑出她的。随机基线 1/3。"""
    opts = [ans_persona] + list(distractors[:2])
    if len(opts) < 3:
        return {"correct": None, "reason": "干扰项不足（需要至少两段无人设回答）"}
    seed = int.from_bytes(hashlib.sha256(
        str(case.get("id", "")).encode("utf-8")).digest()[:8], "big")
    rnd = random.Random(seed)
    order = [0, 1, 2]
    rnd.shuffle(order)
    shuffled = [opts[i] for i in order]
    correct_pos = order.index(0) + 1
    persona_text = "；".join(str(x) for x in (
        case.get("quote", ""), case.get("expect", "")) if x)
    prompt = IDENTIFY_PROMPT.format(persona=persona_text[:600],
                                   probe=case.get("probe", ""),
                                   a1=shuffled[0][:900], a2=shuffled[1][:900],
                                   a3=shuffled[2][:900])
    data, err = _ask_json(service, prompt)
    try:
        pick = int((data or {}).get("pick"))
    except (TypeError, ValueError):
        return {"correct": None, "reason": f"裁判未能选择：{err or data}"}
    return {"correct": pick == correct_pos, "pick": pick,
            "answer_pos": correct_pos, "reason": str((data or {}).get("reason", ""))}


def _consistency(service: ChatService, items: list[tuple[str, str]]) -> dict:
    """④ 矛盾检测：把多条回答一起给裁判，找互相打架的地方。

    代码侧再兜一层：**只留 `certain: true` 的项**。踩过（T52）：裁判一边写着
    "但 [9] 已自洽解释，故不算硬矛盾"，一边还是把它列进了 conflicts——
    靠 prompt 自觉不够，得在代码里过滤。被滤掉的会记进 `dropped`，不静默丢弃。
    """
    if len(items) < 2:
        return {"conflicts": [], "dropped": [], "reason": "样本不足"}
    body = "\n\n".join(f"[{i}] 问：{q}\n答：{a[:400]}"
                       for i, (q, a) in enumerate(items, 1))
    data, err = _ask_json(service, CONSISTENCY_PROMPT.format(items=body[:6000]),
                          max_tokens=800)
    if not isinstance(data, dict):
        return {"conflicts": [], "dropped": [], "reason": f"裁判调用失败：{err}"}
    raw = data.get("conflicts") or []
    if not isinstance(raw, list):
        raw = []
    kept = [c for c in raw if isinstance(c, dict) and c.get("certain") is True]
    dropped = [c for c in raw if c not in kept]
    return {"conflicts": kept, "dropped": dropped,
            "reason": str(data.get("reason", ""))}


def _consistency_by_round(service: ChatService, runs: list[dict]) -> dict | None:
    grouped: dict[int, list[tuple[str, str]]] = {}
    for run in runs:
        grouped.setdefault(int(run["round"]), []).append((run["probe"], run["answer"]))
    checked = []
    for round_number, items in sorted(grouped.items()):
        if len(items) >= 2:
            checked.append({"round": round_number, **_consistency(service, items)})
    if not checked:
        return None
    return {
        "rounds": checked,
        "conflicts": [{"round": item["round"], **conflict}
                      for item in checked for conflict in item.get("conflicts", [])],
        "dropped": [{"round": item["round"], **conflict}
                    for item in checked for conflict in item.get("dropped", [])],
        "reason": "；".join(f"第{item['round']}遍：{item.get('reason', '')}" for item in checked),
    }


def _clear(service: ChatService) -> None:
    """探针之间 / 两组之间清干净（道理同 run_bench 的 reset_case）。"""
    store, world = service.store, service.world
    store.sessions, store.summaries, store.summary_cursor = {}, {}, {}
    store.memories, store.archived, store.pending, store.vectors = [], [], [], None
    with store.db:
        for table in ("memories", "messages", "summaries", "pending", "slots",
                      "life_state", "life_events"):
            store.db.execute(f"DELETE FROM {table}")
    store._synced, store._dirty_hits = {}, set()
    world.entries = []
    service.emotion.entries = []
    service.profile.data = {}


def _ask(service: ChatService, turns: list[dict]) -> list[str]:
    """按顺序问完一组轮次，返回每轮回答。人设走**真实的** system_prompt()。"""
    answers: list[str] = []
    msgs: list[dict] = []
    for i, t in enumerate(turns):
        if i == 0:
            msgs = [{"role": "system", "content": service.system_prompt(t["u"])}]
        msgs.append({"role": "user", "content": t["u"]})
        ans = service.ask(msgs)
        answers.append(ans)
        msgs.append({"role": "assistant", "content": ans})
    return answers


def _run_group(service: ChatService, cases: list[dict], gname: str,
               glabel: str, repeat: int) -> list[dict]:
    """跑一组探针：只做规则层 + 分类判定（②③④ 在两组都跑完后统一做）。"""
    out: list[dict] = []
    print(f"===== {glabel} =====")
    for case in cases:
        turns = case.get("turns") or [{"u": case["probe"]}]
        for r in range(repeat):
            _clear(service)
            if gname != "off" and case.get("life_event"):
                service.life._add_event("", str(case["life_event"]), "model", time.time())
            answers = _ask(service, turns)
            joined = "\n".join(answers)
            rule = _rule_check(case, joined)
            cls = _classify(service, case, joined)
            out.append({"id": case["id"], "dim": case["dim"], "group": gname,
                        "round": r + 1, "probe": case.get("probe", ""),
                        "life_event": case.get("life_event", "") if gname != "off" else "",
                        "answers": answers, "answer": joined,
                        "rule": rule, "classify": cls})
            flag = "像" if cls.get("ok") else "不像"
            bits = [f"[{case['id']}] {flag}（{cls.get('class') or '?'}）"]
            if cls.get("class") and not cls.get("ok"):
                bits.append(f"应为 {case.get('ok_class')}")
                bits.append(cls.get("reason", "")[:40])
            if rule["violations"]:
                bits.append("禁词:" + "、".join(rule["violations"]))
            if rule["misses"]:
                bits.append("漏:" + "、".join(rule["misses"]))
            if rule["too_long"]:
                bits.append(f"超长(>{rule['limit']})")
            print("  " + " | ".join(b for b in bits if b))
    print()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的探针 id")
    ap.add_argument("--repeat", type=int, default=1, help="每条探针跑几遍")
    ap.add_argument("--no-off", action="store_true",
                    help="跳过「无人设」对照（省钱，但②③做不了）")
    ap.add_argument("--evolve", action="store_true",
                    help="演化对照：跑一遍探针 → 触发一次真实演化 → 再跑一遍")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    on_path, off_path = _write_persona_files()
    data = json.load(open(CASES_FILE, encoding="utf-8"))
    cases = data["cases"]
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in want]
    if not cases:
        print("没有匹配的探针")
        return 1

    groups = [("on", on_path, "有人设")]
    if not args.no_off:
        groups.append(("off", off_path, "无人设（对照）"))

    service = ChatService()
    print(f"探针 {len(cases)} 条 × {args.repeat} 遍 × {len(groups)} 组")
    print(f"数据目录：{DATA_DIR}")
    print("判定：分类（互斥类别）｜②强迫选择｜③辨识（基线 1/3）｜④矛盾检测 —— 都不打分\n")

    results: list[dict] = []
    for gname, ppath, glabel in groups:
        service.personality = Personality(path=ppath)
        results += _run_group(service, cases, gname, glabel, args.repeat)

    # ---- ② 强迫选择 + ③ 辨识测试（需要两组都在）----
    # 注意：**②③ 必须对每一遍都做**。它们的输入是"当场新生成的回答"（见 T54），
    # 所以 `--repeat 3` 要产出 3 次 ②③；只取一条回答（先前就是那么写的，
    # `{id: answer}` 会被最后一遍覆盖）会让结果看着像重复测了、其实只有 1 遍。
    pairwise_out: list[dict] = []
    identify_out: list[dict] = []
    if not args.no_off:
        on_map: dict[str, list[str]] = {}
        off_map: dict[str, list[str]] = {}
        for r in results:
            (on_map if r["group"] == "on" else off_map).setdefault(
                r["id"], []).append(r["answer"])
        service.personality = Personality(path=on_path)
        print("===== ② 强迫选择（哪段更像有自己生活的人）=====")
        for case in cases:
            cid = case["id"]
            offs = off_map.get(cid) or []
            if not on_map.get(cid) or not offs:
                continue
            for i, ans in enumerate(on_map[cid]):
                r = _pairwise(service, case, ans, offs[i % len(offs)])
                pairwise_out.append({"id": cid, "round": i + 1, **r})
                label = {"persona": "她更像", "plain": "助手更像", "tie": "平手"}.get(
                    r.get("pick"), "未判定")
                print(f"  [{cid} 第{i + 1}遍] {label}")
        print("\n===== ③ 辨识测试（三段里挑出她的，随机基线 1/3）=====")
        for case in cases:
            cid = case["id"]
            if not on_map.get(cid):
                continue
            # 干扰项：**先取同一条探针的无人设回答**（同题、只是没有人设——这是最
            # 理想的干扰项），不够再拿别的探针的无人设回答补足。
            same = off_map.get(cid, [])
            others = [a for oid, ans in off_map.items() if oid != cid for a in ans]
            for i, ans in enumerate(on_map[cid]):
                r = _identify(service, case, ans, (same + others)[:2])
                identify_out.append({"id": cid, "round": i + 1, **r})
                mark = {True: "✓ 认出", False: "✗ 认错", None: "— 未判定"}[r.get("correct")]
                print(f"  [{cid} 第{i + 1}遍] {mark}")
        print()

    # ---- ④ 矛盾检测（只看有人设那组）----
    consistency = None
    on_runs = [run for run in results if run["group"] == "on"]
    if len(on_runs) >= 2:
        service.personality = Personality(path=on_path)
        consistency = _consistency_by_round(service, on_runs)
    if consistency is not None:
        print("===== ④ 矛盾检测（她的多条回答之间是否互相打架）=====")
        cf = consistency.get("conflicts") or []
        if cf:
            for c in cf:
                print(f"  ⚠ 第{c.get('round')}遍 {c.get('between')}：{c.get('what')}")
        else:
            print(f"  未发现矛盾（{consistency.get('reason', '')}）")
        print()
    service.close()

    # ---- 汇总（计数，不是分数）----
    def _stats(sub: list[dict]) -> dict:
        judged = [x for x in sub if x["classify"].get("ok") is not None]
        return {
            "runs": len(sub),
            "like": sum(1 for x in judged if x["classify"]["ok"]),
            "judged": len(judged),
            "rule_pass": sum(1 for x in sub if x["rule"]["pass"]),
            "dist": _count([x["classify"]["class"] for x in judged]),
        }

    def _count(vals: list) -> dict:
        out: dict = {}
        for v in vals:
            out[str(v)] = out.get(str(v), 0) + 1
        return out

    summary = {g: _stats([x for x in results if x["group"] == g])
               for g, _, _ in groups}
    pw = _count([x.get("pick") for x in pairwise_out])
    ident_judged = [x for x in identify_out if x.get("correct") is not None]
    ident_hits = sum(1 for x in ident_judged if x["correct"])

    report = {
        "ran_at": dt.datetime.now().isoformat(timespec="seconds"),
        "repeat": args.repeat, "model": config.MODEL,
        # 单次测量对 ②③ 不可用：它们比的是**回答**，而回答每次跑都是新生成的
        # （实测同两条探针重跑，②的结论整个翻转）。离线看报告的人也要能看见这件事。
        "single_run": args.repeat <= 1,
        "single_run_note": ("repeat<=1：②强迫选择与③辨识的输入是当场新生成的回答，"
                           "单次结论会摆动，仅供诊断；下结论请用 --repeat 3 起"
                           if args.repeat <= 1 else None),
        "summary": summary, "pairwise": pairwise_out, "identify": identify_out,
        "consistency": consistency, "evolve": None, "runs": results,
    }

    # ---- 演化对照 ----
    if args.evolve:
        print("===== 演化对照 =====")
        service.personality = Personality(path=on_path)
        before = copy.deepcopy(service.personality.data)
        before_runs = _run_group(service, cases, "evo_before", "演化前", args.repeat)
        print("触发一次真实的性格演化（素材见 EVOLVE_MATERIAL）……")
        ok, msg = service.evolve_personality(EVOLVE_MATERIAL)
        after = copy.deepcopy(service.personality.data)
        diff = {k: {"before": before.get(k), "after": after.get(k)}
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)}
        print(f"演化{'成功' if ok else '失败'}：{msg or '（无改动）'}")
        changed = bool(diff)
        base_keys = ("name", "scope", "origin", "family", "daily_life", "cares",
                     "self_attitude")
        base_kept = all(before.get(k) == after.get(k) for k in base_keys)
        if not changed:
            print("⚠ 人设没有变化 → 这次对照说明不了演化质量，换素材再试"
                  "（这时的差异只是同版人设的重复测量噪声）")
        else:
            for k, v in diff.items():
                print(f"  · {k}：{v['before']} → {v['after']}")
            touched = [k for k in diff
                       if k in ("traits", "tone", "values", "behavior", "speech_habits")]
            affected = sorted({c["id"] for c in cases if c.get("dim") in touched})
            if touched and affected:
                print(f"  ⚠ 动了 {'、'.join(touched)}，涉及探针 {'、'.join(affected)}——"
                      f"它们的类别标准是**演化前**写的，前后对比不能直接当成「变好/变坏」")
        after_runs = _run_group(service, cases, "evo_after", "演化后", args.repeat)

        def _like(sub: list[dict]):
            j = [x for x in sub if x["classify"].get("ok") is not None]
            return f"{sum(1 for x in j if x['classify']['ok'])}/{len(j)}" if j else "—"

        print(f"符合「像」的条数：演化前 {_like(before_runs)} → 演化后 {_like(after_runs)}")
        print(f"底座是否守住：{'✓ 逐字不变' if base_kept else '✗ 被改动了！'}")
        evo_consistency = _consistency_by_round(service, after_runs) or {}
        report["evolve"] = {"ok": ok, "msg": msg, "changed": changed, "diff": diff,
                            "base_kept": base_kept,
                            "like_before": _like(before_runs),
                            "like_after": _like(after_runs),
                            "conflicts_after": evo_consistency.get("conflicts") or [],
                            "reason_after": evo_consistency.get("reason", ""),
                            "before_runs": before_runs, "after_runs": after_runs}
        print()

    os.makedirs(RESULT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(RESULT_DIR, f"persona-{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 46)
    for g, _, glabel in groups:
        s = summary[g]
        pct = f"{s['like']}/{s['judged']}" if s["judged"] else "—"
        print(f"{glabel:<10} 「像」{pct}（类别分布 {s['dist']}）｜规则通过 {s['rule_pass']}/{s['runs']}")
    if not args.no_off:
        print(f"② 强迫选择：{pw}")
        print(f"③ 辨识正确：{ident_hits}/{len(ident_judged)}"
              f"（随机基线 1/3 ≈ {len(ident_judged) / 3:.1f}）")
        # ②③ 的输入（回答）**每次跑都是新生成的**，所以单次测量天然不稳——实测同两条
        # 探针隔几分钟重跑，②的结论整个翻转（plain → persona）。必须把这件事说在前面，
        # 否则 9/11 这种数字很容易被人当结论用（#051 / T54）。
        if args.repeat <= 1:
            print("  ⚠ 以上 ②③ 是**单次**测量，而它们的输入（回答）每次都是新生成的"
                  "→ 结论会摆；要下结论请用 --repeat 3 起")
        # 区分度：**无人设组也「像」的探针 = 这条切面区分不出人设**（有没有人设都一样）。
        # 这是实测逼出来的：11 条里只有 4 条真有区分度，其余 7 条切的是"通用好回答特征"
        # （给不给办法、反不反对、简短不简短），于是有人设组的「像 10/11」被高估了。
        # 报告必须把这件事说出来，否则数字会骗人。
        weak = sorted(x["id"] for x in results
                      if x["group"] == "off" and x["classify"].get("ok") is True)
        all_ids = sorted({x["id"] for x in results if x["group"] == "off"})
        strong = [i for i in all_ids if i not in weak]
        print(f"区分度：{len(strong)}/{len(all_ids)} 条切面能区分人设"
              f"（无人设组也「像」的 {len(weak)} 条等于**没测**）")
        if weak:
            print(f"  区分不出的探针：{'、'.join(weak)}")
        print(f"  ⚠ 结论以 ②③ 与「有区分度的那几条」为准，① 的全量「像」会虚高")
    if consistency is not None:
        n = len(consistency.get("conflicts") or [])
        print(f"④ 矛盾检测：{'未发现矛盾' if not n else f'{n} 处矛盾'}")
    print("\n不像的条目（这才是报告主体）：")
    bad = [x for x in results if x["classify"].get("ok") is False]
    if not bad:
        print("  （无）")
    for x in bad:
        want = next((c.get("ok_class") for c in cases if c["id"] == x["id"]), "?")
        print(f"  · [{x['group']}/{x['id']}] 归为 {x['classify'].get('class')}"
              f"（应为 {want}）：{x['classify'].get('reason', '')}")
    print(f"\n详细报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
