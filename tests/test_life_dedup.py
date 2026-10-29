import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models import db as memory_db
from app.models.life import LifeBook


class SameActivityClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"candidates": [{"place": "出租屋", "activity": "听歌",
                                                  "mood": "平静", "on_mind": "", "event": "同一件事"}],
                                      "choice": 0}, ensure_ascii=False)))])


class LifeDedupTests(unittest.TestCase):
    def test_dedup_fallback_does_not_repeat_same_slot_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db = memory_db.connect(str(Path(directory) / "memory.db"))
            life = LifeBook(db, path=str(Path(directory) / "life.json"),
                            advance_delay_min=0, advance_delay_max=0)
            client = SameActivityClient()
            try:
                life.advance(client=client, model="test", now=dt.datetime(2026, 10, 7, 23, 30), force=True)
                life.advance(client=client, model="test", now=dt.datetime(2026, 10, 8, 23, 30), force=True)
                life.advance(client=client, model="test", now=dt.datetime(2026, 10, 9, 23, 30), force=True)
                rows = db.execute("SELECT text FROM life_events ORDER BY id").fetchall()
                self.assertEqual(len(rows), 3)
                self.assertEqual(len({row["text"] for row in rows}), 3,
                                 "去重兜底不能在相同时间段跨天再次写出相同流水")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
