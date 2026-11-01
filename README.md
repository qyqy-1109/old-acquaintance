# 旧识（old-acquaintance） · 带记忆治理的本机对话助手

一个跑在本机的陪伴型对话助手。**重点不是聊天界面，而是记忆系统**：跨会话记住发生过的事，把「她是谁 / 你是谁 / 你们共同的世界 / 你们聊过什么」分开存，并且允许旧记录被新事实推翻。

> 个人项目，单机自用，默认只监听 `127.0.0.1`。代码与评测以「可核验」为先：失败要可见，评测要能复现，不把模型自报的分数当成结论。

---

## 它能做什么

- **四层记忆**：原文消息 → 滚动摘要 → 事实短句 + 向量 → 遗忘/作废/推翻。
- **写路径治理**：提炼时区分事实、世界书、状态槽位和约束；相似度去重；冲突时软删旧条目并保留归档；状态变化可触发「旧值作废」。
- **读路径**：按语义阈值召回长期记忆，叠加世界书、状态槽位、约束、今日/明日约定和最近窗口；过期约定不再冒充待办。
- **共同世界**：人格模板、世界书、用户画像、关系阶段、生活线、连续性话题分别落盘，互不越权。
- **人格成长**：演化先攒证据、进候选，够格才转正；删除/改写、一致性冲突和漂移拦截进待确认；每版留成长叙事。
- **多用户**：每个账号一套独立数据目录 `users/<id>/`，令牌鉴权 + LRU 实例缓存。
- **首次启动**：三步向导（模型 → 账号 → 可选人格卡片）；没有 Key 可进入只读演示。
- **模型设置**：网页填写地址/Key/模型、测试连接、脱敏显示；真实环境变量注入的 Key 在页面只读。
- **会话**：新建、切换、改名、删除；切回旧会话会加载历史消息，不重新生成开场白。
- **消息操作**：编辑、删除单条消息；重新生成最后一条助手回复。
- **搜索**：跨会话消息、长期记忆和世界书；点消息结果会切到对应会话并高亮具体气泡。
- **会话标题**：首条用户消息会自动固化为标题；手动改名优先，不会被覆盖。
- **备份**：画像页“全部数据”导出 ZIP（记忆库、人格、成长层、世界书、画像、关系、生活、情绪、头像、反馈；不含账号和 API Key）。
- **主动说话**：低频心跳 + 可数闸门（静默时段、每日上限、对话冷却），不会变成推送机器人。
- **感知**：天气、附近真实地点（高德）、情绪轨迹；未配置密钥时对应能力自动关闭。
- **可观测**：网页侧栏可查看关系、世界书、状态、时间轴、生活线、画像和记忆体检。
- **评测**：47 条记忆用例、13 条人格探针、12 组陪伴对照，支持重复跑和离线重评。

---

## 架构

```
浏览器（原生 HTML / Tailwind / ES Modules / SSE）
        │
FastAPI controllers ── chat / sessions / persona / settings / search / export
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

python web.py             # → http://127.0.0.1:8765
```

首次打开会按顺序引导：

1. 配置模型：填写主对话与 embedding 的 API 地址/Key，可先点“测试连接”。
2. 创建本机账号（密码至少 8 位）。
3. 可选：向导第 3 步直接导入人格卡片（也可以跳过，之后在「画像与人设」页导入）。

没有 API Key 时，首次页面还提供“先看看界面（演示）”：进入只读预览，能看到对话、世界书、状态、时间轴、生活、关系和人设页；配置模型后即可开始真实对话。

也可以继续用手工 `.env`（模板见 `.env.example`）；**真实环境变量 > `DATA_DIR/settings.json` > `.env`**。从网页保存的配置写在 `DATA_DIR/settings.json`，不会回写 `.env`，也不会被提交到仓库。

命令行版与网页版共用同一套 Service：

```bash
python openai_hello.py
```

### 配置

所有配置都支持环境变量覆盖。优先级：**真实环境变量 > `DATA_DIR/settings.json` > `.env` > 代码默认值**。最少需要：

| 变量 | 用途 |
|---|---|
| `WB_API_KEY` / `WB_BASE_URL` | 后台任务（提炼、摘要、画像、开场白）；Key 留空时复用 `WB_CHAT_API_KEY` |
| `WB_CHAT_API_KEY` / `WB_CHAT_BASE_URL` | 主对话；留空复用 `WB_API_KEY` |
| `WB_EMBED_API_KEY` / `WB_EMBED_BASE_URL` | embedding；缺了记忆召回会瘫痪 |
| `WB_MODEL` / `WB_EMBED_MODEL` / `WB_EMBED_DIMENSIONS` | 模型名与向量维度 |

可选能力：`WB_AMAP_KEY`（天气/附近）、`WB_EMOTION_*`（情绪）、`WB_TTS_*`（语音）。
完整列表见 [`.env.example`](.env.example) 与 [`docs/技术栈.md`](docs/技术栈.md)。

### 设置与密钥

- 网页「模型与密钥」可以配置主对话、后台任务和 embedding；保存时尝试热重建服务。
- Key 只以脱敏形式返回（如 `sk-1…cdef`），不会把明文重新显示在页面上。
- 如果真实环境变量已经提供某个值，该字段在页面只读；页面写入的 `DATA_DIR/settings.json` 不会覆盖它。
- 有对话或后台记忆正在写入时，保存会提示“重启后完全生效”，不会强行关闭正在使用的实例。
- 切换 embedding 模型或维度后，旧向量需要重建；不要把它当成无损热切换。

