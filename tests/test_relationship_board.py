import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controllers.chat_controller import router
from app.models.relationship import Relationship


class RelationshipBoardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "relationship.json"
        self.rel = Relationship(str(self.path))

    def test_legacy_ids_survive_completion_and_reload(self):
        self.rel.data["promises"] = ["带伞", {"text": "看电影", "status": "open"}]
        self.rel.save()
        self.rel = Relationship(str(self.path))
        rows = self.rel.promise_items()
        self.assertEqual([row.get("id") for row in rows], ["p1", "p2"])
        self.rel.complete_promise("p1")
        self.assertIn("看电影", self.rel.miss_promise("p2", "临时加班"))
        stored = Relationship(str(self.path)).promise_items(include_closed=True)
        self.assertEqual([(row["id"], row["status"]) for row in stored],
                         [("p1", "done"), ("p2", "missed")])
        self.assertIn("p2", self.rel.report())

    def test_due_query_never_guesses_time_or_changes_status(self):
        self.assertTrue(callable(getattr(self.rel, "due_promises", None)))
        self.rel.add_promise("没有明确时间")
        self.rel.add_promise("带伞", due_at=2000)
        self.rel.add_promise("看电影", due_at=3000)
        self.assertEqual(self.rel.due_promises(now=1999), [])
        self.assertEqual([row["id"] for row in self.rel.due_promises(now=2000)], ["p2"])
        self.assertEqual(self.rel.promise_items()[1]["status"], "open")
        self.assertTrue(self.rel.mark_promise_reminded("p2", now=2000))
        self.assertFalse(self.rel.mark_promise_reminded("p2", now=2001))
        self.assertFalse(self.rel.mark_promise_reminded("p3", now=2001))
        self.assertEqual(Relationship(str(self.path)).due_promises(now=2002), [])
        self.assertEqual([row["kind"] for row in self.rel.events()].count("promise_reminded"), 1)

    def test_reminder_write_failure_keeps_promise_retryable(self):
        self.assertTrue(callable(getattr(self.rel, "mark_promise_reminded", None)))
        self.rel.add_promise("带伞", due_at=2000)
        with patch.object(self.rel, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.rel.mark_promise_reminded("p1", now=2000)
        self.assertEqual([row["id"] for row in self.rel.due_promises(now=2001)], ["p1"])
        self.assertNotIn("promise_reminded", [row["kind"] for row in self.rel.events()])

    def test_command_transition_write_failure_preserves_open_state(self):
        for status in ("done", "missed"):
            with self.subTest(status=status):
                row = self.rel.create_promise(f"失败时可重试 {status}")
                event_count = len(self.rel.events())
                with patch.object(self.rel, "save", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        if status == "done":
                            self.rel.complete_promise(row["id"])
                        else:
                            self.rel.miss_promise(row["id"], "临时加班")
                snapshot = next(item for item in self.rel.promise_items(include_closed=True)
                                if item["id"] == row["id"])
                self.assertEqual(snapshot["status"], "open")
                self.assertEqual(len(self.rel.events()), event_count)

    def test_concurrent_completion_is_one_transition(self):
        self.assertTrue(callable(getattr(self.rel, "update_promise", None)))
        self.rel.add_promise("带伞")
        def complete(_index):
            try:
                return self.rel.update_promise("p1", "done")
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(complete, range(20)))
        self.assertEqual(sum(result is not None for result in results), 1)
        loaded = Relationship(str(self.path))
        self.assertEqual([row["kind"] for row in loaded.events()].count("promise_completed"), 1)
        self.assertEqual(loaded.promise_items(include_closed=True)[0]["status"], "done")

    def test_independent_relationships_do_not_share_default_promises(self):
        self.rel.add_promise("属于第一位用户")
        other = Relationship(str(Path(self.directory.name) / "other.json"))
        self.assertEqual(other.promise_items(), [])

    def make_client(self):
        app = FastAPI()
        app.include_router(router)
        service = SimpleNamespace(relationship=self.rel, _user_msg_count=lambda: 60)
        other = SimpleNamespace(relationship=Relationship(str(Path(self.directory.name) / "other.json")),
                                _user_msg_count=lambda: 0)
        app.state.service = service
        app.state.registry = SimpleNamespace(get=lambda uid: other)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_api_lifecycle_is_isolated_and_keeps_history(self):
        client = self.make_client()
        with patch("app.services.auth.required", return_value=False):
            created = client.post("/api/relationship/promises", json={"text": "带伞", "due_at": 2000})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["promise"]["id"], "p1")
            self.assertEqual(client.get("/api/relationship", headers={"X-User-Id": "other"}).json()["promises"], [])
            board = client.get("/api/relationship").json()
            self.assertEqual(board["stage"]["key"], "刚认识")
            self.assertEqual(board["promises"][0]["status"], "open")
            self.assertTrue(board["promises"][0]["overdue"])
            self.assertEqual(client.post("/api/relationship/promises/p1/miss", json={"reason": " "}).status_code, 422)
            completed = client.post("/api/relationship/promises/p1/complete", json={"note": "已经送到"})
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(completed.json()["promise"]["status"], "done")
            self.assertEqual(completed.json()["promise"]["note"], "已经送到")
            self.assertEqual(client.post("/api/relationship/promises/p1/miss", json={"reason": "迟到了"}).status_code, 409)
            self.assertEqual(client.post("/api/relationship/promises/p999/complete", json={}).status_code, 404)
            self.assertEqual(client.post("/api/relationship/promises/1/complete", json={}).status_code, 404)
            self.assertEqual(client.get("/api/relationship").json()["promises"][0]["note"], "已经送到")
            client.post("/api/relationship/promises", json={"text": "陪他看电影"})
            missed = client.post("/api/relationship/promises/p2/miss", json={"reason": "临时加班"})
            self.assertEqual(missed.status_code, 200)
            self.assertEqual(missed.json()["promise"]["status"], "missed")
            self.assertEqual(missed.json()["promise"]["reason"], "临时加班")

    def test_api_auth_and_invalid_payloads_do_not_write(self):
        client = self.make_client()
        with patch("app.services.auth.required", return_value=True):
            self.assertEqual(client.get("/api/relationship").status_code, 401)
            self.assertEqual(client.post("/api/relationship/promises", json={"text": "带伞"}).status_code, 401)
        with patch("app.services.auth.required", return_value=False):
            for payload in ({"text": " "}, {"text": "带伞", "due_at": -1},
                            {"text": "带伞", "due_at": "NaN"}):
                self.assertEqual(client.post("/api/relationship/promises", json=payload).status_code, 422)
        self.assertEqual(self.rel.promise_items(), [])

    def test_invalid_json_is_not_replaced_by_migration(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            Relationship(str(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
