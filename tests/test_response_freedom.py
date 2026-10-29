"""提示只提供已确认的关系与相关背景，不按情绪词选择回应步骤。"""

import datetime as dt
import os
import shutil
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.models.memory import KIND_CONSTRAINT
from app.services.chat_service import ChatService
from tests.test_proactive_personality import RecordingClient


class ResponseFreedomTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path("_tmptest/natural-193-196") / (
            f"response-test-{os.getpid()}-{id(self)}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        for name, value in {
            "DATA_DIR": str(self.directory), "API_KEY": "test-key", "CHAT_API_KEY": "test-key",
            "EMBED_API_KEY": "test-key", "AMAP_KEY": "", "AMAP_CITY": "", "AUTO_EMOTION": False,
            "INJECT_TIME": True,
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ("httpx.Client.send", "requests.sessions.Session.request", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.now = dt.datetime(2026, 10, 15, 15, 0).timestamp()
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.service.relationship.set_stage("熟络")
        self.service.relationship.set_stage("she")
        self.service.weather.key = "test-weather-key"
        self.service.weather.city = "440300"
        self.service.weather._data = {
            "city": "成都", "date": "2026-10-15", "day": "小雨", "temperature": "24",
        }
        self.service.store.upsert_slots([
            {"entity": "年糕", "attr": "状态", "value": "在家睡觉"},
            {"entity": "用户", "attr": "胃", "value": "已经好转"},
            {"entity": "小兰", "attr": "位置", "value": "在图书馆"},
        ])

    def test_distress_help_and_affection_words_do_not_select_relationship_actions(self):
        relation = self.service.relationship
        expected = relation.render(self.now, scene_text="今天没什么特别的")
        before = Path(relation.path).read_bytes()
        for question in (
            "今天被甩锅，气死我了，陪我待会儿", "明天汇报好紧张，帮我想想怎么开口",
            "有点想你了，抱抱我", "不用建议也别分析，听我说就好", "已经好多了，这事翻篇了",
        ):
            with self.subTest(question=question):
                self.assertEqual(relation.render(self.now, scene_text=question), expected)
        self.assertEqual(Path(relation.path).read_bytes(), before)
        self.assertEqual(relation.data["stage"], "熟络")
        self.assertEqual(relation.data["decide"], "she")

    def test_confirmed_care_style_call_and_affection_permission_remain(self):
        relation = self.service.relationship
        relation.data["call_note"] = "认真说事时叫全名"
        baseline = relation.render(self.now)
        self.assertIn("现在算是「熟络」", baseline)
        self.assertIn("你平常叫他「阿哲」", baseline)
        self.assertIn("认真说事时叫全名", baseline)
        self.assertIn(relation.data["care_style"], baseline)
        self.assertIn("【你愿意怎么靠近他】", baseline)
        relation.data["care_style"] = "安静听着，不撒娇，不表达想念"
        custom = relation.render(self.now, scene_text="抱抱我")
        self.assertIn("安静听着，不撒娇，不表达想念", custom)
        self.assertNotIn("【你愿意怎么靠近他】", custom)

    def test_ordinary_message_omits_weather_and_unrelated_state(self):
        prompt = self.service.system_prompt("给你听一首歌")
        self.assertNotIn("【天气】", prompt)
        self.assertNotIn("【当前状态】", prompt)
        self.assertNotIn("年糕·状态", prompt)
        self.assertNotIn("小兰·位置", prompt)

    def test_weather_question_or_outing_receives_dated_weather(self):
        for question in ("今天成都天气怎么样", "待会儿出门要不要带伞"):
            with self.subTest(question=question):
                prompt = self.service.system_prompt(question)
                self.assertIn("【天气】成都，2026-10-15：小雨，24°C", prompt)
                self.assertNotIn("年糕·状态", prompt)

    def test_state_selection_uses_named_entities_and_attributes(self):
        for question, included, excluded in (
            ("年糕现在怎么样了", "年糕·状态：在家睡觉", "小兰·位置"),
            ("我的胃还需要注意什么", "用户·胃：已经好转", "年糕·状态"),
            ("小兰在哪里", "小兰·位置：在图书馆", "用户·胃"),
        ):
            with self.subTest(question=question):
                prompt = self.service.system_prompt(question)
                self.assertIn(included, prompt)
                self.assertNotIn(excluded, prompt)
                self.assertNotIn("【天气】", prompt)

    def test_hard_constraints_remain_in_model_input_for_unrelated_messages(self):
        self.service.store.memories.append({
            "id": "allergy-fixture", "text": "他对花生过敏", "kind": KIND_CONSTRAINT, "ts": self.now,
        })
        with patch.object(self.service.store, "_query_vec", return_value=None):
            messages, _ = self.service.store.build_messages(
                self.service.system_prompt("给你听一首歌"), "给你听一首歌")
        body = "\n".join(message["content"] for message in messages)
        self.assertIn("【必须遵守的约束】", body)
        self.assertIn("他对花生过敏", body)

    def test_explicit_first_person_state_query_selects_only_the_users_attribute(self):
        self.service.store.upsert_slots([
            {"entity": "用户", "attr": "位置", "value": "市医院"},
            {"entity": "用户", "attr": "状态", "value": "刚办好入院"},
            {"entity": "用户", "attr": "近况", "value": "在准备搬家"},
            {"entity": self.service.personality.display_name(), "attr": "位置", "value": "学校球场"},
        ])
        for question, expected, omitted in (
            ("你还记得我现在的位置吗", "用户·位置：市医院", "用户·状态"),
            ("我的位置你还记得吗", "用户·位置：市医院", "用户·近况"),
            ("我目前的状态你还记得吗", "用户·状态：刚办好入院", "用户·位置"),
            ("我的近况你还记得吗", "用户·近况：在准备搬家", "用户·位置"),
        ):
            with self.subTest(question=question):
                prompt = self.service.system_prompt(question)
                self.assertIn(expected, prompt)
                self.assertNotIn(omitted, prompt)
                self.assertNotIn("小兰·位置", prompt)
                self.assertNotIn("位置：学校球场", prompt)

    def test_first_person_mention_does_not_claim_her_or_third_party_state(self):
        self.service.store.upsert_slots([{"entity": "用户", "attr": "位置", "value": "市医院"}])
        for question in ("我想知道她现在的位置", "我现在想知道小兰的位置", "我想知道你的位置"):
            with self.subTest(question=question):
                prompt = self.service.system_prompt(question)
                self.assertNotIn("用户·位置", prompt)

    def test_generic_state_words_do_not_expand_all_entities(self):
        self.service.store.upsert_slots([{"entity": "用户", "attr": "状态", "value": "刚办好入院"}])
        for question in ("这是什么状态呀", "位置和近况是什么意思"):
            with self.subTest(question=question):
                prompt = self.service.system_prompt(question)
                self.assertNotIn("【当前状态】", prompt)
                self.assertNotIn("用户·状态", prompt)
                self.assertNotIn("小兰·位置", prompt)

    def test_fact_and_shared_experience_boundaries_reach_both_entries(self):
        with self.service.store._lock, self.service.store.db:
            self.service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 60, "afternoon", "刚听到首想和他分享的歌", "model"))
        client = RecordingClient()
        self.service.speaker.speak(client, "test-model", chat_lines=["他：有点想你了"], now=self.now)
        conversation = self.service.system_prompt("有点想你了")
        proactive = client.prompts[-1]
        for prompt in (conversation, proactive):
            self.assertIn("别顺着对方补一个具体结果", prompt)
            self.assertIn("别顺着话头默认它发生过", prompt)
        self.assertIn("系统兜底候选", proactive)
        self.assertIn("共同经历只回扣当前提供的明确记录", proactive)
        self.assertIn("没有记录就不补细节", proactive)

    def test_bubble_protocol_does_not_prescribe_length_or_a_care_example(self):
        hint = self.service.MULTI_MESSAGE_HINT
        self.assertIn("@@@", hint)
        self.assertIn("1~3", hint)
        self.assertNotIn("40 字", hint)
        self.assertNotIn("先去洗个热水澡", hint)
        self.assertNotIn("今天累坏了吧", hint)


if __name__ == "__main__":
    unittest.main()
