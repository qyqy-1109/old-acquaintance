"""全局配置：所有硬编码常量集中在这里，其它层一律从这里取。

数据文件默认就放在项目根目录，跟重构前的位置一致，
所以已有的 personality.json / memory_data.json / world.json 不用挪窝。
想隔离测试或者换地方存，设环境变量 DATA_DIR 就行。
"""

from __future__ import annotations

import os
import math
from urllib.parse import unquote

# ===== 先把 .env 读进环境变量 =====
# 密钥**不再硬编码在这个文件里**（`#064`）：值放项目根的 `.env`（不进版本库），
# 代码里只读环境变量。**已存在的环境变量优先**（见 `app/utils/envfile.py`），
# 所以部署时用真正的环境变量覆盖 `.env` 是安全的、也是推荐的。
# 必须放在任何 `os.environ.get` 之前。
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from app.utils.envfile import load as _load_env
    _load_env(os.path.join(_BASE, ".env"))
except Exception as _exc:            # 加载失败不该让整个进程起不来
    print(f"[config] .env 加载失败（继续只用环境变量）：{_exc}")

# app/config.py -> app/ -> 项目根
BASE_DIR = _BASE
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
VIEW_DIR = os.path.join(BASE_DIR, "app", "views")

# 数据目录不存在就建出来。不然启动会崩在"写 personality.json 失败"上，
# 报错指向的是文件路径，看不出真实原因是目录不存在，很费时间。
os.makedirs(DATA_DIR, exist_ok=True)


def data_path(name: str) -> str:
    """数据文件的绝对路径（单用户 / 根目录）。"""
    return os.path.join(DATA_DIR, name)


_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_RESERVED_NAMES.update(
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
)


