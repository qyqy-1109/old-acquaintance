"""Regression tests for failures after a chat stream has started."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.controllers.chat_controller import chat_stream
from app.controllers.schema import ChatIn
from app.models.memory_storage import StorageMixin


class ChatStreamFailureTests(unittest.TestCase):
    def test_completed_reply_updates_life_before_done(self):
        observed = []

        class Service:
            def prepare(self, question, multi_message=False):
                return [], []

            def stream(self, messages):
                yield "单子理完啦"

            def observe_life_reply(self, answer):
                observed.append(answer)

            def stage_delivered_turn(self, question, answer):
                return 1

            def finish_turn(self, question, answer):
                pass

            def speak_stream(self, question, answer):
                return iter(())

        request = SimpleNamespace(state=SimpleNamespace(uid=""))

        async def collect(response):
            events = []
            async for chunk in response.body_iterator:
                event = json.loads(chunk.removeprefix("data: "))
                events.append(event)
                if event["type"] == "done":
                    self.assertEqual(observed, ["单子理完啦"])
            return events

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=Service()), \
             patch("app.services.worker.submit") as submit, \
             patch("app.services.quota.spend"):
            response = chat_stream(ChatIn(message="回来请你喝东西"), request)
            events = asyncio.run(collect(response))

        self.assertIn("done", [event["type"] for event in events])
        self.assertEqual(observed, ["单子理完啦"])
        submit.assert_called_once()

    def test_model_connection_failure_reports_error_without_saving_turn(self):
        saved = []

        class Service:
            def prepare(self, question, multi_message=False):
                return [], []

            def stream(self, messages):
                yield "部分回答"
                raise ConnectionError("TLS connection closed")

            def finish_turn(self, question, answer):
                saved.append((question, answer))

        request = SimpleNamespace(state=SimpleNamespace(uid=""))
        async def collect(response):
            return [json.loads(chunk.removeprefix("data: "))
                    async for chunk in response.body_iterator]

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=Service()), \
             patch("app.services.quota.spend") as spend:
            response = chat_stream(ChatIn(message="你好"), request)
            with self.assertLogs("app.chat", level="ERROR") as captured:
                events = asyncio.run(collect(response))

        self.assertEqual([event["type"] for event in events], ["meta", "delta", "error"])
        self.assertIn("连接", events[-1]["message"])
        self.assertNotIn("TLS connection closed", events[-1]["message"])
        self.assertEqual(saved, [])
        spend.assert_not_called()
        self.assertRegex(captured.output[0], r"phase=generate elapsed_ms=\d+ error_type=ConnectionError")
        self.assertIn("visible_chunks=1", captured.output[0])

    def test_empty_stream_reports_error_without_saving_turn(self):
        """模型一个字都没生成、又没抛异常 → **也必须算失败**（`T93`）。

        原来的写法只看"有没有抛异常"：空流会一路走到成功分支——`observe_life_reply("")`、
        把空轮次交给后台 `finish_turn` 写进记忆、花掉配额、还发一个 `done`，
        而用户眼前**一片空白**（真机撞到过：`#131` 勘察里 6 次有 1 次空回复）。
        """
        saved = []

        class Service:
            def prepare(self, question, multi_message=False):
                return [], []

            def stream(self, messages):
                return iter(())                       # 一个字都没有，也不抛异常

            def finish_turn(self, question, answer):
                saved.append((question, answer))

            def observe_life_reply(self, answer):
                saved.append(("life", answer))

        request = SimpleNamespace(state=SimpleNamespace(uid=""))

        async def collect(response):
            return [json.loads(chunk.removeprefix("data: "))
                    async for chunk in response.body_iterator]

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=Service()), \
             patch("app.services.worker.submit") as submit, \
             patch("app.services.quota.spend") as spend:
            response = chat_stream(ChatIn(message="你好"), request)
            with self.assertLogs("app.chat", level="ERROR") as captured:
                events = asyncio.run(collect(response))

        self.assertEqual([event["type"] for event in events], ["meta", "error"])
        self.assertIn("回复生成失败", events[-1]["message"])
        self.assertEqual(saved, [], "空轮次不该同步生活、更不该写进记忆")
        submit.assert_not_called()
        spend.assert_not_called()
        self.assertRegex(captured.output[0], r"phase=generate elapsed_ms=\d+ error_type=EmptyReply")

    def test_time_marker_only_stream_reports_error_without_side_effects(self):
        side_effects = []

        class Service:
            def prepare(self, question, multi_message=False):
                return [], []

            def stream(self, messages):
                yield "（今天下午 13:54）"

            def observe_life_reply(self, answer):
                side_effects.append(("life", answer))

            def finish_turn(self, question, answer):
                side_effects.append(("finish", question, answer))

            def speak_stream(self, question, answer):
                side_effects.append(("tts", answer))
                return iter(())

        request = SimpleNamespace(state=SimpleNamespace(uid=""))

        async def collect(response):
            return [json.loads(chunk.removeprefix("data: "))
                    async for chunk in response.body_iterator]

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=Service()), \
             patch("app.services.worker.submit") as submit, \
             patch("app.services.quota.spend") as spend:
            response = chat_stream(ChatIn(message="你今天干嘛了"), request)
            events = asyncio.run(collect(response))

        self.assertEqual([event["type"] for event in events], ["meta", "delta", "error"])
        self.assertIn("回复生成失败", events[-1]["message"])
        self.assertEqual(side_effects, [])
        submit.assert_not_called()
        spend.assert_not_called()

    def test_prepare_failure_logs_stage_and_duration(self):
        class Service:
            def prepare(self, question, multi_message=False):
                raise RuntimeError("prepare failed")

        request = SimpleNamespace(state=SimpleNamespace(uid=""))
        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=Service()):
            with self.assertLogs("app.chat", level="ERROR") as captured:
                with self.assertRaises(RuntimeError):
                    chat_stream(ChatIn(message="你好"), request)

        self.assertRegex(captured.output[0], r"phase=prepare elapsed_ms=\d+ error_type=RuntimeError")

    def test_recall_embedding_failure_logs_degradation_and_duration(self):
        store = SimpleNamespace(vectors=np.ones((1, 2)), _embed=lambda texts: None)
        with self.assertLogs("app.memory", level="WARNING") as captured:
            self.assertIsNone(StorageMixin._query_vec(store, "你好"))

        self.assertRegex(captured.output[0], r"phase=recall elapsed_ms=\d+ outcome=embedding_unavailable")

    def test_embedding_request_logs_exception_type_and_duration(self):
        def fail(**kwargs):
            raise ConnectionError("TLS connection closed")

        store = SimpleNamespace(
            embed_dimensions=None, embed_model="test",
            embed_client=SimpleNamespace(embeddings=SimpleNamespace(create=fail)))
        with patch("app.models.memory_storage.time.sleep"), \
             self.assertLogs("app.memory", level="WARNING") as captured:
            self.assertIsNone(StorageMixin._embed_chunk(store, ["你好"]))

        self.assertEqual(len(captured.output), 2)
        self.assertRegex(captured.output[0], r"phase=embedding elapsed_ms=\d+ error_type=ConnectionError")
        self.assertRegex(captured.output[1], r"phase=embedding elapsed_ms=\d+ error_type=ConnectionError")


if __name__ == "__main__":
    unittest.main()
