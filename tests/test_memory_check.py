"""记忆体检使用有限远程请求，并且不能把失败显示成检查通过。"""

import json
import shutil
import struct
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.controllers.chat_controller import command
from app.controllers.schema import CommandIn
from app.models.memory import MemoryStore
from app.services.chat_service import ChatService
from app.utils.llmjson import chat_json


class MemoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(__file__).resolve().parents[1] / "_tmptest/memory-check-199/cases" / self._testMethodName
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory))
        self.requests = []
        self.response = {"issues": []}
        self.failure = None
        self.status = 200
        self.http = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.client = OpenAI(api_key="memory-check-test", base_url="https://memory-check.invalid/v1",
                             http_client=self.http, timeout=0.01, max_retries=2)
        self.addCleanup(self.client.close)
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key", "CHAT_API_KEY": "test-key",
            "EMBED_API_KEY": "test-key", "AMAP_KEY": "", "AMAP_CITY": "",
            "AUTO_EMOTION": False, "AUTO_EVOLVE_PERSONALITY": False, "CHECK_ON_START": False,
            "MEMORY_CFG": dict(config.MEMORY_CFG, check_timeout=0.25),
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ("socket.socket.connect", "socket.create_connection", "requests.sessions.Session.request",
                       "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.store = MemoryStore(self.client, path=str(self.directory / "memory.db"),
                                 embed_model="test-embedding", cfg=dict(config.MEMORY_CFG))
        self.addCleanup(self.store.db.close)
        self.store.memories = [
            {"id": 1, "kind": "fact", "actor": "user", "text": "他周一在家"},
            {"id": 2, "kind": "fact", "actor": "user", "text": "他周一在外地"},
        ]

    def respond(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure("controlled offline failure", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "controlled failure", "type": "server_error"}})
        content = self.response if isinstance(self.response, str) else json.dumps(self.response, ensure_ascii=False)
        return httpx.Response(200, json={
            "id": "check-test", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        })

    def service(self):
        service = object.__new__(ChatService)
        service.store = self.store
        service.memory_issues = []
        service.memory_check_error = ""
        return service

    def test_check_scopes_timeout_and_does_not_mutate_shared_client(self):
        self.assertEqual(self.store.check_consistency(), [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].extensions["timeout"],
                         {"connect": 0.25, "read": 0.25, "write": 0.25, "pool": 0.25})
        self.assertEqual(self.client.timeout, 0.01)
        self.assertEqual(self.client.max_retries, 2)
        self.assertEqual(self.store.events_stats().get("reconcile"), 1)

    def test_timeout_is_one_request_and_is_reported_as_incomplete(self):
        self.failure = httpx.ReadTimeout
        service = self.service()
        report = service.reconcile_report()
        self.assertIn("未完成", report)
        self.assertIn("重试", report)
        self.assertNotIn("没发现矛盾", report)
        self.assertEqual(len(self.requests), 1, "网络超时不能触发SDK、JSON或关思考回退重试")
        self.assertNotIn("reconcile", self.store.events_stats())

    def test_server_failure_is_one_request_and_preserves_last_success(self):
        self.response = {"issues": [{"type": "conflict", "items": ["他周一在家", "他周一在外地"], "reason": "同一时刻地点不同"}]}
        service = self.service()
        self.assertIn("发现 1 处", service.reconcile_report())
        previous = service.memory_issues.copy()
        self.requests.clear()
        self.status = 503
        self.assertIn("未完成", service.reconcile_report())
        self.assertEqual(service.memory_issues, previous)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("503", service.memory_check_error)
        self.status = 200
        self.response = {"issues": []}
        self.assertIn("没发现矛盾", service.reconcile_report())
        self.assertEqual(service.memory_issues, [])
        self.assertEqual(service.memory_check_error, "")

    def test_invalid_json_does_not_retry_or_claim_success(self):
        self.response = "not json"
        report = self.service().reconcile_report()
        self.assertIn("未完成", report)
        self.assertEqual(len(self.requests), 1)

    def test_missing_issue_list_does_not_claim_success(self):
        for response in ({}, {"issues": None}, {"issues": "none"}, {"issues": ["invalid"]},
                         {"issues": [{"items": "他周一在家"}]}):
            with self.subTest(response=response):
                self.requests.clear()
                self.response = response
                self.assertIn("未完成", self.service().reconcile_report())
                self.assertEqual(len(self.requests), 1)

    def test_empty_store_does_not_call_remote_client(self):
        self.store.memories = []
        self.assertEqual(self.store.check_consistency(), [])
        self.assertEqual(self.requests, [])

    def test_command_returns_error_and_last_success_with_the_same_report(self):
        self.failure = httpx.ReadTimeout
        service = self.service()
        previous = [{"type": "conflict", "items": ["旧证据一", "旧证据二"]}]
        service.memory_issues = previous.copy()
        with patch("app.controllers.chat_controller.svc", return_value=service):
            data = command(CommandIn(command="/mem check"), None)
        self.assertIn("未完成", data["output"])
        self.assertIn("未完成", data.get("memory_check_error", ""))
        self.assertEqual(data.get("memory_issues"), previous)
        self.assertEqual(len(self.requests), 1)

    def test_non_finite_or_non_positive_timeout_is_visible_without_request(self):
        for timeout in (float("inf"), float("nan"), 0, -1):
            with self.subTest(timeout=timeout):
                self.requests.clear()
                self.store.cfg["check_timeout"] = timeout
                self.assertIn("未完成", self.service().reconcile_report())
                self.assertEqual(self.requests, [])

    def test_startup_failure_is_logged_without_preventing_service(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO memories(text,kind,ts,session,vec) VALUES(?,?,?,?,?)",
                                  ("他周一在家", "fact", 1, "default", struct.pack("f", 1.0)))
        self.failure = httpx.ReadTimeout
        with patch("app.services.chat_service.OpenAI", return_value=self.client), \
             patch.object(config, "CHECK_ON_START", True), \
             self.assertLogs("app.chat", level="ERROR") as captured:
            service = ChatService()
            self.addCleanup(service.close)
        self.assertEqual(service.memory_issues, [])
        self.assertIn("未完成", service.memory_check_error)
        self.assertIn("未完成", "\n".join(captured.output))
        self.assertIn("未完成", service.state()["memory_check_error"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(service.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_no_think_fallback_only_for_explicit_unsupported_parameter(self):
        def unsupported(request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(400, json={"error": {"code": "1210", "message": "thinking is not supported"}})
            return httpx.Response(200, json={"id": "fallback", "object": "chat.completion", "created": 1,
                "model": "test-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"issues": []}'}, "finish_reason": "stop"}]})

        with httpx.Client(transport=httpx.MockTransport(unsupported)) as transport:
            client = OpenAI(api_key="test", base_url="https://memory-check.invalid/v1", http_client=transport, max_retries=0)
            self.assertEqual(chat_json(client, "test", [{"role": "user", "content": "test"}], no_think=True), ({"issues": []}, ""))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("thinking", json.loads(self.requests[0].content))
        self.assertNotIn("thinking", json.loads(self.requests[1].content))

    def test_no_think_does_not_retry_network_timeout(self):
        self.failure = httpx.ReadTimeout
        data, error = chat_json(self.client.with_options(max_retries=0), "test", [{"role": "user", "content": "test"}], no_think=True)
        self.assertIsNone(data)
        self.assertIn("APITimeoutError", error)
        self.assertEqual(len(self.requests), 1)

    def test_other_bad_request_does_not_trigger_thinking_fallback(self):
        def bad_temperature(request):
            self.requests.append(request)
            return httpx.Response(400, json={"error": {
                "code": "invalid_temperature", "message": "thinking is supported; temperature must be between 0 and 1"}})

        with httpx.Client(transport=httpx.MockTransport(bad_temperature)) as transport:
            client = OpenAI(api_key="test", base_url="https://memory-check.invalid/v1", http_client=transport, max_retries=0)
            data, error = chat_json(client, "test", [{"role": "user", "content": "test"}], no_think=True)
        self.assertIsNone(data)
        self.assertIn("invalid_temperature", error)
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
