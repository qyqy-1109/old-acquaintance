# -*- coding: utf-8 -*-
"""记忆 recall 测评 runner：注入 → 延迟 → 探测 → 自动打分。

跑法（在项目根目录）：
    python benchmark/run_bench.py
    ... --layer recall        只测召回层（不问模型，便宜，用来定位是检索还是生成的问题）
    ... --ids B01,A04         只跑指定用例
    ... --difficulty stress   只跑压力级
    ... --judge               对 judge_required 的用例启用 LLM 裁判
    ... --fresh               先清空本次专用的数据目录

两点决定是刻意做的：
1. 数据写到 benchmark/_bdata，绝不碰根目录的真实记忆；config 在 import 时读 DATA_DIR，
   所以必须先**强制**设好它（`os.environ["DATA_DIR"] = ...`，不是 setdefault），
   并在 import 之后断言一次——外部已导出 DATA_DIR 时 setdefault 不会覆盖（T74）。
2. 每个用例开始前清空 store / world / profile 的全部状态。
   不这么做的话，上一条用例的「滨江」会变成下一条用例的世界书背景，分数就没意义了。
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(HERE, "_bdata")

# `--fresh` 会 rmtree 这个目录（见 `_drop_data_dir`），而 `reset_case` 每例都
# `DELETE FROM memories/messages/summaries/pending/slots`。所以 DATA_DIR **强制**指向
# 跑分专用目录，import 之后再断言一次——**外部若已经导出 DATA_DIR**（Dockerfile 的
# `ENV DATA_DIR=/data`、CI 变量、本机 shell），老写法 `os.environ.setdefault(...)`
# 不会覆盖它，于是清场精确地作用在真实记忆上（T74）。宁可起不来。
os.environ["DATA_DIR"] = DATA_DIR
sys.path.insert(0, ROOT)

from app import config                                    # noqa: E402
from app.models import MemoryStore, WorldBook             # noqa: E402
from app.services.chat_service import ChatService         # noqa: E402
from app.models.user import EMPTY_PROFILE                 # noqa: E402
from app.models.personality import DEFAULT_PERSONALITY    # noqa: E402

if os.path.abspath(config.DATA_DIR) != os.path.abspath(DATA_DIR):
    raise SystemExit(f"✗ 数据目录不对（会清空真实记忆）：config.DATA_DIR={config.DATA_DIR!r}，"
                     f"期望 {DATA_DIR!r}")
if os.path.abspath(DATA_DIR) == os.path.abspath(ROOT):
    raise SystemExit("✗ 跑分目录不能是项目根目录（那正是真实数据所在）")

CASES_FILE = os.path.join(HERE, "cases.json")
RESULT_DIR = os.path.join(HERE, "results")

# 各违规类型的默认豁免词，由 cases.json 的 meta.default_exemptions 填充
DEFAULT_EXEMPT: dict[str, list[str]] = {}

# 稀释用的无关对话：只进 session 历史，不做提炼，便宜且可控
FILLER_POOL = [
    ("今天中午吃啥好呢", "天热就吃点凉面吧，省得在厨房出汗"),
    ("刚看了个综艺笑死我了", "哪个综艺呀，给我也讲讲"),
    ("外面好像要下雨了", "出门记得带伞，别淋着了"),
    ("最近总睡不好", "是不是压力太大了，睡前别刷手机试试"),
    ("楼下超市今天打折", "那要不要去囤点纸巾洗衣液"),
    ("地铁今天人好多", "早高峰嘛，能早点走就早点走"),
    ("同事送了我一盆绿萝", "绿萝好养，放办公室正合适"),
    ("手机又快没电了", "出门带个充电宝吧，或者少开点后台"),
    ("今天开了三个会，累", "会多是真折磨人，中间得起来走走"),
    ("想学着做点家常菜", "先从番茄炒蛋开始呗，翻车了也没人笑你"),
    ("周末要不要出去走走", "天气好的话去公园转转挺好的"),
    ("快递又放快递柜了", "记得及时取，别压着"),
]

JUDGE_PROMPT = """你是记忆测评的裁判。系统刚刚依据它的记忆回答了一个问题，请你判断这个回答是否合格。

【它的回答】
{answer}

【期望它记住的】（必须体现，不能遗漏）
{expect}

