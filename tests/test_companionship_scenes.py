"""相处背景保留现有关系与关心方式，回应动作由模型结合原话决定。"""

import datetime as dt
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.services.chat_service import ChatService
from tests.test_proactive_personality import RecordingClient


class CompanionshipSceneTests(unittest.TestCase):
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
        self.service.relationship.set_stage("熟络")
        self.service.relationship.set_stage("she")
        self.now = dt.datetime(2026, 10, 15, 15, 0).timestamp()

    def prompts(self, question):
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 60, "afternoon", "刚听到首想和他分享的歌", "model"))
        client = RecordingClient()
        self.service.speaker.speak(client, "test-model", chat_lines=[f"他：{question}"], now=self.now)
        return {
            "conversation": self.service.system_prompt(question),
            "proactive": client.prompts[-1],
        }

    def test_venting_keeps_confirmed_care_without_selecting_a_response_script(self):
        for entry, prompt in self.prompts("今天被同学当众甩锅，气死我了，只想你陪我待会儿").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：倾诉】", prompt)
                self.assertNotIn("【这一刻怎么陪他：明确求助】", prompt)
                self.assertIn("他诉苦时先陪着，明确想要办法时再一起想", prompt)

    def test_help_request_keeps_care_style_without_adding_ordered_steps(self):
        for entry, prompt in self.prompts("明天汇报好紧张，你帮我想想怎么开口").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：明确求助】", prompt)
                self.assertNotIn("【这一刻怎么陪他：倾诉】", prompt)
                self.assertIn("他诉苦时先陪着，明确想要办法时再一起想", prompt)

    def test_affection_is_reciprocal_at_familiar_stage_without_advancing_or_rewriting_data(self):
        paths = [self.directory / name for name in ("personality.json", "relationship.json")]
        before = {path.name: path.read_bytes() for path in paths}
        for entry, prompt in self.prompts("有点想你了，你想不想我").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：亲近表达】", prompt)
                self.assertIn("【你愿意怎么靠近他】", prompt)
                self.assertIn("想念", prompt)
                self.assertIn("按当前称呼", prompt)
                self.assertIn("现在算是「熟络」", prompt)
        self.assertEqual(self.service.relationship.data["stage"], "熟络")
        self.assertEqual(self.service.relationship.data["decide"], "she")
        for path in paths:
            self.assertEqual(path.read_bytes(), before[path.name])

    def test_unfamiliar_stage_does_not_receive_familiar_affection_instruction(self):
        self.service.relationship.set_stage("刚认识")
        for entry, prompt in self.prompts("有点想你了").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：亲近表达】", prompt)
                self.assertIn("现在算是「刚认识」", prompt)

    def test_ordinary_contact_can_express_a_personal_wish_without_inventing_life(self):
        for entry, prompt in self.prompts("刚回来，今天没什么特别的").items():
            with self.subTest(entry=entry):
                self.assertIn("【你愿意怎么靠近他】", prompt)
                self.assertIn("小撒娇", prompt)
                self.assertIn("不查岗", prompt)
                self.assertIn("不编", prompt)

    def test_proactive_shared_experiences_require_evidence_instead_of_a_blanket_ban(self):
        prompt = self.prompts("还记得前晚我们在楼下石桌边一起吃宵夜吗")["proactive"]
        self.assertIn("共同经历只回扣当前提供的明确记录", prompt)
        self.assertIn("没有记录就不补细节", prompt)
        self.assertNotIn('不要提"你们一起做过什么"', prompt)
        self.assertNotIn("加起来别超过 60 字", prompt)

    def test_segment_summary_is_general_background_not_a_specific_completion(self):
        with self.service.store.db:
            self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now, "afternoon", "这几天在学校上课，空闲时打球", "catchup_summary"))
        prompt = self.prompts("下午好")["proactive"]
        self.assertIn("分段补记摘要，只支持概况不支持某格具体完成", prompt)

    def test_custom_care_style_is_not_replaced_by_familiarity_hints(self):
        self.service.relationship.data["care_style"] = "安静听着，不撒娇，不表达想念"
        prompt = self.prompts("有点想你了")["conversation"]
        self.assertIn("安静听着，不撒娇，不表达想念", prompt)
        self.assertNotIn("【你愿意怎么靠近他】", prompt)
        self.assertNotIn("【这一刻怎么陪他：亲近表达】", prompt)

    def test_closed_or_improved_topic_does_not_receive_a_new_venting_prompt(self):
        for question in ("已经好多了，不难受了", "这件事翻篇了，不想再聊那次甩锅"):
            for entry, prompt in self.prompts(question).items():
                with self.subTest(question=question, entry=entry):
                    self.assertNotIn("【这一刻怎么陪他：倾诉】", prompt)

    def test_company_request_is_not_classified_into_a_response_script(self):
        for entry, prompt in self.prompts("不用给我建议，也别分析了，听我说就好").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：倾诉】", prompt)
                self.assertNotIn("【这一刻怎么陪他：明确求助】", prompt)
                self.assertIn("他诉苦时先陪着，明确想要办法时再一起想", prompt)

    def test_explicitly_declining_affection_is_not_treated_as_an_invitation(self):
        for entry, prompt in self.prompts("别说想我了，我不想聊这些，先自己待会儿").items():
            with self.subTest(entry=entry):
                self.assertNotIn("【这一刻怎么陪他：亲近表达】", prompt)
                self.assertIn("【你愿意怎么靠近他】", prompt)


if __name__ == "__main__":
    unittest.main()
