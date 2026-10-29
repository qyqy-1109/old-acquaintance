"""主动生成先读用户原话，生活素材带来源，完成说法不能由候选补编。"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService


class OutputClient:
    def __init__(self, text, motive_ids):
        self.text = text
        self.motive_ids = motive_ids
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"speak": True, "text": self.text, "reminder_ids": [],
             "motive_ids": self.motive_ids}, ensure_ascii=False)))])


class ProactiveFocusTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key", "CHAT_API_KEY": "test-key",
            "EMBED_API_KEY": "test-key", "AMAP_KEY": "", "AMAP_CITY": "", "AUTO_EMOTION": False,
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch("httpx.Client.send", side_effect=AssertionError("测试禁止联网")))
        self.stack.enter_context(patch("requests.sessions.Session.request",
                                      side_effect=AssertionError("测试禁止联网")))
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.now = dt.datetime(2026, 10, 9, 15, 0)
        self.service.life.advance(client=None, now=self.now, force=True)
        self.record("在窗边读了几页小说", source="model")

    def generate(self, text, chat_lines=None):
        candidates = self.service.speaker.motive_candidates(now=self.now.timestamp())
        client = OutputClient(text, [candidates[0]["id"]])
        output = self.service.speaker.speak(client, "test-model", chat_lines=chat_lines,
                                            now=self.now.timestamp())
        prompt = client.requests[-1]["messages"][0]["content"]
        return output, prompt

    def record(self, text, source="model", hidden=False, at=None):
        with self.service.store.db:
            cursor = self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source,hidden_at) VALUES(?,?,?,?,?)",
                (at or self.now.timestamp(), "afternoon", text, source,
                 self.now.timestamp() if hidden else None))
        return cursor.lastrowid

    def test_latest_his_line_is_the_follow_up_start_not_her_reply(self):
        _, prompt = self.generate("汇报辛苦了，想说我就在", [
            "他：明天要汇报，有点紧张", "我：我下午想打球", "他：现在心里还是有点堵", "我：慢慢说呀",
        ])
        self.assertIn("【本次接话的起点】\n他：现在心里还是有点堵", prompt)
        self.assertNotIn("【本次接话的起点】\n我：", prompt)
        self.assertIn("优先接他在意的地方", prompt)
        self.assertNotIn("报备和分享是最自然的一种", prompt)

    def test_without_his_line_sharing_remains_an_option(self):
        _, prompt = self.generate("忽然想跟你聊两句", ["我：我刚说过的近况"])
        self.assertIn("【本次接话的起点】\n（没有近期用户原话，可以分享自己的想法或已确认近况）", prompt)
        self.assertIn("报备", prompt)
        self.assertIn("可以没有问句", prompt)

    def test_fallback_and_explicit_records_keep_distinct_sources_in_prompt(self):
        self.record("午休后在图书馆翻了会儿书", source="model")
        self.record("花已浇过", source="conversation")
        self.record("不能再当当前素材的隐藏记录", hidden=True)
        _, prompt = self.generate("有点想跟你说话")
        self.assertIn("【你自己此刻的状态】（活动未确认：系统兜底候选", prompt)
        self.assertIn("（系统兜底候选，未确认发生）下午：", prompt)
        self.assertIn("（生活记录）午休后在图书馆翻了会儿书", prompt)
        self.assertIn("（对话中确认）花已浇过", prompt)
        self.assertNotIn("不能再当当前素材的隐藏记录", prompt)

    def test_fallback_cannot_support_a_completed_game_but_the_other_bubble_survives(self):
        output, _ = self.generate("刚打完球，一身汗\n@@@\n明天汇报我给你加油呀")
        self.assertEqual(output, ["明天汇报我给你加油呀"])
        self.assertEqual(self.service.speaker.last_grounding, ["刚打完球，一身汗"])
        events = self.service.store.db.execute(
            "SELECT payload FROM events WHERE kind='proactive_grounding_drop'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload"])["dropped"], ["刚打完球，一身汗"])

    def test_rejected_batch_does_not_choose_silence_or_mutate_life_and_quota(self):
        state = self.service.life.state()
        life = self.service.life.recent_events(6)
        output, _ = self.generate("刚在球场打完两场，热死了")
        self.assertEqual(output, [])
        self.assertIsNone(self.service.speaker.last_decision)
        self.assertEqual(self.service.life.state(), state)
        self.assertEqual(self.service.life.recent_events(6), life)
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.service.speaker._said_today(self.now.timestamp()), 0)

    def test_explicit_completed_record_supports_completion_without_extra_game_count(self):
        self.record("我刚打完球，回到屋里休息", source="conversation")
        output, _ = self.generate("刚打完篮球，想歇会儿")
        self.assertEqual(output, ["刚打完篮球，想歇会儿"])
        output, _ = self.generate("刚在球场打完两场，热死了")
        self.assertEqual(output, [], "记录只说打完球，不能补出两场")

    def test_recorded_game_count_supports_the_same_count_only(self):
        self.record("今天打了两场篮球，刚歇下", source="model")
        output, _ = self.generate("打完2场球啦，我先歇会儿")
        self.assertEqual(output, ["打完2场球啦，我先歇会儿"])
        output, _ = self.generate("刚打了三场球，好累")
        self.assertEqual(output, [])

    def test_plans_in_progress_and_hidden_records_cannot_support_completion(self):
        for source, text, hidden in (
            ("model", "准备打完球再去吃饭", False),
            ("dedup", "正在球场打球", False),
            ("model", "打完球啦", True),
        ):
            self.record(text, source=source, hidden=hidden)
        output, _ = self.generate("打完球了，歇一会儿")
        self.assertEqual(output, [])

    def test_old_completed_record_cannot_support_a_current_completion(self):
        self.record("昨天在球场最后投了会儿篮，打完球就回屋了",
                    at=(self.now - dt.timedelta(days=1)).timestamp())
        output, _ = self.generate("刚打完篮球，一身汗")
        self.assertEqual(output, [])

    def test_his_game_negated_and_fallback_records_do_not_confirm_her_game(self):
        for source, text in (("conversation", "他刚打完球"), ("model", "我还没打完球"),
                             ("fallback", "我刚打完篮球")):
            self.record(text, source=source)
        output, _ = self.generate("刚打完篮球，一身汗")
        self.assertEqual(output, [])

    def test_tick_rejects_unconfirmed_completion_before_pending_or_chat_write(self):
        before = self.service.store.db.execute("SELECT count(*) FROM messages").fetchone()[0]
        candidates = self.service.speaker.motive_candidates(now=self.now.timestamp())
        self.service.client = OutputClient("刚在球场打完两场，热死了", [candidates[0]["id"]])
        with patch("time.time", return_value=self.now.timestamp()), patch.object(
            self.service.speaker, "should_speak", return_value=(True, "隔离机会", {"gate": "probe"})
        ):
            output = self.service.proactive_tick()
        self.assertEqual(output, "")
        self.assertEqual(self.service.speaker.pending_items(), [])
        self.assertEqual(self.service.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], before)
        self.assertEqual(self.service.speaker._said_today(self.now.timestamp()), 0)
        self.assertEqual(self.service.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='proactive_grounding_drop'").fetchone()[0], 1)

    def test_negation_future_condition_and_his_game_do_not_get_blocked(self):
        for text in ("我还没打完球", "等我打完球再聊", "我打完球就回去", "你打完球了吗？",
                     "你刚打完球了就好好歇着", "我刚才想打完球再吃饭",
                     "打完球的你累了吧", "同学打完球了，我还在家"):
            with self.subTest(text=text):
                output, _ = self.generate(text)
                self.assertEqual(output, [text])

    def test_retrospective_clause_is_still_a_completion_claim(self):
        for text in ("我刚打完球之后回了屋", "我已经打完球了，再歇会儿"):
            with self.subTest(text=text):
                output, _ = self.generate(text)
                self.assertEqual(output, [])


if __name__ == "__main__":
    unittest.main()
