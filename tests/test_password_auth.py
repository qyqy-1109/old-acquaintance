"""Password authentication and legacy identity tests without model calls."""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi.testclient import TestClient
from app import config
from app.main import app
from app.services import auth, registry

NAME = "\u5976\u9f99"
PASSWORD = "a-good-password"


class LocalService:
    def __init__(self, user_id=""):
        self.user_id = user_id
        self.data_dir = Path(config.DATA_DIR) / "users" / user_id if user_id else Path(config.DATA_DIR)

    def state(self):
        world = self.data_dir / "world.json"
        entries = json.loads(world.read_text("utf8")).get("entries", []) if world.exists() else []
        return {"world_count": len(entries), "world": entries, "storage_uid": self.user_id}

    def close(self):
        pass


class PasswordAuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(config, "DATA_DIR", str(self.root)))
        self.enterContext(patch.object(config, "AUTH_REQUIRED", False))
        self.enterContext(patch.dict("os.environ", {"WB_ADMIN_TOKEN": ""}))
        self.enterContext(patch.object(registry, "_services", OrderedDict()))
        self.enterContext(patch.object(registry, "_refs", {}))
        self.enterContext(patch("app.services.chat_service.ChatService", LocalService))
        self.enterContext(patch.object(app.state, "registry", registry, create=True))
        self.enterContext(patch.object(app.state, "service", registry.get(""), create=True))
        self.client = TestClient(app, client=("127.0.0.1", 12345))
        self.addCleanup(self.client.close)

    def setup_account(self, name=NAME, inherit=False):
        response = self.client.post("/api/auth/setup", json={
            "username": name, "password": PASSWORD, "inherit_default": inherit,
        })
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["username"], name)
        self.assertEqual(data["token_type"], "bearer")
        self.assertEqual(data["expires_in"], 7 * 24 * 3600)
        return data["access_token"]

    def login(self, name=NAME, password=PASSWORD):
        return self.client.post("/api/auth/login", json={"username": name, "password": password})

    @staticmethod
    def bearer(token):
        return {"Authorization": "Bearer " + token}

    def test_login_returns_verified_identity_and_enforces_auth(self):
        token = self.setup_account()
        self.assertEqual(self.client.get("/api/auth/me", headers=self.bearer(token)).json()["username"], NAME)
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/api/state", headers={"X-User-Id": "someone"}).status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()["access_token"], token)
        self.assertEqual(auth.resolve(response.json()["access_token"]), NAME)
        self.assertTrue(self.client.get("/api/healthz").json()["auth_required"])

    def test_wrong_and_unknown_credentials_have_same_error(self):
        self.setup_account()
        wrong = self.login(password="wrong-password")
        unknown = self.login(name="unknown-account")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())
        self.assertNotIn(PASSWORD, wrong.text)

    def test_passwords_are_salted_and_tokens_are_hashed(self):
        token = self.setup_account()
        auth.create_account("second-user", PASSWORD)
        with closing(sqlite3.connect(auth.db_path())) as con, con:
            hashes = [row[0] for row in con.execute("SELECT password_hash FROM accounts")]
            token_hash = con.execute("SELECT token_hash FROM tokens").fetchone()[0]
        self.assertEqual(len(hashes), 2)
        self.assertNotEqual(hashes[0], hashes[1])
        self.assertTrue(all(value.startswith("pbkdf2_sha256$600000$") for value in hashes))
        self.assertTrue(all(PASSWORD not in value for value in hashes))
        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())

    def test_expired_token_is_rejected_and_legacy_token_remains_compatible(self):
        old = auth.issue(NAME)
        token = self.setup_account()
        with closing(sqlite3.connect(auth.db_path())) as con, con:
            con.execute("UPDATE tokens SET expires_at=0 WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        self.assertEqual(self.client.get("/api/auth/me", headers=self.bearer(token)).status_code, 401)
        self.assertEqual(auth.resolve(old), NAME)

    def test_logout_revokes_only_current_session(self):
        first = self.setup_account()
        second = self.login().json()["access_token"]
        response = self.client.post("/api/auth/logout", headers=self.bearer(first))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(auth.resolve(first))
        self.assertEqual(auth.resolve(second), NAME)

    def test_setup_is_local_same_origin_and_only_once(self):
        remote = TestClient(app, client=("203.0.113.8", 1234))
        self.addCleanup(remote.close)
        response = remote.post("/api/auth/setup", json={"username": NAME, "password": PASSWORD})
        self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post("/api/auth/setup", headers={"Origin": "https://unrelated.example"},
                                    json={"username": NAME, "password": PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.setup_account()
        response = self.client.post("/api/auth/setup", json={"username": "another-user", "password": PASSWORD})
        self.assertEqual(response.status_code, 409)

    def test_inherited_data_uses_original_service_and_balances_borrowing(self):
        original = {"entries": [{"text": "old world", "source": "manual"}]}
        path = self.root / "world.json"
        path.write_text(json.dumps(original), encoding="utf8")
        before = path.read_bytes()
        token = self.setup_account(inherit=True)
        self.assertEqual(auth.storage_uid(NAME), "")
        self.assertIs(registry.get(NAME), registry.get(""))
        response = self.client.get("/api/state", headers=self.bearer(token))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["world_count"], 1)
        self.assertEqual(registry._refs, {})
        self.assertEqual(path.read_bytes(), before)
        auth.create_account("second-user", PASSWORD)
        self.assertEqual(registry.get("second-user").state()["world_count"], 0)
        response = self.client.delete("/api/users/" + NAME)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(path.read_bytes(), before)

    def test_delete_rejects_active_service_and_preserves_account_data(self):
        token = self.setup_account()
        account_dir = self.root / "users" / NAME
        account_dir.mkdir(parents=True)
        marker = account_dir / "keep.txt"
        marker.write_text("still here", encoding="utf8")

        with registry.borrow(NAME):
            response = self.client.delete("/api/users/" + NAME)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(account_dir.is_dir())
        self.assertEqual(marker.read_text(encoding="utf8"), "still here")
        self.assertEqual(auth.resolve(token), NAME)

    def test_delete_keeps_token_when_registry_drop_races_after_preflight(self):
        token = self.setup_account()
        account_dir = self.root / "users" / NAME
        account_dir.mkdir(parents=True)
        marker = account_dir / "keep.txt"
        marker.write_text("still here", encoding="utf8")

        with patch.object(registry, "in_use", return_value=False), \
                patch.object(registry, "drop", return_value=False):
            response = self.client.delete("/api/users/" + NAME)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(auth.resolve(token), NAME)
        self.assertTrue(account_dir.is_dir())
        self.assertEqual(marker.read_text(encoding="utf8"), "still here")

    def test_existing_independent_data_cannot_be_hidden_by_default_binding(self):
        directory = self.root / "users" / NAME
        directory.mkdir(parents=True)
        (directory / "world.json").write_text('{"entries":[{"text":"existing"}]}', encoding="utf8")
        response = self.client.post("/api/auth/setup", json={
            "username": NAME, "password": PASSWORD, "inherit_default": True,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(auth.has_accounts())

    def test_validation_and_failed_login_throttling(self):
        for name in ("../outside", "..", "a/b", "a\\b", "NUL", "CON.txt", "name."):
            response = self.client.post("/api/auth/setup", json={"username": name, "password": PASSWORD})
            self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/api/auth/setup", json={"username": NAME, "password": "short"})
        self.assertEqual(response.status_code, 400)
        self.setup_account()
        for _ in range(5):
            self.assertEqual(self.login(password="bad").status_code, 401)
        response = self.login(password="bad")
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("Retry-After", response.headers)

    def test_account_names_cannot_alias_the_same_windows_directory(self):
        self.setup_account(name="Alice")
        with self.assertRaises(auth.AccountConflict):
            auth.create_account("alice", PASSWORD)

    def test_password_reset_revokes_tokens_and_preserves_data_identity(self):
        token = self.setup_account(inherit=True)
        auth.set_password(NAME, "a-new-password")
        self.assertIsNone(auth.resolve(token))
        self.assertEqual(auth.storage_uid(NAME), "")
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password="a-new-password").status_code, 200)

    def test_password_reset_blocks_an_overlapping_old_password_login(self):
        self.setup_account()
        hash_read = Event()
        resume = Event()
        verify = auth._verify_password

        def paused_verification(password, encoded):
            hash_read.set()
            if not resume.wait(5):
                raise TimeoutError("Password reset did not release the paused login")
            return verify(password, encoded)

        with patch.object(auth, "_verify_password", side_effect=paused_verification), ThreadPoolExecutor() as pool:
            pending_login = pool.submit(self.login)
            try:
                self.assertTrue(hash_read.wait(5), "Login did not read the old password hash")
                auth.set_password(NAME, "a-new-password")
            finally:
                resume.set()
            response = pending_login.result(timeout=5)
        self.assertEqual(response.status_code, 401, response.text)
        with closing(sqlite3.connect(auth.db_path())) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0], 0)
        self.assertEqual(self.login(password="a-new-password").status_code, 200)

    def test_existing_token_database_is_migrated(self):
        path = Path(auth.db_path())
        if path.exists():
            path.unlink()
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE tokens (token_hash TEXT PRIMARY KEY, uid TEXT NOT NULL, created_at REAL NOT NULL)")
            con.execute("INSERT INTO tokens VALUES(?,?,?)", (hashlib.sha256(b"old-token").hexdigest(), NAME, 1))
        self.assertEqual(auth.resolve("old-token"), NAME)
        self.setup_account()
        self.assertEqual(auth.resolve("old-token"), NAME)

    def test_legacy_availability_ignores_only_automatic_greeting(self):
        path = self.root / "memory.db"
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE messages (session TEXT, seq INTEGER, role TEXT, content TEXT, ts REAL, greeting INTEGER DEFAULT 0)")
            con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                        ("default", 1, "assistant", "哟，隔壁的", 1, 1))
        self.assertFalse(auth.data_available(self.root))
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                        ("default", 2, "user", "你好", 2, 0))
        self.assertTrue(auth.data_available(self.root))

    def test_logged_in_empty_account_can_claim_legacy_data_and_keep_session(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界", "source": "manual"}]}),
            encoding="utf8")
        token = self.setup_account(inherit=False)

        status = self.client.get("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json(), {"available": True, "storage_uid": NAME})

        claimed = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json(), {"claimed": True, "storage_uid": ""})
        self.assertEqual(auth.storage_uid(NAME), "")
        self.assertEqual(auth.resolve(token), NAME)

        again = self.client.get("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json(), {"available": False, "storage_uid": ""})

    def test_repeating_legacy_claim_is_idempotent_after_response_loss(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界", "source": "manual"}]}),
            encoding="utf8")
        token = self.setup_account(inherit=False)

        first = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(first.status_code, 200, first.text)
        retry = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), {"claimed": True, "storage_uid": ""})
        with registry.borrow(NAME):
            active_retry = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(active_retry.status_code, 200, active_retry.text)

    def test_custom_life_file_makes_account_nonempty_for_legacy_claim(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界", "source": "manual"}]}),
            encoding="utf8")
        account_dir = self.root / "users" / NAME
        account_dir.mkdir(parents=True)
        (account_dir / "life.json").write_text(
            json.dumps({"cares": "她自己钉下的牵挂"}), encoding="utf8")
        token = self.setup_account(inherit=False)

        status = self.client.get("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["available"], False)

    def test_overlapping_legacy_retry_succeeds_after_another_request_claims(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界"}]}), encoding="utf8")
        token = self.setup_account(inherit=False)
        preflight = Event()
        resume = Event()
        exclusive = registry.exclusive

        def delayed_exclusive(user_id):
            if not preflight.is_set():
                preflight.set()
                if not resume.wait(5):
                    raise TimeoutError("认领重试未被放行")
            return exclusive(user_id)

        with patch.object(registry, "exclusive", side_effect=delayed_exclusive), ThreadPoolExecutor() as pool:
            retry = pool.submit(self.client.post, "/api/auth/legacy", headers=self.bearer(token))
            try:
                self.assertTrue(preflight.wait(5))
                first = self.client.post("/api/auth/legacy", headers=self.bearer(token))
                self.assertEqual(first.status_code, 200, first.text)
                with registry.borrow(NAME):
                    resume.set()
                    response = retry.result(timeout=5)
            finally:
                resume.set()
        self.assertEqual(response.status_code, 200, response.text)

    def test_generated_default_templates_do_not_block_legacy_claim(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界", "source": "manual"}]}),
            encoding="utf8")
        account_dir = self.root / "users" / NAME
        account_dir.mkdir(parents=True)
        from app.models.life import DEFAULT_LIFE
        from app.models.personality import DEFAULT_PERSONALITY
        from app.models.relationship import DEFAULT_RELATIONSHIP
        personality = {"current": DEFAULT_PERSONALITY, "history": []}
        relationship = dict(DEFAULT_RELATIONSHIP)
        relationship["since"] = "2026-10-06"
        relationship["events"] = []
        relationship["history"] = []
        (account_dir / "personality.json").write_text(json.dumps(personality), encoding="utf8")
        (account_dir / "life.json").write_text(json.dumps(DEFAULT_LIFE), encoding="utf8")
        (account_dir / "relationship.json").write_text(json.dumps(relationship), encoding="utf8")
        token = self.setup_account(inherit=False)

        status = self.client.get("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["available"], True)

    def test_relationship_customization_is_independent_data(self):
        (self.root / "relationship.json").write_text(
            json.dumps({"stage": "亲密", "call_note": "她自己定下的称呼"}), encoding="utf8")
        self.assertTrue(auth.data_available(self.root))

    def test_quarantined_or_unreadable_personality_is_not_empty_data(self):
        retained = self.root / "personality.json.corrupt"
        retained.write_bytes(b'{"current": {"name": "unfinished')
        self.assertTrue(auth.data_available(self.root))
        retained.unlink()
        (self.root / "personality.json").write_bytes(b"\xff\xfe\x00")
        self.assertTrue(auth.data_available(self.root))

    def test_summary_and_pending_memory_are_not_empty_history(self):
        path = self.root / "memory.db"
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE summaries (session TEXT, text TEXT, cursor INTEGER)")
            con.execute("CREATE TABLE pending (text TEXT, kind TEXT)")
        self.assertFalse(auth.data_available(self.root))
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("INSERT INTO summaries VALUES ('default', '过去发生过的事', 1)")
        self.assertTrue(auth.data_available(self.root))
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("DELETE FROM summaries")
            con.execute("INSERT INTO pending VALUES ('还在等向量的记忆', 'fact')")
        self.assertTrue(auth.data_available(self.root))

    def test_legacy_claim_rejects_independent_data_and_existing_root_owner(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"text": "旧世界", "source": "manual"}]}),
            encoding="utf8")
        token = self.setup_account(inherit=False)
        (self.root / "users" / NAME / "world.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "users" / NAME / "world.json").write_text(
            json.dumps({"entries": [{"text": "独立世界"}]}), encoding="utf8")
        blocked = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(auth.storage_uid(NAME), NAME)

        other = "另一个账号"
        auth.create_account(other, PASSWORD, inherit_default=True)
        self.assertEqual(auth.storage_uid(other), "")
        # The first account can never claim after the root data has an owner.
        (self.root / "users" / NAME / "world.json").unlink()
        blocked = self.client.post("/api/auth/legacy", headers=self.bearer(token))
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(auth.storage_uid(NAME), NAME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
