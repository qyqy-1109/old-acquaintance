import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import db as memory_db
from app.models.life import LifeBook


class CatchUpClient:
    def __init__(self, fail_summary=False):
        self.prompts = []
        self.fail_summary = fail_summary
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        if '"summary"' in prompt:
            content = {} if self.fail_summary else {
                "summary": "这几天有课去学校，闲时在出租屋写作业，回家后照看团子。"
            }
        else:
            content = {"candidates": [{
                "place": "出租屋", "activity": "整理课程笔记",
                "mood": "平静", "on_mind": "", "event": f"整理课程笔记第{len(self.prompts)}段"
            }], "choice": 0}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(content, ensure_ascii=False)))])


class LifeCatchUpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db = memory_db.connect(str(root / "memory.db"))
        self.addCleanup(self.db.close)
        self.life = LifeBook(self.db, path=str(root / "life.json"),
                             advance_delay_min=0, advance_delay_max=0)
        self.last = dt.datetime(2026, 10, 7, 7, 30)
        self.now = dt.datetime(2026, 10, 15, 9, 45)
        self.life.advance(now=self.last)

    def added_events(self):
        return [dict(row) for row in self.db.execute(
            "SELECT ts,slot,text,source FROM life_events WHERE ts>? ORDER BY id",
            (self.last.timestamp(),))]

    def test_long_gap_keeps_summary_and_the_latest_four_historical_slots(self):
        self.assertEqual(self.life.missed_slots(self.now), 65)
        client = CatchUpClient()
        self.life.catch_up(client=client, model="offline-test", now=self.now)
        events = self.added_events()
        summaries = [row for row in events if row["source"] == "catchup_summary"]
        self.assertEqual(len(summaries), 1, "远期的60格不能直接跳过")
        summary = summaries[0]
        self.assertIn("2026-10-07 09:30", summary["text"])
        self.assertIn("2026-10-14 17:30", summary["text"])
        self.assertIn("60", summary["text"])
        self.assertIn("照看团子", summary["text"])
        detailed = [row for row in events if row["source"] == "model"]
        self.assertEqual([
            dt.datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M")
            for row in detailed
        ], ["2026-10-14 19:30", "2026-10-14 23:30", "2026-10-15 00:30",
            "2026-10-15 06:30", "2026-10-15 09:45"])
        self.assertLessEqual(len(client.prompts), 6)
        self.assertEqual(self.life.state()["advanced_at"], self.now.timestamp())
        self.assertEqual(self.life.missed_slots(self.now), 0)

    def test_summary_is_past_context_for_recent_details_and_recall(self):
        client = CatchUpClient()
        self.life.catch_up(client=client, model="offline-test", now=self.now)
        detail_prompts = [prompt for prompt in client.prompts if '"summary"' not in prompt]
        self.assertTrue(all("照看团子" in prompt for prompt in detail_prompts),
                        "最近几格要接得上压缩后的历史")
        history = self.life.history_for("你最近经历过什么？")
        self.assertIn("分段补记摘要", history)
        self.assertNotIn("照看团子。（系统兜底）", history)

    def test_unavailable_summary_preserves_gap_without_inventing_details(self):
        self.life.catch_up(now=self.now)
        summaries = [row for row in self.added_events()
                     if row["source"] == "catchup_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("2026-10-07 09:30", summaries[0]["text"])
        self.assertIn("2026-10-14 17:30", summaries[0]["text"])
        self.assertIn("未逐格记录", summaries[0]["text"])
        self.assertEqual(self.life.state()["slot"], "forenoon")

    def test_malformed_summary_falls_back_and_continues_details(self):
        self.life.catch_up(client=CatchUpClient(fail_summary=True),
                           model="offline-test", now=self.now)
        events = self.added_events()
        summaries = [row for row in events if row["source"] == "catchup_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("未逐格记录", summaries[0]["text"])
        self.assertEqual(sum(row["source"] == "model" for row in events), 5)

    def test_catchup_is_idempotent_and_short_gaps_stay_detailed(self):
        client = CatchUpClient()
        self.life.catch_up(client=client, model="offline-test", now=self.now)
        original = self.added_events()
        calls = len(client.prompts)
        self.life.catch_up(client=client, model="offline-test", now=self.now)
        self.assertEqual(self.added_events(), original)
        self.assertEqual(len(client.prompts), calls)
        self.life.catch_up(client=client, model="offline-test",
                           now=dt.datetime(2026, 10, 15, 15, 30))
        fresh = self.added_events()[len(original):]
        self.assertEqual([row["slot"] for row in fresh], ["noon", "afternoon"])
        self.assertTrue(all(row["source"] == "model" for row in fresh))

    def test_catchup_finishes_current_slot_even_before_its_random_delay(self):
        self.life.advance_delay_min = self.life.advance_delay_max = 50
        current = dt.datetime(2026, 10, 15, 9, 10)
        self.life.catch_up(client=CatchUpClient(), model="offline-test", now=current)
        self.assertEqual(self.life.missed_slots(current), 0)
        self.assertEqual(self.life.state()["advanced_at"], current.timestamp())


if __name__ == "__main__":
    unittest.main()