### 人格卡片

在「画像与人设」页可以导入：

- 旧识原生 JSON（`{"current": {...}}` 或扁平人设对象）；
- SillyTavern / character card v2 JSON（`{"spec": "chara_card_v2", "data": {...}}`）；
- 带 `chara`/`ccv3` 元数据的 PNG 角色卡。

支持拖拽或文件选择；导入前会展示当前人设、目标人设的完整内容、开场白、行为规则和世界书条目，并可选择是否同时导入世界书。确认后旧人格进入历史，可回滚；角色卡内的头像会保存为当前头像。`mes_example` 和扩展字段不会原样注入。导出支持旧识原生 JSON 和 SillyTavern v2 两种格式。

### Docker

```bash
docker build -t old-acquaintance .
docker run -p 8765:8765 --env-file .env -v wb-data:/data \
  -e WB_AUTH_REQUIRED=1 old-acquaintance
```

数据卷必须挂出来：记忆库、人设、画像、世界书、设置与日志都在 `/data`。
如果从**局域网/远程**打开首次设置页，管理接口默认会拒绝；请设置 `WB_ADMIN_TOKEN` 并让浏览器带上管理令牌，或先在服务器本机完成初始化。

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
python -m unittest tests.test_settings_and_persona
```

评测会真实调用模型，需要有效密钥：

```bash
python benchmark/run_bench.py --fresh --judge       # 47 条记忆用例
python benchmark/run_bench.py --repeat 8 --ids B11  # 单条重复跑
python benchmark/run_bench.py --rescore benchmark/results/<report>.json
python benchmark/run_persona.py                     # 13 条人格探针
```

评测结果不提交到仓库（`benchmark/results/` 已被忽略）。

### CI

GitHub Actions 会跑：

- `python -m compileall` 全量语法检查
- `ruff check --select E9,F63,F7,F82`（未定义名/语法类错误）
- `tests/test_pure.py` + 密码/身份/前端契约/设置与人格测试
- `node --check` 前端 JS + `npm run test:frontend`

Docker 构建和 Playwright 浏览器回归未接进 CI；浏览器脚本保留在 `tests/*-browser.mjs` 供本地运行。

---

## 目录结构

```
app/
  controllers/      chat / auth / settings / persona / sessions / search / export
  services/         对话编排、鉴权、worker、主动说话、TTS
  models/           记忆（含会话/消息）、世界书、槽位、生活、关系、连续性、人格、成长、画像
  utils/            LLM JSON、天气、地点、定位、文本相似度、设置/人格卡片解析
  views/            原生前端与已编译静态资源
benchmark/          记忆/人格/陪伴评测
tests/              Python + Node 测试
docs/               架构、技术栈、设计决策与调研
```

数据文件默认在项目根目录；设置 `DATA_DIR` 可整体迁移。多用户数据在 `DATA_DIR/users/<id>/`。
`DATA_DIR/settings.json` 保存网页写入的模型设置；`avatar.bin` 按用户数据目录保存导入的头像；两者都被 `.gitignore` 排除。

---

## 安全与隐私

- `.env`、`DATA_DIR/settings.json`、数据库、日志、用户目录、评测结果都被 `.gitignore` 忽略；**不要把密钥提交到仓库**。
- 网页设置只在本机/管理员可达；真实环境变量注入的 Key 在页面上只读。
- 默认只监听 `127.0.0.1`，适合本机使用。
- **对外提供服务前必须设置 `WB_AUTH_REQUIRED=1`**。默认值 0 是为了本机首次向导、只读演示和 CLI；在还没有账号的阶段，未强制鉴权的业务接口是敞开的。
- `/api/healthz` 只返回可写/密钥就绪等状态，不返回本机数据目录路径。
- Docker 默认监听 `0.0.0.0`，只应在受信任的网络中运行，或放在带鉴权的反向代理后。
- 密码使用随机盐 PBKDF2-HMAC-SHA256（600000 次）；令牌只存 SHA256 摘要。

---

## 已知限制

- 这是一个**单机 SQLite** 项目，没有做多进程并发写与水平扩展。
- 默认测试用假模型验证机制；真实模型下的记忆质量、自然度和 TTS 效果需要用自己的密钥复测。
- 人格卡片支持 JSON 和 PNG 内嵌元数据；SillyTavern 的 `mes_example` 和扩展字段不会原样注入。
- 编辑/删除消息只影响会话原文，不会回溯改写已经提炼出的长期记忆；重新生成会重跑最后一轮用户+助手，因此可能再提炼一次记忆（已有去重）。
- 天气、附近地点、情绪判定依赖第三方服务；未配置时自动关闭，不会编造结果。
- 语音默认开启并复用 embedding Key 走千问 TTS；如果 embedding 不是千问，请设置 `WB_TTS_ENABLED=0` 或单独配置 `WB_TTS_API_KEY`。
- 评测分数受模型随机性和用例污染影响；判断改动是否退化应看 `--repeat`，不要只看单次。
- 设计取舍与踩坑记录见 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

---

## License

MIT，详见 [LICENSE](LICENSE)。
