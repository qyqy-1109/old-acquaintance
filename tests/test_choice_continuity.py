"""她的短期选择须由她的消息支持，并保留改变主意的证据。"""

import datetime as dt
import json
import shutil
import sys
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService
from app.utils import agency


class ChoiceContinuityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path("_tmptest/natural-193-196") / f"choice-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
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

    def turn(self, user, answer, *, service=None, timestamp=None):
        service = service or self.service
        source_ts = self.now if timestamp is None else timestamp
        with service.store._lock:
            history = service.store.sessions.setdefault(service.store.session, [])
            user_seq = len(history)
            history.extend([
                {"role": "user", "content": user, "ts": source_ts},
                {"role": "assistant", "content": answer, "ts": source_ts},
            ])
            service.store.save()
        return dict(session=service.store.session, user_seq=user_seq,
                    assistant_seq=user_seq + 1, user_text=user, assistant_text=answer)

    def update(self, quote="我想去书店翻翻书", *, topic="书店", state="intention",
               actor="assistant", status="open", topic_id=None):
        result = dict(topic=topic, quote=quote, text=quote, actor=actor,
                      state=state, status=status)
        if topic_id is not None:
            result["id"] = topic_id
        return result

    def apply(self, update=None, *, user="周末你想去哪儿", answer=None,
              timestamp=None, service=None):
        service = service or self.service
        update = update or self.update()
        answer = update["quote"] if answer is None else answer
        source = self.turn(user, answer, service=service, timestamp=timestamp)
        return service.continuity.apply([update], now=self.now, **source)

    def first_choice(self):
        accepted = self.apply()
        self.assertEqual(len(accepted), 1, "她自己的原话应能形成有来源的意向")
        return accepted[0]

    def test_assistant_intention_uses_assistant_source_and_survives_restart(self):
        row = self.first_choice()
        self.assertEqual((row["actor"], row["state"], row["quote"]),
                         ("assistant", "intention", "我想去书店翻翻书"))
        self.assertEqual(row["seq"], 1)
        self.assertTrue(row["source_valid"])
        self.assertTrue(row["source_digest"])
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        restored = reopened.continuity.get(row["id"])
        self.assertEqual((restored["state"], restored["quote"]),
                         ("intention", "我想去书店翻翻书"))
        rendered = reopened.continuity.render("书店", now=self.now)
        self.assertIn("我想去书店翻翻书", rendered)
        self.assertIn("意向", rendered)
        self.assertIn("她", rendered)

    def test_user_suggestion_is_not_evidence_for_her_decision(self):
        source = self.turn("你就决定去书店吧", "我还没想好呢")
        result = self.service.continuity.apply(
            [self.update("你就决定去书店吧", state="decision")], now=self.now, **source)
        self.assertEqual(result, [])
        self.assertEqual(self.service.continuity.items(), [])

    def test_assistant_quote_requires_its_actual_message_and_extraction_window(self):
        source = self.turn("周末你想去哪儿", "我想去书店翻翻书")
        for changed in (
            dict(assistant_seq=None), dict(assistant_seq=source["user_seq"]),
            dict(assistant_text="我想去公园"),
        ):
            with self.subTest(changed=changed):
                result = self.service.continuity.apply(
                    [self.update()], now=self.now, **dict(source, **changed))
                self.assertEqual(result, [])
        long_answer = "先聊别的。" * 300 + "我想去书店翻翻书"
        source = self.turn("周末你想去哪儿", long_answer)
        source["assistant_text"] = long_answer[:1500]
        self.assertEqual(self.service.continuity.apply(
            [self.update()], now=self.now, **source), [])

    def test_assistant_source_from_another_turn_cannot_be_attached(self):
        first = self.turn("周末你想去哪儿", "我想去书店翻翻书")
        second = self.turn("晚饭想吃什么", "我想吃面条")
        forged = dict(second, assistant_seq=first["assistant_seq"],
                      assistant_text=first["assistant_text"])
        self.assertEqual(self.service.continuity.apply(
            [self.update()], now=self.now, **forged), [])

    def test_intention_decision_and_experience_stay_distinct(self):
        row = self.first_choice()
        chosen = self.apply(self.update("我决定周六去书店", state="decision", topic_id=row["id"]))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["state"], "decision")
        self.assertEqual(chosen[0]["status"], "open")
        completed = self.apply(self.update(
            "我刚从书店回来", state="experience", status="resolved", topic_id=row["id"]))
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["state"], "experience")
        self.assertEqual(self.service.continuity.snapshot("书店", now=self.now), [])

    def test_changed_mind_reuses_known_id_and_keeps_revision_evidence(self):
        row = self.first_choice()
        quote = "今天又犯懒了，还是留在家里看书吧"
        changed = self.apply(self.update(quote, state="decision", topic_id=row["id"]))
        self.assertEqual(len(changed), 1, "同一已知选择可由自然表达改变，原主题不必逐字复现")
        self.assertEqual(changed[0]["id"], row["id"])
        self.assertEqual(changed[0]["quote"], quote)
        self.assertEqual(changed[0]["revision"], 2)
        history = getattr(self.service.continuity, "history", None)
        self.assertTrue(callable(history), "改变主意后仍需能读取旧版本的来源")
        evidence = history(row["id"])
        self.assertEqual([(entry["revision"], entry["state"], entry["quote"])
                          for entry in evidence],
                         [(1, "intention", "我想去书店翻翻书"), (2, "decision", quote)])
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.continuity.history(row["id"])), 2)

    def test_same_topic_user_and_assistant_cannot_overwrite_each_others_choice(self):
        user_row = self.apply(self.update("我想去书店", actor="user"),
                              user="我想去书店", answer="我也想去书店")
        her_row = self.apply(self.update("我也想去书店"),
                             user="你想去哪儿", answer="我也想去书店")
        self.assertEqual((len(user_row), len(her_row)), (1, 1))
        self.assertNotEqual(user_row[0]["id"], her_row[0]["id"])
        forged = self.apply(self.update("我决定去书店", topic_id=user_row[0]["id"],
                                        state="decision"))
        self.assertEqual(forged, [])
        self.assertEqual(self.service.continuity.get(user_row[0]["id"])["actor"], "user")

    def test_replay_and_older_source_cannot_restore_superseded_intention(self):
        source = self.turn("周末你想去哪儿", "我想去书店翻翻书", timestamp=self.now - 100)
        initial = self.service.continuity.apply([self.update()], now=self.now, **source)
        self.assertEqual(len(initial), 1)
        row = initial[0]
        self.apply(self.update("这次书店不去了，我决定在家看书", state="decision",
                               topic_id=row["id"]))
        replay = self.service.continuity.apply(
            [self.update(topic_id=row["id"])], now=self.now + 50, **source)
        self.assertEqual(replay, [])
        retained = self.service.continuity.get(row["id"])
        self.assertEqual((retained["state"], retained["revision"]), ("decision", 2))
        history = getattr(self.service.continuity, "history", None)
        self.assertTrue(callable(history), "旧来源重放不能增加审计版本")
        self.assertEqual(len(history(row["id"])), 2)

    def test_mutated_assistant_source_is_excluded_and_remains_auditable(self):
        row = self.first_choice()
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "UPDATE messages SET content=? WHERE session=? AND seq=?",
                ("我想在家睡觉", row["session"], row["seq"]))
        self.assertFalse(self.service.continuity.get(row["id"])["source_valid"])
        self.assertEqual(self.service.continuity.snapshot("书店", now=self.now), [])
        history = getattr(self.service.continuity, "history", None)
        self.assertTrue(callable(history), "失效证据仍须可审计")
        self.assertEqual(history(row["id"])[0]["quote"], "我想去书店翻翻书")
        self.assertFalse(history(row["id"])[0]["source_valid"])

    def test_missing_or_unknown_assistant_state_is_not_recorded_as_experience(self):
        source = self.turn("周末你想去哪儿", "我想去书店翻翻书")
        missing = self.update()
        missing.pop("state")
        for update in (missing, self.update(state="decided"), self.update(state="unknown")):
            with self.subTest(update=update):
                self.assertEqual(self.service.continuity.apply(
                    [update], now=self.now, **source), [])

    def test_legacy_unknown_state_stays_readable_without_rewriting_evidence(self):
        rows = self.apply(self.update("明天面试，我有点紧张", topic="面试",
                                     actor="user", state="experience"),
                          user="明天面试，我有点紧张", answer="听着呢")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "UPDATE continuity_topics SET state='pending' WHERE id=?", (row["id"],))
            self.service.store.db.execute(
                "DELETE FROM continuity_revisions WHERE topic_id=?", (row["id"],))
        self.service.close()
        reopened = ChatService()
        self.addCleanup(reopened.close)
        try:
            rendered = reopened.continuity.render("面试", now=self.now)
        except KeyError as error:
            self.fail(f"旧状态不应使有效话题整块丢失：{error}")
        self.assertIn("明天面试，我有点紧张", rendered)
        self.assertEqual(reopened.continuity.get(row["id"])["state"], "pending")
        self.assertEqual(reopened.continuity.history(row["id"])[0]["state"], "pending")

    def test_short_term_choice_does_not_rewrite_personality_or_relationship_files(self):
        original_data = json.loads(json.dumps(self.service.personality.data, ensure_ascii=False))
        paths = [self.directory / name for name in ("personality.json", "relationship.json")]
        before = {path.name: path.read_bytes() if path.exists() else None for path in paths}
        output = {"facts": [], "world": [], "constraints": [], "drop": [], "self": [],
                  "slots": [], "continuity": [self.update()]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")):
            self.service.finish_turn("周末你想去哪儿", "我想去书店翻翻书")
        self.assertEqual(len(self.service.continuity.items()), 1)
        self.assertEqual(self.service.personality.data, original_data)
        after = {path.name: path.read_bytes() if path.exists() else None for path in paths}
        self.assertEqual(after, before)

    def test_finish_turn_captures_her_choice_with_the_existing_single_extraction(self):
        output = {"facts": [], "world": [], "constraints": [], "drop": [], "self": [],
                  "slots": [], "continuity": [self.update()]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")) as extraction:
            self.service.finish_turn("周末你想去哪儿", "我想去书店翻翻书")
        self.assertEqual(extraction.call_count, 1)
        self.assertEqual(self.service.store._llm_calls, 1)
        rows = self.service.continuity.items()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor"], rows[0]["seq"], rows[0]["state"]),
                         ("assistant", 1, "intention"))
        prompt = self.service.system_prompt("书店还想去吗")
        self.assertIn("我想去书店翻翻书", prompt)
        self.assertIn("意向", prompt)

    def test_finish_turn_can_reconsider_an_open_choice_without_repeating_its_topic(self):
        output = {"continuity": [self.update()]}
        with patch("app.models.memory_write.chat_json", return_value=(output, "")):
            self.service.finish_turn("周末你想去哪儿", "我想去书店翻翻书")
        initial = self.service.continuity.items()
        self.assertEqual(len(initial), 1)
        row = initial[0]
        received = []
        changed_quote = "今天犯懒，还是在家看书吧"

        def extract(client, model, messages, **kwargs):
            block = messages[0]["content"].split("【已知未完话题（只能使用这些 id 更新）】\n", 1)[1]
            known = json.loads(block)
            received.extend(known)
            updates = [self.update(changed_quote, state="decision", topic_id=row["id"])] \
                if any(item["id"] == row["id"] for item in known) else []
            return {"continuity": updates}, ""

        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", side_effect=extract) as extraction:
            self.service.finish_turn("你考虑好了吗", changed_quote)
        self.assertEqual(extraction.call_count, 1)
        self.assertLessEqual(len(received), self.service.continuity.context_limit)
        self.assertEqual(received[0]["quote"], row["quote"])
        self.assertEqual(received[0]["state"], "intention")
        self.assertEqual(self.service.continuity.get(row["id"])["quote"], changed_quote)

    def test_finish_turn_rejects_assistant_quote_outside_extraction_window(self):
        answer = "先聊别的。" * 300 + "我想去书店翻翻书"
        with patch("app.models.memory_write.chat_json", return_value=({"continuity": [self.update()]}, "")):
            self.service.finish_turn("周末想去哪儿", answer)
        self.assertEqual(self.service.continuity.items(), [])

    def test_system_prompt_receives_relevant_life_tastes(self):
        with patch.object(self.service.life, "script", return_value={"tastes": "喝茶喜欢乌龙茶；周末喜欢打球"}):
            prompt = self.service.system_prompt("乌龙茶还是咖啡，你来选")
        evidence = prompt.split("【你的选择依据】", 1)
        self.assertEqual(len(evidence), 2)
        self.assertIn("喝茶喜欢乌龙茶", evidence[1])
        self.assertNotIn("周末喜欢打球", evidence[1])

    def test_assistant_choice_rebuilds_after_clear_and_keeps_invalid_old_evidence(self):
        original = self.first_choice()
        self.service.store.clear_session()
        self.service.store.save()
        source = self.turn("周末想去哪儿", "我决定去书店", timestamp=self.now + 600)
        source["source_ts"] = self.now + 600
        accepted = self.service.continuity.apply(
            [self.update("我决定去书店", state="decision")], now=self.now + 600, **source)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["id"], original["id"])
        self.assertGreater(accepted[0]["revision"], original["revision"])
        history = getattr(self.service.continuity, "history", None)
        self.assertTrue(callable(history), "重建后仍保留失效的旧证据")
        evidence = history(original["id"])
        self.assertFalse(evidence[0]["source_valid"])
        self.assertTrue(evidence[1]["source_valid"])

    def test_closed_or_expired_assistant_choices_are_not_unprompted_update_context(self):
        row = self.first_choice()
        self.apply(self.update("书店不去了，这事结束了", state="decision",
                               status="resolved", topic_id=row["id"]))
        with patch.object(self.service.store, "recall", return_value=[]), \
                patch("app.models.memory_write.chat_json", return_value=({}, "")) as extraction:
            self.service.store._extract("你考虑好了吗", "今天在家")
        block = extraction.call_args.args[2][0]["content"].split(
            "【已知未完话题（只能使用这些 id 更新）】\n", 1)[1]
        self.assertEqual(json.loads(block), [])


