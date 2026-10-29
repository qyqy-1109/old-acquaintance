"""T65 lifecycle red tests for registry, legacy switching, and worker ownership."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from starlette.requests import Request
from starlette.responses import StreamingResponse
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import config
from app.controllers import auth_controller
from app.main import app as production_app
from app.services import auth, registry, worker


class FakeChatService:
    instances = []

    def __init__(self, user_id=""):
        self.user_id = user_id
        self.closed = False
        type(self).instances.append(self)

    def close(self):
        self.closed = True

    def wait_for_release(self, started, release):
        started.set()
        if not release.wait(5):
            raise AssertionError("测试 worker 没有收到释放信号")


class LegacyLifecycleTests(unittest.TestCase):
    password = "a-good-password"

    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.patches = []
        self.patches.append(patch.object(config, "DATA_DIR", str(self.root)))
        self.patches.append(patch.object(registry, "_services", OrderedDict()))
        self.patches.append(patch.object(registry, "_refs", {}))
        self.patches.append(patch("app.services.chat_service.ChatService", FakeChatService))
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        FakeChatService.instances.clear()

        self.worker_state = {
            "WORKERS": worker.WORKERS,
            "queues": worker._queues,
            "threads": worker._threads,
            "started": worker._started,
        }
        worker.WORKERS = 1
        worker._queues = []
        worker._threads = []
        worker._started = False
        self.addCleanup(registry.close_all)
        self.addCleanup(self._restore_worker)

    def _restore_worker(self):
        worker.flush(timeout=5)
        worker.WORKERS = self.worker_state["WORKERS"]
        worker._queues = self.worker_state["queues"]
        worker._threads = self.worker_state["threads"]
        worker._started = self.worker_state["started"]

    def _write_root_history(self):
        (self.root / "world.json").write_text(
            json.dumps({"entries": [{"term": "old", "content": "旧日记"}]}, ensure_ascii=False),
            encoding="utf8",
        )

    def _new_empty_account(self, uid="alice"):
        auth.create_account(uid, self.password)
        return auth.issue(uid)

    @staticmethod
    def _request_with_token(token):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/legacy",
                "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
                "client": ("127.0.0.1", 12345),
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )

    def test_pending_bound_service_blocks_drop_and_legacy_claim(self):
        self._write_root_history()
        token = self._new_empty_account()
        service = registry.get("alice")
        queue_blocked = threading.Event()
        queue_release = threading.Event()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(queue_release.set)
        self.addCleanup(release.set)

        def block_queue():
            queue_blocked.set()
            queue_release.wait(5)

        worker.submit("alice", block_queue)
        self.assertTrue(queue_blocked.wait(3))
        worker.submit("alice", service.wait_for_release, started, release)
        self.assertFalse(started.is_set(), "绑定实例的后台任务必须仍在队列中等待")
        self.assertTrue(registry.in_use("alice"))

        with self.assertRaises(registry.ServiceBusy):
            with registry.exclusive("alice"):
                pass
        self.assertFalse(registry.drop("alice"), "待执行任务持有引用时不能摘除实例")
        self.assertFalse(service.closed)

        with self.assertRaises(HTTPException) as caught:
            auth_controller.claim_legacy(self._request_with_token(token))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(service.closed, "认领冲突不能关闭仍在执行的实例")

        queue_release.set()
        self.assertTrue(started.wait(3), "队列放行后绑定实例的任务应执行")
        self.assertTrue(registry.in_use("alice"), "从排队到执行结束必须持续持有实例引用")
        release.set()
        self.assertTrue(worker.flush(timeout=5))
        self.assertFalse(registry.in_use("alice"))

    def test_exclusive_switch_pauses_acquire_until_claim_maps_to_root(self):
        self._write_root_history()
        self._new_empty_account()
        registry.get("alice")
        acquired = threading.Event()
        observed = []

        def acquire_user():
            observed.append(registry.acquire("alice"))
            acquired.set()

        with registry.exclusive("alice"):
            thread = threading.Thread(target=acquire_user)
            thread.start()
            time.sleep(0.15)
            self.assertFalse(acquired.is_set(), "切换期间并发 acquire 必须等待独占锁")
            self.assertTrue(registry.drop("alice"))
            self.assertTrue(auth.claim_legacy("alice"))

        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [""], "认领完成后 acquire 必须拿到根数据实例")
        registry.release(observed[0])
        self.assertEqual(auth.storage_uid("alice"), "")

    def test_borrow_cached_holds_snapshot_identity_and_never_revives_stale_service(self):
        service = registry.get("alice")
        snapshot = registry.all_services()
        self.assertEqual(snapshot, [service])
        stale = snapshot[0]

        with registry.borrow_cached(stale) as borrowed:
            self.assertIs(borrowed, service)
            self.assertTrue(registry.in_use("alice"))
            self.assertFalse(registry.drop("alice"))
            self.assertFalse(service.closed)

        self.assertTrue(registry.drop("alice"))
        self.assertTrue(service.closed)
        with registry.borrow_cached(stale) as borrowed:
            self.assertIsNone(borrowed, "已摘除的快照实例不能被 borrow_cached 重新放回缓存")
        self.assertEqual(registry.count(), 0)

    def test_acquire_keeps_construction_to_reference_step_under_registry_lock(self):
        service = registry.get("alice")
        returned = threading.Event()
        proceed = threading.Event()
        acquired = []
        dropped = []
        drop_started = threading.Event()
        original_get = registry.get

        def delayed_get(uid=""):
            result = original_get(uid)
            returned.set()
            self.assertTrue(proceed.wait(5), "测试没有放行 acquire")
            return result

        def acquire_user():
            acquired.append(registry.acquire("alice"))

        def drop_user():
            drop_started.set()
            dropped.append(registry.drop("alice"))

        with patch.object(registry, "get", delayed_get):
            acquire_thread = threading.Thread(target=acquire_user)
            acquire_thread.start()
            drop_thread = None
            try:
                self.assertTrue(returned.wait(3))
                drop_thread = threading.Thread(target=drop_user)
                drop_thread.start()
                self.assertTrue(drop_started.wait(3))
                time.sleep(0.15)
                self.assertFalse(
                    dropped,
                    "实例构建完成到引用计数登记之间，drop 必须被同一把锁挡住",
                )
            finally:
                proceed.set()
                acquire_thread.join(3)
                if drop_thread:
                    drop_thread.join(3)

        self.assertEqual(acquired, ["alice"])
        self.assertEqual(dropped, [False])
        self.assertFalse(service.closed)
        registry.release(acquired[0])
        self.assertTrue(registry.drop("alice"))
        self.assertTrue(service.closed)

    def test_worker_plain_callable_does_not_construct_chat_service(self):
        worker.WORKERS = 0
        called = []

        worker.submit("plain", lambda: called.append(True))

        self.assertEqual(called, [True])
        self.assertEqual(FakeChatService.instances, [], "普通函数入队不应构造 ChatService")
        self.assertEqual(registry.count(), 0)

    def _streaming_client(self, body):
        streaming_app = FastAPI()
        for middleware in reversed(production_app.user_middleware):
            streaming_app.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)

        @streaming_app.post("/api/chat/stream")
        async def stream():
            return StreamingResponse(body(), media_type="text/event-stream")

        client = TestClient(streaming_app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def test_stream_holds_user_service_until_entire_body_finishes(self):
        token = self._new_empty_account()
        service = registry.get("alice")
        observed = []

        async def body():
            yield "first"
            await asyncio.sleep(0)
            observed.append((registry.in_use("alice"), registry.drop("alice"), service.closed))
            yield "last"

        client = self._streaming_client(body)
        response = client.post("/api/chat/stream", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [(True, False, False)], "SSE 正文生成期间不能关闭实例")
        self.assertEqual(response.text, "firstlast")
        self.assertFalse(registry.in_use("alice"), "SSE 正文全部结束后应释放实例引用")
        self.assertTrue(registry.drop("alice"))
        self.assertTrue(service.closed)

    def test_stream_body_error_releases_user_service(self):
        token = self._new_empty_account()
        service = registry.get("alice")
        observed = []

        async def body():
            yield "first"
            await asyncio.sleep(0)
            observed.append((registry.in_use("alice"), registry.drop("alice"), service.closed))
            raise RuntimeError("模拟流式正文生成异常")

        client = self._streaming_client(body)
        client.post("/api/chat/stream", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(observed, [(True, False, False)], "正文异常之前不能提前关闭实例")
        self.assertFalse(registry.in_use("alice"), "正文抛出异常后应释放实例引用")
        self.assertTrue(registry.drop("alice"))
        self.assertTrue(service.closed)


if __name__ == "__main__":
    unittest.main()
