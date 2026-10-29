"""正向相处方式进入两个生成入口，已有用户配置不会被默认项覆盖。"""

import copy
import datetime as dt
import json
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


FOLLOW_HIM = "他在说自己的事时，先回应他最在意的地方，顺着他的事聊；自己的近况等合适时机再分享。"
LISTEN_OR_HELP = "他只是诉苦时，先陪他把话说完；他明确想要办法时，再一起想自己懂的可行做法。"
OWN_JUDGMENT = "有自己的想法，赞成就直接说，意见不同也坦率讲理由，同时愿意听他的解释。"
RELATION_CARE = "他诉苦时先陪着，明确想要办法时再一起想"


class CareStyleTests(unittest.TestCase):
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

    def prompts(self, service=None):
        service = service or self.service
        now = dt.datetime(2026, 10, 9, 15, 0).timestamp()
        with service.store._lock, service.store.db:
            service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (now - 60, "afternoon", "刚听到首想和他分享的歌", "model"))
        client = RecordingClient()
        service.speaker.speak(client, "test-model", chat_lines=["他：今天有点累"],
                              now=now)
        return {
            "conversation": service.system_prompt("今天有点累"),
            "proactive": client.prompts[-1],
        }

    def test_following_his_topic_reaches_both_generation_prompts(self):
        for scene, prompt in self.prompts().items():
            with self.subTest(scene=scene):
                self.assertIn(FOLLOW_HIM, prompt)

    def test_complaining_and_help_requests_have_consistent_care_in_both_prompts(self):
        for scene, prompt in self.prompts().items():
            with self.subTest(scene=scene):
                self.assertIn(LISTEN_OR_HELP, prompt)
                self.assertIn(RELATION_CARE, prompt)
                self.assertNotIn("再顺口给个建议", prompt)

    def test_her_own_judgment_reaches_both_generation_prompts(self):
        for scene, prompt in self.prompts().items():
            with self.subTest(scene=scene):
                self.assertIn(OWN_JUDGMENT, prompt)

    def test_long_fact_guard_and_professional_boundary_remain_in_their_prompts(self):
        prompts = self.prompts()
        for scene, prompt in prompts.items():
            with self.subTest(scene=scene):
                self.assertIn("别顺着对方补一个具体结果", prompt)
                self.assertIn("别顺着话头默认它发生过", prompt)
        self.assertIn("不要算数字", prompts["conversation"])
        self.assertIn("偶尔撒娇、小吃醋，但不查岗、不追问到底", prompts["proactive"])

    def test_existing_users_keep_their_explicit_personality_and_care_style(self):
        directory = Path(config.user_data_dir("custom"))
        personality = {
            "current": copy.deepcopy(self.service.personality.data),
            "history": [{"data": {"tone": "旧自定义语气"}, "reason": "自定义历史", "ts": 1}],
        }
        personality["current"]["behavior"] = ["按这位用户自己选的方式相处"]
        personality["current"]["speech_habits"] = ["用这位用户确认过的说话方式"]
        relationship = copy.deepcopy(self.service.relationship.data)
        relationship["care_style"] = "这位用户确认过的关心方式"
        for name, data in (("personality.json", personality), ("relationship.json", relationship)):
            (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = {name: (directory / name).read_bytes()
                  for name in ("personality.json", "relationship.json")}
        custom = ChatService(user_id="custom")
        self.addCleanup(custom.close)
        for scene, prompt in self.prompts(custom).items():
            with self.subTest(scene=scene):
                self.assertIn("按这位用户自己选的方式相处", prompt)
                self.assertIn("这位用户确认过的关心方式", prompt)
                self.assertNotIn(FOLLOW_HIM, prompt)
                self.assertNotIn(LISTEN_OR_HELP, prompt)
                self.assertNotIn(OWN_JUDGMENT, prompt)
        self.assertEqual(custom.personality.history, personality["history"])
        for name, original in before.items():
            self.assertEqual((directory / name).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
