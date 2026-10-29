import datetime as dt
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.memory import MemoryStore


class _OfflineClient:
    pass


class SlotFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path("_tmptest/state-display-200-201/cases") / f"slots-{os.getpid()}-{id(self)}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        self.store = MemoryStore(
            _OfflineClient(),
            path=str(self.directory / "memory.db"),
            embed_model="offline-test",
            cfg={"her_name": "沈知夏"},
        )
        self.addCleanup(self.store.db.close)

    def test_same_value_refreshes_last_confirmation_without_counting_change(self):
        slot = {"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}
        with patch("app.models.memory_read.time.time", return_value=100.0):
            self.assertEqual(self.store.upsert_slots([slot]), 1)
        with patch("app.models.memory_read.time.time", return_value=200.0):
            self.assertEqual(self.store.upsert_slots([slot]), 0)
        row = self.store.slots_all()[0]
        self.assertEqual(row["updated_at"], 200.0)
        self.assertEqual(self.store.events_stats().get("slots"), 1)

    def test_render_slots_marks_location_as_last_confirmed(self):
        with patch("app.models.memory_read.time.time", return_value=dt.datetime(2026, 10, 17, 8).timestamp()):
            self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}])
        rendered = self.store.render_slots(self.store.slots_all())
        self.assertIn("沈知夏·位置：在车上，正前往宠物医院", rendered)
        self.assertIn("最后确认", rendered)
        self.assertIn("2026-10-17 08:00", rendered)
        self.assertIn("之后未确认", rendered)
        self.assertIn("沈知夏·位置", self.store.render_slots())

    def test_render_slots_uses_only_supplied_rows_and_handles_unknown_time(self):
        self.store.upsert_slots([{"entity": "用户", "attr": "位置", "value": "公司"}])
        self.assertEqual(self.store.render_slots([]), "")
        rendered = self.store.render_slots([
            {"entity": "沈知夏", "attr": "状态", "value": "刚忙完手头的事", "updated_at": None},
        ])
        self.assertIn("确认时间未知", rendered)
        self.assertNotIn("用户", rendered)

    def test_extraction_prompt_contains_existing_slots_and_confirmation_time(self):
        confirmed_at = dt.datetime(2026, 10, 17, 8).timestamp()
        with patch("app.models.memory_read.time.time", return_value=confirmed_at):
            self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}])
        output = {"facts": [], "world": [], "constraints": [], "drop": [], "self": [], "slots": []}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")) as extraction:
            self.store._extract("团子状态怎么样了", "病后待观察，明天量体温正常就带下楼适应")
        prompt = extraction.call_args.args[2][0]["content"]
        self.assertIn("【最近确认的状态槽位】", prompt)
        self.assertIn("沈知夏·位置：在车上，正前往宠物医院", prompt)
        self.assertIn("最后确认", prompt)
        self.assertIn("2026-10-17 08:00", prompt)
        self.assertEqual(extraction.call_count, 1)
        self.assertEqual(self.store._llm_calls, 1)

    def test_extraction_prioritizes_mentioned_entity_before_context_limit(self):
        with patch("app.models.memory_read.time.time", return_value=100.0):
            self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}])
        for index in range(13):
            with patch("app.models.memory_read.time.time", return_value=200.0 + index):
                self.store.upsert_slots([{"entity": f"邻居{index}", "attr": "状态", "value": f"测试背景{index}"}])
        with patch("app.models.memory_write.chat_json", return_value=({}, "")) as extraction:
            self.store._extract("沈知夏到哪了", "还没说到哪了")
        prompt = extraction.call_args.args[2][0]["content"]
        self.assertIn("沈知夏·位置：在车上，正前往宠物医院", prompt)

    def test_extraction_keeps_all_fields_for_her_entity(self):
        with patch("app.models.memory_read.time.time", return_value=100.0):
            self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}])
        with patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": f"属性{i}", "value": f"值{i}"}
                for i in range(13)
            ])
        output = {"facts": [], "world": [], "constraints": [], "drop": [], "self": [], "slots": []}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")) as extraction:
            self.store._extract("你到哪了", "我刚到医院")
        prompt = extraction.call_args.args[2][0]["content"]
        self.assertIn("沈知夏·位置：在车上，正前往宠物医院", prompt)
        self.assertNotIn("邻居0·状态", prompt)

    def test_cat_status_update_does_not_clear_her_location(self):
        with patch("app.models.memory_read.time.time", return_value=100.0):
            self.store.upsert_slots([
                {"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"},
                {"entity": "沈知夏", "attr": "状态", "value": "正带团子去看医生"},
            ])
        with patch("app.models.memory_read.time.time", return_value=200.0):
            self.store.upsert_slots([
                {"entity": "团子", "attr": "状态", "value": "病后待观察"},
                {"entity": "沈知夏", "attr": "状态", "value": "刚忙完手头的事"},
            ])
        rows = {(row["entity"], row["attr"]): row for row in self.store.slots_all()}
        self.assertEqual(rows[("沈知夏", "位置")]["value"], "在车上，正前往宠物医院")
        self.assertEqual(rows[("沈知夏", "位置")]["updated_at"], 100.0)

    def test_extracted_arrival_updates_location_and_can_mark_unknown_destination(self):
        self.store.upsert_slots([{"entity": "沈知夏", "attr": "位置", "value": "在车上，正前往宠物医院"}])
        for response, location in (
            ("已经到宠物医院了", "宠物医院"),
            ("已经离开宠物医院了，还没决定去哪", "当前位置待确认"),
        ):
            with self.subTest(response=response):
                output = {"slots": [{"entity": "沈知夏", "attr": "位置", "value": location}]}
                with patch("app.models.memory_write.chat_json", return_value=(output, "")):
                    slots = self.store._extract("后来呢", response)[-1]
                self.store.upsert_slots(slots)
                self.assertEqual(self.store.slots_all()[0]["value"], location)

if __name__ == "__main__":
    unittest.main()
