# -*- coding: utf-8 -*-
"""实验：注入几条「反馈学来的倾向」最合适，以及**措辞**该怎么写？

给 `③ opinion with confidence` 定两个参数：注入条数 N 与注入块的措辞。

第一轮（N=0/2/3/4，112 次调用）的结论：**N 不是主变量**——
  - 成本几乎不随 N 变（N=2 约 92 token/轮、N=3 109、N=4 123）；
  - 但注入后出现一处**稳定的人格损伤**：探针 P2（"你说我该不该…"）
    在 N=0 时 2/2 明确表态，N≥2 时 4/6 变成反问"你先给我几句背景"、
    甚至"你这问题我先不接"。N=2 就已经出现，N=4 也没更糟。
  → 所以第二轮把**措辞**做成变量，在固定 N=3 下比较（`--wording`）。

三条要看的曲线：
  1. **遵守率**——该照做的倾向有没有落地（选小了等于没做）；
  2. **误用 / 系统腔**——把无关倾向硬套、或变成复述规则的系统腔；
  3. **代价**——注入块字符数，以及该表态/该给办法时有没有被压住（`expect_ok`）。

判定交给**独立裁判**（仓库纪律：不靠出题人自己读）。零成本的部分（字符数、分点符号）用规则算。

⚠ 两个踩过的工具坑（都在第一轮暴露，已修）：
  - 裁判必须只看到**实际注入的**倾向，否则它会报"误用了根本没注入的偏好"；
  - P3/P6 是"用户明确要求优先于偏好"的**冲突探针**，不能算进遵守率，
    否则"正确地让位于用户要求"会被记成"没做到"。

倾向集与探针都是**合成**的：shape 与真实反馈一致，但**不使用用户私密备注原文**。

用法：
    python benchmark/run_opinions.py --n 0,2,3,4 --repeat 2                 # 比较条数
    python benchmark/run_opinions.py --n 3 --wording w1,w2,w3 --only P1,P2,P4,P5 --repeat 2
数据落在 `benchmark/_pdata`（gitignored），**不碰真实数据目录**。
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(HERE, "_pdata")

# 这个脚本每轮都会 `DELETE FROM memories/messages/slots`（跟 run_persona 一样清场）。
# 所以 DATA_DIR **强制**指向跑分专用目录，而且 import 之后再断言一次——
# 一旦落到真实数据目录，跑一次实验就把用户的记忆清空了。宁可起不来。
os.environ["DATA_DIR"] = DATA_DIR                    # 必须在 import app 之前
sys.path.insert(0, ROOT)

from app import config                                       # noqa: E402
from app.services.chat_service import ChatService            # noqa: E402
from app.utils.llmjson import chat_json                      # noqa: E402

if os.path.abspath(config.DATA_DIR) != os.path.abspath(DATA_DIR):
    raise SystemExit(f"✗ 数据目录不对（会清空真实记忆）：config.DATA_DIR={config.DATA_DIR!r}，"
                     f"期望 {DATA_DIR!r}")

RESULT_DIR = os.path.join(HERE, "results")

# ---- 合成的倾向集（按置信度降序取前 N 条；同分按 id 顺序，与生产一致）----------
OPINIONS = [
    {"id": "O1", "confidence": 0.875, "text": "他问「该不该」的时候，给出明确态度", "relevant": ["P2"]},
    {"id": "O2", "confidence": 0.75, "text": "他说烦心事时先接住情绪，别马上给办法", "relevant": ["P1", "P7"]},
    {"id": "O3", "confidence": 0.75, "text": "用说话的方式回答，不要写成列表或分点", "relevant": []},
    {"id": "O4", "confidence": 0.5, "text": "别追问太多细节，问一句就够", "relevant": ["P1"]},
    {"id": "O5", "confidence": 0.5, "text": "别用「你说得对」这类肯定套话", "relevant": ["P4"]},
    {"id": "O6", "confidence": 0.5, "text": "收尾自然停下，不要说成总结陈词", "relevant": ["P2", "P4"]},
]

# ---- 探针：`relevant` 列出的倾向在这一轮**该被照做**；`conflict=True` 表示
#      "用户明确要求优先于偏好"，只按 expect_ok 判，不计入遵守率 --------------------
CASES = [
    {"id": "P1", "u": "我最近工作压力特别大，感觉快扛不住了。",
     "expect": "先接住情绪，不急着给一整套办法"},
    {"id": "P2", "u": "我妈老催我结婚，你说我该不该跟她把话说开？",
     "expect": "**明确表态**（该/不该/什么条件下该），不要反问、不要「你先说说」"},
    {"id": "P3", "u": "帮我列个周末要做的事，分条写清楚。", "conflict": True,
     "expect": "**按要求分条写**（用户明确要求优先于任何说话偏好）"},
    {"id": "P4", "u": "我是不是挺失败的。",
     "expect": "像邻居那样接住，不套话、不复述规则"},
    {"id": "P5", "u": "今天天气怎么样？",
     "expect": "正常答天气（这一轮任何倾向都不适用）"},
    {"id": "P6", "u": "怎么才能睡得好？给我点具体办法。", "conflict": True,
     "expect": "**给具体办法**（他直接求办法，别被「别马上给办法」压住）"},
    {"id": "P7", "u": "我最近老熬夜，白天整个人是懵的。",
     "expect": "关心 + 可以给建议（这是她人设里「热心」的地方，倾向不该压掉它）"},
]

JUDGE_PROMPT = """你是独立评审。只按下面的口径判断，不要替我改写回答。

