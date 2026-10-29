# 旧识 · 带记忆治理的本机对话助手

一个跑在本机的陪伴型对话助手。**重点不是聊天界面，而是记忆系统**：跨会话记住发生过的事，把「她是谁 / 你是谁 / 你们共同的世界 / 你们聊过什么」分开存，并且允许旧记录被新事实推翻。

> 个人项目，单机自用，默认只监听 `127.0.0.1`。代码与评测以「可核验」为先：失败要可见，评测要能复现，不把模型自报的分数当成结论。

---

## 它能做什么

- **四层记忆**：原文消息 → 滚动摘要 → 事实短句 + 向量 → 遗忘/作废/推翻。
- **写路径治理**：提炼时区分事实、世界书、状态槽位和约束；相似度去重；冲突时软删旧条目并保留归档；状态变化可触发「旧值作废」。
- **读路径**：按语义阈值召回长期记忆，叠加世界书、状态槽位、约束、今日/明日约定和最近窗口；过期约定不再冒充待办。
- **共同世界**：人格模板、世界书、用户画像、关系阶段、生活线、连续性话题分别落盘，互不越权。
- **多用户**：每个账号一套独立数据目录 `users/<id>/`，令牌鉴权 + LRU 实例缓存。
- **主动说话**：低频心跳 + 可数闸门（静默时段、每日上限、对话冷却），不会变成推送机器人。
- **感知**：天气、附近真实地点（高德）、情绪轨迹；未配置密钥时对应能力自动关闭。
- **可观测**：网页侧栏可查看关系、世界书、状态、时间轴、生活线、画像和记忆体检。
- **评测**：47 条记忆用例、13 条人格探针、12 组陪伴对照，支持重复跑和离线重评。

---

## 架构

```
浏览器（原生 HTML / Tailwind / ES Modules / SSE）
        │
FastAPI controllers ── 鉴权、用户、静态资源
        │
ChatService ── 记忆读写、生活线、连续性、关系、主动说话
        │
SQLite（messages / summaries / memories+vector / slots / events ...）
+ JSON（personality / world / profile / relationship / life）
        │
OpenAI 兼容模型 · embedding · TTS · 高德 · 情绪判定
```

| 层 | 位置 | 职责 |
|---|---|---|
| View | `app/views/` | 登录、聊天、记忆页、生活看板、音频播放 |
| Controller | `app/controllers/` | HTTP/SSE 进出口、身份校验 |
| Service | `app/services/` | 一轮对话的编排、主动说话、鉴权、worker、实例注册 |
| Model | `app/models/` | 记忆、世界书、状态槽位、生活、关系、连续性、人格、画像 |
| Util | `app/utils/` | LLM JSON、天气、地点、定位、文本相似度、TTS |

更细的设计说明见 [`docs/架构.md`](docs/架构.md)、[`docs/项目总览.md`](docs/项目总览.md) 与 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

---

## 快速开始

要求 Python 3.13。

```bash
pip install -r requirements.txt

cp .env.example .env      # 填入自己的 key；.env 不进版本库
python web.py             # → http://127.0.0.1:8765
```

命令行版与网页版共用同一套 Service：

```bash
python openai_hello.py
```

### 配置

所有配置都支持环境变量覆盖，**环境变量优先于 `.env`**。最少需要：

| 变量 | 用途 |
|---|---|
| `WB_API_KEY` / `WB_BASE_URL` | 后台任务（提炼、摘要、画像、开场白） |
| `WB_CHAT_API_KEY` / `WB_CHAT_BASE_URL` | 主对话；留空复用 `WB_API_KEY` |
| `WB_EMBED_API_KEY` / `WB_EMBED_BASE_URL` | embedding；缺了记忆召回会瘫痪 |
| `WB_MODEL` / `WB_EMBED_MODEL` | 模型名 |

可选能力：`WB_AMAP_KEY`（天气/附近）、`WB_EMOTION_*`（情绪）、`WB_TTS_*`（语音）。
完整列表见 [`.env.example`](.env.example) 与 [`docs/技术栈.md`](docs/技术栈.md)。

### Docker

```bash
docker build -t jiushi .
docker run -p 8765:8765 --env-file .env -v wb-data:/data \
  -e WB_AUTH_REQUIRED=1 jiushi
```

数据卷必须挂出来：记忆库、人设、画像、世界书、日志都在 `/data`。

---

## 测试与评测

```bash
# 纯函数 + 记忆机制 + 鉴权 + 主动说话闸门；不联网、不调模型
python tests/test_pure.py

# 前端契约与 SSE 解析
npm ci
npm run test:frontend

# 其他离线回归
python -m unittest tests.test_password_auth tests.test_identity_transport tests.test_frontend_contract
```

评测会真实调用模型，需要有效密钥：

```bash
python benchmark/run_bench.py --fresh --judge       # 47 条记忆用例
python benchmark/run_bench.py --repeat 8 --ids B11  # 单条重复跑
python benchmark/run_bench.py --rescore benchmark/results/<report>.json
python benchmark/run_persona.py                     # 13 条人格探针
```

评测结果不提交到仓库（`benchmark/results/` 已被忽略）。

---

## 目录结构

```
app/
  controllers/      HTTP 与 SSE
  services/         对话编排、鉴权、worker、主动说话、TTS
  models/           记忆、世界书、槽位、生活、关系、连续性、人格、画像
  utils/            LLM JSON、天气、地点、定位、文本相似度
  views/            原生前端与已编译静态资源
benchmark/          记忆/人格/陪伴评测
tests/              Python + Node 测试
docs/               架构、技术栈、设计决策与调研
```

数据文件默认在项目根目录；设置 `DATA_DIR` 可整体迁移。多用户数据在 `DATA_DIR/users/<id>/`。

---

## 安全与隐私

- `.env`、数据库、日志、用户目录、评测结果都被 `.gitignore` 忽略；**不要把 `.env` 提交到仓库**。
- 默认只监听 `127.0.0.1`，适合本机使用。
- **对外提供服务前必须设置 `WB_AUTH_REQUIRED=1`**。在还没有账号的阶段，未强制鉴权的业务接口是敞开的。
- Docker 默认监听 `0.0.0.0`，只应在受信任的网络中运行，或放在带鉴权的反向代理后。
- 密码使用随机盐 PBKDF2-HMAC-SHA256（600000 次）；令牌只存 SHA256 摘要。

---

## 已知限制

- 这是一个**单机 SQLite** 项目，没有做多进程并发写与水平扩展。
- 默认测试用假模型验证机制；真实模型下的记忆质量、自然度和 TTS 效果需要用自己的密钥复测。
- 天气、附近地点、情绪判定依赖第三方服务；未配置时自动关闭，不会编造结果。
- 评测分数受模型随机性和用例污染影响；判断改动是否退化应看 `--repeat`，不要只看单次。
- 设计取舍与踩坑记录见 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

---

## License

MIT，详见 [LICENSE](LICENSE)。
