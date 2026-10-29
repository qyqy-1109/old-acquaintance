import datetime as dt
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app.services.chat_service import ChatService


class BlockingClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.overlap = threading.Event()
        self.active = 0
        self.guard = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        with self.guard:
            self.active += 1
            if self.active > 1:
                self.overlap.set()
        self.started.set()
        try:
            if not self.release.wait(5):
                raise RuntimeError("测试未释放模型")
            data = {"summary": "这段时间在学校上课，回屋休息。", "place": "出租屋",
                    "activity": "听歌", "event": "回屋听歌"}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(data, ensure_ascii=False)))])
        finally:
            with self.guard:
                self.active -= 1


class LifeBoardCatchupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        for name, value in {"DATA_DIR": directory, "API_KEY": "test-key",
                            "CHAT_API_KEY": "test-key", "EMBED_API_KEY": "test-key",
                            "AMAP_KEY": "", "AMAP_CITY": ""}.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch("httpx.Client.send", side_effect=AssertionError("禁止联网")))
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.service.life.advance(now=dt.datetime(2026, 10, 1, 9, 30), force=True)
        self.now = dt.datetime(2026, 10, 9, 14, 30)
        self.stack.enter_context(patch("time.time", return_value=self.now.timestamp()))
        self.client = BlockingClient()
        self.service.client = self.client

    def test_board_reports_running_until_background_catchup_finishes(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(self.service._advance_life)
            try:
                self.assertTrue(self.client.started.wait(2))
                board = self.service.life_board()
                self.assertTrue(board.get("catchup", {}).get("running"),
                                "不能把补算中的一条流水当作最终看板")
                self.assertGreater(board["catchup"]["remaining_slots"], 0)
            finally:
                self.client.release.set()
                task.result(timeout=5)
        board = self.service.life_board()
        self.assertFalse(board["catchup"]["running"])
        self.assertEqual(board["catchup"]["remaining_slots"], 0)

    def test_chat_and_heartbeat_do_not_advance_the_same_gap_twice(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service._advance_life)
            self.assertTrue(self.client.started.wait(2))
            second = pool.submit(self.service._advance_life)
            try:
                self.assertFalse(self.client.overlap.wait(0.2),
                                 "同一生活补算不应并发调用模型并写重复流水")
            finally:
                self.client.release.set()
                first.result(timeout=5)
                second.result(timeout=5)


if __name__ == "__main__":
    unittest.main()
