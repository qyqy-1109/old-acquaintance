"""Password session endpoints; credentials are verified on the server."""

from collections import OrderedDict
from threading import Lock
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from app import config
from app.controllers.chat_controller import _admin_ok
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])
_attempts = OrderedDict()
_attempt_lock = Lock()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: SecretStr


class SetupIn(LoginIn):
    inherit_default: bool = False


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() != str(request.base_url).rstrip("/").lower():
        raise HTTPException(status_code=403, detail="请从本服务的登录页面提交。")


def _session(session: tuple[str, str], response: Response) -> dict:
    username, token = session
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": token,
            "token_type": "bearer", "expires_in": auth.SESSION_SECONDS, "username": username}


def _identity(request: Request) -> str:
    uid = auth.resolve(auth.bearer_token(request.headers))
    if not uid:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    return uid


@router.post("/setup", status_code=201)
def setup(body: SetupIn, request: Request, response: Response) -> dict:
    _same_origin(request)
    _admin_ok(request)
    try:
        username = auth.create_account(body.username, body.password.get_secret_value(),
                                       inherit_default=body.inherit_default, first_only=True)
    except auth.AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = auth.authenticate(username, body.password.get_secret_value())
    if not session:
        raise HTTPException(status_code=409, detail="账号已变更，请使用当前密码登录。")
    return _session(session, response)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    _same_origin(request)
    password = body.password.get_secret_value()
    if len(password) > 128:
        raise HTTPException(status_code=401, detail="账号或密码不正确。")
    key = (config.DATA_DIR, request.client.host if request.client else "", body.username.strip())
    now = time.monotonic()
    with _attempt_lock:
        count, until = _attempts.get(key, (0, now + 60))
        if until <= now:
            count, until = 0, now + 60
        if count >= 5:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试。",
                                headers={"Retry-After": str(max(1, int(until - now)))})
        _attempts[key] = (count + 1, until)
        _attempts.move_to_end(key)
        while len(_attempts) > 512:
            _attempts.popitem(last=False)
    session = auth.authenticate(body.username, password)
    if not session:
        raise HTTPException(status_code=401, detail="账号或密码不正确。")
    with _attempt_lock:
        _attempts.pop(key, None)
    return _session(session, response)


@router.get("/me")
def me(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"username": _identity(request)}


@router.get("/legacy")
def legacy_status(request: Request, response: Response) -> dict:
    """Tell the signed-in account whether the root history can be claimed."""
    response.headers["Cache-Control"] = "no-store"
    uid = _identity(request)
    return auth.legacy_status(uid)


@router.post("/legacy")
def claim_legacy(request: Request) -> dict:
    """Bind an empty account to root history after an explicit user action."""
    _same_origin(request)
    uid = _identity(request)
    from app.services import registry

    if auth.storage_uid(uid) == "":
        return {"claimed": True, "storage_uid": ""}
    try:
        with registry.exclusive(uid):
            status = auth.legacy_status(uid)
            if status["storage_uid"] == "":
                return {"claimed": True, "storage_uid": ""}
            if not status["available"]:
                raise auth.AccountConflict("当前账号不能认领这套旧数据。")
            if not registry.drop(uid):
                raise registry.ServiceBusy("账号实例暂时无法关闭，请稍后重试。")
            auth.claim_legacy(uid)
    except registry.ServiceBusy as exc:
        if auth.storage_uid(uid) == "":
            return {"claimed": True, "storage_uid": ""}
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except auth.AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"claimed": True, "storage_uid": ""}


@router.post("/logout")
def logout(request: Request) -> dict:
    _same_origin(request)
    _identity(request)
    auth.revoke(auth.bearer_token(request.headers))
    return {"ok": True}
