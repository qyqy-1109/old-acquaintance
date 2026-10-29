"""Red tests for identity validation, lifecycle ownership, and worker sharding."""

from __future__ import annotations

import threading
import time
import tempfile
import queue
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import config
from app.controllers.chat_controller import create_user, delete_user, uid_of
from app.services import auth, registry, worker


class _FakeService:
    instances = []

    def __init__(self, user_id=""):
        self.user_id = user_id
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


class IdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        self.data_patch = patch.object(config, "DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.auth_required_patch = patch.object(config, "AUTH_REQUIRED", False)
        self.auth_required_patch.start()
        self.addCleanup(self.auth_required_patch.stop)
        self.services_patch = patch.object(registry, "_services", OrderedDict())
        self.refs_patch = patch.object(registry, "_refs", {})
        self.services_patch.start()
        self.refs_patch.start()
        self.addCleanup(self.services_patch.stop)
        self.addCleanup(self.refs_patch.stop)
        self.chat_patch = patch("app.services.chat_service.ChatService", _FakeService)
        self.chat_patch.start()
        self.addCleanup(self.chat_patch.stop)
        _FakeService.instances.clear()

    def test_percent_encoded_path_uid_is_rejected_before_directory_use(self):
        malicious = "..%5C..%5CWindows%5CTemp%5Cevil"
        with self.assertRaises(ValueError):
            auth.nickname_uid({
                "X-User-Id": malicious,
                "X-User-Id-Encoding": "percent-encoded",
            })
        with self.assertRaises(ValueError):
            auth.issue(malicious)
        with self.assertRaises(ValueError):
            config.user_data_dir("..\\Windows\\Temp\\evil")
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"), headers={},
            query_params={"uid": malicious})
        with self.assertRaises(HTTPException) as created:
            create_user(request)
        self.assertEqual(created.exception.status_code, 400)
        with self.assertRaises(HTTPException) as deleted:
            delete_user(malicious, request)
        self.assertEqual(deleted.exception.status_code, 400)

    def test_issuing_a_token_turns_on_authentication(self):
        auth.issue("小林")
        self.assertTrue(auth.required())

    def test_empty_bearer_header_is_not_treated_as_nickname_auth(self):
        request = SimpleNamespace(headers={"Authorization": "Bearer"})
        with self.assertRaises(HTTPException) as caught:
            uid_of(request)
        self.assertEqual(caught.exception.status_code, 401)

    def test_default_service_is_never_recycled_when_user_limit_is_reached(self):
        with patch.object(registry, "MAX_SERVICES", 1):
            default = registry.get("")
            registry.get("alice")
            self.assertFalse(default.closed)
            self.assertIs(registry.get(""), default)

    def test_failed_close_keeps_service_cached(self):
        first = registry.get("first")
        second = registry.get("second")
        first.closed = False
        with patch.object(registry, "MAX_SERVICES", 1), \
                patch.object(first, "close", side_effect=OSError("disk busy")):
            registry._recycle(protect="second")
        self.assertIs(registry._services.get("first"), first)
        self.assertFalse(first.closed)

    def test_accounts_bound_to_root_share_the_default_worker_shard(self):
        auth.create_account("legacy", "a-good-password", inherit_default=True)
        self.assertEqual(auth.storage_uid("legacy"), "")
        queues = [queue.Queue() for _ in range(3)]
        with patch.object(worker, "WORKERS", 3), \
                patch.object(worker, "_queues", queues), \
                patch.object(worker, "_started", True):
            worker.submit("legacy", lambda: None)
        self.assertEqual([item.qsize() for item in queues], [1, 0, 0])

    def test_delete_holds_exclusive_registry_lock_until_revoke_and_remove_finish(self):
        uid = "alice"
        auth.issue(uid)
        config.user_data_dir(uid)
        revoke_entered = threading.Event()
        release_revoke = threading.Event()
        contender_entered = threading.Event()
        contender_uid = []

        original_revoke = auth.revoke_all

        def blocking_revoke(account):
            revoke_entered.set()
            release_revoke.wait(3)
            return original_revoke(account)

        def contender():
            contender_uid.append(registry.acquire(uid))
            contender_entered.set()

        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"), headers={})
        result = []
        with patch.object(auth, "revoke_all", side_effect=blocking_revoke):
            deletion = threading.Thread(
                target=lambda: result.append(delete_user(uid, request)))
            deletion.start()
            self.assertTrue(revoke_entered.wait(3))
            race = threading.Thread(target=contender)
            race.start()
            try:
                self.assertFalse(contender_entered.wait(0.2))
            finally:
                release_revoke.set()
            deletion.join(3)
            race.join(3)

        if contender_uid:
            registry.release(contender_uid[0])
        self.assertFalse(deletion.is_alive())
        self.assertFalse(race.is_alive())
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["data_dir_removed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
