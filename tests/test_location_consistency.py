import datetime as dt
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.life import LifeBook
from app.models.memory import MemoryStore
from app.services.chat_service import ChatService


class _OfflineClient:
    pass


class LocationConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = MemoryStore(
            _OfflineClient(),
            path=str(self.directory / "memory.db"),
            embed_model="offline-test",
            cfg={"her_name": "沈知夏"},
        )
        self.addCleanup(self.store.db.close)

    def _life(self) -> LifeBook:
        life = LifeBook(
            self.store.db,
            path=str(self.directory / "life.json"),
            her_name="沈知夏",
            advance_delay_min=0,
            advance_delay_max=0,
        )
        return life

    def test_arrival_updates_life_state_and_her_location_slot_together(self):
        life = self._life()
        now = dt.datetime(2026, 10, 17, 10, 0)
        life.advance(now=now)
        current = life.state()
        life._save_state(
            current["slot"], current["slot_date"],
            {"place": "在车上", "activity": "带团子去医院",
             "mood": current.get("mood", ""), "on_mind": current.get("on_mind", "")},
            at=now.timestamp(), advanced_at=now.timestamp(),
        )
        with patch("app.models.memory_read.time.time", return_value=now.timestamp()):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "在车上"},
            ])

        service = object.__new__(ChatService)
        service.life = life
        service.store = self.store
        service.observe_life_reply("我已经到宠物医院了")

        self.assertEqual(life.state()["place"], "宠物医院")
        her_location = next(
            row for row in self.store.slots_all(limit=-1)
            if row["entity"] == "沈知夏" and row["attr"] == "位置"
        )
        self.assertEqual(her_location["value"], "宠物医院")

    def test_cat_illness_only_cannot_overwrite_her_location(self):
        confirmed_at = 100.0
        with patch("app.models.memory_read.time.time", return_value=confirmed_at):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "宠物医院"},
            ])
        output = {
            "facts": [], "world": [], "constraints": [], "drop": [], "self": [],
            "slots": [
                {"entity": "团子", "attr": "状态", "value": "病后待观察"},
                {"entity": "沈知夏", "attr": "位置", "value": "在车上"},
            ],
        }
        with patch("app.models.memory_write.chat_json", return_value=(output, "")), \
             patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.add_turn("猫猫已经病后待观察了", "嗯，先观察一下")

        rows = {(row["entity"], row["attr"]): row for row in self.store.slots_all(limit=-1)}
        self.assertEqual(rows[("团子", "状态")]["value"], "病后待观察")
        self.assertEqual(rows[("沈知夏", "位置")]["value"], "宠物医院")
        self.assertEqual(rows[("沈知夏", "位置")]["updated_at"], confirmed_at)

    def test_location_without_current_evidence_does_not_refresh_confirmation_time(self):
        confirmed_at = 100.0
        with patch("app.models.memory_read.time.time", return_value=confirmed_at):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "宠物医院"},
            ])
        output = {
            "facts": [], "world": [], "constraints": [], "drop": [], "self": [],
            "slots": [{"entity": "沈知夏", "attr": "位置", "value": "宠物医院"}],
        }
        with patch("app.models.memory_write.chat_json", return_value=(output, "")), \
             patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.add_turn("猫猫已经病后待观察了", "我陪着它休息")

        her_location = next(
            row for row in self.store.slots_all(limit=-1)
            if row["entity"] == "沈知夏" and row["attr"] == "位置"
        )
        self.assertEqual(her_location["value"], "宠物医院")
        self.assertEqual(her_location["updated_at"], confirmed_at)

    def test_life_advance_updates_canonical_location_without_refreshing_on_activity_only(self):
        life = self._life()
        life._save_state("morning", "2026-10-17",
                         {"place": "出租屋", "activity": "看书"}, at=100.0)
        rows = self.store.slots_all(limit=-1)
        self.assertEqual([(row["entity"], row["attr"], row["value"], row["updated_at"])
                          for row in rows], [("沈知夏", "位置", "出租屋", 100.0)])
        life._save_state("morning", "2026-10-17",
                         {"place": "出租屋", "activity": "收拾书包"}, at=200.0)
        self.assertEqual(self.store.slots_all(limit=-1)[0]["updated_at"], 100.0)

    def test_slot_confirmation_updates_life_place_without_advancing_schedule(self):
        life = self._life()
        life._save_state("morning", "2026-10-17",
                         {"place": "车上", "activity": "看猫"}, at=100.0,
                         advanced_at=90.0, next_advance_at=500.0)
        with patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "宠物医院"},
            ])
        state = life.state()
        self.assertEqual(state["place"], "宠物医院")
        self.assertEqual(state["activity"], "看猫")
        self.assertEqual(state["advanced_at"], 90.0)
        self.assertEqual(state["next_advance_at"], 500.0)

    def test_old_life_save_cannot_replace_newer_confirmed_location(self):
        life = self._life()
        life._save_state("morning", "2026-10-17",
                         {"place": "车上", "activity": "看猫"}, at=100.0)
        with patch("app.models.memory_read.time.time", return_value=300.0):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "宠物医院"},
            ])
        life._save_state("morning", "2026-10-17",
                         {"place": "出租屋", "activity": "收拾书包"}, at=200.0)
        self.assertEqual(life.state()["place"], "宠物医院")
        row = self.store.slots_all(limit=-1)[0]
        self.assertEqual((row["value"], row["updated_at"]), ("宠物医院", 300.0))

    def test_location_extraction_requires_matching_current_subject_and_value(self):
        rejected = [
            ("我在宠物医院", "好，等你消息", "宠物医院"),
            ("你到医院了吗", "我还没到宠物医院", "宠物医院"),
            ("后来呢", "我明天到宠物医院", "宠物医院"),
            ("后来呢", "我昨天到宠物医院了", "宠物医院"),
            ("后来呢", "如果我到宠物医院了就告诉你", "宠物医院"),
            ("后来呢", "我到宠物医院了吗？", "宠物医院"),
            ("猫猫呢", "猫猫已经到宠物医院了", "宠物医院"),
            ("后来呢", "我到宠物医院了", "出租屋"),
        ]
        for user, assistant, place in rejected:
            with self.subTest(user=user, assistant=assistant, place=place):
                output = {"slots": [{"entity": "沈知夏", "attr": "位置", "value": place}]}
                with patch("app.models.memory_write.chat_json", return_value=(output, "")):
                    self.assertEqual(self.store._extract(user, assistant)[-1], [])

    def test_explicit_same_location_remains_a_new_confirmation(self):
        output = {"slots": [{"entity": "沈知夏", "attr": "位置", "value": "宠物医院"}]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")), \
             patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.add_turn("你在哪", "我现在还在宠物医院")
        self.assertEqual(self.store.slots_all(limit=-1)[0]["updated_at"], 200.0)

    def test_bad_quote_or_claimed_source_does_not_validate_location(self):
        for evidence in (
            {"quote": "我到出租屋了", "source": "assistant"},
            {"quote": "我到宠物医院了", "source": "user"},
        ):
            with self.subTest(evidence=evidence):
                output = {"slots": [{"entity": "沈知夏", "attr": "位置",
                                     "value": "宠物医院", **evidence}]}
                with patch("app.models.memory_write.chat_json", return_value=(output, "")):
                    self.assertEqual(self.store._extract("你在哪", "我到宠物医院了")[-1], [])

    def test_current_location_ignores_activity_text_and_second_person(self):
        life = self._life()
        life._save_state("morning", "2026-10-17", {"place": "学校", "activity": "休息"}, at=100.0)
        for response, expected in (
            ("我现在还在宠物医院", "宠物医院"),
            ("我在家里看书", "家里"),
            ("我回家了", "家里"),
            ("你回家了，我也放心了", "家里"),
            ("我现在在想明天吃什么", "家里"),
        ):
            with self.subTest(response=response):
                life.observe_assistant_update(response)
                self.assertEqual(life.state()["place"], expected)

    def test_location_confirmation_is_subject_bound_even_in_long_response(self):
        rejected = (
            "你问我到宠物医院了没有", "我听说邻居在宠物医院", "刚才医生说猫在宠物医院",
            "我想起昨天在宠物医院的事", "我在想去宠物医院要带什么", "我已经到宠物医院门口了吗",
            "我在宠物医院？", "我到宠物医院了?",
            "在宠物医院等我", "在宠物医院休息吧", "到了宠物医院给我发消息", "我在等你到宠物医院",
        )
        for response in rejected:
            with self.subTest(response=response):
                output = {"slots": [{"entity": "沈知夏", "attr": "位置", "value": "宠物医院"}]}
                with patch("app.models.memory_write.chat_json", return_value=(output, "")):
                    self.assertEqual(self.store._extract("后来呢", response)[-1], [])

    def test_waiting_for_user_at_a_place_is_still_her_location(self):
        life = self._life()
        life._save_state("morning", "2026-10-17", {"place": "学校", "activity": "休息"}, at=100.0)
        life.observe_assistant_update("我在宠物医院等你")
        self.assertEqual(life.state()["place"], "宠物医院")

    def test_location_batch_rolls_back_life_state_when_later_slot_is_invalid(self):
        life = self._life()
        life._save_state("morning", "2026-10-17", {"place": "车上", "activity": "休息"}, at=100.0)
        with patch("time.time", return_value=200.0), self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "家里"},
                {"entity": None, "attr": "状态", "value": "无效"},
            ])
        self.assertEqual(life.state()["place"], "车上")
        self.assertEqual(self.store.slots_all(-1)[0]["value"], "车上")

    def test_valid_location_batch_rolls_back_if_second_write_fails(self):
        life = self._life()
        life._save_state("morning", "2026-10-17", {"place": "车上", "activity": "休息"}, at=100.0)
        self.store.db.execute(
            "CREATE TRIGGER reject_status BEFORE INSERT ON slots WHEN NEW.attr='状态' "
            "BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
        with patch("time.time", return_value=200.0), self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "家里"},
                {"entity": "团子", "attr": "状态", "value": "病后待观察"},
            ])
        self.assertEqual(life.state()["place"], "车上")
        self.assertEqual(self.store.slots_all(-1)[0]["value"], "车上")

    def test_new_location_confirmed_while_old_save_waits_is_not_overwritten(self):
        life = self._life()
        life._save_state("morning", "2026-10-17", {"place": "车上", "activity": "休息"}, at=100.0)
        lock = threading.RLock()
        waiting = threading.Event()
        errors = []

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is worker:
                    waiting.set()
                lock.acquire()
                return self

            def __exit__(self, *args):
                lock.release()

        def save_old():
            try:
                life._save_state("morning", "2026-10-17", {
                    "place": "出租屋", "activity": "收拾书包",
                }, at=200.0)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=save_old)
        life._lock_source = self.store
        self.store._lock = ObservedLock()
        with lock:
            worker.start()
            self.assertTrue(waiting.wait(3))
            with patch("time.time", return_value=300.0):
                self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "宠物医院"}])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(life.state()["place"], "宠物医院")
        self.assertEqual(self.store.slots_all(-1)[0]["updated_at"], 300.0)

    def test_unrelated_future_or_question_clause_does_not_hide_current_arrival(self):
        life = self._life()
        for response in ("我到宠物医院了，明天再复查", "我在宠物医院，你吃饭了吗？"):
            with self.subTest(response=response):
                life._save_state("morning", "2026-10-17", {"place": "车上", "activity": "休息"})
                life.observe_assistant_update(response)
                self.assertEqual(life.state()["place"], "宠物医院")

    def test_activity_observation_keeps_location_confirmed_while_waiting_for_lock(self):
        life = self._life()
        life._lock_source = self.store
        life._save_state("morning", "2026-10-17", {"place": "车上", "activity": "休息"}, at=100.0)
        waiting = threading.Event()
        lock = threading.RLock()

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is worker:
                    waiting.set()
                lock.acquire()
                return self

            def __exit__(self, *args):
                lock.release()

        worker = threading.Thread(target=lambda: life._update_observed_state({"activity": "量完体温"}))
        self.store._lock = ObservedLock()
        with lock:
            worker.start()
            self.assertTrue(waiting.wait(3))
            with patch("time.time", return_value=300.0):
                self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "宠物医院"}])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(life.state()["place"], "宠物医院")



if __name__ == "__main__":
    unittest.main()
