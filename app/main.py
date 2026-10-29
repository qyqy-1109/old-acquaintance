"""应用装配：把 Controller 挂上，把前端页面托管出去。

运行：
    python web.py          然后浏览器打开 http://127.0.0.1:8765
或：
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config
from app.controllers.chat_controller import router
from app.controllers.auth_controller import router as auth_router
from app.controllers.export_controller import router as export_router
from app.controllers.persona_controller import router as persona_router
from app.controllers.session_controller import router as session_router
from app.controllers.settings_controller import router as settings_router
from app.log import get
from app.services.chat_service import ChatService

log = get("main")


def _start_proactive_beat(app) -> "threading.Event":
    """起一个低频心跳线程，替她判断"现在想不想说话"（`#108`）。

    为什么用线程而不是 asyncio 任务：`ChatService` 全是同步代码（OpenAI SDK + sqlite），
    放线程里最省事、也跟现有 `services/worker.py` 的做法一致。
    线程是 daemon，且**每一圈都包 try/except**——主动说话永远不能拖垮服务。
    """
    import threading

    stop = threading.Event()

    def loop():
        interval = max(60, int(config.PROACTIVE_EVERY or 20) * 60)
        # 启动后先等一会儿再第一次——刚起来时用户在配置/登录，不该立刻收到消息
        if stop.wait(min(interval, 120)):
            return
        while not stop.is_set():
            try:
                from app.services import registry
                for svc in registry.all_services():
                    if stop.is_set():
                        break
                    with registry.borrow_cached(svc) as held:
                        if held is not None:
                            held.proactive_tick()
            except Exception as exc:
                log.warning("主动说话心跳异常（继续跑）：%s: %s", type(exc).__name__, exc)
            stop.wait(interval)

    threading.Thread(target=loop, name="proactive-beat", daemon=True).start()
    if config.PROACTIVE:
        log.info("主动说话已开启：每 %d 分钟过一遍闸门（一天最多 %d 次，静默 %s）",
                 config.PROACTIVE_EVERY, config.PROACTIVE_DAILY_MAX, config.PROACTIVE_QUIET)
    else:
        log.info("主动说话已关闭（WB_PROACTIVE=0）")
    return stop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务实例按用户建、按需缓存（形态：每人一个她）。

    启动时只预热**默认用户**那一个（`user_id=""`，沿用根数据目录）——CLI、benchmark、
    单用户部署的行为因此完全不变；带 `X-User-Id` 的请求来了再按需建对应用户的实例
    （见 `app/services/registry.py`，带 LRU 上限）。
    """
    from app.services import registry

    app.state.registry = registry
    app.state.service = registry.get("")        # 默认用户；探针与单用户部署仍走它
    if app.state.service.sync_note:
        log.info(app.state.service.sync_note)
    # 她主动说话的**后台心跳**（`#108`）：低频（默认 20 分钟）过一遍闸门，空闲时也走。
    # ⚠ 这是本项目第一次引入"服务端主动发起"：线程是 daemon、异常全吞并只记日志，
    # 而且**闸门在实体里**（一天 ≤3 条、夜里不说、正在聊不插嘴），所以它不会变成推送机器人。
    # `WB_PROACTIVE=0` 关掉。
    stop_beat = _start_proactive_beat(app)
    yield
    stop_beat.set()
    # 先等后台队列清空（`T61`）：写记忆已经挪到后台，**不清空就退出会丢最后几轮**。
    from app.services import worker
    if not worker.flush(timeout=60):
        log.warning("后台队列 60s 内没清空，仍有 %d 件未完成（这几轮的记忆可能会丢）",
                    worker.pending())
    # 退出兜底：**每个用户**都把上次演化之后的新对话收尾演化掉，再统一落盘关闭。
    # 平时每 EVOLVE_EVERY 轮已经小步演化过，这里通常没有新东西，不空跑模型。
    for svc in registry.all_services():
        try:
            svc.run_evolve_pending()
        except Exception as exc:
            log.error("退出演化失败（继续关闭）：%s", exc)
    registry.close_all()


app = FastAPI(title="旧识", description="MVC+S 分层后的本地对话助手",
              lifespan=lifespan)


class HoldUserServiceMiddleware:
    """Retain the user's service through the response body and background tasks.

    Identity validation remains in the controllers; this middleware only owns
    the cached instance until the complete ASGI request finishes or fails.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from app.services import auth, registry

        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path.startswith("/api/auth/") \
                or path.startswith("/api/settings"):
            return await self.app(scope, receive, send)
        request = Request(scope)
        uid = ""
        token = auth.bearer_token(request.headers)
        if token:
            uid = auth.resolve(token) or ""
        elif not auth.required():
            try:
                uid = auth.nickname_uid(request.headers)
            except (UnicodeDecodeError, ValueError):
                uid = ""
        if not uid:
            return await self.app(scope, receive, send)
        held_uid = registry.acquire(uid)
        try:
            request.state.uid = uid
            await self.app(scope, receive, send)
        finally:
            registry.release(held_uid)


app.add_middleware(HoldUserServiceMiddleware)


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    """静态资源每次都要**回服务器校验**（`no-cache`），别让浏览器按启发式缓存吃住旧 JS/CSS。

    踩过（2026-09-30）：新加的「她的生活」页一直显示"正在读取…"——接口和文件都没问题，
    是浏览器用了**旧的 `app.js`**：`/` 有 `no-store`，而 `/static/*` 原来**一个
    `Cache-Control` 都没有**（只有 ETag / Last-Modified），浏览器于是按"启发式新鲜度"
    直接用缓存、**连一次校验请求都不发**。表现就是**新 HTML + 旧 JS**：
    新导航项在、点进去却什么都不加载（页面上永远停在 HTML 里的占位字）。

    `no-cache`（不是 `no-store`）才是这里要的：**可缓存，但每次必须校验**——
    没变回 304，变了拿新的；代价只有一次条件请求。
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.include_router(router)
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(persona_router)
app.include_router(session_router)
app.include_router(export_router)
app.mount("/static", StaticFiles(directory=config.VIEW_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    # 明确禁止缓存：这个页面是边做边改的，浏览器一旦缓存住旧版，
    # 就会出现「代码改了、页面还是老的」，排查起来很误导人。
    return FileResponse(os.path.join(config.VIEW_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 浏览器每次开页面都会默认请求这个路径，不给就天天刷一条 404。
    # 内联 SVG，不额外放文件；同时 index.html 里有 <link rel="icon">，
    # 两个都留着，老标签页和新标签页都不会再打 404。
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<text y="52" font-size="52">🐱</text></svg>')
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/api/health", tags=["chat"])
def health() -> dict:
    return {"ok": True, "model": config.MODEL}