【这一轮**注入**给她的说话偏好】（只有这些存在，别判断没列出来的）
{opinions}

【这一轮**相关**的偏好】（只有这些需要照做，其余这一轮不适用）
{relevant}

【用户这一轮说的话】
{probe}

【她的回答】
{answer}

【这一轮的合理期待】
{expect}

只输出 JSON：
{{
 "honored": [上面「相关偏好」里她确实照做的编号],
 "violated": [上面「相关偏好」里她明显违背的编号],
 "misapplied": [**不相关**的偏好却被她硬套进这一轮的编号],
 "system_tone": 她是否变成复述规则/系统腔（true/false）,
 "expect_ok": 这一轮是否满足上面「合理期待」（true/false）,
 "note": "一句话理由"
}}

口径（严格照这个来，别放宽）：
- honored / violated **只能**从「相关偏好」里选；碰巧没提到不算 honored。
- 用户明确要求（如"帮我分条写"）**优先于**任何偏好；照用户要求做，不算违反"不要分点"。
- 无关话题上提到某条偏好，算 misapplied。
- **"相关"指的是话题相关**，不是"她这一轮碰巧没违反"。
"""

# ---- 三种措辞：变量是"框法"，内容（倾向本身）完全一样 -------------------------
WORDINGS = {
    # w1：第一轮用的原版（否定式 + 谨慎尾巴）
    "w1": "【他在意的】下面这些是他反馈过的、关于**说话方式**的偏好。它们不是硬性约束，"
          "相关的时候照做就行：\n{items}\n照做时**不要点破来源**（别说「我记录到你讨厌…」这类话），"
          "也不要为了照做而变得不像自己。",
    # w2：去掉谨慎尾巴、改成正向框法
    "w2": "【他更喜欢这样】这是他从过去的反馈里提到过的、关于你**说话方式**的偏好：\n{items}\n"
          "相关的时候就这么说；跟这一轮无关时不用管它。",
    # w3：最简陈述，不带任何"要不要照做"的元讨论
    "w3": "【关于说话方式的偏好】\n{items}",
}


def render_block(opinions: list[dict], wording: str = "w1") -> str:
    """注入块（措辞是变量；低置信度用弱措辞，≥0.75 用确定措辞）。

    置信度公式是 `1-0.5^独立命中次数`：1 次命中 0.5（弱措辞）、2 次 0.75。
    """
    if not opinions:
        return ""
    items = []
    for o in opinions:
        prefix = "他不喜欢" if o["confidence"] >= 0.75 else "他好像更希望"
        items.append(f"- {prefix}：{o['text']}")
    return WORDINGS[wording].format(items="\n".join(items))


def top(n: int) -> list[dict]:
    return sorted(OPINIONS, key=lambda o: -o["confidence"])[:n]


def _clear(service: ChatService) -> None:
    """每次测量都从干净状态开始（同 run_persona 的做法；数据在 _pdata 里）。"""
    store, world = service.store, service.world
    store.sessions, store.summaries, store.summary_cursor = {}, {}, {}
    store.memories, store.archived, store.pending, store.vectors = [], [], [], None
    with store.db:
        for table in ("memories", "messages", "summaries", "pending", "slots"):
            store.db.execute(f"DELETE FROM {table}")
    store._synced, store._dirty_hits = {}, set()
    world.entries = []
    service.emotion.entries = []
    service.profile.data = {}


def ask(service: ChatService, block: str, user: str) -> str:
    """真实人设 + 可选注入块 —— 注入位置与将来生产一致（system 消息，紧随人设之后）。"""
    msgs = [{"role": "system", "content": service.system_prompt()}]
    if block:
        msgs.append({"role": "system", "content": block})
    msgs.append({"role": "user", "content": user})
    return service.ask(msgs)


def judge(service: ChatService, injected: list[dict], case: dict, answer: str,
          relevant: list[str]) -> dict:
    listing = ("\n".join(f"{o['id']}. {o['text']}" for o in injected)
               if injected else "（这一轮没有注入任何偏好）")
    rel = "\n".join(f"{oid}. " + next(o["text"] for o in OPINIONS if o["id"] == oid)
                    for oid in relevant) or "（这一轮没有任何偏好适用）"
    prompt = JUDGE_PROMPT.format(opinions=listing, relevant=rel, probe=case["u"],
                                 answer=answer, expect=case["expect"])
    data, err = chat_json(service.client, config.EXTRACT_MODEL,
                          [{"role": "user", "content": prompt}],
                          temperature=0.1, max_tokens=500, retries=2,
                          no_think=bool(config.NO_THINK))
    if not isinstance(data, dict):
        return {"error": str(err)[:120]}
    return data


def surface(answer: str) -> dict:
    """零成本的可观测量。"""
    return {"chars": len(answer),
            "bullets": len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.、])", answer)),
            "says_youre_right": "你说得对" in answer}


def summarize(rows: list[dict], keys: list[str], label_of) -> None:
    print(f"\n{'键':>4} | {'块字数':>5} | {'期待满足':>8} | {'遵守(不计冲突)':>12} | {'违背':>4} | "
          f"{'误用':>4} | {'系统腔':>5} | {'平均字数':>8} | {'冲突探针':>8}")
    for k in keys:
        sub = [r for r in rows if r["key"] == k]
        plain = [r for r in sub if not r["conflict"]]
        want = sum(len(r["relevant"]) for r in plain)
        got = sum(len(set(r["judge"].get("honored") or []) & set(r["relevant"])) for r in plain)
        conf = [r for r in sub if r["conflict"]]
        print(f"{label_of(k):>4} | {sub[0]['block_chars']:>5} | "
              f"{sum(1 for r in sub if r['judge'].get('expect_ok')):>3}/{len(sub):<4} | "
              f"{got:>5}/{want:<6} | {sum(len(r['judge'].get('violated') or []) for r in plain):>4} | "
              f"{sum(len(r['judge'].get('misapplied') or []) for r in sub):>4} | "
              f"{sum(1 for r in sub if r['judge'].get('system_tone')):>5} | "
              f"{sum(r['surface']['chars'] for r in sub) // max(1, len(sub)):>8} | "
              f"{sum(1 for r in conf if r['judge'].get('expect_ok'))}/{len(conf)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", default="0,2,3,4", help="要比较的注入条数，逗号分隔")
    ap.add_argument("--wording", default="w1", help="措辞变量：w1/w2/w3，逗号分隔")
    ap.add_argument("--select", default="top", choices=["top", "relevant"],
                    help="top=按置信度取前 N 条；relevant=只注入与该话题相关的（oracle 上界）")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--only", default="", help="只用这几条探针，逗号分隔")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ns = [int(x) for x in args.n.split(",") if x.strip()]
    wordings = [w for w in args.wording.split(",") if w.strip()]
    for w in wordings:
        if w not in WORDINGS:
            raise SystemExit(f"✗ 未知措辞 {w}，可选 {list(WORDINGS)}")
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    service = ChatService()
    rows = []
    print(f"探针 {len(cases)} 条 × N {ns} × 措辞 {wordings} × 选择 {args.select} × repeat {args.repeat}")
    for w in wordings:
        for n in (ns if args.select == "top" else [0]):
            print(f"\n===== 措辞 {w} / 选择 {args.select} / N={n} =====")
            for case in cases:
                # 选择策略：按置信度取前 N（生产拟采用）vs 只取与这一轮话题相关的（上界）
                picks = (top(n) if args.select == "top"
                         else [o for o in OPINIONS if case["id"] in o["relevant"]])
                block = render_block(picks, w)
                key = f"{w}|{n}" if args.select == "top" else f"{w}|relev"
                rel = [o["id"] for o in picks if case["id"] in o["relevant"]]
                if not picks:
                    print(f"  [{case['id']}] （这一轮不注入任何倾向）")
                for r in range(args.repeat):
                    _clear(service)
                    ans = ask(service, block, case["u"])
                    jd = judge(service, picks, case, ans, rel)
                    rows.append({"key": key, "wording": w, "select": args.select, "n": len(picks),
                                 "case": case["id"],
                                 "conflict": bool(case.get("conflict")), "round": r + 1,
                                 "relevant": rel, "block_chars": len(block),
                                 "answer": ans, "judge": jd, "surface": surface(ans)})
                    mark = "✓" if jd.get("expect_ok") else "✗"
                    extra = f" 误用{jd.get('misapplied')}" if jd.get("misapplied") else ""
                    tone = " 系统腔!" if jd.get("system_tone") else ""
                    print(f"  [{case['id']}] r{r+1} {mark} 遵守{jd.get('honored') or []} "
                          f"违背{jd.get('violated') or []}{extra}{tone}"
                          + (f"  ({jd.get('error')})" if jd.get("error") else ""))
                    time.sleep(0.2)

    os.makedirs(RESULT_DIR, exist_ok=True)
    out = args.out or os.path.join(RESULT_DIR, f"opinions_{int(time.time())}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"opinions": OPINIONS, "cases": CASES, "wordings": WORDINGS,
                   "rows": rows}, f, ensure_ascii=False, indent=1)

    keys = ([f"{w}|{n}" for w in wordings for n in ns] if args.select == "top"
            else [f"{w}|relev" for w in wordings])
    summarize(rows, keys, lambda k: k)
    print(f"\n原始结果：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
