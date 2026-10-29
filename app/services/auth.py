# -*- coding: utf-8 -*-
"""鉴权：服务端签发、可校验的令牌（多用户的「门锁」）。

**为什么不能直接用 `X-User-Id`**：那个头是客户端自报的，谁都能填别人的 id 去读别人的
记忆。做完数据隔离（`#061`）之后，这是最后一道还没上的锁。

设计上的三个取舍：
1. **只存哈希**：库里存 `sha256(token)`，不是明文——库被拖走也不能直接拿来登录。
2. **无效一律 `None`**，绝不"解析不出来就退回默认用户"：那等于冒充失败反而进了门。
3. **每次操作开关连接**：鉴权是低频动作（登录一次用很久），不值得维护长连接；
   顺带也让测试能随时改 `DATA_DIR`（与 `ChatService` 的做法一致）。

用法：
    python -m app.services.auth issue <uid>     # 签发（明文只显示这一次）
    python -m app.services.auth list            # 列出已有用户
    python -m app.services.auth revoke <token>  # 注销某个 token
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

from app import config
from app.log import get

log = get("auth")

TOKEN_BYTES = 32          # token_urlsafe(32) ≈ 43 个字符，够随机
SESSION_SECONDS = 7 * 24 * 3600
PASSWORD_ITERATIONS = 600_000
_DUMMY_PASSWORD = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_uid ON tokens(uid);
CREATE TABLE IF NOT EXISTS accounts (
    uid TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    data_uid TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""


class AccountConflict(ValueError):
    pass


def bearer_token(headers: Mapping[str, str]) -> str:
    value = headers.get("Authorization") or ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


_PCT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def nickname_uid(headers: Mapping[str, str]) -> str:
    """还原浏览器编码的昵称（`X-User-Id`）。

    两种都要还原（T70-②）：
    · 带 `X-User-Id-Encoding: percent-encoded` 的——按约定还原，非法 UTF-8 交给调用方报 400；
    · **不带标记、但解出来是「非 ASCII」的**——旧客户端不会带标记，若按原值当昵称，
      同一个人的数据会落到第二个目录（实测 `users/小林` 与 `users/%E5%A5%B6%E9%BE%99` 并存，
      见 `#84`）。这一条是修掉那个分裂的根因。
    边界（刻意收窄，免得把真名改坏）：不带标记时，**只有解出来含非 ASCII 字符才还原**——
    中文/emoji 昵称被编码就是这种形态；而 `%41%42`（解出来是 "AB"）这类按原值保留，
    因为真昵称里完全可能带百分号。解不开的（截断 UTF-8）也按原值返回，不把旧客户端打成 400。
    ⚠ 这条**替换了 `#79` 的一条契约**（"未带标记一律按原值"），按上面的实测分裂证据改的；
    `tests/test_identity_transport.py` 的对应用例已同步更新。
    """
    uid = (headers.get("X-User-Id") or "").strip()
    if not uid:
        return ""
    if headers.get("X-User-Id-Encoding") == "percent-encoded":
        return _username(unquote(uid, encoding="utf-8", errors="strict"))
    if _PCT_ESCAPE.search(uid):
        try:
            decoded = unquote(uid, encoding="utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return _username(uid)
        if any(ord(ch) > 127 for ch in decoded):
            return _username(decoded)
    return _username(uid)


def db_path() -> str:
    """账号库路径。**每次现算**：这样测试能临时改 DATA_DIR（与 ChatService 一致）。"""
    return os.path.join(config.DATA_DIR, "accounts.db")


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=10.0)
    con.executescript(SCHEMA)
    if "expires_at" not in {row[1] for row in con.execute("PRAGMA table_info(tokens)")}:
        try:
            con.execute("ALTER TABLE tokens ADD COLUMN expires_at REAL")
            con.commit()
        except sqlite3.OperationalError:
            # Another process may have completed the same additive migration.
            if "expires_at" not in {row[1] for row in con.execute("PRAGMA table_info(tokens)")}:
                con.close()
                raise
    return con


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(uid: str, *, note: str = "", ttl_seconds: int | None = None) -> str:
    """给某个用户签发一个新 token，返回**明文**（只在此刻可见这一次）。"""
    uid = _username(uid)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = time.time()
    expires_at = now + ttl_seconds if ttl_seconds is not None else None
    con = _connect()
    try:
        with con:
            con.execute("INSERT INTO tokens(token_hash,uid,created_at,expires_at) VALUES(?,?,?,?)",
                        (_hash(token), uid, now, expires_at))
    finally:
        con.close()
    log.info("已为用户 %s 签发 token%s", uid, f"（{note}）" if note else "")
    return token


def resolve(token: str) -> str | None:
    """把 token 解析成 uid；**认不出来就返回 None**（绝不退回默认用户）。"""
    token = (token or "").strip()
    if not token:
        return None
    con = _connect()
    try:
        row = con.execute("SELECT uid FROM tokens WHERE token_hash=? "
                          "AND (expires_at IS NULL OR expires_at>?)",
                          (_hash(token), time.time())).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def revoke(token: str) -> bool:
    """注销一个 token。返回是否真的删掉了（调用方据此给反馈）。"""
    con = _connect()
    try:
        with con:
            cur = con.execute("DELETE FROM tokens WHERE token_hash=?", (_hash(token),))
        return cur.rowcount > 0
    finally:
        con.close()


def revoke_all(uid: str) -> int:
    """吊销某个用户的**全部**令牌（删号时用），返回吊销了几条。

    删号必须连令牌一起吊销：我们每人一套数据目录，删目录只清了数据，
    而旧令牌还在账号库里 —— 拿着它照样能进来，还会**新建一套空数据**继续用。
    """
    uid = (uid or "").strip()
    if not uid:
        return 0
    con = _connect()
    try:
        with con:
            cur = con.execute("DELETE FROM tokens WHERE uid=?", (uid,))
        if cur.rowcount:
            log.info("已为 %s 吊销 %d 个令牌", uid, cur.rowcount)
        return cur.rowcount
    finally:
        con.close()


def list_uids() -> list[str]:
    con = _connect()
    try:
        return [r[0] for r in con.execute("SELECT uid FROM accounts UNION SELECT uid FROM tokens ORDER BY uid")]
    finally:
        con.close()


def has_accounts() -> bool:
    with closing(_connect()) as con:
        return con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None


def required() -> bool:
    return config.AUTH_REQUIRED or has_accounts() or has_tokens()


def has_tokens() -> bool:
    with closing(_connect()) as con:
        return con.execute("SELECT 1 FROM tokens LIMIT 1").fetchone() is not None


def storage_uid(uid: str) -> str:
    if not uid:
        return ""
    uid = _username(uid)
    with closing(_connect()) as con:
        row = con.execute("SELECT data_uid FROM accounts WHERE uid=?", (uid,)).fetchone()
    return row[0] if row else uid


def data_available(directory: str | Path) -> bool:
    """Check real records, ignoring automatically generated empty templates.

    An assistant greeting is bootstrap UI state, not evidence that an account has
    usable history. Other local records are conservative: once a user has written
    or accepted state, claiming a different data set must stop rather than hide it.
    """
    directory = Path(directory)
    if any(directory.glob("personality.json.corrupt*")) or any(directory.glob("soul.json.corrupt*")):
        return True
    for filename, keys in (("world.json", ("entries",)),
                           ("user_profile.json", ("summary", "facts", "preferences", "topics",
                                                  "expertise", "communication"))):
        path = directory / filename
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                return True
            if not isinstance(data, dict) or any(data.get(key) for key in keys):
                return True
    from app.models.life import DEFAULT_LIFE
    from app.models.personality import DEFAULT_PERSONALITY
    from app.models.relationship import DEFAULT_RELATIONSHIP

    defaults = {"life.json": DEFAULT_LIFE,
                "personality.json": {"current": DEFAULT_PERSONALITY, "history": []},
                "relationship.json": {**DEFAULT_RELATIONSHIP, "events": [], "history": []}}
    for filename, default in defaults.items():
        path = directory / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return True
        if not isinstance(data, dict):
            return True
        if filename == "relationship.json":
            data = {**default, **data}
            data.pop("since", None)
            data.pop("hint", None)
            default = {key: value for key, value in default.items() if key not in ("since", "hint")}
        if data != default:
            return True
    for filename in ("soul.json", "emotion.json", "personality.json.pending.json"):
        path = directory / filename
        if path.exists():
            try:
                if json.loads(path.read_text(encoding="utf8")):
                    return True
            except (OSError, ValueError):
                return True
    feedback = directory / "feedback.jsonl"
    if feedback.exists():
        try:
            if any(line.strip() for line in feedback.read_text(encoding="utf8").splitlines()):
                return True
        except (OSError, ValueError):
            return True
    path = directory / "memory.db"
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "memories" in tables and con.execute("SELECT 1 FROM memories LIMIT 1").fetchone():
                    return True
                if "messages" in tables and con.execute(
                        "SELECT 1 FROM messages WHERE COALESCE(content, '') <> '' "
                        "AND COALESCE(greeting, 0)=0 LIMIT 1").fetchone():
                    return True
                for table in ("slots", "life_events", "pending_say", "feedback_corrections", "opinions", "events"):
                    if table in tables and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                        return True
                for table in ("summaries", "pending"):
                    if table in tables and con.execute(
                            f"SELECT 1 FROM {table} WHERE COALESCE(text, '') <> '' LIMIT 1").fetchone():
                        return True
                if "life_state" in tables and con.execute(
                        "SELECT 1 FROM life_state WHERE COALESCE(activity, '') <> '' "
                        "OR COALESCE(on_mind, '') <> '' LIMIT 1").fetchone():
                    return True
        except (OSError, sqlite3.DatabaseError):
            return True
    return False


def legacy_status(uid: str) -> dict[str, str | bool]:
    """Return whether an independent account may claim the root data set."""
    uid = (uid or "").strip()
    if not uid:
        raise ValueError("缺少账号")
    with closing(_connect()) as con:
        row = con.execute("SELECT data_uid FROM accounts WHERE uid=?", (uid,)).fetchone()
        root_owner = con.execute("SELECT 1 FROM accounts WHERE data_uid='' LIMIT 1").fetchone()
    storage = row[0] if row else uid
    account_dir = Path(config.DATA_DIR) / "users" / uid
    available = bool(row and storage == uid and not root_owner
                     and not data_available(account_dir)
                     and data_available(config.DATA_DIR))
    return {"available": available, "storage_uid": storage}


def claim_legacy(uid: str) -> bool:
    """Atomically bind an empty account to the root data set."""
    uid = (uid or "").strip()
    if not uid:
        raise ValueError("缺少账号")
    account_dir = Path(config.DATA_DIR) / "users" / uid
    with closing(_connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data_uid FROM accounts WHERE uid=?", (uid,)).fetchone()
        if not row:
            raise AccountConflict("当前账号不能认领这套旧数据：账号已有数据、旧数据已被认领，或根目录没有可用历史。")
        if row[0] == "":
            return True
        root_owner = con.execute("SELECT 1 FROM accounts WHERE data_uid='' AND uid<>? LIMIT 1",
                                 (uid,)).fetchone()
        if (row[0] != uid or root_owner or data_available(account_dir)
                or not data_available(config.DATA_DIR)):
            raise AccountConflict("当前账号不能认领这套旧数据：账号已有数据、旧数据已被认领，或根目录没有可用历史。")
        con.execute("UPDATE accounts SET data_uid='' WHERE uid=? AND data_uid=?", (uid, uid))
    return True


def _username(uid: str) -> str:
    return config.validate_user_id(uid)


def _password_hash(password: str) -> str:
    if not 8 <= len(password) <= 128:
        raise ValueError("密码需为 8 至 128 个字符。")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def create_account(uid: str, password: str, *, inherit_default: bool = False,
                   first_only: bool = False) -> str:
    uid = _username(uid)
    encoded = _password_hash(password)
    if inherit_default and data_available(Path(config.DATA_DIR) / "users" / uid):
        raise AccountConflict("此账号已有独立记忆，不能直接绑定另一套数据。")
    with closing(_connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        if first_only and con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            raise AccountConflict("账号已初始化，请使用账号密码登录。")
        # Windows paths ignore case; distinct account names must not share a directory.
        key = unicodedata.normalize("NFC", uid).casefold()
        known = con.execute("SELECT uid FROM accounts UNION SELECT uid FROM tokens").fetchall()
        if any(other != uid and unicodedata.normalize("NFC", other).casefold() == key for (other,) in known):
            raise AccountConflict("已有同名账号，请使用原账号名称。")
        try:
            con.execute("INSERT INTO accounts(uid,password_hash,data_uid,created_at) VALUES(?,?,?,?)",
                        (uid, encoded, "" if inherit_default else uid, time.time()))
        except sqlite3.IntegrityError as exc:
            raise AccountConflict("账号已存在或原有记忆已关联到其他账号。") from exc
    return uid


def authenticate(uid: str, password: str) -> tuple[str, str] | None:
    """Verify credentials and issue a session only if the password is still current."""
    with closing(_connect()) as con:
        row = con.execute("SELECT uid,password_hash FROM accounts WHERE uid=?", (uid.strip(),)).fetchone()
    valid = _verify_password(password, row[1] if row else _DUMMY_PASSWORD)
    if not row or not valid:
        return None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = time.time()
    with closing(_connect()) as con, con:
        # The conditional insert is atomic with password reset and token revocation.
        result = con.execute("INSERT INTO tokens(token_hash,uid,created_at,expires_at) "
                             "SELECT ?,uid,?,? FROM accounts WHERE uid=? AND password_hash=?",
                             (_hash(token), now, now + SESSION_SECONDS, row[0], row[1]))
        if not result.rowcount:
            return None
    log.info("已为用户 %s 签发登录会话", row[0])
    return row[0], token


def set_password(uid: str, password: str) -> None:
    encoded = _password_hash(password)
    with closing(_connect()) as con, con:
        result = con.execute("UPDATE accounts SET password_hash=? WHERE uid=?", (encoded, uid.strip()))
        if not result.rowcount:
            raise ValueError("账号不存在。")
        con.execute("DELETE FROM tokens WHERE uid=?", (uid.strip(),))


def delete_account(uid: str) -> None:
    with closing(_connect()) as con, con:
        con.execute("DELETE FROM accounts WHERE uid=?", (uid,))


def _main(argv: list[str]) -> int:
    """Local administrator account and token commands."""
    if not argv:
        print("用法：python -m app.services.auth account <账号> [--inherit-default] | password <账号> | issue <uid> | list | revoke <token>")
        return 2
    cmd = argv[0]
    if cmd in ("account", "password") and len(argv) >= 2:
        import getpass
        password = getpass.getpass("密码（至少 8 位）：")
        if password != getpass.getpass("确认密码："):
            print("两次密码不一致。")
            return 2
        try:
            if cmd == "account":
                create_account(argv[1], password, inherit_default="--inherit-default" in argv[2:])
            else:
                set_password(argv[1], password)
        except ValueError as exc:
            print(str(exc))
            return 2
        print("账号已创建，可在页面用账号密码登录。" if cmd == "account" else "密码已更新，旧令牌已吊销。")
        return 0
    if cmd == "issue" and len(argv) >= 2:
        token = issue(argv[1])
        print(f"已为用户 {argv[1]} 签发令牌（**只显示这一次**，请立刻保存）：")
        print(f"\n  {token}\n")
        print(f"之后请求带上：Authorization: Bearer {token}")
        return 0
    if cmd == "list":
        uids = list_uids()
        print("\n".join(uids) if uids else "（还没有任何用户）")
        return 0
    if cmd == "revoke" and len(argv) >= 2:
        print("已注销" if revoke(argv[1]) else "没找到这个 token")
        return 0
    print("用法：python -m app.services.auth account <账号> [--inherit-default] | password <账号> | issue <uid> | list | revoke <token>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
