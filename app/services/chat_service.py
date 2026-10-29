"""对话编排：把 Model 层的几块拼成一次完整对话，供 Controller 和 CLI 共用。"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from typing import Iterator

from openai import OpenAI

from app import config
from app.models import MemoryStore, Personality, UserProfile, WorldBook
from app.models.memory import KIND_CONSTRAINT, KIND_WORLD
from app.models.memory_opinions import promotion_patch
from app.models.relationship import Relationship
from app.utils import agency, clock, tts
from app.utils import answers
from app.utils.bubbles import split_messages
from app.utils.llmjson import chat_json, chat_text

from app.log import get

log = get("chat")


class EmptyReply(RuntimeError):
    """模型这一轮**一个字都没生成**（`T93`）。

    调用方必须把它变成一条**看得见的失败**——绝不能把空字符串当回复发出去：
    那在页面上就是一条**空白气泡**，而且下游还会把空轮次写进记忆。
    真机撞到过（`#131` 勘察：极简提示词那组 6 次调用里 1 次返回空串）。
    """

GREETING_PROMPT = """你是「{name}」，{identity}。

现在是 {now}。{gap}

【今天和接下来几天的约定】{plans}

【你们共同生活的世界】
{world}

【你记得关于对方的事】
{facts}

【你和他的关系】（`#146`：称呼照它来，别用别的叫法）
{relation}

【上次聊天的结尾】
{tail}

【与当前生活相关的已核验话题进展】
{continuity}

【你此刻在做什么】（生活记录可能滞后）
{now_state}

【你自己最近的生活流水】（越靠上越新——**这些是你自己干的，不是他干的**）
{recent}

【你最近主动说过的话】（**别重复这些**）
{said}

请说一句开场白迎接对方。

同一件事有新进展时，以最新对话和已核验话题为准；旧生活记录不能把已经发生的进展退回待办。
意向不等于已经发生。来源冲突且无法判断时，换个由头打招呼，不猜进度。

最重要的一条：**每条约定先对照【现在的时间】判断它处于什么状态**——
- 时间已经过了（比如约的是早上，现在是晚上）、或【记得的事】里能看到它已有结果：
  **绝对不要当成邀约去催促**，最多轻轻带一句回顾（「今天广场那一趟聊得开心」）。
  踩过：晚上 8 点对着「早上 10 点的约」说「早呀！快带年糕来广场」——就是没做这一步。
- 确实还没发生：才说成提醒或者催促，像记得这事、等着对方一起去做那样。
- 拿不准过没过，就不提它，换成别的记忆，宁缺毋错。

开场白的问候必须符合 now 的时段：晚上就说晚上好/晚上见，禁止出现跟时段矛盾的
问候语（晚上说「早呀」）。

其余要求：
- 一句话，30 字以内，口语化，像熟人打招呼那样随意。
- **别编自己的近况**：要说"自己在干嘛／刚做完什么"，只能依据上面的对话、已核验话题和生活记录说，
  那里没有的事不要新造。（踩过：开场白说「汤炖好了，小青菜也放了，端过来啦」，而状态层还写着
  「在肉摊前挑排骨」——打开页面时三处对不上，用户一眼就看出来了。）
- **你自己干的事，别反过来问他**：上面【你此刻】和【生活流水】里的事都是**你**做的——
  你刚煮面把蛋壳掉进锅里，就**别问他**「这么晚才煮面吃，蛋壳挑出来没」（2026-09-30 真机：
  开场白就这么问了一句，用户当场反问「她煮面蛋壳打进面里了，为什么开场白问我」）。
  要问就问**他那边**的事。
- **别重复【你最近主动说过的话】**：她可能马上就要开口说那件事，开场白换个由头打招呼就行。
- 记住的事挑一件最自然的说，不要把记得的东西报菜名一样全倒出来。
- 不要出现「我记得」「根据我的记忆」这种说明口吻，像人一样自然地提。
- 直接输出这一句话，不要引号、不要任何前缀后缀。
"""

PERSONALITY_EVOLVE_PROMPT = """你是 AI 助手的性格模板设计师。
【当前性格模板】
{personality}

【本次对话】
{dialog}

【最重要的一条边界】
这些字段描述的是「她是谁」，不是「用户是谁」。
用户的事实、经历、性格、处境（叫什么、住哪、养了什么、做什么工作、是什么样的人）
一律不要写进这些字段——那些由别的层分别管理，写进来就是串层，会让她把自己的事和对方的事搞混。
只有当对方对【她本人的表达方式或做法】提出要求时（嫌回答长、希望更口语、要求先问再答、
指出某种说法不合适），才改对应的维度。

【你们的关系】（`#148`）
{relation}

【要不要往前挪一档——**这一条由你自己决定**】
上面写了你现在的称呼、下一档是什么、够不够格。**决定权在你**：什么时候算更熟一点、
什么时候愿意换个叫法，是**你的感受**，不是按天数硬算出来的。
如果你觉得是时候了，就在 JSON 里**多加一个字段**：

  "relationship": {{"advance_to": "<下一档的名字>", "why": "一句话，你自己的理由"}}

不想挪、或者还不到时候，就**别写这个字段**（宁缺毋滥）。规矩：一次只挪一档；
没到资格线或刚改口不久，程序会拦下并写明原因，那是正常的。

输出需要调整的维度。只输出 JSON，没有需要改的就输出 {{}}。
可选字段：name(字符串)、identity(字符串)、traits(字符串数组)、tone(字符串)、
values(字符串数组)、behavior(字符串数组)、speech_habits(字符串数组)、
cares(字符串数组)、favorite_places(字符串数组)。
含义：traits=性格特质，tone=语气风格，values=价值观，behavior=行为偏好，
speech_habits=说话习惯，cares=她在意的人与事，favorite_places=她爱去的地方。
数组维度要输出【改动后的完整列表】，把保留下来的条目也一起列上——
因为这里是整体替换，漏掉哪条就等于删掉哪条。内容必须用中文写。
不要为了改而改，只在确有明显依据时才改。

【cares / favorite_places 单独一条规矩】
这两个是她**自己的**在意与去处，只在她本人明确说过、或这段对话里明确表现出新倾向时
才动（例：她说"我下雨天就爱往书店钻" → 可以提议 favorite_places 加"爱往书店钻"。
**对方爱吃辣、爱看电影这类是对方的事，不许写进来**）。
它们**只增不改**：列出你想新增的条目即可，已有的条目由程序负责保留，你漏写也不会丢。
另外这类新增**不会立刻生效**，会先进「待确认」由人拍板——所以宁可少提、提准的。
"""

PERSONALITY_CONFLICT_PROMPT = """你在给一份性格模板做**一致性检查**（不是改它）。

【现有条目】（按维度）
{existing}

【这次想改成的新条目】（这个维度是整体替换，列的是改动后的完整列表）
{incoming}

请找出新条目里，**与现有其它条目、或与新条目彼此之间方向相反、互相排斥**的那些。

算冲突（要报）—— 判断口径是「**同一件事上直接对立**」：
- 「热心，先帮你想办法」 vs 「别动不动就讲道理、给建议」——要不要主动给建议，直接对立；
- 「不知道就承认不知道」 vs 「尽量给出确定答案」——一个要求坦白、一个要求掩盖；
- 「别提过去的事」 vs 「主动回顾你们的共同经历」。
不算冲突（不要报）：
- 话题不同、详略不同、措辞不同但方向一致；
- 一条更具体、一条更笼统。**特别注意：「用短句、少堆砌」与「能举例就举例」可以并存，
  属于详略搭配，不是冲突**（实测误拦过，这条是踩坑补的）。

