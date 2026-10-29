"""Promise reminders use real gates, persistence, filtering and delivery."""

import datetime as dt
import json
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService


class PromiseReminderTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.now = dt.datetime(2026, 10, 6, 15, 0).timestamp()
        settings = {
            "DATA_DIR": self.directory, "API_KEY": "test-key",
            "CHAT_API_KEY": "test-key", "EMBED_API_KEY": "test-key",
            "AMAP_KEY": "", "AMAP_CITY": "", "PROACTIVE": True,
            "PROACTIVE_QUIET": (0, 0), "PROACTIVE_DAILY_MAX": 6,
            "PROACTIVE_MIN_GAP_H": 0, "PROACTIVE_QUIET_MIN": 5,
            "PROACTIVE_LOW_CHANCE": 1.0, "PROACTIVE_HIGH_CHANCE": 1.0,
            "PROACTIVE_CHANCE_CEIL": 1.0, "PROACTIVE_ROLL_EVERY_MIN": 0,
        }
        for name, value in settings.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch("time.time", return_value=self.now))
        self.stack.enter_context(patch("random.random", return_value=0))
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.service.life.advance(now=dt.datetime.fromtimestamp(self.now), force=True)
        with self.service.store.db:
            self.service.store.db.execute("UPDATE life_events SET ts=?", (self.now - 3600,))
        self.service.speaker._set_ts("user_last_ts", self.now - 7200)
        self.service.relationship.data["promises"] = [{
            "id": "p1", "text": "给你带后街的烧烤", "status": "open",
            "created_at": self.now - 7200, "due_at": self.now - 60,
        }]
        self.service.relationship.save()
        self.payload = {"speak": True, "text": "我还记着给你带后街的烧烤", "reminder_ids": ["p1"],
                        "motive_ids": ["promise:p1"]}
        with self.service.store.db:
            cur = self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 90, "afternoon", "推荐列表里遇到一段新旋律", "model"))
        self.life_motive = next(row["id"] for row in self.service.speaker.motive_candidates(self.now)
                                if row["kind"] == "life")
        self.prompts = []
        self.on_model = None

        def create(**kwargs):
            self.prompts.append(kwargs["messages"][0]["content"])
            if self.on_model is not None:
                self.on_model()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False)))])

        self.service.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    def promise(self):
        return self.service.relationship.promise_items(include_closed=True)[0]

    def test_due_promise_can_be_raised_after_this_slots_report(self):
        self.service.speaker._set_ts("her_last_ts", self.now - 600)
        self.assertEqual(self.service.proactive_tick(), "我还记着给你带后街的烧烤")
        self.assertIn("p1", self.prompts[0])
        self.assertIn("给你带后街的烧烤", self.prompts[0])
        self.assertEqual(len(self.service.speaker.pending_items()), 1)
        self.assertEqual(self.promise().get("reminded_at"), self.now)
        self.assertEqual(self.promise()["status"], "open")

    def test_due_promise_still_obeys_chat_and_silence_wait(self):
        self.service.speaker._set_ts("user_last_ts", self.now - 60)
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertFalse(self.promise().get("reminded_at"))
        self.service.speaker._set_ts("user_last_ts", self.now - 7200)
        self.service.speaker._set_ts("declined_at", self.now - 60)
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])

    def test_only_surviving_reminder_bubble_consumes_promise(self):
        self.service.speaker.enqueue("我还记着给你带后街的烧烤")
        self.service.speaker._set_ts("her_last_ts", self.now - 7200)
        self.payload["text"] = "我还记着给你带后街的烧烤\n@@@\n刚听到首挺好听的歌"
        self.payload["motive_ids"].append(self.life_motive)
        self.assertEqual(self.service.proactive_tick(), "刚听到首挺好听的歌")
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(len(self.service.speaker.pending_items()), 2)

    def test_unmentioned_or_unknown_ids_are_not_consumed(self):
        self.payload["text"] = "刚听到首挺好听的歌"
        self.payload["motive_ids"] = [self.life_motive]
        self.assertEqual(self.service.proactive_tick(), "刚听到首挺好听的歌")
        self.assertFalse(self.promise().get("reminded_at"))

    def test_stale_fulltext_promise_is_suppressed_even_without_declared_id(self):
        self.on_model = lambda: self.service.relationship.complete_promise("p1")
        self.payload.update(reminder_ids=[])
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.promise()["status"], "done")

    def test_stale_fulltext_promise_is_suppressed_with_unknown_declared_id(self):
        self.on_model = lambda: self.service.relationship.complete_promise("p1")
        self.payload.update(reminder_ids=["p999"])
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.promise()["status"], "done")
        self.service.speaker._set_ts("her_last_ts", self.now - 7200)
        self.payload.update(text="我还记着给你带后街的烧烤", reminder_ids=["p999"])
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertFalse(self.promise().get("reminded_at"))

    def test_enqueue_failure_leaves_promise_for_retry(self):
        self.service.store.db.execute(
            "CREATE TRIGGER fail_pending BEFORE INSERT ON pending_say "
            "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END")
        self.service.store.db.commit()
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.service.store.db.execute("DROP TRIGGER fail_pending")
        self.service.store.db.commit()
        self.assertEqual(self.service.proactive_tick(), "我还记着给你带后街的烧烤")
        self.assertEqual(self.promise().get("reminded_at"), self.now)

    def test_user_speaking_during_generation_suppresses_delivery(self):
        self.on_model = self.service.speaker.touch_user
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(self.service.speaker.said_today(), 0)

    def test_user_speaking_while_enqueue_waits_on_store_lock_suppresses_delivery(self):
        real_lock = self.service.store._lock
        worker_entering = threading.Event()

        class TrackingLock:
            def __enter__(inner):
                if threading.current_thread() is worker:
                    worker_entering.set()
                real_lock.acquire()
                return inner

            def __exit__(inner, exc_type, exc, tb):
                real_lock.release()
                return False

            def acquire(inner, *args, **kwargs):
                return real_lock.acquire(*args, **kwargs)

            def release(inner):
                real_lock.release()

        self.service.store._lock = TrackingLock()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.service.proactive_tick()))
        with real_lock:
            worker.start()
            self.assertTrue(worker_entering.wait(5))
            self.service.speaker.touch_user()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [""])
        self.assertEqual(self.service.speaker.pending_items(), [])

    def test_due_promise_obeys_quiet_cap_and_probability(self):
        with patch.object(config, "PROACTIVE_QUIET", (14, 16)):
            self.assertEqual(self.service.proactive_tick(), "")
        for order in range(6):
            self.service.store.event("proactive", text=f"previous {order}", n=1)
        self.assertEqual(self.service.proactive_tick(), "")
        with self.service.store.db:
            self.service.store.db.execute("DELETE FROM events WHERE kind='proactive'")
        self.service.speaker._set_ts("user_last_ts", self.now - 600)
        with patch.object(config, "PROACTIVE_LOW_CHANCE", 0), \
             patch("random.random", return_value=0.9):
            self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertFalse(self.promise().get("reminded_at"))

    def test_ownership_filtered_reminder_does_not_consume_promise(self):
        self.service.relationship.data["promises"][0]["text"] = "团子没扒窗户吧"
        self.service.relationship.save()
        self.payload["text"] = "团子没扒窗户吧\n@@@\n刚听到首挺好听的歌"
        self.payload["motive_ids"].append(self.life_motive)
        self.assertEqual(self.service.proactive_tick(), "刚听到首挺好听的歌")
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(self.service.speaker.pending_items()[0]["text"], "刚听到首挺好听的歌")

    def test_silence_does_not_consume_promise(self):
        self.payload.update(speak=False, text="")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(self.service.speaker.said_today(), 0)

    def test_failed_reminder_persistence_preserves_delivered_history_and_cap(self):
        self.stack.enter_context(patch.object(self.service.speaker, "mark_motive_reminded",
                                             side_effect=OSError("模拟入队后的关系文件写失败")))
        self.assertEqual(self.service.proactive_tick(), "我还记着给你带后街的烧烤")
        self.assertEqual(self.service.speaker.pending_items()[0]["text"], "我还记着给你带后街的烧烤")
        self.assertFalse(self.promise().get("reminded_at"))
        self.assertEqual(self.service.store.sessions["default"][-1]["content"], "我还记着给你带后街的烧烤")
        self.assertEqual(self.service.speaker.said_today(), 1)

    def test_promise_closed_during_generation_suppresses_stale_reminder(self):
        self.on_model = lambda: self.service.relationship.complete_promise("1")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.promise()["status"], "done")
        self.assertFalse(self.promise().get("reminded_at"))

    def test_concurrent_ticks_enqueue_once(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        def hold_model():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("second tick did not finish")

        self.on_model = hold_model
        first = threading.Thread(target=lambda: results.append(self.service.proactive_tick()))
        first.start()
        self.assertTrue(entered.wait(5))
        try:
            self.assertEqual(self.service.proactive_tick(), "")
        finally:
            release.set()
            first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(results, ["我还记着给你带后街的烧烤"])
        self.assertEqual(len(self.service.speaker.pending_items()), 1)
        self.assertEqual(self.service.speaker.said_today(), 1)


if __name__ == "__main__":
    unittest.main()
