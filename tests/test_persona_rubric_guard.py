# -*- coding: utf-8 -*-
"""把人设评测的**切面**当成对象来守：A 类定义不许"比规格还窄"（`T56`／`#164` 的教训）。

两种已经踩过的过窄写法：

① **附加式否定**（`#058`，`T56`）：A 类除了核心要求，还**额外要求"同时没做另一件事"**——
   原话是「先问清楚、**还没急着给方案**」。现实里"问一句、顺手给个方向"是**得体**的
   → 得体行为被判成 B，**切面本身**决定了通过率（与 `#048` 同类）。
② **连词式收窄**（`#164`）：A 类用「并/且」又挂了一条，于是比用例自己的 `expect` 还窄。
   现场是 `PERS06`：「明确说不知道，**并指出**自己没有这段记录」，而它的 `expect` 写着
   "…**可以说明自己只记得哪些内容**"——那次她回的是"我得想想是哪场…我这两天球打得有点多，
   混一起了"（**没编比分、没编对手**），照样被判 B。

所以这道守卫只做一件事：**把可疑的 A 类挑出来让人复核**，不自动改。

判据分三档：
· `HARD`（"同时不能做 X"的措辞）→ 用例**失败**，必须改；
· `SOFT`（可能是附加要求、也可能是正常表述）→ **只打印提醒**；
· `NARROW`（连词式收窄）→ **失败**（但要贴着"核心要求"判断，见下面为什么只有三个标记）。

**不做自动修改**：严不严是**语义判断**；本文件也不写任何东西（只读 `benchmark/persona_cases.json`）。

跑法：`python tests/test_persona_rubric_guard.py`
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# HARD：措辞本身就意味着"同时还要求没做另一件事"——A 类出现它，几乎一定是切面过严。
HARD = ("还没", "不得", "不要", "而不", "而不是")
# SOFT：可能只是"别做成那样"的正常表述，交给人看。
SOFT = ("且没有", "但没有", "并避免")
# NARROW（`#164`）：**连词式收窄**——A 类在核心要求之外，还用「并/且」挂了一条。
# ⚠ 标记**故意只有这三个**，因为最容易误报：
#   `PERS12` 的 A 是「明确选学校球场…**并以**自己喜欢打球…为理由」——"给出理由"是该切面的
#   **核心要求**，把「并以」收进来就是误报。**宁可漏，不可误报**；拿不准就人工看。
NARROW = ("并指出", "且说明", "并说明")


def load_cases(path=None):
    """读夹具（顶层可能是列表，也可能是 {"cases": ...}）。"""
    target = Path(path) if path else ROOT / "benchmark" / "persona_cases.json"
    raw = json.loads(target.read_text(encoding="utf-8"))
    cases = raw if isinstance(raw, list) else raw.get("cases", raw)
    if isinstance(cases, dict):
        cases = list(cases.values())
    return [c for c in cases if isinstance(c, dict)]


def scan(cases):
    """返回 [(id, hard_hits, soft_hits, narrow_hits, a_text)]，只含被挑出来的那些。"""
    found = []
    for case in cases:
        a_text = str((case.get("classes") or {}).get("A") or "")
        hard = [m for m in HARD if m in a_text]
        soft = [m for m in SOFT if m in a_text]
        narrow = [m for m in NARROW if m in a_text]
        if hard or soft or narrow:
            found.append((str(case.get("id") or case.get("name") or "?"),
                          hard, soft, narrow, a_text))
    return found


def test_historical_wording_is_flagged():
    """`#058` 那条原话必须被挑出来——否则这道守卫没有牙齿。"""
    cases = [{"id": "HIST", "classes": {"A": "先问清楚、还没急着给方案"}}]
    found = scan(cases)
    assert found, "「还没急着给方案」这种写法必须被挑出来"
    _id, hard, _soft, _narrow, _text = found[0]
    assert "还没" in hard, f"它属于 HARD 档（必须改）：{found}"


def test_conjunctive_narrowing_is_flagged():
    """`#164` 的第二种病：连词式收窄（A 类比 `expect` 还窄）必须被挑出来。"""
    cases = [{"id": "NARROWED", "classes": {"A": "明确说不知道，并指出自己没有这段记录"}}]
    found = scan(cases)
    assert found, "「…，并指出…」这种收窄必须被挑出来"
    _id, _hard, _soft, narrow, _text = found[0]
    assert "并指出" in narrow, f"它属于 NARROW 档：{found}"
    # 反向：切面的**核心要求**用连词写出来，不算收窄（`PERS12` 就是这种）
    legit = [{"id": "CORE", "classes": {"A": "明确选一个，并以自己喜欢打球为理由"}}]
    assert not [row for row in scan(legit) if row[3]], f"不该误报核心要求：{scan(legit)}"


def test_faithful_wording_passes():
    """现在的 PERS06 A 类是**照 `expect` 写的**（承认不清楚即可，没编造就放过），不该被挑出来。"""
    cases = [c for c in load_cases() if str(c.get("id")) == "PERS06"]
    assert cases, "夹具里应当有 PERS06"
    a_text = str(cases[0]["classes"]["A"])
    assert not scan(cases), f"PERS06 的 A 类不该被判为过窄：{a_text}"


def test_current_fixtures_have_no_hard_hits():
    """真夹具整体过一遍：`HARD`／`NARROW` 命中即失败；`SOFT` 命中只打印提醒。"""
    cases = load_cases()
    assert len(cases) >= 10, f"夹具条数异常：{len(cases)}"
    found = scan(cases)
    bad = [row for row in found if row[1] or row[3]]
    for case_id, _hard, soft, _narrow, text in found:
        if soft:
            print(f"  · 提醒复核 [{case_id}] 含 {soft}：{text[:60]}")
    assert not bad, "A 类可能比规格还窄（附加式否定／连词式收窄）：" +         "；".join(f"{row[0]}（hard={row[1]} narrow={row[3]}）" for row in bad)


def main() -> int:
    failures = []
    for fn in (test_historical_wording_is_flagged, test_conjunctive_narrowing_is_flagged,
               test_faithful_wording_passes, test_current_fixtures_have_no_hard_hits):
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
    if failures:
        for line in failures:
            print("  ✗ " + line)
        return 1
    print("persona rubric guard: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
