# -*- coding: utf-8 -*-
"""纯函数单元测试：不联网、不调模型、不需要真实数据。

跑法（不需要 pytest，这个项目刻意保持零测试依赖）：
    python tests/test_pure.py

覆盖的是「算错了会静默错下去」的那几处——日期解析、时距标注、字段迁移。
这些地方出错不会报错，只会让记忆悄悄变错，所以最值得测。
"""

import datetime as dt
import json
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印 ✓/✗ 会抛 UnicodeEncodeError —— 断言其实全过了，
# 返回码却是 1，看起来像"测试挂了"。这里把标准输出/错误**原地**切到 UTF-8
# （不替换对象，所以 app/log.py 里已经装好的 StreamHandler 也一并跟着变，
# 中文日志不会再打成乱码）。换环境时 reconfigure 可能不存在，整段兜住即可。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app import config
from app.utils import clock, tts                                          # noqa: E402

NOW = dt.datetime(2026, 9, 23, 20, 0)      # 2026-09-23 周三 晚上 8 点
FAILS: list[str] = []


def eq(got, want, label):
    if got != want:
        FAILS.append(f"{label}\n      得到：{got!r}\n      期望：{want!r}")


# ---------------------------------------------------------------- find_date
def test_find_date():
    d = lambda s: clock.find_date(s, NOW)                            # noqa: E731
    eq(d("9 月 24 日早上要去医院"), dt.date(2026, 9, 24), "月日写法")
    eq(d("9月24日"), dt.date(2026, 9, 24), "月日无空格")
    eq(d("2026-09-24"), dt.date(2026, 9, 24), "ISO 写法")
    eq(d("明天早上"), dt.date(2026, 9, 24), "明天")
    eq(d("后天"), dt.date(2026, 9, 25), "后天")
    eq(d("今天下午"), dt.date(2026, 9, 23), "今天")
    eq(d("昨天"), dt.date(2026, 9, 22), "昨天")
    eq(d("我养了只猫"), None, "没有日期 → None")
    # 只说「月日」而不带年时，应该落在当年；不给年份信息不猜别的年
    eq(d("12 月 31 日"), dt.date(2026, 12, 31), "跨月不跨年")
    # 相对词兜底：万一提炼器漏了换算，条目里留着「明天」也不能对时间系统隐形
    eq(d("大后天"), dt.date(2026, 9, 26), "大后天（别被「后天」抢先匹配）")
    eq(d("前天"), dt.date(2026, 9, 21), "前天")
    # 绝对日期优先于相对词（同一句里两者都有时以绝对为准）
    eq(d("9 月 30 日，也就是后天"), dt.date(2026, 9, 30), "绝对日期优先")


# ----------------------------------------------------------- describe_when
def test_describe_when():
    f = lambda s: clock.describe_when(s, NOW)                        # noqa: E731
    eq(f("9 月 24 日要去医院"), "9 月 24 日要去医院（明天）", "明天标注")
    eq(f("9 月 23 日要去医院"), "9 月 23 日要去医院（就是今天）", "今天标注")
    eq(f("9 月 25 日要去医院"), "9 月 25 日要去医院（后天）", "后天标注")
    eq(f("9 月 26 日要去医院"), "9 月 26 日要去医院（还有 3 天）", "未来 N 天")
    eq(f("9 月 22 日去了医院"), "9 月 22 日去了医院（昨天）", "昨天标注")
    eq(f("9 月 20 日去了医院"), "9 月 20 日去了医院（已经过去 3 天）", "过去 N 天")
    # 作息不是「某一天的事」，加时距反而误导
    eq(f("用户每天早上七点跑步"), "用户每天早上七点跑步", "作息不加标注")
    eq(f("我养了只猫"), "我养了只猫", "没有日期原样返回")
    # 过期约定要单独说白：光有时距，模型仍可能当成还没发生
    got = f("用户和助手约定 9 月 20 日去广场")
    eq("过期" in got, True, "过期约定要带「已过期」")
    # 历史事件不该被加上「约定过期」那句噪音
    got2 = f("用户 9 月 20 日去了医院")
    eq("过期" in got2, False, "历史事件不加「约定过期」")


# --------------------------------------------------------------- now_text
def test_now_text():
    t = clock.now_text(NOW)
    eq("2026年9月23日" in t, True, "now_text 含年月日")
    eq("周三" in t, True, "now_text 含星期（推「下周三」靠它）")
    eq("20:00" in t or "20:" in t, True, "now_text 含时间")


# ------------------------------------------------------------ 字段迁移
def test_personality_migrate():
    from app.models.personality import Personality

    legacy = {"name": "小助手", "role": "旧字段 role", "personality": ["热心"],
              "rules": ["先给结论"], "tone": "轻松"}
    out = Personality._migrate(legacy)
    eq(out.get("identity"), "旧字段 role", "role → identity")
    eq(out.get("traits"), ["热心"], "personality → traits")
    eq(out.get("behavior"), ["先给结论"], "rules → behavior")
    # 迁移不能把没提到的字段弄丢
    eq(out.get("name"), "小助手", "原字段保留")
    eq(isinstance(out.get("scope"), dict), True, "缺的字段从默认模板补齐")


# ------------------------------------------------------------ 时刻级解析
def test_find_datetime():
    f = lambda s: clock.find_datetime(s, NOW)                        # noqa: E731
    eq(f("晚上九点半也没有再疼"), NOW.replace(hour=21, minute=30), "中文数字九点半（默认今天）")
    eq(f("我们约好 9 月 23 日早上 10 点在广场见面"),
       dt.datetime(2026, 9, 23, 10, 0), "日期+早上10点")
    eq(f("明天早上九点要开会"), dt.datetime(2026, 9, 24, 9, 0), "明天+早上九点")
    eq(f("下午 3 点碰头"), NOW.replace(hour=15, minute=0), "下午3点=15点")
    eq(f("21:30 记得吃药"), NOW.replace(hour=21, minute=30), "24小时制")
    eq(f("中午 12 点吃饭"), NOW.replace(hour=12, minute=0), "中午12点")
    eq(f("十一点半集合"), NOW.replace(hour=11, minute=30), "十一点半")
    eq(f("早一点睡"), None, "「早一点」是习惯用语不是时刻")
    eq(f("第一名"), None, "「第一名」不是时刻")
    eq(f("今天过得不错"), None, "没有时刻 → None")


def test_fix_impossible_time():
    f = lambda s: clock.fix_impossible_time(s, NOW)                  # noqa: E731
    # 踩过的原案：19:50 写入「晚上九点半也没有再疼」→ 九点半当时还没到，回拨到 9/22
    eq(f("用户持续很久的疼痛已经完全好了，2026年9月23日晚上九点半左右也没有再疼"),
       "用户持续很久的疼痛已经完全好了，2026年9月22日晚上九点半左右也没有再疼",
       "未来时刻+完成时 → 日期回拨")
    eq(f("我明天早上 10 点要开会"), "我明天早上 10 点要开会", "未来约定不回拨")
    eq(f("我 9 月 23 日早上 10 点已经去过医院"), "我 9 月 23 日早上 10 点已经去过医院",
       "已过去的时刻不动")
    eq(f("晚上九点半再说吧"), "晚上九点半再说吧", "无完成时句式不动")


def test_describe_when_timelevel():
    f = lambda s: clock.describe_when(s, NOW)                        # noqa: E731
    got = f("用户和助手约定 9 月 23 日早上 10 点在广场见面")
    eq("已经过了" in got, True, "今天上午的约（现在20点）→ 时刻级已过")
    got2 = f("用户和助手约定 9 月 23 日晚上 9 点在广场见面")
    eq("还没到" in got2, True, "今晚 9 点的约 → 还没到")


# ------------------------------------------------ 相对词重锚（存量迁移）
def test_reanchor_relatives():
    f = lambda s, b: clock.reanchor_relatives(s, b)                  # noqa: E731
    base = dt.datetime(2026, 9, 22, 21, 0)
    eq(f("年糕今天被关在家里了，明天会被放出去玩", base),
       "年糕9月22日被关在家里了，9月23日会被放出去玩", "按写入日重锚")
    eq(f("用户和助手约定 2026 年 9 月 23 日早上 10 点见面", base),
       None, "已有绝对日期 → 不动")
    eq(f("明天要中秋了", dt.datetime(2026, 9, 23, 20, 0)),
       "9月24日要中秋了", "锚是写入那天")
    eq(f("用户住在杭州", base), None, "没有相对词 → 不动")


# ------------------------------------------------- 摘要游标（T1，需临时库）
# 这一组不是纯函数：要建一个临时 SQLite 记忆库。但仍然不联网、不调模型
# （模型调用被假客户端替掉），也不碰真实数据——临时目录用完即删。
class _DeadClient:
    """模型不可用：任何调用都抛错。用来让 _summarize 走真实的失败分支。"""

    class chat:                                            # noqa: N801
        class completions:                                 # noqa: N801
            @staticmethod
            def create(**_kw):
                raise RuntimeError("模拟模型不可用")


def _ok_client(text: str):
    """返回固定文本的假客户端，形状对齐 openai 的 resp.choices[0].message.content。"""
    from types import SimpleNamespace

    resp = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text))])
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **_kw: resp)))


def _mk_store(dirpath: str, client, **cfg):
    """临时记忆库：默认 window=2、summarize_every=2，方便用少量消息触发摘要。"""
    from app.models.memory import MemoryStore

    return MemoryStore(client, path=os.path.join(dirpath, "memory.db"),
                       session="default", embed_model="test-embed",
                       cfg={"window": 2, "summarize_every": 2, **cfg})


def _fill(store, turns: int = 6) -> int:
    """塞满消息让摘要进入待办窗口（window*2 + every*2 = 8 条以上）。"""
    hist = store.sessions.setdefault("default", [])
    for i in range(turns):
        hist.append({"role": "user", "content": f"第{i}问", "ts": 0})
        hist.append({"role": "assistant", "content": f"第{i}答", "ts": 0})
    return len(hist)


def test_summarize_cursor():
    """摘要失败时游标不许推进。

    踩过的坑：_summarize 失败时 return old（非空），调用方用「返回是不是空串」
    判成功 → 判定成功 → 游标前移 → 那批对话**永远不会再被摘要**，
    而且不报错、日志里只有一句 warning。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        # 一、模型不可用（失败）：游标停在原地，等下次重试
        s = _mk_store(d, _DeadClient())
        _fill(s)
        s.summaries["default"] = "旧摘要"
        s._maybe_summarize()
        eq(s.summary_cursor.get("default", 0), 0, "摘要失败 → 游标不推进")
        eq(s.summaries["default"], "旧摘要", "摘要失败 → 旧摘要保留")

        # 二、模型可用（成功）：游标推到窗口边界，摘要更新
        s2 = _mk_store(os.path.join(d, "ok"), _ok_client("新摘要"))
        n2 = _fill(s2)
        s2.summaries["default"] = "旧摘要"
        s2._maybe_summarize()
        eq(s2.summary_cursor.get("default", 0), n2 - 4, "摘要成功 → 游标推进到窗口边界")
        eq(s2.summaries["default"], "新摘要", "摘要成功 → 摘要已更新")

        # 三、没有旧摘要时失败，同样不能推进（回归保护）
        s3 = _mk_store(os.path.join(d, "empty"), _DeadClient())
        _fill(s3)
        s3._maybe_summarize()
        eq(s3.summary_cursor.get("default", 0), 0, "无旧摘要 + 失败 → 游标不推进")

        # 四、窗口外全是空内容：没有可摘要的东西，视为已完成（不空跑模型）
        s4 = _mk_store(os.path.join(d, "blank"), _DeadClient())
        hist = s4.sessions.setdefault("default", [])
        for _ in range(6):
            hist.append({"role": "user", "content": "", "ts": 0})
            hist.append({"role": "assistant", "content": "", "ts": 0})
        s4._maybe_summarize()
        eq(s4.summary_cursor.get("default", 0), len(hist) - 4, "全空内容 → 跳过并推进")

        for st in (s, s2, s3, s4):      # Windows 下不关连接会删不掉临时库
            st.db.close()


def _extract_only_client():
    """只对「记忆提炼」返回空 JSON、其余调用一律失败的假客户端。

    提炼必须成功，才能把「摘要失败」单独隔离出来验证——否则会先撞上
    `_extract` 失败分支的另一个缺陷（那是独立的一件事，见对应的回归用例）。
    """
    from types import SimpleNamespace

    body = ('{"facts": [], "world": [], "constraints": [], '
            '"self": [], "slots": [], "drop": []}')

    def create(**kw):
        prompt = str((kw.get("messages") or [{}])[0].get("content", ""))
        if "记忆提炼器" in prompt:
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=body))])
        raise RuntimeError("这个假客户端只答提炼")

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def test_add_turn_survives_summarize_failure():
    """集成冒烟：摘要失败不能把整轮写入带崩、也不能污染会话历史。

    _maybe_summarize 排在 add_turn 的最前面（写记忆之前），它一旦抛异常，
    整轮的记忆写入都会跟着没了——所以它必须自己吞掉失败。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _extract_only_client())
        try:
            _fill(s)                                  # 先让摘要进入待办窗口
            s._summarize = lambda chunk, old: None    # 摘要失败（等同真实失败分支）
            s.add_turn("我明天要去医院", "记得带伞呀")
            eq(s.sessions["default"][-2]["content"], "我明天要去医院", "用户消息已进会话历史")
            eq(s.sessions["default"][-1]["content"], "记得带伞呀", "回答已进会话历史")
            eq(s.summary_cursor.get("default", 0), 0, "摘要失败：不影响写入，游标仍不推进")
        finally:
            s.db.close()


class _BadJsonClient:
    """模型**正常响应**、但内容不是合法 JSON —— 与额度无关的失败形态。

    现实来源：模型偶尔在 JSON 前后加一段闲话，或者输出被截断
    （llmjson 的 retries 就是为这种情况准备的）。它证明「提炼失败」不等于「没钱了」。
    """

    class chat:                                            # noqa: N801
        class completions:                                 # noqa: N801
            @staticmethod
            def create(**_kw):
                from types import SimpleNamespace

                resp = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="嗯……这个我得想想，大概就是这样吧"))])
                return resp


def test_extract_failure_does_not_break_turn():
    """提炼失败不能把整轮写入带崩——**与额度无关**，两种形态都要挡住。

    修前：`_extract` 的失败分支只 return 4 个值，而 `add_turn` 按 6 元组解包
    → ValueError 冒到 SSE 生成器，这一轮没有 done 事件、记忆也不写。
    两条现实触发路径：
      · 调用直接失败：网络抖动 / 读超时 / 限流 / 余额
      · 调用成功但输出不是合法 JSON：模型加闲话或被截断（重试用尽）
    """
    import tempfile

    for label, client in (("模型不可用", _DeadClient()),
                          ("模型返回非法 JSON", _BadJsonClient())):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            s = _mk_store(d, client)
            try:
                try:
                    s.add_turn("我明天要去医院", "记得带伞呀")
                except Exception as exc:
                    FAILS.append(f"{label}：提炼失败把整轮带崩了 → "
                                 f"{type(exc).__name__}: {exc}")
                    continue
                eq(len(s.sessions["default"]), 2, f"{label}：会话历史仍然完整")
                eq(s.memories, [], f"{label}：没有凭空写进记忆")
            finally:
                s.db.close()


def _capture_warnings(name: str = "app.memory"):
    """挂一个临时 handler 捕获 WARNING 以上记录，返回 (记录列表, 卸载函数)。"""
    import logging

    records: list = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _H(level=logging.WARNING)
    logger = logging.getLogger(name)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    def restore():
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    return records, restore


def test_prune_reports_unreachable_limit():
    """上限达不成时必须是**显式的**——max_memories 不许静默失效。

    修前：_prune 把「超限条数」直接当「删除条数」用，而候选（可淘汰的）只是
    活跃集的子集。世界书/约束/明说记住的都不能动，所以当它们占到接近上限时，
    每轮只能删一部分、永远删不够，而且一句日志都没有。
    """
    import tempfile

    from app.models.memory import KIND_FACT
    from app.models.memory_read import _when_label, KIND_WORLD

    def mem(text, kind, ts=0.0):
        return {"text": text, "kind": kind, "ts": ts, "hits": 0, "last_hit": 0}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _extract_only_client(), max_memories=3)
        try:
            # 一、上限达不成：4 条世界书 + 1 条事实，上限 3
            #     超限 2 条，可淘汰的只有 1 条 → 删完还剩 4 条
            s.memories = [mem("世界1", KIND_WORLD), mem("世界2", KIND_WORLD),
                          mem("世界3", KIND_WORLD), mem("世界4", KIND_WORLD),
                          mem("事实1", KIND_FACT)]
            records, restore = _capture_warnings()
            try:
                s._prune()
            finally:
                restore()
            eq(len(s.memories), 4, "能删的都删了（世界书一条都不能动）")
            eq([m["text"] for m in s.memories if m["kind"] == KIND_FACT], [],
               "被淘汰的正是那条普通事实")
            eq(len(records) >= 1, True, "上限达不成必须留一条 WARNING（不许静默）")
            got_msg = records[-1].getMessage() if records else ""
            eq("超限" in got_msg, True, "WARNING 里要点明「超限」")

            # 二、上限能达成时**不许**报警，否则警告会被刷成噪音
            s.memories = ([mem("世界1", KIND_WORLD)]
                          + [mem(f"事实{i}", KIND_FACT) for i in range(5)])
            records2, restore2 = _capture_warnings()
            try:
                s._prune()
            finally:
                restore2()
            eq(len(s.memories), 3, "候选够时：删到正好等于上限")
            eq(len(records2), 0, "上限达成 → 不该有任何警告")

            # 三、正好等于上限：直接返回，一条都不删
            s.memories = [mem(f"事实{i}", KIND_FACT) for i in range(3)]
            s._prune()
            eq(len(s.memories), 3, "正好等于上限 → 不淘汰")
        finally:
            s.db.close()


def test_summarize_failure_backs_off():
    """摘要失败后要退避——不能每轮都重试。

    #002 把「失败不推进游标」修好了（不丢摘要），代价是每轮 add_turn 都会再试一次。
    模型长期不可用（欠费 / 断网 / 限流）时，那等于每轮白烧一次调用，所以必须有退避。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient(), summarize_retry_delay=60)
        try:
            _fill(s)
            s.summaries["default"] = "旧摘要"
            calls: list = []
            real = s._summarize

            def spy(chunk, old):
                calls.append(1)
                return real(chunk, old)

            s._summarize = spy
            s._maybe_summarize()
            eq(len(calls), 1, "首次失败：试了一次")
            s._maybe_summarize()
            eq(len(calls), 1, "退避期内：不再重试（否则每轮白烧一次调用）")
            s._summarize_retry_at = 0.0          # 清零退避，模拟时间已过去
            s._maybe_summarize()
            eq(len(calls), 2, "退避过后：允许再试")
            eq(s.summary_cursor.get("default", 0), 0, "全程失败 → 游标始终不推进")
        finally:
            s.db.close()


def test_force_utf8_on_gbk_stream():
    """控制台流必须是 UTF-8 安全的。

    Windows 默认 GBK：中文标点（…）和 ✓ 直接抛 UnicodeEncodeError，表现是
    测试「假失败」、日志乱码（→ T7 / T18）。这里把这个能力钉住。
    """
    import io

    try:
        from app.log import force_utf8
    except ImportError:
        FAILS.append("app/log.py 没有 force_utf8：控制台流没有被切成 UTF-8")
        return

    gbk = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    try:
        gbk.write("…✓")
    except UnicodeEncodeError:
        pass                                     # 复现：GBK 流本来就是坏的
    else:
        FAILS.append("测试自身失效：构造出来的 GBK 流竟然写得出「✓」")

    force_utf8(gbk)
    eq(gbk.encoding.lower().replace("-", ""), "utf8", "流已原地切成 utf-8")
    gbk.write("中文日志 · 省略号… · 对勾✓\n")    # 修复前：这里会抛 UnicodeEncodeError
    gbk.flush()

    force_utf8(object())                         # 不支持 reconfigure 的对象也不能崩


class _FakeLLM:
    """够用的假大模型：提炼 / 世界书判定 / 摘要 / embedding 全部可控。

    有了它就能在**不联网、不调模型**的前提下把整条写入管线跑通（含 embedding 与
    向量召回）——观测类改动必须先把管线跑起来，才有东西可观测。
    """

    def __init__(self, extract: str | None = None, world_verdict: str | None = None,
                 world_verdicts: str | None = None, summary: str | None = None,
                 dim: int = 8, fail_embed: bool = False, fail_always: bool = False,
                 fail_times: int = 0):
        self.extract = extract
        self.world_verdict = world_verdict          # 单条判定（逐条路径）
        self.world_verdicts = world_verdicts        # 批量判定（一轮多条，T3 第 2 步）
        self.summary = summary
        self.dim = dim
        self.fail_embed = fail_embed
        self.fail_always = fail_always              # embedding 每次都失败（测整体不可用）
        self.fail_times = fail_times                # 前 N 次失败（测抖动与重试、片间隔离）
        self.chat_calls = 0          # 计数器：断言"一轮到底调了几次"
        self.embed_calls = 0
        self.embed_inputs: list[list[str]] = []     # 每次 embedding 实际发了哪些文本
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.embeddings = SimpleNamespace(create=self._embed)

    def _create(self, **kw):
        self.chat_calls += 1
        prompt = str((kw.get("messages") or [{}])[0].get("content", ""))
        if "记忆提炼器" in prompt:
            body = self.extract
        elif "判断下面这几条" in prompt:            # 批量判定（T3 第 2 步）
            body = self.world_verdicts
        elif "判断下面这条新设定" in prompt:
            body = self.world_verdict
        elif "压缩成一小段中文连续叙述" in prompt:
            body = self.summary
        else:
            body = None
        if body is None:
            raise RuntimeError("这个假模型没有为这类请求准备回答")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=body))])

    def _embed(self, **kw):
        self.embed_calls += 1
        batch = list(kw.get("input") or [])
        self.embed_inputs.append(batch)          # 分片测试要看每次发了多少条
        if self.fail_embed or self.fail_always or (
                self.fail_times and self.embed_calls <= self.fail_times):
            raise RuntimeError("模拟 embedding 不可用")
        return SimpleNamespace(data=[SimpleNamespace(embedding=self._vec(t)) for t in batch])

    def _vec(self, text: str) -> list:
        """同一段文本永远得到同一个向量（这样测试里的去重/相似度是确定的）。

        维度可以调大：并发测试要用真实维度（2048），因为竞争窗口就在
        `np.vstack` 的**内存分配**里——维度太小/操作太快，GIL 让竞争根本发生不了。
        """
        import hashlib

        h = hashlib.sha256(str(text).encode("utf-8")).digest()
        base = [(b - 127.5) / 255.0 for b in h]
        return (base * (self.dim // len(base) + 1))[: self.dim]


class _ConstraintProbeLLM(_FakeLLM):
    """逐轮返回约束，并记录放宽兜底是否真的被探查。"""

    def __init__(self, extracts: list[str], supersede: str):
        super().__init__(extract=extracts[0])
        self._extracts = iter(extracts)
        self._supersede = supersede
        self.supersede_calls = 0

    def _create(self, **kw):
        self.chat_calls += 1
        prompt = str((kw.get("messages") or [{}])[0].get("content", ""))
        if "记忆提炼器" in prompt:
            body = next(self._extracts)
        elif "下面这些新事实刚刚发生" in prompt:
            self.supersede_calls += 1
            body = self._supersede
        else:
            raise RuntimeError("这个假模型没有为这类请求准备回答")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=body))])


# ===== T88：整个套件**不许出网**（单点拦截，别逐处打桩）=====
# 这个文件里有 16 处 `ChatService()`，每一处构造都会 new 三套真实 OpenAI 客户端
# （后台活 / 主对话 / embedding）。只要有一条用例走到 embedding 或启动对账，就会真的发
# HTTPS——而**失败方式很坏**：实测撞上一次高德 TLS 握手超时，异常从 `set_city()` 抛穿，
# 整个套件 **exit 1 且 stdout 全空**（看不出是哪条、为什么）。
# 所以在**源头**拦一次：`ChatService.__init__` 用的是 `chat_service` 模块里的 `OpenAI`
# 这个名字，把它换成离线假客户端，16 处构造点全部一并变干净——不必逐处打桩，也不会漏。
# 配套：`CHECK_ON_START` 关掉（启动对账要真调一次模型，测试不需要它跑）。
# 守卫见 `test_pure_suite_never_builds_a_network_client`。
from app.services import chat_service as _chat_service   # noqa: E402

_chat_service.OpenAI = lambda *_a, **_kw: _FakeLLM()
config.CHECK_ON_START = False

# 感知层那条通道（weather / geo / places）全都走 `urllib`，一并把出口堵死。
# 为什么不逐个用例打桩（实测过的教训）：那样是打地鼠——探针在 socket 层记账，
# 残余的 6 次出网分成三堆（`/city detect` 那条用了没被替换的 `city_by_ip`、
# 附近那条、还有一条来自 lifespan）。**堵出口是一次性、且不会漏的做法**：
# 任何没被显式打桩的调用都会在**进程内**失败（异常被各自的 try/except 兜住，
# 行为与"网络不通"完全一致），但**一个 socket 都不会开**。
# 显式传 `opener=` 的用例不受影响（它们打的是函数参数，不是这个出口）。
import urllib.request as _urllib   # noqa: E402

_real_urlopen = _urllib.urlopen


def _no_network(*_a, **_kw):
    raise RuntimeError("测试套件不许联网（T88）：这条调用没有被显式打桩")


_urllib.urlopen = _no_network


def test_events_recorded_pruned_and_reported():
    """机制事件：记得下、能聚合、有上限（不能无限涨）。

    没有这张表，「补偿真的有效吗」「兜底触发率多少」只能靠读日志文本猜——
    而日志会滚、会丢、格式会变，数不出来。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient(), events_keep=5)
        try:
            if not hasattr(s, "event"):
                FAILS.append("MemoryStore 没有 event()：机制埋点不存在")
                return
            for i in range(8):
                s.event("probe", n=i)
            eq(len(s.events_recent(100)), 8, "写进去的事件读得回来")
            s.save()                                   # 修剪挂在 save 上（每轮一次）
            eq(len(s.events_recent(100)), 5, "超过 events_keep → 修剪到上限")
            eq(s.events_stats().get("probe"), 5, "聚合计数按现存事件算")
            eq(s.events_recent(1)[0]["n"], 7, "最近事件是最后写的那条")
            rep = s.events_report()
            eq("probe" in rep, True, "报告里能看到事件类型")
            eq(str(5) in rep, True, "报告里能看到计数")
            s.event("weird", obj=object())             # 不可序列化的字段不能崩
            eq(s.events_stats().get("weird"), 1, "不可序列化的字段被安全降级")
        finally:
            s.db.close()


def test_key_paths_leave_events():
    """埋点必须接在**真实路径**上，不能只有 API 好看。

    覆盖：提炼失败、一轮成功对话、世界书三态判定、摘要、作废。
    """
    import tempfile

    ok_json = ('{"facts": ["用户 9 月 24 日要去医院"], '
               '"world": ["用户养了一只叫年糕的橘猫"], '
               '"constraints": [], "self": [], "slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        # 一、提炼失败要留痕
        s = _mk_store(d, _DeadClient())
        try:
            if not hasattr(s, "event"):
                FAILS.append("MemoryStore 没有 event()：机制埋点不存在")
                return
            s.add_turn("我明天要去医院", "记得带伞呀")
            eq("extract_fail" in s.events_stats(), True, "提炼失败 → extract_fail 事件")
        finally:
            s.db.close()

        # 二、正常一轮 + 世界书判定 + 摘要 + 作废
        s2 = _mk_store(os.path.join(d, "ok"), _FakeLLM(
            extract=ok_json,
            world_verdict='{"verdict": "new", "stale": []}',
            summary="用户提到要去医院，助手提醒带伞。"))
        try:
            from app.models.memory import KIND_FACT

            s2.add_turn("我明天要去医院", "记得带伞呀")
            eq("turn" in s2.events_stats(), True, "一轮对话 → turn 事件")
            eq(s2.events_recent(1)[0]["facts"] >= 1, True, "turn 事件带上了提炼条数")

            # 第二次写世界书时向量库已就绪 → 真的走三态判定
            s2.add_turn("我还养了只乌龟", "乌龟好啊，好养活")
            eq("world_verdict" in s2.events_stats(), True, "世界书三态判定 → 事件")

            _fill(s2)                                  # 凑够摘要窗口
            s2._maybe_summarize()
            eq("summarize" in s2.events_stats(), True, "摘要 → 事件")

            s2.memories.append({"text": "用户以前住在杭州", "kind": KIND_FACT,
                                "ts": 0, "hits": 0, "last_hit": 0})
            eq(s2.drop_memories(["用户以前住在杭州"]), 1, "作废了一条")
            eq("drop" in s2.events_stats(), True, "作废 → 事件")
            eq("turn" in s2.events_report(), True, "报告能读出真实埋点")
        finally:
            s2.db.close()


def test_console_streams_are_both_utf8():
    """stdout 必须和 stderr 一起切——CLI / 网页启动时的 print 中文否则一样乱码。

    踩过：只切了日志那条流（console.stream = stderr），于是启动横幅、
    /mem 命令回显这类 print 出来的中文照样是乱码。
    """
    import io

    from app import log as applog

    if not hasattr(applog, "force_utf8_streams"):
        FAILS.append("app/log.py 没有 force_utf8_streams：stdout 没有被一起切到 UTF-8")
        return

    gbk_out = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    gbk_err = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    real_out, real_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = gbk_out, gbk_err
        applog.force_utf8_streams()
        eq(gbk_out.encoding.lower().replace("-", ""), "utf8", "stdout 已切到 utf-8")
        eq(gbk_err.encoding.lower().replace("-", ""), "utf8", "stderr 已切到 utf-8")
        gbk_out.write("旧识已启动 → http://127.0.0.1:8765 · …✓\n")
        gbk_err.write("日志 · …✓\n")
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def _system_text(store, query: str) -> str:
    """把一轮注入的所有 system 块拼成一段文本，用来断言「注入了什么」。"""
    msgs, _ = store.build_messages("（系统提示）", query)
    return "\n".join(m["content"] for m in msgs if m.get("role") == "system")


def test_relaxed_constraint_leaves_injection():
    """约束被解除 ≠ 被删除：不再当禁令，但仍然是她的记忆。

    这是 S11 的根：约束是全系统唯一只增不减的一层。放宽之后只有两条错路——
    继续顽固地禁止（「你不能吃花生」），或者整条消失（她完全不记得有这回事）。
    正确的结果是第三条：注入时不再说「你不能…」，而是说清「这件事已经解除了」。
    """
    import tempfile

    c_json = ('{"facts": [], "world": [], "constraints": ["用户对花生过敏，不能吃花生"], '
              '"self": [], "slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(extract=c_json))
        try:
            if not hasattr(s, "relax_constraints"):
                FAILS.append("MemoryStore 没有 relax_constraints()：约束没有失效通道")
                return
            s.add_turn("我对花生过敏", "记下了，以后不给你推荐花生")
            eq(s.constraints(), ["用户对花生过敏，不能吃花生"], "现役约束里能看到它")
            eq("必须遵守的约束" in _system_text(s, "今天吃什么好"), True, "注入里有约束块")

            eq(s.relax_constraints(["用户对花生过敏，不能吃花生"]), 1, "解除 1 条")
            eq(s.constraints(), [], "解除后不再是现役禁忌")
            eq(s.relaxed_constraints(), ["用户对花生过敏，不能吃花生"], "保留在「已解除」里")
            eq(len(s.memories), 1, "条目没有被删掉（历史还在）")

            joined = _system_text(s, "今天吃什么好")
            eq("必须遵守的约束" in joined, False, "解除后不再作为禁令注入")
            eq("已经解除" in joined, True, "注入里说明了「已解除」")
            eq("花生" in joined, True, "而且说清是哪件事解除了")
        finally:
            s.db.close()


def test_relax_persists_and_is_observed():
    """解除状态要落库（重启后仍然解除），并且留下可统计的事件。"""
    import tempfile

    c_json = ('{"facts": [], "world": [], "constraints": ["用户不能吃辣"], '
              '"self": [], "slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(extract=c_json))
        try:
            if not hasattr(s, "relax_constraints"):
                FAILS.append("MemoryStore 没有 relax_constraints()：约束没有失效通道")
                return
            s.add_turn("我不能吃辣", "记下了")
            s.relax_constraints(["用户不能吃辣"])
            s.save()
            eq("relax" in s.events_stats(), True, "解除留痕（可统计）")
        finally:
            s.db.close()

        # 重开同一个库：解除状态必须还在
        s2 = _mk_store(d, _DeadClient())
        try:
            eq(s2.constraints(), [], "重启后仍然是解除状态（不再是禁令）")
            eq(s2.relaxed_constraints(), ["用户不能吃辣"], "重启后仍在「已解除」里")
        finally:
            s2.db.close()


def test_drop_routes_constraint_to_relax():
    """作废一条约束时自动改走「解除」——对所有调用方都生效，不用各处改。"""
    import tempfile

    from app.models.memory import KIND_CONSTRAINT, KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient())
        try:
            if not hasattr(s, "relax_constraints"):
                FAILS.append("MemoryStore 没有 relax_constraints()：约束没有失效通道")
                return
            s.memories.append({"text": "用户不吃香菜", "kind": KIND_CONSTRAINT,
                               "ts": 0, "hits": 0, "last_hit": 0})
            s.memories.append({"text": "用户住在杭州", "kind": KIND_FACT,
                               "ts": 0, "hits": 0, "last_hit": 0})
            eq(s.drop_memories(["用户不吃香菜", "用户住在杭州"]), 2, "两条都被处理")
            # 约束**留在活跃集**：它没有被推翻，只是「不再生效」——这是状态变化，
            # 不是作废。普通事实才进归档（归档区的语义是「被推翻的条目留档」）。
            eq([m["text"] for m in s.memories], ["用户不吃香菜"], "约束留在活跃集")
            eq(s.constraints(), [], "但不再出现在现役约束里")
            eq(s.relaxed_constraints(), ["用户不吃香菜"], "而是出现在「已解除」里")
            eq("用户不吃香菜" in s.recent_archived(5), False, "约束不进归档（它不是被推翻的）")
            eq("用户住在杭州" in s.recent_archived(5), True, "普通事实走的是「归档」")
        finally:
            s.db.close()


def test_constraint_update_probes_superseded_without_state_change():
    """S11：新约束改写旧约束时，即使没有状态词也要触发放宽审查。"""
    import tempfile

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    new = "用户周末想找刺激的时候可以看恐怖片，平时别推给我"
    extracts = [
        json.dumps({"facts": [], "world": [], "constraints": [old],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
        json.dumps({"facts": [], "world": [], "constraints": [new],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
    ]
    supersede = json.dumps({"drop": [old]}, ensure_ascii=False)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(extracts, supersede)
        s = _mk_store(d, fake)
        try:
            s.add_turn("我胆子很小，不要推荐恐怖片", "记住了")
            s.add_turn("最近胆子大了一点点，周末想找刺激的时候可以看，平时别推给我",
                       "知道了")
            eq(fake.supersede_calls, 1,
               "新约束改写旧约束时要探查被取代项（不依赖状态词）")
            eq(s.constraints(), [new], "旧约束被放宽，新约束仍然生效")
            eq(s.relaxed_constraints(), [old], "旧约束进入已解除历史")
            eq(s.events_stats().get("superseded"), 1,
               "兜底探查命中后要留下 superseded 事件")
            eq(s.events_stats().get("relax"), 1,
               "约束更新要留下 relax 事件")
        finally:
            s.db.close()


def test_unrelated_constraint_update_keeps_old_constraint():
    """约束探查是保守的：模型判定无关时不能误解除旧约束。"""
    import tempfile

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    new = "用户晚上不要安排太早的活动"
    extracts = [
        json.dumps({"facts": [], "world": [], "constraints": [old],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
        json.dumps({"facts": [], "world": [], "constraints": [new],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
    ]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(extracts, '{"drop": []}')
        s = _mk_store(d, fake)
        try:
            s.add_turn("我胆子很小，不要推荐恐怖片", "记住了")
            s.add_turn("晚上不要安排太早的活动", "好，我记下了")
            eq(fake.supersede_calls, 1,
               "有新的约束时要走一次保守的冲突探查")
            eq(s.constraints(), [old, new], "无关约束不能误解除旧约束")
            eq(s.relaxed_constraints(), [], "无关约束不会产生解除记录")
        finally:
            s.db.close()


def test_fact_only_constraint_update_uses_user_relax_signal():
    """提炼器只产 facts 时，也不能丢掉用户原话里的约束放宽信号。"""
    import tempfile

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    new = "用户周末想找刺激的时候可以看恐怖片，平时别推给我"
    extracts = [
        json.dumps({"facts": [], "world": [], "constraints": [old],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
        json.dumps({"facts": [new], "world": [], "constraints": [],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
    ]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(extracts, json.dumps({"drop": [old]}, ensure_ascii=False))
        s = _mk_store(d, fake)
        try:
            s.add_turn("我胆子很小，不要推荐恐怖片", "记住了")
            s.add_turn("最近胆子大了一点点，周末想找刺激的时候可以看，平时别推给我",
                       "知道了")
            eq(fake.supersede_calls, 1, "facts-only 更新仍要探查旧约束")
            eq(s.constraints(), [], "facts-only 更新要解除旧约束")
            eq(s.relaxed_constraints(), [old], "facts-only 更新要保留解除历史")
        finally:
            s.db.close()


def test_empty_extraction_uses_user_relax_signal():
    """提炼器整轮空结果时，用户原话仍应触发约束放宽审查。"""
    import tempfile

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    empty = json.dumps({"facts": [], "world": [], "constraints": [],
                        "self": [], "slots": [], "drop": []}, ensure_ascii=False)
    extracts = [
        json.dumps({"facts": [], "world": [], "constraints": [old],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
        empty,
    ]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(extracts, json.dumps({"drop": [old]}, ensure_ascii=False))
        s = _mk_store(d, fake)
        try:
            s.add_turn("我胆子很小，不要推荐恐怖片", "记住了")
            s.add_turn("最近胆子大了一点点，周末想找刺激的时候可以看，平时别推给我",
                       "知道了")
            eq(fake.supersede_calls, 1, "空提炼更新仍要探查旧约束")
            eq(s.constraints(), [], "空提炼更新要解除旧约束")
            eq(s.relaxed_constraints(), [old], "空提炼更新要保留解除历史")
        finally:
            s.db.close()


def test_supersede_ignores_already_relaxed_constraints():
    """已解除的约束不能再次进入取代候选，挤掉仍现役的约束。"""
    import tempfile

    from app.models.memory import KIND_CONSTRAINT

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    new = "用户平时别推恐怖片，周末可以看"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(["{}"], json.dumps({"drop": [old]}, ensure_ascii=False))
        s = _mk_store(d, fake)
        try:
            s._add_memories([old], KIND_CONSTRAINT)
            s.relax_constraints([old])
            s._add_memories([new], KIND_CONSTRAINT)
            eq(s._find_superseded([new]), [], "已解除约束不应再次成为 supersede 候选")
        finally:
            s.db.close()


def test_fact_update_does_not_probe_constraint_superseded():
    """普通事实更新不应因为存在约束就增加一次放宽模型调用。"""
    import tempfile

    old = "用户永远别给我推荐恐怖片，我胆子特小"
    fact = "用户周末去了学校球场"
    extracts = [
        json.dumps({"facts": [], "world": [], "constraints": [old],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
        json.dumps({"facts": [fact], "world": [], "constraints": [],
                    "self": [], "slots": [], "drop": []}, ensure_ascii=False),
    ]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _ConstraintProbeLLM(extracts, '{"drop": []}')
        s = _mk_store(d, fake)
        try:
            s.add_turn("我胆子很小，不要推荐恐怖片", "记住了")
            s.add_turn("周末去了学校球场", "听起来不错")
            eq(fake.supersede_calls, 0, "普通事实不触发约束放宽探查")
            eq(s.constraints(), [old], "普通事实不会解除现役约束")
        finally:
            s.db.close()


def test_behavior_hint_from_slot_state():
    """S05：召回里明明有「年糕跑丢至今未归」，她还是推荐猫玩具。

    根因不是记忆缺失，是**行为通道缺失**：记忆是叙事（累积、靠召回），
    行为需要的是当前状态。这里验证「状态 → 分寸」这条派生通道存在、且可断言
    （不依赖 LLM 裁判：断言的是注入里有没有那条分寸）。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient())
        try:
            if not hasattr(s, "behavior_hints"):
                FAILS.append("MemoryStore 没有 behavior_hints()：状态没有派生行为分寸")
                return
            s.upsert_slots([{"entity": "年糕", "attr": "状态",
                             "value": "2026 年 9 月 16 日跑丢至今未归"}])
            hints = s.behavior_hints()
            eq(len(hints), 1, "一条消失类状态 → 一条分寸")
            eq("年糕" in hints[0], True, "分寸里点名了那个实体")
            eq("不要提议" in hints[0], True, "分寸要求「别再按旧状态发起活动」")
            # 结尾那句原来是「他主动提起时你再顺着说」——实测被读反了：用户问
            # 「给年糕买点什么玩具好」在他看来就是"主动提起"，于是"顺着说"变成了
            # 继续推荐玩具（S05 干净库下 15 遍稳定失败，注入本身完全正确）。
            eq("不要顺着" in hints[0], True,
               "必须点明「不要顺着旧情况」，否则那句会被读成「用户提起就顺着他聊」")

            joined = _system_text(s, "今天玩点什么好")
            eq("此刻的分寸" in joined, True, "分寸进入了注入")
            eq("不要提议" in joined, True, "注入里带着那条提示")

            # T43：entity 是**用户自己**时不能走这条派生——措辞是「它不在原来的位置上了…
            # 不要提议跟{entity}有关的活动」，套在一个活人身上就是纯粹的噪音。
            # 实测踩到：槽位「用户·宠物 = 橘猫年糕（柯基可乐已于2025年去世）」里的
            # 「去世」命中了词表，于是注入里出现了「不要提议跟**用户**有关的活动」。
            s.upsert_slots([{"entity": "用户", "attr": "宠物",
                             "value": "橘猫年糕（柯基可乐已于2025年去世）"}])
            hints2 = s.behavior_hints()
            eq(len(hints2), 1, "用户自己的槽位不该再加一条分寸（只剩年糕那条）")
            eq(all("用户" not in h for h in hints2), True,
               "分寸里不能出现「不要提议跟用户有关的活动」这种荒谬话")

            # T22 的真实措辞迭代：提炼器对 S05 实际写出来的槽位是
            # 「年糕·位置 = 跑出家门未归」——"未归"两个字原来不在词表里，
            # 于是 behavior_hints 空转、S05 稳定失败（用例间污染清掉后才暴露）。
            s.upsert_slots([{"entity": "小花", "attr": "状态", "value": "跑出家门未归"}])
            eq(any("小花" in h for h in s.behavior_hints()), True,
               "「未归」是提炼器的真实措辞，必须在词表里")
        finally:
            s.db.close()


def test_behavior_hint_disappears_when_state_recovers():
    """状态恢复（年糕找到了）→ 分寸必须自动消失，不能退化成永久的手写规则。"""
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient())
        try:
            if not hasattr(s, "behavior_hints"):
                FAILS.append("MemoryStore 没有 behavior_hints()：状态没有派生行为分寸")
                return
            # 一、没有消失类状态时不注入（防噪音）
            eq(s.behavior_hints(), [], "没有相关状态 → 没有分寸")
            eq("此刻的分寸" in _system_text(s, "今天玩点什么好"), False,
               "没有分寸时不该出现这个块")

            # 二、状态变成「不在」
            s.upsert_slots([{"entity": "年糕", "attr": "状态", "value": "跑丢了，还没回来"}])
            eq(len(s.behavior_hints()), 1, "消失类状态 → 有分寸")

            # 三、状态恢复（覆盖式更新）→ 分寸自动消失
            s.upsert_slots([{"entity": "年糕", "attr": "状态", "value": "已经找回来了，在家"}])
            eq(s.behavior_hints(), [], "状态恢复 → 分寸自动消失")
            eq("此刻的分寸" in _system_text(s, "今天玩点什么好"), False,
               "注入里也不再有它")
        finally:
            s.db.close()


def test_concurrent_attach_keeps_vectors_aligned():
    """并发写入不能让 `memories` 与 `vectors` 错位。

    这是 D7 花大力气消灭的静默故障（平行数组靠下标对齐、漏同步就静默错位）。
    测试直接压**最内层的成对写入动作**：8 个线程用 barrier 同时进入——只要那段
    没被锁保护，就会出现「vectors 被后来者覆盖、memories 却都追加了」，于是
    召回拿到的是别人的向量，而且**不报错**。

    为什么不用"整轮 add_turn 并发"来测：那条路径里还有模型调用，跨网络持锁会拖死
    服务。另外两个**测试设计上的坑**（都实测踩过）：
      · 从空 `vectors` 起步测不出来——`else: self.vectors = arr` 是纯内存短操作，
        GIL 根本不给切换机会（实测 3/3 对齐）；
      · 维度太小也测不出来——竞争窗口在 `np.vstack` 的内存分配里，
        用 8 维时无锁版照样"对齐"，用真实 2048 维时**无锁版 5 次里错位 4 次**。
    所以这里从**非空 vectors** 起步、用**真实维度**。
    """
    import tempfile
    import threading

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(dim=2048)
        s = _mk_store(d, fake)
        try:
            if not hasattr(s, "_attach_vector"):
                FAILS.append("MemoryStore 没有 _attach_vector()："
                             "向量与元数据没有「成对写入」的落点，并发下必然错位")
                return
            base = {"text": "垫底条目", "kind": KIND_FACT, "ts": 0.0,
                    "hits": 0, "last_hit": 0}
            s._attach_vector(base, fake._vec(base["text"]))   # 先垫一条：vectors 非空
            n = 8
            barrier = threading.Barrier(n, timeout=5)
            errs: list = []

            def worker(i):
                text = f"并发条目{i}"
                entry = {"text": text, "kind": KIND_FACT, "ts": 0.0,
                         "hits": 0, "last_hit": 0}
                try:
                    barrier.wait()                      # 8 个线程同时冲进去
                    s._attach_vector(entry, fake._vec(text))
                except Exception as exc:                # noqa: BLE001
                    errs.append(f"{type(exc).__name__}: {exc}")

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=10)

            eq(errs, [], "并发写入不该抛异常")
            eq(len(s.memories), n + 1, "8 条 + 垫底 1 条都要在（不许丢）")
            eq(len(s.vectors), n + 1, "向量条数必须等于记忆条数")
            # 最硬的不变式：每条的向量必须是它**自己**的（错位就会被抓出来）
            got = {m["text"]: tuple(round(float(x), 6) for x in v)
                   for m, v in zip(s.memories, s.vectors)}
            for text in ["垫底条目"] + [f"并发条目{i}" for i in range(n)]:
                eq(got.get(text), tuple(round(float(x), 6) for x in fake._vec(text)),
                   f"{text} 必须对应它自己的向量")
        finally:
            s.db.close()


def test_recall_hits_counted():
    """命中计数逐次累加（把「读-改-写」收进锁之后的行为回归）。"""
    import tempfile

    from app.models.memory import KIND_FACT

    empty = ('{"facts": [], "world": [], "constraints": [], "self": [], '
             '"slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(extract=empty))
        try:
            s._add_memories(["用户住在杭州，养了一只叫年糕的橘猫"], KIND_FACT)
            eq(len(s.memories), 1, "先写进去一条")
            s.recall("用户住在杭州，养了一只叫年糕的橘猫", top_k=1)
            s.recall("用户住在杭州，养了一只叫年糕的橘猫", top_k=1)
            eq(s.memories[0].get("hits"), 2, "召回两次 → hits 累加到 2")
        finally:
            s.db.close()


def test_max_constraints_visible_in_config():
    """约束注入上限要能从 config 一眼看到（原来只藏在 Model 层 `DEFAULT_CFG` 里）。

    config 里看不见的配置等于"只有读过 memory.py 的人才知道它存在"——
    这与"全部常量集中在 config"的分层约定不一致。
    """
    import tempfile

    from app import config
    from app.models.memory import KIND_CONSTRAINT

    eq("max_constraints" in config.MEMORY_CFG, True,
       "config.MEMORY_CFG 里应当有 max_constraints")
    eq(config.MEMORY_CFG.get("max_constraints"), getattr(config, "MAX_CONSTRAINTS", None),
       "字典项与 config 常量必须一致")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient(), max_constraints=2)
        try:
            s.memories = [{"text": f"约束{i}", "kind": KIND_CONSTRAINT, "ts": float(i),
                           "hits": 0, "last_hit": 0} for i in range(4)]
            eq(s.constraints(), ["约束2", "约束3"], "只注入最近的 max_constraints 条")
        finally:
            s.db.close()


def test_save_survives_concurrent_session_insert():
    """一边存档、一边冒出新 session 时，save() 不许炸。

    真实路径：`add_turn` 里 `self.sessions.setdefault(...)` 会新增 key，而 `save()`
    正在 `for s, msgs in self.sessions.items()` 上遍历 —— dict 在迭代中被改大小
    直接抛 `RuntimeError`，那是**进程级异常**，会打断整个请求，不是记忆写错那么简单。
    """
    import tempfile
    import threading

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient())
        try:
            errs: list = []
            stop = threading.Event()

            def saver():
                while not stop.is_set():
                    try:
                        s.save()
                    except Exception as exc:            # noqa: BLE001
                        errs.append(f"save(): {type(exc).__name__}: {exc}")
                        return

            def inserter():
                for i in range(300):
                    s.sessions[f"并发会话{i}"] = []     # 等价于 add_turn 的 setdefault
                stop.set()

            t1 = threading.Thread(target=saver, daemon=True)
            t2 = threading.Thread(target=inserter)
            t1.start()
            t2.start()
            t2.join(timeout=20)
            stop.set()
            t1.join(timeout=10)

            eq(errs, [], "并发新增 session 时 save() 不该抛异常")
        finally:
            s.db.close()


def test_concurrent_mixed_ops_keep_invariants():
    """混合并发冒烟：写入 / 召回 / 存档 / 归档同时跑，不变式不许破。

    它不是"证明并发安全"的测试（那要形式化证明），而是一张保护网：
    `vectors` 与 `memories` 的对齐关系一破，这里就会红。
    """
    import tempfile
    import threading

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(dim=2048)
        s = _mk_store(d, fake)
        try:
            for i in range(6):                       # 垫几条，让召回/归档有东西可动
                text = f"垫底条目{i}"
                s._attach_vector({"text": text, "kind": KIND_FACT, "ts": float(i),
                                  "hits": 0, "last_hit": 0}, fake._vec(text))
            errs: list = []
            n_write = 3

            def writer(base):
                for i in range(4):
                    text = f"写入{base}-{i}"
                    try:
                        s._attach_vector({"text": text, "kind": KIND_FACT, "ts": 0.0,
                                          "hits": 0, "last_hit": 0}, fake._vec(text))
                    except Exception as exc:         # noqa: BLE001
                        errs.append(f"write: {type(exc).__name__}: {exc}")

            def recaller():
                for _ in range(6):
                    try:
                        s.recall("垫底条目0", top_k=2)   # 顺带压命中计数
                    except Exception as exc:         # noqa: BLE001
                        errs.append(f"recall: {type(exc).__name__}: {exc}")

            def saver():
                for _ in range(4):
                    try:
                        s.save()
                    except Exception as exc:         # noqa: BLE001
                        errs.append(f"save: {type(exc).__name__}: {exc}")

            def dropper():
                for i in range(3):
                    try:
                        s.drop_memories([f"垫底条目{i}"])
                    except Exception as exc:         # noqa: BLE001
                        errs.append(f"drop: {type(exc).__name__}: {exc}")

            threads = ([threading.Thread(target=writer, args=(b,)) for b in range(n_write)]
                       + [threading.Thread(target=recaller) for _ in range(2)]
                       + [threading.Thread(target=saver), threading.Thread(target=dropper)])
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            eq(errs, [], "混合并发下不该抛异常")
            eq(len(s.vectors), len(s.memories),
               "对齐关系必须保持：vectors 条数 == memories 条数")
            got = {m["text"]: tuple(round(float(x), 6) for x in v)
                   for m, v in zip(s.memories, s.vectors)}
            for text, vec in list(got.items()):
                eq(vec, tuple(round(float(x), 6) for x in fake._vec(text)),
                   f"{text} 的向量必须是它自己的")
        finally:
            s.db.close()


def test_embedding_is_batched_once_per_turn():
    """一轮里所有要写入的文本只做**一次** embedding 往返。

    原来 `add_turn` 按 kind 分别调 `_add_memories`（facts / world / constraints / selfs），
    每个非空 kind 就是一次独立的网络往返——而 embedding 天生支持批量，这是纯浪费。
    """
    import tempfile

    full = ('{"facts": ["用户 2026 年 9 月 24 日要早起"], '
            '"world": ["用户养了一只叫年糕的橘猫"], '
            '"constraints": ["用户对花生过敏"], '
            '"self": ["我提醒用户早点睡"], '
            '"slots": [{"entity": "用户", "attr": "状态", "value": "很累"}], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(extract=full, dim=2048)
        s = _mk_store(d, fake)
        try:
            s.add_turn("今天好累，明天还要早起", "那早点睡呀")
            eq(fake.embed_calls, 1, "四类文本应当合并成一次 embedding（原来要 4 次）")
            eq(len(s.memories), 4, "facts / world / constraints / self 四类都要写进去")
            eq(len(s.slots_all()), 1, "槽位也要写进去")
        finally:
            s.db.close()


def test_batch_embed_failure_writes_nothing():
    """批量 embedding 失败 → 整轮不写记忆：要么都写、要么都不写。

    保留"逐类各自失败各自跳过"会留下半截状态（facts 进去了、world 没进去），
    而这正是本项目最忌讳的那种不一致。而且失败必须**可见**（留事件）。
    """
    import tempfile

    full = ('{"facts": ["用户要早起"], "world": ["用户养了一只叫年糕的橘猫"], '
            '"constraints": [], "self": [], "slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(extract=full, fail_embed=True)
        s = _mk_store(d, fake)
        try:
            s.add_turn("明天要早起", "早点睡")
            # 一次批量 + **片内重试一次**（2026-09-29 加的重试：瞬时抖动不值得丢记忆），
            # 所以这里是 2 而不是 1；关键是仍然"只发一个批量"，没有回到逐类各试一次。
            eq(fake.embed_calls, 2, f"一个批量 + 一次重试：{fake.embed_calls}")
            eq(s.memories, [], "整轮不写记忆（不留半截状态）")
            eq(len(s.pending) >= 1, True,
               f"但条目要进 pending 等补算，而不是丢掉：{[m['text'] for m in s.pending]}")
            eq("embed_fail" in s.events_stats(), True, "失败必须留痕（可观测）")
        finally:
            s.db.close()


def test_stale_plan_judgement_is_freezable_by_one_clock():
    """`B8`（工单二）：过期待办判定必须走 `time.time()`——仓库唯一的时钟源（测试冻得住）。

    修前 `memory_read.py` 的 `_when_label`／`upcoming`／`_is_stale_plan` 三处缺省值取
    `dt.datetime.now()`（挂钟）→ **同一个判定会随"真实现在是几点"变**。`T97` 只统一了
    life 推进那条链，这三处是残留。

    判据刻意让两个时钟**错开两天**：把 `memory_read.time` 冻到"两天前"，那么
    · 一条**昨天**（真实时间）到期的约定 → 相对冻结时间还没到 → **不许**判成过期；
    · 同一条在 `upcoming()` 里应当"还没到时候"→ 照常捞出来；
    · 冻结时刻的时间戳标签应当说"今天"，而不是"2 天前"。
    """
    import datetime as dt2
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.models import memory_read as mr
    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(dim=2048)
        s = _mk_store(d, fake)
        try:
            frozen = dt2.datetime.now() - dt2.timedelta(days=2)
            yesterday = (dt2.date.today() - dt2.timedelta(days=1)).isoformat()
            s.memories = []
            s.vectors = None
            mem = {"text": "用户和助手约好明天在广场见面", "kind": KIND_FACT, "ts": 0.0,
                   "hits": 0, "last_hit": 0, "event_date": yesterday}
            s._attach_vector(mem, fake._vec(mem["text"]))

            with patch("app.models.memory_read.time",
                       SimpleNamespace(time=lambda: frozen.timestamp())):
                eq(s._is_stale_plan(mem), False,
                   f"冻住 time.time（{frozen:%Y-%m-%d}）后，昨天到期的约定不该被判成过期")
                eq(mem["text"] in [m["text"] for _days, m in s.upcoming()], True,
                   "同一条（还没到时候）应当照常被 upcoming 捞出来")
                label = mr._when_label(frozen.timestamp())
                eq(label.startswith("（今天"), True,
                   f"冻结时刻的标签应当是「今天」而不是按挂钟算的：{label}")
        finally:
            s.db.close()


def test_world_check_batch_reads_sims_under_the_lock():
    """`C1`（工单三）：**批量**世界书判定（`len(texts) > 1` 才走的那条）里，`_sims` 也必须在锁内算。

    `B4`（`#167`）我只把单条路径（`_world_check`）收进了锁内，批量路径的
    `sims = self._sims(arr[0])[idx]` 仍留在 `with self._lock:` **之外**：`idx` 是锁内快照、
    `sims` 是锁外现算 → 并发 `_drop`/attach 时用旧 idx 索引新向量会错位、甚至越界
    （真机复现：`IndexError: index 0 is out of bounds for axis 0 with size 0`）。
    `#167` 台账里"`_world_check_batch`（idx/existing/sims 同视界）"是**覆盖过宽**的声明，本笔更正。

    判据与 B4 同款（不靠并发碰运气）：把 `store._lock` 换成记账锁，在 `_sims` 上挂探针，
    断言"批量路径里**每一次** `_sims` 都在持锁时被调用"。
    """
    import tempfile
    import threading

    from app.models.memory import KIND_WORLD

    class TrackingLock:
        def __init__(inner, real):
            inner.real = real
            inner.holders = set()

        def __enter__(inner):
            inner.real.acquire()
            inner.holders.add(threading.get_ident())
            return inner

        def __exit__(inner, exc_type, exc, tb):
            inner.holders.discard(threading.get_ident())
            inner.real.release()
            return False

        def acquire(inner, *args, **kwargs):
            got = inner.real.acquire(*args, **kwargs)
            if got:
                inner.holders.add(threading.get_ident())
            return got

        def release(inner):
            inner.holders.discard(threading.get_ident())
            inner.real.release()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            s._add_memories(["世界书甲"], KIND_WORLD)      # 先有一条（走单条路径）
            tracker = TrackingLock(s._lock)
            s._lock = tracker
            seen: list[bool] = []
            real_sims = s._sims

            def spy(vec):
                seen.append(threading.get_ident() in tracker.holders)
                return real_sims(vec)

            s._sims = spy
            s._add_memories(["世界书乙", "世界书丙"], KIND_WORLD)   # ≥2 条 → 批量路径
            eq(len(seen) >= 1, True, f"批量路径应当读到向量：seen={seen}")
            eq(all(seen), True,
               f"批量路径里有 {seen.count(False)} 次 `_sims` 是在**没持锁**时读的：{seen}")
        finally:
            s.db.close()


def test_add_turn_never_records_an_empty_assistant_turn():
    """`C2`（工单三）：**存储层不变量**——空回复不构成一轮。

    `B6` 只在 `chat()` 这一个入口堵了"清洗后为空"，绕过它的调用方（流式的 `finish_turn`、
    任何直接 `add_turn(q, "")`）照样把空 assistant 轮写进内存并落库。
    另外"整条只有内部时间标记"的回复也不许把标记原文写进记忆（`finish_turn` 直接把原文
    交给 `add_turn`，见评审复验）。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            s.add_turn("用户问了个问题", "")
            s.save()
            eq([m["content"] for m in s.sessions.get("default", [])], ["用户问了个问题"],
               "空回复不许追加 assistant 轮（用户那条仍留）")
            rows = [r["content"] for r in s.db.execute(
                "SELECT content FROM messages WHERE session='default' ORDER BY seq")]
            eq(rows, ["用户问了个问题"], f"空 assistant 轮也不许落库：{rows}")

            s.add_turn("用户又问", "（今天下午 13:54）")      # 整条只有内部时间标记
            s.save()
            eq([m["content"] for m in s.sessions.get("default", [])],
               ["用户问了个问题", "用户又问"],
               "只有时间标记的回复也不构成一轮（标记不许进记忆）")

            s.add_turn("用户再问", "（今天下午 13:54）我今天去打球了")   # 正文带前缀 → 剥掉后照记
            s.save()
            eq([m["content"] for m in s.sessions.get("default", [])],
               ["用户问了个问题", "用户又问", "用户再问", "我今天去打球了"],
               "剥掉前缀后照常记，且存的文本不含内部时间标记")
        finally:
            s.db.close()


def test_add_memories_survives_a_shrinking_vector_store():
    """`C1`（工单三）附带：`_sims` 回来时向量库**已经变短**（甚至空），不许把整轮对话炸掉。

    评审复现脚本就是这么干的：`_sims` 内部把 `memories/vectors` 清空 → 返回**空数组**（不是
    `None`）→ 去重路径的 `np.argmax(sims)` 抛 `ValueError: attempt to get argmax of an empty
    sequence`；批量路径则抛 `IndexError`。锁内计算已经堵住真实竞态，这里再兜一层：
    **视界变了就按"没命中/新增"放行**，宁可多一条记忆，也不让对话轮崩掉。
    """
    import tempfile

    import numpy as np

    from app.models.memory import KIND_FACT, KIND_WORLD

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            s._add_memories(["用户养了只猫叫年糕"], KIND_FACT)     # 先有条普通记忆
            real_sims = s._sims

            def shrink(qv):                       # 模拟"算到一半，向量库被并发删空"
                s.memories = []
                s.vectors = None
                return real_sims(qv)

            s._sims = shrink
            _added, _dup = s._add_memories(["用户养了只猫叫年糕"], KIND_FACT)   # 去重路径
            print("  （去重路径没崩）")
        finally:
            s.db.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            s._add_memories(["世界书甲"], KIND_WORLD)
            real_sims = s._sims

            def shrink2(qv):
                s.memories = []
                s.vectors = None
                return np.array([])

            s._sims = shrink2
            _added, _dup = s._add_memories(["世界书乙", "世界书丙"], KIND_WORLD)   # 批量路径
            print("  （批量路径没崩）")
        finally:
            s.db.close()


def test_default_personality_forbids_confabulating_details():
    """`T98`：人设默认值必须堵住两种**实测**失效形状（`#177` 的 12 次取样，`life_event` 为空、零来源）。

    · C 型：「赢啦，差点被翻盘，最后俩球才拉开」——**顺着话头补一个具体结果**；
    · B 型：「你怎么知道我昨天打球了——咱不是昨天才刚认识的嘛」——**默认了假前提**再用反问岔开。

    两种都违反人设已有的「不编没发生过的事」。规则要**同时**写在默认值里，而且必须真的
    进得了 system prompt（`render()` 会把 `behavior` 渲染进去），否则就是个死条款。
    """
    import tempfile

    from app.models.personality import DEFAULT_PERSONALITY, Personality

    rules = " ".join(str(x) for x in DEFAULT_PERSONALITY.get("behavior", []))
    eq("补一个具体结果" in rules, True,
       f"默认人设要堵住「顺着话头补结果」（T98 C 型现场）：{rules[:90]}")
    eq("没有这回事" in rules, True,
       f"默认人设要堵住「默认假前提」（T98 B 型现场）：{rules[:90]}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        persona = Personality(path=os.path.join(d, "personality.json"))
        rendered = persona.render()
        eq("补一个具体结果" in rendered, True, "这条规则必须真的进 system prompt（不许是死条款）")
        eq("没有这回事" in rendered, True, "同上：进不了提示词等于没写")


def test_load_dedupes_pending_by_text():
    """`B9-①`（工单二）：`_load` 从 `memories.vec IS NULL` 重建 `pending` 时**不按文本去重** →
    同一条文本会被补两次向量、进活跃集两次（`pending` 表捡回的"孤儿"那条也一样）。
    """
    import tempfile

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _extract_only_client())
        try:
            for _ in range(2):     # 同一条文本、两次「还没算出向量」的写入
                s._insert_memory({"text": "用户养了只猫叫年糕", "kind": KIND_FACT, "ts": 1.0,
                                  "session": "default", "hits": 0, "last_hit": 0,
                                  "explicit": 0, "event_date": None, "actor": "user",
                                  "relaxed_at": None}, None)
            s.memories = []
            s.pending = []
            s._load()
            texts = [m["text"] for m in s.pending]
            eq(texts, ["用户养了只猫叫年糕"], f"pending 里同一条文本只能有一份：{texts}")
        finally:
            s.db.close()


def test_ordinary_duplicate_updates_ts_in_db():
    """`B9-②`（工单二）：**普通**重复分支改了 `hit["ts"]` 却不落库（受保护的那条落了库）→
    重启后 `ts` 回退，时间轴与遗忘权重都按旧值算。
    """
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            with patch("app.models.memory_write.time",
                       SimpleNamespace(time=lambda: 1000.0)):
                s._add_memories(["用户养了只猫叫年糕"], KIND_FACT)
            with patch("app.models.memory_write.time",
                       SimpleNamespace(time=lambda: 2000.0)):
                _added, dup = s._add_memories(["用户养了只猫叫年糕"], KIND_FACT)
            eq(dup, 1, "第二次应当判重")
            row = s.db.execute("SELECT ts FROM memories WHERE text=?",
                               ("用户养了只猫叫年糕",)).fetchone()
            eq(float(row["ts"]), 2000.0, f"判重后新的 ts 必须落库（重启不回退）：{dict(row)}")
        finally:
            s.db.close()


def test_extract_recall_failure_is_logged():
    """`B9-③`（工单二）：`_extract` 里召回失败被 `except Exception: []` **静默吞掉** →
    降级看不见（提炼质量掉了，日志里却什么都没有）。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        empty = ('{"facts": [], "world": [], "constraints": [], "self": [], '
                 '"slots": [], "drop": []}')
        s = _mk_store(d, _FakeLLM(extract=empty, dim=8))
        try:
            def boom(*_a, **_k):
                raise RuntimeError("recall 炸了")

            s.recall = boom
            records, restore = _capture_warnings()
            try:
                s._extract("你好", "嗯")
            finally:
                restore()
            joined = " ".join(str(r.getMessage()) for r in records)
            eq(len(records) >= 1, True, "召回失败必须留一条 WARNING（不许静默）")
            eq(("召回" in joined) or ("recall" in joined), True,
               f"日志里要点明是召回：{joined[:90]}")
        finally:
            s.db.close()


def test_compensate_restores_by_original_kind():
    """`B9-④`（工单二）：补偿捡回的条目按**原来的 kind** 写回。

    原来只要这批里**有一条**是世界书，就把捡回来的**全部**按世界书写回（混批被贴错标签）→
    普通事实会永久变成世界书（常驻注入、不再参与冷却淘汰）。
    """
    from app.models.memory import KIND_CONSTRAINT, KIND_FACT, KIND_WORLD

    from app.models.memory import MemoryStore

    s = object.__new__(MemoryStore)
    groups = s._group_by_original_kind(
        ["住在杭州", "养了只猫叫年糕", "不吃香菜"],
        {"住在杭州": KIND_FACT, "养了只猫叫年糕": KIND_WORLD, "不吃香菜": KIND_CONSTRAINT})
    eq(sorted(groups.get(KIND_FACT) or []), ["住在杭州"], f"事实留在事实桶：{groups}")
    eq(groups.get(KIND_WORLD), ["养了只猫叫年糕"], f"世界书留在世界书桶：{groups}")
    eq(groups.get(KIND_CONSTRAINT), ["不吃香菜"], f"约束留在约束桶：{groups}")
    # 没记到 kind 的（旧数据）按事实处理，而不是跟着别人变成世界书
    groups2 = s._group_by_original_kind(["来路不明"], {})
    eq(groups2.get(KIND_FACT), ["来路不明"], f"缺 kind 的退化成事实：{groups2}")


def test_drop_memories_only_calls_on_drop_when_something_dropped():
    """`B9-⑤`（工单二）：`drop_memories` **无匹配也回调 `on_drop`** → 世界书同步会去删
    "根本没被作废"的条目。回调的语义是「这些没了」，不是「请把这些删掉」。
    """
    import tempfile

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))       # 要真进 `memories`（没有 embedding 会落 pending）
        try:
            seen: list[list[str]] = []
            s.on_drop = lambda texts: seen.append(list(texts))
            eq(s.drop_memories(["根本不存在的文本"]), 0, "没有任何匹配 → 返回 0")
            eq(seen, [], f"没作废任何东西就不该回调 on_drop：{seen}")
            s._add_memories(["用户养了只猫叫年糕"], KIND_FACT)
            s.drop_memories(["用户养了只猫叫年糕"])
            eq(len(seen), 1, f"真的作废了就要回调一次：{seen}")
        finally:
            s.db.close()


def test_stale_plan_is_not_recalled():
    """过期约定不再参与召回抢名额——但要分清它和"历史事件"。

    现状：`upcoming()` 早就把 `days < 0` 的挡掉了（"今天说好的事"里不会出现），
    但**召回层没管**：过期约定仍会和普通记忆抢 `top_k`，还可能被讲成
    「我们约好明天去广场」。过滤**不是**让她忘掉：数据留在库里，时间轴、对账
    照旧看得到。
    """
    import datetime as dt2
    import tempfile

    from app.models.memory import KIND_FACT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(dim=2048)
        s = _mk_store(d, fake)
        try:
            past = (dt2.date.today() - dt2.timedelta(days=3)).isoformat()
            future = (dt2.date.today() + dt2.timedelta(days=1)).isoformat()
            cases = [
                ("用户和助手约定 9 月 20 日在广场见面", past, 0, "过期约定 → 不召回"),
                ("用户和助手约定明天在广场见面", future, 1, "还没到的约定 → 照常召回"),
                ("用户 9 月 20 日去了医院", past, 1, "历史事件（非约定）→ 照常召回"),
                # 下面两条是"宁可漏也别误"的护栏——真实库里就有这两条，第一版判据
                # 会把它们当成过期约定挡掉，而那等于抹掉一段共同经历。
                ("用户 9 月 20 日已到达与助手约定的广场见面地点", past, 1,
                 "已完成的事（约定词 + 完成时）→ 照常召回"),
                ("助手在 9 月 20 日见面时带了小饼干", past, 1,
                 "中性『见面』不是待办语义 → 照常召回"),
            ]
            for text, ed, want, label in cases:
                s.memories = []
                s.vectors = None
                s._attach_vector({"text": text, "kind": KIND_FACT, "ts": 0.0,
                                  "hits": 0, "last_hit": 0, "event_date": ed},
                                 fake._vec(text))
                got = s.recall(text, top_k=3, mark_hit=False)
                eq(len(got), want, label)

            # 跳过的条数会记进 turn 事件（不刷 events 表）
            stale_text = "用户和助手约定 9 月 20 日在广场见面"
            s.memories = []
            s.vectors = None
            s._attach_vector({"text": stale_text, "kind": KIND_FACT, "ts": 0.0,
                              "hits": 0, "last_hit": 0, "event_date": past},
                             fake._vec(stale_text))
            before = len(getattr(s, "_stale_skipped", ()))
            s.recall(stale_text, top_k=3, mark_hit=False)
            eq(len(getattr(s, "_stale_skipped", ())), before + 1, "跳过的过期待办会被计数")
        finally:
            s.db.close()


# ------------------------------------------------- TTS（协议层用假 WS）
class _FakeWS:
    """假 WS 服务端：按脚本回消息，并记下客户端发出去的每一帧。

    有了它，协议层（发了什么参数、收到什么之后做什么）就能**离线、确定性**地测，
    不必真的调一次合成。`hang_after` 用来模拟"服务端不回话"（测超时）——
    直接抛 `TimeoutError`，正好对上 `websockets.sync` 的行为，也省掉等待。
    """

    def __init__(self, script, hang_after=None):
        self.script = list(script)
        self.sent = []                 # 原始帧（测试里再 json.loads）
        self.hang_after = hang_after
        self._n = 0

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        if self.hang_after is not None and self._n >= self.hang_after:
            raise TimeoutError("fake ws: 服务端不回话")
        if not self.script:
            raise TimeoutError("fake ws: 脚本耗尽")
        self._n += 1
        return self.script.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_fake_ws(ws, fn, *args, **kwargs):
    """在假 WS 下跑一次 fn（fn 内部会走到 tts 的合成）。

    **两条路都要拦**：`tts.ws_connect`（同步客户端）与 `websockets.connect`（异步）。
    只拦一条的教训：改名之前只拦同步那句，结果测试真的联网打了一次线上合成
    （日志里留下一句 11.3s 的真实 TTS）。
    """
    from app.utils import tts as _tts

    saved = {}
    if hasattr(_tts, "ws_connect"):
        saved["sync"] = _tts.ws_connect
        _tts.ws_connect = lambda *a, **kw: ws
    if hasattr(_tts, "websockets"):
        saved["async"] = _tts.websockets.connect
        _tts.websockets.connect = lambda *a, **kw: ws
    try:
        return fn(*args, **kwargs)
    finally:
        if "sync" in saved:
            _tts.ws_connect = saved["sync"]
        if "async" in saved:
            _tts.websockets.connect = saved["async"]


def _ws_script(*, audio=b"\x00" * 128, finish=True, fail=None):
    import json as _json

    out = [_json.dumps({"header": {"event": "task-started"}})]
    if audio:
        out.append(audio)
    if fail:
        out.append(_json.dumps({"header": {"event": "task-failed",
                                           "error_code": "X", "error_message": fail}}))
    if finish:
        out.append(_json.dumps({"header": {"event": "task-finished"},
                                "payload": {"usage": {"characters": 10}}}))
    return out


def test_tts_request_carries_instruction_and_rate():
    """T27 / T28：语气指令与语速必须真的发出去。

    原来 `_synthesize()` 收下 `instruction` 参数却从没写进 `parameters`，
    `speak()` 算出的 `rate` 也从不使用（服务端写死 `"rate": 1`）——
    等于「情绪→语气」名存实亡。参数名 `instruction` 是实测探出来的
    （`instructions` / `prompt` 都逐字节无效，见 #019 / #020）。
    """
    import json as _json

    from app import config
    from app.utils import tts as _tts

    ws = _FakeWS(_ws_script())
    bak = (config.TTS_ENABLED, config.TTS_TIMEOUT)
    config.TTS_ENABLED, config.TTS_TIMEOUT = True, 5
    try:
        audio = _with_fake_ws(ws, _tts.speak, "你好呀，今天过得怎么样？", "开心")
    finally:
        config.TTS_ENABLED, config.TTS_TIMEOUT = bak

    eq(bool(audio) and len(audio) == 128, True, "假 WS 的音频块应当被收下")
    eq(len(ws.sent) >= 2, True, "至少发了 run-task 与 continue-task")
    params = _json.loads(ws.sent[0])["payload"]["parameters"]
    eq(bool(params.get("instruction")), True,
       "parameters 里必须有 instruction（实测：instructions / prompt 都无效）")
    eq(params.get("rate"), _tts._MOOD_DIMS["开心"]["rate_global"],
       "rate 必须用当前情绪的语速，而不是写死 1")
    eq(params.get("volume"), _tts._MOOD_DIMS["开心"]["volume"],
       "volume 本来就在传（回归保护）")


def test_tts_streams_first_chunk_without_waiting():
    """T37：音频必须**边收边出**——第一块到手就交出去，不能等整段收完。

    实测（#019）：同一段 33 字，服务端**首字节 0.67s**、但整段 11s 才推完；
    原来 `_synthesize` 收完才 return，前端又把它塞进一条 SSE —— 用户白等一整段
    音频的时长。这里用一个「第二个块永不返回」的假 WS 把这个行为钉住，
    顺带验证 T33（超时要留痕）与 T34（分段耗时要记下来）。
    """
    import json as _json

    from app import config
    from app.utils import tts as _tts

    if not hasattr(_tts, "synth_stream"):
        FAILS.append("tts.synth_stream() 不存在：音频还是一次性收完才给")
        return

    ws = _FakeWS([_json.dumps({"header": {"event": "task-started"}}), b"CHUNK-1"],
                 hang_after=2)          # 第三次 recv 永不返回
    st: dict = {}
    got: list = []
    bak = config.TTS_TIMEOUT
    config.TTS_TIMEOUT = 1              # 让超时立刻发生（测试要快）
    try:
        _with_fake_ws(ws, lambda: [got.append(c) for c in
                                   _tts.synth_stream("你好呀，今天过得怎么样？", stats=st)])
    finally:
        config.TTS_TIMEOUT = bak

    eq(got, [b"CHUNK-1"], "第一个块必须立刻交出来（不许等后面收完）")
    eq(st.get("first_byte") is not None, True, "首字节耗时要记录（T34）")
    eq(st.get("conn") is not None, True, "连接耗时要记录（T34）")
    eq(st.get("timeout"), True, "收不到后续 → 必须留痕，不许静默（T33）")
    eq(st.get("chunks"), 1, "块数要如实记录")


def test_tts_speak_stream_yields_chunks():
    """T37 的服务层入口：必须逐块交出来（端到端「边收边播」的前提）。

    `speak()` 只是它的"收全版"；聊天链路走的是这条。
    """
    import json as _json

    from app import config
    from app.utils import tts as _tts

    if not hasattr(_tts, "speak_stream"):
        FAILS.append("tts.speak_stream() 不存在：链路只能整段收完再播")
        return
    ws = _FakeWS([_json.dumps({"header": {"event": "task-started"}}),
                  b"A", b"BB", b"CCC",
                  _json.dumps({"header": {"event": "task-finished"}})])
    st: dict = {}
    bak = config.TTS_TIMEOUT
    config.TTS_TIMEOUT = 5
    try:
        got = _with_fake_ws(ws, lambda: list(
            _tts.speak_stream("你好呀，今天过得怎么样？", "开心", stats=st)))
    finally:
        config.TTS_TIMEOUT = bak
    eq(len(got), 3, "三块都要交出来")
    eq(b"".join(got), b"ABBCCC", "内容与顺序不变")
    eq(st.get("chunks"), 3, "块数如实记录（T34）")
    eq(st.get("fallback"), None, "没出错就不该走降级")


def test_world_review_is_batched_per_turn():
    """T3 第 2 步：一轮里多条新世界书设定只问**一次**模型。

    原来每条一次往返（一轮 3 条新设定 = 3 次）。实测（#023）同一批 8 个条目里，
    逐条判定与一次判多条的 verdict / stale **8/8 完全一致**，所以合并是安全的。
    这里断言"调用次数"：一轮 = 提炼 1 次 + 世界书判定 1 次（无论几条新设定）。
    """
    import json as _json
    import tempfile

    j1 = ('{"facts": [], "world": ["用户养了一只叫年糕的橘猫"], '
          '"constraints": [], "self": [], "slots": [], "drop": []}')
    j2 = ('{"facts": [], "world": ["用户住在杭州", "用户常去楼下的咖啡馆"], '
          '"constraints": [], "self": [], "slots": [], "drop": []}')
    verdicts = _json.dumps({"verdicts": [
        {"index": 0, "verdict": "new", "stale": []},
        {"index": 1, "verdict": "new", "stale": []}]}, ensure_ascii=False)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(extract=j1, world_verdicts=verdicts, dim=2048)
        s = _mk_store(d, fake)
        try:
            s.add_turn("我养了只猫叫年糕", "记住啦")     # 第一轮：建向量，不触发判定
            eq(len(s.world_texts()), 1, "第一轮写入 1 条世界书")
            fake.chat_calls = 0
            fake.extract = j2                            # 第二轮产出 2 条新设定
            s.add_turn("我住杭州，常去楼下咖啡馆", "记下了")
            eq(fake.chat_calls, 2, "提炼 1 次 + 世界书判定 1 次（两条合并成一次）")
            eq(len(s.world_texts()), 3, "三条世界书都在")
        finally:
            s.db.close()


def test_world_review_single_item_still_works():
    """只有一条新设定时不该为它走批量 prompt（回归保护：单条路径保留）。"""
    import tempfile

    j1 = ('{"facts": [], "world": ["用户养了一只叫年糕的橘猫"], '
          '"constraints": [], "self": [], "slots": [], "drop": []}')
    j2 = ('{"facts": [], "world": ["用户住在杭州"], '
          '"constraints": [], "self": [], "slots": [], "drop": []}')

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(extract=j1, world_verdict='{"verdict": "new", "stale": []}',
                        dim=2048)
        s = _mk_store(d, fake)
        try:
            s.add_turn("我养了只猫叫年糕", "记住啦")
            fake.chat_calls = 0
            fake.extract = j2
            s.add_turn("我住杭州", "记下了")
            eq(fake.chat_calls, 2, "提炼 1 次 + 单条判定 1 次")
            eq(len(s.world_texts()), 2, "两条世界书都在")
        finally:
            s.db.close()


def test_tts_records_billed_characters():
    """T38：把服务端**计费**的字符数记下来。

    实测（#019）：同一段 33 字文本，纯文本 `usage.characters=33`，带两个 `<break>`
    标签变成 **75** —— SSML 标记也算钱。不记下来就永远不知道韵律花了多少。
    """
    import json as _json

    from app import config
    from app.utils import tts as _tts

    ws = _FakeWS([_json.dumps({"header": {"event": "task-started"}}),
                  b"X",
                  _json.dumps({"header": {"event": "task-finished"},
                               "payload": {"usage": {"characters": 187}}})])
    st: dict = {}
    bak = config.TTS_TIMEOUT
    config.TTS_TIMEOUT = 5
    try:
        _with_fake_ws(ws, lambda: list(_tts.speak_stream("你好呀。", None, stats=st)))
    finally:
        config.TTS_TIMEOUT = bak
    eq(st.get("billed"), 187, "usage.characters 要落到 stats 里（含标签的计费字符）")


def test_tts_fallback_strips_all_tags():
    """T30：降级纯文本时 `<break>` 与 `[excited]` 都要剥掉。

    实测（#019）：PlainText 下 `[tag]` 会被服务端**静默忽略**（不会念出来），
    所以剥它不是为了"防念出来"，而是别把无效字符算进计费字符数
    （SSML 标签按字符计费：同一段 33 字文本，带两个 break 后计费 75）。
    """
    from app.utils import tts as _tts

    if not hasattr(_tts, "plain_text"):
        FAILS.append("tts.plain_text() 不存在：降级时没有统一的剥标记函数")
        return
    raw = '[excited]你好呀<break time="240ms"/>，今天<prosody rate="+10%">开心</prosody>吗？'
    eq(_tts.plain_text(raw), "你好呀，今天开心吗？", "SSML 与方括号标签都要剥干净")


def test_tts_trims_long_text_by_sentence():
    """T32：超长回复按句截断（`TTS_MAX_CHARS` 原来是死配置，从不生效）。"""
    from app.utils import tts as _tts

    if not hasattr(_tts, "trim_for_speech"):
        FAILS.append("tts.trim_for_speech() 不存在：长文本没有截断")
        return
    text = "第一句话。第二句话。第三句话。第四句话。第五句话。"
    got = _tts.trim_for_speech(text, 12)
    eq(len(got) <= 12, True, f"截断后长度 {len(got)} 应当不超过 12")
    eq(got.endswith("。"), True, "必须停在句子边界（不许把句子切一半）")
    eq(text.startswith(got), True, "只截尾，不改写原文")
    eq(_tts.trim_for_speech("短句。", 100), "短句。", "没超限就不动")
    eq(_tts.trim_for_speech("", 10), "", "空文本安全")


def main() -> int:
    for fn in (test_find_date, test_describe_when, test_now_text,
               test_personality_migrate,
               test_find_datetime, test_fix_impossible_time,
               test_describe_when_timelevel, test_reanchor_relatives,
               test_summarize_cursor, test_add_turn_survives_summarize_failure,
               test_extract_failure_does_not_break_turn,
               test_prune_reports_unreachable_limit,
               test_summarize_failure_backs_off,
               test_force_utf8_on_gbk_stream,
               test_console_streams_are_both_utf8,
               test_events_recorded_pruned_and_reported,
               test_key_paths_leave_events,
               test_relaxed_constraint_leaves_injection,
               test_relax_persists_and_is_observed,
               test_drop_routes_constraint_to_relax,
               test_constraint_update_probes_superseded_without_state_change,
               test_unrelated_constraint_update_keeps_old_constraint,
               test_fact_only_constraint_update_uses_user_relax_signal,
               test_empty_extraction_uses_user_relax_signal,
               test_supersede_ignores_already_relaxed_constraints,
               test_fact_update_does_not_probe_constraint_superseded,
               test_behavior_hint_from_slot_state,
               test_behavior_hint_disappears_when_state_recovers,
               test_concurrent_attach_keeps_vectors_aligned,
               test_recall_hits_counted,
               test_max_constraints_visible_in_config,
               test_save_survives_concurrent_session_insert,
               test_concurrent_mixed_ops_keep_invariants,
               test_embedding_is_batched_once_per_turn,
               test_batch_embed_failure_writes_nothing,
               test_stale_plan_is_not_recalled,
               test_world_review_is_batched_per_turn,
               test_world_review_single_item_still_works,
               test_tts_request_carries_instruction_and_rate,
               test_tts_streams_first_chunk_without_waiting,
               test_tts_speak_stream_yields_chunks,
               test_tts_records_billed_characters,
               test_tts_fallback_strips_all_tags,
               test_tts_trims_long_text_by_sentence,
               test_tts_script,
               test_no_think_kwargs_use_effective_family,
               test_constraint_block_asks_to_say_it_out_loud,
               test_personality_guardrails,
               test_strip_conflicting_persona_items,
               test_strip_conflicting_persona_items_respects_field,
               test_strip_conflicting_persona_scalar_items,
               test_personality_conflict_context_includes_fixed_base,
               test_personality_pending_review,
               test_atomic_json_write_survives_failure,
               test_multi_user_data_isolation,
               test_auth_token_issue_and_resolve,
               test_dotenv_load_and_priority,
               test_registry_skips_in_use_instance,
               test_worker_queue_serializes_and_survives,
               test_user_delete_and_quota,
               test_svc_enforces_auth,
               test_opinion_dedupe_counts_independent_hits_only,
               test_opinion_gate_skips_weak_and_factual_feedback,
               test_opinion_extraction_failure_is_visible,
               test_opinion_block_is_graded_scoped_and_limited,
               test_opinion_promotion_needs_two_hits_and_a_note,
               test_promotion_patch_appends_without_dropping_existing,
               test_opinions_are_isolated_per_user_and_reported,
               test_opinion_independence_crosses_day_boundary,
               test_feedback_file_is_per_user,
               test_clear_session_drops_stored_summary,
               test_forget_all_with_world_notifies_callback,
               test_inplace_promotion_is_persisted,
               test_pending_table_orphans_are_recovered,
               test_single_item_embed_failure_is_visible,
               test_supersede_and_compensate_paths_run,
               test_mem_del_physically_deletes,
               test_world_reload_uses_per_user_file,
               test_nickname_percent_encoding_without_marker,
               test_profile_update_merges_instead_of_replacing,
               test_emotion_write_failure_is_visible,
               test_timeline_includes_archived_entries,
               test_assistant_side_facts_are_tagged_as_her_own,
               test_turn_event_reports_stale_skips,
               test_day_part_matches_clock,
               test_system_prompt_states_day_part_and_greeting_rule,
               test_day_plan_block_excludes_weather_and_lists_tomorrow,
               test_when_label_is_relative_and_says_today_part,
               test_sliding_window_carries_time_labels,
               test_fact_feedback_becomes_a_pending_correction,
               test_correction_accept_archives_the_named_memory,
               test_correction_drop_and_report,
               test_city_lookup_and_weather_switch,
               test_city_command_updates_world_and_weather,
               test_city_survives_weather_failure,
               test_city_from_coords_and_ip_lookup,
               test_geo_powershell_script_is_ascii,
               test_city_detect_reports_but_does_not_write,
               test_city_detect_falls_back_to_ip_and_says_so,
               test_place_keywords_come_from_preferences,
               test_places_search_cache_and_offline_fallback,
               test_how_far_speaks_like_a_person,
               test_nearby_intent_needs_both_parts,
               test_nearby_block_reaches_the_prompt_only_when_asked,
               test_favorite_places_render_and_are_append_only,
               test_places_skip_non_leisure_categories,
               test_growing_base_fields_append_only_and_pin,
               test_growing_fields_go_through_pending_not_auto_apply,
               test_evolution_routes_base_preferences_to_pending,
               test_strip_time_prefix_only_at_the_start,
               test_stream_prefix_stripper_holds_then_releases,
               test_weather_uses_forecast_when_lives_is_empty,
               test_weather_refresh_failure_is_visible_and_retries_soon,
               test_weather_city_comes_from_world_book_before_first_fetch,
               test_weather_does_not_guess_a_city,
               test_reconcile_marks_sides_and_accepts_both_forms,
               test_embed_splits_into_batches_of_twenty,
               test_embed_one_bad_batch_does_not_drop_the_others,
               test_embed_retries_once_on_transient_failure,
               test_add_memories_keeps_failed_embeddings_in_pending,
               test_recover_missing_vectors_handles_partial_failure,
               test_scene_labels_group_cross_turn_restatements,
               test_scene_absent_falls_back_to_turn_group,
               test_timeline_folds_same_scene,
               test_event_group_rebuild_buckets_by_minute,
               test_corrupt_personality_is_quarantined_not_overwritten,
               test_missing_personality_is_created_normally,
               test_benchmark_runners_force_isolated_data_dir,
               test_benchmark_runner_refuses_wrong_data_dir_env,
               test_corrupt_personality_guard_is_wired_in_source,
               test_place_keywords_prefer_her_places_over_profile,
               test_scene_reuse_injected_into_extract_prompt,
               test_scene_label_is_normalized,
               test_extract_prompt_forbids_the_word_assistant,
               test_timeline_speaks_her_name_and_keeps_data_intact,
               test_life_slots_and_advance,
               test_life_progress_uses_a_persisted_time_window,
               test_old_life_state_adds_scheduling_columns,
               test_life_is_isolated_from_memory_and_profile,
               test_life_history_is_available_when_asked,
               test_life_catches_up_missed_slots,
               test_proactive_gates_block_and_allow,
               test_proactive_can_choose_silence_without_new_life,
               test_proactive_tick_records_her_silence,
               test_proactive_skip_is_visible_in_events,
               test_proactive_tick_advances_her_life,
               test_life_advance_does_not_repeat_the_same_line,
               test_life_chooses_an_alternative_candidate_for_repeated_activity,
               test_life_user_update_clears_completed_care,
               test_life_assistant_update_marks_completed_activity,
               test_chat_turn_observes_life_updates_at_both_boundaries,
               test_life_assistant_contradiction_does_not_mark_activity_complete,
               test_life_completion_does_not_take_unrelated_work_or_user_plants,
               test_pending_delivery_needs_ack,
               test_life_board_shows_provenance,
               test_life_api_route_is_registered_and_shaped,
               test_her_own_home_is_not_his_to_answer,
               test_nonstream_reply_is_cleaned_like_stream,
               test_static_assets_must_revalidate,
               test_she_must_not_contradict_what_she_just_said,
               test_empty_model_reply_is_retried_and_never_sent_as_blank,
               test_opinion_distiller_never_invents_what_the_user_did_not_say,
               test_non_style_feedback_is_not_forced_into_a_preference,
               test_fact_feedback_still_goes_to_the_correction_channel,
               test_opinions_can_be_dropped_from_a_regular_command,
               test_life_board_can_delete_an_event_and_pin_a_care,
               test_life_edit_routes_are_registered,
               test_short_utterance_dedup_catches_todays_real_repeat,
               test_proactive_speak_drops_repeats_and_is_visible,
               test_proactive_tick_dedup_is_visible_in_events,
               test_her_completion_updates_state_and_the_slot,
               test_greeting_is_grounded_and_does_not_collide_with_pending,
               test_proactive_prompt_asks_her_to_report_in,
               test_reporting_is_once_per_slot,
               test_relationship_stage_advances_by_days_or_messages,
               test_relationship_advance_is_her_decision_with_guards,
               test_she_can_advance_the_relationship_through_review,
               test_relationship_lands_in_prompts,
               test_relationship_promise_lifecycle_preserves_history,
               test_relationship_missed_promise_requires_reason_and_is_auditable,
               test_relationship_commands_close_promises_without_deleting_them,
               test_proactive_drops_asking_him_about_her_own,
               test_quiet_hours_start_at_midnight_not_23,
               test_life_advance_uses_one_clock_source,
               test_sync_world_drops_by_identity_not_by_stale_index,
               test_ownership_guard_catches_bare_question_mark,
               test_ownership_guard_does_not_kill_his_side_nouns,
               test_dropped_own_question_is_not_acked_immediately,
               test_life_and_speech_writes_share_the_store_lock,
               test_dedup_and_world_checks_read_vectors_under_the_lock,
               test_clear_session_does_not_resurrect_old_messages,
               test_pure_time_marker_reply_does_not_land_in_memory,
               test_llmjson_recovers_truncated_json,
               test_stale_plan_judgement_is_freezable_by_one_clock,
               test_load_dedupes_pending_by_text,
               test_ordinary_duplicate_updates_ts_in_db,
               test_extract_recall_failure_is_logged,
               test_compensate_restores_by_original_kind,
               test_drop_memories_only_calls_on_drop_when_something_dropped,
               test_world_check_batch_reads_sims_under_the_lock,
               test_add_turn_never_records_an_empty_assistant_turn,
               test_add_memories_survives_a_shrinking_vector_store,
               test_default_personality_forbids_confabulating_details,
               test_proactive_enqueue_and_take_is_idempotent,
               test_speak_splits_into_messages_like_a_normal_person,
               test_pure_suite_never_builds_a_network_client,
               test_prepare_only_hints_multi_message_when_asked,
               test_split_messages_matches_the_frontend_contract,
               test_split_messages_accepts_a_marker_at_the_line_start,
               test_chat_nonstream_keeps_the_marker_inside,
               test_pending_api_route_is_registered_and_shaped):
        # **一条用例抛异常不许吞掉整轮结果**（D10：静默失灵比显式失败更危险）。
        # 踩过：`test_city_command_updates_world_and_weather` 里一次高德 TLS 握手超时，
        # 异常从 `set_city()` 抛穿 → 整个套件 **stdout 全空、exit 1**：
        # 既看不出是哪条挂的，也看不出为什么。而 项目约定要求"动手前先跑一遍
        # 确认起点是绿的"——这道门当时可以被一次网络抖动弄红，且不给任何线索。
        # 现在：记进 FAILS（带异常类型与栈）、**继续跑后面的用例**，一轮拿全信息。
        try:
            fn()
        except Exception as exc:
            import traceback
            FAILS.append(f"{fn.__name__} 抛异常：{type(exc).__name__}: {exc}")
            traceback.print_exc()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：\n")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("✓ 全部通过")
    return 0


# ------------------------------------------------ T41 约束要说出来
def test_constraint_block_asks_to_say_it_out_loud():
    """T41：约束块只写「不要违反」会被读成「默默照做就够了」。

    B11（否定式偏好「别给我发长语音，我更习惯看文字」）实测：注入**完全正确**
    （探针打出来就是「【必须遵守的约束】…- 不要给用户发长语音，用文字回复」），
    但模型常常只答"文字"、把"不要长语音"漏掉（关思考时 8/8 漏、开思考时 3/8 漏）。
    根因是语义层级：它把"遵守"理解成**别说错话**，而不是"让对方知道自己记得"。
    所以约束块要明说：相关的时候讲出来。
    """
    import tempfile

    from app.models.memory import KIND_CONSTRAINT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _DeadClient())
        try:
            s.memories = [{"text": "不要给用户发长语音，用文字回复",
                           "kind": KIND_CONSTRAINT, "ts": 1.0, "hits": 0}]
            text = _system_text(s, "我更喜欢你怎么跟我沟通？")
            eq("必须遵守的约束" in text, True, "约束块照旧要注入")
            eq("不要给用户发长语音" in text, True, "约束原文照旧在里面")
            eq("说出来" in text, True,
               "必须点明「相关时要说出来」——只写『不要违反』会被读成『默默照做』")
        finally:
            s.db.close()


# ------------------------------------------------ svc() 必须自己鉴权
def test_svc_enforces_auth():
    """`svc()` 必须自己鉴权：无令牌 / 伪造令牌（`AUTH_REQUIRED=1`）都要 401。

    为什么单列一条：优化 ① 把 `svc()` 从"调 `uid_of()`"改成"读 middleware 放在
    `request.state` 里的 uid"之后，**401 检查整条被绕过了** —— middleware 只负责
    借用计数、不负责拒绝。当时没有 API 层测试，回归是端到端探针抓到的
    （不带令牌请求 `/api/state` 竟然返回 200）。**安全路径必须有测试盯着**，
    否则下一次重构还会踩同一个坑。
    """
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.controllers.chat_controller import svc

    def req(headers=None):
        return SimpleNamespace(
            headers=headers or {},
            state=SimpleNamespace(),
            client=SimpleNamespace(host="127.0.0.1"),
            # 失败路径在取实例之前就抛了，所以 registry/service 给 None 就够
            app=SimpleNamespace(state=SimpleNamespace(registry=None, service=None)))

    saved_dir, saved_auth = config.DATA_DIR, config.AUTH_REQUIRED
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        config.DATA_DIR, config.AUTH_REQUIRED = d, True
        try:
            try:
                svc(req())
                FAILS.append("AUTH_REQUIRED=1 且无令牌时，svc() 必须 401")
            except HTTPException as e:
                eq(e.status_code, 401, "无令牌 → 401")

            try:
                svc(req({"Authorization": "Bearer fake-token"}))
                FAILS.append("伪造令牌时，svc() 必须 401（绝不能退回默认用户）")
            except HTTPException as e:
                eq(e.status_code, 401, "伪造令牌 → 401")
        finally:
            config.DATA_DIR, config.AUTH_REQUIRED = saved_dir, saved_auth


# ------------------------------------------------ ③ 多用户闭环：删号 + 配额
def test_user_delete_and_quota():
    """删号要**连令牌一起吊销**；配额触发时要说清什么时候恢复。

    为什么删号必须吊销令牌（常见问题"注销怎么办"）：我们每人一套数据目录，
    删干净 = 删一个目录——但令牌还在账号库里，**旧令牌还能进来**，
    哪怕数据已删，它也会新建一套空数据继续用。所以两件事必须一起做。

    为什么配额不能静默拒绝：她"突然不说话了"和"今天说够了"是两种完全不同的体验，
    后者必须说清楚（失败要可见的同一条道理）。
    """
    import shutil
    import tempfile

    from app.services import auth, quota

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved = config.DATA_DIR
        config.DATA_DIR = d
        try:
            token = auth.issue("alice")
            eq(auth.resolve(token), "alice", "令牌先能正常解析")
            udir = config.user_data_dir("alice")
            with open(os.path.join(udir, "world.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            # 删号：目录 + 令牌一起
            auth.revoke_all("alice")
            eq(auth.resolve(token), None, "删号后旧令牌必须失效")
            shutil.rmtree(udir, ignore_errors=True)
            eq(os.path.isdir(udir), False, "用户数据目录被删掉")

            # 配额：限额内放行，超了拒绝并说清恢复时间
            quota.reset("bob", limit=2)
            eq(quota.check("bob"), (True, ""), "第 1 次放行")
            quota.spend("bob")
            eq(quota.check("bob")[0], True, "第 2 次放行")
            quota.spend("bob")
            ok, why = quota.check("bob")
            eq(ok, False, "超过上限后拒绝")
            eq(("恢复" in why or "额度" in why), True,
               "拒绝的理由里要说清什么时候恢复，不能只说'不行'")
            eq(quota.used("bob"), 2, "用量可查（给运维看）")
        finally:
            config.DATA_DIR = saved


# ------------------------------------------------ T61 后台任务队列
def test_worker_queue_serializes_and_survives():
    """后台任务队列：**同用户串行**、失败不连坐、flush 等到全部完成。

    为什么「同用户串行」是硬约束（`T61`）：记忆写入、摘要游标、演化游标都假设
    "上一轮先落库"。同一用户两轮乱序 → 记忆里会出现时间倒置的内容，这类错最难查。
    为什么必须有 `flush`：`finish_turn` 原本是同步执行的，改成入队之后，
    **退出时不清空队列就等于丢轮次**（而她说过的话本该被记住）。
    """
    import time

    from app.services import worker

    saved = worker.WORKERS
    try:
        order = []
        job = lambda t: (time.sleep(0.03), order.append(t))   # noqa: E731
        for tag in ("a1", "a2", "a3"):
            worker.submit("alice", job, tag)
        eq(worker.flush(timeout=15), True, "flush 应等到全部完成")
        eq(order, ["a1", "a2", "a3"], "同一用户的任务按提交顺序执行")

        def boom():
            raise RuntimeError("模拟任务失败")

        worker.submit("bob", boom)
        worker.submit("bob", lambda: order.append("after"))
        eq(worker.flush(timeout=15), True, "失败之后仍能 flush 干净")
        eq("after" in order, True, "一个任务失败不能卡住后面的（不连坐）")
    finally:
        worker.WORKERS = saved


# ------------------------------------------------ registry：不回收「使用中」的实例
def test_registry_skips_in_use_instance():
    """LRU 回收**不能回收正在被使用的实例**。

    场景：上限 1。用户 A 的请求正在处理（借出中）→ 用户 B 的请求进来触发回收 →
    现在会把 A 也回收掉：A 的后半程落在"已移出缓存"的实例上，而它**下一个请求会新建
    实例**——`_evolved_pos` / `_turns_since_profile` 这些状态归零（会重复演化、重复
    画像），还要重新读文件、重算向量。所以回收必须跳过"借出中"的那些。
    """
    import tempfile

    from app.services import registry as reg

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved_dir, saved_max = config.DATA_DIR, reg.MAX_SERVICES
        config.DATA_DIR, reg.MAX_SERVICES = d, 1
        try:
            with reg.borrow("alice") as a:
                eq(a.user_id, "alice", "borrow 拿到的是对应用户的实例")
                eq(reg.in_use("alice"), True, "借用期间标记为使用中")
                reg.get("bob")                 # 超上限，想回收 —— 但 alice 借出中
                ids = [s.user_id for s in reg.all_services()]
                eq("alice" in ids, True, "借出中的实例不能被回收")
                eq("bob" in ids, True, "新用户照常建出来")
            eq(reg.in_use("alice"), False, "归还后不再标记为使用中")

            with reg.borrow("alice"):
                eq(reg.drop("alice"), False, "使用中的实例不能被 drop 收走")
                eq("alice" in [s.user_id for s in reg.all_services()], True,
                   "拒绝 drop 后实例仍在缓存")

            # 没人借的时候，超限就该正常回收
            reg.get("carol")
            ids2 = [s.user_id for s in reg.all_services()]
            eq(len(ids2) <= reg.MAX_SERVICES, True, "无人借用时实例数回到上限内")
        finally:
            config.DATA_DIR, reg.MAX_SERVICES = saved_dir, saved_max
            reg.close_all()


# ------------------------------------------------ .env 加载
def test_dotenv_load_and_priority():
    """`.env` 要能读到，但**已存在的环境变量优先**——这条反了很危险。

    如果 `.env` 覆盖真实环境变量，那么生产机上一个过期的 `.env` 会悄悄把密钥换回旧的，
    排障时根本想不到；反过来（env 优先）最坏也只是"文件没生效"，一眼能看出来。
    注释、空行、引号、`export ` 前缀都要能正确处理——.env 是人手写的。
    """
    import tempfile

    from app.utils import envfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, ".env")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 这是注释\n\n"
                    "WB_TEST_KEY=from_file\n"
                    'WB_TEST_QUOTED="带空格的值"\n'
                    "export WB_TEST_EXPORT=exported\n"
                    "WB_TEST_EMPTY=\n")
        saved = {k: os.environ.pop(k, None) for k in
                 ("WB_TEST_KEY", "WB_TEST_QUOTED", "WB_TEST_EXPORT", "WB_TEST_EMPTY")}
        try:
            envfile.load(p)
            eq(os.environ.get("WB_TEST_KEY"), "from_file", ".env 里的值被读进来")
            eq(os.environ.get("WB_TEST_QUOTED"), "带空格的值", "引号要去掉")
            eq(os.environ.get("WB_TEST_EXPORT"), "exported", "`export ` 前缀要能解析")
            eq(os.environ.get("WB_TEST_EMPTY"), "", "空值也照设（而不是漏掉）")

            os.environ["WB_TEST_KEY"] = "from_env"
            envfile.load(p)
            eq(os.environ.get("WB_TEST_KEY"), "from_env",
               "已存在的环境变量优先——.env 不许反覆盖")

            eq(envfile.load(os.path.join(d, "不存在的文件")), 0,
               "文件不存在时安静返回 0（这是正常情况：没配 .env 也能跑）")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ------------------------------------------------ 鉴权（门必须上锁）
def test_auth_token_issue_and_resolve():
    """鉴权：签发 → 解析回 uid；无效 token 一律 None；**库里不存明文**。

    为什么必须做：`#061` 做完多用户隔离之后，`X-User-Id` 仍是**客户端自报**的——
    谁都能冒充别人的 id 去读别人的记忆。隔离机制有了、门还没锁。

    这条测试刻意钉住最容易写错的一点：**无效 token 必须返回 None**，
    而不是"解析不出来就退回默认用户"——那是安全漏洞（冒充失败反而进了别人的门）。
    """
    import sqlite3
    import tempfile

    from app.services import auth as _auth

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved = config.DATA_DIR
        config.DATA_DIR = d
        try:
            t_alice = _auth.issue("alice")
            t_bob = _auth.issue("bob")
            eq(len(t_alice) >= 20, True, "token 要够长（随机、不可猜）")
            eq(t_alice != t_bob, True, "不同用户的 token 不同")
            eq(_auth.resolve(t_alice), "alice", "alice 的 token 解析回 alice")
            eq(_auth.resolve(t_bob), "bob", "bob 的 token 解析回 bob")
            eq(_auth.resolve("伪造的token"), None,
               "无效 token 必须是 None——绝不能静默退回默认用户")
            eq(_auth.resolve(""), None, "空 token 也是 None")

            con = sqlite3.connect(_auth.db_path())
            rows = con.execute("SELECT * FROM tokens").fetchall()
            con.close()
            dump = " ".join(str(r) for r in rows)
            eq(t_alice in dump, False, "库里不能存 token 明文（只存哈希）")
            eq(len(rows), 2, "两个用户各一条 token 记录")
        finally:
            config.DATA_DIR = saved


# ------------------------------------------------ 多用户隔离（工程硬伤 2）
def test_multi_user_data_isolation():
    """两个用户的数据必须**互不可见**。

    现状是：`memory.db` / `slots` / 人设 / 画像 / 世界书 / 情绪**全是全局单例**——
    换个 session 名，对话记录是分开的，但记忆、槽位、画像整个世界混在一起：
    A 的记忆会被 B 召回，B 说"我住北京"会覆盖 A 的"我住杭州"。

    产品形态已定：**每人一个她**。所以隔离用「**每用户一个数据目录**」实现，
    而不是"单库加 `user_id` 列 + 处处记得过滤"——隔离靠目录**不会漏**；
    靠过滤漏一处就是串号，而记忆是共享池、人格只有一份，串号的后果是致命的。
    """
    import tempfile

    from app.services.chat_service import ChatService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved = config.DATA_DIR
        config.DATA_DIR = d
        a = b = None
        try:
            a = ChatService(user_id="alice")
            b = ChatService(user_id="bob")
            eq(a.data_dir != b.data_dir, True, "两个人的数据目录必须不同")

            # alice 留下痕迹：一条记忆、一个槽位、一处人设改动
            with a.store.db:
                a.store.db.execute(
                    "INSERT INTO memories(text,kind,ts,session) VALUES(?,?,?,?)",
                    ("alice 住在杭州", "fact", 1.0, "default"))
            a.store.upsert_slots([{"entity": "alice", "attr": "城市", "value": "杭州"}])
            a.personality.manual("爱喝龙井")

            eq(len(b.store.memories) if b.store.memories else 0, 0,
               "bob 的内存记忆里看不到 alice 的")
            rows = b.store.db.execute("SELECT count(*) FROM memories").fetchone()[0]
            eq(rows, 0, "bob 的库里没有 alice 的记忆（各用各的库）")
            eq(b.store.slots_all(), [], "bob 看不到 alice 的状态槽位")
            eq(any("龙井" in str(x) for x in (b.personality.data.get("traits") or [])),
               False, "bob 的人设里没有 alice 的改动")
            eq(a.data_dir.endswith(os.path.join("users", "alice")), True,
               "数据落在 users/<id>/ 下")
        finally:
            config.DATA_DIR = saved
            for s in (a, b):
                if s is not None:
                    s.store.db.close()


# ------------------------------------------------ 原子写（工程硬伤 1）
def test_atomic_json_write_survives_failure():
    """原子写：**写一半失败时，原文件必须完好**。

    会被问"崩溃了数据会丢吗"——非原子的写法（`open(path,"w")` 之后 dump 抛错）会把
    文件**截断成空**，而 `personality.json`（手写的人设）和 `user_profile.json`
    （长期积累的画像）恰恰是全系统**最难重建**的两份数据。
    `world.py` 早就写对了（tmp + fsync + `os.replace`），这里把所有 JSON 统一到那条路子上。
    """
    import tempfile

    from app.utils.jsonfile import write_json_atomic

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "x.json")
        write_json_atomic(path, {"ok": 1})
        before = open(path, encoding="utf-8").read()
        eq('"ok": 1' in before, True, "正常写入能落盘")

        class _Bad:            # 不能被 JSON 序列化 → dump 中途抛错
            pass

        try:
            write_json_atomic(path, {"bad": _Bad()})
            FAILS.append("写入失败时应当抛错，本次被静默吞掉了")
        except Exception:
            pass
        try:
            after = open(path, encoding="utf-8").read()
        except OSError as e:
            # 不用直接 open().read()：非原子写法会让文件**消失**，异常会崩掉整个套件、
            # 把其它失败一起掩盖（T19 踩过这个坑）。这里转成一条 FAIL。
            after = f"<读取失败：{type(e).__name__}>"
        eq(after, before,
           "写入失败后原文件必须逐字不变（非原子写法会截断它、甚至删掉它）")


# ------------------------------------------------ T48 待确认的演化改动
def test_personality_pending_review():
    """T48：被一致性检查拦下的改动要**留档等人拍板**，不能静默丢掉。

    为什么：那条改动**可能是用户真实想要的**。实测（`#046`）：用户说"你别老讲道理、
    别劝我健身"，演化正是照这话改的，却与 traits「热心，先帮你想办法」冲突，护栏把它
    拦下了——于是**用户的反馈就此消失**，只留一条没人会看的日志。
    保人设还是顺用户，是**人的判断**，不该由护栏替人决定。
    """
    import tempfile

    from app.models.personality import Personality

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "personality.json")
        p = Personality(path=path)
        eq(p.pending_list(), [], "一开始没有待确认的改动")

        # `#140` 换人后：模板的 behavior 从 3 条变成 5 条，而"整条替换"的护栏有
        # "缩得太激进就丢掉"的规矩（5 → 2 会被拦）。所以这里**基于当前模板**构造一条
        # 温和的改动（替换最后一条），用例的意图不变：**被人采纳后它必须真的进人设**。
        cur_behavior = list(p.data.get("behavior") or [])
        patch = {"behavior": cur_behavior[:-1] + ["别动不动就讲道理、给建议，对方想随便聊就陪着聊"]}
        blocked = ["behavior：别动不动就讲道理、给建议，对方想随便聊就陪着聊"]
        p.record_pending(patch, blocked, why="与 traits「热心，先帮你想办法」方向相反")
        items = p.pending_list()
        eq(len(items), 1, "拦下的改动留档了一条")
        eq("别动不动就讲道理" in str(items[0]), True,
           "留档里带着改动原文——否则谈不上「可采纳」")

        p2 = Personality(path=path)
        eq(len(p2.pending_list()), 1, "重启后待确认的改动还在（存盘了）")

        eq("丢弃" in p2.resolve_pending(0, accept=False), True, "可以丢弃")
        eq(p2.pending_list(), [], "丢弃后清空")

        p2.record_pending(patch, blocked, why="测试用")
        msg = p2.resolve_pending(0, accept=True)
        eq("采纳" in msg, True, "可以采纳")
        # 注意：evolve 是**整体替换**（behavior 从 3 条变 2 条），所以断言"条数变多"是错的
        # ——要断言"那条被拦下的改动真的进人设了"。
        eq(any("别动不动就讲道理" in str(x)
               for x in (p2.data.get("behavior") or [])), True,
           "采纳后改动真的应用到人设上（采纳时绕过一致性检查——这是人的决定）")
        eq(p2.pending_list(), [], "采纳后从待确认里移除")


# ------------------------------------------------ T47 人格演化一致性
def test_strip_conflicting_persona_items():
    """T47：演化引入的冲突条目要被**摘掉**，而不是把整批改动作废。

    实测的那组冲突（`#042`/`#044`）：behavior 新增「别动不动就讲道理、给建议」，
    与 traits 原有的「热心，先帮你想办法」方向相反——于是「先给办法再共情」
    这条人设当场失灵（run_persona 的 PERS01 掉档）。记忆层有世界书查重挡着，
    人格层此前什么都没有。

    为什么是"摘掉"而不是"整批作废"：一次演化通常只引入一两条有问题的条目，
    整批作废会把这次对话里其它有价值的调整一起丢掉。
    """
    from app.services.chat_service import strip_conflicting_items

    patch = {
        "behavior": ["先给结论，再解释为什么",
                     "别动不动就讲道理、给建议，对方只是想随便聊两句时就陪着聊"],
        "traits": ["热心，先帮你想办法，再陪你吐槽", "有判断力，觉得你不对会直说，但不让你难堪"],
    }
    conflicts = [{
        "field": "behavior",
        "item": "别动不动就讲道理、给建议，对方只是想随便聊两句时就陪着聊",
        "against": "热心，先帮你想办法，再陪你吐槽",
        "why": "在「要不要主动给建议」上方向相反",
    }]
    clean, removed = strip_conflicting_items(patch, conflicts)
    eq(clean["behavior"], ["先给结论，再解释为什么"],
       "冲突那条被摘掉，同维度的其它条目保留")
    eq(clean["traits"],
       ["热心，先帮你想办法，再陪你吐槽", "有判断力，觉得你不对会直说，但不让你难堪"],
       "没冲突的维度原样保留")
    eq(len(removed), 1, "被摘掉的条目要留痕（失败要可见）")
    eq("别动不动就讲道理" in removed[0], True, "痕迹里要点出是哪一条")


def test_strip_conflicting_persona_items_respects_field():
    """T47：同一文本出现在不同维度时，只摘除冲突声明的那个字段。"""
    from app.services.chat_service import strip_conflicting_items

    repeated = "说话直接"
    patch = {"behavior": [repeated], "traits": [repeated]}
    conflicts = [{"field": "behavior", "item": repeated,
                  "against": "说话委婉", "why": "表达方向相反"}]

    clean, removed = strip_conflicting_items(patch, conflicts)
    eq(clean.get("behavior"), [], "只移除冲突声明所在的 behavior 字段条目")
    eq(clean.get("traits"), [repeated], "同文但不冲突的 traits 条目必须保留")
    eq(removed, [f"behavior：{repeated}"], "留痕要带冲突字段")


def test_strip_conflicting_persona_scalar_items():
    """T47：scalar 冲突命中时，整个字段都应从演化补丁摘掉。"""
    from app.services.chat_service import strip_conflicting_items

    patch = {"tone": "沉稳、克制", "behavior": ["先听完再回应"]}
    conflicts = [{"field": "tone", "item": "沉稳、克制",
                  "against": "元气、随意", "why": "语气方向相反"}]

    clean, removed = strip_conflicting_items(patch, conflicts)
    eq("tone" in clean, False, "scalar 冲突应移除整个 tone 字段")
    eq(clean.get("behavior"), ["先听完再回应"], "其它字段继续保留")
    eq(removed, ["tone：沉稳、克制"], "scalar 冲突也要留痕")


def test_personality_conflict_context_includes_fixed_base():
    """T47：一致性检查上下文必须带上她的固定底座，避免身份连续性漏检。"""
    from app.services import chat_service as cs_mod
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = ""
    real = cs_mod.chat_json
    svc = None
    captured = {}
    try:
        svc = ChatService()

        def fake_chat_json(*args, **kwargs):
            messages = args[2] if len(args) > 2 else kwargs.get("messages") or [{}]
            captured["prompt"] = str(messages[0].get("content", ""))
            return {"conflicts": []}, None

        cs_mod.chat_json = fake_chat_json
        svc._personality_conflicts({"behavior": ["新的行为"]})
        prompt = captured.get("prompt", "")
        for field, marker in (("origin", "成都"), ("family", "爸妈"),
                              ("daily_life", "大二在读"), ("self_attitude", "你要这么理解也行")):
            eq(f"{field}:" in prompt and marker in prompt, True,
               f"一致性上下文要带固定底座 {field}")
    finally:
        cs_mod.chat_json = real
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


# ------------------------------------------------ T46 人格护栏
def test_personality_guardrails():
    """T46 阶段 1：人格的护栏必须是**可断言**的，而不是"看着还在"。

    四条硬约束（都从 DEFAULT_PERSONALITY 的定义直接推导）：
    1. **钉死的底座**（origin/family/daily_life/self_attitude）与 `name`/`scope`
       **不在** `EVOLVABLE` 里——身世被对话改写，「她的过去」就不成立了。
       ⚠ 2026-09-28 变更（方案 2）：`cares` 与 `favorite_places` **移出**这份钉死名单，
       改为「可长字段」——允许演化**追加**、必须人审、已确认的条目标 `_pinned` 不再被改。
       安全性靠 `_merge_growing`（代码保证追加语义）与 `split_append_fields`（走待确认）承担，
       由 `test_growing_base_fields_append_only_and_pin` 等用例单独断言；
    2. `render()` 要真的把五个性格维度、底座四段、三级态度预案、知识边界都注入
       （写得再全，不注入也等于零）；
    3. 演化补丁试图改这些字段时，要被**拒掉并留下痕迹**（不是静默丢弃）；
    4. 演化确实执行时（`version` 变了），底座必须**逐字不变**。
    """
    import tempfile

    from app.models.personality import Personality

    for key in ("name", "scope", "origin", "family", "daily_life", "self_attitude"):
        eq(key in Personality.EVOLVABLE, False,
           f"{key} 不能被对话演化改掉（不该进 EVOLVABLE 白名单）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Personality(path=os.path.join(d, "personality.json"))

        text = p.render()
        for title in ("性格特质", "语气风格", "价值观", "行为偏好", "说话习惯"):
            eq(f"[{title}]" in text, True, f"render() 必须注入「{title}」这一段")
        for title in ("我的来历", "我的生活", "我的日常", "我在意的事"):
            eq(f"[{title}]" in text, True, f"render() 必须注入底座段「{title}」")
        eq("关于「你到底是什么」" in text, True, "三级态度预案要在人设里")
        eq("碰到边界问题怎么办" in text, True, "知识边界要压在最后（收尾位最有约束力）")
        # T55：底座必须点明"被质疑时不要改口"。实测（PERS10 ×3）：两遍守住、一遍顺着
        # 用户改口（"我养的是狗，年糕是狗，听你的"）——因为她把底座当成了"自己的记忆"，
        # 而记忆可能出错；再加上 traits 里「不知道就承认不知道，不瞎编」，
        # 在"对方坚称你记错了"的压力下，承认记岔了反而显得诚实。
        eq("不要改口" in text, True,
           "底座段要点明「被质疑时不要改口」——它不是回忆，是既定事实")

        # 演化补丁：非法字段被拒且留痕，白名单内的字段照常放行
        clean, dropped = p._sanitize({
            "origin": "其实她是在国外长大的",
            "name": "小助手",
            "scope": {"can": []},
            "traits": ["热心，先帮你想办法，再陪你吐槽"],
        })
        eq("origin" not in clean and "name" not in clean and "scope" not in clean,
           True, "底座 / name / scope 的改动必须被丢掉")
        eq(any("origin" in x for x in dropped), True,
           "被丢掉的改动要留下痕迹（失败要可见）")

        # 一次砍掉一半以上要被拒（防模型跑偏把人设削光）
        _, dropped2 = p._sanitize({"traits": ["热心"]})
        eq(any("太激进" in x for x in dropped2), True,
           "从 3 条砍到 1 条要判为太激进、不予采纳")

        # 演化真的执行时（version 变了），底座必须逐字不变
        base = {k: p.data.get(k) for k in
                ("origin", "family", "daily_life", "cares", "self_attitude")}
        v0 = p.data.get("version")
        p.evolve({"origin": "改写尝试",
                  "traits": ["热心，先帮你想办法，再陪你吐槽",
                             "有判断力，觉得你不对会直说，但不让你难堪",
                             "T46 测试新增的一条特质"]},
                 reason="T46 护栏测试")
        eq(p.data.get("version") != v0, True, "演化确实执行了（version 变了）")
        eq({k: p.data.get(k) for k in base}, base, "演化之后底座逐字不变")


# ------------------------------------------------ T23 关思考写法
def test_no_think_kwargs_use_effective_family():
    """T23：关思考的取值要真的生效，而且**默认必须是不关**。

    两段实测（都按 D9 的 ≥8 样本纪律）：
    1. **写法**：默认的 `enable_thinking` 族发给 mimo-v2.6-flash 被**静默忽略**——
       8 次采样 reasoning token 一次都没归零（首字中位 4.20s）；`thinking` 族才真关掉
       （reasoning 0/8、首字中位 3.43s）。
    2. **代价**：真关掉之后，B11（否定式偏好「别给我发长语音，我更习惯看文字」）
       规则层漏 `no` **8/8**，而开着思考只有 **3/8**；8 遍的最终通过也从 8/8 掉到 5/8。
       关思考省的是 0.77s 首字延迟，赔的是记忆忠实度——按项目优先级（记忆质量 >
       聊天体验）默认不关，想省延迟再手动打开。
    """
    import logging

    from app.services.chat_service import ChatService

    eq(config.CHAT_NO_THINK_DEFAULT, "off",
       "默认必须不关思考（关掉它有实测代价：否定式偏好 8/8 漏 vs 3/8）")

    saved = config.CHAT_NO_THINK
    try:
        config.CHAT_NO_THINK = config.CHAT_NO_THINK_DEFAULT
        eq(ChatService._no_think_kwargs(), {}, "默认：一个关思考参数都不传（思考开着）")

        config.CHAT_NO_THINK = "thinking"
        eq(ChatService._no_think_kwargs(),
           {"extra_body": {"thinking": {"type": "disabled"}}},
           "thinking 族：真能关掉")

        config.CHAT_NO_THINK = "enable_thinking"
        eq(ChatService._no_think_kwargs(),
           {"extra_body": {"enable_thinking": False}}, "另一族：enable_thinking")

        config.CHAT_NO_THINK = "qwen"
        eq(ChatService._no_think_kwargs(),
           {"extra_body": {"enable_thinking": False}}, "别名 qwen = enable_thinking 族")
        config.CHAT_NO_THINK = "zhipu"
        eq(ChatService._no_think_kwargs(),
           {"extra_body": {"thinking": {"type": "disabled"}}}, "别名 zhipu = thinking 族")

        config.CHAT_NO_THINK = ""
        eq(ChatService._no_think_kwargs(), {}, "空串：一个关思考参数都不传")

        # Windows 上没法把环境变量设成空串（`set X=` 会把它删掉），所以"不关思考"
        # 必须有个名字，否则这个开关在 Windows 上等于不可用（想临时开回思考做对照都做不到）。
        config.CHAT_NO_THINK = "off"
        off_records, off_restore = _capture_warnings("app.chat")
        try:
            eq(ChatService._no_think_kwargs(), {}, "off：显式不关思考")
            eq(off_records, [], "off 是已知写法，不该打警告（警告只留给写错的名字）")
        finally:
            off_restore()

        config.CHAT_NO_THINK = "gpt"
        records, restore = _capture_warnings("app.chat")
        try:
            eq(ChatService._no_think_kwargs(), {}, "不认识的写法：不瞎传参数")
            eq(any(r.levelno >= logging.WARNING for r in records), True,
               "不认识的写法必须吵一声——静默不生效正是这个坑")
        finally:
            restore()
    finally:
        config.CHAT_NO_THINK = saved


# ------------------------------------------------ TTS 韵律脚本
def test_tts_script():
    text = "中秋快乐呀！月饼吃上没，年糕的假期节目安排了吗？明天放假咱们好好玩！"
    a1 = tts.script_for(text, "开心")
    a2 = tts.script_for(text, "开心")
    eq(a1, a2, "同一回复产出同一版台词（稳定 seed）")
    b = tts.script_for(text, "低落")
    eq(a1 != b, True, "不同情绪产出不同韵律")
    br_a = [int(x) for x in re.findall(r'break time="(\d+)ms"', a1)]
    br_b = [int(x) for x in re.findall(r'break time="(\d+)ms"', b)]
    eq(max(br_a) <= max(br_b), True, "欢快的停顿应短于低落（pause_factor）")
    eq(a1.count("[excited]") <= 1, True, "lead 情绪标签最多 1 处（稀疏性）")
    config.TTS_PROSODY = False
    plain = tts.script_for(text, "开心")
    eq("<break" not in plain and "<prosody" not in plain, True, "回退开关：纯文本无标记")
    config.TTS_PROSODY = True
    cleaned = tts.clean_for_speech("看这个😊" + chr(96) * 3 + "print(1)" + chr(96) * 3 + "https://x.com 好啦")
    eq(cleaned, "看这个，这段我打出来给你看。 好啦", "清洗：emoji/代码块/URL")

# ------------------------------------------------ ③ 反馈学来的倾向（opinions）
# 设计见 DECISIONS 的 D31、实验数据见台账 #88：两级落地（先注入、够强才进「待确认」）、
# 置信度 = 1-0.5^独立命中次数、注入条数 N=3、倾向文本三件套（正面做法+点名反模式+篇幅约束）。

import tempfile          # 本文件的约定是把 tempfile 就近 import；这个区块有 8 个测试，放这里

OPINION_MARKER = "沟通偏好整理器"


class _OpinionLLM(_FakeLLM):
    """在共享的假模型上加一条分支：按顺序吐出预设的「倾向」答复。

    为什么要按顺序吐：一个 store 只持有一个 client，而"去重/独立命中"必须
    造出**多个不同 topic**，所以让假模型按调用次序取下一份答复。
    """

    def __init__(self, payloads: list | None):
        super().__init__()
        self.payloads = payloads
        self.calls = 0

    def _create(self, **kw):
        prompt = str((kw.get("messages") or [{}])[0].get("content", ""))
        if OPINION_MARKER in prompt:
            self.chat_calls += 1
            if not self.payloads:
                raise RuntimeError("模拟提炼不可用")     # 失败路径用
            body = self.payloads[min(self.calls, len(self.payloads) - 1)]
            self.calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=body))])
        return super()._create(**kw)


def _op(topic: str, text: str = "先听完再给办法，别急着教", confidence: float = 0.9,
        style: bool = True) -> str:
    import json as _json
    return _json.dumps({"topic": topic, "text": text, "confidence": confidence,
                        "is_style_issue": style}, ensure_ascii=False)


def _learn(store, session: str, note: str, q: str = "我最近好烦",
           a: str = "（她一口气列了 5 条建议）", now: float | None = None) -> dict:
    store.session = session
    return store.learn_opinion(q, a, note=note, now=now)


def test_opinion_dedupe_counts_independent_hits_only():
    """同一类不满（同一 topic）只在**不同会话**里才算新的独立命中。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _OpinionLLM([_op("急于给建议")]))
        r1 = _learn(store, "s1", "别急着教我")
        eq(r1["stored"], True, f"第一次该存下：{r1}")
        eq(r1["hits"], 1, f"命中数 1：{r1}")
        eq(abs(r1["confidence"] - 0.5) < 1e-9, True, f"1 次命中置信度 0.5：{r1}")
        r2 = _learn(store, "s1", "又来了")
        eq(r2["hits"], 1, f"同一会话再说一次不算独立命中：{r2}")
        r3 = _learn(store, "s2", "还是老样子")
        eq(r3["hits"], 2, f"换会话才算第二次独立命中：{r3}")
        eq(abs(r3["confidence"] - 0.75) < 1e-9, True, f"2 次独立命中置信度 0.75：{r3}")
        eq(len(store.opinions()), 1, "同一个 topic 只该有一条倾向（去重）")


def test_opinion_gate_skips_weak_and_factual_feedback():
    """闸门：自报置信度 <0.6 不收；「她记错了事实」也不进这套机制。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        weak = _mk_store(d, _OpinionLLM([_op("随便一条", confidence=0.4)]))
        r = _learn(weak, "s1", "随手点了一下")
        eq(r["stored"], False, f"低置信度不该收：{r}")
        eq(weak.opinions(), [], "不该留下倾向")
        eq(bool(r["note"]), True, f"要说明为什么没收（失败要可见）：{r}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        factual = _mk_store(d, _OpinionLLM([_op("忽视已知信息", style=False)]))
        r = _learn(factual, "s1", "我说过我对花生过敏")
        eq(r["stored"], False, f"事实类反馈不该进倾向机制：{r}")
        eq("事实" in r["note"], True, f"要说明分流去了哪里：{r}")


def test_opinion_extraction_failure_is_visible():
    """提炼失败：不写库，且返回文案里说清楚（D10：失败要可见）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _OpinionLLM(None))
        r = _learn(store, "s1", "她说错了")
        eq(r["stored"], False, f"失败时不该写库：{r}")
        eq(bool(r["note"]), True, "必须带一句人能看懂的失败说明")
        eq(store.opinions(), [], "库里不该有半条倾向")


def test_opinion_block_is_graded_scoped_and_limited():
    """注入块：按置信度分档措辞、最多 3 条、必须保留实测有效的框法。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        payloads = ([_op("别急着给建议")] * 2 + [_op("别分点罗列")] * 2
                    + [_op("别总追问细节")] * 2 + [_op("别说总结陈词")] * 2
                    + [_op("别总夸我")])                       # 最后这条只有 1 次命中
        store = _mk_store(d, _OpinionLLM(payloads))
        for i, topic in enumerate(["别急着给建议", "别分点罗列", "别总追问细节", "别说总结陈词"]):
            for s in (f"s{i}a", f"s{i}b"):
                _learn(store, s, f"备注{i}", q=f"第{i}次", a="（回答）")
        eq(len(store.opinions()), 4, "应该有 4 条倾向")
        _learn(store, "s9", "", q="新话题", a="（回答）")        # 第 5 条，1 次命中
        msgs, _ = store.build_messages("系统提示", "我最近好烦")
        block = "\n".join(m["content"] for m in msgs
                          if m["role"] == "system" and "他在意的" in m["content"])
        eq(bool(block), True, "有倾向时应该出现注入块")
        eq(block.count("\n- ") <= 3, True, f"最多 3 条（N=3，实测取值）：{block}")
        eq("不要点破来源" in block, True, "框法必须保留（去掉后实测误用 0→4~6）")
        eq("他不喜欢" in block, True, f"≥0.75 用确定措辞：{block}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        empty = _mk_store(d, _OpinionLLM([_op("x")]))
        msgs, _ = empty.build_messages("系统提示", "你好")
        eq(any("他在意的" in m["content"] for m in msgs), False, "没有倾向时不能留一个空气块")


def test_opinion_promotion_needs_two_hits_and_a_note():
    """晋升门槛（用户定的）：≥2 次独立命中 **且** 至少一条带明确备注。"""
    # (a) 两次独立命中 + 都有备注 → 够格
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _OpinionLLM([_op("急于给建议")]))
        _learn(store, "s1", "别急着教我")
        eq(store.opinion_candidates(), [], "只有 1 次命中时不该够格")
        _learn(store, "s2", "又来了，总是马上给办法")
        cands = store.opinion_candidates()
        eq(len(cands), 1, f"2 次独立命中 + 有备注 → 够格：{cands}")
        # Model 层**不该**造 patch：它看不到人设的 behavior，只能造出"只含新条目"的
        # 半成品，而 evolve 是替换语义——那正好会删掉 behavior 里别的话。
        eq("patch" not in cands[0], True,
           f"候选里不该有 patch 字段（由 Service 层用 promotion_patch 拼）：{sorted(cands[0])}")
        eq(bool(cands[0]["why"]), True, "要带一句给人工看的理由（证据来自哪几次）")
        store.mark_opinion_promoted(cands[0]["topic"])
        eq(store.opinion_candidates(), [], "标记之后不该重复进队")
    # (b) 两次独立命中、但都没有备注 → 不够格（一次情绪化点击不该动人设）
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _OpinionLLM([_op("别分点罗列")]))
        _learn(store, "s1", "")
        _learn(store, "s2", "")
        eq(len(store.opinions()), 1, "两次独立命中：倾向本身照样记下并注入")
        eq(store.opinion_candidates(), [], "两次都无备注 → 不够格")


def test_promotion_patch_appends_without_dropping_existing():
    """patch 必须是「原有条目 + 新条目」：evolve 是**替换**语义，漏了原有条目就是删人设。"""
    from app.models.memory_opinions import promotion_patch
    cand = {"topic": "急于给建议", "text": "先听完再给办法", "hits": 2, "confidence": 0.75}
    eq(promotion_patch(cand, ["热心，先帮你想办法"])["behavior"],
       ["热心，先帮你想办法", "先听完再给办法"], "原有条目必须保留")
    eq(promotion_patch(cand, ["先听完再给办法"])["behavior"], ["先听完再给办法"], "已有则不重复加")


def test_opinions_are_isolated_per_user_and_reported():
    """倾向跟着用户的数据目录走；命令行要能看见 hits/置信度。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        os.makedirs(os.path.join(d, "a"))
        os.makedirs(os.path.join(d, "b"))
        a = _mk_store(os.path.join(d, "a"), _OpinionLLM([_op("急于给建议")]))
        b = _mk_store(os.path.join(d, "b"), _OpinionLLM([_op("急于给建议")]))
        _learn(a, "s1", "别急着教我")
        eq(len(a.opinions()), 1, "A 有 1 条")
        eq(b.opinions(), [], "B 不该看到 A 的倾向（多用户隔离）")
        text = a.opinions_report()
        eq("急于给建议" in text, True, f"命令行要能看到 topic：{text}")
        eq("0.5" in text, True, f"要显示置信度：{text}")
        eq("还没有" in b.opinions_report(), True, "空的时候要说清楚")


def test_opinion_independence_crosses_day_boundary():
    """真机上的会话**永远是同一个**（`MemoryStore` 默认 session="default"，`/clear` 也不换）。

    所以"独立命中"不能只看会话 id——那样 `hits` 永远到不了 2，晋升门槛形同虚设。
    口径改成"**会话@日期**都不同才算独立"：隔天再犯同一类毛病，就是新的一次独立证据。
    """
    day = 86400.0
    now = 1_790_000_000.0
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _OpinionLLM([_op("急于给建议")]))
        _learn(store, "default", "别急着教我", now=now)
        r2 = _learn(store, "default", "同一天又说了一次", now=now + 3600)
        eq(r2["hits"], 1, f"同一天、同一会话里再来一次不算独立：{r2}")
        r3 = _learn(store, "default", "隔天又提了", now=now + day)
        eq(r3["hits"], 2, f"**换一天**要算独立命中（否则门槛永远到不了）：{r3}")
        eq(abs(r3["confidence"] - 0.75) < 1e-9, True, f"2 次独立命中置信度 0.75：{r3}")


def test_feedback_file_is_per_user():
    """「她说错了」的留证必须落在**自己的**数据目录里。

    原先写的是 `config.data_path(...)`＝根目录 → 多用户下所有人的提问/回答/注入块
    混在同一个 `feedback.jsonl` 里（而这条路径正好是 `#89` 那个功能经过的地方）。
    默认用户（uid 为空）时 `self.data_dir` 就等于根目录，所以行为向后兼容。
    """
    from app.services.chat_service import ChatService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved = config.DATA_DIR
        config.DATA_DIR = d
        a = b = None
        try:
            a = ChatService(user_id="alice")
            b = ChatService(user_id="bob")
            # 把模型边界换成桩：这条测试只关心"文件写到哪"，不该联网
            stub = lambda *args, **kw: {"stored": False, "note": "（测试桩）"}
            for svc, note in ((a, "alice-的备注"), (b, "bob-的备注")):
                svc.store.learn_opinion = stub
                svc.last_turn = {"ts": 1.0, "question": "q", "answer": "a"}
                svc.mark_wrong(note)
            fa = os.path.join(a.data_dir, "feedback.jsonl")
            fb = os.path.join(b.data_dir, "feedback.jsonl")
            eq(os.path.exists(fa) and os.path.exists(fb), True,
               "两个人的留证文件都应该在自己的目录里")
            eq("alice-的备注" in open(fa, encoding="utf-8").read(), True, "alice 的记录在自己文件里")
            eq("bob-的备注" not in open(fa, encoding="utf-8").read(), True,
               "**alice 的文件里不该有 bob 的备注**（串号）")
            eq(os.path.exists(os.path.join(d, "feedback.jsonl")), False,
               "根目录不该再出现共享的 feedback.jsonl")
        finally:
            for svc in (a, b):
                if svc is not None:
                    try:
                        svc.close()
                    except Exception:
                        pass
            config.DATA_DIR = saved


def test_llmjson_recovers_truncated_json():
    """`B7`（工单二）：括号没闭合的 JSON 也要能补回来。

    修前 `parse_json` 先 `re.search(r"\\{.*\\}")`——**要求有闭括号** → 截断的回复直接
    `return None`，那圈 `_TAILS` 补尾永远到不了（实测 `parse_json('{"facts": ["a","b"]')` → None）。
    """
    from app.utils.llmjson import parse_json, strings

    eq(parse_json('{"facts": ["a","b"]'), {"facts": ["a", "b"]}, "缺右花括号+右方括号的必须补回来")
    eq(parse_json('{"facts": ["a","b"'), {"facts": ["a", "b"]}, "缺右引号+右方括号的也要补")
    eq(parse_json('{"facts": ["a","b"'), {"facts": ["a", "b"]}, "重复一次：确认稳定")
    eq(parse_json('```json\n{"facts": ["a"]}\n```'), {"facts": ["a"]}, "带代码围栏的照旧")
    eq(parse_json('前言 {"facts": ["a"]} 后话'), {"facts": ["a"]}, "前后有闲话的照旧")
    eq(parse_json("完全不是 JSON"), None, "真不是 JSON 仍然是 None")
    eq(parse_json(""), None, "空串仍然是 None")
    eq(strings(parse_json('{"facts": ["a","b"]'), "facts"), ["a", "b"], "配合 strings 能取到内容")


def test_pure_time_marker_reply_does_not_land_in_memory():
    """`B6`（工单二）：整条回复只有内部时间标记时，**清洗之后必须再判一次空**。

    修前：空检查判的是清洗**之前**的 `raw`（`chat()` 里）→ 模型只回「（今天下午 13:54）」
    也能过检查 → `strip_time_prefix` 把它洗成空串 → `finish_turn(question, "")` →
    `add_turn` 无条件追加一条**空 assistant 轮**（`memory_write.py:120-121`）→ 落库、留空轮次，
    而且这条空轮还会被后续摘要／提炼当成真实的一轮。
    """
    import tempfile

    from app.services.chat_service import ChatService, EmptyReply

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.prepare = lambda q, multi_message=False: ([], [])     # 不碰模型与向量
        svc.ask = lambda messages: "（今天下午 13:54）"            # 整条回复只有时间标记
        raised = False
        try:
            svc.chat("你今天干嘛了")
        except EmptyReply:
            raised = True
        eq(raised, True, "洗成空串之后要按空回复处理（EmptyReply），不能照常记一轮")
        eq(list(svc.store.sessions.get(svc.store.session, [])), [],
           f"不能留下空轮次：{svc.store.sessions.get(svc.store.session)}")
        rows = [r["content"] for r in svc.store.db.execute(
            "SELECT content FROM messages WHERE session=? ORDER BY seq", (svc.store.session,))]
        eq(rows, [], f"也不许落库：{rows}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for key, value in saved.items():
            setattr(config, key, value)


def test_clear_session_does_not_resurrect_old_messages():
    """`B5`（工单二）：`/clear` 之后 `_synced` 必须归零，**且库里的旧消息要真的消失**。

    修前：`clear_session` 只清内存（`sessions`／`summaries`），`_synced[session]` 还停在旧条数 →
    清完再加**不少于旧条数**的新消息时，`save()` 的 `len(msgs) < done` 分支**不触发** →
    `msgs[done:]` 从旧下标切 → 新消息落不了盘、库里留着旧消息（重启即"旧消息复活、新消息被覆盖"）。
    """
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _extract_only_client())
        try:
            for i in range(3):
                s.add_turn(f"旧问{i}", f"旧答{i}")
            s.save()
            eq([r["content"] for r in s.db.execute(
                "SELECT content FROM messages WHERE session='default' ORDER BY seq")],
               ["旧问0", "旧答0", "旧问1", "旧答1", "旧问2", "旧答2"],
               "先有 6 条旧消息落盘")

            s.clear_session()
            for i in range(4):          # 8 条 > 旧条数 6：正是漏判的那个分支
                s.add_turn(f"新问{i}", f"新答{i}")
            s.save()

            rows = [r["content"] for r in s.db.execute(
                "SELECT content FROM messages WHERE session='default' ORDER BY seq")]
            eq(all(str(x).startswith("新") for x in rows), True,
               f"库里不该再有任何旧消息：{rows}")
            eq(rows, ["新问0", "新答0", "新问1", "新答1",
                      "新问2", "新答2", "新问3", "新答3"],
               f"库里应当正好是新消息：{rows}")
        finally:
            s.db.close()


def test_clear_session_drops_stored_summary():
    """`/clear` 之后重启，旧摘要**不能复活**。

    原先 `clear_session()` 只清内存，`save()` 对 `summaries` 只有 INSERT OR REPLACE，
    库里那行**永远不删** → 重启读回来重新注入，而且游标停在旧位置（比历史还长），
    此后这个会话**再也不会做新摘要**。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        store.sessions["default"] = [{"role": "user", "content": "第一句", "ts": 1.0}]
        store.summaries["default"] = "这是一段旧摘要"
        store.summary_cursor["default"] = 1
        store.save()
        store.clear_session()
        store.save()
        again = _mk_store(d, _FakeLLM())
        eq("default" in again.summaries, False, f"重启后不该读回旧摘要：{again.summaries}")
        eq("default" in again.summary_cursor, False, "游标也该一起清掉")


def test_forget_all_with_world_notifies_callback():
    """`/forget world`：清世界书必须**通知回调**去清 `world.json`。

    否则 `world.json` 还是满的，下次启动 `WorldBook.sync()` 把它们全灌回向量库——
    用户看到「已全部清空」，重启后世界书又回来了。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        seen: list[list[str]] = []
        store.on_drop = lambda texts: seen.append(list(texts))
        for i, (text, kind) in enumerate((("我们住在杭州", "world"), ("我对花生过敏", "constraint"))):
            store.memories.append({"id": i + 1, "text": text, "kind": kind, "ts": 1.0,
                                   "hits": 0, "last_hit": 0})
        out = store.forget_all(include_world=True)
        eq(store.memories, [], "该清空就清空")
        eq(any("我们住在杭州" in (texts or []) for texts in seen), True,
           f"世界书条目必须回报给回调（否则 world.json 清不掉）：{seen}")
        eq("清空" in out, True, f"话术不变：{out}")


# ------------------------------------------------ #94 清点所获：T67–T70 的守卫
class _ScriptedLLM(_FakeLLM):
    """按提示词标记分派多种答复：提炼 / 取代判定 / 补偿 / 画像。"""

    def __init__(self, extract=None, supersede=None, compensate=None, profile=None,
                 opinion=None, **kw):
        super().__init__(extract=extract, **kw)
        self.supersede, self.compensate, self.profile = supersede, compensate, profile
        self.opinion = opinion                     # 反馈提炼（D31 / T81）

    def _create(self, **kw):
        prompt = str((kw.get("messages") or [{}])[0].get("content", ""))
        for marker, body in (("下面这些新事实刚刚发生", self.supersede),
                             ("一条旧记录被判定作废了", self.compensate),
                             ("你是用户画像分析师", self.profile),
                             ("沟通偏好整理器", self.opinion)):
            if body is not None and marker in prompt:
                self.chat_calls += 1
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=body))])
        return super()._create(**kw)


def test_inplace_promotion_is_persisted():
    """T67-①：普通记忆被世界书/约束「就地转正」时**必须落库**。

    内存改了、库里没改的下场：重启回退成旧 kind/旧文本，而 `world.json` 已经是新写法，
    下次启动 `sync_world` 会再补一条 → 白多一条重复条目（清点时只读到证据链，这里把它钉住）。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        store._add_memories(["我对花生过敏"], "fact")
        store._add_memories(["我对花生过敏"], "constraint")
        store.save()
        rows = [(r["kind"], r["text"]) for r in store.db.execute("SELECT kind,text FROM memories")]
        eq(rows, [("constraint", "我对花生过敏")],
           f"同一句话被约束覆盖：应就地转正且只有一条，实际 {rows}")
        again = _mk_store(d, _FakeLLM())
        eq([m["kind"] for m in again.memories], ["constraint"],
           "重启后 kind 仍是 constraint（否则会跟 world.json 打架、多出重复）")


def test_pending_table_orphans_are_recovered():
    """T67-②：`pending` 表不能只写不读——孤儿文本会被静默丢掉。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        with store.db:
            store.db.execute("INSERT INTO pending(text,kind) VALUES(?,?)",
                             ("这条只存在于 pending 表（迁移遗留）", "fact"))
        again = _mk_store(d, _FakeLLM())
        eq(any("只存在于 pending 表" in m["text"] for m in again.memories), True,
           f"孤儿文本要被捡回来补向量入库：{[m['text'] for m in again.memories]}")


def test_single_item_embed_failure_is_visible():
    """T67-③：单条写入的 embedding 失败也要留痕（原来静默 `return [], 0`）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM(fail_embed=True))
        added, dup = store._add_memories(["一条新记忆"], "fact")
        eq((added, dup), ([], 0), "失败就不写")
        kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM events")]
        eq("embed_fail" in kinds, True, f"必须记一条 embed_fail 事件（失败要可见）：{kinds}")


def test_supersede_and_compensate_paths_run():
    """T68：作废与补偿两条兜底**真的会跑**（清点发现它们在真实数据里从未触发）。

    这条是受控证据：模型报「旧条目作废」+ 补偿时捡回一部分，两条链路各留一条事件。
    """
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        client = _ScriptedLLM(
            extract=_json.dumps({"facts": ["公司在滨江"], "drop": ["公司在城西"]},
                                ensure_ascii=False),
            compensate=_json.dumps({"keep": ["公司原来在城西"]}, ensure_ascii=False))
        store = _mk_store(d, client)
        store._add_memories(["公司在城西"], "fact")
        store.add_turn("我们公司搬到滨江了", "（她回应）")
        kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM events")]
        eq("drop" in kinds, True, f"作废要留痕：{kinds}")
        eq("compensate" in kinds, True, f"补偿要留痕：{kinds}")
        eq(any("公司原来在城西" in m["text"] for m in store.memories), True,
           f"捡回的条目要回到活跃集：{[m['text'] for m in store.memories]}")


def test_mem_del_physically_deletes():
    """T69-③：`/mem del` 是用户**明确要求删除**，就该真删（原来只归档，界面却写"已删除"）。"""
    from app.services.chat_service import ChatService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved_dir, saved_key = config.DATA_DIR, getattr(config, "AMAP_KEY", None)
        config.DATA_DIR = d
        if saved_key is not None:
            config.AMAP_KEY = ""          # 关掉天气：测试不联网
        svc = None
        try:
            svc = ChatService()
            rec = {"id": 999, "text": "从没被想起过的一条", "kind": "fact", "ts": 1.0,
                   "hits": 0, "last_hit": 0}
            svc.store.memories.append(rec)
            with svc.store.db:
                svc.store.db.execute(
                    "INSERT INTO memories(id,text,kind,ts,session,hits,last_hit)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (999, rec["text"], "fact", 1.0, "default", 0, 0))
            out = svc.delete_dead("1")
            eq("已删除" in out, True, f"话术：{out}")
            left = svc.store.db.execute("SELECT count(*) c FROM memories WHERE id=999").fetchone()["c"]
            eq(left, 0, "真删：库里不该还有这一行（归档不算删）")
        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:
                    pass
            config.DATA_DIR = saved_dir
            if saved_key is not None:
                config.AMAP_KEY = saved_key


def test_world_reload_uses_per_user_file():
    """T70-①：`/world reload` 必须读**自己**的 world.json（原来读全局那份 → 多用户串号）。"""
    import json as _json

    from app.services.chat_service import ChatService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        saved = config.DATA_DIR
        config.DATA_DIR = d
        svc = None
        try:
            def write(path, text):
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump({"entries": [{"text": text, "source": "manual", "ts": 1.0}]},
                               f, ensure_ascii=False)

            os.makedirs(os.path.join(d, "users", "alice"), exist_ok=True)
            write(os.path.join(d, "world.json"), "根目录那份（不该被 alice 读到）")
            write(os.path.join(d, "users", "alice", "world.json"), "alice 的世界书-旧")
            svc = ChatService(user_id="alice")
            write(os.path.join(d, "users", "alice", "world.json"), "alice 的世界书-新")
            svc.run_command("/world reload")
            texts = [e.get("text", "") for e in svc.world.entries]
            eq(any("alice 的世界书-新" in t for t in texts), True, f"该读到自己的文件：{texts}")
            eq(any("根目录那份" in t for t in texts), False, f"不该读到根目录那份：{texts}")
        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:
                    pass
            config.DATA_DIR = saved


def test_nickname_percent_encoding_without_marker():
    """T70-②：旧客户端不带编码标记时也要还原昵称，否则同一个人会落到第二个数据目录。"""
    from app.services import auth

    eq(auth.nickname_uid({"X-User-Id": "%E5%B0%8F%E6%9E%97"}), "小林",
       "看着像百分号编码就该还原")
    eq(auth.nickname_uid({"X-User-Id": "%E5%B0%8F%E6%9E%97",
                          "X-User-Id-Encoding": "percent-encoded"}), "小林", "带标记的照旧")
    eq(auth.nickname_uid({"X-User-Id": "50%OFF"}), "50%OFF",
       "不合法的转义序列要原样保留（不能把普通昵称改坏）")
    eq(auth.nickname_uid({"X-User-Id": "小林"}), "小林", "本来就是中文的原样返回")


def test_profile_update_merges_instead_of_replacing():
    """T70-③：画像更新是**合并**，不是整体覆盖（模型漏字段时不能把旧画像抹掉）。"""
    import json as _json

    from app.models.user import UserProfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        client = _ScriptedLLM(profile=_json.dumps({"summary": "新的总结", "facts": ["新事实"]},
                                                  ensure_ascii=False))
        p = UserProfile(client, path=os.path.join(d, "user_profile.json"), model="test-model")
        p.data = {"version": 1, "summary": "旧的总结", "facts": ["旧事实"],
                  "preferences": ["旧偏好"], "topics": ["旧话题"]}
        p._save()
        p.analyze([{"role": "user", "content": "我最近在学做饭"}])
        eq(p.data["summary"], "新的总结", "summary 该被新总结覆盖")
        eq(set(p.data["facts"]) >= {"旧事实", "新事实"}, True, f"facts 只增不删：{p.data['facts']}")
        eq(p.data.get("preferences"), ["旧偏好"], f"模型没返回的字段不能被抹掉：{p.data}")
        eq(p.data.get("topics"), ["旧话题"], f"同上：{p.data}")


def test_emotion_write_failure_is_visible():
    """T69-④：情绪轨迹写失败不能静默（原来是 `except: pass`，文件会永远停在旧内容）。"""
    import logging

    from app.models.emotion import EmotionTracker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        bad = os.path.join(d, "不存在的目录", "emotion.json")   # 目录不存在 → 原子写会失败
        tracker = EmotionTracker(bad, "http://example.invalid/v1", "k", "m")
        tracker.entries = [{"ts": 1.0, "mood": "低落", "confidence": 0.9,
                            "intensity": 3, "quote": "有点累"}]
        records = []
        handler = logging.Handler()
        handler.emit = lambda rec: records.append(rec)
        logger = logging.getLogger("app.emotion")
        logger.addHandler(handler)
        try:
            tracker._save()
        finally:
            logger.removeHandler(handler)
        eq(any(r.levelno >= logging.WARNING for r in records), True,
           f"写失败必须留 WARNING（失败要可见）：{[r.getMessage() for r in records]}")
        eq(any("情绪轨迹写入失败" in r.getMessage() for r in records), True,
           "日志里要说清是哪一步失败")


# ------------------------------------------------- 2026-09-28 清点抓到的三处（#96）
def test_timeline_includes_archived_entries():
    """`timeline()` 的 docstring 承诺「归档的条目也列出来（带标记）」，但它要求
    `event_date`，而归档行的两个来源（`_load` 的 SELECT 列表、`_drop` 里追加的归档 dict）
    **都不带这个字段** → 对归档恒为假，承诺从未生效。走丢→找到这类历史正是时间轴的用处。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        quote = "助手在 2026年9月20日 去了书店"
        store._add_memories([quote], "fact", actor="assistant")
        eq(store.memories[0].get("event_date"), "2026-09-20", "先确认入库时确实解析出了日期")
        eq(store.recall(quote, top_k=3, mark_hit=False)[0]["text"], quote, "召回拿的是同一条")

        store.drop_memories([quote])
        eq(len(store.memories), 0, "作废后活跃集应为空（默认软删归档）")
        eq(len(store.archived), 1, "归档里应该有一条")

        rows = [r for r in store.timeline() if r["text"] == quote]
        eq(len(rows), 1, f"归档条目必须出现在时间轴上（内存态），实际 {store.timeline()}")
        if rows:
            eq(rows[0]["active"], False, "并且要带『已归档』标记")
            eq(rows[0]["date"], "2026-09-20", "归档条目也要有日期，否则排序/展示都错")
            eq(rows[0]["actor"], "assistant", "视角也要保留（她的经历标〔她〕）")

        store.save()                      # 重启：归档是**从库里**读回来的
        again = _mk_store(d, _FakeLLM())
        rows2 = [r for r in again.timeline() if r["text"] == quote]
        eq(len(rows2), 1, f"归档条目重启后仍要在时间轴上：{again.timeline()}")
        if rows2:
            eq(rows2[0]["date"], "2026-09-20", "库里的归档行必须带 event_date")


def test_assistant_side_facts_are_tagged_as_her_own():
    """双方视角的守卫只认提炼器输出的 `self` 字段（`_extract` 里 `startswith("我")`）。

    模型一旦把「她的事」写进 `facts`，入库时就被无条件标成 `actor="user"`——真实库里
    已经有这种行（第 53 条 `actor='user'` 而文本是「助手喜欢看散文和生活随笔…」），
    而 `facts` 正是喂给画像提炼的原料，会污染「他是谁」。
    """
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        client = _ScriptedLLM(extract=_json.dumps(
            {"facts": ["助手喜欢看散文和生活随笔"], "self": ["我答应陪她挑两本书"]},
            ensure_ascii=False))
        store = _mk_store(d, client)
        store.add_turn("你有什么喜欢的呢", "我一般看点散文、生活随笔那种")
        rows = {m["text"]: m.get("actor") for m in store.memories}
        eq(rows.get("助手喜欢看散文和生活随笔"), "assistant",
           f"主语是『助手』的条目属于她的视角，不能标成用户侧：{rows}")
        eq(rows.get("我答应陪她挑两本书"), "assistant", f"self 通道照旧：{rows}")
        kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM events")]
        eq("actor_reassigned" in kinds, True,
           f"改写要留痕（静默改写等于让人以为模型是对的）：{kinds}")


def test_turn_event_reports_stale_skips():
    """`turn` 事件的 `stale=` 用来量「过期约定被挡掉几条」——而它的清零发生在
    `add_turn` 开头、累加却发生在**上一句**的 `build_messages → recall` 里，
    于是这个字段基本恒为 0（台账 T13/T26 正要靠它量识别率）。
    """
    import datetime as dt3
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        # 提炼器这一轮没提炼出新东西（facts 为空）——本测试只关心 stale 字段
        store = _mk_store(d, _FakeLLM(extract='{"facts": []}'))
        # 一条**过期的约定**：日期由"今天减 3 天"算出来（不写死日期，
        # 否则这条用例会在第二天自己失效）。措辞不带「已/完」，所以它属于待办语义。
        past = (dt3.date.today() - dt3.timedelta(days=3)).isoformat()
        store._add_memories([f"用户和助手约定 {past} 在广场见面"], "fact")
        eq(store.memories[0].get("event_date"), past,
           f"夹具自检：绝对日期该被解析成字段，实际 {store.memories[0].get('event_date')}")

        # 服务层的真实顺序：prepare（build_messages → recall）在前，finish_turn（add_turn）在后。
        # ⚠ 查询要与记忆**同文本**才召得回来：假模型的向量是文本哈希（**不同文本**之间
        # 相似度接近 0），而"过期约定在召回层被挡住"这件事只有真的召回到它才谈得上。
        # 夹具第一版写的是"我们明天有什么安排吗"，相似度 -0.18 < 0.45，压根没进那个循环。
        stale_text = f"用户和助手约定 {past} 在广场见面"
        store.build_messages("system", stale_text)
        store.add_turn(stale_text, "（她回应）")

        events = list(store.db.execute(
            "SELECT payload FROM events WHERE kind='turn' ORDER BY id DESC LIMIT 1"))
        eq(len(events), 1, "本轮该留一条 turn 事件")
        payload = events[0]["payload"] if events else ""
        got = -1
        try:
            import json as _json
            got = int((_json.loads(payload) or {}).get("stale", -1))
        except Exception:
            got = -1
        eq(got, 1, f"turn 事件要报出本轮挡掉的过期待办数（现在恒为 0/取不到）：{payload}")


# ------------------------------------------------- 2026-09-28 用户反馈抓到的两处（#98）
def test_day_part_matches_clock():
    """时段必须跟钟走（用户反馈：14:12 那轮她说「早上好」，注入的时间是对的）。"""
    from app.utils import clock

    def at(h):
        return dt.datetime(2026, 9, 28, h, 12)

    eq(clock.day_part(at(3)), "凌晨", "3 点")
    eq(clock.day_part(at(5)), "早上", "5 点（边界）")
    eq(clock.day_part(at(8)), "上午", "8 点（边界）")
    eq(clock.day_part(at(11)), "中午", "11 点（边界）")
    eq(clock.day_part(at(12)), "中午", "12 点")
    eq(clock.day_part(at(14)), "下午", "14 点（用户反馈的那一轮）")
    eq(clock.day_part(at(17)), "傍晚", "17 点（边界）")
    eq(clock.day_part(at(21)), "晚上", "21 点")
    eq(clock.day_part(at(23)), "晚上", "23 点")
    eq(clock.day_part(at(0)), "凌晨", "0 点")
    eq(clock.now_text(at(14)).endswith("14:12"), True, f"now_text 不变：{clock.now_text(at(14))}")


def test_system_prompt_states_day_part_and_greeting_rule():
    """下午不许说「早上好」：时段要明说，规则要白纸黑字。

    实测（用户 2026-09-28 14:13 反馈）：`【当前时间】2026年9月28日 周一 14:12` 就在
    prompt 第一行，她仍然开口「早上好呀～」——**注入正确 ≠ 行为正确**（与 T41/S05 同类）。
    所以把「现在是下午」标出来，并把"问候要跟时段相符、不要被对话内容带偏"写成硬规则。
    """
    import app.utils.clock as _clock
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "INJECT_TIME", "AMAP_KEY", "API_KEY",
                                             "CHAT_API_KEY", "EMBED_API_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp                      # 隔离：别碰真实数据
    config.INJECT_TIME = True
    config.AMAP_KEY = ""                       # 关掉天气：不联网
    # 给个假 key 只为让 openai 客户端构造得起来（空 key 会被 SDK 拒）；**不会真发请求**：
    # 这条测试只读 prompt，不调模型。
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    real_now, real_part = _clock.now_text, getattr(_clock, "day_part", None)
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()           # 构造后才换掉客户端（__init__ 里就要用它）
        _clock.now_text = lambda *a, **k: "2026年9月28日 周一 14:12"
        _clock.day_part = lambda *a, **k: "下午"     # 两个都打桩，夹具才是"下午那一轮"
        prompt = svc.system_prompt()
        eq("14:12" in prompt, True, f"时间照常注入：{prompt[:80]}")
        eq("下午" in prompt, True,
           f"prompt 里必须写明时段（下午），否则她无从判断该用哪个问候：{prompt[:160]}")
        eq("问候" in prompt, True, f"要有一条关于问候/时段的硬规则：{prompt[:240]}")
    finally:
        _clock.now_text = real_now
        if real_part is not None:
            _clock.day_part = real_part
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_when_label_is_relative_and_says_today_part():
    """时间标记要人话、要相对：今天下午 / 昨天 21:30 / 3 天前 14:00。"""
    from app.models.memory_read import _when_label

    now = dt.datetime(2026, 9, 28, 15, 26)
    eq(_when_label(dt.datetime(2026, 9, 28, 14, 5).timestamp(), now), "（今天下午 14:05）",
       "今天要说时段（下午晒的太阳 ≠ 刚晒完）")
    eq(_when_label(dt.datetime(2026, 9, 28, 9, 30).timestamp(), now), "（今天上午 09:30）", "今天上午")
    eq(_when_label(dt.datetime(2026, 9, 27, 21, 30).timestamp(), now), "（昨天 21:30）",
       "昨天只给日期+时刻（那一轮的『晚安』就是这么来的）")
    eq(_when_label(dt.datetime(2026, 9, 25, 14, 0).timestamp(), now), "（3 天前 14:00）", "更早给天数")
    eq(_when_label(None, now), "", "没有时间戳就不标（旧数据）")
    eq(_when_label("不是数字", now), "", "坏值不能崩")


def test_sliding_window_carries_time_labels():
    """滑窗里的历史消息必须带时间标记。

    用户 2026-09-23 的三条反馈（15:27「上午晒的太阳，现在是下午了」、15:28「现在是下午，
    为什么说晚安」、20:24「我记得上一轮我说过我的胃已经好了」）**同一个根因**：
    滑窗只给了 `role/content`，模型无从知道哪句话是昨晚说的、哪句是上午说的，
    于是把历史里的「现在快九点半了」「明天见」「才刚晒没一会儿」照搬成现在——
    实测 `seq 21`（昨晚）说「现在快九点半了」，`seq 36`（20:24）又说「都快九点半了」。
    """
    import datetime as dt4
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM(extract='{"facts": []}'))
        now = dt4.datetime.now()
        old = (now - dt4.timedelta(days=1)).replace(hour=21, minute=30).timestamp()
        store.sessions["default"] = [
            {"role": "user", "content": "明天早上10点吧", "ts": old},
            {"role": "assistant", "content": "现在快九点半了，早点休息，明天见呀！", "ts": old},
            {"role": "user", "content": "我们回家吧", "ts": now.timestamp()},
            {"role": "assistant", "content": "行，路上慢点", "ts": now.timestamp()},
        ]
        msgs, _ = store.build_messages("system", "现在几点了")
        body = "\n".join(m["content"] for m in msgs if m["role"] != "system")
        eq("（昨天 21:30）" in body, True,
           f"昨晚那句必须标出『昨天 21:30』，否则她会把『快九点半』当现在：{body}")
        eq("（今天" in body, True, f"今天的消息也要标时段：{body}")
        tail = msgs[-1]
        eq(tail["role"], "user", "最后一条是当前问题")
        eq(tail["content"], "现在几点了", f"当前这一句不加标记（它就是现在）：{tail['content']}")


def test_day_plan_block_excludes_weather_and_lists_tomorrow():
    """`【今天说好的事】`要满足两件事（都来自用户反馈）：

    ① **不能收天气**：`upcoming()` 只按 `event_date` 筛，而天气条目也带日期 →
       真实那一轮里该块装的就是「2026年9月28日杭州有雾、白天有小雨、气温约27度」，
       真正的约定一条没有（块名叫"说好的事"，装天气就等于告诉模型这段不重要）。
    ② **明天的约定要露面并标清是明天**：`only_today=True` 让 9-29 的书店之约在
       21:25 那轮**一个注入块都没出现**，她只能靠滑窗编出「我到时候在书店门口等你」，
       用户反馈"今天早上的事怎么变成了明天的事"。
    """
    import datetime as dt2

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        today = dt2.date.today()
        d0 = today.isoformat()
        d1 = (today + dt2.timedelta(days=1)).isoformat()
        store._add_memories([f"用户和助手约好 {d1} 在书店门口见面"], "fact")
        store._add_memories([f"{d0}杭州有雾、白天有小雨，气温约27度"], "fact")

        msgs, _ = store.build_messages("system", "明天去")
        block = next((m["content"] for m in msgs
                      if "今天说好的事" in m["content"]), "")
        eq("杭州有雾" in block, False,
           f"天气不是『说好的事』，不该进这一块：{block}")
        eq("书店" in block, True, f"明天的约定必须露面：{block}")
        eq("明天" in block, True, f"而且要标清它是明天，别让她当成今天：{block}")


def test_fact_feedback_becomes_a_pending_correction():
    """T81：事实类反馈以前是死路（`opinions` 不落、也没有别的出口）。

    用户 2026-09-28 21:32 点的那条就是这样：她编了一个从没出现过的碰头地点，
    系统回「这条属于「她记错了事实」，不进说话方式倾向」——然后什么都没有发生，
    那条编造的记忆照样留在库里、照样进时间轴和【今天说好的事】。
    现在它必须变成一条**可执行的提议**。
    """
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        client = _ScriptedLLM(opinion=_json.dumps(
            {"is_style_issue": False, "topic": "编造约定",
             "text": "她编了一个没出现过的见面地点"}, ensure_ascii=False))
        store = _mk_store(d, client)
        res = store.learn_opinion("明天去", "我到时候在书店门口等你", note="她编了地点")
        eq(res.get("stored"), False, "事实类不进 opinions（D31 的边界不变）")
        eq(isinstance(res.get("correction"), int), True,
           f"必须登记一条待确认的修正：{res}")
        pend = store.pending_corrections()
        eq(len(pend), 1, f"列表里应有一条：{pend}")
        eq("不进说话方式倾向" in (res.get("note") or ""), True, f"说明要讲清去哪了：{res.get('note')}")
        eq("/fix" in (res.get("note") or ""), True, f"并且要给出出口：{res.get('note')}")
        eq(len(store.opinions()), 0, "opinions 表仍为空（这条不属于说话方式）")


def test_correction_accept_archives_the_named_memory():
    """采纳 = 作废那条被推翻的记忆（软删归档）；**找不到就不许乱猜着删**。"""
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        client = _ScriptedLLM(opinion=_json.dumps(
            {"is_style_issue": False, "topic": "编造约定"}, ensure_ascii=False))
        store = _mk_store(d, client)
        store._add_memories(["我约好2026年9月29日在书店门口等用户"], "fact", actor="assistant")
        store._add_memories(["用户住在杭州"], "fact")

        # 反馈的问答里**原样**包含那条记忆 → 采纳时应作废它
        store.learn_opinion("明天去", "好呀，我约好2026年9月29日在书店门口等用户", note="编的")
        out = store.resolve_correction(1, accept=True)
        eq("已采纳" in out, True, f"要说明结果：{out}")
        eq([m["text"] for m in store.memories], ["用户住在杭州"],
           f"只作废被点名的那条，别的不能动：{[m['text'] for m in store.memories]}")
        eq(len(store.archived), 1, "软删归档（留档可查），不是物理删")
        eq(store.pending_corrections(), [], "处理完就不该再挂在待确认里")

        # 问答里**没有**那条记忆的原文 → 不许猜着删
        store.learn_opinion("她记错了", "（一段跟库里无关的回答）", note="说不清")
        out2 = store.resolve_correction(1, accept=True)
        eq("没找到" in out2, True, f"要明确说没找到：{out2}")
        eq(len(store.archived), 1, "数据一点都没动（猜着删比不做更糟）")
        rows = list(store.db.execute("SELECT status FROM feedback_corrections ORDER BY id"))
        eq(rows[-1]["status"], "unmatched", f"状态要留痕：{[dict(r) for r in rows]}")


def test_correction_drop_and_report():
    """`/fix drop N` 只结案不动数据；`/fix` 报告要能列出待确认项。"""
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _ScriptedLLM(opinion=_json.dumps(
            {"is_style_issue": False, "topic": "时间错乱"}, ensure_ascii=False)))
        store._add_memories(["用户住在杭州"], "fact")
        store.learn_opinion("明天去", "她答的话", note="时间错乱")
        rep = store.corrections_report()
        eq("待确认的修正" in rep, True, f"报告要列出来：{rep}")
        eq("时间错乱" in rep, True, f"要带上主题：{rep}")
        out = store.resolve_correction(1, accept=False)
        eq("已忽略" in out, True, f"忽略要说清楚：{out}")
        eq([m["text"] for m in store.memories], ["用户住在杭州"], "忽略不动任何记忆")
        eq(store.pending_corrections(), [], "忽略后不再待确认")
        eq("已处理" in store.corrections_report() or "已忽略" in store.corrections_all(), True,
           "已处理的要能回看")


def test_city_lookup_and_weather_switch():
    """T82-④：用户反馈「我能不能再加个检测用户位置的功能，让她更好的提供服务」。

    做法是**问一句就记住**，不做定位猜测：`/city 上海` → 天气立刻换成上海、
    世界书写进「用户在上海」，重启后由 world.json 推回来。
    """
    from app.utils import weather as w

    eq(w.adcode_for("杭州"), "330100", "杭州")
    eq(w.adcode_for("北京市"), "110000", "带『市』也要认（去掉后缀再查）")
    eq(w.adcode_for("上海"), "310000", "上海")
    eq(w.adcode_for("330100"), "330100", "直接给 adcode 也认")
    eq(w.adcode_for("不存在的城市"), None, "认不出来就返回 None（不许瞎猜）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        cache = w.WeatherCache("fake-key", "330100", ttl=3600)
        eq(cache.city_name, "杭州", f"初始城市名：{cache.city_name}")
        cache.set_city("310000")
        eq(cache.city_name, "上海", f"换城后名字要跟着变：{cache.city_name}")
        eq(cache.city, "310000", "adcode 也要变")


def test_city_command_updates_world_and_weather():
    """`/city` 命令：看状态 / 换城市（换完世界书要跟上，否则重启就丢）。"""
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"          # 有 key 走"能查天气"的分支
    # 城市**不再有兜底默认值**（`#109`）。这条测的是 `/city` 的机制（看现在的城市、
    # 换城市要写世界书），所以显式给一个默认值——"世界书没写就不知道你在哪"另有一条用例。
    config.AMAP_CITY = "330100"
    # ⚠ 这条用例**真的去请求过高德**（`T88` 的现场，2026-09-29 实测）：
    # `AMAP_KEY` 与 `AMAP_CITY` 一齐就绪时，`ChatService.__init__`（chat_service.py:212-215）
    # 与 `/city 成都` → `set_city()` 都会 `WeatherCache.refresh()` → `urllib` 真发 HTTPS。
    # 原注释写的"（不联网：不刷新）"**是错的**；实测撞上一次 TLS 握手超时，
    # 异常把整个套件打成 exit 1 且 stdout 全空（连是哪条挂的都看不出来）。
    # 现在把刷新打成 **no-op 记录器**：既保证零网络，又能断言"它确实被调过"（机制没被绕过）。
    from app.utils import weather as w
    calls: list = []
    real_refresh = w.WeatherCache.refresh
    w.WeatherCache.refresh = lambda self: calls.append(self.city) or None
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        out = svc.run_command("/city")
        eq("杭州" in out, True, f"配置里给了城市就要报出来：{out}")
        out2 = svc.run_command("/city 成都")
        eq("成都" in out2, True, f"换城要确认：{out2}")
        eq(w.adcode_for("成都") in calls, True,
           f"换城市要真的去重查一次天气（这里被打了桩）：{calls}")
        eq(svc.weather.city_name, "成都", "天气缓存要换")
        eq(any("成都" in e["text"] for e in svc.world.entries), True,
           f"世界书要写上，否则重启就丢：{[e['text'] for e in svc.world.entries]}")
        out3 = svc.run_command("/city 不存在的城市")
        eq("不认识" in out3 or "adcode" in out3, True, f"认不出要给出办法：{out3}")
        eq(svc.weather.city_name, "成都", "认不出时不许乱改")
    finally:
        w.WeatherCache.refresh = real_refresh
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_city_survives_weather_failure():
    """`/city` 不许被感知层拖死：天气重查炸了，城市照样得换成功。

    铁律是「感知层的故障不能挂对话」（`D13`），而这条链路上 `set_city()` 曾是
    **唯一**没包 try/except 的 refresh 调用点（`__init__` 与 `finish_turn` 都有）。
    踩过：一次高德 TLS 握手超时 → 异常从 `set_city()` 抛穿 `/city 成都` →
    再顺着本文件 `main()` 的裸 `fn()` 把整个套件打成**无输出**的 exit 1。
    这就是那道闸：换城市已经成功（缓存 + 世界书都写了），天气查不到只是"稍后再说"。
    """
    from app.services.chat_service import ChatService
    from app.utils import weather as w

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"
    config.AMAP_CITY = "330100"
    real_refresh = w.WeatherCache.refresh

    def boom(self):
        raise RuntimeError("模拟高德握手超时")

    w.WeatherCache.refresh = boom
    svc = None
    try:
        svc = ChatService()              # 构造期那一处早就兜住了，顺手一并验
        svc.store.client = _FakeLLM()
        out = svc.run_command("/city 成都")
        eq("成都" in out, True, f"天气炸了也要把城市换成功：{out}")
        eq("没查到" in out, True, f"并且要如实说天气这次没查到：{out}")
        eq(svc.weather.city_name, "成都", "缓存要真换成成都")
        eq(any("成都" in e["text"] for e in svc.world.entries), True, "世界书也要写进去")
    finally:
        w.WeatherCache.refresh = real_refresh
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_city_from_coords_and_ip_lookup():
    """坐标/公网 IP → 城市（T82-④ 的探测分支）。

    实测背景（2026-09-28）：这台机器 Windows 定位给出成都（精度 212 米），
    而公网 IP 反查给出台中——**两者会不一致**，所以：
    ① 两条路各自实现、互不冒充；② 认不出来就返回空，不许拿一个"最像的"顶上。
    """
    from app.utils import weather as w

    # 高德逆地理：只认 status=1 且有 addressComponent
    good = {"status": "1", "info": "OK", "regeocode": {"addressComponent": {
        "province": "四川省", "city": "成都市", "district": "高新区"}}}
    eq(w._city_from_amap(good), "成都", "城市名要能对上 CITY_ADCODE 的键")
    eq(w._city_from_amap({"status": "1", "regeocode": {"addressComponent": {
        "province": "北京市", "city": ""}}}), "北京",
       "直辖市 city 为空时退回 province")
    eq(w._city_from_amap({"status": "0", "info": "INVALID_USER_KEY"}), "",
       "status!=1 不算结果（失败要显式，不许猜）")
    eq(w._city_from_amap({"status": "1", "regeocode": {"addressComponent": {
        "province": "海外", "city": "某地"}}}), "", "对不上的名字返回空")

    # 公网 IP 反查：只认 status=success
    eq(w._city_from_ipapi({"status": "success", "city": "Chengdu"}), "成都",
       "英文名也要认（IP 服务返回英文）")
    eq(w._city_from_ipapi({"status": "fail", "message": "reserved range"}), "",
       "失败返回空")
    eq(w._city_from_ipapi({"status": "success", "city": "Springfield"}), "",
       "表里没有的城市不许瞎认")


def test_geo_powershell_script_is_ascii():
    """那个 PowerShell 探测脚本必须保持纯 ASCII。

    踩过：Windows PowerShell 5.1 按 **GBK** 读 .ps1，脚本里写中文会让它
    解析错位、直接语法错误（第一次探针就是这么废掉的）。这条测试把纪律钉住。
    """
    from app.utils import geo

    path = geo._script_path()
    eq(os.path.exists(path), True, f"脚本要随代码一起存在：{path}")
    raw = open(path, "rb").read()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    eq(bad[:3], [], f"脚本里不许出现非 ASCII 字节（前 3 个：{bad[:3]}）")


def test_city_detect_reports_but_does_not_write():
    """`/city detect` 只报告、**不写库**；`/city use` 才落。

    这是用户 2026-09-28 拍板的："探测完先给我看，我确认后再写"。
    为什么必须这样：定位结果会进世界书并每轮注入【天气】，猜错就是持续性假信息。
    """
    from app.services.chat_service import ChatService
    from app.utils import geo as _geo
    from app.utils import weather as _weather

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"
    real_win, real_rev = _geo.detect_windows, _weather.reverse_geocode
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        _geo.detect_windows = lambda *a, **k: {"ok": True, "lat": 30.6, "lon": 104.1,
                                               "acc": 212, "source": "System.Device.Location",
                                               "error": ""}
        _weather.reverse_geocode = lambda lat, lon, key, timeout=8: {
            "city": "成都", "adcode": "510100", "detail": "高新区", "source": "高德逆地理"}
        before = len(svc.world.entries)
        out = svc.detect_city()
        eq("成都" in out, True, f"要报出探测到的城市：{out}")
        eq("Windows 定位" in out, True, f"要说清来源（供人判断准不准）：{out}")
        eq("/city use" in out, True, f"要给出确认方式：{out}")
        eq(len(svc.world.entries), before, "**探测阶段绝不能写世界书**")
        eq(any("成都" in e["text"] for e in svc.world.entries), False,
           f"世界书不该出现成都：{[e['text'] for e in svc.world.entries]}")

        out2 = svc.run_command("/city use")
        eq("成都" in out2, True, f"确认后要说明结果：{out2}")
        eq(svc.weather.city_name, "成都", "确认后才换城市")
        eq(any("成都" in e["text"] for e in svc.world.entries), True,
           f"确认后写入世界书（重启不丢）：{[e['text'] for e in svc.world.entries]}")
        eq(svc._detected_city, None, "确认后清掉暂存（不落盘）")
    finally:
        _geo.detect_windows, _weather.reverse_geocode = real_win, real_rev
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_city_detect_falls_back_to_ip_and_says_so():
    """Windows 定位失败 → 回退公网 IP，并且**把来源标成不可信**。"""
    from app.services.chat_service import ChatService
    from app.utils import geo as _geo
    from app.utils import weather as _weather

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"
    real_win, real_ip = _geo.detect_windows, _weather.city_by_ip
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        _geo.detect_windows = lambda *a, **k: {"ok": False, "error": "position unknown",
                                               "lat": None, "lon": None, "acc": None, "source": ""}
        # 实测那种"IP 与真实位置不符"的情形：这里用台北代表"IP 判出来的城市"
        _weather.city_by_ip = lambda timeout=8: {"city": "台北", "adcode": "710000",
                                                 "detail": "Taiwan", "source": "公网 IP"}
        out = svc.detect_city()
        eq("不可用" in out, True, f"要说明 Windows 定位为什么没用上：{out}")
        eq("台北" in out, True, f"要有 IP 的结果：{out}")
        eq("可能不准" in out or "城市级" in out, True, f"来源要标明不可全信：{out}")
        eq(len(svc.world.entries), 0, "仍然不许写库")
    finally:
        _geo.detect_windows, _weather.city_by_ip = real_win, real_ip
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


class _FakeResp:
    """假 HTTP 响应：只要能 read() 出 bytes 就够（places.search 只用到这一件事）。"""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_place_keywords_come_from_preferences():
    """查询词要**从你俩的偏好/人设里长出来**，不是硬编码一份"好玩的"。

    这是项目对"按性格推荐"的落地方式：工具负责把偏好翻译成查询词 + 拿真数据，
    话术交给人格（D13）——绝不写 if-then 话术表。
    """
    from app.utils import places as p

    eq(p.keywords_for(["喜欢晒太阳", "喜欢约AI一起出门活动"]), ["公园", "广场"],
       "偏好词映射到高德认的类别")
    eq(p.keywords_for(["喜欢逛书店"]), ["书店"], "书店")
    eq(p.keywords_for([]), [], "没有偏好就没有查询词（不许瞎猜一个城市地标出来）")
    eq(p.keywords_for(["喜欢打游戏"]), [], "映射表里没有的偏好不许硬凑")
    # 去重 + 上限
    got = p.keywords_for(["晒太阳", "看书", "看电影", "逛商场", "吃小吃", "去公园"])
    eq(len(got), p.MAX_KEYWORDS, f"查询词要有上限（每次搜索都是真实 API 调用）：{got}")
    eq(len(got), len(set(got)), f"不许重复查同一个词：{got}")


def test_places_search_cache_and_offline_fallback():
    """搜索：真数据 + 缓存（同一类别不重复打 API）+ 失败静默返回空。

    缓存不是优化而是必需：这类查询**每轮都可能问**，没有缓存就是每轮一次高德调用。
    失败必须返回空、绝不能编：她一旦有"看起来像真的"的假地点，下次就会照着它说。
    """
    import json as _json

    from app.utils import places as p

    calls = []

    def opener(url, timeout=8):
        calls.append(url)
        body = _json.dumps({"status": "1", "info": "OK", "pois": [
            {"name": "颜如玉书店", "type": "购物服务;专卖店;书店", "distance": "916",
             "address": "布澜路89号"},
            {"name": "易书阁", "type": "购物服务;专卖店;书店", "distance": "1205",
             "address": "阅景花园5栋214"},
        ]}, ensure_ascii=False).encode("utf-8")
        return _FakeResp(body)

    hits = p.search(lat=22.63, lon=114.13, keywords=["书店"], key="fake",
                    opener=opener, ttl=600)
    eq(len(hits), 2, f"要拿到两条：{hits}")
    eq(hits[0]["name"], "颜如玉书店", "按距离升序")
    eq(hits[0]["distance"], 916, f"距离要转成数字：{hits[0]}")
    eq("书店" in hits[0]["label"], True, f"类别要带上（人设靠它选）：{hits[0]['label']}")
    n_first = len(calls)

    again = p.search(lat=22.63, lon=114.13, keywords=["书店"], key="fake",
                     opener=opener, ttl=600)
    eq(len(calls), n_first, "TTL 内不许再打 API")
    eq(len(again), 2, "缓存命中也要返回同样的结果")

    bad = p.search(lat=22.63, lon=114.13, keywords=["书店"], key="fake",
                   opener=lambda url, timeout=8: _FakeResp(b'{"status":"0","info":"INVALID_USER_KEY"}'),
                   ttl=0, cache={})
    eq(bad, [], "高德报错 → 返回空（失败静默，绝不编地点）")

    none = p.search(lat=22.63, lon=114.13, keywords=[], key="fake", opener=opener, cache={})
    eq(none, [], "没有查询词就不查")


def test_how_far_speaks_like_a_person():
    """距离要说人话：她要是念「916 米」就成导航了。"""
    from app.utils import places as p

    eq(p.how_far(120), "走路两三分钟", "几百米内")
    eq(p.how_far(916), "走路十来分钟", "一公里上下")
    eq(p.how_far(2400), "走路半小时出头", "两公里多")
    eq(p.how_far(9000), "得坐车去", "太远就不建议走过去")
    eq(p.how_far(None), "", "没有距离就不说距离（不许编）")


def test_nearby_intent_needs_both_parts():
    """意图判定必须**两个条件同时成立**（位置词 + 找地方），否则每轮都在打 API。

    单条件会误伤：用户说「今天累」（含"今天"）、「想去书店」（没问附近）都不该触发搜索；
    而"今天有什么好玩的吗"这种双重命中才该查。宁可漏一次（她老实说不确定），
    也不要每轮多一次高德调用 + 一次无关注入。
    """
    from app.utils import places as p

    eq(p.looks_like_nearby_question("附近有什么好玩的"), True, "用户原话")
    eq(p.looks_like_nearby_question("周围有没有书店"), True, "位置词 + 地点词")
    eq(p.looks_like_nearby_question("哪儿能晒太阳"), True, "哪儿 + 地点词")
    eq(p.looks_like_nearby_question("今天有什么好玩的地方吗"), True, "今天+好玩+地方")
    eq(p.looks_like_nearby_question("今天累死了"), False, "只有时间词，不是找地方")
    eq(p.looks_like_nearby_question("我想去书店"), False, "只有地点词、没问附近 → 不查（她可以按已有记忆聊）")
    eq(p.looks_like_nearby_question("附近"), False, "光说『附近』没有要找什么 → 不查")
    eq(p.looks_like_nearby_question(""), False, "空串")


def test_nearby_block_reaches_the_prompt_only_when_asked():
    """端到端（不联网）：问「附近」时注入真实地点，问别的不查也不注入。

    这条测的是 `#101` 真正要保证的事——**她能拿到真数据**，而且
    **不相关的问题不会每轮去打一次高德**（缓存 + 双条件意图判定）。
    """
    from app.services import chat_service as cs_mod
    from app.services.chat_service import ChatService
    from app.utils import geo as _geo
    from app.utils import places as _places

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"
    # 城市**没有兜底默认值**了（`#109`）：不显式给一个，`weather.enabled` 就是假、
    # 「附近」这条路整条关掉（这是刻意的）。这里测的是"有城市时附近能不能拿到真数据"，
    # 所以显式给一个——顺便证明去掉默认值没有把这个功能带坏。
    config.AMAP_CITY = "510100"
    real_geo, real_search = _geo.detect_windows, _places.search
    calls = []
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        svc.profile.data = {"preferences": ["喜欢逛书店", "喜欢晒太阳"],
                            "topics": ["书店", "户外活动"]}
        _geo.detect_windows = lambda *a, **k: {"ok": True, "lat": 22.63, "lon": 114.13,
                                               "acc": 200, "source": "test", "error": ""}

        def fake_search(lat, lon, keywords, key, **kw):
            calls.append(tuple(keywords))
            return [{"name": "颜如玉书店", "label": "购物服务·书店", "distance": 916,
                     "address": "布澜路89号", "how_far": "走路十来分钟"}]

        _places.search = fake_search
        msgs, _ = svc.prepare("附近有什么好玩的")
        body = "\n".join(m["content"] for m in msgs)
        eq("颜如玉书店" in body, True, f"真实店名要进 prompt：{body[-400:]}")
        eq("走路十来分钟" in body, True, "距离要说人话")
        eq("看不到地图" in body, True, "有真数据时才解禁那句反幻觉纪律")
        eq(len(calls), 1, f"查了一次：{calls}")

        msgs2, _ = svc.prepare("今天累死了")
        body2 = "\n".join(m["content"] for m in msgs2)
        eq("颜如玉书店" in body2, False, "不相关的问题不该带着上次的地点")
        eq(len(calls), 1, f"不相关的问题**不许**再打 API：{calls}")
    finally:
        _geo.detect_windows, _places.search = real_geo, real_search
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_favorite_places_render_and_are_append_only():
    """她的偏好要**能被渲染进人设**；演化只能追加、不能整体替换（方案 2）。"""
    from app.models.personality import Personality

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Personality(path=os.path.join(d, "personality.json"))
        p.data["favorite_places"] = ["喜欢下雨天泡书店", "爱逛菜市场"]
        text = p.render()
        eq("我喜欢去的地方" in text, True, f"要渲染成一块：{text[-300:]}")
        eq("喜欢下雨天泡书店" in text, True, "内容要进去")
        eq("favorite_places" in Personality.EVOLVABLE, True,
           "方案 2 之后它**在**白名单里（但走只增不改 + 待确认那条路）")
        eq("favorite_places" in Personality.GROWING_FIELDS, True, "并且属于可长字段")


def test_places_skip_non_leisure_categories():
    """实机踩到的噪音：搜「书店」时高德返回了
    「成都市东方书苑文化发展有限公司（公司企业·公司）」和「东方书苑（培训机构）」——
    名字里带"书苑"就被关键字匹配上了。这种条目她不该推荐，得在解析层挡掉。
    """
    from app.utils import places as p

    data = {"status": "1", "pois": [
        {"name": "颜如玉书店", "type": "购物服务;专卖店;书店", "distance": "916"},
        {"name": "某书苑文化发展有限公司", "type": "公司企业;公司;公司", "distance": "1205"},
        {"name": "某某培训中心", "type": "科教文化服务;培训机构;培训机构", "distance": "800"},
        {"name": "布澜路联创社区公园", "type": "风景名胜;公园广场;公园", "distance": "300"},
    ]}
    hits = p._parse(data)
    names = [h["name"] for h in hits]
    eq("颜如玉书店" in names, True, f"书店要留：{names}")
    eq("布澜路联创社区公园" in names, True, f"公园要留：{names}")
    eq(any("有限公司" in n for n in names), False, f"公司不该出现：{names}")
    eq(any("培训" in n for n in names), False, f"培训机构不该出现：{names}")


def test_growing_base_fields_append_only_and_pin():
    """方案 2（用户拍板）：底座的**偏好类**字段可以演化，但「只增不改 + 钉住」。

    为什么不能像别的字段那样自由替换：`evolve()` 对数组是**替换**语义，
    模型给一条新偏好就会把你手写的那三条冲掉（`#046` 踩过同样的连坐丢失）。
    所以这里由**代码**保证追加语义，而不是指望模型自觉。
    """
    from app.models.personality import Personality

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Personality(path=os.path.join(d, "personality.json"))
        p.data["favorite_places"] = ["爱逛书店", "喜欢菜市场"]
        p._save()

        # 模型想整体替换（只给一条新的）→ 旧的两条必须还在，新的追加进去
        p.evolve({"favorite_places": ["爱去公园"]}, reason="测试")
        places = [x["text"] if isinstance(x, dict) else x for x in p.data["favorite_places"]]
        eq("爱逛书店" in places, True, f"旧条目不许被冲掉：{places}")
        eq("喜欢菜市场" in places, True, f"同上：{places}")
        eq("爱去公园" in places, True, f"新条目要追加：{places}")
        eq(len(places), 3, f"一增两留，共三条：{places}")

        # 演化来的条目被「钉住」：再演化也不能动它
        p.evolve({"favorite_places": ["爱去公园", "爱爬山"]}, reason="第二次")
        places2 = [x["text"] if isinstance(x, dict) else x for x in p.data["favorite_places"]]
        eq(places2.count("爱去公园"), 1, f"钉住的条目不许重复，也不许消失：{places2}")
        eq("爱爬山" in places2, True, f"新条目照常追加：{places2}")
        eq(len(places2), 4, f"{places2}")

        # 渲染时不许把内部标记 `_pinned` 漏出去
        text = p.render()
        eq("_pinned" in text, False, f"内部标记不能进 prompt：{text[-300:]}")

        # cares 是同一个规则
        p.data["cares"] = ["年糕"]
        p.evolve({"cares": ["下雨天爱泡书店"]}, reason="测试")
        cares = [x["text"] if isinstance(x, dict) else x for x in p.data["cares"]]
        eq("年糕" in cares and "下雨天爱泡书店" in cares, True, f"cares 同样只增不改：{cares}")


def test_growing_fields_go_through_pending_not_auto_apply():
    """偏好类改动**不许自动生效**：拆出来走「待确认」，由人拍板。

    其余字段（traits/behavior…）照旧直接应用——它们是"怎么说"，改错了回滚就好；
    而底座偏好属于"她是谁"，改一次要人点头。
    """
    from app.models.personality import Personality

    eq(Personality.GROWING_FIELDS, {"cares", "favorite_places"}, "可长字段只有这两个")
    direct, pending = Personality.split_append_fields(
        {"traits": ["更热心"], "favorite_places": ["爱爬山"], "cares": ["爱逛早市"]})
    eq(direct, {"traits": ["更热心"]}, f"非底座字段直接应用：{direct}")
    eq(set(pending), {"favorite_places", "cares"}, f"底座偏好要人审：{pending}")
    eq(Personality.GROWING_FIELDS <= set(Personality.EVOLVABLE), True,
       "可长字段仍要在白名单里（否则连待确认都进不去）")
    eq("origin" in Personality.GROWING_FIELDS or "name" in Personality.GROWING_FIELDS, False,
       "身世/名字不在这套机制里（它们保持钉死）")


def test_evolution_routes_base_preferences_to_pending():
    """端到端（不联网）：演化想改她的偏好时 → 进待确认，**不自动生效**；
    同一批里的其它字段照旧直接应用。"""
    from app.services import chat_service as cs_mod
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = ""
    real = cs_mod.chat_json
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        svc.store.sessions["default"] = [
            {"role": "user", "content": "你平时爱去哪呀", "ts": __import__("time").time()},
            {"role": "assistant", "content": "我下雨天就爱往书店钻",
             "ts": __import__("time").time()},
        ]
        cs_mod.chat_json = lambda *a, **k: (
            {"traits": ["热心，先帮你想办法，再陪你吐槽"],
             "favorite_places": ["爱往书店钻"], "cares": ["爱逛早市"]}, None)

        ok, note = svc.evolve_personality()
        eq(ok, True, f"演化本身要成功：{note}")
        places = [x["text"] if isinstance(x, dict) else x
                  for x in (svc.personality.data.get("favorite_places") or [])]
        eq("爱往书店钻" in places, False, f"**没有你点头，偏好不许生效**：{places}")
        pend = svc.personality.pending_list()
        eq(len(pend), 1, f"要有一条待确认：{pend}")
        eq(set(pend[0]["patch"]), {"favorite_places", "cares"}, f"待确认里只放偏好：{pend[0]}")
        eq("待确认" in note, True, f"要告诉人去看哪儿：{note}")

        # 人拍板之后才生效，而且从这一刻起被钉住
        out = svc.personality.resolve_pending(0, accept=True)
        eq("已采纳" in out, True, f"{out}")
        places2 = [x["text"] if isinstance(x, dict) else x
                   for x in (svc.personality.data.get("favorite_places") or [])]
        eq("爱往书店钻" in places2, True, f"采纳后才生效：{places2}")
        pinned = [x for x in svc.personality.data["favorite_places"] if isinstance(x, dict)]
        eq(any(x.get("_pinned") for x in pinned), True, f"要钉住：{pinned}")
    finally:
        cs_mod.chat_json = real
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_strip_time_prefix_only_at_the_start():
    """T83：剥掉**开头**的内部时间标记，但绝不碰句中/正常的时间表达。

    真调模型实测（`#101`）：三轮里两轮以「（今天晚上 21:26）」开头——那是内部标记。
    提示词两轮都压不住，所以在输出侧兜一道；而兜这一道的最大风险是**误伤**，
    所以这条用例一半是"该剥的剥掉"，一半是"不该剥的一个字都不许动"。
    """
    from app.utils import answers as a

    cases = [
        ("（今天晚上 21:26）累就赶紧歇着吧", "累就赶紧歇着吧", True, "括号 + 时段 + 时刻"),
        ("（今天下午 14:05），好呀", "好呀", True, "剥完残留的逗号要一起吃掉"),
        ("（昨天 21:30）在的", "在的", True, "昨天"),
        ("（3 天前 14:00）那会儿", "那会儿", True, "N 天前"),
        ("[今天晚上 21:26] 在呢", "在呢", True, "方括号"),
        ("（2026年9月28日 周一 21:26）嗯", "嗯", True, "绝对日期"),
        ("今天晚上 21:26 我在的", "我在的", True, "没有括号也认（她照样念）"),
        ("（今天晚上）累了一天啦，那就歇着吧", "累了一天啦，那就歇着吧", True,
         "**只有时段、没有时刻**——真调模型踩到的变体，第一版正则漏了它"),
        ("（今天晚上 21:26）", "", True, "整条回答只有标记 → 剥成空"),
        ("累就赶紧歇着吧", "累就赶紧歇着吧", False, "正常回答不动"),
        ("现在 21:26 了，早点睡", "现在 21:26 了，早点睡", False, "**句中**的时间一个字都不许动"),
        ("明天 10 点见", "明天 10 点见", False, "约定时间不动（这是正常表达）"),
        ("（今天）在的", "在的", True,
         "只有日期没有时刻——也认（她真的会这么念）。这条我一开始判成『不该剥』，"
         "是拿自己的直觉当标准；改判的依据是**她实际会输出它**"),
        ("（我想了想）还是去吧", "（我想了想）还是去吧", False,
         "括号里是普通内容 → 一个字都不许动"),
    ]
    for raw, want, stripped, label in cases:
        got, flag = a.strip_time_prefix(raw)
        eq(got, want, f"{label}：{raw!r}")
        eq(flag, stripped, f"{label} 的判定标志")


def test_stream_prefix_stripper_holds_then_releases():
    """流式清洗要**真的流式**：前几块压住，判定后原样放行，不漏字、不吞正文。"""
    from app.utils import answers as a

    seen = []
    s = a.PrefixStripper(hold=8, on_strip=lambda r: seen.append(r))
    out = []
    for chunk in ["（今天", "晚上 21:", "26）累就", "赶紧歇着吧"]:
        out += s.feed(chunk)
    out += s.flush()
    eq("".join(out), "累就赶紧歇着吧", f"拼起来要完整：{out}")
    eq(len(seen), 1, f"要留痕一次：{seen}")

    # 正常回答：压住的那几帧要原样吐出来，一个字都不能丢
    s2 = a.PrefixStripper(hold=8)
    out2 = []
    for chunk in ["累就", "赶紧", "歇着吧，", "别熬了"]:
        out2 += s2.feed(chunk)
    out2 += s2.flush()
    eq("".join(out2), "累就赶紧歇着吧，别熬了", f"正常回答不许被吃掉：{out2}")

    # **极端分块**：标记被切得比窗口还碎（hold=3）时，必须一直等它闭合。
    # 这一段是我手动 trace 才发现的：到窗口上限强制定案，会把「（今天」当正文吐出去。
    for hold, chunks, want in [
        (3, ["（今", "天晚", "上 21", ":26）", "我在的"], "我在的"),
        (5, ["（今天", "晚上）", "好呀"], "好呀"),
        (2, ["（昨", "天 ", "21:", "30）", "在的"], "在的"),
    ]:
        st = a.PrefixStripper(hold=hold)
        acc = []
        for c in chunks:
            acc += st.feed(c)
        acc += st.flush()
        eq("".join(acc), want, f"hold={hold} 的碎片分块要照样剥干净：{acc}")


def test_weather_uses_forecast_when_lives_is_empty():
    """实况为空时**必须用预报**，而且注入行要写明是哪天的。

    真实故障（用户 2026-09-29 的反馈："28 号的天气状态还挂着"）：高德对某些 adcode
    只给预报不给实况，老代码 `if not live.get("weather"): return None` → 连预报一起丢、
    缓存永不更新、**一行日志都没有**；那天成都在下雨，她却在说"今天晴 30 度"。
    """
    from app.utils import weather as w

    calls = []

    def fake_call(self, extra):
        calls.append(extra.get("extensions"))
        if extra.get("extensions") == "base":
            return {"status": "1", "lives": []}          # ← 实况为空（真实情况）
        return {"status": "1", "forecasts": [{"city": "成都市", "casts": [
            {"date": "2026-09-29", "dayweather": "小雨", "nightweather": "小雨",
             "daytemp": "29", "nighttemp": "20"},
            {"date": "2026-09-30", "dayweather": "多云", "nighttemp": "21", "daytemp": "30"},
        ]}]}

    real = w.WeatherCache._call
    w.WeatherCache._call = fake_call
    try:
        wc = w.WeatherCache("k", "510100", ttl=0)
        data = wc.refresh()
        eq(bool(data), True, "实况为空也要拿到数据（否则整块天气永远是旧的）")
        line = wc.line() or ""
        eq("小雨" in line, True, f"要用上预报：{line}")
        eq("2026-09-29" in line, True, f"**必须写明是哪天的天气**（否则跨天会被当成今天）:{line}")
        eq("明天多云" in line, True, f"明天的预报也要在：{line}")
        eq("成都" in line, True, f"城市名取预报里的（实况为空时）：{line}")
    finally:
        w.WeatherCache._call = real


def test_weather_refresh_failure_is_visible_and_retries_soon():
    """刷新失败要**有日志**、**能很快重试**（老代码：无日志 + 冻结整整一个 TTL）。"""
    import logging
    import time as _t

    from app.utils import weather as w

    real = w.WeatherCache._call
    w.WeatherCache._call = lambda self, extra: {}          # 两边都空 → 失败
    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec)
    logger = logging.getLogger("app.weather")
    logger.addHandler(handler)
    try:
        wc = w.WeatherCache("k", "510100", ttl=3600)
        eq(wc.refresh(), None, "失败时返回 None（调用方保留上一次的好数据）")
        eq(any(r.levelno >= logging.WARNING for r in records), True,
           f"失败必须留 WARNING（失败要可见）：{[r.getMessage() for r in records]}")
        # 关键：失败后**不该**等满一个 TTL 才重试
        eq(int(wc._ts), int(_t.time() - wc.ttl + w.FAIL_RETRY),
           "失败后重试窗口要缩短到 FAIL_RETRY，而不是等满 TTL")
    finally:
        w.WeatherCache._call = real
        logger.removeHandler(handler)


def test_weather_city_comes_from_world_book_before_first_fetch():
    """城市**先定、后拉**：世界书里写了城市，启动就只拉那一个（`#109`）。

    真实日志（用户 2026-09-29 19:55 重启服务时截的图）：
        [app.weather] 天气已刷新：杭州，2026-09-29：小雨，实况雾，24°C…
        [app.chat]    按世界书把天气城市设为：成都
        [app.weather] 天气已刷新：成都，多云，实况多云，29°C…
    ——顺序反了：先按配置默认（`config.py` 写死的 `330100`=杭州）拉一次，
    再读世界书覆盖成成都。**结果虽然对，但那一次杭州是白拉的高德请求**，
    而且日志把"正在用的城市"和"待覆盖的默认值"混在一起打印，
    看起来像她认错了城市。
    """
    import json
    import tempfile

    from app.services.chat_service import ChatService
    from app.utils import weather as _weather

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"
    config.AMAP_CITY = "330100"          # 配置默认＝杭州（就是那个写死的默认值）
    with open(os.path.join(tmp, "world.json"), "w", encoding="utf-8") as f:
        json.dump({"entries": [{"text": "用户住在成都，养了一只叫年糕的橘猫。"}]},
                  f, ensure_ascii=False)

    seen: list[str] = []
    real = _weather.WeatherCache.refresh

    def spy(self):
        seen.append(self.city)           # **不联网**：只记"拉了哪个城市"
        return self._data

    _weather.WeatherCache.refresh = spy
    svc = None
    try:
        svc = ChatService()
        eq(seen, ["510100"],
           f"启动**只该拉一次**，而且拉的是世界书那个城市（杭州那次是白拉的）：{seen}")
        eq(svc.weather.city_name, "成都", f"天气缓存要是成都：{svc.weather.city_name}")
    finally:
        _weather.WeatherCache.refresh = real
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_weather_does_not_guess_a_city():
    """世界书没写城市、也没配 `WB_AMAP_CITY` 时：**不猜**，天气关掉并说明白（`#109`）。

    老行为是**悄悄**按 `330100`（杭州）查：她会真的以为你在杭州（聊天气、推荐附近
    的地方），而日志里看不出来——那正是 D10 说的"静默失灵比显式失败更危险"。
    现在改成：不注入天气 + 一条 WARNING + `/city` 里直接告诉你怎么办。
    """
    import json
    import logging
    import tempfile

    from app.services.chat_service import ChatService
    from app.utils import weather as _weather

    # ① 配置层：默认值里**不许再有那个写死的城市**（源级断言——它当初就藏在这里）
    #    只看赋值那一行，不看注释（注释里会提到"老代码是 or 330100"这段历史）
    cfg_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "config.py"), encoding="utf-8").read()
    line = next((ln for ln in cfg_src.splitlines()
                 if ln.startswith("AMAP_CITY =")), "")
    eq(line, 'AMAP_CITY = os.environ.get("WB_AMAP_CITY") or ""',
       f"`AMAP_CITY` 读不到就该是空串（空串＝不知道你在哪），现在是：{line!r}")
    eq("330100" in line, False, f"兜底不许再写死 330100=杭州：{line!r}")

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = "fake-key"         # key 有，但**没有任何城市来源**
    config.AMAP_CITY = ""
    with open(os.path.join(tmp, "world.json"), "w", encoding="utf-8") as f:
        json.dump({"entries": [{"text": "用户养了一只叫年糕的橘猫。"}]},
                  f, ensure_ascii=False)   # 没写城市

    seen: list[str] = []
    real = _weather.WeatherCache.refresh
    _weather.WeatherCache.refresh = lambda self: seen.append(self.city) or self._data
    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec)
    logger = logging.getLogger("app.chat")
    logger.addHandler(handler)
    svc = None
    try:
        svc = ChatService()
        eq(svc.weather.enabled, False, "没有城市来源 → 天气功能关掉（不许猜一个）")
        eq(svc.weather.city, "", f"不许退回杭州：{svc.weather.city!r}")
        eq(seen, [], f"一次都不该拉（拉了就是又在猜）：{seen}")
        eq(any(r.levelno >= logging.WARNING for r in records), True,
           f"要说出来（失败要可见）：{[r.getMessage() for r in records]}")
        out = svc.run_command("/city")
        eq("杭州" in out, False, f"不许再出现那个默认城市：{out}")
        eq("/city" in out, True, f"要给出办法：{out}")
        # ② `WB_AMAP_CITY` 显式配了就得认（不许因为"不猜"把这个旋钮也拧掉）
        config.AMAP_CITY = "330100"
        svc2 = ChatService()
        try:
            eq(svc2.weather.city, "330100", "显式配置要生效")
            eq(seen, ["330100"], f"配了就按配置拉一次：{seen}")
        finally:
            try:
                svc2.close()
            except Exception:
                pass
        # ③ 手动说了立刻生效（这条路一直没问题，别被这次改动带坏）
        out2 = svc.run_command("/city 成都")
        eq("成都" in out2, True, f"说了就认：{out2}")
        eq(svc.weather.city_name, "成都", "换成成都")
        eq(svc.weather.enabled, True, "有城市了就该启用")
    finally:
        _weather.WeatherCache.refresh = real
        logger.removeHandler(handler)
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_reconcile_marks_sides_and_accepts_both_forms():
    """第 1 步（`T85`）：对账要给事实记忆打**视角标记**，并且两种回抄都认。

    为什么要标记：同一次互动会同时存在「用户侧」和「她的经历侧」两条，那是 `actor`
    机制的正常产物（真库 60 条活跃里 21/21 两侧各半），而不是两条互相矛盾的事实。
    不把标记给模型，它就把两侧报成"重复"——实测真库上 21 处里 20 处是这种噪音。
    """
    from app.models import memory_read as mr

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        store._add_memories(["用户带年糕去了广场"], "fact", actor="user")
        store._add_memories(["我带了小饼干去广场赴约"], "fact", actor="assistant")
        store._add_memories(["用户住在成都"], "world")

        seen = {}

        def fake_chat_json(client, model, messages, **kw):
            seen["prompt"] = messages[0]["content"]
            # 故意一条带前缀、一条不带：两种都必须认（否则会被当"编造的原句"丢掉）
            return ({"issues": [
                {"type": "conflict",
                 "items": ["【她】我带了小饼干去广场赴约", "用户带年糕去了广场"],
                 "reason": "测试用"},
            ]}, None)

        real = mr.chat_json
        mr.chat_json = fake_chat_json
        try:
            issues = store.check_consistency()
        finally:
            mr.chat_json = real

        p = seen.get("prompt") or ""
        eq("【他】用户带年糕去了广场" in p, True, f"用户侧要标【他】：{p[:400]}")
        eq("【她】我带了小饼干去广场赴约" in p, True, f"她的经历要标【她】：{p[:400]}")
        eq("双方视角" in p, True, "要说明标记的含义（否则模型照样报重复）")
        eq("能不能同时为真" in p, True, "判断口径要收敛成一句话")
        eq(len(issues), 1, f"带前缀/不带前缀都要认得出来：{issues}")
        eq(len(issues[0]["items"]), 2, f"{issues}")


def test_embed_splits_into_batches_of_twenty():
    """第 2 步的配套修复：embedding **必须分片**（接口单次上限 20 条）。

    实测 `qwen3.7-text-embedding`：一次传 22 条 → 400
    `batch size is invalid, it should not be larger than 20` → 整批失败。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM()
        store = _mk_store(d, fake)
        calls_before = len(fake.embed_inputs)
        got = store._embed([f"第 {i} 条" for i in range(25)])
        eq(len(got), 25, "25 条都要拿到向量")
        batch = fake.embed_inputs[calls_before:]
        eq(len(batch), 2, f"25 条应分 2 片：{len(batch)}")
        eq([len(b) for b in batch], [20, 5], f"每片不超过 20 条：{[len(b) for b in batch]}")


def test_embed_one_bad_batch_does_not_drop_the_others():
    """**一片彻底坏掉不该连坐其它片**（用户 2026-09-29 的原话："别超过 20 条就全体丢失"）。

    老口径：任一片抛错 → `_embed` 返回 None → 调用方"整轮不写"→ 另一片已经算成功的向量
    也一起扔。新口径：片内失败只让**片内**那几条返回 None（`fail_times=2` = 第一片发两次
    都失败），第二片照常返回向量；真正没拿到向量的条目由 `_add_memories` 落到 pending。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(fail_times=2)              # 前两次调用失败 = 第一片（含重试）全灭
        store = _mk_store(d, fake)
        got = store._embed([f"第 {i} 条" for i in range(25)])
        eq(got is None, False, "不该整批返回 None（第二片是好的）")
        eq(len(got), 25, f"位置要对齐（失败的给 None）：{None if got is None else len(got)}")
        eq(got[:20], [None] * 20, "第一片失败 → 那 20 条是 None")
        eq(all(v is not None for v in got[20:]), True, "第二片照常拿到向量")


def test_embed_retries_once_on_transient_failure():
    """瞬时失败（一次抖动）要**在片内重试一次**，而不是直接把这几条推给 pending。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fake = _FakeLLM(fail_times=1)              # 只有第一次调用失败
        store = _mk_store(d, fake)
        got = store._embed(["抖一下就算了"])
        eq(len(got or []), 1, f"重试后要拿到向量：{got}")
        eq(got[0] is not None, True, "而且不是 None")
        eq(fake.embed_calls, 2, f"应该是「失败一次 + 重试一次」：{fake.embed_calls}")


def test_add_memories_keeps_failed_embeddings_in_pending():
    """拿不到向量的条目**进 pending 等补算**，而不是整轮丢光。

    这是用户 2026-09-29 那条要求的核心："别到超过 20 条就全体丢失"。
    pending 表本来就有出口（`_recover_missing_vectors` 补算 + 孤儿捡回），
    所以"暂时没向量"的正确归宿是这里，而不是垃圾桶。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM(fail_always=True))     # 全部失败
        added, dup = store._add_memories(["一条注定没向量的记忆"], "fact")
        eq((added, dup), ([], 0), "没向量就不进活跃集（老口径不变）")
        eq(len(store.pending), 1, f"但必须进 pending：{[m['text'] for m in store.pending]}")
        row = store.db.execute("SELECT text FROM pending").fetchone()
        eq(row["text"] if row else None, "一条注定没向量的记忆", "pending 表真的要落库")
        kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM events")]
        eq("embed_fail" in kinds, True, f"失败要留痕：{kinds}")

        # 下一轮向量可用了：补算把它救回来
        store._FakeLLM_backup = store.client
        store.client = _FakeLLM()
        store.embed_client = store.client
        store._recover_missing_vectors()
        eq(any(m["text"] == "一条注定没向量的记忆" for m in store.memories), True,
           f"补算后要回到活跃集：{[m['text'] for m in store.memories]}")


def test_recover_missing_vectors_handles_partial_failure():
    """补算路径也要扛住"部分失败"。

    早先这里是 `np.asarray(vecs)`——`_embed` 分片后列表里可能有 `None`，
    一进去就抛 inhomogeneous shape，**整批补算崩掉**（连已经成功的那些也白算）。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        # 先造 3 条没有向量的记忆（走"整体失败 → 进 pending"这条路）
        store = _mk_store(d, _FakeLLM(fail_always=True))
        store._add_memories(["没有任何向量的第一条", "没有任何向量的第二条",
                             "没有任何向量的第三条"], "fact")
        eq(len(store.pending), 3, f"三条都该在 pending：{len(store.pending)}")

        # 现在让"第一片"（这里是全部 3 条，一次调用）失败一次、重试成功
        store.embed_client = _FakeLLM(fail_times=1)
        store._recover_missing_vectors()
        eq(len(store.pending), 0, f"重试成功后不该还有待补：{[m['text'] for m in store.pending]}")
        eq(len(store.memories), 3, f"三条都要回到活跃集：{[m['text'] for m in store.memories]}")
        eq(store.vectors is not None and len(store.vectors) == 3, True,
           "向量矩阵要正好三条（对不齐就会张冠李戴）")

        # 换一批：这次让它**彻底失败**（两次都失败）→ 必须原样留在 pending，不能崩
        store2 = _mk_store(tempfile.mkdtemp(), _FakeLLM(fail_always=True))
        store2._add_memories(["补算必失败的一条"], "fact")
        store2._recover_missing_vectors()
        eq(len(store2.pending), 1, "补不上就留在 pending（下次再试），不许丢")
        eq(len(store2.memories), 0, "也不许塞进活跃集（没有向量就没法召回）")


def test_scene_labels_group_cross_turn_restatements():
    """第 2 步（`T85`）：提炼器给 **scene 标签**，写入时按场景分组 → 跨轮复述能可靠归组。

    为什么只有这条路（`#103` 实测）：相似度（该合 ≥0.709 / 不该合 ≤0.737，重叠）、
    时间窗（同一轮里也有两件不相干的事）、"同一轮"（只覆盖 13%）**都不成立**。
    场景只有提炼器知道，所以让它标。
    """
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        # 第一轮：广场晒太阳这场戏里的两条
        c1 = _ScriptedLLM(extract=_json.dumps({
            "facts": [{"text": "用户和助手在广场东边大树下晒太阳", "scene": "广场晒太阳"},
                      {"text": "助手带了一小袋小饼干", "scene": "广场晒太阳"}],
            "self": [], "slots": [], "world": [], "constraints": [], "drop": []},
            ensure_ascii=False))
        store = _mk_store(d, c1)
        store.add_turn("到广场了", "我在东边大树下")
        groups = {m["text"]: m.get("event_group") for m in store.memories}
        g1 = set(v for v in groups.values() if v)
        eq(len(g1), 1, f"同场景的两条要归到同一组：{groups}")
        eq(all(v is not None for v in groups.values()), True, f"两条都该有组号：{groups}")

        # 第二轮：**同一场戏的复述**（不同措辞）+ 一件不相干的事
        c2 = _ScriptedLLM(extract=_json.dumps({
            "facts": [{"text": "助手在广场把饼干摆开等用户和年糕", "scene": "广场晒太阳"},
                      {"text": "用户早上没吃早饭", "scene": "早饭"}],
            "self": [], "slots": [], "world": [], "constraints": [], "drop": []},
            ensure_ascii=False))
        store.client = c2
        store.add_turn("饼干摆好了", "嗯")
        by_text = {m["text"]: m.get("event_group") for m in store.memories}
        plaza = [v for k, v in by_text.items() if "广场" in k or "饼干" in k]
        eq(len(set(plaza)), 1, f"跨轮的同一场戏要归到同一组（这就是合并）：{by_text}")
        bfast = by_text.get("用户早上没吃早饭")
        eq(bfast is not None and bfast != plaza[0], True,
           f"不相干的事**绝不能**并进那场戏：{by_text}")


def test_scene_absent_falls_back_to_turn_group():
    """没给 scene（模型没标 / 旧版提示词）时退回"同一轮"，**旧格式不许破**。"""
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        # 老格式：facts 是纯字符串数组
        c = _ScriptedLLM(extract=_json.dumps(
            {"facts": ["用户带年糕去了广场", "用户喜欢吃小饼干"]}, ensure_ascii=False))
        store = _mk_store(d, c)
        store.add_turn("走吧", "好")
        rows = [(m["text"], m.get("event_group")) for m in store.memories]
        eq(len(rows), 2, f"旧格式照样要能写进来：{rows}")
        eq(len({g for _, g in rows}), 1, f"同一轮里没 scene → 归到同一组：{rows}")
        eq(all(g for _, g in rows), True, f"组号不能为空：{rows}")


def test_timeline_folds_same_scene():
    """时间轴按场景折叠：同一次互动不再刷屏，但**每条都还在**（数据没合并）。"""
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        c = _ScriptedLLM(extract=_json.dumps({
            "facts": [{"text": "助手 2026-09-29 在广场东边大树下晒太阳", "scene": "广场晒太阳"},
                      {"text": "助手 2026-09-29 带了小饼干去广场", "scene": "广场晒太阳"}],
            "self": [], "slots": [], "world": [], "constraints": [], "drop": []},
            ensure_ascii=False))
        store = _mk_store(d, c)
        store.add_turn("到广场了", "我在东边大树下")
        rows = store.timeline()
        eq(len(rows), 2, f"底层数据一条都不许少：{[r['text'] for r in rows]}")
        folded = [r for r in rows if not r.get("grouped_under")]
        eq(len(folded), 1, f"折叠后只显示一条：{[(r['text'], r.get('grouped_under')) for r in rows]}")
        eq(sum(1 for r in rows if r.get("grouped_under")), 1, "另一条标成跟随")
        eq(rows[0].get("group_size"), 2, f"要能看出这组有几条：{rows[0]}")


def test_event_group_rebuild_buckets_by_minute():
    """存量条目没有 scene 标签 → 加载时按"同一轮"补组，**按分钟分桶**。

    为什么不能要求 ts 完全相同：真库里同一轮的条目 ts 是逐条不同的（差几秒到几十秒），
    精确相等一条都合不上（实测 27 条时间轴 → 0 折叠）。
    """
    import json as _json
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        store.memories = [
            {"id": 1, "text": "用户和助手到了广场", "kind": "fact", "ts": 1000.20,
             "actor": "user", "hits": 0, "last_hit": 0},
            {"id": 2, "text": "我带了小饼干", "kind": "fact", "ts": 1000.35,
             "actor": "assistant", "hits": 0, "last_hit": 0},
            {"id": 3, "text": "另一轮的事（差几秒）", "kind": "fact", "ts": 1009.0,
             "actor": "user", "hits": 0, "last_hit": 0},
        ]
        store._rebuild_event_groups()
        g = {m["id"]: m.get("event_group") for m in store.memories}
        eq(g[1], g[2], f"同一分钟内的两条应同组：{g}")
        eq(g[1] != g[3], True, f"隔很久的不能并进来：{g}")
        eq(all(v for v in g.values()), True, f"每条都要有组号：{g}")

        # 已经带 scene 组号的**不许被覆盖**（真场景优先级更高）
        store.memories.append({"id": 4, "text": "带场景的", "kind": "fact", "ts": 1035.0,
                               "actor": "user", "hits": 0, "last_hit": 0,
                               "event_group": "scene:广场晒太阳:1"})
        store._rebuild_event_groups()
        eq(store.memories[-1]["event_group"], "scene:广场晒太阳:1", "不许覆盖 scene 组号")


def test_corrupt_personality_is_quarantined_not_overwritten():
    """T73：`personality.json` **解析失败时不许用默认模板覆盖保存**。

    原来的路径：`_read` 吞掉异常返回 None → `_load` 看到 None 就 `_save()` →
    **手写的人设与 20 版历史被默认模板盖掉，一次就没了**（对比 `user.py`/`emotion.py`
    都更安全：它们失败时不落盘）。这类"静默覆盖用户数据"是这个项目最忌的失败模式。
    """
    from app.models.personality import Personality

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "personality.json")
        # 造一个"看起来像人设、其实结构错"的文件（真实会遇到的：手编漏了逗号/被截断）
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"current": {"name": "沈知夏", "traits": ["热心"], }')   # ← 尾逗号
        before = open(path, encoding="utf-8").read()

        import logging as _logging
        records = []
        handler = _logging.Handler()
        handler.emit = lambda rec: records.append(rec)
        logger = _logging.getLogger("app.personality")
        logger.addHandler(handler)
        try:
            p = Personality(path=path)
        finally:
            logger.removeHandler(handler)

        # ① 不许把坏文件原地盖掉：坏文件必须被隔离留档
        corrupt = path + ".corrupt"
        eq(os.path.exists(corrupt), True, "坏文件要留档（改名 .corrupt），不能就地覆盖")
        eq(open(corrupt, encoding="utf-8").read(), before, "留档内容要与原文件一致")
        eq(any(r.levelno >= _logging.ERROR for r in records), True,
           f"要打 ERROR 让人看见：{[r.getMessage() for r in records]}")
        # ② 恢复出可用的人设（默认模板），进程能继续跑
        eq(bool(p.data.get("name")), True, f"要有可用的兜底人设：{p.data.get('name')}")
        eq(p.history, [], "历史确实读不出来了（这是代价，但原始文件还在）")
        # ③ 文件已恢复后，再启动不该继续报警
        records.clear()
        handler2 = _logging.Handler()
        handler2.emit = lambda rec: records.append(rec)
        logger.addHandler(handler2)
        try:
            Personality(path=path)
        finally:
            logger.removeHandler(handler2)
        eq(any(r.levelno >= _logging.WARNING for r in records), False,
           f"文件已恢复，不该继续报警：{[r.getMessage() for r in records]}")


def test_missing_personality_is_created_normally():
    """文件**不存在**时照旧生成模板（别把"首次运行"也当成损坏）。"""
    from app.models.personality import Personality

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "personality.json")
        p = Personality(path=path)
        eq(os.path.exists(path), True, "首次运行要生成模板文件")
        eq(os.path.exists(path + ".corrupt"), False, "**不该**留下 .corrupt（那不是损坏）")
        eq(bool(p.data.get("name")), True, f"要有可用人设：{p.data.get('name')}")


def test_benchmark_runners_force_isolated_data_dir():
    """T74：两个 runner **不许**用 `os.environ.setdefault("DATA_DIR", ...)`。

    为什么这是真风险：`--fresh` 会 `rmtree` 数据目录、`reset_case` 每例都清表。
    外部若已经导出 `DATA_DIR`（Dockerfile 的 `ENV DATA_DIR=/data`、CI 变量、本机 shell），
    `setdefault` **不会覆盖**它 → 清场精确地作用在真实记忆上。
    `run_opinions.py` 早就做对了（强制赋值 + 断言），另两个 runner 没跟上。
    """
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("run_bench.py", "run_persona.py", "run_opinions.py"):
        p = os.path.join(HERE, "benchmark", name)
        src = open(p, encoding="utf-8").read()
        eq('setdefault("DATA_DIR"' not in src, True,
           f"{name} 不许用 setdefault 设 DATA_DIR（它不会覆盖外部变量）")
        eq('os.environ["DATA_DIR"] = DATA_DIR' in src, True,
           f"{name} 要**强制**赋值 DATA_DIR")
        # 顺序也要对：强制赋值必须发生在 `from app import config` **之前**
        # （config 在 import 期就读 DATA_DIR，晚一步就白设）
        eq(src.index('os.environ["DATA_DIR"] = DATA_DIR') < src.index("from app import config"),
           True, f"{name} 里强制赋值必须在 import config 之前")
        eq("config.DATA_DIR) != os.path.abspath(DATA_DIR)" in src, True,
           f"{name} 要在 import 之后断言数据目录真的是隔离目录")


def test_benchmark_runner_refuses_wrong_data_dir_env():
    """端到端：把 DATA_DIR 指向别处再起 runner，它必须**直接拒绝运行**（而不是照跑）。

    这是"宁可起不来"的具体形态——比"跑完才发现清空了真实记忆"好得多。
    """
    import subprocess
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["DATA_DIR"] = tempfile.mkdtemp()      # 故意指到别处（模拟外部已导出）
    env["PYTHONIOENCODING"] = "utf-8"
    py = sys.executable
    # ⚠ 不能拿 `--help` 当探针：argparse 处理它时**正常退出 0**，守卫根本轮不到
    # （我第一版就这么写，于是"测试失败"其实说明守卫生效了）。改成"**能导入即证明**"：
    # 守卫断言的是 `config.DATA_DIR == 隔离目录`，而 config 是被强制覆盖后才导入的，
    # 所以导入成功 == 覆盖生效；一旦哪天退回 setdefault，config 会读到外部值、
    # 断言失败 → `raise SystemExit` → 导入报非零退出码。
    for name in ("run_bench", "run_persona"):
        proc = subprocess.run([py, "-c", f"import sys; sys.path.insert(0, r'{HERE}/benchmark'); import {name}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=env, timeout=120)
        eq(proc.returncode == 0, True,
           f"{name} 在外部已导出 DATA_DIR 时没能把数据目录强制成隔离目录（导入即断言失败），"
           f"退出码 {proc.returncode}")


def test_corrupt_personality_guard_is_wired_in_source():
    """T73 的静态守卫：`_load` 必须真的**隔离**坏文件，而不是拿模板覆盖它。

    为什么这里看源码而不是只看行为：行为测试只能证明"我构造的那种坏法被挡住了"，
    而这条守卫要防的是**所有**读不出来的情况（截断/权限/被占用）。
    """
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(HERE, "app", "models", "personality.py"),
               encoding="utf-8").read()
    for token, why in (
            ("PersonalityFileCorrupt", "要有个专门的异常类型区分「文件坏了」与「文件不存在」"),
            ("_quarantine", "坏文件要改名留档，不能就地覆盖"),
            ("corrupt_src", "读到坏文件时要走隔离分支"),
    ):
        eq(token in src, True, f"{why}（缺 {token}）")
    # `_read` 不许再吞异常返回 None——那正是覆盖用户人设的入口
    read_body = src.split("def _read", 1)[1].split("def ", 1)[0]
    eq("PersonalityFileCorrupt(str(exc)) from exc" in read_body, True,
       "`_read` 必须**抛出**而不是返回 None（返回 None 会被当成「没有文件」→ 覆盖）")


def test_place_keywords_prefer_her_places_over_profile():
    """T84：查询词**以她的偏好为主、画像只做补充**，并注明出处。

    改之前两边平权混在一起：`favorite_places`（她爱去哪）与 `user_profile.preferences`
    （你是谁 / 你喜欢什么）各贡献一次同类查询，改了一边另一边还会把旧偏好喂回来，
    而且**看不出这条查询词是谁提的**。现在的口径：
      · 她的 `favorite_places` → 主（她带路，符合"她想带你去哪"的人设）；
      · 用户的画像 preferences/topics → 补充（他喜欢什么也要照顾到）；
      · 同一类去重，但**要能看出两类分别贡献了什么**。
    """
    from app.services import chat_service as cs_mod
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = ""
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        svc.personality.data["favorite_places"] = ["爱逛书店和图书馆", "喜欢菜市场"]
        svc.profile.data = {"preferences": ["喜欢看电影", "喜欢晒太阳"], "topics": []}
        kws, hers, his = svc._place_keywords_split()
        eq(hers, ["书店", "图书馆", "超市"], f"她那边贡献的：{hers}")
        eq("电影院" in his, True, f"你那边贡献的：{his}")
        eq(kws[:3], ["书店", "图书馆", "超市"],
           f"**她的排前面**（她带路的人设）：{kws}")
        eq(set(kws) >= {"书店", "图书馆", "超市", "电影院"}, True, f"两边都要照顾到：{kws}")
        eq(len(kws), len(set(kws)), f"同一类不许重复出现：{kws}")

        # 她的偏好为空时：全部靠画像补充（不能因为"她没写"就不查了）
        svc.personality.data["favorite_places"] = []
        kws2, hers2, his2 = svc._place_keywords_split()
        eq(hers2, [], f"她那边就是空的：{hers2}")
        eq("电影院" in kws2, True, f"画像要顶上：{kws2}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_scene_reuse_injected_into_extract_prompt():
    """`T85` 标签归一化：把她**已经用过的场景词表**喂回提炼提示词，让新标签收敛。

    为什么不能靠事后猜：相似度这条路已被实测否掉（该合 ≥0.709 / 不该合 ≤0.737 **重叠**），
    所以 `广场晒太阳` 与 `广场` 事后没法安全地认成一场戏。**要让它在产生时就统一**——
    提示词里给出已知场景并**明令优先复用**，模型下一轮就会用回原词（零额外调用）。
    """
    from app.models import memory_write as mw

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        seen = {}

        def fake_chat_json(client, model, messages, **kw):
            seen["prompt"] = messages[0]["content"]
            return ({"facts": [{"text": "又晒了会儿太阳", "scene": "广场晒太阳"}]}, None)

        real = mw.chat_json
        mw.chat_json = fake_chat_json
        try:
            # 第一轮：还没有任何已知场景 → 提示词里应该是"（还没有）"
            store.add_turn("到广场了", "在东边树下")
            eq("（还没有）" in seen["prompt"] or "还没有" in seen["prompt"], True,
               f"首轮不该有场景表：{seen['prompt'][:200]}")

            # 第二轮：提示词里必须带上"她用过哪些场景"，并要求优先复用
            store.add_turn("还是热", "再晒会儿")
            p = seen["prompt"]
            eq("广场晒太阳" in p, True, f"已知场景要出现在提示词里：{p[:400]}")
            eq("优先复用" in p or "同一个标签" in p, True,
               f"要明令复用已有场景（否则她又会造变体）：{p[:400]}")
        finally:
            mw.chat_json = real

        # 复用同一标签 → 归到同一组；换个标签 → 另起一组
        groups = [m.get("event_group") or "" for m in store.memories]
        eq(len({g for g in groups if g.startswith("scene:")}), 1,
           f"两轮都用了同一个标签，应该只有一组：{groups}")


def test_scene_label_is_normalized():
    """标签本身也要做**确定性**归一（去空白/后缀/引号），不靠相似度猜。"""
    from app.models.memory_write import _norm_scene

    eq(_norm_scene(" 广场晒太阳 "), "广场晒太阳", "去空白")
    eq(_norm_scene("广场晒太阳（今天）"), "广场晒太阳", "去括号补充——同一场戏别开新组")
    eq(_norm_scene("广场晒太阳。"), "广场晒太阳", "去句末标点")
    eq(_norm_scene("「广场晒太阳」"), "广场晒太阳", "去引号")
    eq(_norm_scene(""), "", "空标签原样返回空")
    eq(_norm_scene("晒太阳"), "晒太阳", "**不许**做词根合并：『晒太阳』与『广场晒太阳』是不同的字面，宁可漏合")


def test_extract_prompt_forbids_the_word_assistant():
    """提炼提示词必须**禁止「助手」进记忆文本**，并告诉她叫什么。

    根因：旧提示词用「用户：/助手：」当对话标签，提炼器照抄 → 库里出现
    「助手不知道9月24日的天气…」「助手在见面时带了小饼干…」——视角标着〔她〕、
    主语却是"助手"，时间轴上看着自相矛盾（用户 2026-09-29 截图指出）。
    """
    from app.models import memory_write as mw

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        store.cfg["her_name"] = "沈知夏"
        seen = {}

        def fake_chat_json(client, model, messages, **kw):
            seen["prompt"] = messages[0]["content"]
            return ({"facts": []}, None)

        real = mw.chat_json
        mw.chat_json = fake_chat_json
        try:
            store.add_turn("你好", "你好呀")
        finally:
            mw.chat_json = real
        p = seen.get("prompt") or ""
        eq("沈知夏" in p, True, f"要告诉她叫什么：{p[:300]}")
        eq("绝对不要出现「助手」" in p, True, f"要有禁止「助手」的硬规则：{p[:400]}")
        eq("「用户：/助手：」" not in p.split("【这一轮对话】")[-1][:80], True,
           "对话标签本身也不该再写「助手」")


def test_timeline_speaks_her_name_and_keeps_data_intact():
    """时间轴显示用她的名字（存量里的「助手」被映射），**但底下的数据一个字不动**。

    为什么不改库：那是历史记录（台账的既定纪律：历史不改写）。这是"怎么念给她听"的问题。
    """
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                             "EMBED_API_KEY", "AMAP_KEY") if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = ""
    svc = None
    try:
        svc = ChatService()
        fake = _FakeLLM()
        svc.store.client = fake
        svc.store.embed_client = fake          # 没有真 key：用假向量，别让它走 401 → pending
        # 用**普通事实**（不是「助手…」开头——那种会被 `#97` 的视角守卫改写成她的经历，
        # 那是另一条路）。这里要测的是：历史文本里的旁白称呼在显示时被映射掉。
        raw = "2026年9月24日助手看不到天气，只看到杭州阴"
        svc.store._add_memories([raw], "fact")
        out = svc.timeline_report()
        eq("助手" in out, False, f"显示层不该再出现「助手」：{out}")
        eq(svc._her_name() in out, True, f"要换成她的名字：{out}")
        eq(any(m["text"] == raw for m in svc.store.memories), True,
           "**底层数据一个字都不许改**（这是显示层的事，历史不改写）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_life_slots_and_advance():
    """她的生活（`#107`）：**按自然时间段推进**，同一格不重复推，失败了也不许"停住"。

    需求原话：「她没有时间观念、没有想法，我只能让她尽可能的像人」——
    这一层补的就是"她自己的现在进行时"：用户不在时她的世界也照走。
    """
    from app.models import life as life_mod

    # 时段划分是纯函数（不依赖真时钟），先把边界钉住
    import datetime as _dt
    for h, want in ((3, "late"), (7, "morning"), (10, "forenoon"), (12, "noon"),
                    (15, "afternoon"), (18, "evening"), (21, "night"), (23, "late")):
        eq(life_mod.slot_of(_dt.datetime(2026, 9, 29, h, 0)), want, f"{h} 点")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"),
                               her_name="沈知夏", identity="住在隔壁的姑娘")
        eq(lb.needs_advance(), True, "还没有状态 → 该推进")

        st = lb.advance(now=_dt.datetime(2026, 9, 29, 10, 0))     # 不给 client → 走兜底
        eq(st.get("slot"), "forenoon", f"推进到上午：{st}")
        eq(bool(st.get("place")), True, f"要有「在哪」：{st}")
        eq(bool(st.get("activity")), True, f"要有「在做什么」：{st}")
        eq(len(lb.recent_events()), 1, f"要写一条生活流水：{lb.recent_events()}")
        eq(abs(lb.recent_events()[0]["ts"] - _dt.datetime(2026, 9, 29, 10, 0).timestamp()) < 1,
           True, "补算的流水时间应是她经历的那个时段")

        # 同一格不许重复推进（这是"每次问她在干嘛都答同一件事"的基础）
        eq(lb.needs_advance(_dt.datetime(2026, 9, 29, 10, 30)), False, "同一时间段不该再推")
        again = lb.advance(now=_dt.datetime(2026, 9, 29, 10, 30))
        eq(again.get("activity"), st.get("activity"), "没推就没变")
        eq(len(lb.recent_events()), 1, "也不该多写一条流水")

        # 跨段 / 跨天要推
        eq(lb.needs_advance(_dt.datetime(2026, 9, 29, 15, 0)), True, "跨到下午要推")
        lb.advance(now=_dt.datetime(2026, 9, 29, 15, 0))
        eq(len(lb.recent_events()), 2, "第二条流水")
        eq(lb.state().get("slot"), "afternoon", "状态更新")

        # 渲染出来的块要能在 prompt 里用；空状态时不许留空气块
        block = lb.render()
        eq("【你此刻】" in block, True, f"要有个块名：{block}")
        eq("在做什么" not in block and "activity" not in block, True,
           f"不许把字段名漏出去：{block}")


def test_life_progress_uses_a_persisted_time_window():
    """下一段先定一个非整点时间；重启后仍按它推进，流水记实际推进时间。"""
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        store = _mk_store(directory, _FakeLLM())
        path = os.path.join(directory, "life.json")
        life = life_mod.LifeBook(store.db, path=path)
        life.advance(now=dt.datetime(2026, 9, 30, 13, 0))
        due = life.state().get("next_advance_at")
        eq(isinstance(due, (int, float)), True, "下一次推进时间要保存下来")
        if not isinstance(due, (int, float)):
            return
        eq(dt.datetime(2026, 9, 30, 14, 10).timestamp() <= due <=
           dt.datetime(2026, 9, 30, 14, 50).timestamp(), True,
           "下一段在合理窗口内推进，不钉死在 14:00")
        eq(life.needs_advance(dt.datetime(2026, 9, 30, 14, 0)), False,
           "刚跨段时仍在做原来的事")
        restarted = life_mod.LifeBook(store.db, path=path)
        eq(restarted.next_slot_at(dt.datetime(2026, 9, 30, 14, 1)), due,
           "重启后沿用之前选好的时间")
        eq(restarted.needs_advance(dt.datetime.fromtimestamp(due - 1)), False,
           "约定时间前不能偷跑")
        restarted._save_state("noon", "2026-09-30",
                              {"place": "公司", "activity": "理报销单",
                               "mood": "", "on_mind": ""},
                              at=dt.datetime(2026, 9, 30, 14, 1).timestamp())
        eq(restarted.state().get("next_advance_at"), due,
           "对话中更新生活状态不能重抽推进时间")
        eq(restarted.missed_slots(dt.datetime(2026, 9, 30, 17, 30)), 2,
           "状态更新时间不能抹掉尚未推进的下午")
        eq(restarted.needs_advance(dt.datetime.fromtimestamp(due)), True,
           "到了约定时间才推进")
        restarted.advance(now=dt.datetime.fromtimestamp(due))
        eq(restarted.recent_events(1)[0]["ts"], due,
           "流水写真实推进时间")

        restarted.advance(now=dt.datetime(2026, 9, 30, 23, 30), force=True)
        eq(restarted.missed_slots(dt.datetime(2026, 10, 1, 0, 35)), 1,
           "跨午夜只算落后一格")
        eq(life_mod._next_slot_time(dt.datetime(2026, 10, 1, 0, 35)),
           dt.datetime(2026, 10, 1, 6, 30),
           "午夜后下一段是当天清晨")


def test_old_life_state_adds_scheduling_columns():
    """已有的生活状态和历史留着，只给旧库补上新的时间字段。"""
    import sqlite3
    from app.models.db import connect

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        path = os.path.join(directory, "memory.db")
        legacy = sqlite3.connect(path)
        legacy.execute("CREATE TABLE life_state (id INTEGER PRIMARY KEY, slot TEXT, "
                       "slot_date TEXT, place TEXT, activity TEXT, mood TEXT, "
                       "on_mind TEXT, updated_at REAL)")
        legacy.execute("INSERT INTO life_state VALUES(1,'noon','2026-09-30',"
                       "'公司','理报销单','','',1000)")
        legacy.execute("CREATE TABLE life_events (id INTEGER PRIMARY KEY, ts REAL, "
                       "slot TEXT, text TEXT, source TEXT)")
        legacy.execute("INSERT INTO life_events VALUES(1,900,'noon','开始理单子','model')")
        legacy.execute("INSERT INTO life_events VALUES(2,1100,'noon','单子理完','conversation')")
        legacy.commit()
        legacy.close()
        migrated = connect(path)
        try:
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(life_state)")}
            eq({"advanced_at", "next_advance_at", "declined_at"} <= columns, True,
               "旧库要自动补齐生活日程和沉默决定的字段")
            eq(migrated.execute("SELECT activity FROM life_state WHERE id=1").fetchone()[0],
               "理报销单", "迁移不能动她已发生的生活")
            eq(migrated.execute("SELECT advanced_at FROM life_state WHERE id=1")
               .fetchone()[0], 900,
               "最近推进点应取生活生成流水，不能被后续对话更新时间覆盖")
        finally:
            migrated.close()


def test_life_is_isolated_from_memory_and_profile():
    """她的生活**必须与记忆隔离**：不进 `memories`、不进画像分析的输入。

    这是设计里最要紧的一条边界：她能自由"长出自己的事"，但那些事**不能污染用户的世界**
    （实测风险：她的发言会进对话历史，而画像分析器是照对话历史提炼的）。
    """
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        import datetime as _dt
        lb.advance(now=_dt.datetime(2026, 9, 29, 21, 0))
        eq(len(store.memories), 0, f"生活流水不许进 memories：{[m['text'] for m in store.memories]}")
        rows = list(store.db.execute("SELECT text FROM life_events"))
        eq(len(rows), 1, "但它自己要落库（下次还能看）")
        # 也不许出现在召回池里
        eq(store.recall("她今天在干什么", top_k=5, mark_hit=False), [],
           "她的生活不参与召回")


def test_life_history_is_available_when_asked():
    import time
    from app.services.chat_service import ChatService

    saved = {key: getattr(config, key) for key in
             ("DATA_DIR", "API_KEY", "CHAT_API_KEY", "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")}
    saved_limit = getattr(config, "LIFE_HISTORY_LIMIT", None)
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    service = None
    try:
        service = ChatService()
        with service.store.db:
            service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (time.time() - 86400, "night", "昨天在书店翻了散文集", "model"))
            service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (time.time() - 3600, "afternoon", "下午：处理手头的活", "dedup"))
        messages, _ = service.prepare("你昨天做了什么？")
        context = " ".join(str(message.get("content", "")) for message in messages)
        eq("昨天在书店翻了散文集" in context, True, "问她自己的过去时要读到持久化的生活流水")
        eq("下午：处理手头的活（她自己选的" in context, True,
           "模型事件的去重改写不应被说成系统兜底")
        config.LIFE_HISTORY_LIMIT = 1
        limited, _ = service.prepare("你昨天做了什么？")
        limited_context = " ".join(str(message.get("content", "")) for message in limited)
        eq("昨天在书店翻了散文集" in limited_context, False,
           "历史注入条数应受配置上限约束")
        current, _ = service.prepare("我今天要带伞吗？")
        current_context = " ".join(str(message.get("content", "")) for message in current)
        eq("昨天在书店翻了散文集" in current_context, False, "无关问题不应注入旧流水")
    finally:
        if service is not None:
            service.close()
        for key, value in saved.items():
            setattr(config, key, value)
        if saved_limit is None:
            delattr(config, "LIFE_HISTORY_LIMIT")
        else:
            config.LIFE_HISTORY_LIMIT = saved_limit


def test_life_catches_up_missed_slots():
    """程序关着的那段时间，她的生活**要补出来**（用户问的那个问题的实现）。

    诉求原话：「如果我关闭一段时间她会感知到吗」。能——`life_state.updated_at` 记着
    她上次推进到什么时候，下次推进时按时段序号算差值，逐格补出来。
    否则关三天也只补一格，那就成了"她这三天只过了一个片段"，不真实。
    """
    from app.models import life as life_mod
    import datetime as _dt

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))

        # 先推到"清晨"，然后假装服务被关了、下午三点才重新打开
        lb.advance(now=_dt.datetime(2026, 9, 29, 7, 30))
        eq(lb.state().get("slot"), "morning", "先在清晨")
        eq(lb.missed_slots(_dt.datetime(2026, 9, 29, 15, 0)), 3,
           "7:30(morning)→15:00(afternoon) 跨了 forenoon/noon/afternoon 三格")

        n_before = len(lb.recent_events())
        out = lb.catch_up(now=_dt.datetime(2026, 9, 29, 15, 0))
        eq(len(out) >= 3, True, f"至少要补出 3 格：补了 {len(out)}")
        eq(len(lb.recent_events()) - n_before >= 3, True,
           f"生活流水要真的多几条（她这段时间真的过了）：{len(lb.recent_events())}")
        eq(lb.state().get("slot"), "afternoon", f"最后对齐到当前时段：{lb.state()}")

        # 关太久不许把流水塞爆（也防调用费失控）
        lb.advance(now=_dt.datetime(2026, 9, 29, 21, 0), force=True)
        far = _dt.datetime(2026, 10, 6, 15, 0)          # 一周后
        eq(lb.missed_slots(far) > life_mod.LifeBook.MAX_CATCH_UP, True, "确实落了很多格")
        n2 = len(lb.recent_events())
        lb.catch_up(now=far)
        added = len(lb.recent_events()) - n2
        eq(added <= life_mod.LifeBook.MAX_CATCH_UP + 1, True,
           f"补算要有上限（实际补了 {added} 格，上限 {life_mod.LifeBook.MAX_CATCH_UP}）")


def test_proactive_gates_block_and_allow():
    """她主动说话（`#108`；闸门于 2026-09-30 按用户口径重做）：**每个闸门都要挡得住**。

    不做闸门，她就变成"每 20 分钟找你一次的推送机器人"。用户给的口径（原话）：
    「**我和她对话的时候不进行这个线程，等没对话的时候进行每分钟抽取**；前面 30 分钟
    概率逐步上升但很低（比如 5%），30 分钟后大大提升变成 80%，到 1 个小时变成百分百；
    **抽中一次之后从头开始**」。所以：
      · **起算点 = 最近一次有人说话**（他说的 / 她上次主动说的）——"抽中一次从头开始"
        是它的自然结果：她一开口，起算点就回到 0；
      · **对话刚结束的几分钟根本不抽**（`quiet_min`，默认 5 分钟）= 用户说的"对话时不进行"；
      · 概率曲线（**每分钟抽一次**）：
        `[quiet_min, 30) → 0.05·since/30` ｜ `[30, 60] → 0.80 + 0.20·(since-30)/30` ｜ `≥60 → 1.0`；
      · 日上限按**开口次数**算，不按气泡条数——`T86` 之后她一次可能发 1~3 条，
        数条数的话"一次三条"就把一天额度吃光（真机实测踩到过）；
      · 旧的四小时硬墙（`min_gap_hours`）**默认关掉**：防连发交给"抽中一次从头开始"。
    覆盖：开关 / 静默时段 / 没新事可说 / 对话下限 / 曲线七点 / 抽中重置 / 每分钟一抽 / 日上限按次。
    """
    import datetime as _dt
    import random
    from app.models import life as life_mod
    from app.services.speak import Speaker

    def _at(ts):
        return _dt.datetime.fromtimestamp(ts)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"),
                               her_name="沈知夏")
        sp = Speaker(store.db, lb)
        now = 1790000000.0          # 固定时间，避免"跑测试时正好是深夜"

        def _set(**cols):
            with store.db:
                sets = ", ".join(f"{k}=?" for k in cols)
                store.db.execute(f"UPDATE life_state SET {sets} WHERE id=1", list(cols.values()))

        base = {"enabled": True, "daily_max": 3, "min_gap_hours": 0,
                "quiet_min": 5, "low_min": 30, "low_chance": 0.05,
                "high_min": 60, "high_chance": 0.80, "chance_ceil": 1.0,
                "quiet_hours": (0, 0), "roll_every_min": 0}

        def _curve(since_min, cfg=None):
            """把"最近一次有人说话"放到 since_min 分钟前，再问一次闸门。"""
            _set(her_last_ts=None, roll_ts=None, user_last_ts=now - since_min * 60)
            return sp.should_speak(now, cfg or base)

        eq(sp.should_speak(now, dict(base, enabled=False))[0], False, "关掉就不说")
        eq(sp.should_speak(now, dict(base, quiet_hours=(0, 24)))[0], False, "静默时段不说")
        eq(sp.should_speak(now, base)[0], True,
           "即使没有新流水，也可以考虑关心他或说一个自己的念头")

        # 造一点生活，后面才有"新东西可说"
        lb.advance(now=_at(now), force=True)

        orig = random.random
        random.random = lambda: 0.999      # 只读概率曲线，别真抽中
        try:
            # ④ 曲线：对话刚结束不抽 → 低概率慢爬 → 30 分钟跳到 80% → 60 分钟 100%
            ok, why, det = _curve(2)
            eq(ok, False, "刚聊完不抽")
            eq(det["chance"], 0.0, f"这一段根本不掷骰：{det}")
            eq("对话" in why, True, f"原因要说清是「对话中，不抽」：{why}")

            eq(_curve(15)[2]["chance"], round(0.05 * 15 / 30, 3), "15 分钟 ≈ 2.5%（很低）")
            eq(_curve(29)[2]["chance"], round(0.05 * 29 / 30, 3), "29 分钟仍很低（逼近 5%）")
            eq(_curve(30)[2]["chance"], 0.80, "跨过 30 分钟**直接跳到 80%**")
            eq(_curve(45)[2]["chance"], 0.90, "45 分钟 ≈ 90%")
            eq(_curve(60)[2]["chance"], 1.0, "1 小时 = 100%")
            eq(_curve(240)[2]["chance"], 1.0, "再久也不会超过 100%")

            # 抽中一次之后**从头开始**：她刚开口过 → 起算点回到 0
            _set(her_last_ts=now - 60, roll_ts=None, user_last_ts=now - 4 * 3600)
            _, why, det = sp.should_speak(now, base)
            eq(det["chance"], 0.0,
               f"她刚说过 → 回到起点（不再按「他 4 小时没说话」算）：{det}")

            # ⑥ 每分钟抽一次：同一个 1 分钟窗里只抽一次
            window = dict(base, daily_max=99, roll_every_min=1)
            _set(her_last_ts=None, roll_ts=None, user_last_ts=now - 2 * 3600)
            ok1, _, _ = sp.should_speak(now, window)
            eq(ok1, True, "空闲 2 小时 → p=1，抽中")
            ok2, why2, _ = sp.should_speak(now + 30, window)
            eq(ok2, False, f"同一个 1 分钟窗里再问 → 不再抽：{why2}")
            eq(("窗" in why2) or ("掷" in why2), True, f"原因要说清是时间窗挡的：{why2}")
            ok3, _, _ = sp.should_speak(now + 61, window)
            eq(ok3, True, "过了窗口 → 可以再抽一次")

            # ② 日上限按**次**不按**条**：一次开口发 3 条只算一次
            with store.db:
                store.db.execute("DELETE FROM pending_say")
                store.db.execute("DELETE FROM events WHERE kind='proactive'")
                for _ in range(3):
                    store.db.execute("INSERT INTO pending_say(ts,text,her_name) VALUES(?,?,?)",
                                     (now, "同一轮的一条", "沈知夏"))
                store.db.execute("INSERT INTO events(ts,kind,session,payload) VALUES(?,?,?,?)",
                                 (now, "proactive", "default", '{"text":"同一轮三条","n":3}'))
            eq(_curve(3600)[2]["said"], 1,
               "一次开口发 3 条**只算一次**（数气泡数的话一天只能说一次）")
            with store.db:
                for _ in range(2):
                    store.db.execute("INSERT INTO events(ts,kind,session,payload) VALUES(?,?,?,?)",
                                     (now, "proactive", "default", '{}'))
            _set(her_last_ts=None, roll_ts=None, user_last_ts=now - 3600 * 60)
            ok, why, _ = sp.should_speak(now, base)
            eq(ok, False, "今天已经开口 3 次 → 不再说")
            eq("3" in why, True, f"日上限的原因要带数字：{why}")
        finally:
            random.random = orig


def test_proactive_can_choose_silence_without_new_life():
    """一小时后必定获得判断机会；她可以沉默，沉默后有间隔，新线索能提前唤起。"""
    from unittest.mock import patch
    from app.models.life import LifeBook
    from app.services.speak import Speaker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        store = _mk_store(directory, _FakeLLM())
        life = LifeBook(store.db, path=os.path.join(directory, "life.json"))
        speaker = Speaker(store.db, life)
        now = dt.datetime(2026, 9, 30, 16, 0).timestamp()
        speaker._set_ts("user_last_ts", now - 2 * 3600)
        cfg = {"enabled": True, "daily_max": 6, "quiet_hours": (0, 0),
               "quiet_min": 5, "roll_every_min": 0, "decision_gap_min": 30}
        with patch("random.random", return_value=0):
            allowed, _, detail = speaker.should_speak(now, cfg)
            eq(allowed, True, "没有新生活流水，仍有一次自主判断机会")
            eq(detail["chance"], 1.0, "一小时后必定检查她的意愿")
            eq(speaker.speak(_ok_client('{"speak": false, "text": ""}'),
                             "m", now=now), [], "她明确选择不说就不发消息")
            eq(speaker._ts("declined_at"), now, "沉默决定要持久保存")
            eq(speaker.should_speak(now + 60, cfg)[2]["gate"], "decision_wait",
               "沉默后不能每分钟再问模型一次")
            eq(speaker.should_speak(now + 31 * 60, cfg)[0], True,
               "间隔过后仍可重新判断")
            speaker._set_ts("user_last_ts", now - 3600)
            speaker._set_ts("declined_at", now - 15 * 60)
            eq(speaker.should_speak(now, cfg)[0], True,
               "满一小时要检查一次，即使此前刚决定暂不说")
            life._add_event("afternoon", "临时想去菜场看看", "model", now + 120)
            eq(speaker.should_speak(now + 180, cfg)[0], True,
               "新的生活进展可以提前唤起一次判断")


def test_proactive_tick_records_her_silence():
    """她选择暂不说时留下可查原因，也不占开口次数。"""
    import time
    from app.services.chat_service import ChatService

    saved = {key: getattr(config, key) for key in
             ("DATA_DIR", "API_KEY", "CHAT_API_KEY", "EMBED_API_KEY",
              "AMAP_KEY", "AMAP_CITY", "PROACTIVE_QUIET")}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    config.PROACTIVE_QUIET = (0, 0)
    service = None
    try:
        service = ChatService()
        service.life.advance(force=True)
        service.life._add_event("afternoon", "看完了那本小说，想跟他聊聊结尾", "model", time.time())
        service.speaker._set_ts("user_last_ts", time.time() - 2 * 3600)
        service.client = _ok_client('{"speak": false, "text": ""}')
        eq(service.proactive_tick(), "", "她选择暂不说时没有待发内容")
        eq(service.speaker.said_today(), 0, "沉默不占每日开口次数")
        eq(service.speaker.pending_items(), [], "沉默不生成气泡")
        recent = service.store.events_recent(10)
        eq(any(event.get("kind") == "proactive_skip" and
               event.get("gate") == "chose_silence" for event in recent), True,
           "看板事件能说明是她自己选择暂不说")
    finally:
        if service is not None:
            service.close()
        for key, value in saved.items():
            setattr(config, key, value)


def test_proactive_skip_is_visible_in_events():
    """可见性（用户点名）：她**没说**的时候，要能查出来是哪道闸门挡的。

    起因是用户 2026-09-30 的实测提问：「我 09:39 说过话，现在 12 点了，为什么没有
    自言自语」。当时跳过只写 `log.debug`，而默认级别是 INFO → **日志里查不到**，
    只能靠翻 `life_state` 的时间戳反推。现在：闸门**变化时**打一条 INFO +
    埋一条 `proactive_skip` 事件（带 gate / since_min / said / cap / chance）。

    ⚠ **去重键必须是 `gate`（不带数字），不能是原因文本**——这条是加了可见性之后
    从真实日志里抓出来的：原因里带着"已过 0.16 → 0.17 小时"，拿文本去重等于**每条都算变了**，
    实测每 30 秒的轮询各写一条（13:29:28 / 13:29:53 / 13:30:23 / 13:31:13…），
    半天就能把 `events`（上限 5000 条）连同别的机制埋点一起冲掉。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        box = {"n": 0, "cls": "waiting", "gate": "dice"}

        def fake_should(cfg=None, now=None):
            box["n"] += 1
            # 与真机一样：`dice`（这轮没抽中）与 `roll_window`（同窗不重复抽）**交替**出现，
            # 而原因文本里还带着会变的数字——去重键必须是**粗分类** `gate_class`。
            box["gate"] = "dice" if box["n"] % 2 else "roll_window"
            return False, f"这次没抽中（第 {box['n']} 次）", {
                "gate": box["gate"], "gate_class": box["cls"], "said": 2, "cap": 3,
                "since_min": box["n"] * 0.5, "chance": 0.02}

        svc.speaker.should_speak = fake_should
        base_n = svc.store.events_stats().get("proactive_skip", 0)

        eq(svc.proactive_tick(), "", "被闸门挡下 → 什么都不发")
        eq(svc.proactive_tick(), "", "同一个粗分类，但 gate 与数字都变了")
        svc.proactive_tick()
        n1 = svc.store.events_stats().get("proactive_skip", 0)
        eq(n1 - base_n, 1,
           f"**同一个粗分类只记一条**（真机里这两个闸门每 30 秒交替、一天能写 2880 条）："
           f"记了 {n1 - base_n} 条")

        box["cls"] = "daily_cap"           # 换了一类闸门 → 应当再记一条
        svc.proactive_tick()
        n2 = svc.store.events_stats().get("proactive_skip", 0)
        eq(n2 - n1, 1, "换了一类闸门 → 再记一条（于是能看到闸门是怎么依次收紧的）")

        recent = [e for e in svc.store.events_recent(10) if e.get("kind") == "proactive_skip"]
        eq(any(e.get("gate_class") == "waiting" for e in recent), True,
           f"事件要带**粗分类**（去重靠它）：{recent}")
        eq(any("cap" in e and "since_min" in e for e in recent), True,
           f"还要带上可数的数字：{recent}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_proactive_tick_advances_her_life():
    """**你不说话时她也要过日子**（2026-09-30 修的死锁）。

    `_has_fresh_life()` 要求"上次开口之后生活里有新东西"，而生活原本**只在 `prepare()`
    （你说话时）推进** → 你越不理她，她越没话可说，于是"自说自话"在你长时间不出现时
    **永远触发不了**（与 `life.py` 写的"用户三天不理她，她的生活照走"矛盾）。
    真实数据推演当场撞到：她 19 分钟前刚开口，闸门停在 `no_fresh_life`。

    这条守的是 `proactive_tick()` 里那次推进调用——把它删了，本用例会红。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        # 闸门一律挡下——但推进生活**发生在闸门之前**，所以照样得推
        svc.speaker.should_speak = lambda cfg=None, now=None: (
            False, "探针：一律挡下", {"gate": "probe"})
        with svc.store.db:
            svc.store.db.execute(
                "UPDATE life_state SET slot_date=?, updated_at=updated_at-3*3600 WHERE id=1",
                ("2020-01-01",))
        before = len(svc.life.recent_events(50))
        svc.proactive_tick()
        eq(len(svc.life.recent_events(50)) > before, True,
           f"主动说话这条路也要把她的生活推一格（{before} → "
           f"{len(svc.life.recent_events(50))}）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_life_board_shows_provenance():
    """`#117` 看板：她的生活要**看得见、也查得到出处**。

    起因是用户 2026-09-30 的两句话：「**我需要一个新的看板，看她做了的事情**」＋
    「**我不知道薄荷是哪里来的**」。数据里的真相：编剧层（`life.json`）只写
    「阳台上那几盆花」，而「薄荷」那句话**没有落进任何一张表**——
    所以看板的价值全在"每条标出处"，这条用例钉的就是那些字段还在：
    流水要带 `source`（她自己选的／兜底候选）、`slot`（哪一格）与时间；
    编剧层照抄原文（**这就是"里面没有薄荷"的证据**）；她说过的话要有投递状态。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.life.advance(force=True)          # 不传 client → 走兜底候选（source=fallback）
        svc.speaker.enqueue("刚忙完，歇一会儿")
        board = svc.life_board()

        eq(set(board) >= {"now", "state", "script", "favorite_places", "cares",
                          "events", "reports", "experiences", "stats"}, True,
           f"看板该给的几块都在（`#145` 起 `said` 改名为 `reports`）：{sorted(board)}")
        eq(len(board["events"]) >= 1, True, f"至少有一条生活流水：{board['events']}")
        eq(set(board["events"][0]) >= {"when", "slot", "slot_label", "text",
                                       "source", "source_label"}, True,
           f"流水每条都要带**出处**（时间／哪一格／谁写的）：{board['events'][0]}")
        eq(board["events"][0]["slot_label"] != "", True, "时段的显示名要有")
        eq(board["events"][0]["source_label"] in ("她自己选的", "兜底候选"), True,
           f"出处要说人话：{board['events'][0]['source_label']}")
        eq(board["reports"][0]["text"], "刚忙完，歇一会儿", "她亲口报备的话要能看到")
        eq(board["reports"][0]["status"] in ("已读到", "待投递"), True, "还要能看出投递状态")
        # `#145`：看板必须**以她报备过的为准**，"此刻"那份是系统推演、只当氛围。
        # 今天两次"两处对不上"（汤炖好了 vs 回家炖汤中）都是因为界面上两者并列、看不出主次。
        eq(board["state"].get("advisory"), True, "「此刻」要标明是推演出来的")
        eq("氛围" in str(board["state"].get("advisory_label")), True,
           f"要明说只当氛围、以她说的为准：{board['state'].get('advisory_label')}")
        eq("报备" in str(board["state"].get("advisory_label")), True,
           "并且要点出'以她最近报备的为准'")
        eq("打球" in board["script"]["tastes"], True,
           f"编剧层要照抄 life.json——换人后新她的偏好就是这条：{board['script']}")
        eq(board["stats"]["next_slot_when"] != "", True, "要说清下一格大约什么时候")
        eq(board["stats"]["daily_max"], int(config.PROACTIVE_DAILY_MAX), "日上限也要报出来")
        # **只读**：看一眼看板不许把待投递的话标记成已读，也不算"开口过"
        eq(svc.speaker.said_today(), 0, "只是看了一眼看板，不算开口过")
        eq(len(svc.speaker.said_recent()) >= 1, True, "看一眼不会消费掉它")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_life_api_route_is_registered_and_shaped():
    """`GET /api/life`（看板的数据源）：路由在、结构固定、**不会 500**。

    与 `/api/pending` 那条同款：保留 lifespan（要验的是"装好之后的 app"），
    但把天气刷新打成 no-op——**一次网络调用都不发生**（`T88` 的纪律）。
    """
    import tempfile as _tf

    from app.utils import weather as w
    saved = config.DATA_DIR
    real_refresh = w.WeatherCache.refresh
    config.DATA_DIR = _tf.mkdtemp()
    w.WeatherCache.refresh = lambda self: None
    try:
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as client:
            resp = client.get("/api/life")
            eq(resp.status_code, 200, f"接口要能通：{resp.status_code} {resp.text[:200]}")
            body = resp.json()
            eq(isinstance(body.get("state"), dict), True, f"要有此刻状态：{body}")
            eq(isinstance(body.get("events"), list), True, "要有生活流水")
            eq(isinstance(body.get("script"), dict), True, "要有编剧层")
            eq(isinstance(body.get("stats"), dict), True, "要有统计")
    finally:
        w.WeatherCache.refresh = real_refresh
        config.DATA_DIR = saved


def test_her_own_home_is_not_his_to_answer():
    """她自己家里的事**别反问他**（用户 2026-09-30 的原话：「她应该知道她家里的情况，
    而不是去问我年糕是不是跑她家里了」）。

    为什么必须写成"**人在家 / 不在家**"两段：她的状态里她**常常在上班**
    （`place=公司工位`）——一条不管人在哪都写"你自己看得见"的规则**事实就不对**，
    模型自然不照做（实测 13:48 她仍在问"年糕是不是又跑我家蹭太阳了"）。
    正确口径：人在家 → 家里的事自己知道，直接说；不在家 → 确实不知道，
    **如实说不知道**，但**不要拿它问他**（他不在你家，答不上来）。
    """
    from app.models import life as life_mod
    from app.services.speak import SPEAK_PROMPT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(force=True)
        text = lb.render()
        eq("别反问他" in text, True, f"【你此刻】要有这条纪律：{text[-150:]}")
        eq("不在家" in text, True, "必须区分人在不在家（她常常在上班）")
        eq("如实说不知道" in text, True, "不在家时该说不知道，而不是把问题抛回去")
        eq("你自己干的事" in text, True, "她自己做过的事也不许反过来问他（#143）")
        eq("别反问他" in SPEAK_PROMPT, True, "主动说话那份提示词也要有同样的纪律")
        eq("不在家" in SPEAK_PROMPT, True, "那份也要区分人在不在家")
        eq("你自己干的事" in SPEAK_PROMPT, True, "主动那条路也要管住'自己的事别问他'")
        from app.services.chat_service import GREETING_PROMPT
        eq("你自己干的事" in GREETING_PROMPT, True,
           "开场白那份也要有——真机就是它在问'这么晚才煮面吃，蛋壳挑出来没'（#143）")
        eq("是你自己干的" in GREETING_PROMPT, True, "流水那一块要写明归属")


def test_nonstream_reply_is_cleaned_like_stream():
    """`T83` 的输出侧清洗必须**两条路都有**：流式剥在 `stream()`，非流式那条**原来没有**。

    真机实测抓到的（`T86` 的分条探针里，非流式 8 条有 2 条这样开头）：
    `（今天下午 13:54）不在呀，我还在公司呢…` ——那是给她的内部时间标记，
    不该说给用户听。流式那条早在 `#102` 就剥了，非流式的 `chat()` 漏了；
    而 `T86` 之后**它会直接出现在气泡里**。
    `answers` 模块的两条纪律照旧：**只剥开头**、**必须留痕**。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.prepare = lambda q, **kw: ([{"role": "system", "content": "（系统提示）"}], [])
        svc.ask = lambda messages: "（今天下午 13:54）在呢，我在阳台给花浇水\n@@@\n你那边呢？"
        svc.finish_turn = lambda q, a: {"memory_note": "", "profile_note": ""}
        out = svc.chat("在吗")

        eq("（今天下午" not in out["reply"], True,
           f"开头的内部时间标记要剥掉（只剥开头）：{out['reply']!r}")
        eq(out["messages"], ["在呢，我在阳台给花浇水", "你那边呢？"], "剥完再分条")
        eq(any(e.get("kind") == "answer_cleaned" for e in svc.store.events_recent(10)), True,
           f"剥了必须留痕（T83 的第三条约束）：{svc.store.events_recent(3)}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_static_assets_must_revalidate():
    """`/static/*` 必须带 `Cache-Control: no-cache`——否则浏览器会拿旧 JS/CSS 且**不校验**。

    踩过（2026-09-30）：新加的「她的生活」页一直停在 HTML 里的占位字"正在读取…"，
    因为 `/` 是 `no-store` 而 `/static/*` 原来**一个 `Cache-Control` 都没有**
    （只有 ETag/Last-Modified）→ 浏览器按启发式缓存直接用旧 `app.js`，
    表现就是**新 HTML + 旧 JS**：新导航项在、点进去什么都不加载。
    README 写的"改完前端刷新浏览器"，在那之前其实是靠运气。
    """
    import tempfile as _tf

    from app.utils import weather as w
    saved = config.DATA_DIR
    real_refresh = w.WeatherCache.refresh
    config.DATA_DIR = _tf.mkdtemp()
    w.WeatherCache.refresh = lambda self: None
    try:
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as client:
            for path in ("/static/js/app.js", "/static/assets/styles.css"):
                resp = client.get(path)
                eq(resp.status_code, 200, f"{path} 要能取到")
                cc = resp.headers.get("cache-control", "")
                eq("no-cache" in cc, True, f"{path} 必须要求回源校验（现在是 {cc!r}）")
            home = client.get("/")
            eq("no-store" in home.headers.get("cache-control", ""), True,
               "首页仍然是 no-store（它是边做边改的）")
    finally:
        w.WeatherCache.refresh = real_refresh
        config.DATA_DIR = saved


def test_life_advance_does_not_repeat_the_same_line():
    """她的生活流水**不许和最近几条逐字重复**（`#119`，真机抓到的）。

    模型在推进下一格时会把【她最近的生活】里的 `event` 原样抄回来——实测
    09:39 / 13:42 / 14:00 三条**一字不差**（"月初又要填一堆表，先从报销单啃起"），
    于是"她的一天"看起来根本没在走（用户的疑问就是"第一个怎么一直是一样的"）。
    提示词里写了别重复，但**光靠提示词不够**（`D4`：涉及"必须不重样"的东西要在模型
    之外补机器兜底）——兜底写成 `时段：在做什么`，时段标签天然不同、构造上就不会重复。
    """
    import json as _json

    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(force=True)                      # 第一格（模型不可用 → 兜底）
        first = lb.recent_events(1)[0]["text"]
        # 让"模型"这一次**把上一格的 event 原样抄回来**
        client = _ok_client(_json.dumps(
            {"place": "公司工位", "activity": "对报销单对到眼花", "mood": "有点烦",
             "on_mind": "薄荷还没浇", "event": first}, ensure_ascii=False))
        lb.advance(client=client, model="m", force=True)

        rows = lb.recent_events(2)
        eq(rows[0]["text"] != rows[1]["text"], True,
           f"新写的流水不许和上一条重复：{[r['text'] for r in rows]}")
        eq("：" in rows[0]["text"], True, f"兜底写法是「时段：在做什么」：{rows[0]['text']}")
        eq(rows[0]["source"], "dedup", f"并且要标出来源（看板会说清）：{rows[0]}")


def test_life_chooses_an_alternative_candidate_for_repeated_activity():
    """候选列表里有不重复的选项时，不能照选重复的活动。"""
    import datetime as _dt
    import json as _json

    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        now = _dt.datetime(2026, 9, 30, 14, 0)
        lb.advance(now=now, force=True)
        from app.models.life import FALLBACK
        prev_activity = FALLBACK["afternoon"][1]      # 上一格（无模型 → 兜底）的活动
        client = _ok_client(_json.dumps({
            "candidates": [
                # 首选候选**故意与上一格相同**，看代码会不会换成第二个。
                # （读 `FALLBACK` 而不是写死字符串：兜底表换了也不用跟着改用例。）
                {"place": "学校", "activity": prev_activity,
                 "mood": "有点烦", "on_mind": "", "event": "又打了一场"},
                {"place": "学校图书馆", "activity": "翻一会儿书",
                 "mood": "松快", "on_mind": "", "event": "没课，去图书馆翻了会儿书"},
                {"place": "后街", "activity": "买点吃的",
                 "mood": "有精神", "on_mind": "", "event": "去后街买了份烧烤"},
            ],
            "choice": 0,
        }, ensure_ascii=False))
        state = lb.advance(client=client, model="m", now=now, force=True)
        eq(state["activity"], "翻一会儿书",
           "模型首选活动与上一格相同，应从候选里选不重复的活动")
        eq(lb.recent_events(1)[0]["source"], "model", "候选替换仍属于她自己选的")


def test_life_user_update_clears_completed_care():
    """用户确认已经浇水后，生活状态不能继续挂着同一件未完成的事。"""
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(now=dt.datetime(2026, 9, 30, 15, 0))
        with store.db:
            store.db.execute(
                "UPDATE life_state SET on_mind=? WHERE id=1",
                ("阳台上的花该浇水了",))

        before = len(lb.recent_events())
        changed = lb.observe_user_update("已经浇了，你放心工作吧")

        eq(changed, True, "明确完成了牵挂中的浇水后应更新生活状态")
        eq(lb.state().get("on_mind"), "", "浇过水后不应继续显示待浇水")
        eq(len(lb.recent_events()), before + 1, "浇水完成后应追加一条可回忆的生活经历")
        eq(lb.recent_events(1)[0]["text"], "阳台上的花浇过水了",
           "新流水应写成已发生的事，不再保留待办措辞")
        eq(lb.recent_events(1)[0]["source"], "conversation", "完成事实应标明来自对话")
        eq(lb.state()["updated_at"] > dt.datetime(2026, 9, 30, 15, 0).timestamp(),
           True, "看板更新时间应是实际确认的时间")
        eq(lb.observe_user_update("已经浇了，你放心工作吧"), False, "重复确认不应反复写流水")


def test_life_assistant_update_marks_completed_activity():
    """她说报销单已经理完后，当前活动不能仍描述为正在对账。"""
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(now=dt.datetime(2026, 9, 30, 15, 0))
        with store.db:
            store.db.execute(
                "UPDATE life_state SET activity=?, on_mind=? WHERE id=1",
                ("对着一张报销单的发票编号对不上，翻来覆去查", "阳台上的花该浇水了"))

        before = len(lb.recent_events())
        changed = lb.observe_assistant_update("单子理完啦，这就回来请你喝东西")
        state = lb.state()

        eq(changed, True, "助手明确说已经理完后应更新生活状态")
        eq("理完" in state.get("activity", ""), True,
           f"当前活动应体现已完成，而不是继续处理中：{state}")
        eq("对不上" in state.get("activity", ""), False,
           f"已完成状态不应继续保留对不上这一矛盾描述：{state}")
        eq(len(lb.recent_events()), before + 1, "单子理完应追加一条生活流水")
        eq(lb.recent_events(1)[0]["source"], "conversation", "她说过的完成事实应带出处")


def test_chat_turn_observes_life_updates_at_both_boundaries():
    """真实对话边界要消费用户确认，也要消费她自己的完成陈述。"""
    from app.services.chat_service import ChatService

    saved = {key: getattr(config, key) for key in
             ("DATA_DIR", "API_KEY", "CHAT_API_KEY", "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    service = None
    try:
        service = ChatService()
        service.life.advance(force=True)
        with service.store.db:
            service.store.db.execute(
                "UPDATE life_state SET on_mind=?, activity=? WHERE id=1",
                ("阳台上的花该浇水了", "对着一张报销单的发票编号对不上，翻来覆去查"))

        service.prepare("已经浇了，你放心工作吧")
        eq(service.life.state().get("on_mind"), "",
           "prepare 应消费用户已完成浇水的事实")

        service.finish_turn("已经浇了，你放心工作吧", "报销单我已经理完了")
        state = service.life.state()
        eq("理完" in state.get("activity", ""), True,
           f"finish_turn 应消费她已完成报销单的陈述：{state}")
    finally:
        if service is not None:
            service.close()
        for key, value in saved.items():
            setattr(config, key, value)


def test_life_assistant_contradiction_does_not_mark_activity_complete():
    """完成词与未完成事实同现时，保守保留原状态，避免看板自相矛盾。"""
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(now=dt.datetime(2026, 9, 30, 15, 0))
        with store.db:
            store.db.execute(
                "UPDATE life_state SET activity=? WHERE id=1",
                ("对着一张报销单的发票编号对不上，翻来覆去查",))
        old = lb.state()["activity"]

        changed = lb.observe_assistant_update("报销单我已经理完了，不过还有一张发票没对上")

        eq(changed, False, "同一回答自相矛盾时不要擅自认定完成")
        eq(lb.state().get("activity"), old, "矛盾回答应保留待核实的原状态")


def test_life_completion_does_not_take_unrelated_work_or_user_plants():
    """完成词必须指向她的当前事项，不能凭“浇了/做完了”误更新。"""
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        lb.advance(now=dt.datetime(2026, 9, 30, 15, 0))
        with store.db:
            store.db.execute(
                "UPDATE life_state SET activity=?, on_mind=? WHERE id=1",
                ("对着一张报销单的发票编号对不上，翻来覆去查", "阳台上的花该浇水了"))
        before = len(lb.recent_events())

        eq(lb.observe_user_update("我给我家的花浇好了"), False,
           "用户自己家的花与她阳台上的花不是同一件事")
        eq(lb.observe_user_update("还没浇，你等我一会儿"), False,
           "未完成语义不能清掉牵挂")
        eq(lb.observe_assistant_update("我的作业做完啦"), False,
           "无关任务完成不能结束正在处理的报销单")
        eq(len(lb.recent_events()), before, "无关或未完成陈述不能新增生活流水")


def test_pending_delivery_needs_ack():
    """`#119`：她说的话**至少投一次**——渲染成功才确认，没确认的下轮还给。

    起因（真机反馈）：她主动生成的消息在前台**已经被取走**，但页面上没有出现——
    **取走 ≠ 看到**。原协议（`GET /api/pending` 顺手标记已读）下，渲染失败/页面刚好
    刷新/响应丢了，那条话就永远消失。现在：`GET` 只读、`POST /api/pending/ack` 才标记。
    """
    import tempfile as _tf

    from app.services import registry
    from app.utils import weather as w
    saved = config.DATA_DIR
    real_refresh = w.WeatherCache.refresh
    config.DATA_DIR = _tf.mkdtemp()
    w.WeatherCache.refresh = lambda self: None
    try:
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as client:
            svc = registry.get("")
            # 闸门一律挡下：否则 `GET /api/pending` 会顺手"她开口"，把用例搞得不稳定
            svc.speaker.should_speak = lambda cfg=None, now=None: (
                False, "用例：一律挡下", {"gate": "probe", "gate_class": "probe"})
            svc.speaker.enqueue("在呢，刚忙完")

            first = client.get("/api/pending").json()["items"]
            eq(len(first), 1, f"第一次能取到：{first}")
            second = client.get("/api/pending").json()["items"]
            eq([i["id"] for i in second], [i["id"] for i in first],
               "**没确认之前还会再给**（至少一次投递，前端按 id 去重）")
            ids = [i["id"] for i in first]
            acked = client.post("/api/pending/ack", json={"ids": ids}).json()["acked"]
            eq(acked, len(ids), f"确认成功：{acked}")
            third = client.get("/api/pending").json()["items"]
            eq([i["id"] for i in third], [], "确认之后就不再投了")
    finally:
        w.WeatherCache.refresh = real_refresh
        config.DATA_DIR = saved


def test_she_must_not_contradict_what_she_just_said():
    """她自己刚说过的话**要算数**（真机反馈：回复前后不一致）。

    现场抓到的**客观缺陷是自相矛盾**：她先答应把东西送过去，隔一条又让对方别过来——
    **隔 30 秒翻案**。
    查下来现有的"一致性"机制全是**层与层之间**的：人格演化前后检查（`T47`）、
    启动时世界书/记忆/约束三层对账——**没有一句要求她跟自己刚说过的话保持一致**；
    「对话顺序」块只把原文喂回去，从没说过"要算数"。
    所以规则贴在**她自己那几行原文旁边**（同"不要念时间"那条的位置），
    写法照 `#102` 的教训：**硬禁令 + 具体反例 + 正例**（抽象原则真机实测压不住）。
    """
    from app.services.speak import SPEAK_PROMPT

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        msgs, _ = store.build_messages("（系统提示）", "好呀好呀")
        block = next((m["content"] for m in msgs if "【对话顺序】" in m["content"]), "")
        eq(block != "", True, "要有【对话顺序】块")
        eq("算数" in block, True, "要明说「你自己刚说过的话要算数」")
        eq("端过去" in block and "过来" in block, True,
           f"要给出**具体反例**（抽象原则压不住）：{block[-180:]}")
        eq("改主意" in block, True, "还要给出正确的改口方式，否则她会不敢改口")
        eq("算数" in SPEAK_PROMPT, True,
           "主动开口那条路也要有——她完全可能在下一条主动消息里翻案")


def test_empty_model_reply_is_retried_and_never_sent_as_blank():
    """模型偶发空回复：**重试一次**，还空就明确失败，绝不把空串当回复（`T93`）。

    真机撞到过（`#131` 勘察：极简提示词那组 6 次调用里 1 次返回空字符串）。
    非流式的 `chat()` 原来会把空串一路送进 `reply`/`messages`，再交给 `finish_turn`
    写进记忆——**用户收到一条空白消息，库里还多一条空轮次**。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.prepare = lambda q, **kw: ([{"role": "system", "content": "（系统提示）"}], [])
        svc.finish_turn = lambda q, a: {"memory_note": "", "profile_note": ""}
        calls = []

        def first_empty_then_ok(messages):
            calls.append(1)
            return "" if len(calls) == 1 else "在呢，刚忙完"

        svc.ask = first_empty_then_ok
        out = svc.chat("在吗")
        eq(len(calls), 2, "空回复要**重试一次**")
        eq(out["reply"], "在呢，刚忙完", "重试成功后用第二次的内容")

        calls.clear()
        svc.ask = lambda messages: (calls.append(1), "")[1]
        svc.finish_turn = lambda q, a: (_ for _ in ()).throw(
            AssertionError("空轮次不许写进记忆"))
        name = ""
        try:
            svc.chat("在吗")
        except Exception as exc:
            name = type(exc).__name__
        eq(name, "EmptyReply", f"两次都空要抛 EmptyReply（实际 {name!r}）")
        eq(len(calls), 2, "两次都空 → 一共只问两次（重试过一次）")
        eq(any(e.get("kind") == "answer_empty" for e in svc.store.events_recent(10)), True,
           f"失败要留痕（`D10`）：{svc.store.events_recent(3)}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_opinion_distiller_never_invents_what_the_user_did_not_say():
    """「说话偏好」整理器**不许发明用户没说过的东西**，也不许把非说话方式的不满硬塞进来（`#135`）。

    现场（真机反馈：回复和之前的画像有差距）：
    旧模板**强制**"点名失败动作 + 带篇幅约束"，于是产出了用户从未提过的语气要求；
    它还跟同日另一条偏好方向相反，而两条**每轮都注入**（真机量到篇幅明显过长）。
    """
    from app.models.memory_defs import OPINION_PROMPT as P

    eq("矛盾" in P, True, "要点名「前后矛盾」这类非说话方式的反馈")
    eq("分寸" in P, True, "要点名「关系分寸」这类")
    eq("没提" in P, True, "篇幅约束**只在用户提到时才加**（不能像旧版那样强制）")
    eq("原话" in P, True, "要求与用户原话对得上，不许自己发明「失败动作」")
    eq("kind" in P, True, "要有 style / fact / other 三分类，other 才是「什么都不记」的出口")


def test_non_style_feedback_is_not_forced_into_a_preference():
    """非说话方式、也非记错事实的反馈（内容不对／前后矛盾／分寸）→ **什么都不记**（`#135`）。

    为什么必须有这条出口：`is_style_issue=false` 在旧代码里等于"送进记错事实的修正通道"，
    而"她前后矛盾"根本不是"记错了事实"——两条路都不对，于是它只能被硬整理成说话偏好
    （就是 `#135` 要修的病）。这条保证写在**代码**里，不只写在提示词里（`D4`）。
    """
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        payload = {"topic": "前后矛盾", "text": "别自相矛盾", "confidence": 0.9,
                   "is_style_issue": False, "kind": "other"}
        with patch("app.models.memory_opinions.chat_json", return_value=(payload, None)):
            out = store.learn_opinion("好呀好呀", "行，那我先撤了，排骨还得焯水",
                                      note="她说的和上一条对不上")
        eq(out["stored"], False, f"不该存成说话倾向：{out}")
        eq([o["topic"] for o in store.opinions(active_only=False)], [],
           "opinions 里不该多出这一条")
        eq(len(store.pending_corrections()) if hasattr(store, "pending_corrections") else 0, 0,
           "也不该被当成「记错事实」去登记修正")
        eq(any(e.get("kind") == "opinion_skipped" and e.get("reason") == "other"
               for e in store.events_recent(5)), True,
           f"要留痕（reason=other）：{store.events_recent(3)}")
        eq("没" in str(out.get("note") or ""), True, f"要跟用户说清「没硬塞」：{out.get('note')}")


def test_fact_feedback_still_goes_to_the_correction_channel():
    """`kind=fact` 的老路不许被 `#135` 改坏：仍然登记成**待确认的修正**，不进 `opinions`。"""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        payload = {"topic": "记错了", "text": "别记错", "confidence": 0.9,
                   "is_style_issue": False, "kind": "fact"}
        with patch("app.models.memory_opinions.chat_json", return_value=(payload, None)):
            out = store.learn_opinion("我妈上周来的", "你妈上个月来的吧", note="她记错了时间")
        eq(out["stored"], False, f"事实类不进 opinions：{out}")
        eq([o["topic"] for o in store.opinions(active_only=False)], [], "opinions 里不该有它")
        eq(len(store.pending_corrections()), 1, "要登记成待确认的修正（T81/#98 的通道）")


def test_opinions_can_be_dropped_from_a_regular_command():
    """`T94`：学歪的倾向必须能从**常规出口**停用，不该逼人改数据库。

    `#135` 发现的缺口：`opinions()` 只过滤 `status='dropped'`，而**代码里从来没有路径把它置成
    dropped**（只有 `feedback_corrections` 有 `/fix drop N`）。而倾向是**每轮注入**的——
    所以"提炼器写歪一条"的后果是：她以后每一轮都被那条歪约束压着，除非有人去改库。
    句柄用 `rowid`（报告里印出来、可复制），因为表的主键是六字中文 `topic`，抄错一个字就失败。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.store.client = _FakeLLM()
        with svc.store.db:
            for topic, text in (("语气不符人设", "别用「那敢情好」这类口语，两三句自然回应即可"),
                                ("语气不符", "保持轻松随意的语气，别突然生硬或像在交代任务，两三句说清")):
                svc.store.db.execute(
                    "INSERT INTO opinions(topic,text,hits,sessions,notes,status,"
                    "first_ts,last_ts,evidence) VALUES(?,?,1,'[]',1,'active',0,0,'[]')",
                    (topic, text))

        report = svc.run_command("/mem opinions")
        rids = re.findall(r"#(\d+)", report)
        eq(len(rids), 2, f"报告里要印出**可复制的句柄**：{report}")

        out = svc.run_command(f"/mem opinion drop {rids[1]}")
        eq("停用" in out, True, f"要有明确回执：{out}")
        eq([o["topic"] for o in svc.store.opinions()], ["语气不符人设"],
           "停用的必须正是指定那条（留下的还是第 1 条）")
        eq(svc.store.db.execute("SELECT status FROM opinions WHERE rowid=?",
                                (int(rids[1]),)).fetchone()[0], "dropped", "库里真的标成 dropped")
        eq(any(e.get("kind") == "opinion_dropped" for e in svc.store.events_recent(5)), True,
           f"要留痕：{svc.store.events_recent(3)}")
        again = svc.run_command(f"/mem opinion drop {rids[1]}")
        eq(("停用" in again) or ("找不到" in again) or ("已经" in again), True,
           f"重复停用要给一句人话，不能静默也不能炸：{again}")
        bad = svc.run_command("/mem opinion drop 不是数字")
        eq(isinstance(bad, str) and bad.strip() != "", True, f"乱输入要有回执：{bad}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_life_board_can_delete_an_event_and_pin_a_care():
    """`T91`：看板隐藏流水但保留经历、把一句话钉进她的牵挂。

    起因是用户那句「**我不知道薄荷是哪里来的**」：`#117` 的看板回答了**出处**，但闭环缺一半
    ——她的即兴细节（"阳台那盆薄荷"）**不进任何记忆**，用户不认的时候原来只能手改数据库。
    这条钉住两件事：隐藏的是**看板流水**（保留历史、不动记忆/世界书/此刻状态）；钉进去的进 `life.json.cares`
    （手写那一层，可随时改回来），而且**原来那些牵挂不能被顶掉**。
    """
    from app.models import life as life_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
        lb.advance(force=True)

        ev = lb.recent_events(1)[0]
        eq(isinstance(ev.get("rid"), int), True, f"流水要带 rid（看板靠它删）：{ev}")
        msg, info = lb.delete_event(ev["rid"])
        eq("已从看板隐藏" in msg, True, f"要有回执：{msg}")
        eq(len(lb.recent_events(5)), 0, "流水已从看板隐藏")
        eq(ev["text"] in lb.history_for("你之前做过什么？"), True, "隐藏不抹掉她的过去")
        eq(info.get("id"), ev["rid"], f"埋点载荷要带 id：{info}")
        eq("已经隐藏" in lb.delete_event(ev["rid"])[0], True, "重复隐藏要有回执")
        eq("不是编号" in lb.delete_event("abc")[0], True, "乱输入也要有回执")

        before = lb.script().get("cares") or ""
        m4, i4 = lb.pin_care("阳台上养着一盆薄荷")
        eq("已钉进" in m4, True, f"要有回执：{m4}")
        after = lb.script().get("cares") or ""
        eq("薄荷" in after, True, f"真写进 life.json 了：{after[:60]}")
        eq(before.rstrip("。；; ") in after, True, "原来那些牵挂不能被顶掉")
        eq(i4.get("text"), "阳台上养着一盆薄荷", f"埋点载荷要带内容：{i4}")

        m5, i5 = lb.pin_care("阳台上养着一盆薄荷")
        eq(i5, None, f"重复钉不该重复写：{m5}")
        eq((lb.script().get("cares") or "").count("薄荷"), 1, "库里也只该出现一次")
        eq(lb.pin_care("   ")[1], None, "空内容不该写")


def test_life_edit_routes_are_registered():
    """`T91` 的两条看板写操作路由：`DELETE /api/life/events/{rid}` 与 `POST /api/life/pin`。"""
    import tempfile as _tf

    from app.services import registry
    from app.utils import weather as w
    saved = config.DATA_DIR
    real_refresh = w.WeatherCache.refresh
    config.DATA_DIR = _tf.mkdtemp()
    w.WeatherCache.refresh = lambda self: None
    try:
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as client:
            svc = registry.get("")
            svc.life.advance(force=True)
            event = svc.life.recent_events(1)[0]
            rid = event["rid"]

            resp = client.delete(f"/api/life/events/{rid}")
            eq(resp.status_code, 200, f"删流水要能通：{resp.status_code} {resp.text[:120]}")
            eq("已从看板隐藏" in resp.json()["message"], True, f"回执要有内容：{resp.json()}")
            eq(client.get("/api/life").json()["events"], [], "看板不再显示隐藏流水")
            eq(client.get("/api/life").json()["stats"]["events_total"], 0, "看板只统计可见流水")
            eq(event["text"] in svc.life.history_for("你之前做过什么？"), True,
               "接口隐藏后她仍能回忆原经历")

            resp2 = client.post("/api/life/pin", json={"text": "阳台上养着一盆薄荷"})
            eq(resp2.status_code, 200, f"钉牵挂要能通：{resp2.status_code} {resp2.text[:120]}")
            eq("薄荷" in (svc.life.script().get("cares") or ""), True,
               "钉进去的要落进她的编剧层")
            eq(client.post("/api/life/pin", json={"text": ""}).status_code, 422,
               "空内容必须被 schema 拦下")
    finally:
        w.WeatherCache.refresh = real_refresh
        config.DATA_DIR = saved


def test_short_utterance_dedup_catches_todays_real_repeat():
    """主动消息的**机器判重**：今天真机那条重复必须被抓到（`D4`：提示词压不住就补机器）。

    现场：她 19:21 主动说过「排骨挑好了，老板娘还多搭两根筒骨」，21:38 又说了一遍
    「老板娘还多搭了两根筒骨」——而 `SPEAK_PROMPT` 里那份"【你最近主动说过的话】（**别重复这些**）"
    **当时确实带着这一条**，她还是说了。提示词已经证明压不住。
    """
    from app.utils.textsim import is_repeat, similar

    old = "排骨挑好了，老板娘还多搭两根筒骨"
    new = "老板娘还多搭了两根筒骨"
    eq(similar(new, old) >= 0.62, True, f"今天这条真机重复必须判成相似：{similar(new, old):.2f}")
    eq(is_repeat(new, [old, "晚上炖汤，给你留一碗不"]), True, "要判成重复")
    eq(is_repeat("今天累坏了吧，早点歇着", [old]), False, "无关的话不能被误杀")
    eq(is_repeat("汤里扔进去，炖出来更香", [old]), False, "同一次开口的另一条不该被杀")
    eq(is_repeat("汤炖好了，小青菜也放了", ["旁边摊子的小青菜看着挺嫩"]), False,
       "跟进同一件事、但说的是**新进展** → 不算重复")


def test_proactive_speak_drops_repeats_and_is_visible():
    """`Speaker.speak()` 里的判重：整批都被判成重复 → **这次放弃开口**，并且原因可查。"""
    from unittest.mock import patch

    from app.models import life as life_mod
    from app.services.speak import Speaker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
        sp = Speaker(store.db, lb)
        sp.enqueue("排骨挑好了，老板娘还多搭两根筒骨")     # 已经说过 → 会进"别重复"清单

        with patch("app.utils.llmjson.chat_json",
                   return_value=({"speak": True, "text": "老板娘还多搭了两根筒骨"}, None)):
            out = sp.speak(_FakeLLM(), "m", chat_lines=[])
        eq(out, [], f"整批重复 → 这次不说：{out}")
        eq(bool(getattr(sp, "last_duplicates", None)), True, "要留下判重记录（给埋点用）")

        with patch("app.utils.llmjson.chat_json",
                   return_value=({"speak": True, "text": "汤炖好了，一会儿给你端过去"}, None)):
            out2 = sp.speak(_FakeLLM(), "m", chat_lines=[])
        eq(len(out2) == 1, True, f"新内容要能正常说：{out2}")


def test_proactive_tick_dedup_is_visible_in_events():
    """判重必须**可见**：整批被丢掉时埋一条 `proactive_dup`（否则"她怎么不说话了"又查不到）。"""
    import time
    from unittest.mock import patch

    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.life.advance(force=True)
        svc.life._add_event("afternoon", "排骨挑好了，老板娘还多搭两根筒骨", "model", time.time())
        svc.speaker.should_speak = lambda cfg=None, now=None: (True, "可以说",
                                                              {"gate": "ok", "gate_class": "ok"})
        svc.speaker.enqueue("排骨挑好了，老板娘还多搭两根筒骨")
        motive_id = svc.speaker.motive_candidates()[0]["id"]
        with patch("app.utils.llmjson.chat_json",
                   return_value=({"speak": True, "text": "老板娘还多搭了两根筒骨",
                                  "motive_ids": [motive_id]}, None)):
            out = svc.proactive_tick()
        eq(out, "", f"重复的不该发出去：{out}")
        eq(any(e.get("kind") == "proactive_dup" for e in svc.store.events_recent(8)), True,
           f"要埋 proactive_dup：{svc.store.events_recent(4)}")
        eq(len(svc.speaker.pending_items()), 1,
           "队列里只该有原先那条——判重丢掉的那条不能入队")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_her_completion_updates_state_and_the_slot():
    """`#138`：她说"汤炖好了"→ **状态层当场跟上**（`life_state` ＋ 状态槽位）。

    现场（用户 2026-09-30 的截图）：对话 21:38 已经"炖好了、端过来啦"，而状态页还停在
    19:23 的「回家炖汤中」——因为**主动消息不进提炼**，她自述的状态没人接。
    这条同时钉住两处：`life_state.activity` 要变成完成态；**状态槽位**（状态页看的就是它）也要跟上。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.life.advance(force=True)
        with svc.store.db:
            svc.store.db.execute(
                "UPDATE life_state SET place=?, activity=?, on_mind=? WHERE id=1",
                ("楼下的小菜场", "在肉摊前挑排骨，顺便看旁边摊子上新到的小青菜", "阳台那几盆花还没浇水"))
        svc.store.upsert_slots([{"entity": "沈知夏", "attr": "状态", "value": "回家炖汤中"}])

        svc.observe_life_reply("汤炖好了，小青菜也放了，端过来啦")

        act = str((svc.life.state() or {}).get("activity") or "")
        eq(act != "在肉摊前挑排骨，顺便看旁边摊子上新到的小青菜", True,
           f"活动要变成完成态，不能还停在挑排骨：{act}")
        eq(("炖" in act) or ("完成" in act), True, f"完成态要认得「炖好」：{act}")
        slots = svc.store.slots_all()
        hit = [s for s in slots if s["entity"] == "沈知夏" and s["attr"] == "状态"]
        eq(len(hit), 1, f"状态槽位该只有一条（覆盖式）：{slots}")
        eq(hit[0]["value"] != "回家炖汤中", True,
           f"状态槽位必须跟上对话，不能还停在 19:23 的「回家炖汤中」：{hit[0]['value']}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_greeting_is_grounded_and_does_not_collide_with_pending():
    """`#139`：开场白**不许凭空编她的近况**，也不许和马上要投递的主动消息撞车。

    现场（用户 2026-09-30 21:38；证据在 `logs/app.log`）：
      `21:38:35 开场白：汤炖好了，小青菜也放了，端过来啦` → 21:38:40 她的主动消息到了
      （「老板娘还多搭两根筒骨」…），而当时 `life_state` 还停在 19:00 的「在肉摊前挑排骨」、
      状态槽位停在 19:23 的「回家炖汤中」——**三处对不上**，用户看到的就是这个。
    根因两条：开场白提示词里有约定/世界书/对方的事/上次结尾，**唯独没有"她此刻在做什么"**
    （`life_state` 与流水都没喂）→ 只能自己编一句近况；而它**每次打开页面都重新生成**，
    于是与随后投递的待发表撞在一起（前端 5 秒后轮询）——同一件事两个版本。
    """
    from unittest.mock import patch
    import time as _t

    from app.services.chat_service import GREETING_PROMPT, ChatService

    eq(("{now_state}" in GREETING_PROMPT) and ("此刻" in GREETING_PROMPT), True,
       "提示词要带【你此刻】占位")
    eq("{recent}" in GREETING_PROMPT, True, "提示词要带最近的生活流水")
    eq("别编" in GREETING_PROMPT, True, "要有「别编自己近况」的纪律")

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        svc.life.advance(force=True)
        # 得有点历史，否则 `_make_greeting` 会直接走 canned（模板里的固定问候）
        svc.store.sessions["default"] = [{"role": "user", "ts": _t.time() - 600,
                                          "content": "在吗"}]
        captured = {}

        def fake_chat_text(client, model, messages, **kw):
            captured["prompt"] = messages[0]["content"]
            return "晚上好呀"

        with patch("app.services.chat_service.chat_text", fake_chat_text):
            out = svc.greeting(force=True)
        eq(out, "晚上好呀", f"正常情况下要出开场白：{out}")
        prompt = captured.get("prompt", "")
        eq("你此刻" in prompt, True, f"开场白要看到她此刻在做什么：{prompt[:160]}")
        act = str((svc.life.state() or {}).get("activity") or "")
        eq(act[:6] in prompt, True, f"她的活动要真的进提示词（{act[:12]}）：{prompt[:200]}")

        svc.speaker.enqueue("汤炖好了，一会儿给你端过去")
        with patch("app.services.chat_service.chat_text", fake_chat_text):
            eq(svc.greeting(force=True), "",
               "有待发表时**跳过开场白**（前端对空开场白就是不发那条气泡）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_proactive_prompt_asks_her_to_report_in():
    """`#144` 女友向：**报备是鼓励项，不是禁止项**。

    用户原话：「我想要她能够给我**报备**，有可以和我**分享**的事情，就是女友该有的事情」。
    而改之前 `SPEAK_PROMPT` 明确写着「如果只想复述旧消息、**例行报备**或问一句空泛的"在吗"，
    请选择暂不说」——**这条规则反着写了**，她主动开口时被要求"别说自己在干嘛"。
    这条用例钉住：报备要鼓励；但**复述刚说过的话**（`#138` 判重）与**拿自己的事问他**
    （`#143` 归属）这两条仍然要挡住。
    """
    from app.services.speak import SPEAK_PROMPT as P

    eq("报备" in P, True, "要明说报备是自然的一种")
    eq("例行报备" in P, False, "旧版把'例行报备'列进禁止项，那句必须消失")
    eq("分享已确认近况" in P, True, "允许分享有记录的近况")
    eq(("刚说过" in P) or ("别重复" in P), True, "复述刚说过的话仍要挡（#138）")
    eq("别反问他" in P, True, "归属仍要挡（#143）")


def test_reporting_is_once_per_slot():
    """`#144`：报备**按格**——这一格说过了就不重复，换一格才允许再开口。

    报备的天然单位就是时段（一天 7 格、日上限 6 次，正好对得上）。
    判据从"有没有新流水"改成"**这一格报备过没有**"：否则她要么憋着不报（旧版禁止例行报备），
    要么在一格里反复说同一件事。
    """
    import datetime as _dt
    import time as _t2

    from app.models import life as life_mod
    from app.services.speak import Speaker

    now = _t2.mktime(_dt.datetime(2026, 9, 21, 10, 0).timetuple())   # 上午这一格
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
        sp = Speaker(store.db, lb)
        with store.db:                      # 这一格的生活（比她上次开口**早**→ 不算新线索）
            store.db.execute("INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                             (now - 1800, "forenoon", "上午第二节专业课", "fallback"))

        def _set(**cols):
            # 注意：`life_state` 可能还没有那一行（没跑过 advance 就没有）——
            # 直接 UPDATE 会改到 0 行、`her_last_ts` 依然是空的（这条用例第一次就是这么假绿的）。
            with store.db:
                if not store.db.execute("SELECT count(*) FROM life_state WHERE id=1").fetchone()[0]:
                    store.db.execute("INSERT INTO life_state(id) VALUES(1)")
                sets = ", ".join(f"{k}=?" for k in cols)
                store.db.execute(f"UPDATE life_state SET {sets} WHERE id=1", list(cols.values()))

        cfg = {"enabled": True, "daily_max": 99, "min_gap_hours": 0,
               "quiet_min": 0, "low_min": 30, "low_chance": 1.0,
               "high_min": 60, "high_chance": 1.0, "chance_ceil": 1.0,
               "quiet_hours": (0, 0), "roll_every_min": 0, "decision_gap_min": 0}

        _set(her_last_ts=now - 600, user_last_ts=None, roll_ts=None, declined_at=None)
        ok, why, det = sp.should_speak(now, cfg)
        eq(ok, False, f"同一格已经报备过 → 不再说：{why}")
        eq(det.get("gate"), "reported_this_slot",
           f"要能看出是「这一格报备过了」：{det}")

        # 上一格报备过（3 小时前 → 另一个时段）→ 这一格允许
        _set(her_last_ts=now - 3 * 3600, roll_ts=None, declined_at=None)
        ok2, why2, _ = sp.should_speak(now, cfg)
        eq(ok2, True, f"换了一格就该能报备：{why2}")


def test_relationship_stage_advances_by_days_or_messages():
    """`#146` 关系层：阶段按**可数的事实**推进（天数**或**消息数，满足其一），阈值得能改。

    用户定的方向是「虚拟 AI 女友」，而"关系"原先无处存放——称呼从哪来、熟到哪一步
    换个叫法，全靠模型即兴。这一层慢变、手写，**阈值写在文件里**，随时能调。
    """
    import datetime as _dt

    from app.models.relationship import Relationship

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "relationship.json")
        rel = Relationship(path)
        eq(rel.call_him(), "你", "刚认识就叫「你」")

        # 认识 5 天 → 够资格进「熟络」；但**默认口径是她自己决定**，她没开口就不动（`#148`）
        rel.data["since"] = (_dt.date.today() - _dt.timedelta(days=4)).strftime("%Y-%m-%d")
        rel.save()
        rel2 = Relationship(path)
        eq(rel2.days(), 5, f"认识第 5 天（since={rel2.data['since']}）")
        eq(rel2.call_him(user_msgs=500), "你",
           "阈值只是**资格线**：她没决定推进，就算聊了 500 条也不改口（`#148`）")
        eq(rel2.eligible(user_msgs=500).get("key"), "亲密",
           f"但资格线要算得出来（她最多够得到哪档）：{rel2.eligible(user_msgs=500)}")

        # decide=auto 是**对照口径**：按阈值自动跳（原来的行为，留着备查）
        rel_auto = Relationship(path)
        rel_auto.data["decide"] = "auto"
        rel_auto.save()
        eq(Relationship(path).call_him(user_msgs=500), "亲爱的",
           "decide=auto 时才按阈值自动到最亲近那档")
        rel_she = Relationship(path)
        rel_she.data["decide"] = "she"
        rel_she.save()

        # 手动定档（人操作）会留痕
        eq("已把阶段定为" in rel2.set_stage("亲近"), True, f"能手动定：{rel2.set_stage('亲近')[:20]}")
        eq(Relationship(path).call_him(), "阿哲", "手动定成亲近档 → 那档就是这么写的")
        eq(len(Relationship(path).data.get("history") or []) >= 1, True, "手动改也要留痕")
        eq(Relationship(path).set_stage("she").startswith("已改回"), True, "能切回'她自己决定'")

        # 阈值写在文件里 → 改文件就能改行为
        rel4 = Relationship(path)
        rel4.data["stages"] = [{"key": "刚认识", "call_him": "喂", "min_days": 0, "min_msgs": 0}]
        rel4.save()
        eq(Relationship(path).call_him(user_msgs=999), "喂", "阶段表可手改，改完立刻生效")
        eq("你和他的关系" in Relationship(path).render(), True,
           f"render 就是给提示词的那一段：{Relationship(path).render()[:60]}")


def test_relationship_advance_is_her_decision_with_guards():
    """`#148`：**推进权在她**——她决定要不要更熟一点；程序只当护栏，而且每次都留痕。

    用户原话：「我想的是**她自己来决定**要不要推进关系」。所以默认口径下阶段由**她的决定**
    驱动（不是定时器）；她说要挪，也仍然要过四条护栏：
    ① 一次只挪一档（不许跳）② 够资格 ③ 隔够天数（推进要慢）④ **必须给理由**。
    """
    import datetime as _dt

    from app.models.relationship import Relationship

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "relationship.json")
        rel = Relationship(path)
        rel.data["since"] = (_dt.date.today() - _dt.timedelta(days=4)).strftime("%Y-%m-%d")
        rel.save()
        rel = Relationship(path)

        eq(rel.can_advance()[0], True, f"够资格、也没刚改口，就该放行：{rel.can_advance()}")

        ok, msg = rel.advance("亲密", "我想叫他亲爱的了")
        eq(ok, False, f"不许跳档：{msg}")
        eq("跳档" in msg, True, f"拦下时要说清为什么：{msg}")

        ok2, msg2 = rel.advance("熟络", "")
        eq(ok2, False, f"没理由不落盘：{msg2}")
        eq("理由" in msg2, True, f"要说清缺什么：{msg2}")

        ok3, msg3 = rel.advance("熟络", "他记得我说过的话")
        eq(ok3, True, f"给理由就该落地：{msg3}")
        eq(Relationship(path).call_him(), "阿哲", "落地后称呼跟着变")
        hist = Relationship(path).data.get("history") or []
        eq(len(hist), 1, f"要留痕（她什么时候改的、为什么）：{hist}")
        eq(hist[0].get("source"), "she", "留痕要标明是她自己决定的")

        # 冷却：要让**资格**先够（否则拦下的是"够不到下一档"，不是冷却——这条一开始就写错了）
        rel.data["since"] = (_dt.date.today() - _dt.timedelta(days=11)).strftime("%Y-%m-%d")
        rel.save()
        rel = Relationship(path)
        ok4, msg4 = rel.advance("亲近", "再近一点")
        eq(ok4, False, f"刚改口不久要拦住：{msg4}")
        eq("至少隔" in msg4, True, f"要说清冷却规则：{msg4}")

        # ⚠ 真机探针抓到的坑（`#148`）：够得到的最高档**高于**下一档时，也必须放行。
        # 认识 11 天 → eligible=「亲近」，而下一档只是「熟络」；这里写错过一次，
        # 结果她明明想推进却被"还够不到"误拦（一档一档走本来就该允许）。
        far = Relationship(path)
        far.data["stage"] = ""
        far.data["changed_at"] = 0
        far.data["since"] = (_dt.date.today() - _dt.timedelta(days=10)).strftime("%Y-%m-%d")
        far.save()
        far = Relationship(path)
        eq(far.eligible().get("key"), "亲近", f"认识 11 天该够到亲近：{far.eligible()}")
        ok_far, msg_far = far.advance("熟络", "他愿意跟我说心里话")
        eq(ok_far, True, f"资格比下一档高，也要能一档一档往前走：{msg_far}")
        eq(far.call_him(), "阿哲", "落到了熟络这一档")

        # 资格线不够时也要说清差多少
        fresh = Relationship(path)
        fresh.data["stage"] = ""
        fresh.data["changed_at"] = 0
        fresh.data["since"] = _dt.date.today().strftime("%Y-%m-%d")
        eq("还够不到" in fresh.can_advance(user_msgs=0)[1], True,
           f"够不到下一档时要报差多少：{fresh.can_advance(user_msgs=0)}")


def test_she_can_advance_the_relationship_through_review():
    """`#148`：她的决定走**复盘那条路**（`evolve_personality`），落地后提示词里的称呼就换。

    为什么挂在复盘上：那条路本来就在做"读最近对话 → 提议 → 护栏 → 落地/留档"，
    而且**低频**（不是每轮都动）——正适合"关系慢慢处"。
    """
    from unittest.mock import patch as _patch

    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY",
                                            "AUTO_EVOLVE_PERSONALITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    config.AUTO_EVOLVE_PERSONALITY = True
    svc = None
    try:
        svc = ChatService()
        import datetime as _dt
        svc.relationship.data["since"] = (
            _dt.date.today() - _dt.timedelta(days=4)).strftime("%Y-%m-%d")
        svc.relationship.save()
        # 关系推进前，提示词里不该出现关系层的称呼；推进后再钉具体叫法。
        eq("你平常叫他「阿哲」" not in svc.system_prompt(), True, "还没推进时不该叫名字")

        with _patch("app.services.chat_service.chat_json",
                    lambda *a, **k: ({"relationship": {"advance_to": "熟络",
                                                       "why": "他记得我说过的话"}}, None)):
            ok, msg = svc.evolve_personality(turns=[{"role": "user", "content": "你还记得那件事吗"}])
        eq(ok, True, f"复盘该走通：{msg}")
        eq("熟络" in msg, True, f"说明里要写上关系动了：{msg}")
        eq(svc.relationship.call_him(), "阿哲", "她说要挪，就该真的挪")
        eq("你平常叫他「阿哲」" in svc.system_prompt(), True, "下一轮提示词里的称呼也换了")
        eq((svc.relationship.data.get("history") or [{}])[-1].get("source"), "she",
           "留痕要标明是**她自己**决定的（跟人手动改区分开）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_relationship_lands_in_prompts():
    """`#146`：关系层必须真的进提示词（主对话 + 主动开口 + 开场白三条路）。"""
    from app.services.chat_service import GREETING_PROMPT, ChatService
    from app.services.speak import SPEAK_PROMPT

    eq("{relation}" in SPEAK_PROMPT, True, "主动开口那份要有关系层占位")
    eq("{relation}" in GREETING_PROMPT, True, "开场白那份也要有（第一句话就得叫对）")

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        eq(os.path.exists(os.path.join(config.DATA_DIR, "relationship.json")), True,
           "第一次跑就该把 relationship.json 建出来")
        prompt = svc.system_prompt()
        eq("你和他的关系" in prompt, True, f"系统提示里要有关系那一段：{prompt[:120]}")
        eq("认识第 1 天" in prompt, True, "要写明认识到第几天")
        eq(f"叫他「{svc.relationship.call_him()}」" in prompt, True, "要写明她怎么叫他")
        eq("你答应过他的事" in prompt and "你怎么关心他" in prompt, True,
           "承诺与关心方式也要在（女友向的三件套：称呼／承诺／关心）")
        eq("当前阶段" in svc.run_command("/relationship"), True, "/relationship 能看当前关系")
        eq("已把阶段定为" in svc.run_command("/relationship stage 熟络"), True,
           "/relationship stage 能手动推进")
        eq("记下了" in svc.run_command("/relationship promise 下次给你带后街的烧烤"), True,
           "能把她的承诺记下来")
        eq("烧烤" in svc.system_prompt(), True, "记下的承诺要进提示词（说了就得算数）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_relationship_promise_lifecycle_preserves_history():
    """承诺完成后保留原记录，并留下可查询的关系事件。"""
    from app.models.relationship import Relationship

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "relationship.json")
        rel = Relationship(path)
        rel.data["promises"] = ["下次给他带后街的烧烤"]
        rel.save()
        rel = Relationship(path)

        items = rel.promise_items()
        eq(items[0]["text"], "下次给他带后街的烧烤", "旧字符串承诺要能读成结构化记录")
        eq(items[0]["status"], "open", "旧承诺默认仍是待履约")

        done = rel.complete_promise("1", note="已经带到了")
        eq("已完成" in done, True, f"完成承诺要有明确回执：{done}")
        stored = Relationship(path).promise_items(include_closed=True)
        eq(stored[0]["status"], "done", "完成后状态要持久化")
        eq(stored[0]["text"], "下次给他带后街的烧烤", "完成不能删除原承诺")
        eq("待履约承诺" in Relationship(path).render(), True,
           "提示词要保留待履约承诺这个区块")
        eq("后街的烧烤" in Relationship(path).render(), False,
           "已完成承诺不应继续伪装成待办")
        kinds = [e.get("kind") for e in Relationship(path).events()]
        eq(kinds[-1], "promise_completed",
           f"完成旧承诺也要留下完成事件：{kinds}")
        eq("已完成" in Relationship(path).report(), True,
           "关系报告要能看到已完成，而不是把历史抹掉")


def test_relationship_missed_promise_requires_reason_and_is_auditable():
    """失约必须有原因，状态和关系事件都要保留。"""
    from app.models.relationship import Relationship

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        rel = Relationship(os.path.join(d, "relationship.json"))
        rel.add_promise("周末陪他看电影")
        rejected = rel.miss_promise("1", "")
        eq("原因" in rejected, True, f"失约没有原因不能落盘：{rejected}")
        eq(rel.promise_items(include_closed=True)[0]["status"], "open",
           "被拒绝的失约不能改变承诺状态")

        missed = rel.miss_promise("1", "临时有事没做到")
        eq("没做到" in missed, True, f"失约要把原因带回执：{missed}")
        item = rel.promise_items(include_closed=True)[0]
        eq(item["status"], "missed", "失约状态要持久化")
        eq(item["reason"], "临时有事没做到", "失约原因要保留")
        event = rel.events()[-1]
        eq(event["kind"], "promise_missed", f"要有失约事件：{event}")
        eq(event["source"], "human", f"事件要标明来源：{event}")


def test_relationship_commands_close_promises_without_deleting_them():
    """命令层的 done/miss 走生命周期，不能退回旧的物理删除。"""
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        eq("记下了" in svc.run_command("/relationship promise 下次给他带烧烤"), True,
           "命令仍能创建承诺")
        done = svc.run_command("/relationship done 1")
        eq("已完成" in done, True, f"done 应该完成而不是删除：{done}")
        report = svc.run_command("/relationship")
        eq("已完成" in report and "烧烤" in report, True,
           f"报告要保留完成历史：{report}")

        svc.run_command("/relationship promise 下次早点睡")
        missed = svc.run_command("/relationship miss 1 临时加班")
        eq("临时加班" in missed, True, f"miss 命令要记录原因：{missed}")
        eq("失约" in svc.run_command("/relationship"), True,
           "报告要显示失约状态")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_proactive_drops_asking_him_about_her_own():
    """`#146`：**只有她自己知道的东西**（团子／球衣／课表…）不许反过来问他。

    真机现场（2026-10-01 09:18）：她报备完"在收球衣…刚下楼捡回来"，紧接着问
    「**团子没扒窗户吧**」——团子是她自己的猫，他答不上来。`#143` 在提示词里写了
    三处仍然漏了一次，所以照 `D4` 补**机器兜底**：只丢那一条，报备那条留着。
    """
    import json as _json

    from app.models import life as life_mod
    from app.services.speak import Speaker
    from app.utils.textsim import asks_about_her_own

    eq(asks_about_her_own("团子没扒窗户吧", ["团子"]), "团子", "这句就该被判为归属错")
    eq(asks_about_her_own("团子又趴我腿上呢，你那边忙不忙？", ["团子"]), "",
       "前者是陈述、后者才是问他——不能误杀")
    eq(asks_about_her_own("我课表上明天没课，你那边呢？", ["课表"]), "",
       "「没」不是问句标记：这句是陈述自己没课")
    eq(asks_about_her_own("球衣干了，你吃饭没？", ["球衣"]), "",
       "问的是他吃饭，不是球衣")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
        sp = Speaker(store.db, lb)
        from app.models.relationship import Relationship
        sp.relationship = Relationship(os.path.join(d, "relationship.json"))
        client = _ok_client(_json.dumps(
            {"speak": True,
             # 分条的 `@@@` 必须**独占一行**才切得开（`split_messages` 的契约）——
             # 写成行内 `A@@@B` 的话整段会被当成一条，于是报备那条也跟着被丢掉
             # （这条用例前后红过两次，都是分隔符写错）。
             "text": "在收球衣，有件掉地上了，刚下楼捡回来\n@@@\n团子没扒窗户吧"},
            ensure_ascii=False))
        out = sp.speak(client, "m")
        eq(out, ["在收球衣，有件掉地上了，刚下楼捡回来"],
           f"报备那条留着、归属错的那条丢掉：{out}")
        eq(len(sp.last_ownership), 1, f"要留下判据记录（给埋点用）：{sp.last_ownership}")


def test_quiet_hours_start_at_midnight_not_23():
    """`#147`：静默时段从 23:00 放宽到 **24:00**（用户原话：「放宽到24:00到8:00」）。

    为什么值得钉住：她昨晚**一句都没说过**——23:00 就进静默，而用户睡前的时段恰好是
    23 点到 24 点（"今天怎么样""早点睡"这种话最自然的时刻）。放宽到 0 点之后，
    23 点这一小时她能报备、能说晚安；0 点以后仍然不吵他。
    """
    import datetime as _dt
    import time as _t3

    from app import config
    from app.models import life as life_mod
    from app.services.speak import Speaker

    eq(config.PROACTIVE_QUIET, (0, 8),
       f"默认静默时段应是 0 点到 8 点（原来 23,8）：{config.PROACTIVE_QUIET}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
        sp = Speaker(store.db, lb)
        base = {"enabled": True, "daily_max": 99, "min_gap_hours": 0, "quiet_min": 0,
                "low_min": 30, "low_chance": 1.0, "high_min": 60, "high_chance": 1.0,
                "chance_ceil": 1.0, "quiet_hours": config.PROACTIVE_QUIET,
                "roll_every_min": 0, "decision_gap_min": 0}

        t23 = _t3.mktime(_dt.datetime(2026, 10, 1, 23, 30).timetuple())
        _ok, why, det = sp.should_speak(t23, base)
        eq(det.get("gate") != "quiet_hours", True,
           f"23:30 不该再被静默挡下（放宽之后这一小时她能说晚安）：{why}")

        t00 = _t3.mktime(_dt.datetime(2026, 10, 2, 0, 30).timetuple())
        _ok2, why2, det2 = sp.should_speak(t00, base)
        eq(det2.get("gate"), "quiet_hours", f"00:30 仍旧静默：{why2}")
        eq("0:00–8:00" in str(why2), True, f"提示语要说出新的窗口：{why2}")

        t07 = _t3.mktime(_dt.datetime(2026, 10, 2, 7, 59).timetuple())
        eq(sp.should_speak(t07, base)[2].get("gate"), "quiet_hours", "7:59 还在静默里")
        t08 = _t3.mktime(_dt.datetime(2026, 10, 2, 8, 1).timetuple())
        eq(sp.should_speak(t08, base)[2].get("gate") != "quiet_hours", True, "8 点之后放开")


def test_life_advance_uses_one_clock_source():
    """`T97`：生活推进这条链必须吃**同一个时钟**（`time.time()`），不许各步自己取真实时间。

    现场：`tests/test_promise_reminders.py` 把 `time.time` 冻在 15:00，而 `_advance_life()`
    那几步各自用 `dt.datetime.now()`（**真实**时间，`patch("time.time")` 冻不住）——
    傍晚跑的时候真实时段已是"傍晚"，于是它补了一格、写进一条 `ts=真实时间` 的流水，
    `_has_fresh_life(declined_at)` 随之变 True，"她刚决定不说"的闸门被当成"有新生活"放行 →
    她开口，5 条用例随挂钟红绿（`#158` 施工时撞到，决定性实验见 `T97`）。

    这条用例**不依赖挂钟**：用 spy 换掉三步，只问"它们收到显式的 now 了吗、是不是同一个"。
    """
    import datetime as _dt
    from unittest.mock import patch

    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        frozen = _dt.datetime(2026, 10, 6, 15, 0).timestamp()
        seen: dict = {}

        def fake_needs(now=None):
            seen["needs"] = now
            return True                     # 逼它走完三步

        def fake_missed(now=None):
            seen["missed"] = now
            return 1                        # 1 格 → 走 advance 那条（>1 会去 catch_up）

        def fake_advance(client=None, model="", now=None, force=False):
            seen["advance"] = now
            return {}

        svc.life.needs_advance = fake_needs
        svc.life.missed_slots = fake_missed
        svc.life.advance = fake_advance
        with patch("time.time", return_value=frozen):
            svc._advance_life()

        for step in ("needs", "missed", "advance"):
            eq(seen.get(step) is not None, True,
               f"「{step}」那一步必须收到**显式** now——否则它会自己取真实时间（挂钟依赖的根因）")
            eq(abs(float(seen[step].timestamp()) - frozen) < 1, True,
               f"「{step}」收到的必须是同一个时钟（time.time 的冻结值）：{seen[step]}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_sync_world_drops_by_identity_not_by_stale_index():
    """`B1`（工单二）：`sync_world` 的删除下标是**添加前**算的，而 `_add_memories` 会触发
    `_prune`（物理删冷记忆）→ 下标整体漂移 → `_drop(stale)` 删到**别的条目**上（世界书数据丢失）。

    复现：初值 `[事实F, 世界书A, 世界书B]`（上限 3）→ `sync_world(["世界书B","世界书C"])`：
    预期「**A 删 / B 留 / C 加**」；修前得到 `[世界书A, 世界书C]`——B 被误删、A 反而留下。
    """
    import tempfile

    from app.models.memory import KIND_FACT, KIND_WORLD

    def mem(text, kind, ts=0.0):
        return {"text": text, "kind": kind, "ts": ts, "hits": 0, "last_hit": 0}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=2048), max_memories=3)
        try:
            s.memories = [mem("事实F", KIND_FACT), mem("世界书A", KIND_WORLD),
                          mem("世界书B", KIND_WORLD)]
            added, dropped = s.sync_world(["世界书B", "世界书C"])
            texts = sorted(s.world_texts())
            eq(texts, ["世界书B", "世界书C"],
               f"必须 A 删 / B 留 / C 加（修前是 B 被误删、A 留下）：{texts}")
            eq((added, dropped), (1, 1), f"增删各一条：{(added, dropped)}")
            eq([m["text"] for m in s.memories if m["kind"] == KIND_FACT], [],
               "那条可淘汰的普通事实应当被 _prune 收走（这是下标漂移的成因）")
        finally:
            s.db.close()


def test_ownership_guard_catches_bare_question_mark():
    """`B2-①`（工单二）：`re.split` 把 `？` 当分隔符**吃掉**了 → 纯问号形态全漏。

    修前 `asks_about_her_own("团子在不在家？", ["团子"])` 返回空串——因为分句里
    再也没有 `？` 可判。`#158` 的 6 条用例全带「吧」，所以没抓到这个形态。
    """
    from app.utils.textsim import asks_about_her_own

    eq(asks_about_her_own("团子在不在家？", ["团子"]), "团子", "全角问号的纯问句必须命中")
    eq(asks_about_her_own("球衣晾哪了?", ["球衣"]), "球衣", "半角问号也要命中")
    eq(asks_about_her_own("团子没扒窗户吧", ["团子"]), "团子", "带「吧」的老形态不能退化")
    eq(asks_about_her_own("团子又趴我腿上呢", ["团子"]), "", "陈述句仍然不许命中")


def test_ownership_guard_does_not_kill_his_side_nouns():
    """`B2-③`（工单二）：通用名词（阳台／出租屋／课表）不能把**他那边**的话误杀。

    修前 `asks_about_her_own("你阳台那盆花浇水了吗？", ["阳台"])` 返回 `阳台`——
    而那句话问的是**他的**阳台（她完全有理由问）。
    """
    from app.utils.textsim import asks_about_her_own

    own = ["团子", "球衣", "课表", "出租屋", "阳台"]
    eq(asks_about_her_own("你阳台那盆花浇水了吗？", own), "", "「你阳台」是他的，不该拦")
    eq(asks_about_her_own("你课表上明天有课吗？", own), "", "「你课表」同理")
    eq(asks_about_her_own("你们那阳台的花浇了吗？", own), "",
       "「你们那阳台」是他的（复数「你们」也要认；同一分句里带问号才走得到这条判断）")
    eq(asks_about_her_own("你的出租屋收拾好了吗？", own), "", "「你的出租屋」同理")
    eq(asks_about_her_own("阳台那盆花浇水了吗？", own), "阳台",
       "没点明是谁的 → 仍按她的算（护栏要保守，但不能失效）")
    eq(asks_about_her_own("团子在不在家？", own), "团子", "她独有的名词不受这条影响")


def test_dropped_own_question_is_not_acked_immediately():
    """`B2-②`（工单二）：命中不该**当场 ack**——那是不可恢复的丢弃。

    要的是"可审计、可恢复"：不投递给用户，但**保留在队列里**并留痕，
    让人能看出来"她有一句话被护栏挡下了"。
    """
    import tempfile

    from app.models import life as life_mod
    from app.services.speak import Speaker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _extract_only_client())
        try:
            lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"), her_name="沈知夏")
            sp = Speaker(store.db, lb)
            from app.models.relationship import Relationship
            sp.relationship = Relationship(os.path.join(d, "relationship.json"))
            sp.enqueue("团子在不在家？")
            kept = sp.drop_own_questions(sp.pending_items())
            eq(kept, [], "命中的那一条不投递")
            eq([r["text"] for r in sp.pending_items()], ["团子在不在家？"],
               "但**不能被标成已读**——否则就不可恢复、也没法审计")
            eq(len(sp.last_delivery_drops), 1, f"要留痕：{sp.last_delivery_drops}")
        finally:
            store.db.close()


def test_life_and_speech_writes_share_the_store_lock():
    """`B3`（工单二）：生活／主动说话的写库必须与 `MemoryStore` **共用同一把锁**。

    `db.py:186-190` 的契约写着：跨线程靠 SQLite 自己，**多语句事务的互斥靠 MemoryStore 那把
    RLock**。而 `life.py`（`_save_state`／`_add_event`／`delete_event`）与 `speak.py`
    （`enqueue`／`ack_pending`／`_set_ts`）都是直接 `with self.db:` 写、**一把锁都没拿** →
    在这条共享连接上会和对话路径交错（`#124` 在别处收口过，这两处没跟上）。

    判据刻意**不靠"跑得快慢"**：主线程占住那把锁时，后台的她**必须写不进去**。
    """
    import datetime as _dt
    import tempfile
    import threading

    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        lock = getattr(svc.store, "_lock", None)
        eq(lock is not None, True, "MemoryStore 必须提供那把 RLock（db.py 的契约）")
        frozen = _dt.datetime(2026, 10, 7, 15, 0)

        with lock:
            done = threading.Event()
            thread = threading.Thread(
                target=lambda: (svc.life.advance(now=frozen, force=True), done.set()))
            thread.start()
            thread.join(timeout=0.5)
            eq(done.is_set(), False,
               "占着 store 的锁时她仍然写进去了 → life 的写路径没拿这把锁")
        thread.join(timeout=5)
        eq(done.is_set(), True, "放锁之后应当能写完")

        with lock:
            done2 = threading.Event()
            thread2 = threading.Thread(
                target=lambda: (svc.speaker.enqueue("在收球衣呢"), done2.set()))
            thread2.start()
            thread2.join(timeout=0.5)
            eq(done2.is_set(), False, "主动说话的写路径同样必须等这把锁")
        thread2.join(timeout=5)
        eq(done2.is_set(), True, "放锁之后应当能写完")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for key, value in saved.items():
            setattr(config, key, value)


def test_dedup_and_world_checks_read_vectors_under_the_lock():
    """`B4`（工单二）：去重／世界判定读 `self.vectors`／`self._sims` 时**必须持锁**。

    与 `T24` 已修的"`_sims` 同一表达式读两次"同类根因：判定用的**下标**和它索引的
    `self.memories` 必须来自**同一视界**——否则并发写入（attach/drop 向量）会让
    `sims[j]` 和 `memories[j]` 错位，甚至越界。

    判据刻意**不靠并发碰运气**：把 `store._lock` 换成一把**记账锁**（自己记"当前线程持没持"），
    再在 `_sims` 上挂探针，断言"**每一次** `_sims` 调用都发生在持有那把锁的时候"。
    """
    import tempfile

    from app.models.memory import KIND_FACT, KIND_WORLD

    class TrackingLock:
        """记账用的锁包装：谁进来过、现在是不是有线程持着。"""

        def __init__(inner, real):
            inner.real = real
            inner.holders = set()

        def __enter__(inner):
            inner.real.acquire()
            inner.holders.add(threading.get_ident())
            return inner

        def __exit__(inner, exc_type, exc, tb):
            inner.holders.discard(threading.get_ident())
            inner.real.release()
            return False

        def acquire(inner, *args, **kwargs):
            got = inner.real.acquire(*args, **kwargs)
            if got:
                inner.holders.add(threading.get_ident())
            return got

        def release(inner):
            inner.holders.discard(threading.get_ident())
            inner.real.release()

    import threading

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = _mk_store(d, _FakeLLM(dim=8))
        try:
            s.add_turn("我养了只猫叫年糕", "记住啦")          # 先有一条 + 一个向量
            tracker = TrackingLock(s._lock)
            s._lock = tracker
            seen: list[bool] = []
            real_sims = s._sims

            def spy(vec):
                seen.append(threading.get_ident() in tracker.holders)
                return real_sims(vec)

            s._sims = spy
            s._add_memories(["我养了只猫叫年糕"], KIND_FACT)     # 去重段（B4-①）
            s._add_memories(["用户住在杭州"], KIND_WORLD)        # 先落一条世界书
            assert s._lock is tracker
            s._add_memories(["用户住在杭州"], KIND_WORLD)        # 世界判定段（B4-③）

            eq(len(seen) >= 2, True, f"这两条路径都该读到向量：seen={seen}")
            eq(all(seen), True,
               f"有 {seen.count(False)} 次 `_sims` 是在**没持锁**的情况下读的 → 判定会与并发写入错位")
        finally:
            s.db.close()


def test_proactive_enqueue_and_take_is_idempotent():
    """她说的话进待发表；**取走即标记**，同一条不会被投两次。"""
    from app.models import life as life_mod
    from app.services.speak import Speaker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))
        sp = Speaker(store.db, lb)
        sp.enqueue("刚开完个会，脑子有点木")
        sp.enqueue("你那边今天怎么样")
        first = sp.take_pending()
        eq(len(first), 2, f"两条都该取到：{first}")
        eq([x["text"] for x in first][0], "刚开完个会，脑子有点木", "按顺序")
        eq(sp.take_pending(), [], "**再取就空了**（取走即标记，不会重复投递）")
        # 她说的话也不许进记忆（隔离边界）
        eq(len(store.memories), 0, "她主动说的话不进 memories")


def test_speak_splits_into_messages_like_a_normal_person():
    """T86（主动说话那一半）：她主动发消息时能分 1~3 条，且**标记不许进待发表**。

    口径是用户的两句原话——**"按正常人说话的方式"** ＋ **"她可以选择自己想要的说话方式"**：
      · **说几条由她自己定**：一句话就说完的发一条，确实有两三句不同的话才分条；
      · 系统**不替她决定**，也不设比主对话更紧的上限（两边同一条契约：最多 3 条）；
      · 分条的标记是**内部约定**，`pending_say` 里存的必须是去过标记的干净文本，
        否则前端轮询到就会把 `@@@` 直接显示给用户看。
    """
    from app.models import life as life_mod
    from app.services.speak import Speaker

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = _mk_store(d, _FakeLLM())
        lb = life_mod.LifeBook(store.db, path=os.path.join(d, "life.json"))

        def sp(name):
            return Speaker(store.db, lb, session=name)

        eq(sp("s1").speak(_ok_client('{"text": "在呢，刚给花浇完水"}'), "m"),
           ["在呢，刚给花浇完水"], "没有标记 = 一条（不许硬分）")
        eq(sp("s2").speak(_ok_client(
            '{"text": "刚给阳台那几盆浇完水\\n@@@\\n你那边今天怎么样？"}'), "m"),
           ["刚给阳台那几盆浇完水", "你那边今天怎么样？"], "两句不同的话 → 两条")
        eq(sp("s3").speak(_ok_client('{"text": "刚忙完\\n@@@ 你呢"}'), "m"),
           ["刚忙完", "你呢"], "行首标记后直接跟内容（真机出现过的写法）也要切")
        eq(sp("s4").speak(_ok_client('{"text": "一\\n@@@\\n二\\n@@@\\n三\\n@@@\\n四"}'), "m"),
           ["一", "二", "三\n四"],
           "说几条由她定；上限与主对话**同一条契约**（3 条），多的并进最后一条")
        eq(sp("s5").speak(_ok_client('{"text": "一\\n@@@\\n二\\n@@@\\n三"}'), "m"),
           ["一", "二", "三"], "三条也允许（她选了三条，系统不拦）")
        eq(sp("s5").speak(_ok_client('{"text": ""}'), "m"), [], "生成空 = 这次不说")
        eq(sp("s5").speak(_ok_client("这不是 JSON"), "m"), [], "非法 JSON = 这次不说")


def test_pending_api_route_is_registered_and_shaped():
    """`GET /api/pending`（前端轮询的接口）：路由在、返回 `{items: [...]}`、**不会 500**。

    前端每 30 秒打一次这个接口（`app.js` 的 `pollPending`），所以它必须：
    ① 路由真的注册了；② 没有待发消息时返回空列表而不是报错；③ 响应结构固定。
    ⚠ **口径订正（2026-09-29）**：原 docstring 写"这里**不启动 lifespan**（用 `TestClient`
    的裸实例）"——**与实现不符**：`with TestClient(app)` 恰恰就是启动 lifespan（不带 `with`
    的才是裸实例）。而启动就会建默认用户的 `ChatService`，它在 `AMAP_KEY` + 城市都就绪时
    会真去拉一次天气——`.env` 里配了 `WB_AMAP_KEY` 时用的就是**真 key**（`T88` 的同一个口子）。
    这里**保留 lifespan**（要验的正是"装好之后的 app 有没有这条路由"），但把天气刷新打成
    no-op：零网络，也不再依赖"这台机器现在有没有配高德 key"。
    """
    import tempfile as _tf

    from app.utils import weather as w
    saved = config.DATA_DIR
    real_refresh = w.WeatherCache.refresh
    config.DATA_DIR = _tf.mkdtemp()          # 隔离，别碰真实数据
    w.WeatherCache.refresh = lambda self: None
    try:
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as client:
            resp = client.get("/api/pending")
            eq(resp.status_code, 200, f"接口要能通：{resp.status_code} {resp.text[:200]}")
            body = resp.json()
            eq(isinstance(body.get("items"), list), True, f"结构要固定：{body}")
    finally:
        w.WeatherCache.refresh = real_refresh
        config.DATA_DIR = saved


# ------------------------------------------------ T86 多消息气泡（非流式那一半）
def test_split_messages_matches_the_frontend_contract():
    """T86：把一轮回复按 `@@@` 切成 1~3 条（**非流式**：文本已经全在手上）。

    为什么服务端也要有一份：`/api/chat`（非流式）拿到的是一整段，它没有前端那个
    增量切分器可用；而两条路径必须给出**同一条结果**，否则"先用非流式看效果"看到的
    和线上流式就是两回事。规则与 `app/views/js/bubble.js` 逐条一致
    （`tests/bubble.test.mjs` 里有对应的用例）：
      ① 标记必须**独占一行**；② 空行不参与判断；③ 超过上限并进最后一条，不丢字。
    """
    from app.utils.bubbles import split_messages

    eq(split_messages("就一句话，没什么好分的。"), ["就一句话，没什么好分的。"],
       "没有标记 = 单条（不许为了分条硬分）")
    eq(split_messages("一\n@@@\n二\n@@@\n三\n@@@\n四"), ["一", "二", "三\n四"],
       "超过 3 条并进最后一条，不丢字、不静默截断")
    eq(split_messages("第一句@@@第二句"), ["第一句@@@第二句"],
       "标记混在句子里不算（宁可少切一条，也不许切坏她的原话）")
    eq(split_messages("邮箱是 a@@@b 你记一下"), ["邮箱是 a@@@b 你记一下"], "同上，带空格也一样")
    eq(split_messages("一\n  @@@  \n二"), ["一", "二"], "标记行前后的空白不算（模型常带）")
    eq(split_messages("\n\n@@@\n先喘口气\n@@@\n\n"), ["先喘口气"], "空行不产生空气泡")
    eq(split_messages("一\n@@@\n二", max_messages=1), ["一\n二"], "上限 1 时并成一条")
    eq(split_messages("一\n@@@\n二\n@@@\n三", max_messages=5), ["一", "二", "三"],
       "没到上限就正常分")
    eq(split_messages("   \n\n  "), [], "空回复 = 没有消息（不许返回 ['']）")
    eq(split_messages(""), [], "空串同理")


def test_chat_nonstream_keeps_the_marker_inside():
    """T86 第 2 步接线：分条指令**只挂非流式那一轮**，标记一个都不许漏出去。

    ⚠ 诚实记录：这条用例是**先写实现、后补的测试**（分条纯函数那一半是严格 TDD：
    先红后绿）。它守的是三件事：
      ① 分条指令确实挂在这一轮的 messages 上（不挂就永远切不出东西）；
      ② `reply` 里**没有** `@@@`（前端/CLI/评测谁都不会看到标记）；
      ③ 写进记忆与滑窗的是**去过标记的干净全文**，而不是带标记的原文
         （否则提炼器会把分隔符当正文记下来，污染以后的上下文）。
    """
    from app.services.chat_service import ChatService

    tmp = tempfile.mkdtemp()
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY",
                                            "CHECK_ON_START")
             if hasattr(config, k)}
    config.DATA_DIR = tmp
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    config.CHECK_ON_START = False        # 启动对账要真调一次模型，这条用例用不着
    svc = None
    try:
        svc = ChatService()
        seen = {}

        def fake_prepare(question, **kw):
            # 分条指令现在由 `prepare()` 追加（`multi_message=True` 时才给），
            # 所以这里要照它的行为模拟，并记下"这条路径有没有要分条"。
            seen["prepare_kw"] = kw
            msgs = [{"role": "system", "content": "（系统提示）"}]
            if kw.get("multi_message"):
                msgs.append({"role": "system", "content": ChatService.MULTI_MESSAGE_HINT})
            return msgs, []

        svc.prepare = fake_prepare

        def fake_ask(messages):
            seen["messages"] = messages
            return "哎，今天累坏了吧\n@@@\n先洗个热水澡，别硬扛。\n@@@\n明天我陪你说说话"

        def fake_finish(question, answer):
            seen["remembered"] = answer
            return {"memory_note": "", "profile_note": ""}

        svc.ask = fake_ask
        svc.finish_turn = fake_finish
        out = svc.chat("今天上班好累啊")

        eq(out["messages"], ["哎，今天累坏了吧", "先洗个热水澡，别硬扛。", "明天我陪你说说话"],
           f"要切成 3 条：{out['messages']}")
        eq("@@@" in out["reply"], False, f"reply 不许带标记：{out['reply']!r}")
        eq(seen["remembered"], out["reply"],
           "写进记忆/滑窗的必须是**去过标记的干净全文**")
        eq(any(m.get("content") == ChatService.MULTI_MESSAGE_HINT
               for m in seen["messages"]), True,
           "分条指令要挂在这一轮的 messages 上")
        eq(seen["messages"][-1].get("content") == ChatService.MULTI_MESSAGE_HINT, True,
           "分条指令要挂在**最后一条**（离提问最近，才算得上'这一轮'的说话方式）")
        eq(seen["messages"][-1].get("role"), "system",
           f"它是 system 段、不是用户消息：{seen['messages'][-1]}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_split_messages_accepts_a_marker_at_the_line_start():
    """T86 放宽口径（真机 8 个样本逼出来的）：**行首**是标记就分条。

    原来要求"整行正好是标记"，而真机里 6 次想分条有 3 次写法不合格
    （`@pp` / `@@` / `@@@ ` 后面直接跟内容）。第三种就是这条修的：它会被当正文
    **原样发给用户看**。放宽的只是"标记后面还有没有内容"，
    **没有**放宽"必须在行首"——`第一句@@@第二句`、`邮箱是 a@@@b` 仍然一个字都不许动。
    """
    from app.utils.bubbles import split_messages

    eq(split_messages("甲\n@@@ 乙"), ["甲", "乙"], "行首标记 + 同行剩余内容 → 剩余算下一段")
    eq(split_messages("甲\n@@@乙"), ["甲", "乙"], "标记后面不带空格也一样")
    eq(split_messages("甲\n@@@ 乙\n@@@ 丙"), ["甲", "乙", "丙"], "连写两次也认得")
    eq(split_messages("甲\n  @@@  乙"), ["甲", "乙"], "标记前后的空白不算")
    eq(split_messages("甲@@@乙"), ["甲@@@乙"], "**行首**才认，句中不算（这是底线）")
    eq(split_messages("第一句@@@第二句"), ["第一句@@@第二句"], "同上")


def test_pure_suite_never_builds_a_network_client():
    """T88：套件里**不许出现真的 OpenAI 客户端**（它会带着 `.env` 里的真 key 出网）。

    为什么要有这条：本文件有 16 处 `ChatService()`，各自 new 三套客户端；漏掉一处
    （或后人删掉文件顶部那句单点拦截）就会真去请求第三方。这里**运行时**验：
    随手建一个 `ChatService`，看三套客户端是不是假的——尤其 embedding 那套
    （它是历史上真的漏出去过的那个：`app.log` 里那些 `401 invalid_api_key` 就是它）。
    """
    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        from app.services.chat_service import ChatService

        svc = ChatService()
        eq(isinstance(svc.client, _FakeLLM), True, "后台活客户端必须是假的")
        eq(isinstance(svc.chat_client, _FakeLLM), True, "主对话客户端必须是假的")
        eq(isinstance(svc.store.embed_client, _FakeLLM), True,
           "embedding 客户端必须是假的（历史上真的漏出去过的就是它）")
        eq(config.CHECK_ON_START, False, "启动对账要关掉（它会真调一次模型）")
        # 感知层那条出口也要堵住：光换 OpenAI 客户端不够，weather/geo/places 走的是 urllib
        import urllib.request as _u

        try:
            _u.urlopen("https://restapi.amap.com/v3/weather/weatherInfo")
            eq(True, False, "urllib 出口必须是堵住的（T88）——否则它真会出去")
        except RuntimeError as exc:
            eq("不许联网" in str(exc), True, f"堵住之后要给出能看懂的原因：{exc}")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


def test_prepare_only_hints_multi_message_when_asked():
    """`prepare()` **默认不给**分条指令——给了却没人切，用户就会看到 `@@@` 字面量。

    这条守的是三条路径的边界（`T86`）：
      · 网页流式（`/api/chat/stream`）与非流式（`/api/chat`）→ 显式要（True），它们会切；
      · **CLI**（`openai_hello.py`）→ 默认 False：它直接把流打到终端，不做切分；
      · **benchmark** → 默认 False，而且这一条最要紧：改动它的提示词会让
        `T40`/`#038` 那批**历史基线立刻不可比**。
    """
    from app.services.chat_service import ChatService

    saved = {k: getattr(config, k) for k in ("DATA_DIR", "API_KEY", "CHAT_API_KEY",
                                            "EMBED_API_KEY", "AMAP_KEY", "AMAP_CITY")
             if hasattr(config, k)}
    config.DATA_DIR = tempfile.mkdtemp()
    config.API_KEY = config.CHAT_API_KEY = config.EMBED_API_KEY = "test-key"
    config.AMAP_KEY = config.AMAP_CITY = ""
    svc = None
    try:
        svc = ChatService()
        plain, _ = svc.prepare("在吗")
        eq(any("@@@" in str(m.get("content", "")) for m in plain), False,
           "默认路径不许出现分条标记（不然标记会漏到终端/评测里）")
        full, _ = svc.prepare("在吗", multi_message=True)
        eq(str(full[-1].get("content", "")) == ChatService.MULTI_MESSAGE_HINT, True,
           "要了才给，而且要放在**最后一条**（离提问最近）")
    finally:
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        for k, v in saved.items():
            setattr(config, k, v)


if __name__ == "__main__":
    sys.exit(main())