class ChoiceEvidenceProjectionTests(unittest.TestCase):
    def test_open_drink_choice_receives_existing_drink_preferences_only(self):
        hint = agency.preference_hint(
            "你想喝什么", [], [], tastes="喜欢喝乌龙茶；周末喜欢打球；晚饭爱吃面条")
        self.assertIn("喜欢喝乌龙茶", hint)
        self.assertNotIn("周末喜欢打球", hint)
        self.assertNotIn("晚饭爱吃面条", hint)

    def test_open_food_choice_receives_existing_food_preferences_only(self):
        hint = agency.preference_hint(
            "你想吃什么", [], [], tastes="喜欢喝乌龙茶；周末喜欢打球；晚饭爱吃面条")
        self.assertIn("晚饭爱吃面条", hint)
        self.assertNotIn("喜欢喝乌龙茶", hint)
        self.assertNotIn("周末喜欢打球", hint)

    def test_open_activity_choice_receives_existing_activity_preferences_only(self):
        hint = agency.preference_hint(
            "周末你想做点什么", [], [], tastes="喜欢喝乌龙茶；周末喜欢打球；晚饭爱吃面条")
        self.assertIn("周末喜欢打球", hint)
        self.assertNotIn("喜欢喝乌龙茶", hint)
        self.assertNotIn("晚饭爱吃面条", hint)

    def test_choice_hint_selects_only_relevant_preferences_and_life_facts(self):
        hint = agency.preference_hint(
            "书店还是球场，你来选", ["喜欢安静的书店", "喜欢逗猫"],
            [{"text": "下午在书店翻了本画册", "source": "model"},
             {"text": "晚上在厨房煮面", "source": "model"},
             {"text": "昨天去了书店", "source": "fallback"}])
        self.assertIn("喜欢安静的书店", hint)
        self.assertIn("下午在书店翻了本画册", hint)
        self.assertNotIn("喜欢逗猫", hint)
        self.assertNotIn("晚上在厨房煮面", hint)
        self.assertNotIn("昨天去了书店", hint)
        self.assertNotIn("先说你的倾向", hint)

    def test_related_life_tastes_are_available_without_prescribed_answer_order(self):
        try:
            hint = agency.preference_hint(
                "乌龙茶还是咖啡，你来选", [], [],
                tastes="喝茶喜欢乌龙茶；周末喜欢打球")
        except TypeError as error:
            self.fail(f"选择依据尚未接入生活脚本已有偏好：{error}")
        self.assertIn("乌龙茶", hint)
        self.assertNotIn("周末喜欢打球", hint)
        self.assertNotIn("先说你的倾向", hint)


if __name__ == "__main__":
    unittest.main()
