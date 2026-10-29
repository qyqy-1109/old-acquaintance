"""Grounded proactive motives survive retries, restarts and delivery races."""

import datetime as dt
import os
import shutil
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService


class ProactiveMotiveTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path("_tmptest/natural-193-196") / (
            f"motive-test-{os.getpid()}-{id(self)}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key",
            "CHAT_API_KEY": "test-key", "EMBED_API_KEY": "test-key",
            "AMAP_KEY": "", "AMAP_CITY": "", "CHECK_ON_START": False,
            "AUTO_EMOTION": False, "AUTO_EVOLVE_PERSONALITY": False,
            "PROACTIVE": True, "PROACTIVE_QUIET": (0, 0),
            "PROACTIVE_DAILY_MAX": 6, "PROACTIVE_MIN_GAP_H": 0,
            "PROACTIVE_QUIET_MIN": 5, "PROACTIVE_DECISION_GAP_MIN": 30,
            "PROACTIVE_ROLL_EVERY_MIN": 0,
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ("httpx.Client.send", "requests.sessions.Session.request",
                       "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.now = dt.datetime(2026, 10, 15, 15, 0).timestamp()
        self.stack.enter_context(patch("time.time", return_value=self.now))
        self.stack.enter_context(patch("random.random", return_value=0))
        self.service = self.new_service()
        self.stack.enter_context(patch.object(self.service, "_advance_life", return_value=None))
        self.prompts = []
        self.payload = {"speak": True, "text": "你的考试还让我有点惦记", "reminder_ids": []}
        self.on_model = None

        def generate(client, model, messages, **kwargs):
            self.prompts.append(messages[0]["content"])
            if self.on_model is not None:
                self.on_model()
            return dict(self.payload), None

        self.stack.enter_context(patch("app.utils.llmjson.chat_json", side_effect=generate))

    def new_service(self, user_id=""):
        service = ChatService(user_id)
        self.addCleanup(service.close)
        service.speaker._set_ts("user_last_ts", self.now - 7200)
        return service

    def source(self, text, role="user", service=None, stamp=None):
        service = service or self.service
        with service.store._lock:
            history = service.store.sessions.setdefault(service.store.session, [])
            seq = len(history)
            history.append({"role": role, "content": text,
                            "ts": self.now - 1200 if stamp is None else stamp})
            service.store.save()
        return seq

    def topic(self, quote="明天考试，我有点紧张", topic="考试", service=None, **fields):
        service = service or self.service
        seq = self.source(quote, service=service)
        update = dict(actor="user", state="experience", status="open",
                      topic=topic, quote=quote, text=quote, **fields)
        accepted = service.continuity.apply([update], session=service.store.session,
                                            user_seq=seq, user_text=quote, now=self.now - 1200)
        self.assertEqual(len(accepted), 1)
        return accepted[0]

    def revise(self, row, quote, status="resolved"):
        seq = self.source(quote)
        accepted = self.service.continuity.apply([
            dict(id=row["id"], actor=row["actor"], state=row["state"], topic=row["topic"],
                 quote=quote, text=quote, status=status)], session="default", user_seq=seq,
            user_text=quote, now=self.now)
        self.assertEqual(len(accepted), 1)
        return accepted[0]

    def candidates(self, service=None):
        speaker = (service or self.service).speaker
        self.assertTrue(callable(getattr(speaker, "motive_candidates", None)),
                        "主动入口还没有带来源的持久候选")
        return speaker.motive_candidates(now=self.now)

    def candidate(self, kind, text="", service=None):
        rows = [row for row in self.candidates(service) if row["kind"] == kind
                and text in row["text"]]
        self.assertEqual(len(rows), 1, f"缺少唯一的 {kind} 候选：{text}")
        return rows[0]

    def select(self, candidate, text=None):
        self.payload["motive_ids"] = [candidate["id"]]
        if text is not None:
            self.payload["text"] = text

    def enqueue(self, texts, candidates, service=None):
        speaker = (service or self.service).speaker
        self.assertTrue(callable(getattr(speaker, "enqueue_motivated", None)),
                        "候选认领和入队必须是一个原子操作")
        return speaker.enqueue_motivated(texts, candidates=candidates,
                                          now=self.now, quiet_min=5)

    def test_same_slot_new_topic_can_be_considered_without_changing_probability(self):
        self.topic()
        self.service.speaker._set_ts("her_last_ts", self.now - 2000)
        self.payload["motive_ids"] = ["unused-until-candidate-api"]
        self.service.proactive_tick()
        self.assertEqual(len(self.prompts), 1, "新话题不能被整格报备闸门挡住")
        self.assertIn("考试", self.prompts[0])

    def test_candidates_have_stable_sources_for_topic_life_promise_and_her_intention(self):
        self.topic()
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 90, "afternoon", "楼下遇到了邻居，聊了新开的书店", "model"))
        self.service.relationship.add_promise("周末一起去逛书店", due_at=self.now - 60)
        user_seq = self.source("你今天想做什么")
        quote = "我想把那本小说读完"
        assistant_seq = self.source(quote, role="assistant", stamp=self.now - 1200)
        accepted = self.service.continuity.apply([
            dict(actor="assistant", state="intention", topic="小说", text=quote,
                 quote=quote, status="open")], session="default", user_seq=user_seq,
            assistant_seq=assistant_seq, user_text="你今天想做什么", assistant_text=quote,
            now=self.now - 60)
        self.assertEqual(len(accepted), 1)
        rows = self.candidates()
        self.assertEqual({row["kind"] for row in rows}, {"topic", "life", "promise", "choice"})
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in self.candidates()])
        self.assertEqual(self.candidate("choice")["state"], "intention")

    def test_hidden_fallback_future_and_expired_sources_are_not_candidates(self):
        valid = self.topic("当前这件事还值得聊", topic="当前")
        self.topic(expires_at=self.now - 1)
        with self.service.store._lock, self.service.store.db:
            for source, stamp, hidden in (("fallback", self.now - 60, None),
                                           ("model", self.now - 60, self.now),
                                           ("model", self.now + 60, None),
                                           ("model", self.now - 40 * 86400, None)):
                self.service.store.db.execute(
                    "INSERT INTO life_events(ts,slot,text,source,hidden_at) VALUES(?,?,?,?,?)",
                    (stamp, "afternoon", "这不是有效的当前生活来源", source, hidden))
        rows = self.candidates()
        self.assertEqual([row["source_id"] for row in rows], [valid["id"]])

    def test_future_eligible_topic_waits_for_its_time(self):
        valid = self.topic("现在可以聊的事", topic="现在")
        self.topic(eligible_at=self.now + 3600)
        self.assertEqual([row["source_id"] for row in self.candidates()], [valid["id"]])

    def test_same_material_is_consumed_once_across_restart_and_new_material_still_works(self):
        self.topic()
        candidate = self.candidate("topic")
        self.select(candidate)
        self.assertTrue(self.service.proactive_tick())
        self.assertEqual(self.candidates(), [])
        self.service.close()
        reopened = self.new_service()
        self.assertEqual(self.candidates(reopened), [])
        self.topic("牙疼还没好", topic="牙疼", service=reopened)
        self.assertIn("牙疼", self.candidate("topic", service=reopened)["text"])

    def test_selected_id_is_required_and_must_belong_to_offered_candidates(self):
        self.topic()
        candidate = self.candidate("topic")
        for selected in (None, [], ["invented-id"]):
            with self.subTest(selected=selected):
                if selected is None:
                    self.payload.pop("motive_ids", None)
                else:
                    self.payload["motive_ids"] = selected
                self.assertEqual(self.service.proactive_tick(), "")
                self.assertEqual(self.service.speaker.pending_items(), [])
                self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])
        self.payload["motive_ids"] = [candidate["id"], "invented-id"]
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])

    def test_silence_waits_before_requesting_again(self):
        self.topic()
        candidate = self.candidate("topic")
        self.select(candidate)
        self.payload.update(speak=False, text="")
        self.assertEqual(self.service.proactive_tick(), "")
        before = len(self.prompts)
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(len(self.prompts), before)

    def test_silence_generation_failure_and_filter_rejection_do_not_consume(self):
        self.topic()
        candidate = self.candidate("topic")
        self.select(candidate)
        self.payload.update(speak=False, text="")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])
        self.service.speaker._set_ts("declined_at", self.now - 3600)
        with patch("app.utils.llmjson.chat_json", side_effect=RuntimeError("model unavailable")):
            self.assertEqual(self.service.proactive_tick(), "")
        self.payload.update(speak=True, text="团子没扒窗户吧")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])
        self.assertEqual(self.service.speaker.said_today(), 0)

    def test_atomic_enqueue_failure_does_not_consume_or_leave_partial_bubbles(self):
        self.topic()
        candidate = self.candidate("topic")
        with self.service.store.db:
            self.service.store.db.execute(
                "CREATE TRIGGER fail_second_motive_bubble BEFORE INSERT ON pending_say "
                "WHEN NEW.text='second bubble' BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END")
        with self.assertRaises(Exception):
            self.enqueue(["first bubble", "second bubble"], [candidate])
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.service.speaker._ts("her_last_ts"), 0)
        self.assertEqual(self.service.speaker.said_today(), 0)
        self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])
        with self.service.store.db:
            self.service.store.db.execute("DROP TRIGGER fail_second_motive_bubble")
        self.assertEqual(len(self.enqueue(["first bubble", "second bubble"], [candidate])), 2)
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.service.speaker.said_today(), 1)

    def test_closed_or_revised_topic_during_generation_cannot_be_enqueued(self):
        for status in ("resolved", "suppressed", "open"):
            with self.subTest(status=status):
                topic = f"{status}考试"
                row = self.topic(f"{topic}还让我担心", topic=topic)
                candidate = self.candidate("topic", text=topic)
                self.select(candidate, text=f"你那件{topic}的事我还惦记着")
                self.on_model = lambda: self.revise(row, f"{topic}有了新进展", status=status)
                self.assertEqual(self.service.proactive_tick(), "")
                self.assertEqual(self.service.speaker.pending_items(), [])
                self.on_model = None

    def test_delivery_invalidates_closed_topic_permanently_without_claiming_user_saw_it(self):
        row = self.topic()
        candidate = self.candidate("topic")
        ids = self.enqueue(["考试那件事我还惦记着"], [candidate])
        self.assertEqual(len(ids), 1)
        self.revise(row, "考试不想聊了，别再提", status="suppressed")
        self.assertEqual(self.service.take_pending_say(), [])
        invalidations = self.service.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='pending_motive_invalid'").fetchone()[0]
        self.assertEqual(invalidations, 1)
        self.revise(row, "考试我又想聊了", status="open")
        self.assertEqual(self.service.take_pending_say(), [])
        stale = self.service.speaker.pending_items()
        self.assertEqual(len(stale), 1)
        consumed = self.service.store.db.execute(
            "SELECT consumed_at FROM pending_say WHERE id=?", (stale[0]["id"],)).fetchone()[0]
        self.assertIsNone(consumed)

    def test_delivery_revalidates_source_digest_and_hidden_life(self):
        row = self.topic()
        candidate = self.candidate("topic")
        self.enqueue(["考试那件事我还记着"], [candidate])
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute("UPDATE messages SET content='旧来源已被更正' WHERE session=? AND seq=?",
                                          (row["session"], row["seq"]))
        self.assertEqual(self.service.take_pending_say(), [])
        with self.service.store._lock, self.service.store.db:
            cur = self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 60, "afternoon", "刚认识了新邻居", "model"))
        life = self.candidate("life")
        self.enqueue(["今天认识了一个新邻居"], [life])
        self.service.life.delete_event(cur.lastrowid)
        self.assertEqual(self.service.take_pending_say(), [])

    def test_valid_pending_requires_client_ack_and_one_batch_counts_once(self):
        self.topic()
        candidate = self.candidate("topic")
        ids = self.enqueue(["突然想到你", "考试的事不用一个人憋着"], [candidate])
        first = self.service.take_pending_say()
        second = self.service.take_pending_say()
        self.assertEqual([row["id"] for row in first], ids)
        self.assertEqual(first, second)
        self.assertEqual(self.service.speaker.said_today(), 1)
        self.assertEqual(self.service.ack_pending_say(ids), 2)
        self.assertEqual(self.service.take_pending_say(), [])

    def test_two_concurrent_claims_enqueue_one_batch(self):
        self.topic()
        candidate = self.candidate("topic")
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait(timeout=5)
            return self.enqueue(["考试的事我还记着"], [candidate])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for unused in range(2)]
            batches = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(len(batch) for batch in batches), [0, 1])
        self.assertEqual(len(self.service.speaker.pending_items()), 1)
        self.assertEqual(self.service.speaker.said_today(), 1)

    def test_same_material_for_other_user_is_independent(self):
        self.topic()
        first = self.candidate("topic")
        other = self.new_service("other_user")
        self.topic(service=other)
        second = self.candidate("topic", service=other)
        self.assertTrue(self.enqueue(["考试那件事我还记着"], [first]))
        self.assertTrue(self.enqueue(["考试那件事我还记着"], [second], service=other))
        self.assertEqual(len(self.service.speaker.pending_items()), 1)
        self.assertEqual(len(other.speaker.pending_items()), 1)

    def test_candidates_do_not_bypass_quiet_chat_cap_probability_or_decision_wait(self):
        self.topic()
        candidate = self.candidate("topic")
        self.select(candidate)
        with patch.object(config, "PROACTIVE_QUIET", (14, 16)):
            self.assertEqual(self.service.proactive_tick(), "")
        self.service.speaker._set_ts("user_last_ts", self.now - 60)
        self.assertEqual(self.service.proactive_tick(), "")
        self.service.speaker._set_ts("user_last_ts", self.now - 600)
        with patch.object(config, "PROACTIVE_LOW_CHANCE", 0), patch("random.random", return_value=0.9):
            self.assertEqual(self.service.proactive_tick(), "")
        self.service.speaker._set_ts("user_last_ts", self.now - 7200)
        self.service.speaker._set_ts("declined_at", self.now - 60)
        self.assertEqual(self.service.proactive_tick(), "")
        self.service.speaker._set_ts("declined_at", self.now - 7200)
        for order in range(6):
            self.service.store.event("proactive", text=f"previous {order}", n=1)
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])

    def test_same_slot_claimed_a_does_not_block_independent_b(self):
        self.topic()
        first = self.candidate("topic")
        self.assertTrue(self.enqueue(["考试那件事我还记着"], [first]))
        with patch("time.time", return_value=self.now + 600):
            self.topic("牙疼还没有好", topic="牙疼")
            second = self.candidate("topic", "牙疼")
            self.select(second, "你的牙疼还让我有点担心")
            self.assertTrue(self.service.proactive_tick())
        self.assertEqual(len(self.service.speaker.pending_items()), 2)
        self.assertNotIn(first["id"], [row["id"] for row in self.candidates()])
        self.assertEqual(self.service.speaker.said_today(), 2)

    def test_cross_instance_same_user_claims_once_and_count_survives_restart(self):
        self.topic()
        first = self.candidate("topic")
        other = self.new_service()
        second = self.candidate("topic", service=other)
        barrier = threading.Barrier(2)

        def submit(service, candidate):
            barrier.wait(timeout=5)
            return self.enqueue(["考试那件事我还记着"], [candidate], service=service)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, self.service, first), pool.submit(submit, other, second)]
            batches = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(len(batch) for batch in batches), [0, 1])
        self.assertEqual(len(self.service.speaker.pending_items()), 1)
        self.assertEqual(self.service.speaker.said_today(), 1)
        reopened = self.new_service()
        self.assertEqual(reopened.speaker.said_today(), 1)
        self.assertEqual(self.candidates(reopened), [])

    def test_all_selected_sources_must_still_be_valid_before_enqueue(self):
        first_row = self.topic()
        first = self.candidate("topic")
        self.topic("牙疼还没好", topic="牙疼")
        second = self.candidate("topic", "牙疼")
        self.revise(first_row, "考试不想聊了，别再提", status="suppressed")
        self.assertEqual(self.enqueue(["考试和牙疼都惦记着"], [first, second]), [])
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertIn(second["id"], [row["id"] for row in self.candidates()])

    def test_pending_batch_checks_every_selected_source(self):
        first_row = self.topic()
        first = self.candidate("topic")
        second_row = self.topic("牙疼还没好", topic="牙疼")
        second = self.candidate("topic", "牙疼")
        self.assertTrue(self.enqueue(["考试和牙疼都让我牵挂"], [first, second]))
        self.revise(second_row, "牙疼已经好了", status="resolved")
        self.assertEqual(self.service.take_pending_say(), [])
        self.assertEqual(self.service.continuity.get(first_row["id"])["status"], "open")

    def test_invalidated_delivery_survives_restart_without_client_ack(self):
        row = self.topic()
        candidate = self.candidate("topic")
        pending = self.enqueue(["考试还让我惦记"], [candidate])[0]
        self.revise(row, "考试不想聊了，别再提", status="suppressed")
        self.assertEqual(self.service.take_pending_say(), [])
        reopened = self.new_service()
        self.assertEqual(reopened.take_pending_say(), [])
        self.assertIsNone(reopened.store.db.execute(
            "SELECT consumed_at FROM pending_say WHERE id=?", (pending,)).fetchone()[0])
        self.assertEqual(reopened.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='pending_motive_invalid'").fetchone()[0], 1)

    def test_life_and_promise_generation_race_leaves_material_unclaimed(self):
        with self.service.store._lock, self.service.store.db:
            cur = self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 60, "afternoon", "新邻居跟我聊了书店", "model"))
        life = self.candidate("life")
        self.select(life, "今天碰上新邻居了")
        self.on_model = lambda: self.service.life.delete_event(cur.lastrowid)
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.service.relationship.add_promise("一起去书店", due_at=self.now - 60)
        promise = self.service.relationship.promise_items()[0]
        candidate = self.candidate("promise")
        self.select(candidate, "还记得一起去书店")
        self.payload["reminder_ids"] = [promise["id"]]
        self.on_model = lambda: self.service.relationship.complete_promise(promise["id"])
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.service.store.db.execute(
            "SELECT count(*) FROM proactive_motive_claims").fetchone()[0], 0)

    def test_selected_promise_is_marked_only_after_successful_enqueue(self):
        self.service.relationship.add_promise("一起去书店", due_at=self.now - 60)
        promise = self.service.relationship.promise_items()[0]
        candidate = self.candidate("promise")
        self.select(candidate, "还记得一起去书店")
        self.payload["reminder_ids"] = [promise["id"]]
        with self.service.store.db:
            self.service.store.db.execute("CREATE TRIGGER fail_selected_promise BEFORE INSERT ON pending_say "
                                          "BEGIN SELECT RAISE(ABORT,'test fail'); END")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertFalse(self.service.relationship.promise_items()[0].get("reminded_at"))
        self.assertIn(candidate["id"], [row["id"] for row in self.candidates()])
        with self.service.store.db:
            self.service.store.db.execute("DROP TRIGGER fail_selected_promise")
        self.assertTrue(self.service.proactive_tick())
        self.assertEqual(self.service.relationship.promise_items()[0].get("reminded_at"), self.now)
        pending = self.service.take_pending_say()
        self.assertEqual(len(pending), 1)
        self.assertIn("一起去书店", pending[0]["text"])
        with self.service.store.db:
            self.service.store.db.execute("DELETE FROM events")
        self.assertEqual(self.service.speaker.said_today(), 1)

    def test_filtered_promise_bubble_does_not_claim_its_material(self):
        self.topic()
        topic = self.candidate("topic")
        self.service.relationship.add_promise("团子没扒窗户吧", due_at=self.now - 60)
        promise = self.candidate("promise")
        self.payload.update(speak=True, motive_ids=[topic["id"], promise["id"]],
                            reminder_ids=[promise["source_id"]],
                            text="团子没扒窗户吧\n@@@\n考试那件事我还惦记着")
        self.assertEqual(self.service.proactive_tick(), "考试那件事我还惦记着")
        self.assertIn(promise["id"], [row["id"] for row in self.candidates()])
        self.assertFalse(self.service.relationship.promise_items()[0].get("reminded_at"))

    def test_source_write_transaction_starts_before_validation(self):
        self.topic()
        candidate = self.candidate("topic")
        trace = []
        self.service.store.db.set_trace_callback(trace.append)
        real = self.service.speaker._motive_current

        def validate(item, now, **kwargs):
            self.assertTrue(any(statement.upper().startswith("BEGIN IMMEDIATE") for statement in trace),
                            "跨连接写锁必须早于来源验证")
            return real(item, now, **kwargs)

        with patch.object(self.service.speaker, "_motive_current", side_effect=validate):
            self.assertTrue(self.enqueue(["考试还让我惦记着"], [candidate]))

    def test_other_instance_completed_promise_invalidates_cached_source(self):
        self.service.relationship.add_promise("一起去书店", due_at=self.now - 60)
        other = self.new_service()
        candidate = self.candidate("promise", service=other)
        self.service.relationship.complete_promise("p1")
        self.assertEqual(self.candidates(other), [])
        self.assertEqual(self.enqueue(["还记得一起去书店"], [candidate], service=other), [])
        self.assertEqual(other.speaker.pending_items(), [])
        self.assertEqual(other.speaker.said_today(), 0)

    def test_filtered_topic_bubble_does_not_claim_its_material(self):
        self.topic()
        first = self.candidate("topic")
        self.topic("牙疼还没好", topic="牙疼")
        second = self.candidate("topic", "牙疼")
        self.service.speaker.enqueue("考试那件事我还惦记着")
        self.service.speaker._set_ts("her_last_ts", self.now - 7200)
        self.payload.update(speak=True, motive_ids=[first["id"], second["id"]],
                            text="考试那件事我还惦记着\n@@@\n你的牙疼还让我有点担心")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.store.db.execute(
            "SELECT count(*) FROM proactive_motive_claims").fetchone()[0], 0)

    def test_concurrent_different_material_cannot_exceed_remaining_daily_cap(self):
        self.topic()
        first = self.candidate("topic")
        self.topic("牙疼还没好", topic="牙疼")
        second = self.candidate("topic", "牙疼")
        other = self.new_service()
        barrier = threading.Barrier(2)

        def submit(service, candidate):
            barrier.wait(timeout=5)
            return service.speaker.enqueue_motivated([candidate["text"]], [candidate],
                                                      now=self.now, quiet_min=0)

        with patch.object(config, "PROACTIVE_DAILY_MAX", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit, self.service, first), pool.submit(submit, other, second)]
                batches = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(len(batch) for batch in batches), [0, 1])
        self.assertEqual(self.service.speaker.said_today(), 1)

    def test_filtered_topic_with_surviving_promise_does_not_claim_topic(self):
        self.topic()
        topic = self.candidate("topic")
        self.service.relationship.add_promise("一起去书店", due_at=self.now - 60)
        promise = self.candidate("promise")
        self.service.speaker.enqueue("考试那件事我还惦记着")
        self.service.speaker._set_ts("her_last_ts", self.now - 7200)
        self.payload.update(speak=True, motive_ids=[topic["id"], promise["id"]],
                            reminder_ids=[promise["source_id"]],
                            text="考试那件事我还惦记着\n@@@\n还记得一起去书店")
        self.assertEqual(self.service.proactive_tick(), "")
        self.assertEqual(self.service.store.db.execute(
            "SELECT count(*) FROM proactive_motive_claims").fetchone()[0], 0)
        self.assertIn(topic["id"], [row["id"] for row in self.candidates()])


if __name__ == "__main__":
    unittest.main()
