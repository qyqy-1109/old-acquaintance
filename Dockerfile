# 旧识 —— 最小可部署镜像
#
# 三个设计约束（都来自这个项目自身）：
#   1. 依赖只有 6 个包（见 requirements.txt），唯一的非标准库"新依赖"是 websockets。
#   2. **密钥绝不烘进镜像**：用 `--env-file .env` 或真正的环境变量传进来（见 .dockerignore）。
#   3. 数据目录必须挂出来：记忆库 / 人设 / 画像 / 日志都在里面，容器重建不能丢。
#
# 跑法：
#   docker build -t jiushi .
#   docker run -p 8765:8765 --env-file .env -v wb-data:/data jiushi
# 首次使用（没有 .env 时）：复制模板 `cp .env.example .env` 再填。
#
# ⚠ 对外提供服务时必须加 `-e WB_AUTH_REQUIRED=1`，否则"每用户一套数据"的隔离等于开着门。
# ⚠ 更要紧的是**建号之前**：不设 WB_AUTH_REQUIRED 时，业务接口在"还没有账号"的阶段是敞开的
#   （实测无身份 GET /api/state 返回 200）——**谁先访问端口，谁就是主人**（`#84`）。
#   加上 `WB_AUTH_REQUIRED=1` 之后，建号前业务接口也是 401，而 `POST /api/auth/setup`
#   与匿名 `GET /api/healthz` 仍可用（都实测过，见 `#84`）。本地自用无所谓，一上公网就必须加。
# ⚠ 多实例前请先确认 README「已知限制」里那两条边界（会话无状态 / SQLite 多读单写）。
#
# 已验证（`#87`）：`docker build` 成功（61s / 327MB）；`/app` 里**没有** .db/.json/.env（隐私数据没被烘进去）；
# 容器跑起来 `healthz ok=true / data_dir=/data / keys_ready=true`，页面与静态资源全部 200。
FROM python:3.13-slim

# 时区很重要：她的"现在几点"、以及「还有几天 / 已经过去几天」的时距计算都依赖本地时间。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8765

WORKDIR /app

# 先装依赖再拷代码：改代码时不必重装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY openai_hello.py ./
COPY README.md ./

# 数据卷：记忆库、人设、画像、世界书、情绪、日志都落在这里
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8765

# 用 uvicorn 直接起：跟 CLI 走同一套 Service 层（app.main:app 里的 lifespan 会预热默认用户）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