def validate_user_id(user_id: str) -> str:
    """Validate the portion of a user id that becomes a directory name."""
    if not isinstance(user_id, str):
        raise ValueError("账号需为 1 至 60 个字符，不能包含路径或文件名特殊字符。")
    user_id = user_id.strip()
    try:
        decoded = unquote(user_id, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        decoded = user_id
    if (not user_id or len(user_id) > 60 or user_id in (".", "..")
            or user_id.endswith(".")
            or user_id.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES
            or any(char in '/\\:*?"<>|' or ord(char) < 32 for char in user_id)
            or any(char in '/\\' for char in decoded)):
        raise ValueError("账号需为 1 至 60 个字符，不能包含路径或文件名特殊字符。")
    return user_id


def user_data_dir(user_id: str) -> str:
    """某个用户的数据目录：`<DATA_DIR>/users/<id>/`。

    形态是「**每人一个她**」，所以每个用户拿一整套数据（人设、画像、世界书、记忆库、
    情绪、待确认的演化改动）。

    为什么用目录隔离，而不是"单库加 `user_id` 列 + 处处过滤"：**隔离靠目录就不会漏**。
    单库方案要在每一次召回、每一个槽位查询上都记得带上 `user_id`，漏一处就是串号——
    而记忆是共享池、人格只有一份，串号意味着"她会把跟 A 的私事说给 B 听"，
    这在人格上是致命的。
    """
    user_id = validate_user_id(user_id)
    d = os.path.join(DATA_DIR, "users", user_id)
    os.makedirs(d, exist_ok=True)
    return d


# ===== 模型与接口 =====
# 三个都支持用环境变量覆盖（WB_API_KEY / WB_BASE_URL / WB_MODEL / WB_EMBED_MODEL），
# 这样换供应商、做 A/B 对照都不用改代码，密钥也不必写进文件。
# ===== 分工：主对话与后台活都走小米 mimo，embedding 单独走千问 =====
# 这是全量 46 条实测出来的最优组合，不要凭直觉改。
#
# 历史（2026-09）：曾经是「后台活 + embedding 留在智谱」，理由是单次提炼 glm-5.2 1.74s
# 快过 mimo-v2.6-flash 2.52s。之后智谱账户余额用尽，后台活也就一并落在 mimo 上——
# 现在 API_KEY / BASE_URL 指的就是小米，配置里已经没有任何智谱端点。
# 唯一还在小米之外的是 embedding（见下）：小米端点没有 /embeddings 接口。
# ⚠ 口径：早先的成绩是「智谱后台 + mimo 主对话」测的，现在的成绩是「mimo 后台 +
# mimo 主对话」，两批不要直接比。
# 要试别的组合改环境变量就行：WB_API_KEY / WB_BASE_URL。
# ⚠ 密钥值在项目根的 `.env`（不进版本库），这里**不再有明文默认值**（#064）。
API_KEY = os.environ.get("WB_API_KEY") or ""
BASE_URL = os.environ.get("WB_BASE_URL") or "https://api.xiaomimimo.com/v1"

# embedding 单独一套：小米那个端点**没有 embedding 接口**（连 /embeddings 路径都是 404），
# 换过去记忆系统的召回会直接瘫痪。
EMBED_API_KEY = os.environ.get("WB_EMBED_API_KEY") or ""
EMBED_BASE_URL = os.environ.get("WB_EMBED_BASE_URL") or "https://maas.qianwenaiapi.com/compatible-mode/v1"

# ===== 主对话：单独走小米 mimo =====
# 为什么只换主对话：这是全量 46 条实测出来的最优组合——
#   mimo-v2.6-flash（关思考）  CSR 93.5% / 完整率 94.9% / 幻觉 1.8% / 首字 ~1.5s
#   glm-5.3-flashx            CSR 95.7% / 完整率 89.8% / 幻觉 1.9% / 首字 ~4s
# 只差 1 条（噪声范围内），完整率反而更高，快 2.7 倍。
# 后台活和 embedding 留在智谱：换聊天模型时不能让记忆系统的地基跟着变，
# 否则出问题分不清是谁的锅（这个坑踩过——把提炼一起换成 qwen3.8-flash 时
# 分数从 89% 掉到 40%，一度误判成聊天模型的问题）。
CHAT_API_KEY = os.environ.get("WB_CHAT_API_KEY") or ""
CHAT_BASE_URL = os.environ.get("WB_CHAT_BASE_URL") or "https://api.xiaomimimo.com/v1"

# 关思考的写法各供应商不一样，所以按**参数族名**给，不用布尔（旧供应商名当别名保留）：
#   "off"              不关 ← **默认**
#   "thinking"         → extra_body={"thinking": {"type": "disabled"}}
#   "enable_thinking"  → extra_body={"enable_thinking": False}   通义/百炼系
# 别名：zhipu = thinking，qwen = enable_thinking
# "off" 必须有个名字：Windows 上 `set WB_CHAT_NO_THINK=` 会把变量直接删掉，空串做不到。
#
# 默认为什么是「不关」——两段 ≥8 样本实测（D9 的纪律）：
#   ① 写法：enable_thinking 族发给 mimo-v2.6-flash 会被**静默忽略**（reasoning 8/8 不归零），
#      只有 thinking 族真关得掉（reasoning 0/8）；真关掉后首字中位 4.20s → 3.43s。
#   ② 代价：真关掉之后，B11（否定式偏好「别给我发长语音，我更习惯看文字」）规则层
#      漏 `no` **8/8**，而开着思考只有 **3/8**；8 遍的最终通过也从 8/8 掉到 5/8。
#      省的是 0.77s 首字延迟，赔的是记忆忠实度——按项目优先级（记忆质量 > 聊天体验）
#      默认不关。想省延迟/省 token 就设 WB_CHAT_NO_THINK=thinking。
# 名字写错也不再静默：chat_service 会打 WARNING（"失败要可见"）。
CHAT_NO_THINK_DEFAULT = "off"
CHAT_NO_THINK = os.environ.get("WB_CHAT_NO_THINK", CHAT_NO_THINK_DEFAULT)
# 主对话模型：mimo-v2.6-flash。选它的依据见上面 45-52 行那张对照表。
# 历史选型数据留档：glm-5.3-flash 首字 13～29s、glm-5.3-flashx 4～7s、glm-5.2 关思考能到
# 0.9s，但它们要么慢、要么在超纲题上完全不守知识边界（glm-5.2），所以都没用。
MODEL = os.environ.get("WB_MODEL") or "mimo-v2.6-flash"
STREAM = True                                      # True=流式逐字输出
# thinking token 也算在 max_tokens 里（关思考一旦失效就尤其明显）。
# 实测 1024 不够：遇到要多想两步的问题（安慰走丢的宠物、带条件的约束），
# 思考把额度吃光后正文就是空的，表现为「她突然不说话了」。
# 提到 2048 之后这类空回答消失，代价是 completion 最多翻倍。
MAX_TOKENS = 2048

PROFILE_MODEL = os.environ.get("WB_PROFILE_MODEL") or "mimo-v2.6-flash"
PROFILE_EVERY = 5                                  # 每多少轮做一次画像分析
AUTO_EVOLVE_PERSONALITY = True                     # 退出时根据对话自动微调性格模板
GREETING = True                                    # 启动时是否打一句开场白
# 开场白从记忆里长出来：接着上次的话题、提一件她真记得的事，而不是随机念一句固定台词。
# 也是后台活（不思考、1 秒左右），只在进程启动后第一次要开场白时生成一次，之后缓存复用。
# 记忆还是空的时候会自动退回 personality.json 里备好的固定开场白。
GREETING_USE_MEMORY = True
GREETING_MODEL = os.environ.get("WB_GREETING_MODEL") or "mimo-v2.6-flash"

# 每轮把当前时间注入上下文，让她知道「现在」是什么时候。
# 配套的两件事都在 app/utils/clock.py 里：相对时间在写入时换算成绝对日期，
# 注入记忆时自动补「还有几天 / 已经过去几天」的时距提示。
INJECT_TIME = True

# 启动时做一次全量跨层对账（世界书 / 记忆 / 约束之间有没有互相矛盾）。
# 只报告、不自动改数据——自动改试过，代价是模型把「住在杭州，养了只叫年糕的橘猫」
# 整条删掉，猫也连坐弄丢。要复查看 /mem check。
CHECK_ON_START = True
MEMORY_CHECK_TIMEOUT = float(os.environ.get("WB_MEMORY_CHECK_TIMEOUT") or 20)
if not math.isfinite(MEMORY_CHECK_TIMEOUT) or MEMORY_CHECK_TIMEOUT <= 0:
    raise ValueError("WB_MEMORY_CHECK_TIMEOUT 必须是有限正数（秒）")

# ===== 记忆 =====
EMBED_MODEL = os.environ.get("WB_EMBED_MODEL") or "qwen3.7-text-embedding"
# 输出维度。0 = 不传这个参数（用模型默认）。千问的 embedding 默认 1024，
# 指定 2048 能跟智谱 embedding-3 的维度对齐，迁移时心智负担小一号。
# 注意：只有不带 flash 的版本认这个参数，flash 版会忽略它（永远 1024）。
EMBED_DIMENSIONS = int(os.environ.get("WB_EMBED_DIMENSIONS") or 2048)

# ===== 语音合成（TTS）：qwen-audio-3.1-tts-flash，走 DashScope WS 协议 =====
# 失败静默：TTS 挂了绝不影响文字对话。音色必须用 _v3.1 后缀（音色与模型代际绑定，
# 混用报 Engine error [411]——踩过）。复刻音色的 voice_id 也填在这里。
TTS_ENABLED = os.environ.get("WB_TTS_ENABLED", "1") not in ("0", "false", "False")
TTS_WS_URL = os.environ.get("WB_TTS_WS_URL") or "wss://maas.qianwenaiapi.com/api-ws/v1/inference"
TTS_API_KEY = os.environ.get("WB_TTS_API_KEY") or EMBED_API_KEY   # 默认复用千问 key
TTS_MODEL = os.environ.get("WB_TTS_MODEL") or "qwen-audio-3.1-tts-flash"
TTS_VOICE = os.environ.get("WB_TTS_VOICE") or "anyuqing_v3.1"     # 安语晴：甜妹
TTS_INSTRUCTION = os.environ.get("WB_TTS_INSTRUCTION") or (
    "沈知夏的语气：轻松、带点俏皮，像串门聊天那样自然，带笑意")
TTS_MAX_CHARS = int(os.environ.get("WB_TTS_MAX_CHARS") or 300)     # 超长回复截断
TTS_PROSODY = os.environ.get("WB_TTS_PROSODY", "1") not in ("0", "false", "False")  # 韵律脚本引擎开关（回退：0=v1 纯文本）
TTS_TIMEOUT = int(os.environ.get("WB_TTS_TIMEOUT") or 30)

# 后台任务专用：记忆提炼 / 世界书查重 / 会话摘要。
# 这些活每轮都要跑，要的是「快、稳、按格式吐一小段 JSON」，用不着深度推理。
# 实测（2026-09-21，同一段提炼任务）：
#   glm-5.3-flash 思考开      7～20s，max_tokens 给小时会被思考吃光、JSON 被截断
#   glm-5.2       思考关      1.5s，  小额度下照样 3/3 成功，facts/world 分类正确
#   glm-4.5-air   思考关      0.9s，  最快，但把该进世界书的内容全塞进 facts，世界书会空掉
# 所以选 glm-5.2 + 关思考。注意 glm-5.3 / 5.3-flash / 5.3-flashx 这三个不支持关思考
# （服务端报 1210），llmjson 里做了兜底：关不掉就退回默认行为，不会报错。
EXTRACT_MODEL = os.environ.get("WB_EXTRACT_MODEL") or "mimo-v2.6-flash"
NO_THINK = True                                    # 后台任务关掉思考链路

WINDOW = 10                                        # 滑动窗口：送进模型的最近轮数
TOP_K = 3                                          # 长期记忆每轮最多召回几条
TOP_K_WORLD = 3                                    # 世界书每轮最多召回几条
RECALL_THRESHOLD = 0.45                            # 相似度低于这个就不召回
MAX_CONSTRAINTS = 12                               # 每轮无条件注入的约束条数上限（超了只留最近的）
DROP_STALE_PLANS = True                            # 过期的约定不再参与召回（不再冒充待办）
AUTO_EXTRACT = True                                # False = 退回"整轮原文入库"的旧行为
EXTRACT_EVERY = 1                                  # 每几轮提炼一次
MAX_MEMORIES = 2000                                # 长期记忆容量上限
HALF_LIFE_DAYS = 30                                # 多久没被想起，权重减半
SUMMARIZE_EVERY = 6                                # 窗口外攒够几轮做一次滚动摘要

CONTINUITY_TTL_DAYS = max(1.0, float(os.environ.get("WB_CONTINUITY_TTL_DAYS") or 7))
CONTINUITY_CONTEXT_LIMIT = max(1, int(os.environ.get("WB_CONTINUITY_CONTEXT_LIMIT") or 3))

MEMORY_CFG = {
    "no_think": NO_THINK,       # 后台任务关思考：快很多，也不会把 token 额度吃光
    "window": WINDOW,
    "top_k": TOP_K,
    "top_k_world": TOP_K_WORLD,
    "threshold": RECALL_THRESHOLD,
    "max_constraints": MAX_CONSTRAINTS,
    "drop_stale_plans": DROP_STALE_PLANS,
    "auto_extract": AUTO_EXTRACT,
    "extract_every": EXTRACT_EVERY,
    "max_memories": MAX_MEMORIES,
    "half_life_days": HALF_LIFE_DAYS,
    "summarize_every": SUMMARIZE_EVERY,
    "continuity_ttl_days": CONTINUITY_TTL_DAYS,
    "continuity_context_limit": CONTINUITY_CONTEXT_LIMIT,
    "check_timeout": MEMORY_CHECK_TIMEOUT,
}

# ===== 文件名 =====
PERSONALITY_FILE = data_path("personality.json")
USER_FILE = data_path("user_profile.json")
WORLD_FILE = data_path("world.json")
EMOTION_FILE = data_path("emotion.json")
# 记忆库改用 SQLite：一条记忆一行，向量作为 BLOB 存在同一行里。
# 换掉「JSON + 独立向量文件」是因为那套有三个结构性问题——
# 每次保存全量重写；作废只能物理删（实测捆在一起的信息会连坐丢失）；
# memories 和 vectors 两个平行数组靠下标对齐，漏同步一个就静默错位。
MEMORY_DB = data_path("memory.db")
# 旧格式的两个文件只用于一次性迁移，迁完会被改名成 *.migrated 保留
LEGACY_MEMORY_FILE = data_path("memory_data.json")
LEGACY_MEMORY_VEC = data_path("memory_data_vecs.npy")

# ===== 鉴权（多用户的"门锁"）=====
# 光有数据隔离不够：`X-User-Id` 是客户端自报的，谁都能冒充别人的 id 去读别人的记忆。
# 所以要有服务端签发、能校验的令牌（见 app/services/auth.py）。
# 默认 False 是**为了本地开发与 benchmark 不受影响**（它们直接调内存里的 service）；
# **对外提供服务的部署必须设 WB_AUTH_REQUIRED=1**，否则等于没锁门。
AUTH_REQUIRED = os.environ.get("WB_AUTH_REQUIRED", "0") not in ("0", "false", "False", "")

# ===== Web =====
HOST = "127.0.0.1"                                 # 只监听本机，别暴露到局域网
PORT = 8765

EXIT_WORDS = {"exit", "quit", "q", "退出", "bye"}

# ===== 高德天气（感知环境） =====
# 形态：天气是「环境常量」，跟【当前时间】同等待遇——缓存 1 小时、注入一行事实，
# 话术交给人格模板（工具给事实，人格给话术；不要写「下雨就说带伞」的模板）。
# key 在 https://lbs.amap.com 免费注册（Web服务 key）。不填则整个功能静默关闭。
AMAP_KEY = os.environ.get("WB_AMAP_KEY") or ""
# 城市**不再有兜底默认值**（`#109`）。老代码是 `or "330100"`（杭州）——那是开发时
# 写死的"用户位置"，结果：① 每次启动先按杭州拉一次、紧接着被世界书覆盖成真实城市，
# 白花一次高德请求、日志里还像她认错了城市（用户 2026-09-29 重启时看到的正是这个）；
# ② 真到了"世界书也没写城市"的情况，她就会**悄悄**按杭州说话（聊天气、推荐附近），
# 而这在日志里看不出来——那正是 D10 说的"静默失灵比显式失败更危险"。
# 现在的口径：**不知道你在哪就不猜**——天气不注入 + 启动打一条 WARNING 告诉你
# `/city 成都` 说一声。城市的真正来源是世界书（见 `ChatService._city_from_world`），
# 这个环境变量只是"世界书还没写时"的显式人工兜底。
AMAP_CITY = os.environ.get("WB_AMAP_CITY") or ""
WEATHER_TTL = int(os.environ.get("WB_WEATHER_TTL") or 3600)   # 缓存秒数；过期后在下轮后台刷新

# ===== 情绪轨迹（感知对方） =====
# 形态：**跨天的情绪记忆**，不是每轮给主对话塞标签。每轮回答完之后后台判一次，
# 存 emotion.json，注入时只给一行总结；没有值得说的就沉默（宁可漏也别误判放大）。
# 模型是 jev（TypeSafe 的 System One **决策模型**，经 tu-zi 中转）——
# 协议不是 OpenAI 的：POST /systemone，发 state + 类型化问题，回概率+置信度。
# 见 app/models/emotion.py。置信度 < 0.6 不记录（低置信度不行动）。
EMOTION_BASE_URL = os.environ.get("WB_EMOTION_BASE_URL") or "https://api.tu-zi.com/v1"
EMOTION_API_KEY = os.environ.get("WB_EMOTION_API_KEY") or ""
EMOTION_MODEL = os.environ.get("WB_EMOTION_MODEL") or "jev-1.13"
AUTO_EMOTION = os.environ.get("WB_AUTO_EMOTION", "1") != "0"
EMOTION_EVERY = int(os.environ.get("WB_EMOTION_EVERY") or 1)   # 每几轮判一次，0 = 关闭

LIFE_HISTORY_LIMIT = max(1, int(os.environ.get("WB_LIFE_HISTORY_LIMIT") or 8))
LIFE_ADVANCE_DELAY_MIN = int(os.environ.get("WB_LIFE_ADVANCE_DELAY_MIN") or 10)
LIFE_ADVANCE_DELAY_MAX = int(os.environ.get("WB_LIFE_ADVANCE_DELAY_MAX") or 50)


# ===== 她主动说话（`#108`）=====
# 从"只能你先说"变成"她也会来找你"。默认开着，但闸门全是**可数**的。
# `WB_PROACTIVE=0` 一键关掉。
#
# ⚠ 概率曲线由用户 2026-09-30 定稿（原话）：
#   「**我和她对话的时候不进行这个线程，等没对话的时候进行每分钟抽取**；
#     前面 30 分钟概率逐步上升但很低（比如 5%），30 分钟后大大提升变成 80%，
#     到 1 个小时变成百分百；**抽中一次之后从头开始**」。
# 于是：
#   · **起算点 = 最近一次有人说话**（他说的 / 她上次主动说的）——她实际开口才重置，
#     抽中判断机会但选择沉默时不重置；
#   · **对话刚结束的几分钟根本不抽**（`QUIET_MIN`）= 用户说的"对话时不进行这个线程"；
#   · 曲线 `p(since)`（**每分钟抽一次**）：
#       `[QUIET_MIN, LOW_MIN) → LOW_CHANCE·since/LOW_MIN`（很低，30 分钟逼近 5%）
#       `[LOW_MIN, HIGH_MIN] → HIGH_CHANCE + (1-HIGH_CHANCE)·(since-LOW_MIN)/(HIGH_MIN-LOW_MIN)`
#       `≥ HIGH_MIN → 100%`
#   · 日上限按**开口次数**算（不是气泡条数：`T86` 之后一次可能发 1~3 条）；
#   · 旧的四小时硬墙（`MIN_GAP_H`）**默认关掉**：防连发交给"开口后从头开始"。
PROACTIVE = os.environ.get("WB_PROACTIVE", "1") not in ("0", "false", "False")
# 后台心跳：用户要"每分钟抽取"，所以默认 1 分钟（闸门是纯本地判断，只有过了才调模型）
PROACTIVE_EVERY = int(os.environ.get("WB_PROACTIVE_EVERY") or 1)
PROACTIVE_DAILY_MAX = int(os.environ.get("WB_PROACTIVE_DAILY_MAX") or 6)   # 一天最多**开口几次**
PROACTIVE_MIN_GAP_H = float(os.environ.get("WB_PROACTIVE_MIN_GAP_H") or 0)  # 0 = 关（默认）
PROACTIVE_DECISION_GAP_MIN = float(os.environ.get("WB_PROACTIVE_DECISION_GAP_MIN") or 30)
# 对话下限：最近一次有人说话之后这么多分钟内**根本不抽**
PROACTIVE_QUIET_MIN = float(os.environ.get("WB_PROACTIVE_QUIET_MIN") or 5)
# 低概率段的终点与概率（"前面 30 分钟逐步上升但很低，比如 5%"）
PROACTIVE_LOW_MIN = float(os.environ.get("WB_PROACTIVE_LOW_MIN") or 30)
PROACTIVE_LOW_CHANCE = float(os.environ.get("WB_PROACTIVE_LOW_CHANCE") or 0.05)
# 高概率段的起点与概率（"30 分钟后大大提升变成 80%"）、以及到达 100% 的时刻（"1 个小时"）
PROACTIVE_HIGH_MIN = float(os.environ.get("WB_PROACTIVE_HIGH_MIN") or 60)
PROACTIVE_HIGH_CHANCE = float(os.environ.get("WB_PROACTIVE_HIGH_CHANCE") or 0.80)
PROACTIVE_CHANCE_CEIL = float(os.environ.get("WB_PROACTIVE_CHANCE_CEIL") or 1.0)
# 同一时间窗里只抽一次（分钟）。1 = 每分钟抽一次（用户口径）；0 = 不限制（排障用）。
PROACTIVE_ROLL_EVERY_MIN = float(os.environ.get("WB_PROACTIVE_ROLL_EVERY_MIN") or 1)
PROACTIVE_QUIET = tuple(int(x) for x in
                        (os.environ.get("WB_PROACTIVE_QUIET") or "0,8").split(","))  # 静默时段
PROACTIVE_MOTIVE_LIMIT = int(os.environ.get("WB_PROACTIVE_MOTIVE_LIMIT") or 4)
PROACTIVE_MOTIVE_TTL_DAYS = float(os.environ.get("WB_PROACTIVE_MOTIVE_TTL_DAYS") or 7)
# `#147` 用户口径：「放宽到 24:00 到 8:00」——原来默认 23,8，结果她**整晚一句都没有**，
# 而 23–24 点恰好是睡前最自然的那一小时（报备、晚安）。现在 0 点才进静默。

# ===== 性格演化节奏 =====
# 原来只在服务退出时演化一次——长期不重启，几天的经验会被压缩成一次大改，
# 且退出时才第一次见到这些对话，改动量大、没法归因。
# 改成小步迭代：每 EVOLVE_EVERY 轮后台复盘一次「新增的对话」（只看增量），
# 退出时再兜底收个尾。改动小、可归因、可回滚（history 留 20 版）。
EVOLVE_EVERY = int(os.environ.get("WB_EVOLVE_EVERY") or 20)

# ===== 启动自检：必需密钥缺了就说清楚 =====
# 密钥的默认值已经删掉（`#064`），所以"忘了配 .env"会一路表现为 401 或调用失败。
# 与其等第一次调用才炸，不如启动时就把缺哪个讲明白——**失败要可见**。
# 只警告、不阻断：本地跑测试、跑 benchmark 都不需要真 key。
_MISSING_KEYS = [
    name for name, value in (
        ("WB_API_KEY（后台活：提炼 / 摘要 / 画像）", API_KEY),
        ("WB_EMBED_API_KEY（embedding，缺了记忆召回会瘫痪）", EMBED_API_KEY),
        ("WB_CHAT_API_KEY 或 WB_API_KEY（主对话至少要有一个）", CHAT_API_KEY or API_KEY),
    ) if not value
]
if _MISSING_KEYS:
    print("[config] [!] 缺少必需的密钥，相关功能会失败：\n  - "
          + "\n  - ".join(_MISSING_KEYS)
          + "\n  把 .env.example 复制成 .env 并填上即可（密钥不需要写进代码）。")
