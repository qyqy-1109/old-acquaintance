"""刷新开场应读取已交付的最新进展及有来源的相关历史。"""

import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.controllers.chat_controller import chat_stream
from app.controllers.schema import ChatIn
from app.services.chat_service import ChatService


class GreetingContinuityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path("_tmptest/refresh-198") / f"case-{os.getpid()}-{id(self)}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key", "CHAT_API_KEY": "test-key",
            "EMBED_API_KEY": "test-key", "AMAP_KEY": "", "AMAP_CITY": "",
            "GREETING": True, "GREETING_USE_MEMORY": True, "TTS_ENABLED": False,
            "AUTO_EMOTION": False, "AUTO_EVOLVE_PERSONALITY": False,
            "CHECK_ON_START": False, "PROFILE_EVERY": 1000,
            "MEMORY_CFG": dict(config.MEMORY_CFG, summarize_every=0,
                               auto_extract=True, extract_every=1000),
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ("httpx.Client.send", "requests.sessions.Session.request", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.now = dt.datetime(2026, 10, 16, 15, 0).timestamp()
        self.stack.enter_context(patch("time.time", return_value=self.now))
        self.service = ChatService()
        self.addCleanup(lambda: self.service.close())
        self.service.life._save_state("afternoon", "2026-10-16", {
            "place": "家里", "activity": "猫猫不太对劲，准备带它去医院",
            "mood": "担心", "on_mind": "猫猫不对劲，要去医院",
        }, at=self.now - 600)

    def turn(self, question, answer, service=None):
        service = service or self.service
        with service.store._lock:
            history = service.store.sessions.setdefault(service.store.session, [])
            seq = len(history)
            history.extend([
                {"role": "user", "content": question, "ts": self.now},
                {"role": "assistant", "content": answer, "ts": self.now},
            ])
            service.store.save()
        return dict(session=service.store.session, user_seq=seq, assistant_seq=seq + 1,
                    user_text=question, assistant_text=answer, source_ts=self.now)

    def greeting_prompt(self, service=None):
        captured = []

        def generate(client, model, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "你回来啦。"

        with patch("app.services.chat_service.chat_text", generate):
            (service or self.service).greeting(force=True)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_refresh_keeps_progress_after_sixtieth_character(self):
        answer = "我一直陪着它，跟你说说刚才的事。" * 8 + "猫猫在医院量过体温了，医生说先观察。"
        self.turn("猫猫怎么样了", answer)
        prompt = self.greeting_prompt()
        self.assertIn(answer, prompt, "完整的最新回复必须到达开场生成口")

    def test_refresh_reads_resolved_progress_related_to_old_life_after_restart(self):
        answer = "猫猫在医院量过体温了，医生说先观察。"
        source = self.turn("猫猫怎么样了", answer)
        accepted = self.service.continuity.apply([dict(
            actor="assistant", state="experience", topic="猫猫", text=answer,
            quote=answer, status="resolved",
        )], now=self.now, **source)
        self.assertEqual(len(accepted), 1)
        self.turn("今晚听什么歌", "听点轻快的吧")
        self.turn("晚饭吃了吗", "吃过了")
        self.service.close()
        reopened = ChatService()
        self.service = reopened
        prompt = self.greeting_prompt(reopened)
        self.assertIn(answer, prompt, "刷新必须按旧生活的主题召回已确认结果")
        self.assertIn("已解决", prompt)

    def test_stream_done_exposes_latest_turn_before_background_write(self):
        question = "猫猫怎么样了"
        answer = "猫猫在医院量过体温了，医生说先观察。"
        queued = []

        async def collect(response):
            return [json.loads(chunk.removeprefix("data: "))
                    async for chunk in response.body_iterator]

        def enqueue(uid, function, *args, **kwargs):
            queued.append((function, args, kwargs))

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=self.service), \
             patch.object(self.service, "prepare", return_value=([], [])), \
             patch.object(self.service, "stream", return_value=iter([answer])), \
             patch("app.services.worker.submit", enqueue), \
             patch("app.services.quota.spend"):
            events = asyncio.run(collect(chat_stream(
                ChatIn(message=question), SimpleNamespace(state=SimpleNamespace(uid="")))))
        self.assertIn("done", [event["type"] for event in events])
        self.assertEqual(len(queued), 1)
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        prompt = self.greeting_prompt()
        self.assertIn(question, prompt)
        self.assertIn(answer, prompt, "已交付正文不能等待后台提炼才能被刷新读取")
        function, args, kwargs = queued[0]
        function(*args, **kwargs)
        self.assertEqual(self.greeting_prompt().count(answer), 1, "落库后不能重复注入暂存的同一轮")
        history = self.service.store.sessions[self.service.store.session]
        self.assertEqual(sum(message.get("content") == answer for message in history), 1)

    def test_refresh_does_not_duplicate_delivery_while_extraction_is_running(self):
        question = "猫猫怎么样了"
        answer = "猫猫在医院量过体温了，医生说先观察。"
        self.service.store.cfg["extract_every"] = 1
        entered = threading.Event()
        release = threading.Event()

        def extract(user, assistant):
            entered.set()
            if not release.wait(5):
                raise AssertionError("提炼没有被释放")
            return [], [], [], [], [], []

        delivery_id = self.service.stage_delivered_turn(question, answer)
        with patch.object(self.service.store, "_extract", extract), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.service.finish_turn, question, answer, delivery_id=delivery_id)
            try:
                self.assertTrue(entered.wait(5))
                prompt = self.greeting_prompt()
                self.assertEqual(prompt.count(answer), 1, "已追加的原文和交付暂存只注入一次")
            finally:
                release.set()
                future.result(timeout=5)

    def test_refresh_preserves_source_sequence_across_saved_greetings(self):
        self.turn("在吗", "在呀")
        self.greeting_prompt()
        answer = "猫猫在医院量过体温了，医生说先观察。"
        source = self.turn("猫猫怎么样了", answer)
        accepted = self.service.continuity.apply([dict(
            actor="assistant", state="experience", topic="猫猫", text=answer,
            quote=answer, status="resolved",
        )], now=self.now, **source)
        self.assertEqual(len(accepted), 1)
        original = [(message["role"], message["content"])
                    for message in self.service.store.sessions[self.service.store.session]]
        self.greeting_prompt()
        refreshed = self.service.store.sessions[self.service.store.session]
        self.assertEqual([(message["role"], message["content"])
                          for message in refreshed[:len(original)]], original,
                         "刷新不得重排已有消息")
        self.turn("今晚听什么歌", "听点轻快的吧")
        self.service.close()
        reopened = ChatService()
        self.service = reopened
        self.assertTrue(reopened.continuity.get(accepted[0]["id"])["source_valid"],
                        "刷新不得移动已保存消息的来源序号")

    def test_same_words_in_two_delivered_turns_are_not_deduplicated(self):
        question = "猫猫怎么样了"
        answer = "猫猫在医院量过体温了，医生说先观察。"
        first = self.service.stage_delivered_turn(question, answer)
        self.service.finish_turn(question, answer, delivery_id=first)
        second = self.service.stage_delivered_turn(question, answer)
        self.assertEqual(self.greeting_prompt().count(answer), 2)
        self.service.finish_turn(question, answer, delivery_id=second)
        self.assertEqual(self.greeting_prompt().count(answer), 2)

    def test_clear_invalidates_delivery_waiting_in_background_queue(self):
        question = "猫猫怎么样了"
        answer = "猫猫在医院量过体温了，医生说先观察。"
        deliveries = [self.service.stage_delivered_turn(question, answer) for _ in range(6)]
        self.service.run_command("/clear")
        for delivery_id in deliveries:
            self.service.finish_turn(question, answer, delivery_id=delivery_id)
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.service.store.sessions[self.service.store.session], [])

    def test_queue_failure_reports_error_and_does_not_leave_delivered_context(self):
        answer = "猫猫在医院量过体温了，医生说先观察。"

        async def collect(response):
            return [json.loads(chunk.removeprefix("data: "))
                    async for chunk in response.body_iterator]

        with patch("app.controllers.chat_controller._quota_gate", return_value=""), \
             patch("app.controllers.chat_controller.svc", return_value=self.service), \
             patch.object(self.service, "prepare", return_value=([], [])), \
             patch.object(self.service, "stream", return_value=iter([answer])), \
             patch("app.services.worker.submit", side_effect=RuntimeError("queue unavailable")), \
             patch("app.services.quota.spend") as spend:
            events = asyncio.run(collect(chat_stream(
                ChatIn(message="猫猫怎么样了"), SimpleNamespace(state=SimpleNamespace(uid="")))))
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("done", [event["type"] for event in events])
        spend.assert_not_called()
        self.turn("在吗", "在呀")
        self.assertNotIn(answer, self.greeting_prompt())

    def test_refresh_during_history_append_can_remove_an_unsaved_greeting(self):
        self.turn("在吗", "在呀")
        self.greeting_prompt()
        question = "猫猫怎么样了"
        answer = "猫猫在医院量过体温了，医生说先观察。"
        original = self.service.store.add_turn
        captured = []

        def add_turn(user, assistant):
            with patch.object(self.service, "_make_greeting", return_value=""):
                self.service.greeting(force=True)
            return original(user, assistant)

        def save():
            captured.append(self.greeting_prompt())

        delivery_id = self.service.stage_delivered_turn(question, answer)
        with patch.object(self.service.store, "add_turn", add_turn), \
             patch.object(self.service.store, "save", save):
            self.service.finish_turn(question, answer, delivery_id=delivery_id)
        self.assertEqual(captured[0].count(answer), 1)

    def test_refresh_reads_resolution_for_topic_still_present_in_recent_life(self):
        answer = "猫猫在医院量过体温了，医生说先观察。"
        source = self.turn("猫猫怎么样了", answer)
        self.assertEqual(len(self.service.continuity.apply([dict(
            actor="assistant", state="experience", topic="猫猫", text=answer,
            quote=answer, status="resolved",
        )], now=self.now, **source)), 1)
        self.turn("今晚听什么歌", "听点轻快的吧")
        self.turn("晚饭吃了吗", "吃过了")
        self.service.life._save_state("evening", "2026-10-16", {
            "place": "家里", "activity": "吃晚饭", "mood": "放松", "on_mind": "",
        }, at=self.now)
        with patch.object(self.service.life, "recent_events", return_value=[{
            "text": "猫猫不太对劲，准备带它去医院", "ts": self.now - 600,
        }]):
            self.assertIn(answer, self.greeting_prompt())


if __name__ == "__main__":
    unittest.main()
