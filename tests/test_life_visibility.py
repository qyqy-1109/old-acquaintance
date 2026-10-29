import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import db as memory_db
from app.models.life import LifeBook
from app.models.memory import MemoryStore
from app.services.chat_service import ChatService
from app.services.speak import Speaker


class LifeVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "memory.db"
        self.db = memory_db.connect(str(self.path))
        self.addCleanup(self.db.close)
        self.life = LifeBook(self.db, path=str(self.root / "life.json"), her_name="沈知夏")
        self.timestamp = dt.datetime(2026, 10, 5, 15, 30).timestamp()
        self.text = "下课后去球场打了两场球"
        with self.db:
            self.event_id = self.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.timestamp, "afternoon", self.text, "model"),
            ).lastrowid

    def test_hidden_event_leaves_board_but_keeps_original_record(self):
        message, payload = self.life.delete_event(self.event_id)
        self.assertEqual(self.life.recent_events(), [])
        row = self.db.execute("SELECT * FROM life_events WHERE id=?", (self.event_id,)).fetchone()
        self.assertIsNotNone(row, "隐藏不能物理删除她的经历")
        self.assertEqual((row["ts"], row["slot"], row["text"], row["source"]),
                         (self.timestamp, "afternoon", self.text, "model"))
        self.assertIn("隐藏", message)
        self.assertEqual(payload["id"], self.event_id)

    def test_hidden_event_remains_recallable_as_past(self):
        self.life.delete_event(self.event_id)
        history = self.life.history_for("你之前做过什么？")
        self.assertIn(self.text, history)
        self.assertIn("隐藏", history)
        self.assertEqual(self.life.history_for("今天的天气怎么样？"), "")

    def test_hidden_history_survives_reopening(self):
        self.life.delete_event(self.event_id)
        reopened_db = memory_db.connect(str(self.path))
        self.addCleanup(reopened_db.close)
        reopened = LifeBook(reopened_db, path=str(self.root / "life.json"))
        self.assertEqual(reopened.recent_events(), [])
        self.assertIn(self.text, reopened.history_for("你最近经历过什么？"))

    def test_hiding_does_not_change_current_state_or_script(self):
        with self.db:
            self.db.execute("INSERT INTO life_state(id,place,activity,on_mind) VALUES(1,?,?,?)",
                            ("出租屋", "写作业", "期末考试"))
        state = self.life.state()
        script = (self.root / "life.json").read_bytes()
        self.life.delete_event(self.event_id)
        self.assertEqual(self.life.state(), state)
        self.assertEqual((self.root / "life.json").read_bytes(), script)
        self.assertEqual(self.db.execute("SELECT count(*) FROM life_events").fetchone()[0], 1)

    def test_hiding_twice_is_idempotent(self):
        self.life.delete_event(self.event_id)
        original = self.db.execute("SELECT * FROM life_events WHERE id=?", (self.event_id,)).fetchone()
        self.assertIsNotNone(original)
        message, payload = self.life.delete_event(self.event_id)
        self.assertIn("已经隐藏", message)
        self.assertIsNone(payload)
        self.assertEqual(dict(self.db.execute("SELECT * FROM life_events WHERE id=?",
                                             (self.event_id,)).fetchone()), dict(original))
        self.assertIsNone(self.life.delete_event("abc")[1])
        self.assertIsNone(self.life.delete_event(9999)[1])

    def test_hidden_event_is_not_fresh_material_for_speaking(self):
        speaker = Speaker(self.db, self.life)
        self.assertTrue(speaker._has_fresh_life(self.timestamp - 60))
        self.life.delete_event(self.event_id)
        self.assertFalse(speaker._has_fresh_life(0))
        self.assertFalse(speaker._has_fresh_life(self.timestamp - 60))
        self.assertIn(self.text, self.life.history_for("你之前去过哪儿？"))
        with self.db:
            self.db.execute("INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                            (self.timestamp + 60, "afternoon", "回屋开始写作业", "model"))
        self.assertTrue(speaker._has_fresh_life(self.timestamp))

    def test_legacy_events_can_be_hidden_without_losing_history(self):
        legacy_path = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as legacy, legacy:
            legacy.execute("CREATE TABLE life_events (id INTEGER PRIMARY KEY, ts REAL, "
                           "slot TEXT, text TEXT, source TEXT)")
            legacy.execute("INSERT INTO life_events VALUES(1,?,?,?,?)",
                           (self.timestamp, "afternoon", self.text, "model"))
        migrated_db = memory_db.connect(str(legacy_path))
        self.addCleanup(migrated_db.close)
        migrated = LifeBook(migrated_db, path=str(self.root / "life.json"))
        self.assertEqual(migrated.recent_events()[0]["text"], self.text)
        migrated.delete_event(1)
        self.assertEqual(migrated.recent_events(), [])
        self.assertIn(self.text, migrated.history_for("你之前做过什么？"))

    def test_concurrent_hiding_keeps_history_and_reports_one_change(self):
        def hide():
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                life = LifeBook(connection, path=str(self.root / "life.json"))
                return life.delete_event(self.event_id)[1]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda unused: hide(), range(4)))
        self.assertEqual(sum(payload is not None for payload in results), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM life_events").fetchone()[0], 1)
        self.assertIn(self.text, self.life.history_for("你之前做过什么？"))

    def test_service_concurrent_hiding_on_shared_connection_records_once(self):
        store = MemoryStore(object(), path=str(self.path), embed_model="offline-test")
        self.addCleanup(store.db.close)
        service = ChatService.__new__(ChatService)
        service.store = store
        service.life = LifeBook(store.db, path=str(self.root / "life.json"))
        barrier = Barrier(8)
        errors = []
        change_counts = []
        rounds = 80

        def hide_shared(event_id):
            barrier.wait(timeout=5)
            try:
                return service.delete_life_event(event_id)
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                return ""

        with ThreadPoolExecutor(max_workers=8) as pool:
            for iteration in range(rounds):
                with self.db:
                    event_id = self.db.execute(
                        "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                        (self.timestamp + iteration, "afternoon", self.text, "model"),
                    ).lastrowid
                messages = list(pool.map(hide_shared, [event_id] * 8))
                change_counts.append(sum("已从看板隐藏" in message for message in messages))

        self.assertEqual(errors, [], f"共用连接并发隐藏出错：{errors[:5]}")
        self.assertEqual(change_counts, [1] * rounds)
        self.assertEqual(store.events_stats().get("life_event_hidden"), rounds)
        self.assertEqual(self.db.execute("SELECT count(*) FROM life_events").fetchone()[0], rounds + 1)


if __name__ == "__main__":
    unittest.main()