【千万不能出现的】（出现即不合格：陈旧=记了被推翻的旧值，混淆=张冠李戴，幻觉=凭空编造）
{forbid}

【规则层的初步判定】（仅供参考）
{machine}

【评分要点】
{rubric}

【怎么判，很重要】
规则层是靠关键词匹配的，很容易因为说法不同而误判——
「黑咖」「花生是雷区」「不额外加糖」这些说法，意思都到位了，只是没用预设的词。
所以：**请按「意思有没有达到」判断，不要因为没出现某个具体词就判不合格。**
反过来，如果意思确实没达到（该提的没提、该守的没守），即使措辞再漂亮也要判不合格。

只输出 JSON：{{"pass": true或false, "reason": "一句话说明理由"}}
"""


# ---------------- 匹配与打分 ----------------

def match_any(text: str, patterns: list[str]) -> bool:
    """alias 命中判定。以 re: 开头的按正则，其余按子串。"""
    for p in patterns:
        if not p:
            continue
        if p.startswith("re:"):
            try:
                if re.search(p[3:], text):
                    return True
            except re.error:
                continue
        elif p in text:
            return True
    return False


def first_match_pos(text: str, patterns: list[str]) -> int:
    """返回第一个命中的位置，-1 表示没命中。违规判定要拿它取邻域。"""
    for p in patterns:
        if not p:
            continue
        try:
            if p.startswith("re:"):
                m = re.search(p[3:], text)
                if m:
                    return m.start()
            else:
                i = text.find(p)
                if i >= 0:
                    return i
        except re.error:
            continue
    return -1


def score_text(case: dict, text: str, default_exempt: dict | None = None) -> dict:
    """按槽位打分。

    关于误判：系统完全可能给出「滨江呀，上个月刚从城西搬过去的」这种
    把旧值当过去时态交代的正确答案。这时候命中 forbid 里的旧值并不是错误。
    所以取命中位置前后 25 字作为邻域，邻域里出现豁免词（以前/改/搬/后来…）
    就判为合法陈述，不算违规。这条会让规则偏松，宁可漏判也不冤枉，
    真正需要严格把关的用例另设 judge_required。
    """
    default_exempt = default_exempt or {}
    expect = case.get("expect", [])
    forbid = case.get("forbid", [])
    hits, misses = [], []
    for e in expect:
        (hits if match_any(text, e.get("aliases", []) or [e.get("must", "")]) else misses).append(e["key"])

    violations = []
    for f in forbid:
        pos = first_match_pos(text, f.get("detect", []))
        if pos < 0:
            continue
        cues = f.get("exempt_if")
        if cues is None:
            cues = default_exempt.get(f.get("kind", "hallucination"), [])
        if cues and match_any(text[max(0, pos - 25): pos + 25], cues):
            continue  # 同一语境里明确标了「过去 / 已改 / 弄错」，是合法陈述
        violations.append({"value": f.get("value", ""), "kind": f.get("kind", "hallucination")})

    n_expect = len(expect)
    return {
        "hits": hits,
        "misses": misses,
        "violations": violations,
        "slot_recall": round(len(hits) / n_expect, 3) if n_expect else 1.0,
        "rule_pass": (not misses) and (not violations),
    }


def judge_case(service: ChatService, case: dict, answer: str,
               missed: list[str] | None = None,
               violated: list[str] | None = None) -> dict:
    expect_txt = "\n".join(f"- {e['key']}：{e['must']}" for e in case.get("expect", [])) or "（无）"
    forbid_txt = "\n".join(f"- [{f.get('kind')}] {f.get('value')}" for f in case.get("forbid", [])) or "（无）"
    # 把规则层的判定一并交给裁判：让它知道哪些词没匹配上，
    # 但明确告诉它「措辞不同不算错」，避免它跟着规则一起误判。
    if missed and violated:
        machine_txt = (f"- 没匹配到的字面线索：{'、'.join(missed)}\n"
                       f"- 触发的禁戒：{'、'.join(violated)}")
    elif missed:
        machine_txt = f"- 没匹配到的字面线索：{'、'.join(missed)}（可能只是说法不同）"
    elif violated:
        machine_txt = f"- 触发的禁戒：{'、'.join(violated)}"
    else:
        machine_txt = "（规则层全部匹配通过）"
    from app.utils.llmjson import chat_json
    prompt = JUDGE_PROMPT.format(
        answer=answer[:1500], expect=expect_txt, forbid=forbid_txt,
        machine=machine_txt,
        rubric=case.get("scoring", {}).get("note", "按常识判断"),
    )
    # 裁判也是后台活：一个分类判断，不需要深度思考。
    # 原来用 config.MODEL（glm-5.3-flashx，始终思考）时，思考会把 800 的额度吃光，
    # 返回空正文 → JSON 解析失败 → 重试也没用（原因是确定性的）。
    # 实测这会直接把一条本该通过的用例判成失败。
    data, err = chat_json(service.client, config.EXTRACT_MODEL,
                          [{"role": "user", "content": prompt}],
                          temperature=0.1, max_tokens=800, retries=2,
                          no_think=bool(config.NO_THINK))
    if data is None:
        return {"pass": None, "reason": f"裁判调用失败：{err}"}
    return {"pass": bool(data.get("pass")), "reason": str(data.get("reason", ""))}


# ---------------- 执行 ----------------

def reset_case(service: ChatService) -> None:
    """把 store / world / profile / 性格 / 情绪 全部抹干净，保证用例之间零污染。

    原则（T43）：**凡是会进注入的落点都必须清**，清完还要自检。
    踩过的坑：这里曾经漏清 `slots`，而 slots 是行为注入的来源，前一条用例的状态
    整块注入了后一条的上下文——还**偶然**给 S05 递了一条提示，把它真正的失败原因
    盖住了（见 #031：污染会掩盖真 bug，而且是静默的）。
    """
    store, world = service.store, service.world
    store.memories, store.pending = [], []
    store.vectors = None
    store.sessions, store.summaries, store.summary_cursor = {}, {}, {}
    # 换了 SQLite 之后，光清内存里的列表不够——库里的行还在，
    # 下次启动会全部读回来，用例之间的隔离就破了。
    # events 有意**不清**：它只用于观测与调用量统计，不参与任何注入。
    with store.db:
        for table in ("memories", "messages", "summaries", "pending", "slots"):
            store.db.execute(f"DELETE FROM {table}")
    store.archived = []
    store._synced = {}
    store._dirty_hits = set()
    store._summarize_retry_at = 0.0      # 上一条用例的摘要退避会把这一条也一起拖住
    world.entries = []
    service.profile.data = dict(EMPTY_PROFILE)
    service._turns_since_profile = 0
    # 性格与情绪同样是**注入来源**：人设模板进 system prompt、情绪轨迹进注入块，
    # 而性格演化（AUTO_EVOLVE_PERSONALITY）会真的改写它们——不清，后一条用例
    # 读到的是前一条的"人格成长"。
    service.personality.data = dict(DEFAULT_PERSONALITY)
    service.personality.history = []
    service._evolved_pos = 0
    service._evolved_turns = 0
    service.emotion.entries = []
    # 磁盘文件也要清：只要有一条用例触发 reload/sync，就会把上一条的世界书、
    # 画像、情绪或性格读回来（内存清空 ≠ 磁盘清空）。
    for path in (config.WORLD_FILE, config.USER_FILE, config.EMOTION_FILE,
                 config.PERSONALITY_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"  ⚠ 清不掉 {os.path.basename(path)}：{e}")

    # 清完自检：**失败要可见**。漏掉任何一个落点，后面所有用例的结论都不可信，
    # 而这种污染是静默的——#031 就是被它骗了一轮。
    leftovers = []
    if store.slots_all(limit=1):
        leftovers.append("slots")
    if store.memories or store.archived or store.sessions:
        leftovers.append("memories/sessions")
    if world.entries:
        leftovers.append("world")
    if service.personality.history:
        leftovers.append("personality.history")
    if service.emotion.entries:
        leftovers.append("emotion")
    if leftovers:
        print(f"  ⚠ reset_case 没清干净：{', '.join(leftovers)}"
              f"（本用例的结论可能被前一条污染）")


def apply_turn(service: ChatService, turn: dict) -> None:
    service.finish_turn(turn["u"], turn["a"])  # 走真实链路：提炼 + embedding + 画像节流


def apply_fillers(service: ChatService, cid: str, n: int) -> None:
    """稀释：直接写 session 历史，不提炼；写完按正常逻辑检查是否触发摘要。"""
    if n <= 0:
        return
    hist = service.store.sessions.setdefault(cid, [])
    for i in range(n):
        u, a = FILLER_POOL[i % len(FILLER_POOL)]
        now = dt.datetime.now().timestamp()
        hist.append({"role": "user", "content": f"（闲聊{i+1}）{u}", "ts": now})
        hist.append({"role": "assistant", "content": a, "ts": now})
    service.store._maybe_summarize()


def run_case(service: ChatService, case: dict, layer: str, use_judge: bool) -> dict:
    cid = case["id"]
    service.store.session = cid
    reset_case(service)
    service.store.sessions[cid] = []

    errors = []
    try:
        for t in case.get("inject", []):
            apply_turn(service, t)
        if case.get("filler_stage") == "after_inject":
            apply_fillers(service, cid, int(case.get("fillers", 0)))
        for t in case.get("distract", []):
            apply_turn(service, t)
        for t in case.get("update", []):
            apply_turn(service, t)
        if case.get("filler_stage", "before_probe") == "before_probe":
            apply_fillers(service, cid, int(case.get("fillers", 0)))
    except Exception as exc:
        errors.append(f"注入阶段失败：{type(exc).__name__}: {exc}")

    # 记忆系统整体失灵检测。
    # 踩过的坑：一处 NameError 让 add_turn 每次都抛异常，被上面的 except 吞掉，
    # 结果所有记忆写入全废，而评测照样跑完出分数——只是每条用例的「召回」都是空的。
    # 这种静默失灵比某条用例失败严重得多，必须响亮地标出来，
    # 否则会把「记忆系统死了」误读成「这个模型记性差」。
    n_inject = len(case.get("inject", [])) + len(case.get("update", [])) + len(case.get("distract", []))
    if n_inject and not service.store.memories:
        errors.append(f"记忆系统疑似失灵：注入 {n_inject} 轮后库里 0 条记忆，"
                      f"本次结果不可信")

    result = {"id": cid, "difficulty": case["difficulty"], "scene": case["scene"],
              "type": case["type"], "layer": layer, "errors": errors}

    try:
        messages, recalled = service.prepare(case["probe"])
        if layer == "recall":
            text = "\n".join(h["text"] for h in recalled)
            result["recalled"] = [{"sim": round(h["sim"], 3), "kind": h["kind"], "text": h["text"]}
                                  for h in recalled]
        else:
            text = service.ask(messages)
            result["recalled"] = [{"sim": round(h["sim"], 3), "kind": h["kind"],
                                   "text": h["text"][:60]} for h in recalled]
        result["answer"] = text[:1200]
        # 推理模型会把 max_tokens 花在思考上，正文变空。这跟「记不记得住」是两回事，
        # 单独标出来，别算进记忆能力里，否则会误判成严重缺陷。
        result["empty_answer"] = not text.strip()
        if result["empty_answer"]:
            result["errors"].append(
                "回答为空：thinking token 吃掉了 max_tokens，调大 MAX_TOKENS 或换非推理模型后重测")
    except Exception as exc:
        result["errors"].append(f"探测阶段失败：{type(exc).__name__}: {exc}")
        result.update(score_text(case, ""), case_pass=False, judge=None)
        return result

    result.update(score_text(case, text, DEFAULT_EXEMPT))

    # 规则层判失败时也让 judge 兜一次底。
    # 原因：规则层靠关键词，漏词太常见（「黑咖」「花生是雷区」意思到位了但不在词表里），
    # 光靠规则判会把大量合格回答误判成失败。judge 判的是「意思有没有达到」。
    # 只在规则失败时才跑，规则通过的用例不用多花这一笔。
    need_judge = (case.get("scoring", {}).get("judge_required", False)
                  or not result["rule_pass"])
    if need_judge and use_judge:
        result["judge"] = judge_case(
            service, case, text, result.get("misses", []),
            [v["value"] for v in result.get("violations", [])])
        jp = result["judge"]["pass"]
        # 这类用例考的是「把握」而非字面（是否真知道自己忘了、是否真的放宽了约束），
        # 关键词规则判不准，所以 judge 有结论时以 judge 为准，规则分只留作参考。
        result["judge_override"] = bool(jp is not None and jp != result["rule_pass"])
        result["case_pass"] = result["rule_pass"] if jp is None else jp
    elif need_judge:
        result["judge"] = None
        result["case_pass"] = result["rule_pass"]
        result["judge_pending"] = True  # 未开 judge，规则分仅供参考
    else:
        result["judge"] = None
        result["case_pass"] = result["rule_pass"]
    return result


def aggregate(results: list[dict]) -> dict:
    total_expect = total_hit = total_viol = 0
    kinds, per_level = {}, {}
    for r in results:
        n_expect = len(r["hits"]) + len(r["misses"])
        n_hit = len(r["hits"])
        total_expect += n_expect
        total_hit += n_hit
        total_viol += len(r["violations"])
        if n_expect > n_hit:
            kinds["omission"] = kinds.get("omission", 0) + (n_expect - n_hit)
        for v in r["violations"]:
            kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
        lv = per_level.setdefault(r["difficulty"], {"total": 0, "pass": 0, "expect": 0, "hit": 0, "viol": 0})
        lv["total"] += 1
        lv["pass"] += 1 if r["case_pass"] else 0
        lv["expect"] += n_expect
        lv["hit"] += n_hit
        lv["viol"] += len(r["violations"])

    def rate(level):
        return {
            "cases": level["total"],
            "case_success_rate": round(level["pass"] / level["total"], 3) if level["total"] else 0,
            "slot_recall": round(level["hit"] / level["expect"], 3) if level["expect"] else 0,
            "hallucination_rate": round(level["viol"] / (level["hit"] + level["viol"]), 3)
                                  if (level["hit"] + level["viol"]) else 0,
        }

    return {
        "total_cases": len(results),
        "case_success_rate": round(sum(1 for r in results if r["case_pass"]) / len(results), 3) if results else 0,
        "slot_recall": round(total_hit / total_expect, 3) if total_expect else 0,
        "hallucination_rate": round(total_viol / (total_hit + total_viol), 3) if (total_hit + total_viol) else 0,
        "slots_expected": total_expect,
        "slots_hit": total_hit,
        "violations": total_viol,
        "empty_answers": sum(1 for r in results if r.get("empty_answer")),
        "error_mix": kinds,
        "by_difficulty": {k: rate(v) for k, v in per_level.items()},
    }


def rescore(report_path: str, out_path: str) -> int:
    """用当前规则对已保存的回答重新打分。

    调规则最贵的地方是每改一次就得重跑一遍全流程（一百多次 API）。
    回答本来就存在报告里，所以规则调优阶段可以离线重评，零成本。
    """
    data = json.load(open(CASES_FILE, encoding="utf-8"))
    DEFAULT_EXEMPT.update(data.get("meta", {}).get("default_exemptions", {}))
    by_id = {c["id"]: c for c in data["cases"]}

    report = json.load(open(report_path, encoding="utf-8"))
    missing = sorted({r["id"] for r in report["cases"] if r["id"] not in by_id})
    if missing:
        print("无法重评：当前 cases.json 缺少报告用例：" + "、".join(missing),
              file=sys.stderr)
        return 2
    results = []
    for r in report["cases"]:
        case = by_id.get(r["id"])
        if case is None:
            continue
        text = r.get("answer", "")
        res = dict(r)
        res.update(score_text(case, text, DEFAULT_EXEMPT))
        need_judge = case.get("scoring", {}).get("judge_required", False)
        judge = r.get("judge")
        # 只要有裁判结论就以裁判为准——现在规则层判失败时也会跑裁判，
        # 所以裁判不再只服务于 judge_required 的用例。
        if isinstance(judge, dict) and judge.get("pass") is not None:
            res["case_pass"] = bool(judge["pass"])
            res["judge_override"] = bool(judge["pass"]) != res["rule_pass"]
        elif need_judge:
            res["case_pass"] = res["rule_pass"]
            res["judge_pending"] = True
        else:
            res["case_pass"] = res["rule_pass"]
        results.append(res)

    summary = aggregate(results)
    report["summary"] = summary
    report["cases"] = results
    report["rescored_at"] = dt.datetime.now().isoformat(timespec="seconds")
    out = out_path or report_path.replace(".json", "-rescored.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"重评 {len(results)} 条（沿用已保存的回答，未调用 API）")
    print(f"用例正确率 CSR : {summary['case_success_rate']:.1%}")
    print(f"完整率 recall  : {summary['slot_recall']:.1%}  ({summary['slots_hit']}/{summary['slots_expected']})")
    print(f"幻觉率         : {summary['hallucination_rate']:.1%}  (违规 {summary['violations']} 次)")
    print("错误构成       :", summary["error_mix"] or "无")
    # 裁判跟规则层不一致的条数。这个数越大，说明规则层越不可靠——
    # 别名漏词是常态，所以不要只看规则层的判定。
    ov = sum(1 for r in results if r.get("judge_override"))
    if ov:
        print(f"裁判与规则不一致：{ov} 条 "
              f"（其中救回 {sum(1 for r in results if r.get('judge_override') and r['case_pass'])} 条）")
    dead = [r["id"] for r in results
            if any("记忆系统疑似失灵" in e for e in r.get("errors", []))]
    if dead:
        print(f"⚠️ 记忆系统失灵：{len(dead)} 条（{','.join(dead[:6])}…）本次分数不可信，"
              f"先查 add_turn / _extract 是不是在抛异常")
    for lv, s in summary["by_difficulty"].items():
        print(f"  {lv:<9} CSR {s['case_success_rate']:.0%} | 完整率 {s['slot_recall']:.0%} | 幻觉率 {s['hallucination_rate']:.0%}")
    print(f"\n报告：{out}")
    return 0


def _drop_data_dir(path: str) -> None:
    """清空本次专用的数据目录。**必须先把日志文件句柄关掉。**

    踩过（T39）：`import app` 时 `app/log.py` 的 `TimedRotatingFileHandler` 已经把
    `_bdata/logs/app.log` 打开了，而 Windows 删不掉被占用的文件——`--fresh` 会直接
    `PermissionError: [WinError 32]` 崩掉，把整个评测挡在门外。
    """
    import logging

    for name in ("app", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    h.close()
                except Exception:
                    pass
                lg.removeHandler(h)
    shutil.rmtree(path, ignore_errors=True)
    # 删不干净必须**说出来**：残留的库里可能带着上一轮的「滨江」，
    # 那会让分数变成假的（这正是 --fresh 存在的理由）。
    if os.path.isdir(path) and os.listdir(path):
        print(f"⚠ 数据目录没清干净，仍有：{os.listdir(path)}——分数可能被残留污染")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rescore", default="", help="对已有报告用当前规则重新打分，不调 API")
    ap.add_argument("--layer", choices=["answer", "recall"], default="answer")
    ap.add_argument("--ids", default="", help="逗号分隔，只跑指定用例")
    ap.add_argument("--difficulty", default="", choices=["", "basic", "advanced", "stress"])
    ap.add_argument("--judge", action="store_true", help="对 judge_required 的用例启用 LLM 裁判")
    ap.add_argument("--fresh", action="store_true", help="清空本次数据目录后再跑")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每条用例重复跑 N 遍：单跑时某条恰好翻车就能带偏总分（#029 差 1 条用例），"
                         "要判断'某改动是否真退化'必须看重复跑（T42）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.rescore:
        return rescore(args.rescore, args.out)

    if args.fresh and os.path.isdir(DATA_DIR):
        _drop_data_dir(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)

    data = json.load(open(CASES_FILE, encoding="utf-8"))
    DEFAULT_EXEMPT.update(data.get("meta", {}).get("default_exemptions", {}))
    cases = data["cases"]
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in want]
    if args.difficulty:
        cases = [c for c in cases if c["difficulty"] == args.difficulty]
    if not cases:
        print("没有匹配的用例")
        return 1

    print(f"用例 {len(cases)} 条 | 探针层：{args.layer} | judge：{'开' if args.judge else '关'}")
    print(f"数据目录：{DATA_DIR}")
    print("固定参数（可复现）："
          f" model={config.MODEL} window={config.WINDOW} top_k={config.TOP_K} "
          f"threshold={config.RECALL_THRESHOLD} extract_every={config.EXTRACT_EVERY} "
          f"auto_extract={config.AUTO_EXTRACT} half_life={config.HALF_LIFE_DAYS}\n")

    service = ChatService()
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']} {case['scene']}/{case['type']} … ", end="", flush=True)
        round_flags = []
        for _ in range(args.repeat):
            r = run_case(service, case, args.layer, args.judge)
            results.append(r)
            round_flags.append(r["case_pass"])
            flag = "通过" if r["case_pass"] else "失败"
            detail = ""
            if r["misses"]:
                detail += f" 漏:{','.join(r['misses'])}"
            for v in r["violations"]:
                detail += f" 违[{v['kind']}]:{v['value']}"
            if r.get("judge_pending"):
                detail += " (待 judge)"
            if r.get("judge_override"):
                # 裁判跟规则层结论不一致。多数时候是规则漏词、裁判认为意思到位了。
                # 显示出来是为了能一眼看出「规则层有多不可靠」，而不是把分歧藏起来。
                detail += f" [裁判{'救回' if r['case_pass'] else '推翻'}]"
            if args.repeat > 1:      # 多遍时每遍各占一行，抖动直接看得见
                print(f"\n    {flag}{detail}", end="")
            else:
                print(flag + detail, end="")
        if args.repeat > 1:
            ok = sum(round_flags)
            mark = "" if ok in (0, args.repeat) else "   ← 不稳定"
            print(f"\n    小计：{ok}/{args.repeat} 遍通过{mark}")
        else:
            print()
    service.close()

    summary = aggregate(results)
    if args.repeat > 1:
        # 多遍跑的意义（T42）：单跑的总分会被"某条用例恰好翻车"带偏——#029 两组差的就是
        # 1 条用例。所以除"每遍当独立样本"的既有口径外，再给一份**按用例先算通过率再平均**
        # 的口径，并把不稳定用例单独列出来：结论要能落在"这条稳不稳"上。
        by_case: dict[str, list] = {}
        for r in results:
            by_case.setdefault(r["id"], []).append(r["case_pass"])
        per_case = {cid: sum(v) / len(v) for cid, v in by_case.items()}
        summary["repeat"] = args.repeat
        summary["per_case"] = {
            "case_success_rate": round(sum(per_case.values()) / len(per_case), 3),
            "unstable_cases": {cid: f"{sum(v)}/{len(v)}" for cid, v in by_case.items()
                               if 0 < sum(v) < len(v)},
        }
    report = {
        "ran_at": dt.datetime.now().isoformat(timespec="seconds"),
        "layer": args.layer,
        "judge": args.judge,
        "repeat": args.repeat,
        "config_snapshot": {
            "model": config.MODEL, "window": config.WINDOW, "top_k": config.TOP_K,
            "top_k_world": config.TOP_K_WORLD, "threshold": config.RECALL_THRESHOLD,
            "auto_extract": config.AUTO_EXTRACT, "extract_every": config.EXTRACT_EVERY,
            "max_memories": config.MAX_MEMORIES, "half_life_days": config.HALF_LIFE_DAYS,
            "summarize_every": config.SUMMARIZE_EVERY,
        },
        "summary": summary,
        "cases": results,
    }

    os.makedirs(RESULT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(RESULT_DIR, f"{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 46)
    if summary.get("repeat"):
        print(f"用例正确率 CSR : {summary['case_success_rate']:.1%}  "
              f"({len(results)} 次运行 = {len(cases)} 条 × {summary['repeat']} 遍)")
        print(f"       按用例 : {summary['per_case']['case_success_rate']:.1%}  "
              f"（每条先算通过率、再在用例间平均）")
        unstable = summary["per_case"]["unstable_cases"]
        print("不稳定用例     :", "、".join(f"{k} {v}" for k, v in unstable.items())
              if unstable else "无（每条重复跑的结果都一致）")
    else:
        print(f"用例正确率 CSR : {summary['case_success_rate']:.1%}  ({len(results)} 条)")
    print(f"完整率 recall  : {summary['slot_recall']:.1%}  ({summary['slots_hit']}/{summary['slots_expected']} 槽位)")
    print(f"幻觉率         : {summary['hallucination_rate']:.1%}  (违规 {summary['violations']} 次)")
    print("错误构成       :", summary["error_mix"] or "无")
    if summary.get("empty_answers"):
        print(f"空回答         : {summary['empty_answers']} 条（非记忆问题，属推理模型 token 预算不足）")
    for lv, s in summary["by_difficulty"].items():
        print(f"  {lv:<9} CSR {s['case_success_rate']:.0%} | 完整率 {s['slot_recall']:.0%} | 幻觉率 {s['hallucination_rate']:.0%} | {s['cases']} 条")
    print(f"\n详细报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
