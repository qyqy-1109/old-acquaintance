"""对话相关的 HTTP 路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import config
from app.controllers.schema import AckIn, ChatIn, CommandIn, PinIn, WorldIn
from app.controllers.schema import PromiseCompleteIn, PromiseIn, PromiseMissIn
from app.services.chat_service import ChatService, EmptyReply

router = APIRouter(prefix="/api", tags=["chat"])


def uid_of(request: Request) -> str:
    """解析这个请求属于哪个用户。**令牌优先**（服务端签发、可校验）。

    `AUTH_REQUIRED=1` 时没有有效令牌一律 401——**绝不退回默认用户**：
    "冒充失败反而进了门"比直接拒绝危险得多。
    未开鉴权时（默认，方便本地开发与 benchmark）仍接受 `X-User-Id`，但那只是权宜：
    它自报身份、任何人可冒充，所以每用一次都留一条 WARNING。
    """
    from app.log import get
    from app.services import auth

    token = auth.bearer_token(request.headers)
    authorization = (request.headers.get("Authorization") or "").strip().lower()
    if not token and (authorization == "bearer" or authorization.startswith("bearer ")):
        raise HTTPException(status_code=401,
                            detail="令牌为空：Authorization: Bearer <token>")
    if token:
        uid = auth.resolve(token)
        if uid:
            return uid
        raise HTTPException(status_code=401, detail="令牌无效")

    if auth.required():
        raise HTTPException(status_code=401,
                            detail="缺少令牌：Authorization: Bearer <token>")

    try:
        uid = auth.nickname_uid(request.headers)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="昵称编码或格式无效") from exc
    if uid:
        get("chat").warning("请求用**自报**的 X-User-Id=%s 进来（未开鉴权，可被冒充）", uid)
    return uid


def svc(request: Request) -> ChatService:
    """取该用户的实例，**并在这里做鉴权**。

    为什么鉴权必须留在这里：`uid_of()` 里那个 401 是唯一的门。优化 ① 曾把这里改成
    "直接读 middleware 放在 `request.state.uid` 的值"，结果**401 整条被绕过** ——
    middleware 只负责借用计数、不负责拒绝；而当时没有 API 层测试，回归是端到端探针
    抓到的（不带令牌请求 `/api/state` 返回了 200）。**安全路径不能靠"记得"**。

    middleware 解析 uid 是为了让"借用计数"提前生效；这里再解析一次是为了**拒绝**。
    重复一次可以忽略（一次 SQLite 查询），换来的是"门只有一道、谁都绕不过"。
    """
    uid = uid_of(request)
    return request.app.state.registry.get(uid) if uid else request.app.state.service


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """健康检查（部署必备）。

    刻意**不依赖模型可用性**：连不上模型只说明"降级可用"，不代表进程坏了；
    真把它算成不健康，编排系统会把整个实例摘掉——那是自伤。所以这里只看本地依赖：
    进程活着、数据目录可写、密钥有没有配齐。
    """
    import os

    from app.services import auth, registry

    return {
        "ok": True,
        "service": "旧识",
        "data_dir": config.DATA_DIR,
        "data_dir_writable": os.access(config.DATA_DIR, os.W_OK),
        "user_instances": registry.count(),          # 常驻实例数（见 WB_MAX_SERVICES）
        "auth_required": auth.required(),
        "password_login": True,
        "account_setup_required": not auth.has_accounts(),
        "legacy_data_available": auth.data_available(config.DATA_DIR),
        "nickname_encoding": "percent-encoded",     # 前端据此拒绝未加载中文昵称修复的旧进程。
        "keys_ready": config.keys_ready(),
        "keys": config.keys_status(),
        "first_run": (not config.keys_ready()) or (not auth.has_accounts()),
    }


def _admin_ok(request: Request) -> None:
    """管理接口的门槛：设了 `WB_ADMIN_TOKEN` 就必须带它；没设则**只允许本机**。

    为什么不搞"某个用户是管理员"的模型：现在还没有角色体系，硬造一个不如先用两个
    最小可行的门槛。等真有多角色需求再换 —— 而且这两个门槛在"本机开发"和
    "对外部署"两种场景下都够用。
    """
    import os

    want = os.environ.get("WB_ADMIN_TOKEN") or ""
    if want:
        got = request.headers.get("X-Admin-Token") or ""
        if got != want:
            raise HTTPException(status_code=403, detail="管理令牌不对（X-Admin-Token）")
        return
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403,
            detail="管理接口默认只允许本机访问；要在远程管理，请设置 WB_ADMIN_TOKEN")


@router.get("/users")
def list_users(request: Request) -> dict:
    """列出已有用户（管理接口）。"""
    _admin_ok(request)
    from app.services import auth, registry

    return {"users": auth.list_uids(),
            "cached_instances": registry.count(),
            "data_dir": config.DATA_DIR}


@router.post("/users")
def create_user(request: Request) -> dict:
    """给某个 uid 签发令牌（明文**只返回这一次**）。用法：`POST /api/users?uid=小林`。"""
    _admin_ok(request)
    from app.services import auth

    uid = (request.query_params.get("uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="用法：POST /api/users?uid=<名字>")
    try:
        token = auth.issue(uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"uid": uid, "token": token,
            "note": "令牌只显示这一次；之后请求带 Authorization: Bearer <token>"}


@router.delete("/users/{uid}")
def delete_user(uid: str, request: Request) -> dict:
    """删号：**吊销令牌 + 删数据目录**，两件必须一起做。

    为什么必须一起（`#069`）：「每人一套数据目录」删干净只需删一个目录，不用写级联
    删除也不会漏表 —— 但令牌还在账号库里，**旧令牌照样能进来，还会新建一套空数据
    继续用**。只做一半，等于"注销"没生效。
    """
    _admin_ok(request)
    import os
    import shutil

    from app.services import auth, registry

    uid = (uid or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 uid")
    try:
        with registry.exclusive(uid) as storage_uid:
            if not storage_uid:
                raise HTTPException(status_code=409,
                                    detail="此账号关联原有记忆，不能通过普通删号删除。")
            if not registry.drop(uid):
                raise HTTPException(status_code=409, detail="此账号正在使用，请稍后重试。")
            revoked = auth.revoke_all(uid)
            d = config.user_data_dir(storage_uid)
            shutil.rmtree(d, ignore_errors=True)
            # 删不干净必须**说出来**：数据合规最怕"以为删了其实没删"。
            # （踩过：close() 不关 SQLite 连接时，Windows 上 memory.db 删不掉，
            #   而 ignore_errors 把失败吞了 —— 接口却回了个"成功"。）
            left = os.listdir(d) if os.path.isdir(d) else []
            if left:
                raise HTTPException(
                    status_code=500,
                    detail=f"用户数据没删干净（残留 {len(left)} 项）：{d}；令牌已吊销")
            auth.delete_account(uid)
    except (ValueError, registry.ServiceBusy) as exc:
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"uid": uid, "revoked_tokens": revoked, "data_dir_removed": True}


@router.get("/state")
def state(request: Request) -> dict:
    """给前端渲染状态栏用：模型、性格、画像、记忆统计、世界书条目。"""
    return svc(request).state()


@router.get("/pending")
def pending(request: Request) -> dict:
    """前端轮询：她有没有主动说的话要给我（`#108`）。

    ⚠ **只读，不再"取走即标记"**（`#119`）：原协议下渲染失败/页面刷新/响应丢失
    都会让那条话永远消失，而库里只留下一个 `consumed_at`——用户实测撞到过
    （"主动说的话我一点都不知道"）。现在改成**至少一次投递**：
    客户端渲染成功后调 `POST /api/pending/ack` 确认，没确认的下轮还会给；
    前端按 id 去重（`seenSay`）。
    顺带在这里过一次闸门：用户刚打开页面 = 她有机会开口，不必等后台心跳。
    """
    service = svc(request)
    try:
        service.proactive_tick()
    except Exception:
        pass
    return {"items": service.take_pending_say()}


@router.post("/pending/ack")
def pending_ack(body: AckIn, request: Request) -> dict:
    """确认已送达（`#119`）：**只有这一步才把待发表标记成已读**。"""
    return {"acked": svc(request).ack_pending_say(body.ids)}


@router.delete("/life/events/{rid}")
def life_event_delete(rid: int, request: Request) -> dict:
    """看板隐藏流水并保留她的历史（`T91`）；不动记忆、世界书与此刻状态。

    为什么需要：她每一格做什么是**她自己选**的，于是会现编细节（真事：「阳台那盆薄荷」），
    而那些细节**不进任何记忆**（`#117` 查清）——用户不认的时候原来只能手改数据库。
    """
    return {"message": svc(request).delete_life_event(rid)}


@router.post("/life/pin")
def life_pin(body: PinIn, request: Request) -> dict:
    """看板：**把一句话钉进她的牵挂**（`life.json` 的 `cares`，`T91`）。

    钉进去 = 从下一格起这件事成为她的长期背景（而不是只在某一句里冒一下）。
    """
    return {"message": svc(request).pin_life_care(body.text)}


@router.get("/greeting")
def greeting(request: Request) -> dict:
    """每次页面打开/刷新调一次：按此刻最新的记忆重新打一句开场白。

    单独开一个接口，是为了跟 /api/state 分开——state 每轮对话后都会被拉一次，
    开场白不能跟着一起重新生成。
    """
    from app import config

    return {"greeting": svc(request).greeting(force=True),
            "needs_setup": not config.keys_ready()}


def _require_keys() -> None:
    if not config.keys_ready():
        raise HTTPException(
            status_code=503,
            detail="模型尚未配置：请先在设置页填写主对话和 embedding 的 API Key。",
        )


def _quota_gate(request: Request) -> str:
    """配额闸门。超了就 429，**并说清什么时候恢复**——只说"不行"用户只会以为她坏了。

    返回 uid（调用方在真的产出之后用它记一笔 `quota.spend`）。
    """
    from app.services import quota

    uid = getattr(request.state, "uid", "") or ""
    ok, why = quota.check(uid)
    if not ok:
        raise HTTPException(status_code=429, detail=why)
    return uid


@router.post("/chat")
def chat(body: ChatIn, request: Request) -> dict:
    """非流式一轮对话。"""
    from app.services import quota

    _require_keys()
    uid = _quota_gate(request)
    try:
        out = svc(request).chat(body.message)
    except EmptyReply as exc:
        # 空回复（`T93`）：`chat()` 已经重试过一次。这里**明确失败**，用与流式那条
        # **一模一样**的措辞（前端 `sse.js` 对 error 事件用的就是这句），
        # 而且**不花配额**——真产出之后才记账。
        raise HTTPException(status_code=502,
                            detail="回复生成失败，请稍后重试；本轮未保存。") from exc
    quota.spend(uid)
    return {
        "reply": out["reply"],
        # 多消息（`T86`）：`messages` 是切成 1~3 条的版本，`reply` 保留为兼容字段
        # （= 这几条按顺序拼起来的干净文本，**不含** `@@@`）。
        "messages": out.get("messages", [out["reply"]]),
        "recalled": out["recalled"],
        "memory_note": out["memory_note"],
        "profile_note": out["profile_note"],
    }


@router.post("/chat/stream")
def chat_stream(body: ChatIn, request: Request) -> StreamingResponse:
    """流式一轮对话，SSE 逐字推送。

    事件：meta（这次召回了什么）、delta（正文片段）、done（收尾 + 分段耗时）、
    audio / audio_end（语音，边合成边推）。
    注意：前端中途断开的话，这一轮不会被写进记忆——生成器会被直接关掉。
    """
    import time

    from app.services import quota

    _require_keys()
    uid = _quota_gate(request)
    service = svc(request)
    t0 = time.perf_counter()
    # `multi_message=True`：网页这条路径会切分（前端 `BubbleSplitter` 按 `@@@` 增量切），
    # 所以敢给她分条指令（`T86`）。CLI 与 benchmark 走 `prepare()` 的默认值，不给。
    try:
        messages, recalled = service.prepare(body.message, multi_message=True)
    except Exception as exc:
        from app.log import get

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        get("chat").exception("phase=prepare elapsed_ms=%d error_type=%s",
                              elapsed_ms, type(exc).__name__)
        raise
    prepare_ms = round((time.perf_counter() - t0) * 1000)

    def events():
        # meta 里先给"准备阶段"耗时：召回 + 组装，这一步不该被人感觉到
        payload = {"type": "meta", "recalled": recalled, "prepare_ms": prepare_ms}
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        chunks = []
        t_start = time.perf_counter()
        first_ms = None
        try:
            for text in service.stream(messages):
                if first_ms is None:
                    # **首字延迟（TTFT）**：思考发生在第一个可见字之前、内容不走 content 流，
                    # 所以这是用户唯一能感知的等待。选型时反复用的就是它，这里必须量下来。
                    first_ms = round((time.perf_counter() - t_start) * 1000)
                chunks.append(text)
                yield f"data: {json.dumps({'type': 'delta', 'text': text}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            from openai import APIConnectionError, APITimeoutError

            from app.log import get

            elapsed_ms = round((time.perf_counter() - t_start) * 1000)
            get("chat").exception(
                "phase=generate elapsed_ms=%d error_type=%s visible_chunks=%d",
                elapsed_ms, type(exc).__name__, len(chunks))
            connection_failed = isinstance(exc, (ConnectionError, APIConnectionError, APITimeoutError))
            message = ("模型连接失败，请稍后重试；本轮未保存。"
                       if connection_failed else "回复生成失败，请稍后重试；本轮未保存。")
            yield f"data: {json.dumps({'type': 'error', 'message': message}, ensure_ascii=False)}\n\n"
            return
        stream_ms = round((time.perf_counter() - t_start) * 1000)
        from app.utils import answers

        answer = "".join(chunks)
        cleaned_answer, did_strip = answers.strip_time_prefix(answer)
        if did_strip:
            answers.note_strip("chat_stream", answer[:40])
        # **一个字都没出来**（既没内容也没抛异常）→ 不能当成功（`T93`）：那样会
        # `observe_life_reply("")`、把空轮次丢给后台写记忆、花掉配额、还发 `done`，
        # 而用户眼前一片空白。走与上面异常分支同一句措辞，日志里报 `EmptyReply`。
        if not chunks or not str(cleaned_answer or "").strip():
            from app.log import get          # 与上面 except 分支同款局部导入（那边也是这么写的）
            get("chat").error(
                "phase=generate elapsed_ms=%d error_type=EmptyReply visible_chunks=%d",
                stream_ms, len(chunks))
            yield f"data: {json.dumps({'type': 'error', 'message': '回复生成失败，请稍后重试；本轮未保存。'}, ensure_ascii=False)}\n\n"
            return
        # 走到这里说明完整收到了正文。**写记忆挪进后台队列**（T61）：
        # 实测它是 4.5 秒（比首字还长），而正文早就上屏了，没必要让这个请求挂着等。
        # 队列保证**同一用户串行**（记忆顺序不会乱），退出时 main.py 会 flush（不丢轮次）。
        from app.services import worker
        uid = getattr(request.state, "uid", "") or ""
        delivery_id = service.stage_delivered_turn(body.message, cleaned_answer)
        try:
            worker.submit(uid, service.finish_turn, body.message, cleaned_answer,
                          delivery_id=delivery_id)
        except Exception as exc:
            service.discard_delivered_turn(delivery_id)
            from app.log import get
            get("chat").error("记忆任务提交失败：%s: %s", type(exc).__name__, exc)
            yield f"data: {json.dumps({'type': 'error', 'message': '回复未能提交保存，请稍后重试。'}, ensure_ascii=False)}\n\n"
            return
        service.observe_life_reply(cleaned_answer)
        quota.spend(uid)                 # 真产出之后才记一笔（中途断开不算）
        # 分段耗时随 done 一起给前端：以后要判断"慢在哪"，靠这几个数而不是猜。
        payload = {"type": "done",
                   "memory_note": "记忆在后台写入（关了页面也不会丢）",
                   "profile_note": "",
                   "memory_async": True,
                   "timing": {"prepare_ms": prepare_ms,
                              "first_token_ms": first_ms,
                              "stream_ms": stream_ms}}
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        # 语音合成：文字先上屏，声音**边合成边推**（T37）。
        # 实测服务端本来就是流式返回的（首字节 0.67s、整段 11s），所以这里也不能
        # 等收完再发——每收到一块就推一个 audio 事件，前端边收边播，首声才进得了 1 秒级。
        # 失败静默：speak_stream 内部已兜底，这里只处理事件组装。
        if config.TTS_ENABLED:
            try:
                import base64
                for chunk in service.speak_stream(body.message, cleaned_answer):
                    yield f"data: {json.dumps({'type': 'audio', 'wav_b64': base64.b64encode(chunk).decode()}, ensure_ascii=False)}\n\n"
                # 明确收尾事件：前端据此判断"这段音频给完了"（回退方案要等它才能拼）
                yield f"data: {json.dumps({'type': 'audio_end'}, ensure_ascii=False)}\n\n"
            except Exception as exc:
                from app.log import get as _get
                _get("chat").warning("audio 事件跳过：%s", exc)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/feedback")
def feedback(body: dict, request: Request) -> dict:
    """「她说错了」：把上一轮的完整现场追加到 feedback.jsonl。"""
    return {"output": svc(request).mark_wrong(str(body.get("note", "")))}


@router.post("/command")
def command(body: CommandIn, request: Request) -> dict:
    """执行 /mem /world /clear 这类命令，返回要显示的文本。"""
    service = svc(request)
    command_head = body.command.strip().split(" ", 1)[0].lower()
    if command_head in ("/timeline", "/时间轴", "/slots", "/状态"):
        output = service.run_command(body.command,
                                     user_name=uid_of(request) or None)
    else:
        output = service.run_command(body.command)
    result = {"output": output}
    if body.command.strip().lower() in ("/mem check", "/mem 对账"):
        result["memory_issues"] = service.memory_issues
        result["memory_check_error"] = service.memory_check_error
    return result


@router.get("/world")
def world_list(request: Request) -> dict:
    return {"entries": svc(request).world.entries}


@router.post("/world")
def world_add(body: WorldIn, request: Request) -> dict:
    return {"output": svc(request).world.add(body.text)}


@router.delete("/world/{index}")
def world_remove(index: int, request: Request) -> dict:
    entries = svc(request).world.entries
    if not (0 <= index < len(entries)):
        raise HTTPException(status_code=404, detail=f"没有第 {index + 1} 条")
    return {"output": svc(request).world.remove(index)}


@router.get("/life")
def life_board(request: Request) -> dict:
    """「她做过什么」看板（`#117`）：她的状态、生活流水、她说过的原话，**每条都带出处**。"""
    return svc(request).life_board()


@router.get("/relationship")
def relationship_board(request: Request) -> dict:
    service = svc(request)
    return service.relationship.board(user_msgs=service._user_msg_count())


@router.post("/relationship/promises")
def relationship_promise_create(body: PromiseIn, request: Request) -> dict:
    row = svc(request).relationship.create_promise(body.text, body.due_at)
    return {"promise": row, "message": f"记下了，她答应过：{row['text']}"}


def _promise_update(request: Request, promise_id: str, status: str,
                    note: str = "", reason: str = "") -> dict:
    try:
        row = svc(request).relationship.update_promise(promise_id, status, note, reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"promise": row, "message": "承诺已完成" if status == "done" else "已记下失约原因"}


@router.post("/relationship/promises/{promise_id}/complete")
def relationship_promise_complete(promise_id: str, body: PromiseCompleteIn, request: Request) -> dict:
    return _promise_update(request, promise_id, "done", note=body.note)


@router.post("/relationship/promises/{promise_id}/miss")
def relationship_promise_miss(promise_id: str, body: PromiseMissIn, request: Request) -> dict:
    return _promise_update(request, promise_id, "missed", reason=body.reason)


@router.post("/clear")
def clear(request: Request) -> dict:
    return {"output": svc(request).run_command("/clear")}


@router.post("/forget")
def forget(request: Request) -> dict:
    return {"output": svc(request).run_command("/forget")}
