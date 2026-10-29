"""两个生成入口读取当前人格；测试只使用临时数据和模型替身。"""

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
from app.models.personality import Personality
from app.services.chat_service import ChatService
from app.services.speak import Speaker


PERSONA = {
    "name": "测试小夏",
    "identity": "住在隔壁、喜欢画画的大学生",
    "traits": ["留意他提过的小事", "有自己的想法，也愿意听他讲"],
    "tone": "温和随意，熟悉后会开小玩笑",
    "values": ["答应的事情认真记住"],
    "behavior": ["他在说自己的事情时先听他讲", "没有记录的事情不补编"],
    "speech_habits": ["不把关心写成工作汇报", "可以说完就停下"],
    "origin": "从小在海边城市长大",
    "family": "家人住在另一个城市",
    "daily_life": "周末固定参加素描练习",
    "cares": ["家里的小乌龟"],
    "favorite_places": ["校门口的旧书摊"],
    "greeting": ["用于开场白的测试句子"],
    "self_attitude": {
        "default": "身份闲聊测试回应",
        "pressed": "身份追问测试回应",
        "fallback": "身份反复追问测试回应",
    },
    "scope": {
        "can": ["知识边界可聊测试项"],
        "cannot": ["知识边界不可聊测试项"],
        "boundary": "遇到知识边界时的测试处理方式",
    },
    "version": 1,
}


class RecordingClient:
    def __init__(self):
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.prompts.append("\n".join(message["content"] for message in kwargs["messages"]))
        content = json.dumps({"speak": True, "text": "刚听到首歌，想和你分享", "reminder_ids": []},
                             ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class ProactivePersonalityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        settings = {
            "DATA_DIR": str(self.directory),
            "API_KEY": "test-key", "CHAT_API_KEY": "test-key", "EMBED_API_KEY": "test-key",
            "AMAP_KEY": "", "AMAP_CITY": "", "AUTO_EMOTION": False,
        }
        for name, value in settings.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch("httpx.Client.send", side_effect=AssertionError("测试禁止联网")))
        self.stack.enter_context(patch("requests.sessions.Session.request",
                                      side_effect=AssertionError("测试禁止联网")))
        (self.directory / "personality.json").write_text(
            json.dumps({"current": PERSONA, "history": []}, ensure_ascii=False), encoding="utf-8")
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.client = RecordingClient()
        self.now = dt.datetime(2026, 10, 7, 15, 0).timestamp()

    def proactive_prompt(self, service=None):
        service = service or self.service
        with service.store._lock, service.store.db:
            service.store.db.execute(
                "INSERT INTO life_events(ts,slot,text,source) VALUES(?,?,?,?)",
                (self.now - 60, "afternoon", "刚听到首想和他分享的歌", "model"))
        source = service.speaker
        source.speak(self.client, "test-model", chat_lines=["他：今天有点累"], now=self.now)
        return self.client.prompts[-1]

    def test_current_personality_reaches_both_prompts(self):
        proactive = self.proactive_prompt()
        conversation = self.service.system_prompt("今天有点累")
        for text in ("测试小夏", "住在隔壁、喜欢画画的大学生", "留意他提过的小事",
                     "有自己的想法，也愿意听他讲", "温和随意，熟悉后会开小玩笑",
                     "答应的事情认真记住", "他在说自己的事情时先听他讲",
                     "没有记录的事情不补编", "不把关心写成工作汇报", "可以说完就停下"):
            with self.subTest(text=text):
                self.assertIn(text, conversation)
                self.assertIn(text, proactive, "主动开口遗漏了当前人格内容")

    def test_evolved_personality_reaches_next_proactive_prompt(self):
        before = self.proactive_prompt()
        self.service.personality.evolve({"tone": "语气放松，熟悉时会逗他一句"}, reason="隔离测试")
        after = self.proactive_prompt()
        self.assertIn("语气放松，熟悉时会逗他一句", after, "下一次主动开口仍未读取更新的人格")
        self.assertNotIn("温和随意，熟悉后会开小玩笑", after)
        self.assertIn("温和随意，熟悉后会开小玩笑", before)
        self.assertIn("语气放松，熟悉时会逗他一句", self.service.system_prompt())

    def test_replaced_personality_reaches_next_proactive_prompt(self):
        before = self.proactive_prompt()
        replacement = Personality(path=str(self.directory / "replacement.json"))
        replacement.evolve({"tone": "替换人格后的轻松语气"}, reason="隔离测试")
        self.service.personality = replacement
        after = self.proactive_prompt()
        self.assertIn("替换人格后的轻松语气", after, "主动开口不能缓存已被替换的人格对象")
        self.assertNotIn("温和随意，熟悉后会开小玩笑", after)
        self.assertIn("温和随意，熟悉后会开小玩笑", before)
        self.assertIn("替换人格后的轻松语气", self.service.system_prompt())

    def test_current_name_overrides_cached_life_name(self):
        self.service.life.her_name = "旧生活实例名字"
        prompt = self.proactive_prompt()
        self.assertIn("你是「测试小夏」", prompt, "主动入口的名字必须取当前人格")
        self.assertNotIn("旧生活实例名字", prompt)

    def test_proactive_omits_conversation_only_sections(self):
        proactive = self.proactive_prompt()
        conversation = self.service.system_prompt()
        self.assertIn("不把关心写成工作汇报", proactive)
        for text in ("从小在海边城市长大", "家人住在另一个城市", "周末固定参加素描练习",
                     "家里的小乌龟", "校门口的旧书摊", "身份闲聊测试回应", "身份追问测试回应",
                     "身份反复追问测试回应", "知识边界可聊测试项", "知识边界不可聊测试项",
                     "遇到知识边界时的测试处理方式"):
            with self.subTest(text=text):
                self.assertIn(text, conversation)
                self.assertNotIn(text, proactive)
        self.assertNotIn("用于开场白的测试句子", proactive)

    def test_users_keep_their_own_personality(self):
        second = ChatService(user_id="second")
        self.addCleanup(second.close)
        second.personality.evolve({"tone": "另一位用户的人格语气"}, reason="隔离测试")
        first_prompt = self.proactive_prompt()
        second_prompt = self.proactive_prompt(service=second)
        self.assertIn("温和随意，熟悉后会开小玩笑", first_prompt)
        self.assertNotIn("另一位用户的人格语气", first_prompt)
        self.assertIn("另一位用户的人格语气", second_prompt)
        self.assertNotIn("温和随意，熟悉后会开小玩笑", second_prompt)
        self.assertNotIn("不把关心写成工作汇报", second_prompt)

    def test_standalone_speaker_without_personality_remains_usable(self):
        standalone = Speaker(self.service.store.db, self.service.life)
        replies = standalone.speak(self.client, "test-model", now=self.now)
        self.assertEqual(replies, ["刚听到首歌，想和你分享"])
        self.assertIn("你是「测试小夏」", self.client.prompts[-1])


if __name__ == "__main__":
    unittest.main()