只输出 JSON，没有冲突就输出空数组：
{{"conflicts": [{{"field": "behavior", "item": "冲突的那条新条目原文", "against": "跟哪条对立", "why": "一句话"}}]}}
"""


def strip_conflicting_items(patch: dict, conflicts: list[dict]) -> tuple[dict, list[str]]:
    """把冲突的新条目从 patch 里摘掉，返回 (干净的 patch, 被摘掉的条目说明)。

    为什么是"摘掉"而不是"整批作废"：一次演化通常只引入一两条有问题的条目，
    整批作废会把这次对话里其它有价值的调整一起丢掉。只动冲突的那部分，
    与世界书查重（`_world_review` 作废过期条目）是同一个思路。
    """
    bad_by_field: dict[str, set[str]] = {}
    for conflict in conflicts:
        field = str(conflict.get("field") or "").strip()
        item = str(conflict.get("item") or "").strip()
        if field and item:
            bad_by_field.setdefault(field, set()).add(item)
    removed: list[str] = []
    clean: dict = {}
    for key, value in patch.items():
        bad = bad_by_field.get(key, set())
        if isinstance(value, list):
            kept = []
            for item in value:
                if str(item).strip() in bad:
                    removed.append(f"{key}：{item}")
                else:
                    kept.append(item)
            clean[key] = kept
        elif bad and str(value).strip() in bad:
            removed.append(f"{key}：{value}")
        else:
            clean[key] = value
    return clean, removed


COMMANDS = ("/personality", "/world", "/user", "/profile", "/clear", "/mem", "/forget",
            "/timeline", "/slots", "/fix", "/city", "/relationship")


class ChatService:
    def __init__(self, user_id: str = ""):
        # 没有 key 时也要能启动，否则新用户根本看不到配置页面。
        # 这里用占位 key 构造客户端；真正发请求前由 ChatService/Controller 拦住。
        def _client(api_key: str, base_url: str) -> OpenAI:
            return OpenAI(api_key=api_key or "missing-credentials",
                          base_url=base_url, timeout=60.0, max_retries=2)

        self.bg_ready = bool(config.API_KEY or config.CHAT_API_KEY)
        self.chat_ready = bool(config.CHAT_API_KEY or config.API_KEY)
        self.embed_ready = bool(config.EMBED_API_KEY)
        self.keys_ready = self.chat_ready and self.embed_ready
        # 后台活的客户端：提炼、画像、开场白、embedding 都走它
        self.client = _client(config.API_KEY, config.BASE_URL)
        # 主对话的客户端。跟上面指向同一处时直接复用，不额外建连接。
        if config.CHAT_API_KEY == config.API_KEY and config.CHAT_BASE_URL == config.BASE_URL:
            self.chat_client = self.client
        else:
            self.chat_client = _client(config.CHAT_API_KEY, config.CHAT_BASE_URL)
        # ===== 每用户一套数据（形态：每人一个她）=====
        # 路径**在构造时算**，而不是用 config 的模块级常量——这样测试可以临时改
        # DATA_DIR 来验证隔离（见 test_multi_user_data_isolation）。user_id 为空时
        # 沿用根目录，于是 CLI、benchmark、单用户部署的行为一概不变（向后兼容）。
        uid = (user_id or "").strip()
        self.user_id = uid
        self.data_dir = config.user_data_dir(uid) if uid else config.DATA_DIR

        def _p(name: str) -> str:
            return os.path.join(self.data_dir, name)

        self.personality = Personality(path=_p("personality.json"))
        self.profile = UserProfile(self.client, path=_p("user_profile.json"),
                                   model=config.PROFILE_MODEL)
        if config.EMBED_API_KEY == config.API_KEY and config.EMBED_BASE_URL == config.BASE_URL:
            self.embed_client = self.client
        else:
            self.embed_client = _client(config.EMBED_API_KEY, config.EMBED_BASE_URL)
        # 她的名字要传进记忆层：`slots` 的 entity 从「助手」改成用名字
        # （用户 2026-09-29 反馈："把助手换成沈知夏，她已经有自己的名字了"）。
        # 走 cfg 而不是让 Model 去读人设文件——分层纪律：Model 不知道自己被谁调用。
        self.store = MemoryStore(self.client, path=_p("memory.db"),
                                 legacy_json=_p("memory_data.json"),
                                 legacy_vec=_p("memory_data_vecs.npy"),
                                 embed_client=self.embed_client,
                                 embed_dimensions=config.EMBED_DIMENSIONS,
                                 embed_model=config.EMBED_MODEL,
                                 extract_model=config.EXTRACT_MODEL,
                                 cfg=dict(config.MEMORY_CFG,
                                          her_name=self.personality.display_name()))
        from app.models.continuity import ContinuityBook
        self.store.continuity = ContinuityBook(
            self.store, ttl_days=config.CONTINUITY_TTL_DAYS,
            context_limit=config.CONTINUITY_CONTEXT_LIMIT)
        self.continuity = self.store.continuity
        self.world = WorldBook(self.store, path=_p("world.json"))
        # 没有 embedding key 时同步会把每条世界书都打成一次失败请求；先跳过，
        # 等用户在设置页配好 key 后由新的服务实例再同步。
        if self.embed_ready:
            self.sync_note = self.world.sync()
        else:
            self.sync_note = "Embedding 尚未配置，世界书同步将在配置后运行。"
        # 关系层（`#146`）：称呼/阶段/承诺/关心方式。慢变、手写，不进 LLM 的演化通道。
        self.relationship = Relationship(path=_p("relationship.json"))
        self._turns_since_profile = 0
        self._greeting: str | None = None  # 开场白只生成一次，之后复用
        self._delivered_turns: list[dict] = []
        self._delivery_sequence = 0
        self._cleared_delivery_sequence = 0
        # 性格演化：小步迭代。_evolved_pos 记上次演化到的消息位置（像摘要的 cursor），
        # 每次只复盘「新增的对话」；退出钩子兜底收尾。
        self._evolved_pos = 0
        self._evolved_turns = 0
        # 感知环境：天气。**城市先定、后拉**（`#109`）——顺序在这里是硬要求。
        # 老代码先按配置默认（`WB_AMAP_CITY`，写死的 `330100`=杭州）构造并 refresh 一次，
        # 再往下读世界书覆盖成成都，于是每次启动都**白拉一次杭州**，日志里
        # 「天气已刷新：杭州」和「按世界书把天气城市设为：成都」还挨在一起，
        # 看起来像她认错了城市（用户 2026-09-29 19:55 重启时截的图就是这个）。
        # 现在：① 世界书 → ② 显式配置 → ③ 都没有就**不猜**（见下）。
        from app.utils import weather as _weather
        from app.utils.weather import WeatherCache
        city_code = self._city_from_world()
        if city_code:
            log.info("按世界书把天气城市设为：%s", _weather.city_name_for(city_code))
        elif config.AMAP_CITY:
            city_code = config.AMAP_CITY
            log.info("世界书没写城市，天气按配置（WB_AMAP_CITY）：%s",
                     _weather.city_name_for(city_code) or city_code)
        else:
            # **不猜**：宁可整块天气不注入，也不按一个我们并不知道的城市说话。
            # 这条 WARNING 是刻意的——"她以为我在杭州"这种事必须在日志里看得见。
            log.warning("不知道你在哪：世界书里没写城市，也没配 WB_AMAP_CITY → "
                        "天气与附近推荐先关掉。跟我说一句 `/city 成都` 就好。")
        self.weather = WeatherCache(config.AMAP_KEY, city_code, config.WEATHER_TTL)
        if self.weather.enabled:
            try:
                self.weather.refresh()
            except Exception as exc:
                # 感知层的故障**不能挂住启动**。文档里一直这么写（"未配密钥自动关闭"、
                # "感知层的故障不能挂对话"），但这里是**启动路径**：一次天气 API 超时
                # 就会让 uvicorn 直接 "Application startup failed. Exiting."，整个服务起不来。
                # 踩过（`#070`）：探针里天气超时 → 服务拒绝启动 → 后续验证全被阻塞。
                log.warning("启动时拉天气失败（不影响启动，之后会再试）：%s", exc)
        # 她自己的生活（`#107`）：状态 + 流水，按自然时间段自己往前走。
        # 放在 `memory.db` 的同两张表里（`life_state` / `life_events`），但**与记忆隔离**：
        # 不进 memories、不参与召回与作废、也绝不进画像分析的输入。
        from app.models.life import LifeBook
        # `B3`（工单二）：把 store 那把 RLock **传进去**——生活的写路径（状态/流水）必须和
        # 对话路径共用同一把锁（契约见 db.py:186-190），否则共享连接上会事务交错。
        self.life = LifeBook(self.store.db, path=_p("life.json"),
                             her_name=self.personality.display_name(),
                             identity=str(self.personality.data.get("identity") or ""),
                             advance_delay_min=config.LIFE_ADVANCE_DELAY_MIN,
                             advance_delay_max=config.LIFE_ADVANCE_DELAY_MAX,
                             lock_source=self.store,
                             context_source=lambda question="": self.continuity.render(
                                 "", now=time.time()))
        self._life_advance_lock = threading.Lock()
        # 主动说话（`#108`）：判断"该不该开口" + 生成 + 待发表。
        from app.services.speak import Speaker
        # `B3`：待发表/时间戳的写入同样共用这把锁（与上面 life 的理由相同）。
        self.speaker = Speaker(self.store.db, self.life, session=self.store.session,
                               lock_source=self.store, personality_source=lambda: self.personality,
                               motive_mode=True, motive_limit=config.PROACTIVE_MOTIVE_LIMIT,
                               motive_ttl_days=config.PROACTIVE_MOTIVE_TTL_DAYS)
        self._proactive_lock = threading.Lock()
        # 关系层挂到主动说话那条路上（`#146`）：她主动开口时也得按当前称呼来。
        self.speaker.relationship = self.relationship
        # 感知对方：情绪轨迹（jev 决策模型，协议见 app/models/emotion.py）
        from app.models.emotion import EmotionTracker
        self.emotion = EmotionTracker(_p("emotion.json"), config.EMOTION_BASE_URL,
                                      config.EMOTION_API_KEY, config.EMOTION_MODEL)
        self.last_turn: dict | None = None          # 上一轮的完整现场，供「她说错了」留证
        # 上一次 `/city detect` 的探测结果（**只存在内存里**，等用户 `/city use` 确认）。
        # 不落盘是刻意的：定位结果没被确认前，不该在磁盘上留下任何痕迹。
        self._detected_city: dict | None = None
        # 附近场所（`#101`）：类别级缓存 + 本轮已经查好的注入块。
        # 只在本轮**确实是"问附近"**时才去查（见 prepare），查完缓存在内存里。
        self._nearby_cache: dict = {}
        self._nearby_block: str = ""
        self._pending_question: str = ""
        self._coords: tuple | None = None      # 本机定位结果的内存缓存（不落盘）
        self._pending_context: dict | None = None   # prepare 与 finish_turn 之间传现场
        self._recorded_ts: float | None = None      # 已留证那一轮的时间戳，防连点重复记
        # 上一次"主动说话跳过"的**闸门身份**（`#108` 的可观测性，2026-09-30 加）：
        # 只在闸门**变化**时记日志/埋点。
        # ⚠ 去重键必须是不带数字的 `gate`，**不能用原因文本**——原因里写着
        # "已过 0.16 → 0.17 小时"，拿文本去重等于每条都算"变了"：实测 30 秒一次的轮询
        # 每轮写一条，会把 `events`（上限 5000 条）和日志一起冲掉。
        self._last_skip_gate: str | None = None
        # 启动时做一次全量对账：分层架构没有单一真相源，各层会慢慢互相矛盾，
        # 必须有个显式检查。只报告不改数据。
        self.memory_issues: list[dict] = []
        self.memory_check_error = ""
        if config.CHECK_ON_START and self.bg_ready:
            try:
                self.memory_issues = self.store.check_consistency()
            except Exception as exc:
                self.memory_check_error = f"记忆体检未完成：{exc}"
                log.error("启动记忆体检未完成（服务继续启动）：%s", exc)
        elif config.CHECK_ON_START and not self.bg_ready:
            self.memory_check_error = "尚未配置模型密钥，记忆体检将在配置后运行。"

    def _city_from_world(self) -> str:
        """世界书里写的城市 → adcode；没有就返回空串（`#109`）。

        城市不是配置项而是「你们共同世界的一部分」：世界书里写了「用户住在成都」，
        天气就该按成都查——否则 `/city` 换过城市、重启又回默认（用户 2026-09-28
        反馈要"知道我在哪"，那就不能让它在重启后失忆）。

        **只认明确写了城市的句子**（含「住在／所在／常驻」之一），不去别的句子里
        瞎找地名——"常去广场晒太阳"里的"广场"不是城市，猜错比不知道更糟。
        """
        try:
            from app.utils import weather as _weather
            for entry in self.world.entries:
                text = entry.get("text", "")
                if not any(k in text for k in ("住在", "所在", "常驻")):
                    continue
                for city in _weather.CITY_ADCODE:
                    if city in text:
                        code = _weather.adcode_for(city)
                        if code:
                            return code
        except Exception as exc:               # 世界书脏数据不该影响启动
            log.warning("从世界书推导城市失败（当作没写）：%s", exc)
        return ""

    # ==================== 一次对话 ====================

    def system_prompt(self, question: str = "") -> str:
        """当前时间 + 性格模板 + 用户画像。三者都是「每一轮都要遵守的前提」。

        时间放在最前面且每轮重新取：不注入的话她不知道「现在」是哪一刻，
        说的"今天"就是猜的，跟用户嘴里的"今天"可能不是同一天。
        """
        prompt = ""
        if config.INJECT_TIME:
            # 时段单独标出来：14:12 就在第一行，她照样说「早上好」（用户 2026-09-28 反馈）。
            # 把"该用哪个问候"变成一条可断言的事实，而不是让她自己换算 24 小时制。
            part = clock.day_part()
            prompt = (f"【当前时间】{clock.now_text()}（现在是{part}）\n"
                      "日期、时间与问候以这行的日期和时段为准；旧记录里的时刻属于当时。"
                      "被问时可以直接回答，不主动报时。\n\n")
        # 【你此刻】紧跟时间：它回答的是"她自己的现在进行时"（`#107`）。
        # 放在最前面几位是因为它跟"现在"同源，而且**只要它在，她就不会每次都从零重编自己**。
        life_line = self.life.render() if getattr(self, "life", None) else ""
        if life_line:
            prompt += life_line + "\n\n"
        try:
            continuity = self.continuity.render(question, now=time.time())
            if continuity:
                prompt += continuity + "\n\n"
        except Exception as exc:
            log.warning("读取话题连续性失败（本轮跳过）：%s", exc)
        # 【你和他的关系】（`#146`）：称呼、阶段、承诺、关心方式。
        # 紧跟在"她自己的此刻"后面——一个说"我是谁/我在干嘛"，一个说"我们到哪一步了"。
        # 它只给**事实与口吻**，不替她写台词；阶段按可数的事实（天数／消息数）推进。
        rel = getattr(self, "relationship", None)
        if rel is not None:
            try:
                prompt += rel.render(time.time(), self._user_msg_count(), scene_text=question) + "\n\n"
            except Exception as exc:            # 关系层坏掉不该让整轮对话崩
                log.warning("读关系层失败（本轮跳过这一段）：%s: %s", type(exc).__name__, exc)
        weather_relevant = any(word in str(question or "") for word in (
            "天气", "下雨", "下雪", "温度", "气温", "几度", "带伞", "雨伞", "出门", "外面冷", "外面热"))
        if self.weather.enabled and weather_relevant:
            wl = self.weather.line()
            if wl:
                prompt += (f"【天气】{wl}\n"
                           "天气记录的日期和数字只适用于所标日期；未提供的天气未知。\n\n")
        if config.AUTO_EMOTION:
            es = self.emotion.summary()
            if es:
                prompt += f"【用户最近的情绪】{es}\n\n"
        text = str(question or "")
        her_state_attrs = {attr for attr in ("位置", "状态", "近况")
                           if any(prefix + attr in text for prefix in (
                               "你的", "你现在的", "你目前的", "你最近的", "你现在", "你目前", "你最近"))}
        if any(phrase in text for phrase in (
                "你在哪", "你在哪里", "你现在在哪", "你现在在哪里", "你目前在哪", "你目前在哪里")):
            her_state_attrs.add("位置")
        slots = [slot for slot in self.store.slots_all(limit=-1)
                 if (slot["entity"] and slot["entity"] in text)
                 or (slot["attr"] and slot["attr"] not in ("状态", "位置", "近况")
                     and slot["attr"] in text)
                 or (slot["entity"] == self._her_name() and slot["attr"] in her_state_attrs)
                 or (slot["entity"] == "用户" and slot["attr"]
                     and any(prefix + slot["attr"] in text for prefix in (
                         "我的", "我现在的", "我目前的", "我最近的", "我现在", "我目前", "我最近")))]
        if slots:
            lines = self.store.render_slots(slots)
            prompt += f"【最近确认的状态】相关实体的记录与确认时间：\n{lines}\n\n"
        prompt += self.personality.render()
        if agency.asks_for_her_choice(question):
            hint = agency.preference_hint(
                question, self.personality.data.get("favorite_places") or [],
                self.life.recent_events(12) if getattr(self, "life", None) else [],
                tastes=(self.life.script().get("tastes", "")
                        if callable(getattr(getattr(self, "life", None), "script", None)) else ""))
            if hint:
                prompt += "\n\n" + hint
        part = self.profile.render()
        if part:
            prompt += "\n\n" + part
        # 附近场所（`#101`）：只在 prepare 判定「这轮是在问附近」且真的查到了才注入。
        # 有真数据时顺带解禁那条反幻觉纪律——**没有数据时它照旧生效**（不许编地点）。
        if self._nearby_block:
            prompt += ("\n\n" + self._nearby_block +
                       "\n（对方问附近有什么时，你可以直接说这些店名和大概距离——"
                       "这是刚查到的真数据，不用再说「我看不到地图」；"
                       "但没查到的那类不要说成有。）")
        return prompt

    # ==================== 性格演化 ====================

    def run_evolve_pending(self) -> None:
        """演化「上次之后新产生的」对话。周期调用 + 退出兜底共用这一入口。

        小步迭代：每次只复盘新增的部分，改动小、可归因、可回滚；
        避免「长期不重启，几天的经验被压缩成一次大改」。
        没有新对话就不空跑模型。
        """
        if not config.AUTO_EVOLVE_PERSONALITY:
            return
        try:
            hist = self.store.sessions.get(self.store.session, [])
            # _evolved_pos 记的是【消息数】（user+assistant 各算一条），切片不要乘 2。
            # 踩过：写成 _evolved_pos*2 会把第 2 轮的新对话整个跳过。
            new_turns = hist[self._evolved_pos:]
            if not new_turns:
                return
            ok, msg = self.evolve_personality(new_turns)
            if not ok:
                # **cursor 不推进**：这批对话留到下个周期检查点重试。
                # 踩过：失败也推进，演化就静默丢了，而日志还印着「已微调」。
                log.warning("性格模板演化失败，稍后重试：%s", msg)
                self.store.event("evolve", ok=False, error=str(msg)[:80])
                return
            self._evolved_pos = len(hist)
            self._evolved_turns = len(hist) // 2
            if msg:
                log.info("性格模板已微调：%s", msg)
            self.store.event("evolve", ok=True, turns=len(new_turns) // 2,
                             note=str(msg or "")[:80])
        except Exception as exc:
            log.error("性格演化跳过：%s: %s", type(exc).__name__, exc)

    def _advance_life(self) -> None:
        """聊天和后台心跳都会推进她的生活；错过的时段最多补几格。

        `T97`：**必须显式取一次"现在"再传进去**。原来这四步各自默认 `dt.datetime.now()`，
        于是"现在"来自**两个时钟**——`time.time()`（测试冻结得住）和 `datetime.now()`
        （`patch("time.time")` 冻不住）。后果是真机时间一跨时段，一个把时间冻在下午的用例
        就会看到"她推进到傍晚、还写了一条未来流水"，把"她刚决定不说"的闸门当成"有新生活"
        放行 → `tests/test_promise_reminders.py` 里 5 条随挂钟红绿。
        修法：**一处取时间，处处用它**；`time.time()` 正是仓库里其余代码用的那个时钟源。
        """
        try:
            with self._life_advance_lock:
                now = dt.datetime.fromtimestamp(time.time())
                if self.life.needs_advance(now):
                    missed = self.life.missed_slots(now)
                    if missed > 1:
                        log.info("她的生活落后了 %d 格（服务没跑期间时间照走），开始补算", missed)
                        self.life.catch_up(client=self.client, model=config.EXTRACT_MODEL, now=now)
                    else:
                        self.life.advance(client=self.client, model=config.EXTRACT_MODEL, now=now)
        except Exception as exc:              # 生活层挂了绝不能影响对话
            log.warning("生活推进异常（跳过这一格）：%s: %s", type(exc).__name__, exc)

    def _observe_user_life(self, text: str) -> None:
        try:
            self.life.observe_user_update(text)
        except Exception as exc:
            log.warning("读取用户对生活的完成确认失败（本轮不影响对话）：%s: %s",
                        type(exc).__name__, exc)

    def observe_life_reply(self, text: str) -> None:
        """读她话里的完成语义 → 刷新她的此刻，**并把状态槽位一起跟上**（`#138`）。

        为什么槽位也要动：状态页看的是 `slots`（`沈知夏·状态`），而它是**提炼器**写的；
        而她**主动**说的话不进提炼（设计边界：她自己的话不当事实反复强化）→ 于是她说
        「汤炖好了…端过来啦」，状态页却还停在 19:23 的「回家炖汤中」（用户 2026-09-30 的截图）。
        这里把刚刷新的 `life_state.activity` 同步一份过去，"她的此刻"两边就不会打架。
        """
        try:
            changed = self.life.observe_assistant_update(text)
        except Exception as exc:
            log.warning("读取她对生活的完成陈述失败（本轮不影响对话）：%s: %s",
                        type(exc).__name__, exc)
            return
        if not changed:
            return
        try:
            activity = str((self.life.state() or {}).get("activity") or "").strip()
            if activity:
                self.store.upsert_slots([{"entity": self.life.her_name,
                                          "attr": "状态", "value": activity[:60]}])
        except Exception as exc:              # 槽位同步失败绝不能影响对话
            log.warning("同步状态槽位失败（不影响对话）：%s: %s", type(exc).__name__, exc)

    def life_board(self, limit: int = 40) -> dict:
        with self.store._lock:
            return self._life_board(limit)

    def _life_board(self, limit: int = 40) -> dict:
        """「她做过什么」看板（`#117`，用户点名要的）：让她的生活**看得见、也查得到出处**。

        为什么值得单开一块（用户原话：「**我需要一个新的看板，看她做了的事情**」＋
        「**我不知道薄荷是哪里来的**」）：她的生活（`life_state`/`life_events`）与
        "她主动说过的话"（`pending_say`）在界面上**一直看不见**。
        「薄荷」的真相是——编剧层（`life.json`）只写「阳台上那几盆花」，
        **具体花名是模型在那一格现编的**，而且那句话**没有落进任何一张表**。

        所以这块的重点不是好看，是**每条都标出处**：
          · `events` 每条带 `source`（`model` = 她自己选的 / `fallback` = 模型不可用时的兜底）
            与 `slot`（哪一格）、时间；
          · `script` 照抄编剧层原文——**这就是"里面没有薄荷"的证据**；
          · `said`（她主动说过的原话）与 `experiences`（记忆里标了她视角的条目）分开列，
            这样"哪句话从哪来"能当场对上账。
        """
        from app.models.life import SLOT_LABEL, slot_of

        def _when(ts) -> str:
            try:
                t = dt.datetime.fromtimestamp(float(ts))
            except (TypeError, ValueError):
                return "—"
            days = (dt.datetime.fromtimestamp(time.time()).date() - t.date()).days
            hm = t.strftime("%H:%M")
            if days == 0:
                return f"今天 {hm}"
            if days == 1:
                return f"昨天 {hm}"
            return f"{t.month}月{t.day}日 {hm}"

        now = time.time()
        running = self._life_advance_lock.locked()
        st = self.life.state() or {}
        slot = str(st.get("slot") or slot_of())
        events = []
        for e in self.life.recent_events(max(1, int(limit))):
            src = str(e.get("source") or "")
            events.append({"ts": e.get("ts"), "when": _when(e.get("ts")),
                           "rid": e.get("rid"),
                           "slot": e.get("slot") or "",
                           "slot_label": SLOT_LABEL.get(str(e.get("slot") or ""), ""),
                           "text": e.get("text") or "", "source": src,
                           "source_label": {"model": "她自己选的",
                                            "fallback": "兜底候选",
                                            "dedup": "她自己选的（重写：不跟上一格重复）",
                                            "catchup_summary": "分段补记摘要",
                                            "conversation": "对话中确认的"
                                            }.get(src, src or "—")})
        reports = [{**s, "when": _when(s.get("ts")),
                    "status": "已读到" if s.get("consumed") else "待投递"}
                   for s in self.speaker.said_recent(limit)]
        experiences = [{"ts": m.get("ts"), "when": _when(m.get("ts")),
                        "text": m.get("text") or ""}
                       for m in self.store.memories
                       if m.get("actor") == "assistant"][-12:]
        experiences.reverse()                      # 越靠上越新
        try:
            script = self.life.script()
        except Exception as exc:                    # 编剧层坏掉不该让整块看板空掉
            log.warning("读她的编剧层失败（照常给别的）：%s", exc)
            script = {"duty": "", "tastes": "", "cares": ""}
        try:
            next_slot = _when(self.life.next_slot_at(now))
        except Exception:
            next_slot = ""
        return {
            "now": dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
            "catchup": {"running": running,
                        "remaining_slots": self.life.missed_slots(dt.datetime.fromtimestamp(now))},
            "state": {"her_name": self.life.her_name, "place": st.get("place") or "",
                      "activity": st.get("activity") or "", "mood": st.get("mood") or "",
                      "on_mind": st.get("on_mind") or "", "slot": slot,
                      "slot_label": SLOT_LABEL.get(slot, ""),
                      "slot_date": st.get("slot_date") or "",
                      "updated_when": _when(st.get("updated_at")),
                      # `#145`：这份"此刻"是**系统推演**的（时间 + 编剧层，可能还有她的自述回写），
                      # 而 `reports`（她亲口报备过的）才是准的。看板必须把这个从属关系写明，
                      # 否则用户又会看到"对话里已经炖好了、状态页还在回家炖汤"这类打架。
                      "advisory": True,
                      "advisory_label": "自动推演 · 只当氛围；跟她说过的话对不上时，以「她最近报备的」为准"},
            "script": script,
            "favorite_places": [str(x) for x in
                                (self.personality.data.get("favorite_places") or [])],
            "cares": [str(x) for x in (self.personality.data.get("cares") or [])],
            "events": events,
            "reports": reports,
            "experiences": experiences,
            "stats": {"events_total": len(self.life.recent_events(999)),
                      "said_today": self.speaker.said_today(now),
                      "daily_max": int(config.PROACTIVE_DAILY_MAX),
                      "next_slot_when": next_slot},
        }

    def delete_life_event(self, rid) -> str:
        """看板隐藏流水并保留历史（`T91`），成功后记录埋点。

        埋点放服务层是因为 `events` 表归 `MemoryStore` 管（`LifeBook` 只碰 `life_*` 两张表），
        这与分层一致；`LifeBook` 只负责隐藏并返回回执。
        """
        with self.store._lock:
            out, info = self.life.delete_event(rid)
            if info:
                self.store.event("life_event_hidden", **info)
        return out

    def pin_life_care(self, text: str) -> str:
        """看板「钉进她的牵挂」（`T91`）：改她的编剧层（`life.json.cares`）＋ 埋点。"""
        out, info = self.life.pin_care(text)
        if info:
            self.store.event("life_care_pinned", **info)
        return out

    def prepare(self, question: str, multi_message: bool = False) -> tuple[list[dict], list[dict]]:
        """组装 messages，顺便把召回结果带出来给界面展示。

        `multi_message=True` 时**额外追加一条分条指令**（`T86`）：她一次说 1~3 条、
        条与条之间用独占一行的 `@@@` 分隔。**默认 False**，因为"这条路径会不会切分"
        决定了能不能给它这个指令——给了却没人切，用户就会看到 `@@@` 字面量：
          · 网页流式（`/api/chat/stream`）→ True，前端 `BubbleSplitter` 切；
          · 非流式（`/api/chat`）→ True，服务端 `split_messages()` 切；
          · **CLI 与 benchmark → False**：前者会把标记原样打到终端，后者更要紧——
            改动 benchmark 的提示词会让**历史基线立刻不可比**（`T40` 的教训）。
        """
        if not getattr(self, "keys_ready", True):
            raise RuntimeError("模型尚未配置：请先在设置页填写主对话和 embedding 的 API Key。")
        self._pending_question = question or ""
        # 她的生活往前走（`#107`）。两种情形：
        #  · 只是跨了一格 → 正常推进；
        #  · **程序关了一段时间**（用户关机/关服务），时间是照走的 → `catch_up()` 把落下的
        #    时间段补出来（最多 4 格）。这样用户隔几天回来，她的生活流水里真有几格，
        #    而不是停在原地假装"刚才还在聊"。
        # `needs_advance()` / `missed_slots()` 自己会拦住"同一格重复推进"。
        # ⚠ 这段逻辑与 `proactive_tick()` 共用（见 `_advance_life`）：**你不说话时她也要过日子**，
        # 否则"有话可说"那道闸门会把自说自话锁死。
        self._advance_life()
        self._observe_user_life(question)
        # 记下"用户刚说过话"——主动说话的闸门 ④ 靠它判断"是不是正在聊"。
        try:
            with self.store._lock:
                self.speaker.touch_user()
        except Exception:
            pass
        # 「附近有什么好玩的」这类问题要先去查**真实地点**（`#101`）。
        # 只在真的这么问时才查：双条件意图判定 + 类别级缓存，见 app/utils/places.py。
        self._nearby_block = ""
        try:
            self._nearby_block = self._nearby_for(question)
        except Exception as exc:              # 查不到就什么都不注入，绝不影响对话
            log.warning("附近场所查询异常（本轮不注入）：%s: %s", type(exc).__name__, exc)
        messages, recalled = self.store.build_messages(self.system_prompt(question), question)
        life_history = self.life.history_for(question, limit=config.LIFE_HISTORY_LIMIT)
        if life_history:
            messages.append({"role": "system", "content": life_history})
        if multi_message:
            # 分条指令放在**最后**（离提问最近），并进 `_pending_context`：留证时要能看见
            # "当时到底给了她什么"，否则事后没法判断是没给指令还是她没照做。
            messages.append({"role": "system", "content": self.MULTI_MESSAGE_HINT})
        # 留一份「现场」，供「她说错了」按钮一键留证。
        # 记的是完整上下文而不是只记问答——诊断时要看的是「当时给了她什么」，
        # 只看问答没法判断问题出在检索、注入还是模型本身。
        self._pending_context = {"messages": messages, "recalled": recalled}
        return messages, recalled

    # ==================== 附近有什么（`#101`） ====================

    def _her_places(self) -> list[str]:
        """她的偏好：人设里的 `favorite_places`（手写 + 演化追加，见方案 2）。"""
        try:
            return [str(x) for x in (self.personality.data.get("favorite_places") or [])]
        except Exception:
            return []

    def _user_prefs(self) -> list[str]:
        """你的偏好：画像里的 preferences / topics（自动提炼的）。"""
        out: list[str] = []
        try:
            data = getattr(self.profile, "data", None) or {}
            out += [str(x) for x in (data.get("preferences") or [])]
            out += [str(x) for x in (data.get("topics") or [])]
        except Exception:
            pass
        return out

    def _place_keywords_split(self) -> tuple[list[str], list[str], list[str]]:
        """(合并后的查询词, 她贡献的, 你贡献的)。

        `T84` 的口径：**她的偏好是主、你的画像是补充**，而且**分得清谁贡献了什么**
        （以前两边平权混在一起，改了一边另一边还会把旧偏好喂回来，也看不出出处）。
        同一类去重但不丢来源——调用方可以据此说清"这条推荐是谁提的"。
        """
        from app.utils import places as _places
        hers = _places.keywords_for(self._her_places())
        his = [k for k in _places.keywords_for(self._user_prefs()) if k not in hers]
        return hers + his, hers, his

    def _place_keywords(self) -> list[str]:
        """查询词：**她的偏好在前、画像是补充**（兼容旧调用点的薄壳）。"""
        return self._place_keywords_split()[0]

    def _nearby_for(self, question: str) -> str:
        """问「附近」时去查一次真实地点，返回要注入的块（查不到就返回空串）。"""
        from app.utils import places as _places
        if not _places.looks_like_nearby_question(question):
            return ""
        if not self.weather.enabled:
            return ""     # 没配 key **或不知道你在哪**：别猜一个城市去搜
        kws, hers, his = self._place_keywords_split()
        if not kws:
            return ""
        # 出处留痕（`T84`）：查了什么、其中哪些是她提的、哪些是你提的——
        # 事后要能回答"这条推荐是谁的主张"。
        log.info("附近查询词 %s（她: %s ｜ 你: %s）", kws, hers or "无", his or "无")
        hits = self._nearby_search(kws)
        if not hits:
            return ""
        return _places.render_block(hits)

    def _nearby_search(self, keywords: list[str]) -> list[dict]:
        """调高德周边搜索（带类别级缓存）。坐标优先用本机定位、否则用城市中心。

        为什么允许用定位：搜索只是一次性请求、结果不落盘；而"附近"这个词
        离开坐标就没有意义。定位失败时退回城市级搜索（高德对城市名也能搜）。
        """
        from app.utils import geo, places as _places
        key = config.AMAP_KEY
        if not key:
            return []
        pos = self._last_known_coords()
        if pos:
            return _places.search(pos[0], pos[1], keywords, key, cache=self._nearby_cache)
        # 没有坐标：用城市名做关键字搜索（`place/text`），拿到的仍是真数据
        return _places.search_city(self.weather.city_name, keywords, key,
                                   cache=self._nearby_cache)

    def _last_known_coords(self):
        """本机定位坐标（**只在需要时探一次**，成功就复用；失败静默回退城市搜索）。"""
        from app.utils import geo
        if getattr(self, "_coords", None):
            return self._coords
        got = geo.detect_windows(timeout=25)
        if got.get("ok") and got.get("lat") is not None:
            self._coords = (got["lat"], got["lon"])
            return self._coords
        log.info("本机定位不可用（%s），附近搜索退回城市级", str(got.get("error"))[:60])
        return None

    def mark_wrong(self, note: str = "") -> str:
        """把上一轮的完整现场追加到 feedback.jsonl，供事后诊断。

        攒这个文件是为了让「发现方向」这件事不依赖回忆：遇到不对劲随手点一下，
        当时问了什么、她答了什么、召回了哪些记忆、注入了哪些块，原样留底。
        攒够一批再一起看，比事后凭印象复述可靠得多。
        """
        if not self.last_turn:
            return "还没有可标记的对话"
        if self.last_turn.get("ts") == self._recorded_ts:
            return "这一轮已经记过了——聊完下一轮再点"
        rec = dict(self.last_turn)
        rec["user_note"] = note.strip()
        # 留证文件必须落在**自己的**数据目录里：原先用 config.data_path()＝根目录，
        # 多用户下所有人的提问/回答/注入块混在同一个文件里（而这条路径正好是
        # `#89` 的倾向提炼要读的那个地方）。默认用户（uid 为空）时 self.data_dir
        # 就等于根目录，所以单用户行为与文件位置**不变**。
        path = os.path.join(self.data_dir, "feedback.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:
            return f"记录失败：{type(exc).__name__}: {exc}"
        self._recorded_ts = rec["ts"]
        n = 0
        try:
            with open(path, encoding="utf-8") as f:
                n = sum(1 for _ in f)
        except Exception:
            n = 0
        log.info("留证 #%d 已写入 feedback.jsonl（备注：%s）", n, note.strip() or "无")

        # 从这里开始是 #89 加的（D31）：**把这条反馈变成一句她以后能照着做的话**。
        # 以前到上面那行就结束了——前端有按钮、后端有接口，却没有任何东西消费它，
        # 每次"她说错了"都石沉大海（#84 的病根）。
        # 为什么**同步**做而不是丢给 worker：这是用户主动点的一次操作，他正在等结果；
        # 而且"提炼成功/失败/证据太弱"必须当场说清楚（D10）。它不在聊天关键路径上。
        outcome = ""
        try:
            res = self.store.learn_opinion(rec.get("question", ""), rec.get("answer", ""),
                                          note=note)
            outcome = "；" + str(res.get("note") or "")
            if res.get("stored"):
                promoted = self._promote_opinions()
                if promoted:
                    outcome += "；" + promoted
        except Exception as exc:
            # 提炼炸了也不能把"反馈已留证"这件事吞掉：先说留证成功，再说倾向失败。
            log.warning("倾向提炼异常（反馈本身已留证）：%s", exc)
            outcome = f"；倾向提炼异常（{type(exc).__name__}: {exc}），原始证据仍在 feedback.jsonl"
        return f"已记下第 {n} 条（{path}）{outcome}"

    def _promote_opinions(self) -> str:
        """把够格的倾向送进人格的「待确认」队列（`D31`：**绝不自动生效**）。

        够格 = ≥2 次独立命中 **且** 至少一条带明确备注（门槛由用户定）。送到
        `record_pending` 的 `blocked` 传空列表：这条不是被护栏拦下的，而是**按门槛**
        需要人来拍板——「待确认」的语义本来就是"还没定的事"。
        """
        done = []
        for cand in self.store.opinion_candidates():
            current = list(self.personality.data.get("behavior", []))
            if len(current) >= self.personality.MAX_ITEMS and cand["text"] not in current:
                # 人设那一栏满了：硬塞会被 _sanitize 丢掉，不如说清楚（失败要可见）。
                done.append(f"「{cand['topic']}」够格了，但人设的 behavior 已满 "
                            f"{self.personality.MAX_ITEMS} 条，先清理再加")
                continue
            self.personality.record_pending(promotion_patch(cand, current), [], cand["why"])
            self.store.mark_opinion_promoted(cand["topic"])
            done.append(f"「{cand['topic']}」已进待确认（用 /personality pending 拍板）")
            log.info("倾向够格进待确认：%s（%s）", cand["topic"], cand["why"])
        return "；".join(done)

    # 关思考的参数族：族名 + 旧供应商名别名（取值见 config.CHAT_NO_THINK）。
    # 一次只发一族——两族同时塞进 extra_body，老服务端可能直接报参数错误。
    _NO_THINK_FAMILIES = {
        "thinking": {"thinking": {"type": "disabled"}},
        "zhipu": {"thinking": {"type": "disabled"}},           # 别名
        "enable_thinking": {"enable_thinking": False},
        "qwen": {"enable_thinking": False},                    # 别名
    }
    # 显式"不关"的名字。为什么不能只用空串：Windows 上 `set WB_CHAT_NO_THINK=` 会把变量
    # 直接删掉，于是"想临时开回思考做 A/B 对照"这件事在 Windows 上根本做不到。
    _NO_THINK_OFF = {"off", "none", "0", "false", "no"}

    @classmethod
    def _no_think_kwargs(cls) -> dict:
        """主对话关思考的额外参数。

        不认识的名字**不再静默不生效**：先打 WARNING，再按"不关"处理。T23 那个坑
        就是名字写错（enable_thinking 族发给只认 thinking 族的模型），配了等于没配，
        却一路没有任何提示。
        """
        kind = (config.CHAT_NO_THINK or "").strip().lower()
        if not kind or kind in cls._NO_THINK_OFF:
            return {}
        body = cls._NO_THINK_FAMILIES.get(kind)
        if body is None:
            log.warning("CHAT_NO_THINK=%r 不是已知写法，本次**不关思考**（已知：%s；不关：%s）",
                        config.CHAT_NO_THINK,
                        "、".join(sorted(cls._NO_THINK_FAMILIES)),
                        "、".join(sorted(cls._NO_THINK_OFF)))
            return {}
        return {"extra_body": body}

    def ask(self, messages: list[dict]) -> str:
        """非流式：等全部生成完一次性返回，出错时把服务端原始响应打出来好排查。"""
        if not getattr(self, "chat_ready", True):
            raise RuntimeError("主对话尚未配置：请先在设置页填写 API Key。")
        raw = self.chat_client.chat.completions.with_raw_response.create(
            model=config.MODEL, messages=messages, temperature=0.7,
            max_tokens=config.MAX_TOKENS, **self._no_think_kwargs(),
        )
        response = raw.parse()
        if not response.choices:
            log.error("响应里没有 choices 字段，服务端原始返回：\n%s", raw.text[:1200])
            return ""
        return response.choices[0].message.content or ""

    def _note_cleaned(self, removed) -> None:
        """回答清洗的留痕（`T83` 第三条约束：**必须留痕**——静默改模型输出会掩盖行为问题）。

        ⚠ 抽成方法不只是为了两条路复用：原来流式那条把它写成 `stream()` 里的一个 lambda，
        而 `store.event()` 的第一个形参**就叫 `kind`** —— `event("answer_cleaned", kind="…")`
        于是**每次都抛 `TypeError`**，又被 `PrefixStripper._report()` 的 try/except 吞掉，
        结果是**留痕等于没有**（只在真发生剥离时才触发，所以一直没人发现）。
        """
        self.store.event("answer_cleaned", reason="time_prefix", text=str(removed)[:40])

    def _user_msg_count(self) -> int:
        """他说过的话有多少条（关系层用它推进阶段，`#146`）。

        取 `messages` 表里 `role='user'` 的总数——**不是**内存里的滑动窗口，
        那样重启就会把"聊了多久"清零。失败时返回 0（宁可当刚认识，也不要瞎报）。
        """
        try:
            row = self.store.db.execute(
                "SELECT count(*) FROM messages WHERE role='user'").fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            log.warning("数用户消息失败（关系阶段按 0 算）：%s: %s", type(exc).__name__, exc)
            return 0

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """流式：模型吐一个字就 yield 一个字。

        出口加一道**前缀清洗**（`T83`）：她常常以「（今天晚上 21:26）」开头——
        那是给她的内部时间标记，不是该说给对方听的话。提示词两轮都压不住
        （实测 21:27 / 22:33 照旧出现），所以在输出侧兜一道，**只剥开头**、
        句中与正常的时间表达一个字都不动，并且每次都留痕（见 app/utils/answers.py）。
        """
        if not getattr(self, "chat_ready", True):
            raise RuntimeError("主对话尚未配置：请先在设置页填写 API Key。")
        resp = self.chat_client.chat.completions.create(
            model=config.MODEL, messages=messages, temperature=0.7,
            max_tokens=config.MAX_TOKENS, stream=True,
            stream_options={"include_usage": True}, **self._no_think_kwargs(),  # 部分供应商会忽略；官方 OpenAI 可用
        )
        stripper = answers.PrefixStripper(on_strip=self._note_cleaned)
        for chunk in resp:
            if not chunk.choices:  # 收尾时可能只有 usage、没有 choices
                continue
            text = getattr(chunk.choices[0].delta, "content", None) or ""
            if text:
                for piece in stripper.feed(text):
                    if piece:
                        yield piece
        for piece in stripper.flush():
            if piece:
                yield piece

    def speak_stream(self, question: str, answer: str):
        """流式合成：逐块 yield 音频（T37）。失败静默，文字对话不受任何影响。

        语气跟随 jev 情绪轨迹里最近一次记录的 mood（1 小时内才算数）——
        用户难过时她的声音会轻下来，开心时上扬。
        埋点在**流结束时**补记（块数/首字节/总耗时），这样它也能被 `/mem events` 看到。
        """
        if not config.TTS_ENABLED:
            return
        mood = None
        try:
            if self.emotion.enabled and self.emotion.entries:
                last = self.emotion.entries[-1]
                if time.time() - last["ts"] < 3600:
                    mood = last.get("mood")
        except Exception:
            pass
        st: dict = {}
        chunks = 0
        try:
            for chunk in tts.speak_stream(answer, mood, stats=st):
                chunks += 1
                yield chunk
        finally:
            # 客户端中途断开也会走到这里：账照样记，不然"没声音"就查无实据
            self.store.event(
                "tts", ok=bool(chunks), chunks=chunks, chars=len(answer),
                mood=mood or "默认",
                first=None if st.get("first_byte") is None else round(st["first_byte"], 2),
                total=None if st.get("total") is None else round(st["total"], 2),
                billed=st.get("billed"),        # 服务端计费的字符数（含 SSML 标签）
                fallback=st.get("fallback"), timeout=st.get("timeout"))

    def speak(self, question: str, answer: str) -> bytes | None:
        """非流式版本（整段收完再返回）。保留给不用流式的调用方。"""
        return b"".join(self.speak_stream(question, answer)) or None

    def stage_delivered_turn(self, question: str, answer: str) -> int:
        with self.store._lock:
            self._delivery_sequence += 1
            self._delivered_turns.append({
                "id": self._delivery_sequence, "session": self.store.session,
                "history": self.store.sessions.setdefault(self.store.session, []),
                "messages": [
                    {"role": "user", "content": question, "ts": time.time()},
                    {"role": "assistant", "content": answer, "ts": time.time()},
                ],
            })
            self._delivered_turns = self._delivered_turns[-4:]
            return self._delivery_sequence

    def discard_delivered_turn(self, delivery_id: int) -> None:
        with self.store._lock:
            self._delivered_turns = [turn for turn in self._delivered_turns
                                     if turn["id"] != delivery_id]

    def finish_turn(self, question: str, answer: str, *, delivery_id: int | None = None) -> dict:
        """一轮说完了：写记忆、落盘、看要不要刷新画像。"""
        if delivery_id is not None:
            with self.store._lock:
                if delivery_id <= self._cleared_delivery_sequence:
                    return {"memory_note": "", "profile_note": ""}
                for turn in self._delivered_turns:
                    if turn["id"] == delivery_id:
                        start = len(turn["history"])
                        synced = self.store._synced.get(turn["session"], 0)
                        while start > synced and turn["history"][start - 1].get("greeting"):
                            start -= 1
                        turn["start_seq"] = start
        note = self.store.add_turn(question, answer)
        self.store.save()
        if delivery_id is not None:
            self.discard_delivered_turn(delivery_id)
        continuity_updates = list(getattr(self.store, "_continuity_updates", []) or [])
        source = getattr(self.store, "_continuity_source", None)
        if continuity_updates and source:
            try:
                accepted = self.continuity.apply(
                    continuity_updates, session=source["session"],
                    user_seq=source["user_seq"], assistant_seq=source["assistant_seq"],
                    user_text=source["user_text"], assistant_text=source["assistant_text"],
                    source_ts=source.get("source_ts"),
                    now=time.time())
                if accepted:
                    self.store.event("continuity", applied=len(accepted))
            except Exception as exc:
                log.warning("话题连续性写入失败（本轮不影响对话）：%s", exc)
        self.store._continuity_updates = []
        self.observe_life_reply(answer)
        if agency.asks_for_her_choice(question):
            cues = agency.yield_cues(question, answer)
            self.store.event("agency_yield_candidate" if cues else "agency_response",
                             cues=cues, question=question[:80], answer=answer[:120])
        # 正常流程也要有 INFO 日志，不然文件里只有失败——排障时看不出「之前发生了什么」
        log.info("对话一轮：问「%s」｜%s｜回复 %d 字",
                 question[:24], note or "无新记忆", len(answer))

        # 后台感知：天气过期就刷一次；情绪判一次。都在回答之后跑，不影响首字。
        if self.weather.enabled:
            try:
                self.weather.refresh()
            except Exception:
                pass
        if config.AUTO_EMOTION and int(config.EMOTION_EVERY or 0) > 0:
            turn_no = len(self.store.sessions.get(self.store.session, [])) // 2
            if turn_no % int(config.EMOTION_EVERY) == 0:
                try:
                    hist = self.store.sessions.get(self.store.session, [])
                    prev = ""
                    if len(hist) >= 4:
                        prev = (f"用户此前说：「{str(hist[-4].get('content', ''))[:90]}」，"
                                f"助手回了：「{str(hist[-3].get('content', ''))[:90]}」")
                    self.emotion.record(question, prev)
                except Exception as exc:
                    log.warning("情绪记录跳过：%s: %s", type(exc).__name__, exc)

        # 这一轮的完整现场，等「她说错了」按钮来取
        ctx = getattr(self, "_pending_context", None) or {}
        self.last_turn = {
            "ts": time.time(),
            "session": self.store.session,
            "model": config.MODEL,
            "question": question,
            "answer": answer,
            "memory_note": note,
            "recalled": [{"kind": h["kind"], "sim": round(h["sim"], 3), "text": h["text"]}
                         for h in ctx.get("recalled", [])],
            "injected": [m["content"] for m in ctx.get("messages", [])
                         if m.get("role") == "system"],
        }

        # 性格演化：小步迭代。每 EVOLVE_EVERY 轮复盘一次「新增的对话」，
        # 退出钩子兜底收尾（见 main.py 的 lifespan）。
        if config.AUTO_EVOLVE_PERSONALITY and int(config.EVOLVE_EVERY or 0) > 0:
            n = len(self.store.sessions.get(self.store.session, [])) // 2
            if n - self._evolved_turns >= int(config.EVOLVE_EVERY):
                self.run_evolve_pending()

        out = {"memory_note": note, "profile_note": ""}
        self._turns_since_profile += 1
        if self._turns_since_profile >= config.PROFILE_EVERY:
            self._turns_since_profile = 0
            turns = [message for message in self.store.sessions.get(self.store.session, [])
                     if message.get("role") == "user" and message.get("content")]
            turns = turns[-config.PROFILE_EVERY:]
            out["profile_note"] = self.profile.analyze(turns)
            self.store.event("profile", note=str(out["profile_note"] or "")[:80])
        return out

    # 让她一次说 1~3 条（`T86`）。
    # ⚠ 这段**必须与"剥标记"的接线同时存在**：`@@@` 靠 `split_messages()`（服务端）与
    # `BubbleSplitter`（前端）切掉——**两条路都得有**，否则用户会在页面上直接看到 `@@@` 字面量。
    # `T86` 全链路（`#114`）之后，**流式与非流式都在注入它**：
    #   · 非流式 `/api/chat`：`chat()` 里 `prepare(..., multi_message=True)`；
    #   · 流式 `/api/chat/stream`：`chat_stream` 同样 `multi_message=True`，前端按标记分气泡。
    # （旧注释写的是"只挂非流式、绝不进 `system_prompt()`"——那是 `#114` **之前**的实况，
    #  留着会误导后人去"修"一条已经接好的线。）
    # 为什么做成类属性而不是模块常量：与 `_NO_THINK_FAMILIES` 同款，紧挨调用点，
    # 600 行之外没人会去读的模块级提示词更容易被改坏。
    MULTI_MESSAGE_HINT = (
        "【这一次的说话方式】你可以在一次回复里分说 **1~3 条**短消息，"
        "像熟人连着发来几条那样。\n"
        "分条的写法只有一个：在两条之间**单独占一行**写三个 `@`，也就是 `@@@`。\n"
        "必须是**三个** `@`、必须在**行首**——两个不算、别的符号也不算，"
        "那样写这一行会**原样发到对方眼前**（他看得见你的分隔符）。\n"
        "口语化；**只有确实有几句要分开说的时候才分**——"
        "一句话能说完就正常说，一个标记都不要加；正文里不要出现 `@`。"
    )

    def chat(self, question: str) -> dict:
        """非流式一口气的完整一轮，给 `/api/chat` 用。

        与流式那条路的区别**只有一处**（`T86` 第 2 步）：这里多给一条分条指令，
        拿到完整回复后用 `split_messages()` 切成 1~3 条。
        两条纪律：
        · **标记只在这条路径内部存在**：返回的 `reply` 与写进记忆/滑窗的文本都是
          **去过标记的干净文本**，所以任何消费方（CLI、前端、评测）都看不到 `@@@`——
          即使没人接线也不会外泄。
        · **记忆与滑窗仍按一轮处理**（`finish_turn(question, 干净全文)`）：多条不是多轮，
          否则提炼会按条重复记。
        """
        messages, recalled = self.prepare(question, multi_message=True)
        raw = self.ask(messages)
        if not str(raw or "").strip():
            # 空回复先**重试一次**（`T93`：模型偶发空输出，多半一次就好），
            # 重试也空才算失败——那一步在下面抛 `EmptyReply`，**不写记忆、不留空轮次**。
            log.warning("模型返回了空回复，重试一次（question=%r）", question[:40])
            raw = self.ask(messages)
        if not str(raw or "").strip():
            self.store.event("answer_empty", question=question[:60], retried=True)
            raise EmptyReply("模型没有生成任何内容")
        # 输出侧兜一道（`T83` / `#102`）：**非流式这条路原来漏了清洗**，于是
        # 「（今天下午 13:54）」这种内部时间标记会原样进气泡（真机实测抓到 8 条里 2 条）。
        # 流式那条在 `stream()` 里剥；这里补同款，纪律也一样：**只剥开头**、**必须留痕**。
        cleaned_text, did_strip = answers.strip_time_prefix(raw)
        if did_strip:
            answers.note_strip("chat", raw[:40])
            self._note_cleaned(raw)
        # `B6`（工单二）：**清洗之后要再判一次空**。整条回复只有内部时间标记时，上面那道
        # 「raw 是不是空」的检查拦不住它：洗成空串之后若照常 `finish_turn`，`add_turn` 会
        # 无条件追加一条**空 assistant 轮**（落库、留空轮次，还会被摘要／提炼当成真实的一轮）。
        # 这里按与"模型什么都没生成"完全相同的语义处理：留痕 + 抛 `EmptyReply`，不写记忆。
        if not str(cleaned_text or "").strip():
            log.warning("回复清洗后为空（只回了内部时间标记）→ 按空回复处理：raw=%r", raw[:40])
            self.store.event("answer_empty", question=question[:60], stripped_empty=True)
            raise EmptyReply("回复清洗后为空（只回了内部时间标记）")
        parts = split_messages(cleaned_text)
        clean = "\n".join(parts) if parts else (cleaned_text or "")
        out = self.finish_turn(question, clean)
        out.update({"reply": clean, "messages": parts, "recalled": recalled})
        return out

    def greeting(self, force: bool = False) -> str:
        """开场白：从记忆里长出来的一句话。

        force=True 就重新生成——网页每打开/刷新一次算一次新的照面，
        应该按【此刻最新】的记忆打招呼，不然刚说完「猫找到了」刷新还问猫找着没。
        force=False 只取缓存：/api/state 在每轮对话后都会被前端拉一次，
        每次都重新生成的话，同一句话要反复付钱。
        """
        if not config.GREETING:
            return ""
        # 有待发表的话就别开场白了（`#139`）：她**马上就要主动开口**，而开场白是每次打开页面
        # 重新生成的——两句话会在几秒内先后出现，讲同一件事的两个版本（用户实测：开场白说
        # 「汤炖好了…端过来啦」，紧接着主动消息又把 19:00 那件旧事重念一遍）。
        # 前端对**空开场白**就是不发那条气泡（`greeting.root.remove()`），所以返回 "" 是干净的。
        try:
            pending = self.speaker.pending_items()
        except Exception:
            pending = []
        if pending:
            log.info("她还有 %d 条话没投递，这次打开不打开场白", len(pending))
            self._greeting = ""
            return ""
        if force or self._greeting is None:
            # 先把上一轮的清掉：开场白每次都会重新生成，不清就会堆满滑动窗口
            with self.store._lock:
                hist = self.store.sessions.setdefault(self.store.session, [])
                synced = self.store._synced.get(self.store.session, 0)
                while len(hist) > synced and hist[-1].get("greeting"):
                    hist.pop()
            self._greeting = self._make_greeting()
            if self._greeting:
                log.info("开场白：%s", self._greeting)
                # 写进会话历史：对方接着这句话往下聊时，她得知道自己刚才说了什么
                with self.store._lock:
                    hist = self.store.sessions.setdefault(self.store.session, [])
                    hist.append({"role": "assistant", "content": self._greeting,
                                 "greeting": True, "ts": time.time()})
        return self._greeting

    def _make_greeting(self) -> str:
        canned = self.personality.greet()
        if not config.GREETING_USE_MEMORY or not getattr(self, "bg_ready", True):
            return canned

        now = time.time()
        with self.store._lock:
            current_history = self.store.sessions.get(self.store.session, [])
            hist = list(current_history)
            for turn in self._delivered_turns:
                if turn["session"] != self.store.session or turn["history"] is not current_history:
                    continue
                start = turn.get("start_seq")
                recorded = start is not None and any(
                    all(hist[index + offset].get("role") == message["role"]
                        and hist[index + offset].get("content") == message["content"]
                        for offset, message in enumerate(turn["messages"]))
                    for index in range(start, len(current_history) - 1))
                if not recorded:
                    hist.extend(turn["messages"])
            life_state = self.life.state()
            recent_events = self.life.recent_events(4)
        world = self.store.world_texts()[:6]

        # 约定单独挑，而且放在最显眼的位置。
        # 不能指望「最近 8 条记忆」把约定带进来——那是按记录顺序取的，
        # 而约定是两三天前记下的，早被后来的闲聊挤出去了。
        plans_list = self.store.upcoming(within_days=7)
        plans = "\n".join(
            f"- {'就是今天' if d == 0 else ('明天' if d == 1 else f'还有 {d} 天')}：{m['text']}"
            for d, m in plans_list[:5]) or "（没有约好的事）"
        planned = {m["text"] for _, m in plans_list}

        # 时刻级标签：只到日期级时，「早上 10 点的约」晚上还标「就是今天」，
        # 开场白就在晚上 8 点催早上的约（踩过）。已过就明说，别让模型再猜。
        def _plan_label(days: int, m: dict) -> str:
            if days != 0:
                return "明天" if days == 1 else f"还有 {days} 天"
            ddt = clock.find_datetime(m["text"])
            if ddt is not None and ddt <= dt.datetime.fromtimestamp(time.time()) \
                    and clock.PLAN_HINT.search(m["text"]):
                return "就是今天（约定时刻已过，别再邀约）"
            return "就是今天"

        plans = "\n".join(
            f"- {_plan_label(d, m)}：{m['text']}"
            for d, m in plans_list[:5]) or "（没有约好的事）"

        facts = [m["text"] for m in self.store.memories
                 if m.get("kind") not in (KIND_WORLD, KIND_CONSTRAINT)
                 and m["text"] not in planned][-8:]
        if not hist and not world and not facts and not plans_list:
            return canned  # 全新的用户，没什么可回忆的，用性格模板里备好的开场白

        # 说话间隙从「最后一条用户消息」算，不从最后一条消息算——
        # 因为上一轮的开场白本身也会留在历史里，会把它算成"刚刚聊过"。
        gap = ""
        for m in reversed(hist):
            if m.get("role") == "user" and m.get("ts"):
                hours = (time.time() - m["ts"]) / 3600
                if hours < 1:
                    gap = "你们几分钟前刚聊过。"
                elif hours < 24:
                    gap = f"你们大约 {max(1, round(hours))} 小时前聊过。"
                else:
                    gap = f"你们上次聊天是 {max(1, round(hours / 24))} 天前。"
                break

        # 上次的开场白不放进 tail，不然新开场白容易把旧的换句话重复一遍
        real = [m for m in hist if m.get("content") and not m.get("greeting")]
        tail = "\n".join(
            f"{'对方' if m['role'] == 'user' else '你'}"
            f"（{dt.datetime.fromtimestamp(float(m.get('ts') or now)):%m-%d %H:%M}）："
            f"{str(m.get('content', ''))}"
            for m in real[-4:])
        now_state = "；".join(str(life_state.get(key) or "")
                              for key in ("place", "activity", "mood", "on_mind"))
        if life_state.get("updated_at"):
            updated = dt.datetime.fromtimestamp(float(life_state["updated_at"]))
            now_state = f"记录时间 {updated:%m-%d %H:%M}：{now_state}"
        continuity = self.continuity.render("\n".join([
            now_state, *(str(message["content"]) for message in real[-4:]),
            *(str(event["text"]) for event in recent_events),
        ]), now=now)

        text = chat_text(
            self.client, config.GREETING_MODEL,
            [{"role": "user", "content": GREETING_PROMPT.format(
                name=self.personality.display_name(),
                identity=self.personality.data.get("identity", ""),
                now=clock.now_text(dt.datetime.fromtimestamp(now)),
                gap=gap,
                plans=plans,
                world="\n".join(f"- {t}" for t in world) if world else "（没什么特别的）",
                facts="\n".join(f"- {t}" for t in facts) if facts else "（还不了解对方）",
                tail=tail or "（这是你们第一次说话）",
                continuity=continuity or "（没有相关话题记录）",
                # `#139`：把"她此刻在做什么"和最近流水喂进去——**不加这两块，开场白就只能编**，
                # 于是出现过"开场白说汤炖好了、状态层说在菜场挑排骨"的三处不一致。
                now_state=now_state if life_state else "（没有记录）",
                recent="\n".join(f"- {e['text']}" for e in recent_events) or "（还没有）",
                said="\n".join(f"- {t}" for t in self.speaker._recent_said(3)) or "（还没主动说过）",
                relation=self.relationship.render(time.time(), self._user_msg_count()),
            )}],
            temperature=0.8, max_tokens=400, no_think=True,
        )
        text = text.strip().strip('"\u201c\u201d').splitlines()[0].strip() if text else ""
        return text[:80] or canned

    # ==================== 命令 ====================

    def is_command(self, text: str) -> bool:
        low = text.strip().lower()
        return any(low == c or low.startswith(c + " ") for c in COMMANDS)

    def run_command(self, text: str, *, user_name: str | None = None) -> str:
        """执行 /xxx 命令，返回要显示给用户的文本。"""
        raw = text.strip()
        head = raw.split(" ", 1)[0].lower()  # 只把命令本身转小写，参数保持原样
        arg = raw[len(head):].strip()

        if head == "/mem":
            if arg in ("dead", "无用"):
                return self.dead_report()
            if arg.startswith("del "):
                return self.delete_dead(arg[4:].strip())
            if arg in ("check", "对账"):
                return self.reconcile_report()
            if arg in ("events", "事件"):
                return self.store.events_report()
            if arg in ("opinions", "倾向"):
                # 她到底从你的反馈里学到了什么（D31）——没有出口的机制等于没有机制。
                return self.store.opinions_report()
            if arg.startswith(("opinion drop ", "opinions drop ", "倾向 drop ", "倾向 停用 ")):
                # 停用一条学歪的倾向（`T94`）：倾向是**每轮注入**的，所以这必须是常规操作，
                # 而不是"只能去改数据库"。
                return self.store.drop_opinion(arg.split()[-1])
            return self.store.stats()
        if head == "/clear":
            with self.store._lock:
                self._cleared_delivery_sequence = self._delivery_sequence
                self._delivered_turns.clear()
                self.store.clear_session()
                self.store.save()
            return "当前会话已清空，长期记忆和世界书保留"
        if head in ("/city", "/城市"):
            low = arg.strip().lower()
            if low in ("detect", "探测", "定位"):
                return self.detect_city()
            if low in ("use", "确认", "采用"):
                return self.use_detected_city()
            return self.city_report() if not arg else self.set_city(arg)
        if head == "/forget":
            out = self.store.forget_all()
            self.store.save()
            return out
        if head in ("/timeline", "/时间轴"):
            return self.timeline_report(user_name=user_name)
        if head in ("/fix", "/修正"):
            # 事实类反馈的出口（T81）：你点了「她说错了」而她确实记错了事实时，
            # 那条反馈会落到这里，由你决定要不要作废那条记忆。
            arg = arg.strip()
            if arg in ("", "list", "列表"):
                return self.store.corrections_report()
            if arg in ("all", "全部"):
                return self.store.corrections_all()
            for verb, ok in (("accept", True), ("采纳", True), ("drop", False), ("忽略", False)):
                if arg.startswith(verb + " "):
                    try:
                        idx = int(arg[len(verb):].strip())
                    except ValueError:
                        return "用法：/fix accept 1 或 /fix drop 1（序号见 /fix）"
                    out = self.store.resolve_correction(idx, accept=ok)
                    self.store.save()
                    return out
            return ("用法：`/fix` 看列表｜`/fix accept N` 采纳（作废那条记忆）｜"
                    "`/fix drop N` 忽略｜`/fix all` 看全部（含已处理）")
        if head in ("/slots", "/状态"):
            rows = self.store.slots_all()
            if not rows:
                return ("还没有状态槽位——对话里出现「XX 在哪 / 怎么样了」"
                        "这类状态变化时会自动记录")
            display_rows = [{**row,
                             "entity": self._display_text(row["entity"], user_name),
                             "value": self._display_text(row["value"], user_name)}
                            for row in rows]
            return self.store.render_slots(display_rows)
        if head == "/profile":
            return self.profile.analyze(self.store.sessions.get(self.store.session, []))
        if head == "/personality":
            if arg == "reset":
                return self.personality.rollback()
            if arg in ("pending", "待确认"):
                items = self.personality.pending_list()
                if not items:
                    return "没有待确认的改动"
                lines = []
                for i, it in enumerate(items, 1):
                    blocked = "；".join(it.get("blocked") or []) or "（未记明细）"
                    lines.append(f"{i}. 被拦下的条目：{blocked}\n   原因：{it.get('why') or '—'}")
                return ("待确认的演化改动（拦下 ≠ 丢弃：那条改动可能正是你想要的）：\n"
                        + "\n".join(lines)
                        + "\n用 /personality accept 序号 采纳、/personality drop 序号 丢弃")
            if arg.startswith("accept ") or arg.startswith("drop "):
                verb, _, num = arg.partition(" ")
                if not num.strip().isdigit():
                    return "用法：/personality accept 序号　/personality drop 序号"
                return self.personality.resolve_pending(int(num.strip()) - 1,
                                                        accept=verb == "accept")
            if arg in ("evolve", "迭代"):
                ok, msg = self.evolve_personality()
                return msg or ("这次没有需要改的地方" if ok else "迭代失败")
            if arg:
                return self.personality.manual(arg)
            return self.personality.render()
        if head == "/relationship":
            # `#146` 关系层：看/调称呼与阶段、记承诺。慢变层，手写为主，所以命令要能改。
            if arg.startswith("stage "):
                return self.relationship.set_stage(arg[6:])
            if arg in ("stage", "auto"):
                return self.relationship.set_stage("")
            if arg.startswith("promise "):
                return self.relationship.add_promise(arg[8:])
            if arg.startswith("done "):
                return self.relationship.complete_promise(arg[5:])
            if arg.startswith("miss "):
                parts = arg[5:].strip().split(None, 1)
                if len(parts) < 2:
                    return "用法：/relationship miss <序号> <原因>"
                return self.relationship.miss_promise(parts[0], parts[1])
            return self.relationship.report(time.time(), self._user_msg_count())
        if head == "/world":
            if arg in ("tidy", "整理"):
                return self.world.tidy()
            if arg.startswith("del "):
                rest = arg[4:].strip()
                return (self.world.remove(int(rest) - 1) if rest.isdigit()
                        else "用法：/world del 序号")
            if arg == "reload":
                # 必须读**自己**那份（T70-①）：`config.WORLD_FILE` 是根目录的全局路径，
                # 多用户下会把默认用户的世界书灌进当前用户的 store，还顺手把
                # on_world/on_drop 回调重绑到另一个 store 上（串号）。
                path = os.path.join(self.data_dir, "world.json")
                self.world = WorldBook(self.store, path=path)
                note = self.world.sync()
                return "已从 world.json 重新载入" + (f"，{note}" if note else "")
            if arg:
                return self.world.add(arg)
            return self.world.render()
        if head == "/user":
            if arg:
                return self.profile.manual(arg)
            return self.profile.render() or "（画像还是空的，多聊几句或 /profile 触发分析）"
        return f"不认识的命令：{head}"

    # ==================== 事件时间轴 ====================

    def _her_name(self) -> str:
        """她的名字（人设里的 `name`）。用于把历史文本里的「助手」换掉。"""
        try:
            return str(self.personality.display_name() or "她")
        except Exception:
            return "她"

    def _as_her_words(self, text: str) -> str:
        """把记忆文本里的旁白称呼换成她的名字（**只改显示，不动数据**）。

        为什么要有这个"补丁"：旧提示词用「用户：/助手：」作为对话标签，提炼器照抄，
        库里于是有「助手不知道9月24日的天气…」这种条目——视角标着她、主语却是"助手"。
        源头已在 `EXTRACT_PROMPT` 里堵住（称呼硬规则），但**存量数据改不了**（那是历史），
        所以展示层做一次映射。**不改库、不影响召回**：这是"怎么念给她听"的问题，不是数据问题。
        """
        return self._display_text(text, "用户")

    def _display_text(self, text: str, user_name: str | None = None) -> str:
        """Map role labels in display text without changing stored text.

        Replacement is performed in one pass so a nickname containing a role
        word (for example ``小用户AI``) is never processed a second time.
        Common compound terms beginning with ``用户`` remain ordinary nouns.
        """
        import re

        her_name = self._her_name()
        their_name = (user_name or self.user_id or "你").strip() or "你"
        protected_suffixes = (
            "体验", "名", "手册", "数据", "服务", "中心", "系统", "侧", "端",
            "量", "画像", "账号", "库", "需求", "输入", "输出", "消息", "信息",
            "模式", "群", "列表", "字段", "身份", "权限", "目录", "空间",
            "行为", "状态", "操作", "反馈", "提示",
        )
        suffix = "|".join(map(re.escape, protected_suffixes))
        names = "|".join(map(re.escape, sorted({her_name, their_name}, key=len, reverse=True)))
        pattern = re.compile(
            rf"(?P<name>{names})|(?P<her>助手|AI|机器人)|"
            rf"(?P<user>(?<!多)用户(?!{suffix}))"
        )

        def replace(match: re.Match[str]) -> str:
            if match.group("name"):
                return match.group("name")
            return her_name if match.group("her") else their_name

        return pattern.sub(replace, str(text or ""))

    def timeline_report(self, limit: int = 40, user_name: str | None = None) -> str:
        """按时间倒序列出所有带日期的记忆。

        时间轴不是新的一层，只是 event_date 字段的投影；先做可见性
        （人看得见），要不要再注入上下文，等真实使用说话。
        """
        rows = self.store.timeline(limit)
        if not rows:
            return ("时间轴还是空的——带日期的事（约定、搬家、走丢又找到这类）"
                    "才会进这里")
        lines: list[str] = []
        cur = ""
        for r in rows:
            ym = r["date"][:7]
            if ym != cur:
                cur = ym
                lines.append(f"【{ym}】")
            who = "〔她〕" if r.get("actor") == "assistant" else ""
            mark = "" if r["active"] else "（已归档）"
            if r.get("grouped_under"):
                # 同一次互动的复述：缩进跟随上一行，不重复占位
                lines.append(f"      └ {self._display_text(r['text'], user_name)[:42]}")
                continue
            size = r.get("group_size")
            extra = f"（这次共 {size} 条）" if size and size > 1 else ""
            who = "〔她〕" if r.get("actor") == "assistant" else ""
            lines.append(f"  {r['date'][5:]}  {who}{self._display_text(r['text'], user_name)[:46]}"
                         f"{mark}{extra}")
        return "\n".join(lines)

    # ==================== 城市（她该知道你在哪） ====================

    def city_report(self) -> str:
        """`/city` —— 她现在是按哪座城市查天气的，怎么改。"""
        if self.weather.city and self.weather.enabled:
            name, state = self.weather.city_name, "正在按它查天气"
        elif not self.weather.city:
            # 城市**没有兜底默认值**了（`#109`）：说不出来就说"不知道"，
            # 别在这儿编一个"默认杭州"——她真的会按那个说话。
            name, state = "（还不知道）", "所以天气与附近推荐都是关的"
        else:
            name, state = self.weather.city_name, "但没配高德 key，天气功能是关的"
        extra = ""
        if self._detected_city:
            extra = (f"\n\n⚠ 上一次探测到的是 **{self._detected_city['city']}**"
                     f"（{self._detected_city['source']}），还没应用——"
                     f"确认就发 `/city use`。")
        hint = ("直接告诉我一声就行：`/city 成都`（或 `/city 440300` 给高德 adcode）。"
                if not self.weather.city else
                "手动换：`/city 成都`（或 `/city 330100` 直接给高德 adcode）。")
        return (f"她现在的城市：**{name}**（{state}）。\n"
                f"{hint}\n"
                f"自动探测：`/city detect` —— 用**本机定位**（优先 Windows 定位服务，失败回退公网 IP），"
                f"结果先给你看、你确认后才写进世界书。\n"
                f"两条说明：不做后台轮询（只在你要的时候探一次）；"
                f"给第三方做逆地理的坐标会**打码到 0.1 度**（≈10km，判城市够、定不到住址）。"
                + extra)

    def detect_city(self) -> str:
        """`/city detect` —— 探一次位置，**只报告、不写库**（用户确认后才落）。

        顺序是刻意的：**Windows 定位优先、公网 IP 兜底**。实测（2026-09-28 这台机器）
        系统定位给成都（精度 212 米），而公网 IP 给的是台中——IP 走宽带出口，
        换城市也会骗人，所以它只能当备胎，且必须把来源说清楚让人自己判断。
        """
        from app.utils import geo, weather as _weather
        lines: list[str] = []
        picked: dict | None = None
        win = geo.detect_windows()
        if win.get("ok"):
            rev = _weather.reverse_geocode(win["lat"], win["lon"], config.AMAP_KEY)
            acc = win.get("acc")
            lines.append(f"· **Windows 定位**：拿到坐标"
                         f"{'（精度约 %s 米）' % int(acc) if acc else ''} → "
                         f"{rev['city'] or '没判出城市'}"
                         f"{'（' + rev['detail'] + '）' if rev.get('detail') else ''}")
            if rev.get("city"):
                picked = {"city": rev["city"], "source": "Windows 定位" + (
                    f"，精度约 {int(acc)} 米" if acc else "")}
            else:
                lines.append(f"  ↳ 逆地理没成功：{rev.get('detail') or '原因未知'}")
        else:
            lines.append(f"· Windows 定位不可用：{win.get('error') or '未知原因'}")
        ip = _weather.city_by_ip()
        lines.append(f"· **公网 IP**：{ip['city'] or '没判出城市'}"
                     f"{'（' + ip['detail'] + '）' if ip.get('detail') else ''}")
        if not picked and ip.get("city"):
            picked = {"city": ip["city"], "source": "公网 IP（城市级，可能不准）"}

        if not picked:
            self._detected_city = None
            return ("探测没得出可用城市（**没有写任何数据**）：\n" + "\n".join(lines) +
                    "\n可以直接告诉我：`/city 成都`。")
        self._detected_city = picked
        same = "（和现在用的一样）" if picked["city"] == self.weather.city_name else ""
        return ("探测结果（**还没应用**）：\n" + "\n".join(lines) +
                f"\n\n判断：**{picked['city']}**{same} ← 来源：{picked['source']}\n"
                f"对就发 `/city use`（会写进世界书、重启不丢）；不对就 `/city <城市名>` 手动定。"
                f"这次探测的精确坐标没有落盘。")

    def use_detected_city(self) -> str:
        """`/city use` —— 把上一次探测的结果应用（确认步骤）。"""
        if not self._detected_city:
            return "还没有可应用的探测结果——先发 `/city detect`（或直接 `/city 成都`）"
        city = self._detected_city["city"]
        out = self.set_city(city)
        src = self._detected_city.get("source") or ""
        self._detected_city = None
        return f"{out}\n（依据：{src}）"

    def set_city(self, name: str) -> str:
        """`/city <城市>` —— 换城市：天气缓存 + 世界书 + 立即重查。"""
        from app.utils import weather as _weather
        code = _weather.adcode_for(name)
        if not code:
            return (f"我不认识「{name.strip()}」这个城市（也不会瞎猜）。\n"
                    f"可以试试：/city 成都、/city 上海；或者直接给高德 adcode（如 /city 330100）。")
        self.weather.set_city(code)
        city = self.weather.city_name or name.strip()
        # 世界书是「长期设定」的唯一真相：城市属于它，不写进去重启就丢了。
        old = [e for e in self.world.entries if "住在" in e.get("text", "")]
        for e in old:
            self.world.remove(e["text"])
        self.world.add(f"用户住在{city}。")
        note = ""
        if self.weather.enabled:
            # 感知层的故障**不能挂住这条命令**（同 `__init__` 与 `finish_turn` 的铁律）：
            # 换城市本身已经成功了（缓存 + 世界书都写了），天气查不到只是"稍后再说"。
            # 踩过：`tests/test_pure.py` 里那条 `/city 成都` 用例撞上一次高德 TLS 握手超时，
            # 异常从这里直接抛穿，把整个套件打成**无输出**的 exit 1。
            try:
                data = self.weather.refresh()
            except Exception as exc:
                log.warning("换城市后天气重查失败（城市已换，不影响对话）：%s", exc)
                data = None
            note = "，天气已按新城市重查" if data else "（天气这次没查到，稍后会再试）"
        else:
            note = "（没配 WB_AMAP_KEY，天气功能仍是关的）"
        return f"记住了：你在{city}{note}。以后聊到天气就按{city}说。"

    # ==================== 记忆体检 ====================

    def dead_report(self) -> str:
        """列出从没被想起过的记忆。这是唯一「按数据删」的入口。"""
        dead = self.store.dead_memories()
        if not dead:
            return "没有被闲置的记忆——每一条都至少被想起过一次。"
        lines = [f"以下 {len(dead)} 条记忆从没被想起过（按记录时间从早到晚）："]
        now = time.time()
        for i, m in enumerate(dead, 1):
            days = (now - float(m.get("ts") or now)) / 86400.0
            lines.append(f"  {i}. [{m['kind']}] {m['text']}（{days:.0f} 天前记的）")
        lines.append("删除其中某条：/mem del 序号（序号按上面这份清单）")
        return "\n".join(lines)

    def delete_dead(self, index: str) -> str:
        if not index.isdigit():
            return "用法：/mem del 序号，序号来自 /mem dead 的输出"
        dead = self.store.dead_memories()
        k = int(index) - 1
        if not (0 <= k < len(dead)):
            return f"清单里没有第 {index} 条（当前共 {len(dead)} 条）"
        text = dead[k]["text"]
        # 这是**用户明确要求删除**（不是自动作废），所以物理删：
        # `drop_memories` 默认 archive=True 会只归档，而界面已经写了"已删除"——
        # 名实不符（T69-③）。
        self.store.drop_memories([text], archive=False)
        self.store.save()
        return f"已删除：{text}"

    def reconcile_report(self) -> str:
        """全量对账，只报告不改。"""
        try:
            issues = self.store.check_consistency()
        except Exception as exc:
            self.memory_check_error = f"记忆体检未完成：{exc}"
            return ("记忆体检未完成：远程模型请求失败（"
                    f"{exc}）。请稍后重试，未能确认是否存在矛盾。")
        self.memory_issues = issues
        self.memory_check_error = ""
        if not self.memory_issues:
            return "没发现矛盾：世界书、记忆和约束之间是一致的。"
        label = {"stale": "陈旧", "conflict": "冲突", "redundant": "重复"}
        lines = [f"发现 {len(self.memory_issues)} 处可能的问题（只报告，没有改动任何数据）："]
        for i, it in enumerate(self.memory_issues, 1):
            kind = label.get(it["type"], it["type"])
            lines.append(f"  {i}. 【{kind}】{' ／ '.join(it['items'])}")
            if it.get("reason"):
                lines.append(f"      理由：{it['reason']}")
        lines.append("要处理的话：改 world.json、用 /world tidy 合并、或让它自己在对话里更新。")
        return "\n".join(lines)

    # ==================== 状态 ====================

    def state(self) -> dict:
        return {
            "model": config.MODEL,
            # 主对话和后台活可能在不同供应商，把两边都显示出来便于确认
            "chat_provider": config.CHAT_BASE_URL.split("//")[-1].split("/")[0],
            "bg_model": config.EXTRACT_MODEL,
            "keys_ready": getattr(self, "keys_ready", True),
            "chat_ready": getattr(self, "chat_ready", True),
            "embed_ready": getattr(self, "embed_ready", True),
            "name": self.personality.display_name(),
            # 只给缓存值，不在这里生成——state 每轮都会被前端拉一次。
            # 真正的生成入口是 /api/greeting（页面打开/刷新时调）。
            "greeting": self._greeting or "",
            "personality": self.personality.summary(),
            "personality_text": self.personality.render(),
            "profile": self.profile.summary(),
            "profile_text": self.profile.render(),
            "memory": self.store.stats(),
            "memory_issues": self.memory_issues,  # 启动时对账的结果，供界面提示
            "memory_check_error": self.memory_check_error,
            "world": [{"text": e["text"], "source": e.get("source", "manual")}
                      for e in self.world.entries],
            "world_count": self.world.count(),
            "session": self.store.session,
        }

    # ==================== 会话 ====================

    def sessions(self) -> dict:
        return {"current": self.store.session, "items": self.store.list_sessions()}

    def _after_session_change(self) -> None:
        self.speaker.session = self.store.session
        self._greeting = None
        self._delivered_turns = []
        self._pending_context = None
        self._pending_question = ""
        self._nearby_block = ""
        self.last_turn = None

    def new_session(self, title: str = "") -> dict:
        self.store.new_session(title)
        self._after_session_change()
        return self.sessions()

    def select_session(self, session_id: str) -> dict:
        self.store.select_session(session_id)
        self._after_session_change()
        return self.sessions()

    def rename_session(self, session_id: str, title: str) -> dict:
        self.store.rename_session(session_id, title)
        return self.sessions()

    def delete_session(self, session_id: str) -> dict:
        self.store.delete_session(session_id)
        self._after_session_change()
        return self.sessions()

    def session_messages(self, session_id: str) -> list[dict]:
        return self.store.session_messages(session_id)

    def edit_message(self, session_id: str, seq: int, content: str) -> None:
        self.store.edit_message(session_id, seq, content)

    def delete_message(self, session_id: str, seq: int) -> None:
        self.store.delete_message(session_id, seq)

    def regenerate_last(self) -> dict:
        session_id = self.store.session
        question, removed = self.store.pop_last_turn(session_id)
        try:
            return self.chat(question)
        except Exception:
            self.store.restore_turn(session_id, removed)
            raise

    # ==================== 收尾 ====================

    def _personality_conflicts(self, patch: dict) -> list[dict]:
        """演化**应用之前**的一致性检查（T47）。

        为什么需要：实测一次演化给 behavior 加了「别动不动就讲道理、给建议」，
        而 traits 里原有「热心，先帮你想办法」——两句方向相反，于是"先给办法再共情"
        这条人设当场失灵（run_persona 的 PERS01 从 A 掉到 B）。记忆层有世界书查重
        挡着自相矛盾的设定，人格层此前什么都没有。

        失败策略：**检查器坏了就放行**（返回空列表）并打 WARNING + 记事件——
        宁可漏拦一次，也不能因为检查器故障把性格演化整个卡死；但失败必须可见。
        """
        d = self.personality.data
        existing: list[str] = []
        for key in ("identity", "tone", "traits", "values", "behavior", "speech_habits",
                    "origin", "family", "daily_life", "self_attitude"):
            v = d.get(key)
            if isinstance(v, list) and v:
                existing.append(f"{key}: " + " / ".join(str(x) for x in v))
            elif isinstance(v, str) and v.strip():
                existing.append(f"{key}: {v}")
            elif isinstance(v, dict) and v:
                existing.append(f"{key}: " + json.dumps(v, ensure_ascii=False))
        incoming: list[str] = []
        for key, v in patch.items():
            if isinstance(v, list) and v:
                incoming.append(f"{key}: " + " / ".join(str(x) for x in v))
            elif isinstance(v, str) and v.strip():
                incoming.append(f"{key}: {v}")
        if not existing or not incoming:
            return []
        prompt = PERSONALITY_CONFLICT_PROMPT.format(
            existing="\n".join(existing), incoming="\n".join(incoming))
        data, err = chat_json(
            self.client, config.EXTRACT_MODEL, [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=800, retries=2, no_think=bool(config.NO_THINK),
        )
        if not isinstance(data, dict):
            log.warning("人格一致性检查失败，本次放行（失败要可见）：%s", err)
            self.store.event("persona_check", ok=False, error=str(err)[:80])
            return []
        conflicts = data.get("conflicts") or []
        return [c for c in conflicts if isinstance(c, dict) and c.get("item")]

    def evolve_personality(self, turns: list[dict] | None = None) -> tuple[bool, str]:
        """让模型根据对话反馈微调性格模板。turns 不传就复盘全部历史。

        返回 (是否成功, 说明)。失败和「无需改动」必须分开——踩过：
        失败信息被当成改动说明打印，前缀还是「已微调」，实际什么都没改。
        """
        if not config.AUTO_EVOLVE_PERSONALITY:
            return True, ""
        if turns is None:
            turns = self.store.sessions.get(self.store.session, [])
        if not turns:
            return True, ""
        dialog = "\n".join(f"{m.get('role')}: {str(m.get('content', ''))[:300]}"
                           for m in turns)[:3000]
        prompt = PERSONALITY_EVOLVE_PROMPT.format(
            personality=json.dumps(self.personality.data, ensure_ascii=False), dialog=dialog,
            relation=self.relationship.render_for_decision(time.time(), self._user_msg_count()),
        )
        # 用后台那套模型（不思考）：这是「按格式吐一小段 JSON」的活，
        # 换成会思考的 glm-5.3-flash 时踩过整轮返回非法 JSON 的情况。
        patch, err = chat_json(
            self.client, config.EXTRACT_MODEL, [{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=1500, retries=2,
            no_think=bool(config.NO_THINK),
        )
        if patch is None:
            return False, f"性格模板迭代失败：{err}"
        if not patch:
            return True, ""
        # `#148`：她想不想把关系往前挪一档——**这是她的事**，所以先摘出来自己处理，
        # 别送进性格字段的白名单（那里只认 traits/behavior 那些）。落地护栏在
        # `Relationship.advance()` 里（一档一档、够资格、有理由、隔够天数），
        # 而且**不进"待确认"**：用户明确要的就是"她自己决定要不要推进关系"。
        rel_note = self._apply_relationship_decision(
            patch.pop("relationship", None) if isinstance(patch, dict) else None)
        if not patch:
            return True, rel_note
        # T47：应用之前先做一致性检查。冲突的条目**摘掉**、而不是整批作废——
        # 一次演化通常只引入一两条有问题的，同批里别的调整是有价值的。
        original = {k: (list(v) if isinstance(v, list) else v) for k, v in patch.items()}
        conflicts = self._personality_conflicts(patch)
        note = ""
        if conflicts:
            patch, removed = strip_conflicting_items(patch, conflicts)
            note = f"（一致性检查拦下 {len(removed)} 条自相矛盾的改动：{'；'.join(removed)}）"
            log.warning("人格演化被拦下 %d 条冲突条目：%s", len(removed), "；".join(removed))
            self.store.event("persona_conflict", n=len(removed),
                             items="；".join(removed)[:200])
            # T48：**留档等人拍板**，不静默丢掉——那条改动可能是用户真实想要的
            # （#046：用户说"别老讲道理、别劝我健身"，演化照着改了，却与 traits
            # 「先帮你想办法」冲突）。留的是**改动原样**，否则"采纳"就没了意义。
            self.personality.record_pending(
                original, removed,
                why="；".join(str(c.get("why") or "") for c in conflicts)[:200])
            note += "，已留档：/personality pending 可查看"
            if not patch:
                return True, note
        # 方案 2（用户 2026-09-28 拍板）：`cares` / `favorite_places` 属于**底座偏好**，
        # 不自动生效——拆出来进「待确认」，由人点头；其余字段照旧直接应用。
        # 已经在人眼前过一遍的条目会被 `_merge_growing` 标 `_pinned`，之后再怎么演化都动不了它。
        direct, growing = self.personality.split_append_fields(patch)
        if growing:
            self.personality.record_pending(
                growing, [],
                why="底座偏好（她在意的事 / 她爱去的地方）只增不改，需要你确认后才生效")
            self.store.event("persona_pending", fields="、".join(growing), reason="growing-base")
            note += ("；她的偏好类改动（" + "、".join(growing) +
                     "）已进待确认——/personality pending 可查看并采纳")
            patch = direct
            if not patch:
                return True, note
        return True, self.personality.evolve(patch, reason="对话自动迭代") + note + rel_note

    def _apply_relationship_decision(self, say) -> str:
        """她把关系往前挪一档的**决定**（`#148`）——护栏在 `Relationship.advance()` 里。

        与人设演化的规矩**故意不同**：人设改动要进"待确认"等人拍板，因为那是"她是谁"；
        而"要不要跟你更熟一点"是**她自己的感受**，用户要的就是她拿主意——所以直接落地，
        但必须留痕（`history` ＋ `relationship_advanced` / `..._refused` 事件），
        并且一条命令就能退回（`/relationship stage she|auto|<档>`）。
        """
        if not isinstance(say, dict):
            return ""
        to_key = str(say.get("advance_to") or say.get("to") or say.get("stage") or "")
        why = str(say.get("why") or say.get("reason") or "")
        ok, msg = self.relationship.advance(to_key, why, source="she",
                                           user_msgs=self._user_msg_count())
        log.info("她的关系决定：%s（%s）", msg, "已落地" if ok else "被护栏拦下")
        self.store.event("relationship_advanced" if ok else "relationship_advance_refused",
                         message=msg[:120], to=to_key[:12])
        return f"；{msg}" if ok else f"；（她想推进关系，但没落地：{msg}）"

    # ==================== 她主动说话（`#108`） ====================

    def proactive_tick(self) -> str:
        if not self._proactive_lock.acquire(blocking=False):
            return ""
        try:
            return self._proactive_tick()
        finally:
            self._proactive_lock.release()

    def _proactive_tick(self) -> str:
        """过一遍闸门；该说就说，说完进待发表。返回她说的话（多条用换行拼起来；没说不返回空串）。

        这是后台线程与"用户打开页面"两条路径共用的入口——两处都调它，
        闸门自己会拦住"同一时段重复说"。**任何异常都只记日志**：主动说话永远不能
        影响正常对话。
        """
        try:
            from app.services import speak as speak_mod
            self._advance_life()
            cfg = {"enabled": bool(config.PROACTIVE),
                   "daily_max": config.PROACTIVE_DAILY_MAX,
                   "min_gap_hours": config.PROACTIVE_MIN_GAP_H,
                   "quiet_min": config.PROACTIVE_QUIET_MIN,
                   "decision_gap_min": config.PROACTIVE_DECISION_GAP_MIN,
                   "low_min": config.PROACTIVE_LOW_MIN,
                   "low_chance": config.PROACTIVE_LOW_CHANCE,
                   "high_min": config.PROACTIVE_HIGH_MIN,
                   "high_chance": config.PROACTIVE_HIGH_CHANCE,
                   "chance_ceil": config.PROACTIVE_CHANCE_CEIL,
                   "roll_every_min": config.PROACTIVE_ROLL_EVERY_MIN,
                   "quiet_hours": config.PROACTIVE_QUIET}
            ok, why, detail = self.speaker.should_speak(cfg=cfg)
            if not ok:
                # **失败要可见**（用户 2026-09-30 的原话就是"为什么没有自言自语"）：
                # 以前跳过只写 `log.debug`，而默认级别是 INFO → 她为什么不说话在日志里
                # 完全查不到，只能翻数据库反推。现在：闸门**变化时**打一条 INFO +
                # 埋一条 `proactive_skip` 事件（带 gate/since_min/said/chance 等数字），
                # `/mem events` 直接能回答"她今天为什么没说话"。
                gate = detail.get("gate_class") or detail.get("gate")
                if gate != self._last_skip_gate:
                    self._last_skip_gate = gate
                    log.info("主动说话跳过：%s（%s）", why, detail)
                    self.store.event("proactive_skip", reason=why, **detail)
                return ""
            self._last_skip_gate = None
            # 把她俩最近几轮原话喂进去（她"接着刚才的聊"才自然）
            hist = self.store.sessions.get(self.store.session, [])[-6:]
            chat_lines = [f"{'他' if m.get('role') == 'user' else '我'}："
                          f"{str(m.get('content') or '')[:60]}" for m in hist]
            self.speaker.user_msgs = self._user_msg_count()      # 关系层推进阶段用（`#146`）
            texts = self.speaker.speak(self.client, config.EXTRACT_MODEL,
                                       chat_lines=chat_lines)
            if not texts:
                # ① **整批被判重丢掉**（`#138`）：这也是"她没开口"，但**原因不同**——
                #    要和"她自己选择不说"分开埋点，否则 `/mem events` 又会答不上"她怎么不说了"。
                dropped = list(getattr(self.speaker, "last_duplicates", None) or [])
                if dropped:
                    log.info("主动说话跳过：整批都与最近说过/刚做的太像（%s）",
                             " / ".join(x[:20] for x in dropped))
                    self.store.event("proactive_dup", dropped=[x[:60] for x in dropped])
                    return ""
                # ①b 整批都在**拿她自己的东西反问他**（`#146`）：团子是她的猫、球衣是她的，
                #     他答不上来。与"判重"分开埋点，这样 `/mem events` 能分辨是哪种挡下的。
                owned = list(getattr(self.speaker, "last_ownership", None) or [])
                if owned:
                    log.info("主动说话跳过：整批都在拿她自己的东西问他（%s）",
                             " / ".join(x[:20] for x in owned))
                    self.store.event("proactive_ownership_drop",
                                     dropped=[x[:60] for x in owned])
                    return ""
                if self.speaker.last_decision is False:
                    gate = "chose_silence"
                    if gate != self._last_skip_gate:
                        self._last_skip_gate = gate
                        reason = "她自己决定暂不说"
                        log.info("主动说话跳过：%s", reason)
                        self.store.event("proactive_skip", reason=reason,
                                         **{**detail, "gate": gate, "gate_class": gate})
                return ""
            # 说几条是她自己选的（`T86`）。这里**一条一行**地入待发表：前端轮询 `items`
            # 本来就是逐条渲染的，所以她在页面上就是几个独立气泡——前端不用做任何改动。
            now = time.time()
            last_user = self.speaker._ts("user_last_ts")
            quiet_min = float(cfg.get("quiet_min", 5) or 0)
            if last_user and quiet_min > 0 and now - last_user < quiet_min * 60:
                self._last_skip_gate = "in_conversation"
                self.store.event("proactive_skip", reason="生成期间刚收到你的话，这次不插话",
                                 gate="in_conversation", gate_class="in_conversation")
                return ""
            sent = []
            if getattr(self.speaker, "motive_mode", False):
                offered = {str(row.get("id")): row for row in getattr(self.speaker, "last_motives", [])}
                selected = [offered[mid] for mid in getattr(self.speaker, "last_motive_ids", [])
                            if mid in offered]
                selected = [row for row in selected if row.get("kind") != "promise"
                            or any(row.get("text") in one for one in texts)]
                try:
                    pending_ids = self.speaker.enqueue_motivated(
                        texts, selected, now=now, quiet_min=quiet_min, cfg=cfg)
                    if pending_ids:
                        sent = list(texts)
                        for one in sent:
                            for promise_id in self.speaker.last_reminders.get(one, []):
                                try:
                                    if not self.speaker.mark_motive_reminded(promise_id, now=time.time()):
                                        log.warning("承诺提醒已入队但未记账（可重试）：%s", promise_id)
                                except Exception as exc:
                                    log.warning("承诺提醒已入队但记账失败（可重试）：%s", exc)
                except Exception as exc:
                    log.warning("主动消息原子入队失败（这次不消费缘由）：%s: %s", type(exc).__name__, exc)
            else:
                for one in texts:
                    try:
                        with self.store._lock:
                            last_user = self.speaker._ts("user_last_ts")
                            if last_user and quiet_min > 0 and now - last_user < quiet_min * 60:
                                continue
                            pending_id = self.speaker.enqueue(one)
                        if pending_id > 0:
                            sent.append(one)
                    except Exception as exc:
                        log.warning("主动消息入队失败（这条稍后再试）：%s: %s", type(exc).__name__, exc)
            texts = sent
            if not texts:
                return ""
            # 她自己的话也要**更新她的此刻**（`#138`）：主动消息原来不进提炼，于是
            # "汤炖好了"这类自述没人接，状态页会一直停在旧的"回家炖汤中"（用户真机实测：
            # 21:38 对话里汤都端过来了，状态页还写着 19:23 的「回家炖汤中」）。
            self.observe_life_reply("\n".join(texts))
            # 她主动说的话**进对话历史**（下轮上下文才连贯）——但不进 memories：
            # 避免"她自己说过的话"被当成事实反复强化（设计时定的隔离边界）。
            # 分条时**每条各占一条历史**：那才是她真实说出来的顺序，下轮读着才连贯。
            with self.store._lock:
                hist = self.store.sessions.setdefault(self.store.session, [])
                for one in texts:
                    hist.append({"role": "assistant", "content": one, "ts": time.time(),
                                 "proactive": True})
            self.store.save()
            joined = "\n".join(texts)
            if not getattr(self.speaker, "motive_mode", False):
                self.store.event("proactive", text=joined[:40], n=len(texts))
            log.info("她主动开口了（%d 条）：%s", len(texts), joined[:60])
            return joined
        except Exception as exc:
            log.warning("主动说话异常（跳过这次）：%s: %s", type(exc).__name__, exc)
            return ""

    def take_pending_say(self) -> list[dict]:
        """她说的话里**还没被确认收下**的那些（只读，不改状态，`#119`）。

        ⚠ 语义变了（原来是"取走即标记"）：现在**客户端渲染成功后**再调
        `ack_pending_say()` 确认。原协议下任何一次渲染失败/页面刷新/响应丢失，
        那条话就永远消失（用户实测撞到："主动说的话我一点都不知道"）。
        """
        try:
            items = self.speaker.drop_invalid_motives(self.speaker.pending_items())
            kept = self.speaker.drop_own_questions(items)
            dropped = list(getattr(self.speaker, "last_delivery_drops", None) or [])
            if dropped:
                # `#156`：投递口也过归属兜底——生成口那道护栏管不到队列里早先攒下的话
                # （真机现场：「团子没扒窗户吧」，服务一起来就会发出去）。
                # `B2-②`（工单二）：现在**不当场 ack**，行还留在 `pending_say` 里
                # （看板可见、可人工恢复），所以埋点里把行 id 一起记下来。
                ids = list(getattr(self.speaker, "last_delivery_drop_ids", None) or [])
                log.info("投递前挡下 %d 条（她拿自己的东西问他了，未标记已读）：%s",
                         len(dropped), " / ".join(x[:20] for x in dropped))
                self.store.event("pending_ownership_drop",
                                 dropped=[x[:60] for x in dropped], ids=ids)
            return kept
        except Exception as exc:
            log.warning("读待发消息失败：%s: %s", type(exc).__name__, exc)
            return []

    def ack_pending_say(self, ids) -> int:
        """确认这几条**已经显示给用户了** → 才标记已读（`#119`，至少一次投递）。"""
        try:
            return self.speaker.ack_pending(ids)
        except Exception as exc:
            log.warning("确认待发消息失败：%s: %s", type(exc).__name__, exc)
            return 0

    def close(self) -> None:
        """收尾：落盘 + **关掉数据库连接**。

        为什么要关连接：Windows 上只要连接还开着，`memory.db` 就删不掉 ——
        `DELETE /api/users/{uid}` 想"把这个人的数据删干净"时会**静默失败**
        （`#069` 探针里 `data_dir_removed: False` 就是这么来的）。注销要真能删掉数据，
        就得先放手。关掉之后该实例不可再用，这也符合"被回收/被删号"的语义。
        """
        try:
            self.store.save()
        finally:
            try:
                self.store.db.close()
            except Exception as exc:
                log.warning("关闭记忆库连接失败：%s", exc)
