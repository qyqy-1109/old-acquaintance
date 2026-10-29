"""有来源的话题跨轮延续，关闭与过期不冒充完成事实。"""

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService


class ConversationContinuityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path("_tmptest/natural-193-196") / (
            f"continuity-test-{os.getpid()}-{id(self)}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key", "CHAT_API_KEY": "test-key",
            "EMBED_API_KEY": "test-key", "AMAP_KEY": "", "AMAP_CITY": "",
            "AUTO_EMOTION": False, "AUTO_EVOLVE_PERSONALITY": False,
            "CHECK_ON_START": False, "PROFILE_EVERY": 1000,
            "MEMORY_CFG": dict(config.MEMORY_CFG, summarize_every=0,
                               auto_extract=True, extract_every=1),
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ("httpx.Client.send", "requests.sessions.Session.request", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.now = dt.datetime(2026, 10, 15, 15, 0).timestamp()
        self.stack.enter_context(patch("time.time", return_value=self.now))
        self.service = ChatService()
        self.addCleanup(self.service.close)

    def book(self, service=None):
        service = service or self.service
        self.assertTrue(hasattr(service, "continuity"), "ChatService 尚未接入持久话题索引")
        self.assertIs(service.continuity, service.store.continuity)
        return service.continuity

    def source(self, content, role="user", service=None):
        service = service or self.service
        with service.store._lock:
            history = service.store.sessions.setdefault(service.store.session, [])
            seq = len(history)
            history.append({"role": role, "content": content, "ts": self.now})
            service.store.save()
        return seq

    def update(self, quote="明天面试，我有点紧张", topic="面试", status="open", **fields):
        return dict(actor="user", state="experience", topic=topic, text=quote,
                    quote=quote, status=status, **fields)

    def apply(self, update, content=None, role="user", service=None, now=None):
        service = service or self.service
        content = content if content is not None else update.get("quote", "")
        seq = self.source(content, role=role, service=service)
        return self.book(service).apply([update], session=service.store.session,
                                       user_seq=seq, user_text=content,
                                       now=now if now is not None else self.now)

    def test_open_topic_is_persisted_with_source_and_survives_restart(self):
        accepted = self.apply(self.update())
        self.assertEqual(len(accepted), 1)
        row = accepted[0]
        self.assertEqual((row["topic"], row["status"], row["actor"]), ("面试", "open", "user"))
        self.assertEqual((row["session"], row["seq"], row["quote"]), ("default", 0, "明天面试，我有点紧张"))
        self.assertTrue(row["source_digest"])
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        self.assertEqual(self.book(reopened).get(row["id"])["status"], "open")
        self.assertIn("面试", self.book(reopened).render("面试怎么样了", now=self.now))

    def test_explicit_resolution_survives_restart_and_overrides_old_open_context(self):
        row = self.apply(self.update())[0]
        self.apply(self.update("面试结束了，已经拿到录用通知", status="resolved", id=row["id"]))
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        book = self.book(reopened)
        self.assertEqual(book.snapshot(now=self.now), [])
        self.assertEqual(book.get(row["id"])["status"], "resolved")
        rendered = book.render("面试的事", now=self.now)
        self.assertIn("已解决", rendered)
        self.assertIn("录用通知", rendered)
        self.assertNotIn("待关心", rendered)
        self.assertNotIn("录用通知", book.render("今天听什么歌", now=self.now))

    def test_refusal_survives_restart_and_stays_out_of_candidates(self):
        row = self.apply(self.update())[0]
        self.apply(self.update("别再问面试了，我不想聊", status="suppressed", id=row["id"]))
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        self.assertEqual(self.book(reopened).snapshot(now=self.now), [])
        self.assertEqual(self.book(reopened).get(row["id"])["status"], "suppressed")
        self.assertIn("不再提", self.book(reopened).render("面试", now=self.now))

    def test_missing_fabricated_other_actor_and_unknown_id_evidence_are_rejected(self):
        book = self.book()
        seq = self.source("明天面试，我有点紧张")
        updates = [
            self.update(quote=""), self.update("面试很顺利"),
            self.update(actor_override="assistant"),
            self.update(id="unknown-topic", status="resolved"),
        ]
        updates[2]["actor"] = updates[2].pop("actor_override")
        self.assertEqual(book.apply(updates, session="default", user_seq=seq,
                                    user_text="明天面试，我有点紧张", now=self.now), [])
        self.assertEqual(book.items(), [])
        self.assertEqual(book.apply([self.update()], session="missing", user_seq=0,
                                    user_text="明天面试，我有点紧张", now=self.now), [])
        self.assertEqual(self.apply(self.update(), role="assistant"), [])

    def test_unrelated_and_ambiguous_closure_cannot_close_known_topic(self):
        row = self.apply(self.update())[0]
        for quote, topic in (("牙疼已经好了", "牙疼"), ("好了", "面试"),
                             ("别问了", "面试")):
            with self.subTest(quote=quote):
                accepted = self.apply(self.update(quote, topic=topic, status="resolved", id=row["id"]))
                self.assertEqual(accepted, [])
        self.assertEqual(self.book().get(row["id"])["status"], "open")

    def test_identical_source_replay_does_not_create_or_advance_topic(self):
        book = self.book()
        seq = self.source("明天面试，我有点紧张")
        first = book.apply([self.update()], session="default", user_seq=seq,
                           user_text="明天面试，我有点紧张", now=self.now)[0]
        replay = book.apply([self.update()], session="default", user_seq=seq,
                            user_text="明天面试，我有点紧张", now=self.now + 60)
        self.assertEqual(replay, [])
        self.assertEqual(len(book.items()), 1)
        self.assertEqual(book.get(first["id"])["revision"], first["revision"])
        self.assertEqual(book.get(first["id"])["updated_at"], self.now)

    def test_old_open_source_replay_cannot_reopen_resolved_or_suppressed_topic(self):
        for status, ending in (("resolved", "面试结束了，拿到录用通知"),
                               ("suppressed", "面试不想聊了，别再提")):
            with self.subTest(status=status):
                book = self.book()
                old_quote = f"这次{status}面试还没结束"
                topic = f"{status}面试"
                seq = self.source(old_quote)
                row = book.apply([self.update(old_quote, topic=topic)],
                                 session="default", user_seq=seq,
                                 user_text=old_quote, now=self.now)[0]
                close_quote = topic + ending
                self.apply(self.update(close_quote, topic=topic, status=status, id=row["id"]))
                closed = book.get(row["id"])
                replay = book.apply([self.update(old_quote, topic=topic, id=row["id"])],
                                    session="default", user_seq=seq, user_text=old_quote,
                                    now=self.now + 120)
                self.assertEqual(replay, [])
                self.assertEqual(book.get(row["id"])["status"], status)
                self.assertEqual(book.get(row["id"])["revision"], closed["revision"])

    def test_older_message_time_cannot_override_newer_topic_from_another_session(self):
        book = self.book()
        old_seq = self.source("面试还在准备")
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "UPDATE messages SET ts=? WHERE session=? AND seq=?",
                (self.now - 120, "default", old_seq))
        self.service.store.session = "later-session"
        row = self.apply(self.update("面试结束了，已经拿到结果"))[0]
        self.apply(self.update("面试已经解决", status="resolved", id=row["id"]))
        accepted = book.apply([self.update("面试还在准备", id=row["id"])],
                              session="default", user_seq=old_seq,
                              user_text="面试还在准备", now=self.now + 600)
        self.assertEqual(accepted, [])
        self.assertEqual(book.get(row["id"])["status"], "resolved")

    def test_expired_topic_is_historical_without_becoming_resolved(self):
        row = self.apply(self.update())[0]
        self.assertEqual(self.book().snapshot(now=self.now + 7 * 86400 + 1), [])
        history = self.book().get(row["id"])
        self.assertEqual(history["status"], "open")
        self.assertEqual(history["expires_at"], self.now + 7 * 86400)
        self.assertEqual(len(self.book().items()), 1)

    def test_reused_message_sequence_invalidates_original_source_without_deleting_history(self):
        row = self.apply(self.update())[0]
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "UPDATE messages SET content=? WHERE session=? AND seq=?",
                ("换了新的话题", row["session"], row["seq"]))
        self.assertEqual(self.book().snapshot(now=self.now), [])
        self.assertFalse(self.book().get(row["id"])["source_valid"])
        self.assertEqual(len(self.book().items()), 1)

    def test_source_expected_text_must_match_persisted_message(self):
        book = self.book()
        seq = self.source("同样说面试，但这是另一条消息")
        self.assertEqual(book.apply([self.update("面试")], session="default", user_seq=seq,
                                    user_text="明天面试，我有点紧张", now=self.now), [])
        self.assertEqual(book.items(), [])

    def test_users_have_independent_topics_and_cannot_update_each_others_ids(self):
        other = ChatService("continuity-other")
        self.addCleanup(other.close)
        row = self.apply(self.update())[0]
        self.assertEqual(self.book(other).items(), [])
        accepted = self.apply(self.update("面试结束了", status="resolved", id=row["id"]), service=other)
        self.assertEqual(accepted, [])
        self.assertEqual(self.book().get(row["id"])["status"], "open")

    def test_user_suggestion_cannot_create_an_assistant_decision(self):
        update = self.update("你明天去书店吧", topic="书店")
        update.update(actor="assistant", state="decided")
        self.assertEqual(self.apply(update), [])
        self.assertEqual(self.book().items(), [])

    def test_relevant_fresh_context_is_bounded_and_closed_history_is_not_a_motive(self):
        for index, topic in enumerate(("面试", "牙疼", "搬家", "毕业设计", "签证")):
            self.apply(self.update(f"我的{topic}还没结束", topic=topic), now=self.now + index)
        rows = self.book().snapshot("牙疼怎么样", now=self.now + 5)
        self.assertLessEqual(len(rows), 3)
        self.assertEqual(rows[0]["topic"], "牙疼")
        self.assertNotIn("面试", [row["topic"] for row in rows])
        self.assertEqual(len(self.book().items()), 5)

    def test_finish_turn_uses_existing_extraction_once_and_prepare_receives_persisted_topic(self):
        output = {"facts": [], "world": [], "constraints": [], "drop": [], "self": [],
                  "slots": [], "continuity": [self.update()]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")) as extraction:
            self.service.finish_turn("明天面试，我有点紧张", "听着呢，你最担心哪一段")
        self.assertEqual(extraction.call_count, 1)
        self.assertEqual(self.service.store._llm_calls, 1)
        row = self.book().items()[0]
        self.assertEqual(row["seq"], 0)
        with patch.object(self.service, "_advance_life"), patch.object(self.service.store, "_query_vec", return_value=None):
            messages, _ = self.service.prepare("面试")
        self.assertIn("明天面试，我有点紧张", "\n".join(message["content"] for message in messages))
        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", return_value=(dict(output, continuity=[]), "")) as next_extract:
            result = self.service.store._extract("面试有进展", "说来听听")
        self.assertEqual(len(result), 6)
        self.assertIn(row["id"], next_extract.call_args.args[2][0]["content"])

    def test_failed_or_old_extraction_clears_pending_updates_and_preserves_six_tuple(self):
        self.book()
        with patch.object(self.service.store, "recall", return_value=[]):
            with patch("app.models.memory_write.chat_json", return_value=({"continuity": [self.update()]}, "")):
                self.service.store._extract("明天面试，我有点紧张", "听着呢")
            self.assertTrue(self.service.store._continuity_updates)
            with patch("app.models.memory_write.chat_json", return_value=(None, "离线失败")):
                result = self.service.store._extract("今天随便聊聊", "好呀")
            self.assertEqual(result, (None, [], [], [], [], []))
            self.assertEqual(self.service.store._continuity_updates, [])
            with patch("app.models.memory_write.chat_json", return_value=({}, "")):
                self.assertEqual(len(self.service.store._extract("今天随便聊聊", "好呀")), 6)
            self.assertEqual(self.service.store._continuity_updates, [])

    def test_quote_outside_actual_extraction_window_is_rejected(self):
        quote = "最后才说面试还没结束"
        user = "先聊别的。" * 240 + quote
        output = {"continuity": [self.update(quote)]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")):
            self.service.finish_turn(user, "听着呢")
        self.assertEqual(self.book().items(), [])

    def test_failed_message_save_cannot_leave_a_topic_with_missing_source(self):
        with patch("app.models.memory_write.chat_json", return_value=({"continuity": [self.update()]}, "")), \
                patch.object(self.service.store, "save"):
            self.service.finish_turn("明天面试，我有点紧张", "听着呢")
        self.assertEqual(self.book().items(), [])

    def test_finish_turn_concurrency_keeps_each_extraction_with_its_own_source(self):
        second_finished = threading.Event()
        first_extracting = threading.Event()
        first_quote = "并发甲的面试让我紧张"
        second_quote = "并发乙的搬家让我紧张"

        def extract(client, model, messages, **kwargs):
            if first_quote in messages[0]["content"]:
                first_extracting.set()
                self.assertTrue(second_finished.wait(5))
                return {"continuity": [self.update(first_quote)]}, ""
            return {"continuity": [self.update(second_quote, topic="搬家")]}, ""

        def second_turn():
            self.assertTrue(first_extracting.wait(5))
            try:
                return self.service.finish_turn(second_quote, "听着呢")
            finally:
                second_finished.set()

        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", side_effect=extract):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self.service.finish_turn, first_quote, "听着呢")
                second = executor.submit(second_turn)
                first.result(timeout=10)
                second.result(timeout=10)
        rows = self.book().items()
        self.assertEqual({row["quote"] for row in rows}, {first_quote, second_quote})
        for row in rows:
            message = self.service.store.db.execute(
                "SELECT content FROM messages WHERE session=? AND seq=?",
                (row["session"], row["seq"])).fetchone()
            self.assertEqual(message[0], row["quote"])

    def test_empty_query_and_life_context_exclude_closed_topics(self):
        resolved = self.apply(self.update("面试还未结束"))[0]
        self.apply(self.update("面试已经结束", status="resolved", id=resolved["id"]))
        suppressed = self.apply(self.update("搬家还未结束", topic="搬家"))[0]
        self.apply(self.update("搬家不想再聊", topic="搬家", status="suppressed", id=suppressed["id"]))
        self.assertEqual(self.book().render("", now=self.now), "")
        self.assertEqual(self.service.life.context_source(""), "")
        output = {"activity": "在图书馆看书", "place": "图书馆", "event": "在图书馆翻书"}
        with patch("app.utils.llmjson.chat_json", return_value=(output, "")) as generation:
            self.service.life.advance(client=self.service.client, model="offline-test",
                                      now=dt.datetime.fromtimestamp(self.now), force=True)
        prompt = generation.call_args.args[2][0]["content"]
        self.assertNotIn("面试已经结束", prompt)
        self.assertNotIn("搬家不想再聊", prompt)

    def test_old_relevant_topic_can_be_closed_among_many_newer_unrelated_topics(self):
        row = self.apply(self.update("面试还没有结果"))[0]
        for index in range(45):
            topic = f"无关事项{index}"
            self.apply(self.update(f"{topic}还在准备", topic=topic), now=self.now + index + 1)
        received = []

        def extract(client, model, messages, **kwargs):
            block = messages[0]["content"].split("【已知未完话题（只能使用这些 id 更新）】\n", 1)[1]
            try:
                known = json.loads(block)
            except json.JSONDecodeError:
                known = []
            received.extend(known)
            return {"continuity": [self.update("面试有了结果", status="resolved", id=row["id"])]
                    if any(item.get("id") == row["id"] for item in known) else []}, ""

        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", side_effect=extract):
            self.service.finish_turn("面试有了结果", "听你说说")
        self.assertEqual(self.book().get(row["id"])["status"], "resolved")
        self.assertLessEqual(len(received), self.book().context_limit)
        self.assertEqual(received[0]["id"], row["id"])
        self.assertEqual(received[0]["quote"], "面试还没有结果")

    def test_topic_context_read_failure_is_visible_and_extraction_still_works(self):
        self.book()
        with patch.object(self.service.store, "recall", return_value=[]), \
                patch.object(self.book(), "items", side_effect=RuntimeError("source-read-failed")), \
                patch("app.models.memory_write.chat_json", return_value=({}, "")):
            with self.assertLogs("app.memory", level="WARNING") as logged:
                result = self.service.store._extract("面试", "听着呢")
        self.assertEqual(len(result), 6)
        self.assertTrue(any("source-read-failed" in item for item in logged.output))
        events = self.service.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='continuity_context_failed'").fetchone()[0]
        self.assertEqual(events, 1)

    def test_same_text_and_sequence_with_new_message_time_cannot_revive_old_source(self):
        row = self.apply(self.update())[0]
        self.service.store.clear_session()
        self.service.store.save()
        self.source("明天面试，我有点紧张")
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "UPDATE messages SET ts=? WHERE session='default' AND seq=0", (self.now + 600,))
        self.assertFalse(self.book().get(row["id"])["source_valid"])
        self.assertEqual(self.book().snapshot(now=self.now + 600), [])

    def test_newer_evidence_rebuilds_topic_after_clear_without_reviving_old_revision(self):
        original = self.apply(self.update())[0]
        self.service.store.clear_session()
        self.service.store.save()
        self.assertFalse(self.book().get(original["id"])["source_valid"])
        with patch("time.time", return_value=self.now + 600):
            output = {"continuity": [self.update("下周面试，我还是有些紧张")]}
            with patch.object(self.service.store, "recall", return_value=[]), \
                    patch("app.models.memory_write.chat_json", return_value=(output, "")):
                self.service.finish_turn("下周面试，我还是有些紧张", "听着呢")
        active = self.book().snapshot(now=self.now + 600)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["quote"], "下周面试，我还是有些紧张")
        self.assertGreater(active[0]["revision"], original["revision"])
        self.assertNotEqual(active[0]["source_digest"], original["source_digest"])
        replay = self.book().apply(
            [self.update(id=original["id"])], session=original["session"], user_seq=original["seq"],
            user_text=original["quote"], now=self.now + 700)
        self.assertEqual(replay, [])
        self.assertEqual(self.book().get(active[0]["id"])["revision"], active[0]["revision"])

    def test_inflight_old_source_cannot_attach_to_same_text_after_sequence_reuse(self):
        def extract(client, model, messages, **kwargs):
            self.service.store.clear_session()
            self.service.store.save()
            seq = self.source("明天面试，我有点紧张")
            with self.service.store._lock, self.service.store.db:
                self.service.store.db.execute(
                    "UPDATE messages SET ts=? WHERE session='default' AND seq=?", (self.now + 600, seq))
            return {"continuity": [self.update()]}, ""

        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", side_effect=extract):
            self.service.finish_turn("明天面试，我有点紧张", "听着呢")
        self.assertEqual(self.book().items(), [])

    def test_text_without_explicit_quote_is_not_source_evidence(self):
        update = self.update()
        update.pop("quote")
        self.assertEqual(self.apply(update, content="明天面试，我有点紧张"), [])
        self.assertEqual(self.book().items(), [])

    def test_life_generation_receives_current_topics_without_an_extra_model_request(self):
        self.apply(self.update())
        output = {"activity": "在图书馆看书", "place": "图书馆", "event": "在图书馆翻书"}
        with patch("app.utils.llmjson.chat_json", return_value=(output, "")) as generation:
            self.service.life.advance(client=self.service.client, model="offline-test",
                                      now=dt.datetime.fromtimestamp(self.now), force=True)
        self.assertEqual(generation.call_count, 1)
        self.assertIn("明天面试，我有点紧张", generation.call_args.args[2][0]["content"])

    def test_shared_sqlite_lock_supports_concurrent_projection_reads_and_writes(self):
        book = self.book()

        def perform(index):
            quote = f"第{index}次面试还没结束"
            accepted = self.apply(self.update(quote, topic=f"第{index}次面试"))
            book.snapshot("面试", now=self.now)
            return len(accepted)

        with ThreadPoolExecutor(max_workers=4) as executor:
            counts = list(executor.map(perform, range(12)))
        self.assertEqual(sum(counts), 12)
        self.assertEqual(len(book.items()), 12)


if __name__ == "__main__":
    unittest.main()
